"""Picks the right parser for an uploaded PDF.

To support a new bank: create parsers/<bank>.py with a StatementParser subclass
and add it to PARSERS. If no parser matches, the API returns a clear error
(future: fall back to an LLM extractor such as Claude API or Unstract).
"""
import pdfplumber

from .base import ParsedStatement
from .kotak_corporate import KotakCorporateParser

PARSERS = [KotakCorporateParser]


class UnsupportedStatement(Exception):
    pass


class PasswordRequired(Exception):
    pass


def parse_pdf(path: str, password: str | None = None) -> ParsedStatement:
    try:
        pdf = pdfplumber.open(path, password=password or "")
        first = pdf.pages[0].extract_text() or ""
    except Exception as e:  # pdfminer raises PDFPasswordIncorrect for locked files
        if "password" in type(e).__name__.lower() or "password" in str(e).lower():
            raise PasswordRequired("This PDF is password protected. Enter the statement password.")
        raise
    with pdf:
        for cls in PARSERS:
            if cls.detect(first):
                return cls().parse(pdf)
    raise UnsupportedStatement(
        "This statement format is not supported yet. Supported: "
        + ", ".join(f"{c.bank} {c.card_type}" for c in PARSERS)
    )
