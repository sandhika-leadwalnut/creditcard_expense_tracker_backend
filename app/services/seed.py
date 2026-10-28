"""Default master data. Runs on startup and only inserts what is missing,
so anything edited in Settings is never overwritten."""
from datetime import datetime

from ..config import get_settings
from ..security import hash_password

COMPANIES = [
    {"code": "LW", "name": "LeadWalnut", "active": True, "order": 0},
    {"code": "CW", "name": "CodeWalnut", "active": True, "order": 1},
]

# invoice_always: every row in this category needs an invoice, whatever the amount
# subscription: rows default to subscription (invoice available on the vendor portal)
CATEGORIES = [
    {"name": "Subscriptions", "invoice_always": False, "subscription": True, "color": "#4F46E5"},
    {"name": "Vendor Payments", "invoice_always": True, "subscription": False, "color": "#DC2626"},
    {"name": "Travel", "invoice_always": False, "subscription": False, "color": "#0891B2"},
    {"name": "Advertising & Marketing", "invoice_always": False, "subscription": False, "color": "#D97706"},
    {"name": "Food & Meals", "invoice_always": False, "subscription": False, "color": "#16A34A"},
    {"name": "Bank Charges & Taxes", "invoice_always": False, "subscription": False, "color": "#6B7280"},
    {"name": "Office & Misc", "invoice_always": False, "subscription": False, "color": "#9333EA"},
    {"name": "Uncategorized", "invoice_always": False, "subscription": False, "color": "#A3A3A3"},
]

# Rates are editable defaults only; confirm applicability with the CA.
TDS_SECTIONS = [
    {"code": "194C", "name": "Payments to contractors", "rate": 2.0},
    {"code": "194J", "name": "Professional / technical services", "rate": 10.0},
    {"code": "194H", "name": "Commission / brokerage", "rate": 2.0},
    {"code": "194I", "name": "Rent", "rate": 10.0},
    {"code": "195", "name": "Payments to non-residents", "rate": 20.0},
]

# pattern = case-insensitive text to look for in the statement description
VENDOR_RULES = [
    ("MAKEMYTRIP", "MakeMyTrip", "Travel", False),
    ("ZOHO", "Zoho", "Subscriptions", True),
    ("AHREFS", "Ahrefs", "Subscriptions", True),
    ("SEMRUSH", "Semrush", "Subscriptions", True),
    ("ANTHROPIC", "Anthropic (Claude)", "Subscriptions", True),
    ("INSTANTLY", "Instantly", "Subscriptions", True),
    ("TELLA", "Tella", "Subscriptions", True),
    ("ATLASSIAN", "Atlassian", "Subscriptions", True),
    ("LINKEDIN (PGSI)", "LinkedIn", "Advertising & Marketing", False),
    ("IND*LINKEDIN HTTP", "LinkedIn (card verification)", "Bank Charges & Taxes", False),
    ("VIACONUAE", "Viacon UAE", "Vendor Payments", False),
    ("BLUEPRINTS", "Blueprints", "Vendor Payments", False),
    ("THE CODE NEWSLETTER", "The Code Newsletter", "Advertising & Marketing", False),
    ("COFFEE", "Coffee / cafe", "Food & Meals", False),
    ("GST", "GST on card charges", "Bank Charges & Taxes", False),
]

# Fallback when no vendor rule matches: bank's "Spends Area" → our category
SPENDS_AREA_MAP = {
    "travel services": "Travel",
    "qsr": "Food & Meals",
    "restaurants": "Food & Meals",
    "professional services": "Vendor Payments",
}


def seed(db) -> None:
    s = get_settings()
    now = datetime.utcnow()
    for c in COMPANIES:
        db.companies.update_one({"code": c["code"]}, {"$setOnInsert": c}, upsert=True)
    for i, c in enumerate(CATEGORIES):
        db.categories.update_one({"name": c["name"]}, {"$setOnInsert": {**c, "active": True, "order": i}}, upsert=True)
    for t in TDS_SECTIONS:
        db.tds_sections.update_one({"code": t["code"]}, {"$setOnInsert": {**t, "active": True}}, upsert=True)
    if db.vendor_rules.count_documents({}) == 0:
        db.vendor_rules.insert_many(
            [
                {
                    "pattern": p, "vendor": v, "category": c, "is_subscription": sub,
                    "company": None, "tds_section": None, "priority": 100 + len(p),
                    "created_at": now, "source": "seed",
                }
                for p, v, c, sub in VENDOR_RULES
            ]
        )
    if not db.users.find_one({"email": s.admin_email.lower()}):
        db.users.insert_one(
            {
                "email": s.admin_email.lower(), "name": s.admin_name, "role": "admin",
                "password_hash": hash_password(s.admin_password), "active": True, "created_at": now,
            }
        )
