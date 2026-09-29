"""
CIBIL Consumer CIR (PDF) -> selective JSON extractor
-----------------------------------------------------
Pulls ONLY the fields requested:
- name, dob
- all telephone numbers
- all PAN numbers
- CKYC number
- all addresses
- all emails
- total no. of accounts
- highest Cr/Sanc Amt (from summary)
- bureau (CreditVision) score + scoring factors, report date / control no.
- account summary incl. zero-balance / overdue counts and total current balance
- per account: type of loan, ownership, date opened, date closed, sanctioned
  amount, highest DPD value, the COMPLETE month-by-month DPD history, last
  payment date, written-off / settled details, and a calculated EMI
- the COMPLETE enquiry ledger (member, date, purpose, amount)
- a "data_checks" block that cross-checks the extraction against the bureau's
  own summary numbers (account count, balances, overdue, enquiry count)

Usage: python3 extract_cibil.py input.pdf output.json
"""

import pdfplumber
import re
import json
import sys
import logging

logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _page_text(page) -> str:
    """Extract text from one page, first collapsing 'doubled' glyphs.

    Some CIBIL PDFs (e.g. browser 'print to PDF' with a synthetic-bold font)
    draw every bold character twice at (almost) the same position, so plain
    extraction returns '45' as '4455' and 'YEAR' as 'YYEEAARR'. dedupe_chars()
    drops a char if an identical char sits within `tolerance` points of an
    earlier one. It is a no-op on clean PDFs, so it is safe to always apply.
    """
    try:
        page = page.dedupe_chars(tolerance=1)
    except Exception:
        pass
    return page.extract_text() or ""


def load_text(pdf_path: str) -> str:
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += _page_text(page) + "\n"
    return text


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------- Person-level fields ----------

def extract_name(text: str) -> str:
    m = re.search(r"CONSUMER NAME\s*:\s*([A-Za-z .]+?)\s*(?:PAN|D\.O\.B|$)", text)
    return clean(m.group(1)) if m else None


def extract_dob(text: str) -> str:
    m = re.search(r"DOB\s*:\s*([\d/]+)", text)
    return m.group(1) if m else None


def extract_all_pans(text: str) -> list:
    pans = set(re.findall(r"\b([A-Z]{5}\d{4}[A-Z])\b", text))
    return sorted(pans)


def extract_primary_pan(text: str) -> str:
    """
    The bureau's own header (CONSUMER INFORMATION section) states one PAN
    as *the* consumer PAN; the IDENTIFICATION(S) table can list additional
    ones (sometimes from enquiry records). We treat the first PAN that
    appears in document order as the primary one - this is the header PAN.
    """
    m = re.search(r"\b([A-Z]{5}\d{4}[A-Z])\b", text)
    return m.group(1) if m else None


def extract_member_id(text: str) -> str:
    """MEMBER ID from the Consumer CIR header (not to be confused with the
    per-account 'MEMBER NAME' fields, which are always NOT DISCLOSED)."""
    m = re.search(r"MEMBER ID\s*:\s*([A-Za-z0-9_]+)", text)
    return m.group(1) if m else None


def extract_primary_phone(text: str) -> str:
    """The single 'TELEPHONE NO.' field in the CONSUMER INFORMATION header
    block (distinct from the multi-row TELEPHONE(S) table further down)."""
    m = re.search(r"TELEPHONE NO\.\s*:\s*(\d+)", text)
    return m.group(1) if m else None


# Labels from the right-hand column of the CONSUMER INFORMATION block. In some
# layouts (e.g. the Rohit report) their text lands in the middle of the wrapped
# ADDRESS lines. If a future report adds a new field there, add it here.
_HEADER_LABELS = (r"CKYC|G RAM G|DRIVING LICENCE NO|DRIVING LICENCE|VOTER ID|PASSPORT NO\.?|"
                  r"AADHAAR NUMBER|PAN|EMAIL ID|TELEPHONE NO\.?|GENDER|DOB")


def extract_primary_address(text: str) -> str:
    """The single 'ADDRESS' field in the CONSUMER INFORMATION header block
    (distinct from the multi-row CONSUMER'S REPORTED ADDRESS(ES) table)."""
    m = re.search(r"\bADDRESS\s*:\s*(.*?)CIBIL TRANSUNION SCORE", text, re.S)
    if not m:
        return None
    addr = m.group(1)
    addr = addr.replace("(UID)", " ")
    addr = re.sub(r"\b(?:" + _HEADER_LABELS + r")\s*:\s*(?:NOT DISCLOSED|\S+)", " ", addr)
    addr = re.sub(r"\bNO\s*$", " ", addr.strip())      # stray wrapped 'NO'
    return clean(addr)


def extract_full_name(text: str) -> str:
    """Full name from the CONSUMER DETAILS strip (the header NAME can be just
    a first name, e.g. 'Rohit' vs 'ROHIT KUMAR S/O UPENDRA')."""
    m = re.search(r"CONSUMER NAME\s*:\s*(.+?)\s+D\.O\.B", text)
    return clean(m.group(1)) if m else None


def extract_ckyc(text: str) -> str:
    m = re.search(r"CKYC\s+(\d+)", text)
    return m.group(1) if m else None


