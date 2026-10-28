"""Business rules: auto-categorisation, invoice requirement, company allocation, TDS.

All rules live here so they are easy to find and change. `apply_rules` is
called whenever a transaction is created or edited.
"""
import re

from ..config import get_settings
from .seed import SPENDS_AREA_MAP

NOISE = re.compile(r"^(PAYPAL \*|RAZ\*|IND\*|SQ \*|GOOGLE \*)", re.I)


def guess_vendor(raw: str) -> str:
    s = NOISE.sub("", raw).strip()
    s = s.split("*")[0].strip() if "*" in s else s
    words = [w for w in s.split() if not re.fullmatch(r"[\d\-]+|[A-Z]{2}|HTTP\S*|WWW\S*", w)]
    return " ".join(words[:3]).title() or raw[:30]


def match_rule(raw: str, rules: list[dict]) -> dict | None:
    up = raw.upper()
    for r in sorted(rules, key=lambda r: -r.get("priority", 0)):
        if r["pattern"].upper() in up:
            return r
    return None


def auto_tag(txn: dict, rules: list[dict]) -> dict:
    """Fill vendor/category/company/subscription/TDS from vendor rules."""
    if txn["txn_type"] == "payment":
        txn.update(vendor="Card payment", category="Card Payment", excluded=True, is_subscription=False)
        return txn
    rule = match_rule(txn["raw_description"], rules)
    if rule:
        txn["vendor"] = rule["vendor"]
        txn["category"] = rule["category"]
        txn["is_subscription"] = bool(rule.get("is_subscription"))
        if rule.get("company"):
            txn["company"] = rule["company"]
        if rule.get("tds_section"):
            txn["tds"] = {**txn.get("tds", {}), "applicable": True, "section": rule["tds_section"]}
        txn["rule_id"] = str(rule["_id"]) if rule.get("_id") else None
        txn["auto_tagged"] = True
    else:
        txn["vendor"] = guess_vendor(txn["raw_description"])
        txn["category"] = SPENDS_AREA_MAP.get((txn.get("spends_area") or "").lower(), "Uncategorized")
        txn["is_subscription"] = False
        txn["auto_tagged"] = txn["category"] != "Uncategorized"
    return txn


def compute_allocations(txn: dict) -> list[dict]:
    amt = txn["amount"]
    if txn.get("excluded"):
        return []
    if txn.get("company") == "SPLIT":
        split = txn.get("split") or []
        return [{"company": s["company"], "amount": round(amt * s["pct"] / 100, 2)} for s in split]
    if txn.get("company"):
        return [{"company": txn["company"], "amount": amt}]
    return [{"company": "UNASSIGNED", "amount": amt}]


def invoice_requirement(txn: dict, categories: dict) -> tuple[bool, str]:
    threshold = get_settings().invoice_threshold
    if txn.get("excluded"):
        return False, "Card payment (excluded)"
    if txn["txn_type"] == "refund":
        return False, "Refund / credit"
    cat = categories.get(txn.get("category"), {})
    if cat.get("invoice_always"):
        return True, f"{txn['category']}: invoice always required"
    if txn.get("is_subscription"):
        return False, "Subscription: invoice available on vendor portal"
    if abs(txn["amount"]) > threshold:
        return True, f"Above ₹{threshold:,.0f}"
    return False, f"₹{threshold:,.0f} or below"


def compute_tds(txn: dict, sections: dict) -> dict:
    tds = dict(txn.get("tds") or {})
    if not tds.get("applicable"):
        return {"applicable": False}
    sec = sections.get(tds.get("section") or "")
    rate = tds.get("rate")
    if rate is None and sec:
        rate = sec["rate"]
    base = tds.get("base_amount") or abs(txn["amount"])
    manual = tds.get("manual_amount")
    if manual is not None:            # amount typed in by the reviewer wins over the rate calculation
        amount = round(float(manual), 2)
    else:
        amount = round(base * (rate or 0) / 100, 2)
    tds.update(rate=rate, base_amount=base, amount=amount, is_manual=manual is not None)
    tds.setdefault("deposited", False)
    return tds


def apply_rules(txn: dict, categories: dict, sections: dict) -> dict:
    req, reason = invoice_requirement(txn, categories)
    txn["invoice_required"] = req
    txn["invoice_reason"] = reason
    if txn.get("attachment_count", 0) > 0:
        txn["invoice_status"] = "attached"
    else:
        txn["invoice_status"] = "missing" if req else "not_required"
    txn["allocations"] = compute_allocations(txn)
    txn["tds"] = compute_tds(txn, sections)
    return txn


def load_lookups(db):
    cats = {c["name"]: c for c in db.categories.find()}
    secs = {s["code"]: s for s in db.tds_sections.find()}
    return cats, secs
