import csv
import io
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import get_db
from ..security import can_edit, current_user
from ..services.rules import apply_rules, load_lookups
from ..utils import audit, clean, oid

router = APIRouter(tags=["transactions"])

ALLOWED_DOC_TYPES = ("invoice", "receipt", "supporting", "tds_challan")
ALLOWED_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".xlsx", ".xls", ".csv", ".docx", ".eml")


class SplitPart(BaseModel):
    company: str
    pct: float = Field(gt=0, le=100)


class TdsIn(BaseModel):
    applicable: bool = False
    section: str | None = None
    rate: float | None = None
    base_amount: float | None = None
    manual_amount: float | None = Field(default=None, ge=0)   # TDS amount entered by hand
    deposited: bool = False
    challan_no: str | None = None
    deposit_date: str | None = None


class TxnPatch(BaseModel):
    company: str | None = None              # LW | CW | SPLIT | "" (clear)
    split: list[SplitPart] | None = None
    category: str | None = None
    vendor: str | None = None
    is_subscription: bool | None = None
    notes: str | None = None
    tds: TdsIn | None = None
    reviewed: bool | None = None
    apply_to_vendor: bool = False           # also save company/category as a rule for this vendor


class BulkPatch(BaseModel):
    ids: list[str]
    company: str | None = None
    category: str | None = None
    reviewed: bool | None = None


def build_query(statement_id=None, month_from=None, month_to=None, company=None, category=None,
                invoice_status=None, tds=None, q=None, include_excluded=False):
    query: dict = {}
    if statement_id:
        query["statement_id"] = oid(statement_id)
    if not include_excluded:
        query["excluded"] = {"$ne": True}
    if month_from or month_to:
        query["month"] = {}
        if month_from:
            query["month"]["$gte"] = month_from
        if month_to:
            query["month"]["$lte"] = month_to
    if company:
        query["allocations.company"] = company
    if category:
        query["category"] = category
    if invoice_status:
        query["invoice_status"] = invoice_status
    if tds == "applicable":
        query["tds.applicable"] = True
    elif tds == "pending":
        query["tds.applicable"] = True
        query["tds.deposited"] = False
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"vendor": rx}, {"raw_description": rx}, {"notes": rx}]
    return query


def _ensure_editable(db, txn):
    st = db.statements.find_one({"_id": txn["statement_id"]}, {"status": 1})
    if st and st.get("status") == "locked":
        raise HTTPException(409, "This month is locked. Ask an admin to unlock it to make changes.")


def _validate(db, changes: dict):
    if "company" in changes and changes["company"] is not None:
        codes = {c["code"] for c in db.companies.find({"active": True})}
        if changes["company"] != "SPLIT" and changes["company"] not in codes:
            raise HTTPException(400, f"Unknown company {changes['company']}")
        if changes["company"] == "SPLIT":
            split = changes.get("split") or []
            if round(sum(p["pct"] for p in split), 2) != 100:
                raise HTTPException(400, "Split percentages must add up to 100")
    if "category" in changes and not db.categories.find_one({"name": changes["category"]}):
        raise HTTPException(400, f"Unknown category {changes['category']}")


@router.get("/transactions")
def list_transactions(
    statement_id: str | None = None, month_from: str | None = None, month_to: str | None = None,
    company: str | None = None, category: str | None = None, invoice_status: str | None = None,
    tds: str | None = None, q: str | None = None, include_excluded: bool = False,
    _=Depends(current_user),
):
    db = get_db()
    query = build_query(statement_id, month_from, month_to, company, category, invoice_status, tds, q, include_excluded)
    rows = list(db.transactions.find(query).sort([("txn_date", 1), ("seq", 1)]))
    return clean(rows)