def extract_phones(text: str) -> list:
    # From the TELEPHONE(S) table: "<Type> <10-digit number> -" lines
    block_m = re.search(r"TELEPHONE\(S\)(.*?)\(e\) - TELEPHONE", text, re.S)
    phones = []
    if block_m:
        rows = re.findall(
            r"(Not Classified|Mobile Phone|Office Phone|Home Phone)\s+(\d{6,})",
            block_m.group(1),
        )
        for ptype, num in rows:
            phones.append({"type": ptype, "number": num})
    # de-duplicate identical (type, number) pairs, keep unique numbers list too
    unique_numbers = sorted({p["number"] for p in phones})
    return {"by_type": phones, "unique_numbers": unique_numbers}


def extract_emails(text: str) -> list:
    block_m = re.search(r"EMAIL CONTACT\(S\)(.*?)CONSUMER.S REPORTED ADDRESS", text, re.S)
    if not block_m:
        return []
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", block_m.group(1))
    return sorted(set(emails))


def _addresses_from_tables(pdf_path: str) -> list:
    """
    Uses pdfplumber's native table detection for the address table.
    Some rows come back with the category/code/date embedded inside the
    address cell's text (a quirk of this report's cell layout), so those
    are cleaned up with a regex pass afterwards.
    """
    CATEGORY_RE = re.compile(
        r"\b(Permanent|Residence|Not Categorized|Office)\s+(Owned|-)\s+(\d{2}/\d{2}/\d{4})\b"
    )

    addresses = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page = page.dedupe_chars(tolerance=1)
            page_text = page.extract_text() or ""
            if "REPORTED ADDRESS" not in page_text:
                continue
            for table in page.extract_tables():
                if not table or "ADDRESS" not in (table[0][0] or ""):
                    continue
                for row in table[1:]:
                    cells = [c for c in row if c]
                    if not cells:
                        continue
                    if len(row) >= 5 and row[1] and row[2] and row[4]:
                        # clean row: address, category, code, date already separated
                        addresses.append(
                            {
                                "address": clean(row[1]).lstrip("(e) ").strip(),
                                "category": row[2],
                                "residence_code": row[3] or "-",
                                "date_reported": row[4],
                            }
                        )
                    else:
                        # garbled row: category/code/date embedded in the text blob
                        blob = clean(" ".join(cells))
                        m = CATEGORY_RE.search(blob)
                        if m:
                            category, code, date = m.groups()
                            addr_text = clean(blob[: m.start()] + " " + blob[m.end():])
                            addr_text = re.sub(r"^\(e\)\s*", "", addr_text).strip()
                            addresses.append(
                                {
                                    "address": addr_text,
                                    "category": category,
                                    "residence_code": code,
                                    "date_reported": date,
                                }
                            )
    return addresses


_ADDR_MARK = re.compile(
    r"\b(Permanent|Residence|Office|Not Categorized|Mortgage Property)\s+"
    r"(Owned|Rented|-)\s+(\d{2}/\d{2}/\d{4})\b"
)


def extract_addresses(pdf_path: str, text: str = None) -> list:
    """
    Text-based parser (robust to table-detection quirks). Each address row is
    a few wrapped lines with a '<Category> <Code> <Date>' marker somewhere in
    the middle. A row is complete once its marker has been seen AND the address
    text (marker removed) ends with a 6-digit PIN. Falls back to pdfplumber's
    table detection if nothing is found.
    """
    if text is None:
        text = load_text(pdf_path)
    m = re.search(r"REPORTED ADDRESS\(ES\)\s*\n(.*?)\(e\)\s*-\s*ADDRESSES REPORTED", text, re.S)
    if not m:
        return _addresses_from_tables(pdf_path)
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    lines = [l for l in lines if not l.startswith("ADDRESS CATEGORY")]

    rows, buf, mark = [], [], None
    for line in lines:
        mk = _ADDR_MARK.search(line)
        if mk:
            mark = mk.groups()
            line = clean(line[:mk.start()] + " " + line[mk.end():])
        if line:
            buf.append(line)
        joined = clean(" ".join(buf))
        if mark and re.search(r"\b\d{6}$", joined):
            enq = joined.startswith("(e)")
            rows.append({
                "address": re.sub(r"^\(e\)\s*", "", joined),
                "category": mark[0],
                "residence_code": mark[1],
                "date_reported": mark[2],
                "from_enquiry": enq,
            })
            buf, mark = [], None
    return rows or _addresses_from_tables(pdf_path)


def extract_enquiry_summary(text: str) -> dict:
    m = re.search(
        r"TOTAL ENQUIRIES MOST RECENT PAST 30 DAYS PAST 12 MONTHS PAST 24 MONTHS\s*\n"
        r"(\d+)\s+([\d/]+)\s+(\d+)\s+(\d+)\s+(\d+)",
        text,
    )
    if not m:
        return {
            "total_enquiries": None,
            "most_recent": None,
            "past_30_days": None,
            "past_12_months": None,
            "past_24_months": None,
        }
    total, most_recent, d30, m12, m24 = m.groups()
    return {
        "total_enquiries": int(total),
        "most_recent": most_recent,
        "past_30_days": int(d30),
        "past_12_months": int(m12),
        "past_24_months": int(m24),
    }


