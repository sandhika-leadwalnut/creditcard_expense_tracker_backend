"""Parser for Kotak Mahindra Bank Corporate Credit Card statements.

Uses word x-positions (pdfplumber) so the 'Spends Area' column is separated
cleanly from the merchant text, instead of guessing from spaces.
"""
import re
from collections import defaultdict
from datetime import datetime

from .base import ParsedRow, ParsedStatement, StatementParser, money

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
AMT_RE = re.compile(r"^₹[\d,]*\.\d{2}(Cr)?$")
FX_RE = re.compile(r"^\d{2}/\d{2}/\d{2}\s+([\d,]+\.\d{2})\s+([A-Z]{3})$")
CARD_RE = re.compile(r"(\d{6}X+(\d{4}))")


def _d(s: str):
    for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


class KotakCorporateParser(StatementParser):
    bank = "Kotak Mahindra Bank"
    card_type = "Corporate Credit Card"

    @classmethod
    def detect(cls, first_page_text: str) -> bool:
        t = first_page_text.lower()
        return "kotak" in t and "transaction table" in t

    # ---- header fields ----
    def _header(self, text: str, st: ParsedStatement):
        m = re.search(r"Statement Date\s+(\d{2}-[A-Za-z]{3}-\d{4})", text)
        if m:
            st.statement_date = _d(m.group(1))
        m = re.search(r"Statement Period\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{2}-[A-Za-z]{3}-\d{4})", text)
        if m:
            st.period_start, st.period_end = _d(m.group(1)), _d(m.group(2))
        m = re.search(r"Total Amount Due \(Rs\.\)\s+(₹[\d,]+\.\d{2})", text)
        if m:
            st.total_due = money(m.group(1))
        # Account summary row: previous, purchases, payments, total
        m = re.search(r"(₹[\d,]+\.\d{2})\s+(₹[\d,]+\.\d{2})\s+(₹[\d,]+\.\d{2})\s+(₹[\d,]+\.\d{2})", text)
        if m:
            st.purchases_total = money(m.group(2))
            st.payments_total = money(m.group(3))
        m = CARD_RE.search(text)
        if m:
            st.card_last4 = m.group(2)

    def parse(self, pdf) -> ParsedStatement:
        st = ParsedStatement(self.bank, self.card_type, "", None, None, None, None, None, None)
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        self._header(full_text, st)

        spends_x, amount_x = 355.0, 455.0  # sensible defaults, refined from header row
        section = ""
        seq = 0

        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            for w in words:
                if w["text"] == "Spends":
                    spends_x = w["x0"] - 25
                if w["text"].startswith("Amount(Rs"):
                    amount_x = w["x0"] - 25

            lines = defaultdict(list)
            for w in words:
                lines[round(w["top"])].append(w)

            for top in sorted(lines):
                ws = sorted(lines[top], key=lambda w: w["x0"])
                texts = [w["text"] for w in ws]
                line_text = " ".join(texts)

                if not DATE_RE.match(texts[0]):
                    # section headings sit in the description column without a date
                    if "Retail Purchases" in line_text:
                        section = "Retail Purchases and Cash Transactions"
                    elif "Other Fees and Charges" in line_text:
                        section = "Other Fees and Charges"
                    continue

                amt_words = [w for w in ws if w["x0"] >= amount_x and AMT_RE.match(w["text"])]
                if not amt_words:
                    st.warnings.append(f"Skipped line without amount: {line_text}")
                    continue
                amt_text = amt_words[-1]["text"]
                desc = " ".join(w["text"] for w in ws[1:] if w["x1"] <= spends_x + 5 and w not in amt_words)
                spends = " ".join(
                    w["text"] for w in ws if spends_x <= w["x0"] < amount_x and w not in amt_words
                )
                if not desc:  # very long descriptions can run into the spends column
                    desc = " ".join(w["text"] for w in ws[1:] if w not in amt_words)
                    spends = ""

                amount = money(amt_text)
                is_credit = amt_text.endswith("Cr")
                txn_date = _d(texts[0])

                # Foreign currency reference line → attach to the previous row
                fx = FX_RE.match(desc)
                if fx and amount == 0 and st.rows:
                    st.rows[-1].fx_amount = float(fx.group(1).replace(",", ""))
                    st.rows[-1].fx_currency = fx.group(2)
                    continue

                if "PAYMENT RECEIVED" in desc.upper():
                    ttype = "payment"
                elif section == "Other Fees and Charges":
                    ttype = "fee"
                elif is_credit:
                    ttype = "refund"
                else:
                    ttype = "purchase"

                seq += 1
                st.rows.append(
                    ParsedRow(
                        seq=seq,
                        txn_date=txn_date,
                        raw_description=desc,
                        amount=amount,
                        is_credit=is_credit,
                        txn_type=ttype,
                        section=section or ("Payments" if ttype == "payment" else ""),
                        spends_area=spends,
                    )
                )

        if not st.statement_date and st.period_end:
            st.statement_date = st.period_end
        return st