@router.patch("/transactions/{txn_id}")
def update_transaction(txn_id: str, body: TxnPatch, user=Depends(can_edit)):
    db = get_db()
    txn = db.transactions.find_one({"_id": oid(txn_id)})
    if not txn:
        raise HTTPException(404, "Transaction not found")
    _ensure_editable(db, txn)
    changes = body.model_dump(exclude_none=True, exclude={"apply_to_vendor"})
    if "split" in changes and changes.get("company") != "SPLIT" and txn.get("company") != "SPLIT":
        changes.pop("split")
    if changes.get("company") == "":           # clicking the selected company again clears it
        changes["company"] = None
        changes["split"] = []
    elif changes.get("company") and changes["company"] != "SPLIT":
        changes["split"] = []
    _validate(db, {**changes, "split": changes.get("split", txn.get("split"))})
    tds_in = changes.get("tds")
    if tds_in and tds_in.get("applicable") and not tds_in.get("section") and tds_in.get("manual_amount") is None:
        raise HTTPException(400, "Enter the TDS amount or choose a TDS section")

    before = {k: txn.get(k) for k in changes}
    txn.update(changes)
    if "category" in changes and "is_subscription" not in changes:
        cat = db.categories.find_one({"name": changes["category"]})
        txn["is_subscription"] = bool(cat and cat.get("subscription"))
    cats, secs = load_lookups(db)
    apply_rules(txn, cats, secs)
    txn["updated_at"] = datetime.utcnow()
    txn["updated_by"] = user["email"]
    db.transactions.replace_one({"_id": txn["_id"]}, txn)
    audit(db, user, "transaction", txn["_id"], "update", {"before": before, "after": changes})

    if body.apply_to_vendor:
        _learn_rule(db, txn, user)
    return clean(txn)


def _learn_rule(db, txn: dict, user: dict):
    """Remember this vendor → category/company so next month's rows tag themselves."""
    pattern = txn["raw_description"].split("  ")[0]
    pattern = re.sub(r"\s+(\d{4,}|[A-Z]{2})(\s+[A-Z]{2})?$", "", pattern).strip() or txn["raw_description"]
    rule = {
        "pattern": pattern.upper(), "vendor": txn.get("vendor"), "category": txn.get("category"),
        "is_subscription": txn.get("is_subscription", False),
        "company": txn.get("company") if txn.get("company") != "SPLIT" else None,
        "tds_section": (txn.get("tds") or {}).get("section") if (txn.get("tds") or {}).get("applicable") else None,
        "priority": 500 + len(pattern), "source": "learned", "created_by": user["email"],
        "created_at": datetime.utcnow(),
    }
    db.vendor_rules.update_one({"pattern": rule["pattern"]}, {"$set": rule}, upsert=True)
    # apply to other not-yet-reviewed rows of the same vendor in open statements
    cats, secs = load_lookups(db)
    for other in db.transactions.find({"_id": {"$ne": txn["_id"]}, "reviewed": False,
                                       "raw_description": {"$regex": re.escape(pattern), "$options": "i"}}):
        st = db.statements.find_one({"_id": other["statement_id"]}, {"status": 1})
        if st and st.get("status") == "locked":
            continue
        other.update(vendor=rule["vendor"], category=rule["category"], is_subscription=rule["is_subscription"])
        if rule["company"]:
            other["company"] = rule["company"]
            other["split"] = []
        apply_rules(other, cats, secs)
        db.transactions.replace_one({"_id": other["_id"]}, other)


@router.post("/transactions/bulk")
def bulk_update(body: BulkPatch, user=Depends(can_edit)):
    db = get_db()
    changes = body.model_dump(exclude_none=True, exclude={"ids"})
    if changes.get("company") == "SPLIT":
        raise HTTPException(400, "Use the row editor to set a split")
    _validate(db, changes)
    cats, secs = load_lookups(db)
    n = 0
    for tid in body.ids:
        txn = db.transactions.find_one({"_id": oid(tid)})
        if not txn or txn.get("excluded"):
            continue
        _ensure_editable(db, txn)
        txn.update(changes)
        if "company" in changes:
            txn["split"] = []
        if "category" in changes:
            cat = cats.get(changes["category"], {})
            txn["is_subscription"] = bool(cat.get("subscription"))
        apply_rules(txn, cats, secs)
        txn["updated_at"] = datetime.utcnow()
        txn["updated_by"] = user["email"]
        db.transactions.replace_one({"_id": txn["_id"]}, txn)
        n += 1
    audit(db, user, "transaction", ",".join(body.ids), "bulk_update", changes)
    return {"updated": n}


@router.get("/transactions/{txn_id}/history")
def history(txn_id: str, _=Depends(current_user)):
    return clean(list(get_db().audit_logs.find({"entity_id": txn_id}).sort("at", -1)))


# ---------------- attachments ----------------
@router.get("/transactions/{txn_id}/attachments")
def list_attachments(txn_id: str, _=Depends(current_user)):
    rows = list(get_db().attachments.find({"transaction_id": oid(txn_id)}).sort("uploaded_at", -1))
    for r in rows:
        r.pop("stored_path", None)
    return clean(rows)