def extract_summary(text: str) -> dict:
    total = re.search(r"Total\s*:\s*(\d+)", text)
    zero_bal = re.search(r"Zero balance\s*:\s*(\d+)", text)
    overdue_cnt = re.search(r"Overdue\s*:\s*(\d+)(?![\d,])", text)
    high_cr = re.search(r"High Cr/Sanc\. Amt\s*:\s*₹([\d,]+)", text)
    current = re.search(r"Current\s*:\s*₹([\d,]+)", text)
    overdue_amt = re.search(r"Overdue\s*:\s*₹([\d,]+)", text)
    recent = re.search(r"Recent\s*:\s*([\d/]+)", text)
    oldest = re.search(r"Oldest\s*:\s*([\d/]+)", text)
    return {
        "total_accounts": int(total.group(1)) if total else None,
        "zero_balance_accounts": int(zero_bal.group(1)) if zero_bal else None,
        "overdue_accounts": int(overdue_cnt.group(1)) if overdue_cnt else None,
        "highest_cr_sanc_amt": f"₹{high_cr.group(1)}" if high_cr else None,
        # Total current balance for this PAN/profile as stated by the bureau summary
        "total_current_balance": f"₹{current.group(1)}" if current else None,
        "total_overdue_amount": f"₹{overdue_amt.group(1)}" if overdue_amt else None,
        "most_recent_account_opened": recent.group(1) if recent else None,
        "oldest_account_opened": oldest.group(1) if oldest else None,
    }


def extract_bureau_score(text: str) -> dict:
    """CreditVision / CIBIL score + the bureau's scoring factors.
    The score is printed twice: in the gauge ("CREDITVISION 714") and in the
    CONSUMER DETAILS strip ("CREDITVISION® SCORE : 714"). The labelled one is
    the reliable anchor; the gauge is the fallback."""
    m = re.search(r"CREDITVISION\S*\s*SCORE\s*:\s*(\d{3})", text)
    if not m:
        m = re.search(r"CREDITVISION\s+(\d{3})\b", text)
    score = int(m.group(1)) if m else None

    rng = re.search(r"(\d{3})\s*\(high risk\)\s*to\s*(\d{3})\s*\(low risk\)", text)
    factors = []
    fb = re.search(r"SCORING FACTORS(.*?)Ranges from", text, re.S)
    if fb:
        for line in fb.group(1).splitlines():
            fm = re.search(r"\b([1-5])\.\s+(\S.*)$", line)
            if fm:
                factors.append(clean(fm.group(2)))
    return {
        "score": score,
        "range_min": int(rng.group(1)) if rng else 300,
        "range_max": int(rng.group(2)) if rng else 900,
        "model": "Enhanced CreditVision" if re.search(r"ENHANCED", text) else None,
        "scoring_factors": factors,
    }


def extract_report_meta(text: str) -> dict:
    m = re.search(r"REPORT DATE & TIME\s*:\s*([\d/]+)\s*\(([\d:]+)\)", text)
    ctrl = re.search(r"CONTROL NUMBER\s*:\s*(\d+)", text)
    ref = re.search(r"REFERENCE NUMBER\s*:\s*(\S+)", text)
    return {
        "report_date": m.group(1) if m else None,
        "report_time": m.group(2) if m else None,
        "control_number": ctrl.group(1) if ctrl else None,
        "reference_number": ref.group(1) if ref else None,
    }


# ---------- Enquiry ledger ----------

ENQ_RE = re.compile(
    r"^(?P<member>[A-Z][A-Z .&'\-]*?)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<purpose>[A-Z][A-Z0-9 ()&/.,\-–]*?)\s+"
    r"₹\s*(?P<amt>[\d,]+)\s*$"
)


def extract_enquiries(text: str) -> dict:
    """Every row of the CONSUMER ENQUIRY DETAILS table (it spans 2+ pages).
    Returns {"rows": [...], "unparsed_rows": [...]} - anything that LOOKS like
    an enquiry row (has a dd/mm/yyyy date and a rupee amount) but does not
    match the strict pattern is reported rather than silently dropped."""
    start = text.find("CONSUMER ENQUIRY DETAILS")
    section = text[start:] if start != -1 else ""
    end = section.find("CIR DATA GLOSSARY")
    if end != -1:
        section = section[:end]

    rows, unparsed = [], []
    for raw in section.splitlines():
        line = clean(raw)
        m = ENQ_RE.match(line)
        if m:
            amt = m.group("amt")
            rows.append({
                "member_name": clean(m.group("member")),
                "enquiry_date": m.group("date"),
                "enquiry_purpose": clean(m.group("purpose")),
                "enquiry_amount": f"₹{amt}",
                "enquiry_amount_value": int(amt.replace(",", "")),
            })
        elif re.search(r"\d{2}/\d{2}/\d{4}", line) and "₹" in line and "ENQUIRY DATE" not in line:
            unparsed.append(line)
    return {"rows": rows, "unparsed_rows": unparsed}


# ---------- Per-account fields ----------

def to_amount(s):
    if s is None:
        return None
    return float(s.replace(",", "").replace("₹", ""))


def calc_emi(principal, annual_rate, months):
    """Standard reducing-balance EMI formula."""
    if not principal or not annual_rate or not months:
        return None
    try:
        r = (annual_rate / 12) / 100
        n = int(months)
        if r == 0:
            return round(principal / n, 2)
        emi = principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
        return round(emi, 2)
    except (ValueError, ZeroDivisionError):
        return None


DPD_TOKEN = re.compile(r"^(\d{3}|STD|SMA|SUB|DBT|LSS|XXX|-)$")


def dpd_numeric(value):
    """'000'-'900' -> int, 'STD' -> 0 (standard / no default). Everything else
    (XXX = not reported, SMA/SUB/DBT/LSS asset classes, '-') -> None."""
    if value is None:
        return None
    if re.fullmatch(r"\d{3}", value):
        return int(value)
    if value == "STD":
        return 0
    return None


