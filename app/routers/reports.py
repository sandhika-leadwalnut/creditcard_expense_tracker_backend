"""Analytics. Everything is computed with MongoDB aggregation pipelines over
transactions.allocations, so a split row counts correctly towards LW and CW."""
from fastapi import APIRouter, Depends

from ..db import get_db
from ..security import current_user

router = APIRouter(prefix="/reports", tags=["reports"])


def _match(month_from, month_to, company):
    m: dict = {"excluded": {"$ne": True}}
    if month_from or month_to:
        m["month"] = {}
        if month_from:
            m["month"]["$gte"] = month_from
        if month_to:
            m["month"]["$lte"] = month_to
    return m


def _group(db, match, company, key):
    pipe = [{"$match": match}, {"$unwind": "$allocations"}]
    if company:
        pipe.append({"$match": {"allocations.company": company}})
    pipe += [
        {"$group": {"_id": key, "total": {"$sum": "$allocations.amount"}, "ids": {"$addToSet": "$_id"}}},
        {"$sort": {"total": -1}},
    ]
    rows = list(db.transactions.aggregate(pipe))
    for r in rows:  # count distinct transactions (a split row has two allocations)
        r["count"] = len(r.pop("ids"))
    return rows


@router.get("/summary")
def summary(month_from: str | None = None, month_to: str | None = None, company: str | None = None,
            _=Depends(current_user)):
    db = get_db()
    match = _match(month_from, month_to, company)

    by_company = _group(db, match, company, "$allocations.company")
    by_category = _group(db, match, company, "$category")
    by_vendor = _group(db, match, company, "$vendor")[:10]
    trend_raw = _group(db, match, company, {"month": "$month", "company": "$allocations.company"})
    trend: dict = {}
    for r in trend_raw:
        trend.setdefault(r["_id"]["month"], {})[r["_id"]["company"]] = round(r["total"], 2)
    subs = _group(db, {**match, "is_subscription": True}, company, "$vendor")

    cmatch = dict(match)
    if company:
        cmatch["allocations.company"] = company
    inv = {s: db.transactions.count_documents({**cmatch, "invoice_status": s})
           for s in ("missing", "attached", "not_required")}
    tds_rows = list(db.transactions.find({**cmatch, "tds.applicable": True}, {"tds": 1}))

    total = round(sum(r["total"] for r in by_company), 2)
    return {
        "total": total,
        "transaction_count": db.transactions.count_documents(cmatch),
        "by_company": [{"company": r["_id"], "total": round(r["total"], 2), "count": r["count"]} for r in by_company],
        "by_category": [{"category": r["_id"], "total": round(r["total"], 2), "count": r["count"]} for r in by_category],
        "top_vendors": [{"vendor": r["_id"], "total": round(r["total"], 2), "count": r["count"]} for r in by_vendor],
        "subscriptions": [{"vendor": r["_id"], "total": round(r["total"], 2), "count": r["count"]} for r in subs],
        "monthly_trend": [{"month": m, **v} for m, v in sorted(trend.items())],
        "invoices": inv,
        "tds": {
            "count": len(tds_rows),
            "total": round(sum((t["tds"].get("amount") or 0) for t in tds_rows), 2),
            "pending": round(sum((t["tds"].get("amount") or 0) for t in tds_rows if not t["tds"].get("deposited")), 2),
        },
    }


@router.get("/months")
def months(_=Depends(current_user)):
    return sorted(get_db().transactions.distinct("month"))
