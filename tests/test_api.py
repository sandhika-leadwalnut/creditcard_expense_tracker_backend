"""End-to-end API test with an in-memory database.

Run:  SAMPLE_STATEMENT=path/to/statement.pdf pytest -q
(Real statements are not committed to the repo; the upload tests are skipped
when SAMPLE_STATEMENT is not set.)
"""
import os

import pytest

os.environ["MONGO_URI"] = "mongomock://localhost"
os.environ["MONGO_DB"] = "test_db"
os.environ["UPLOAD_DIR"] = "./.test_uploads"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SAMPLE = os.environ.get("SAMPLE_STATEMENT")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "admin@leadwalnut.com", "password": "ChangeMe@123"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c


def test_login_rejects_bad_password():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "admin@leadwalnut.com", "password": "nope"})
        assert r.status_code == 401


def test_requires_auth():
    with TestClient(app) as c:
        assert c.get("/api/statements").status_code == 401


def test_meta(client):
    m = client.get("/api/meta").json()
    assert {c["code"] for c in m["companies"]} == {"LW", "CW"}
    assert any(c["name"] == "Vendor Payments" and c["invoice_always"] for c in m["categories"])


@pytest.mark.skipif(not SAMPLE, reason="SAMPLE_STATEMENT not set")
def test_upload_and_rules(client):
    with open(SAMPLE, "rb") as f:
        r = client.post("/api/statements/upload", files={"file": ("st.pdf", f, "application/pdf")})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["reconciliation"]["ok"], st["reconciliation"]

    with open(SAMPLE, "rb") as f:  # duplicate upload is blocked
        assert client.post("/api/statements/upload", files={"file": ("st.pdf", f, "application/pdf")}).status_code == 409

    rows = client.get("/api/transactions", params={"statement_id": st["id"]}).json()
    by_vendor = {}
    for t in rows:
        by_vendor.setdefault(t["vendor"], []).append(t)
    # vendor payments always need an invoice, subscriptions never do
    assert all(t["invoice_required"] for t in by_vendor["Viacon UAE"])
    assert not any(t["invoice_required"] for t in by_vendor["Semrush"])
    # >10k travel needs invoice, <=10k does not
    mmt = by_vendor["MakeMyTrip"]
    assert all(t["invoice_required"] == (t["amount"] > 10000) for t in mmt)

    # split + TDS + attachment flow
    t = by_vendor["Blueprints"][0]
    r = client.patch(f"/api/transactions/{t['id']}", json={
        "company": "SPLIT", "split": [{"company": "LW", "pct": 60}, {"company": "CW", "pct": 40}],
        "tds": {"applicable": True, "section": "195"}})
    assert r.status_code == 200, r.text
    u = r.json()
    assert round(sum(a["amount"] for a in u["allocations"]), 2) == t["amount"]
    assert u["tds"]["amount"] == round(t["amount"] * 0.20, 2)
    r = client.post(f"/api/transactions/{t['id']}/attachments",
                    files={"file": ("inv.pdf", b"%PDF-1.4 test", "application/pdf")}, data={"doc_type": "invoice"})
    assert r.json()["transaction"]["invoice_status"] == "attached"

    # lock is blocked while items are open
    assert client.post(f"/api/statements/{st['id']}/lock", json={}).status_code == 409
    s = client.get("/api/reports/summary").json()
    assert s["total"] == st["purchases_total"]