def parse_dpd_grid(block: str) -> dict:
    """
    Parse the DAYS PAST DUE / ASSET CLASSIFICATION grid into a complete
    month-by-month history.

    Robustness notes:
    - A grid can be split by a page break (e.g. Account 3: 2026/2025 rows on
      one page, 2024/2023 on the next, with page footer/header lines in
      between). Lines that are not grid rows are therefore SKIPPED, not
      treated as the end of the grid; parsing only stops at the next
      section ("ACCOUNT INFORMATION" / the enquiry table).
    - A grid row must be a year followed by exactly 12 valid tokens, so page
      furniture and glossary text can never be mistaken for data.
    - '-' cells (no data for that month) are not stored; every reported
      month - including XXX (institution did not report) - is kept.
    """
    header = "YEAR JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC"
    idx = block.find(header)
    if idx == -1:
        return {"history": [], "last_payment": None}

    pre = block[:idx]
    lp = re.search(r"LAST PAYMENT\s*:\s*([\d/]*)", pre)
    last_payment = lp.group(1) if lp and lp.group(1) else None

    history = []
    for line in block[idx + len(header):].splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("ACCOUNT INFORMATION", "CONSUMER ENQUIRY DETAILS", "MEMBER NAME ENQUIRY")):
            break
        m = re.match(r"^((?:19|20)\d{2})\s+(.*)$", line)
        if not m:
            continue  # page-break furniture between grid rows
        toks = m.group(2).split()
        if len(toks) != 12 or not all(DPD_TOKEN.match(t) for t in toks):
            continue
        year = int(m.group(1))
        for month, tok in enumerate(toks, start=1):
            if tok != "-":
                history.append({"year": year, "month": month, "value": tok})
    history.sort(key=lambda h: (h["year"], h["month"]))
    return {"history": history, "last_payment": last_payment}


def highest_dpd(history: list):
    """Highest numeric DPD in the history. STD counts as 0 days; XXX and
    asset-class codes are ignored. None only if nothing numeric was reported."""
    vals = [dpd_numeric(h["value"]) for h in history]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


KNOWN_LOAN_TYPES = [
    "BUSINESS LOAN – GENERAL", "BUSINESS LOAN - GENERAL", "BUSINESS LOAN – UNSECURED",
    "BUSINESS LOAN - UNSECURED", "BUSINESS LOAN – SECURED", "BUSINESS LOAN - SECURED",
    "SHORT TERM PERSONAL LOAN", "P2P PERSONAL LOAN", "AUTO LOAN (PERSONAL)", "TWO-WHEELER LOAN",
    "USED CAR LOAN", "PROPERTY LOAN", "CONSUMER LOAN", "HOUSING LOAN", "PERSONAL LOAN",
    "GOLD LOAN", "EDUCATION LOAN", "CREDIT CARD", "SECURED CREDIT CARD", "KISAN CREDIT CARD",
    "OVERDRAFT", "LOAN AGAINST SHARES/SECURITIES", "LOAN TO PROFESSIONAL", "OTHERS",
]
_TYPE_STOP = (r"SANCTIONED|HIGH CREDIT|CURRENT|PAYMENT|REPAYMENT|CREDIT|SUIT|INTEREST|EMI|"
              r"OVERDUE|ACTUAL|WRITTEN|COLLATERAL|NA|AMOUNT|MEMBER|NAME|ACCOUNT|NUMBER|OWNERSHIP|"
              r"BALANCE|FREQUENCY|TENURE|RATE|FACILITY|STATUS|VALUE|WILFUL|DEFAULT")


