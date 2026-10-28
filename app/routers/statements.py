import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from ..config import get_settings
from ..db import get_db
from ..parsers.registry import PasswordRequired, UnsupportedStatement, parse_pdf
from ..security import can_edit, current_user, is_admin
from ..services.rules import apply_rules, auto_tag, load_lookups
from ..utils import audit, clean, oid

router = APIRouter(prefix="/statements", tags=["statements"])


def reconciliation(db, st: dict) -> dict:
    txns = list(db.transactions.find({"statement_id": st["_id"], "excluded": {"$ne": True}}, {"amount": 1}))
    captured = round(sum(t["amount"] for t in txns), 2)
    expected = st.get("purchases_total")
    diff = round(captured - expected, 2) if expected is not None else None
    return {"captured_total": captured, "statement_total": expected, "difference": diff,
            "ok": diff is not None and abs(diff) < 1}


def checklist(db, st_id) -> dict:
    q = {"statement_id": st_id, "excluded": {"$ne": True}}
    return {
        "unassigned_company": db.transactions.count_documents({**q, "company": None}),
        "uncategorized": db.transactions.count_documents({**q, "category": "Uncategorized"}),
        "missing_invoices": db.transactions.count_documents({**q, "invoice_status": "missing"}),
        "tds_not_deposited": db.transactions.count_documents({**q, "tds.applicable": True, "tds.deposited": False}),
    }


@router.get("")
def list_statements(_=Depends(current_user)):
    db = get_db()
    out = []
    for st in db.statements.find().sort("statement_date", -1):
        st["checklist"] = checklist(db, st["_id"])
        out.append(st)
    return clean(out)


@router.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    password: str | None = Form(None),
    user=Depends(can_edit),
):
    s = get_settings()
    db = get_db()
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload the statement as a PDF")
    data = await file.read()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, f"File is larger than {s.max_upload_mb} MB")

    folder = os.path.join(s.upload_dir, "statements")
    os.makedirs(folder, exist_ok=True)
    stored = os.path.join(folder, f"{uuid.uuid4().hex}.pdf")
    with open(stored, "wb") as f:
        f.write(data)

    try:
        parsed = parse_pdf(stored, password)
    except PasswordRequired as e:
        os.remove(stored)
        raise HTTPException(422, {"code": "password_required", "message": str(e)})
    except UnsupportedStatement as e:
        os.remove(stored)
        raise HTTPException(422, {"code": "unsupported", "message": str(e)})
    if not parsed.rows:
        os.remove(stored)
        raise HTTPException(422, {"code": "no_rows", "message": "No transactions found in this PDF"})

    now = datetime.utcnow()
    st_doc = {
        "bank": parsed.bank,
        "card_type": parsed.card_type,
        "card_last4": parsed.card_last4,
        "statement_date": parsed.statement_date.isoformat(),
        "period_start": parsed.period_start.isoformat() if parsed.period_start else None,
        "period_end": parsed.period_end.isoformat() if parsed.period_end else None,
        "total_due": parsed.total_due,
        "purchases_total": parsed.purchases_total,
        "payments_total": parsed.payments_total,
        "file_name": file.filename,
        "file_path": stored,
        "status": "in_review",
        "uploaded_by": user["email"],
        "uploaded_at": now,
        "parse_warnings": parsed.warnings,
    }
    try:
        st_id = db.statements.insert_one(st_doc).inserted_id
    except DuplicateKeyError:
        os.remove(stored)
        raise HTTPException(409, {"code": "duplicate", "message": "This statement has already been uploaded"})

    rules = list(db.vendor_rules.find())
    cats, secs = load_lookups(db)
    docs = []
    for r in parsed.rows:
        signed = -r.amount if r.is_credit else r.amount
        t = {
            "statement_id": st_id,
            "seq": r.seq,
            "txn_date": r.txn_date.isoformat(),
            "month": r.txn_date.strftime("%Y-%m"),
            "raw_description": r.raw_description,
            "spends_area": r.spends_area,
            "txn_type": r.txn_type,
            "section": r.section,
            "amount": signed,
            "currency": "INR",
            "fx_amount": r.fx_amount,
            "fx_currency": r.fx_currency,
            "bank": parsed.bank,
            "card_last4": parsed.card_last4,
            "company": None,
            "split": [],
            "excluded": False,
            "is_subscription": False,
            "attachment_count": 0,
            "tds": {"applicable": False},
            "notes": "",
            "reviewed": False,
            "created_at": now,
            "updated_at": now,
        }
        auto_tag(t, rules)
        apply_rules(t, cats, secs)
        docs.append(t)
    db.transactions.insert_many(docs)
    db.statements.update_one({"_id": st_id}, {"$set": {"txn_count": len(docs)}})
    audit(db, user, "statement", st_id, "upload", {"file": file.filename, "rows": len(docs)})
    return get_statement(str(st_id), user)


