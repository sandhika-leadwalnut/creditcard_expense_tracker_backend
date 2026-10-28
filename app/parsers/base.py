"""Common structures for statement parsers.

Every bank/card format gets its own parser class. A parser:
  * says whether it recognises a PDF (detect)
  * turns the PDF into a ParsedStatement (parse)
Adding a new bank = add one file in this folder and register it in registry.py.
"""
from dataclasses import dataclass, field, asdict
from datetime import date


@dataclass
class ParsedRow:
    seq: int
    txn_date: date
    raw_description: str
    amount: float                 # always positive, as printed on the statement
    is_credit: bool               # "Cr" on statement
    txn_type: str                 # purchase | refund | payment | fee
    section: str = ""
    spends_area: str = ""         # bank's own merchant category (MCC style)
    fx_amount: float | None = None
    fx_currency: str | None = None

    def to_dict(self):
        d = asdict(self)
        d["txn_date"] = self.txn_date.isoformat()
        return d


@dataclass
class ParsedStatement:
    bank: str
    card_type: str
    card_last4: str
    statement_date: date
    period_start: date | None
    period_end: date | None
    total_due: float | None
    purchases_total: float | None
    payments_total: float | None
    rows: list[ParsedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class StatementParser:
    bank: str = ""
    card_type: str = ""

    @classmethod
    def detect(cls, first_page_text: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, pdf) -> ParsedStatement:  # pragma: no cover - interface
        raise NotImplementedError


def money(s: str) -> float:
    s = s.replace("₹", "").replace("Rs.", "").replace(",", "").replace("Cr", "").strip()
    if s.startswith("."):
        s = "0" + s
    return round(float(s or 0), 2)