def extract_loan_type(block: str):
    """Account type. The label can wrap onto the next line(s) ('SHORT TERM
    PERSONAL / LOAN', 'BUSINESS LOAN – / GENERAL'), with unrelated column text
    in between, so: take the words after 'TYPE :' up to the next column label,
    and while that isn't a complete known type, append the leading words of the
    following lines. Unknown types fall back to whatever text was read (never
    silently None / never a hardcoded guess)."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*TYPE\s*:\s*(.*)$", line)
        if not m:
            continue
        first = re.split(r"\s+(?:" + _TYPE_STOP + r")\b|\s+:", m.group(1))[0]
        cand = clean(first)
        if cand in KNOWN_LOAN_TYPES:
            return cand
        for nxt in lines[i + 1:i + 4]:
            lead = re.split(r"\s+(?:" + _TYPE_STOP + r")\b|\s+:|:", nxt.strip())[0]
            lead = clean(lead)
            if not lead or lead in ("MEMBER", "ACCOUNT"):
                continue
            joined = clean(cand + " " + lead)
            if joined in KNOWN_LOAN_TYPES:
                return joined
            if joined.replace("–", "-") in [k.replace("–", "-") for k in KNOWN_LOAN_TYPES]:
                return joined
        return cand or None
    return None


def extract_accounts(text: str) -> list:
    # Only the account section: stop before the enquiry table so the LAST
    # account block can't bleed into it.
    cut = text.find("CONSUMER ENQUIRY DETAILS")
    if cut != -1:
        text = text[:cut]
    # Split on "<n>. ACCOUNT" headers, keep the account number
    parts = re.split(r"\n(\d+)\.\s*ACCOUNT\n", text)
    # parts[0] = preamble before first account; then alternating number, block
    accounts = []
    for i in range(1, len(parts), 2):
        acc_no = int(parts[i])
        block = parts[i + 1]

        date_opened = re.search(r"DATE OPENED\s*:\s*([\d/]+)", block)
        date_closed = re.search(r"DATE CLOSED\s*:\s*([\d/]*)", block)
        date_reported_certified = re.search(r"DATE REPORTED & CERTIFIED\s*:\s*([\d/]+)", block)

        loan_type_str = extract_loan_type(block)

        sanctioned = re.search(r"SANCTIONED\s*(?:AMOUNT)?\s*:\s*₹\s*([\d,]+)", block)
        if not sanctioned:
            # Credit cards report "HIGH CREDIT AMOUNT" instead of a sanctioned amount
            sanctioned = re.search(r"HIGH CREDIT\s*(?:AMOUNT)?\s*:\s*₹\s*([\d,]+)", block)
        current_bal = re.search(r"CURRENT\s*:\s*₹\s*([\d,\-]+)", block)
        overdue = re.search(r"\bOVERDUE\s*:\s*₹\s*([\d,]+)", block)
        actual_payment = re.search(r"ACTUAL\s*:\s*₹\s*([\d,]+)", block)
        interest_rate = re.search(r"INTEREST\s*:\s*([\d.]+)", block)
        tenure = re.search(r"REPAYMENT\s*:\s*(\d+)\b", block)
        reported_emi = re.search(r"\bEMI\s*:\s*₹\s*([\d,]+)", block)
        status = "ACTIVE" if re.search(r"\bACTIVE\b", block[:400]) else (
            "INACTIVE" if re.search(r"\bINACTIVE\b", block[:400]) else None
        )

        # Credit facility status: finer-grained than ACTIVE/INACTIVE - flags
        # written-off, settled, or post-write-off-settled accounts.
        cfs_m = re.search(
            r"CREDIT\s*:\s*(WRITTEN-OFF|POST \(WO\) SETTLED|SETTLED|SUIT FILED|WILFUL DEFAULT|RESTRUCTURED)",
            block,
        )
        credit_facility_status = cfs_m.group(1) if cfs_m else None

        # Account ownership: INDIVIDUAL / GUARANTOR / JOINT / AUTHORISED USER
        own_m = re.search(r"OWNERSHIP\s*:\s*(INDIVIDUAL|GUARANTOR|JOINT|AUTHORI[SZ]ED USER)", block)
        ownership = own_m.group(1) if own_m else None

        # Settlement amount (only some reports carry it; this one does not)
        settle_m = re.search(r"SETTLE(?:D|MENT)\s*AMOUNT\s*:\s*₹\s*([\d,]+)", block)
        settlement_amount = f"₹{settle_m.group(1)}" if settle_m else None

        dpd = parse_dpd_grid(block)
        history = dpd["history"]
        latest = next((h for h in reversed(history) if h["value"] != "XXX"), None)

        # Written-off amounts (only present on written-off / post-WO-settled
        # accounts). Other unrelated fields (ACCOUNT NUMBER, OWNERSHIP) can
        # sit between the amount and its "(TOTAL)"/"(PRINCIPLE)" label due to
        # the report's column-wrap layout, so allow a bounded gap.
        wo_total_m = re.search(r"WRITTEN OFF\s*:\s*₹\s*([\d,]+).{0,80}?\(TOTAL\)", block, re.S)
        wo_principle_m = re.search(r"WRITTEN OFF\s*:\s*₹\s*([\d,]+).{0,80}?\(PRINCIPLE\)", block, re.S)
        written_off_total = f"₹{wo_total_m.group(1)}" if wo_total_m else None
        written_off_principle = f"₹{wo_principle_m.group(1)}" if wo_principle_m else None

        # Collateral (only present on secured facilities: housing/gold loans
        # with a pledged asset). "COLLATERAL : ₹ x" is the value; the type
        # label (GOLD / PROPERTY / MULTIPLE SECURITIES) appears separately.
        collateral_value_m = re.search(r"COLLATERAL\s*:\s*₹\s*([\d,]+)", block)
        collateral_value = f"₹{collateral_value_m.group(1)}" if collateral_value_m else None

        collateral_type = None
        ctype_m = re.search(r"COLLATERAL\s*:\s*(GOLD|PROPERTY|MULTIPLE)\b", block)
        if ctype_m:
            ctype = ctype_m.group(1)
            if ctype == "MULTIPLE" and "SECURITIES" in block[ctype_m.end():ctype_m.end() + 40]:
                ctype = "MULTIPLE SECURITIES"
            collateral_type = ctype

        principal = to_amount(sanctioned.group(1)) if sanctioned else None
        rate = float(interest_rate.group(1)) if interest_rate else None
        months = int(tenure.group(1)) if tenure else None

        calculated_emi = calc_emi(principal, rate, months)

        accounts.append(
            {
                "account_no": acc_no,
                "loan_type": clean(loan_type_str) if loan_type_str else None,
                "status": status,
                "ownership": ownership,
                "date_opened": date_opened.group(1) if date_opened else None,
                "date_closed": date_closed.group(1) if date_closed and date_closed.group(1) else None,
                "date_reported_certified": date_reported_certified.group(1) if date_reported_certified else None,
                "sanctioned_amount": f"₹{sanctioned.group(1)}" if sanctioned else None,
                "current_balance": f"₹{current_bal.group(1)}" if current_bal else None,
                "overdue_amount": f"₹{overdue.group(1)}" if overdue else None,
                "actual_payment": f"₹{actual_payment.group(1)}" if actual_payment else None,
                "interest_rate_pct": rate,
                "repayment_tenure_months": months,
                "reported_emi": f"₹{reported_emi.group(1)}" if reported_emi else None,
                "calculated_emi": calculated_emi,
                "collateral_value": collateral_value,
                "collateral_type": collateral_type,
                "credit_facility_status": credit_facility_status,
                "written_off_total": written_off_total,
                "written_off_principle": written_off_principle,
                "settlement_amount": settlement_amount,
                "is_written_off": credit_facility_status in ("WRITTEN-OFF", "POST (WO) SETTLED") or bool(written_off_total),
                "is_settled": credit_facility_status in ("SETTLED", "POST (WO) SETTLED"),
                "last_payment_date": dpd["last_payment"],
                "highest_dpd_value": highest_dpd(history),
                "latest_dpd": latest,
                "dpd_history": history,
            }
        )
    return accounts


def build_data_checks(result: dict) -> dict:
    """Cross-check what was extracted against the bureau's own summary."""
    accs = result["accounts"]
    summ = result["account_summary"]
    enq_sum = result["enquiry_summary"]
    enqs = result["enquiries"]

    def amt(s):
        return int(round(to_amount(s))) if s else None

    # Bureau's "Current" balance = sum of all POSITIVE balances across every
    # account (active or not). Verified on two reports: an inactive account
    # holding +Rs1 is counted, one holding -Rs1,032 is not.
    active_bal = sum(max(amt(a["current_balance"]) or 0, 0) for a in accs)
    all_bal = sum(amt(a["current_balance"]) or 0 for a in accs)
    overdue_sum = sum(amt(a["overdue_amount"]) or 0 for a in accs)
    overdue_n = sum(1 for a in accs if (amt(a["overdue_amount"]) or 0) > 0)
    zero_n = sum(1 for a in accs if (amt(a["current_balance"]) or 0) == 0)

    def chk(label, got, want):
        # A None bureau value used to short-circuit to "ok" - that let an
        # unrecognized template (e.g. Sunita's) pass silently with all-zero
        # output. None is now always a FAIL: the field genuinely wasn't found.
        return {"check": label, "extracted": got, "bureau_summary": want,
                "ok": (want is not None) and (got == want)}

    checks = [
        chk("account count", len(accs), summ["total_accounts"]),
        chk("total current balance (sum of positive balances)", active_bal, amt(summ["total_current_balance"])),
        chk("total overdue amount", overdue_sum, amt(summ["total_overdue_amount"])),
        chk("overdue account count", overdue_n, summ["overdue_accounts"]),
        chk("zero-balance account count", zero_n, summ["zero_balance_accounts"]),
        chk("enquiry rows listed vs bureau total", len(enqs), enq_sum["total_enquiries"]),
        chk("unparsed enquiry-looking lines", len(result["enquiries_unparsed_rows"]), 0),
        chk("accounts with a DPD history", sum(1 for a in accs if a["dpd_history"]), len(accs)),
    ]
    return {
        "all_ok": all(c["ok"] for c in checks),
        "checks": checks,
        "informational": {
            "sum_of_all_account_balances_incl_inactive": all_bal,
            "note": "Bureau 'Current' total = sum of positive balances over all accounts; "
                    "negative residual balances (e.g. -Rs1,032) are excluded.",
        },
    }