@router.get("/{statement_id}")
def get_statement(statement_id: str, _=Depends(current_user)):
    db = get_db()
    st = db.statements.find_one({"_id": oid(statement_id)})
    if not st:
        raise HTTPException(404, "Statement not found")
    st["reconciliation"] = reconciliation(db, st)
    st["checklist"] = checklist(db, st["_id"])
    st.pop("file_path", None)
    return clean(st)


@router.get("/{statement_id}/file")
def download_statement(statement_id: str, _=Depends(current_user)):
    st = get_db().statements.find_one({"_id": oid(statement_id)})
    if not st or not os.path.exists(st["file_path"]):
        raise HTTPException(404, "File not found")
    return FileResponse(st["file_path"], filename=st["file_name"], media_type="application/pdf")


class LockIn(BaseModel):
    override_reason: str | None = None


@router.post("/{statement_id}/lock")
def lock_statement(statement_id: str, body: LockIn, user=Depends(can_edit)):
    db = get_db()
    st = db.statements.find_one({"_id": oid(statement_id)})
    if not st:
        raise HTTPException(404, "Statement not found")
    cl = checklist(db, st["_id"])
    blocking = {k: v for k, v in cl.items() if v and k in ("unassigned_company", "uncategorized", "missing_invoices")}
    if blocking:
        if not body.override_reason:
            raise HTTPException(409, {"code": "not_ready", "message": "Month is not ready to lock", "checklist": cl})
        if user["role"] != "admin":
            raise HTTPException(403, "Only an admin can lock with open items")
    db.statements.update_one(
        {"_id": st["_id"]},
        {"$set": {"status": "locked", "locked_by": user["email"], "locked_at": datetime.utcnow(),
                  "lock_override_reason": body.override_reason}},
    )
    audit(db, user, "statement", st["_id"], "lock", {"override_reason": body.override_reason, "checklist": cl})
    return get_statement(statement_id, user)


@router.post("/{statement_id}/unlock")
def unlock_statement(statement_id: str, user=Depends(is_admin)):
    db = get_db()
    db.statements.update_one({"_id": oid(statement_id)}, {"$set": {"status": "in_review"}})
    audit(db, user, "statement", statement_id, "unlock")
    return get_statement(statement_id, user)


@router.delete("/{statement_id}")
def delete_statement(statement_id: str, user=Depends(is_admin)):
    db = get_db()
    st = db.statements.find_one({"_id": oid(statement_id)})
    if not st:
        raise HTTPException(404, "Statement not found")
    if st["status"] == "locked":
        raise HTTPException(409, "Unlock the statement before deleting it")
    txn_ids = [t["_id"] for t in db.transactions.find({"statement_id": st["_id"]}, {"_id": 1})]
    for a in db.attachments.find({"transaction_id": {"$in": txn_ids}}):
        if os.path.exists(a["stored_path"]):
            os.remove(a["stored_path"])
    db.attachments.delete_many({"transaction_id": {"$in": txn_ids}})
    db.transactions.delete_many({"statement_id": st["_id"]})
    db.statements.delete_one({"_id": st["_id"]})
    if os.path.exists(st["file_path"]):
        os.remove(st["file_path"])
    audit(db, user, "statement", statement_id, "delete", {"file": st["file_name"]})
    return {"ok": True}
