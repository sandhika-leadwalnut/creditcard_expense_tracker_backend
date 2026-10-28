from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from ..config import get_settings
from ..db import get_db
from ..security import ROLES, create_token, current_user, hash_password, is_admin, verify_password
from ..utils import audit, clean, oid

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserIn(BaseModel):
    email: EmailStr
    name: str
    role: str = "finance"
    password: str = Field(min_length=8)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


@router.post("/auth/login")
def login(body: LoginIn):
    db = get_db()
    user = db.users.find_one({"email": body.email.lower(), "active": True})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Incorrect email or password")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
    return {"access_token": create_token(user), "token_type": "bearer", "user": clean(user)}


@router.get("/auth/me")
def me(user=Depends(current_user)):
    return clean(user)


@router.post("/auth/change-password")
def change_password(body: PasswordIn, user=Depends(current_user)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    get_db().users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    return {"ok": True}


# ---------- user management (admin) ----------
@router.get("/users")
def list_users(_=Depends(is_admin)):
    return clean(list(get_db().users.find().sort("email")))


@router.post("/users")
def create_user(body: UserIn, admin=Depends(is_admin)):
    db = get_db()
    email = body.email.lower()
    if email.split("@")[1] not in get_settings().domains:
        raise HTTPException(400, f"Only these domains are allowed: {', '.join(get_settings().domains)}")
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}")
    if db.users.find_one({"email": email}):
        raise HTTPException(409, "User already exists")
    doc = {
        "email": email, "name": body.name, "role": body.role, "active": True,
        "password_hash": hash_password(body.password), "created_at": datetime.utcnow(),
    }
    doc["_id"] = db.users.insert_one(doc).inserted_id
    audit(db, admin, "user", doc["_id"], "create", {"email": email, "role": body.role})
    return clean(doc)


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserPatch, admin=Depends(is_admin)):
    db = get_db()
    changes = body.model_dump(exclude_none=True)
    if "role" in changes and changes["role"] not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}")
    if "password" in changes:
        changes["password_hash"] = hash_password(changes.pop("password"))
    db.users.update_one({"_id": oid(user_id)}, {"$set": changes})
    audit(db, admin, "user", user_id, "update", {k: v for k, v in changes.items() if k != "password_hash"})
    return clean(db.users.find_one({"_id": oid(user_id)}))