def detect_template(text: str) -> str:
    """Identify which CIBIL layout this text came from. Add new templates by
    adding a new elif here plus a new extract_template_x() below - never
    touch an existing branch when adding one."""
    if re.search(r"CONSUMER NAME\s*:", text) and "CONSUMER ACCOUNT SUMMARY" in text:
        return "A"
    if re.search(r"\nNAME:\s", text) and "ACCOUNT(S):" in text and "PMT HIST START" in text:
        return "B"
    return None


# ---------- Template B: "CONSUMER CIR" compact/API-style layout ----------
_TYPE_STOP_B = r"OVERDUE|OWNERSHIP|REPORTED|EMI|PMT|COLLATERAL|SANCTIONED|CURRENT|ACCOUNT|MEMBER|DAYS|STATUS"


def _b_field(block, pat, cast=str, flags=0):
    m = re.search(pat, block, flags)
    return cast(m.group(1)) if m else None


def extract_loan_type_b(block: str, stop_words=_TYPE_STOP_B, known_types=KNOWN_LOAN_TYPES):
    """Account type for Template B. Same defense as Template A's
    extract_loan_type(): match against a known vocabulary and look ahead
    across a line-wrap, because this report's column layout can wrap a
    multi-word type (e.g. 'GOLD LOAN', 'BUSINESS LOAN \u2013 SECURED') with an
    unrelated field's value landing in between - so a naive one-line regex
    silently truncates it (e.g. to just 'GOLD'). Falls back to whatever text
    was read if the type isn't in the known list - never silently None."""
    m = re.search(r"TYPE:(.*)$", block, re.M)
    if not m:
        return None
    first = re.split(r"\s+(?:" + stop_words + r")\b", m.group(1))[0]
    cand = clean(first)
    if cand in known_types:
        return cand
    for nxt in block[m.end():].splitlines()[:4]:
        lead = re.split(r"\s+(?:" + stop_words + r")\b|:", nxt.strip())[0]
        lead = clean(lead)
        if not lead:
            continue
        joined = clean(cand + " " + lead)
        if joined in known_types or joined.replace("\u2013", "-") in [k.replace("\u2013", "-") for k in known_types]:
            return joined
    return cand or None


