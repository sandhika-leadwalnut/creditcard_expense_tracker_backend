from datetime import date, datetime

from bson import ObjectId
from fastapi import HTTPException


def oid(v: str) -> ObjectId:
    try:
        return ObjectId(v)
    except Exception:
        raise HTTPException(404, "Not found")


def clean(doc):
    """Make a Mongo document JSON friendly (ObjectId → str, _id → id)."""
    if isinstance(doc, list):
        return [clean(d) for d in doc]
    if isinstance(doc, dict):
        out = {}
        for k, v in doc.items():
            if k == "password_hash":
                continue
            key = "id" if k == "_id" else k
            out[key] = clean(v)
        return out
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, (datetime, date)):
        return doc.isoformat()
    return doc


def audit(db, user: dict, entity: str, entity_id, action: str, changes: dict | None = None):
    db.audit_logs.insert_one(
        {
            "entity": entity,
            "entity_id": str(entity_id),
            "action": action,
            "changes": changes or {},
            "user_id": str(user["_id"]),
            "user_email": user["email"],
            "at": datetime.utcnow(),
        }
    )
