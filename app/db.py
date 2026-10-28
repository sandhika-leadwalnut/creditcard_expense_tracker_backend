"""MongoDB connection + indexes.

Set MONGO_URI=mongomock://localhost to run fully in memory (demo / tests).
"""
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from .config import get_settings

_client = None


def get_client():
    global _client
    if _client is None:
        uri = get_settings().mongo_uri
        if uri.startswith("mongomock://"):
            import mongomock

            _client = mongomock.MongoClient()
        else:
            _client = MongoClient(uri, tz_aware=False)
    return _client


def get_db() -> Database:
    return get_client()[get_settings().mongo_db]


def ensure_indexes(db: Database) -> None:
    db.users.create_index("email", unique=True)
    db.companies.create_index("code", unique=True)
    db.categories.create_index("name", unique=True)
    db.tds_sections.create_index("code", unique=True)
    db.vendor_rules.create_index([("priority", DESCENDING)])
    db.statements.create_index([("card_last4", ASCENDING), ("statement_date", ASCENDING)], unique=True)
    db.transactions.create_index([("statement_id", ASCENDING), ("seq", ASCENDING)])
    db.transactions.create_index([("month", ASCENDING), ("company", ASCENDING), ("category", ASCENDING)])
    db.transactions.create_index("invoice_status")
    db.attachments.create_index("transaction_id")
    db.audit_logs.create_index([("entity_id", ASCENDING), ("at", DESCENDING)])