def parse_dpd_strip_b(block: str) -> list:
    m = re.search(r"DAYS PAST DUE/ASSET CLASSIFICATION.*?\n(.*?)\n(.*?)(?:\n\n|$)", block, re.S)
    if not m:
        return []
    vals, labels = m.group(1).split(), m.group(2).split()
    history = []
    for v, lab in zip(vals, labels):
        if "-" not in lab:
            continue
        mm, yy = lab.split("-")
        history.append({"year": 2000 + int(yy), "month": int(mm), "value": v})
    history.sort(key=lambda h: (h["year"], h["month"]))
    return history


def extract_template_b(text: str) -> dict:
    name = _b_field(text, r"\nNAME:\s*([A-Za-z .]+)", clean)
    dob = _b_field(text, r"DATE OF BIRTH:\s*([\d-]+)")
    member_id = _b_field(text, r"MEMBER ID:\s*(\S+)")
    rd = re.search(r"CONSUMER CIR\n.*?DATE:\s*([\d-]+)\n.*?TIME:\s*([\d:]+)\n.*?CONTROL NUMBER:\s*(\S+)", text, re.S)
    score_m = re.search(r"SCORE NAME SCORE SCORING FACTORS\s*\n\s*0*(\d+)", text)
    pan = _b_field(text, r"INCOME TAX ID NUMBER \(PAN\)\s+(\S+)")
    phone_nums = re.findall(r"MOBILE PHONE\s+(\d{6,})", text)
    email_block = text[text.find("EMAIL CONTACT"):text.find("ADDRESS(ES)")]
    emails = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", email_block)))

    addresses = []
    for enq_tag, addr, cat, code, date in re.findall(
        r"ADDRESS\s*(\(e\))?\s*:\s*(.*?)\nCATEGORY:\s*([A-Z ]+?)\s+RESIDENCE CODE:\s*(\S+)\s+DATE REPORTED:\s*([\d-]+)",
        text,
    ):
        addresses.append({
            "address": clean(addr), "category": clean(cat),
            "residence_code": code, "date_reported": date,
            "from_enquiry": bool(enq_tag),
        })

    sm = re.search(
        r"TOTAL:(\d+)\s+HIGH CR/SANC\. AMT:([\d,]+)\s+CURRENT:([\d,]+)\s+RECENT:([\d-]+)\s+"
        r"OVERDUE:(\d+)\s+OVERDUE:([\d,]+)\s+OLDEST:([\d-]+)\s+ZERO-BALANCE:(\d+)", text)
    summary = {
        "total_accounts": int(sm.group(1)) if sm else None,
        "zero_balance_accounts": int(sm.group(8)) if sm else None,
        "overdue_accounts": int(sm.group(5)) if sm else None,
        "highest_cr_sanc_amt": f"\u20b9{sm.group(2)}" if sm else None,
        "total_current_balance": f"\u20b9{sm.group(3)}" if sm else None,
        "total_overdue_amount": f"\u20b9{sm.group(6)}" if sm else None,
        "most_recent_account_opened": sm.group(4) if sm else None,
        "oldest_account_opened": sm.group(7) if sm else None,
    }

    eq = re.search(r"All Enquiries\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d-]+)", text)
    enquiry_summary = {
        "total_enquiries": int(eq.group(1)) if eq else None,
        "most_recent": eq.group(5) if eq else None,
        "past_30_days": int(eq.group(2)) if eq else None,
        "past_12_months": int(eq.group(3)) if eq else None,
        "past_24_months": int(eq.group(4)) if eq else None,
    }

    accounts = []
    acc_sec_m = re.search(r"\nACCOUNT\(S\):\s*\n(.*?)\nENQUIRIES:", text, re.S)
    if acc_sec_m:
        blocks = re.split(r"ACCOUNT DATES AMOUNTS STATUS\n", acc_sec_m.group(1))[1:]
        for i, block in enumerate(blocks, start=1):
            history = parse_dpd_strip_b(block)
            sanctioned = _b_field(block, r"SANCTIONED:([\d,]+)")
            overdue = _b_field(block, r"OVERDUE:([\d,]+)")
            current_bal = _b_field(block, r"CURRENT BALANCE:([\d,]+)")
            emi = _b_field(block, r"EMI:([\d,]+)")
            tenure = _b_field(block, r"REPAYMENT TENURE:(\d+)", int)
            rate = _b_field(block, r"INTEREST RATE:([\d.]+)", float)
            principal = to_amount(sanctioned) if sanctioned else None
            accounts.append({
                "account_no": i,
                "loan_type": extract_loan_type_b(block),
                "status": "ACTIVE" if not _b_field(block, r"[^D]CLOSED:([\d-]+)") else "INACTIVE",
                "ownership": _b_field(block, r"OWNERSHIP:(\S+)"),
                "date_opened": _b_field(block, r"OPENED:([\d-]+)"),
                "date_closed": _b_field(block, r"[^D]CLOSED:([\d-]+)"),
                "date_reported_certified": _b_field(block, r"REPORTED AND CERTIFIED:.*?\n\s*([\d-]+)", str, re.S),
                "sanctioned_amount": f"\u20b9{sanctioned}" if sanctioned else None,
                "current_balance": f"\u20b9{current_bal}" if current_bal else None,
                "overdue_amount": f"\u20b9{overdue}" if overdue else None,
                "actual_payment": None,
                "interest_rate_pct": rate,
                "repayment_tenure_months": tenure,
                "reported_emi": f"\u20b9{emi}" if emi else None,
                "calculated_emi": calc_emi(principal, rate, tenure),
                "collateral_value": None,
                "collateral_type": _b_field(block, r"COLLATERAL TYPE:(\S+)"),
                "credit_facility_status": None,
                "written_off_total": None,
                "written_off_principle": None,
                "settlement_amount": None,
                "is_written_off": False,
                "is_settled": False,
                "last_payment_date": None,
                "highest_dpd_value": highest_dpd(history),
                "latest_dpd": history[-1] if history else None,
                "dpd_history": history,
            })

    enq_rows, enq_unparsed = [], []
    enq_sec = text[text.find("\nENQUIRIES:"):]
    ENQ_RE_B = re.compile(
        r"^(?P<member>[A-Z][A-Za-z .&'\-]*?)\s+"
        r"(?P<date>\d{2}-\d{2}-\d{4})\s+"
        r"(?P<purpose>[A-Za-z][A-Za-z0-9 ()&/.,\u2013\-]*?)\s+"
        r"(?P<amt>[\d,]+)\s*$"
    )
    for raw in enq_sec.splitlines():
        line = clean(raw)
        m = ENQ_RE_B.match(line)
        if m:
            amt = m.group("amt")
            enq_rows.append({
                "member_name": clean(m.group("member")),
                "enquiry_date": m.group("date"),
                "enquiry_purpose": clean(m.group("purpose")),
                "enquiry_amount": f"\u20b9{amt}",
                "enquiry_amount_value": int(amt.replace(",", "")),
            })
        elif re.search(r"\d{2}-\d{2}-\d{4}", line) and re.search(r"\d", line) and "ENQUIRY DATE" not in line:
            enq_unparsed.append(line)

    return {
        "name": name, "full_name_as_reported": name, "dob": dob, "member_id": member_id,
        "report_meta": {
            "report_date": rd.group(1) if rd else None,
            "report_time": rd.group(2) if rd else None,
            "control_number": rd.group(3) if rd else None,
            "reference_number": _b_field(text, r"MEMBER REFERENCE NUMBER:\s*(\S+)"),
        },
        "bureau_score": {
            "score": int(score_m.group(1)) if score_m else None,
            "range_min": 300, "range_max": 900, "model": None, "scoring_factors": [],
        },
        "pan_numbers": [pan] if pan else [], "primary_pan": pan,
        "ckyc_number": None,
        "phone_numbers": {"by_type": [{"type": "Mobile Phone", "number": n} for n in phone_nums],
                           "unique_numbers": sorted(set(phone_nums))},
        "primary_phone": phone_nums[0] if phone_nums else None,
        "emails": emails,
        "addresses": addresses,
        "primary_address": addresses[0]["address"] if addresses else None,
        "account_summary": summary,
        "enquiry_summary": enquiry_summary,
        "accounts": accounts,
        "enquiries": enq_rows,
        "enquiries_unparsed_rows": enq_unparsed,
    }


