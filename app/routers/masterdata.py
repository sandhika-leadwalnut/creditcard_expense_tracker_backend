"""Companies, categories, TDS sections and vendor rules: the lists that let
the system grow (new company, new category, new TDS section) without code changes."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..db import get_db
from ..security import can_edit, current_user, is_admin
from ..utils import audit, clean, oid

router = APIRouter(tags=["master data"])


@router.get("/meta")
def meta(_=Depends(current_user)):
    db = get_db()
    return {
        "companies": clean(list(db.companies.find({"active": True}).sort([("order", 1), ("code", 1)]))),
        "categories": clean(list(db.categories.find({"active": True}).sort("order"))),
        "tds_sections": clean(list(db.tds_sections.find({"active": True}).sort("code"))),
        "invoice_threshold": get_settings().invoice_threshold,
    }


class CompanyIn(BaseModel):
    code: str
    name: str
    active: bool = True


@router.post("/companies")
def add_company(body: CompanyIn, user=Depends(is_admin)):
    db = get_db()
    code = body.code.upper().strip()
    if code in ("SPLIT", "UNASSIGNED"):
        raise HTTPException(400, "Reserved code")
    db.companies.update_one({"code": code}, {"$set": {**body.model_dump(), "code": code},
                                             "$setOnInsert": {"order": db.companies.count_documents({})}}, upsert=True)
    audit(db, user, "company", code, "upsert", body.model_dump())
    return clean(db.companies.find_one({"code": code}))


class CategoryIn(BaseModel):
    name: str
    invoice_always: bool = False
    subscription: bool = False
    color: str = "#6B7280"
    active: bool = True


@router.post("/categories")
def upsert_category(body: CategoryIn, user=Depends(is_admin)):
    db = get_db()
    existing = db.categories.find_one({"name": body.name})
    data = body.model_dump()
    if not existing:
        data["order"] = db.categories.count_documents({})
    db.categories.update_one({"name": body.name}, {"$set": data}, upsert=True)
    audit(db, user, "category", body.name, "upsert", data)
    return clean(db.categories.find_one({"name": body.name}))


class TdsSectionIn(BaseModel):
    code: str
    name: str
    rate: float
    active: bool = True


@router.post("/tds-sections")
def upsert_tds(body: TdsSectionIn, user=Depends(is_admin)):
    db = get_db()
    db.tds_sections.update_one({"code": body.code}, {"$set": body.model_dump()}, upsert=True)
    audit(db, user, "tds_section", body.code, "upsert", body.model_dump())
    return clean(db.tds_sections.find_one({"code": body.code}))


class RuleIn(BaseModel):
    pattern: str
    vendor: str
    category: str
    is_subscription: bool = False
    company: str | None = None
    tds_section: str | None = None
    priority: int = 300


@router.get("/vendor-rules")
def list_rules(_=Depends(current_user)):
    return clean(list(get_db().vendor_rules.find().sort("vendor")))


@router.post("/vendor-rules")
def create_rule(body: RuleIn, user=Depends(can_edit)):
    db = get_db()
    doc = {**body.model_dump(), "pattern": body.pattern.upper(), "source": "manual",
           "created_by": user["email"], "created_at": datetime.utcnow()}
    doc["_id"] = db.vendor_rules.insert_one(doc).inserted_id
    audit(db, user, "vendor_rule", doc["_id"], "create", body.model_dump())
    return clean(doc)


@router.put("/vendor-rules/{rule_id}")
def update_rule(rule_id: str, body: RuleIn, user=Depends(can_edit)):
    db = get_db()
    db.vendor_rules.update_one({"_id": oid(rule_id)}, {"$set": {**body.model_dump(), "pattern": body.pattern.upper()}})
    audit(db, user, "vendor_rule", rule_id, "update", body.model_dump())
    return clean(db.vendor_rules.find_one({"_id": oid(rule_id)}))


@router.delete("/vendor-rules/{rule_id}")
def delete_rule(rule_id: str, user=Depends(can_edit)):
    db = get_db()
    db.vendor_rules.delete_one({"_id": oid(rule_id)})
    audit(db, user, "vendor_rule", rule_id, "delete")
    return {"ok": True}