@router.post("/transactions/{txn_id}/attachments")
async def add_attachment(
    txn_id: str, file: UploadFile = File(...), doc_type: str = Form("invoice"), user=Depends(can_edit)
):
    s = get_settings()
    db = get_db()
    txn = db.transactions.find_one({"_id": oid(txn_id)})
    if not txn:
        raise HTTPException(404, "Transaction not found")
    _ensure_editable(db, txn)
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(400, f"doc_type must be one of {ALLOWED_DOC_TYPES}")
    name = file.filename or "file"
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Allowed file types: {', '.join(ALLOWED_EXT)}")
    data = await file.read()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, f"File is larger than {s.max_upload_mb} MB")

    company = txn.get("company") or "UNASSIGNED"
    folder = os.path.join(s.upload_dir, "invoices", company, txn["month"])
    os.makedirs(folder, exist_ok=True)
    stored = os.path.join(folder, f"{uuid.uuid4().hex}{ext}")
    with open(stored, "wb") as f:
        f.write(data)
    doc = {
        "transaction_id": txn["_id"], "file_name": name, "stored_path": stored,
        "content_type": file.content_type, "size": len(data), "doc_type": doc_type,
        "uploaded_by": user["email"], "uploaded_at": datetime.utcnow(),
    }
    doc["_id"] = db.attachments.insert_one(doc).inserted_id

    txn["attachment_count"] = db.attachments.count_documents({"transaction_id": txn["_id"]})
    cats, secs = load_lookups(db)
    apply_rules(txn, cats, secs)
    db.transactions.replace_one({"_id": txn["_id"]}, txn)
    audit(db, user, "transaction", txn["_id"], "attach", {"file": name, "doc_type": doc_type})
    doc.pop("stored_path")
    return {"attachment": clean(doc), "transaction": clean(txn)}


@router.get("/attachments/{att_id}/download")
def download_attachment(att_id: str, _=Depends(current_user)):
    a = get_db().attachments.find_one({"_id": oid(att_id)})
    if not a or not os.path.exists(a["stored_path"]):
        raise HTTPException(404, "File not found")
    return FileResponse(a["stored_path"], filename=a["file_name"], media_type=a.get("content_type"))


@router.delete("/attachments/{att_id}")
def delete_attachment(att_id: str, user=Depends(can_edit)):
    db = get_db()
    a = db.attachments.find_one({"_id": oid(att_id)})
    if not a:
        raise HTTPException(404, "Not found")
    txn = db.transactions.find_one({"_id": a["transaction_id"]})
    _ensure_editable(db, txn)
    db.attachments.delete_one({"_id": a["_id"]})
    if os.path.exists(a["stored_path"]):
        os.remove(a["stored_path"])
    txn["attachment_count"] = db.attachments.count_documents({"transaction_id": txn["_id"]})
    cats, secs = load_lookups(db)
    apply_rules(txn, cats, secs)
    db.transactions.replace_one({"_id": txn["_id"]}, txn)
    audit(db, user, "transaction", txn["_id"], "detach", {"file": a["file_name"]})
    return clean(txn)


# ---------------- export ----------------
@router.get("/export/transactions.csv")
def export_csv(
    statement_id: str | None = None, month_from: str | None = None, month_to: str | None = None,
    company: str | None = None, category: str | None = None, invoice_status: str | None = None,
    tds: str | None = None, q: str | None = None, _=Depends(current_user),
):
    db = get_db()
    query = build_query(statement_id, month_from, month_to, company, category, invoice_status, tds, q)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Month", "Vendor", "Statement description", "Company", "Allocation", "Category",
                "Subscription", "Type", "Amount (INR)", "FX amount", "FX currency", "Invoice required",
                "Invoice status", "Attachments", "TDS section", "TDS rate %", "TDS amount", "TDS deposited",
                "Card", "Notes"])
    for t in db.transactions.find(query).sort("txn_date", 1):
        tds_ = t.get("tds") or {}
        alloc = "; ".join(f"{a['company']} {a['amount']:.2f}" for a in t.get("allocations", []))
        w.writerow([t["txn_date"], t["month"], t.get("vendor"), t["raw_description"], t.get("company") or "",
                    alloc, t.get("category"), "Yes" if t.get("is_subscription") else "No", t["txn_type"],
                    f"{t['amount']:.2f}", t.get("fx_amount") or "", t.get("fx_currency") or "",
                    "Yes" if t.get("invoice_required") else "No", t.get("invoice_status"),
                    t.get("attachment_count", 0), tds_.get("section") or "", tds_.get("rate") or "",
                    tds_.get("amount") or "", "Yes" if tds_.get("deposited") else ("No" if tds_.get("applicable") else ""),
                    f"XXXX{t.get('card_last4', '')}", t.get("notes", "")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=expenses.csv"})