def main(pdf_path: str, out_path: str):
    text = load_text(pdf_path)
    template = detect_template(text)

    if template == "B":
        result = extract_template_b(text)
    elif template == "A":
        enq = extract_enquiries(text)
        result = {
            "name": extract_name(text),
            "full_name_as_reported": extract_full_name(text),
            "dob": extract_dob(text),
            "member_id": extract_member_id(text),
            "report_meta": extract_report_meta(text),
            "bureau_score": extract_bureau_score(text),
            "pan_numbers": extract_all_pans(text),
            "primary_pan": extract_primary_pan(text),
            "ckyc_number": extract_ckyc(text),
            "phone_numbers": extract_phones(text),
            "primary_phone": extract_primary_phone(text),
            "emails": extract_emails(text),
            "addresses": extract_addresses(pdf_path, text),
            "primary_address": extract_primary_address(text),
            "account_summary": extract_summary(text),
            "enquiry_summary": extract_enquiry_summary(text),
            "accounts": extract_accounts(text),
            "enquiries": enq["rows"],
            "enquiries_unparsed_rows": enq["unparsed_rows"],
        }
    else:
        print("WARNING: unrecognized report layout - no fields extracted. "
              "Add a new extract_template_x() (see detect_template).")
        result = {"name": None, "accounts": [], "enquiries": [],
                   "enquiries_unparsed_rows": [], "account_summary": {}, "enquiry_summary": {}}

    result["template"] = template
    result["data_checks"] = build_data_checks(result)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Wrote {out_path}")
    print(f"  bureau score : {result['bureau_score']['score']}")
    print(f"  accounts     : {len(result['accounts'])}")
    print(f"  enquiries    : {len(result['enquiries'])} rows extracted "
          f"(bureau says {result['enquiry_summary']['total_enquiries']})")
    print("  data checks  :")
    for c in result["data_checks"]["checks"]:
        print(f"    [{'OK ' if c['ok'] else 'FAIL'}] {c['check']}: extracted={c['extracted']} bureau={c['bureau_summary']}")


if __name__ == "__main__":
    pdf_arg = sys.argv[1] if len(sys.argv) > 1 else "input.pdf"
    out_arg = sys.argv[2] if len(sys.argv) > 2 else "output.json"
    main(pdf_arg, out_arg)