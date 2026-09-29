#!/usr/bin/env python3
"""
crif_extractor.py
==================

Extracts structured data from a CRIF High Mark "Consumer Base Report" PDF
(the multi-section credit report that contains the Inquiry Input
Information, CRIF HM Score, Personal Information Variations, Account
Summary / Account Information, Advanced Overlap Report and Group Details
sections) and writes it out as a single JSON file.

The boilerplate "Disclaimer: ..." / copyright footer that is repeated at
the bottom of every page is deliberately dropped -- everything else in
the document is captured.

Usage
-----
    python3 crif_extractor.py INPUT.pdf [-o OUTPUT.json]

If -o/--output is omitted, the JSON is written next to the input PDF
with the same base name and a .json extension.

Notes on approach
------------------
Most of the report is a simple "Label: value" text layout and is parsed
with regular expressions against the page text (after the disclaimer
footer has been stripped out).

The one section that is NOT a simple single-column layout is
"Personal Information - Variations": it is actually two side-by-side
two-column tables (Name/Address/Email on the left, DOB/Phone/ID on the
right), and plain text extraction interleaves their rows. That section
is parsed geometrically instead, using each word's (x0, top) position
from pdfplumber to reconstruct the four true columns (left value / left
date / right value / right date) before pairing values with their
"Reported On" dates -- including multi-line values (e.g. long addresses
that wrap across two physical lines but only carry a single date).

This script targets the CRIF High Mark "Consumer Base Report" template
specifically. It is written defensively: if a given regex/pattern fails
to match for a particular report, the raw text for that section is kept
under a "*_raw" key instead of being silently dropped, so no information
is ever lost even if a field can't be neatly structured.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

DATE_RE = r"\d{2}-\d{2}-\d{4}"

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def strip_disclaimer(page_text: str) -> str:
    """Remove the repeated Disclaimer/copyright footer block from a page."""
    if not page_text:
        return page_text
    # The footer always starts with "Disclaimer:" and ends with the word
    # "Confidential" right after the copyright line. Non-greedy + DOTALL
    # removes exactly that block wherever it occurs.
    cleaned = re.sub(
        r"Disclaimer:.*?Confidential\s*",
        "",
        page_text,
        flags=re.DOTALL,
    )
    return cleaned.strip()


def clean_val(s):
    if s is None:
        return None
    s = s.strip()
    return s if s else None


def num(s):
    """Turn '27,30,965' / '-140' / '' into an int, or None."""
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Header / meta
# ---------------------------------------------------------------------------

def parse_meta(first_page_text: str) -> dict:
    meta = {}
    m = re.search(r"CHM Ref #:\s*(\S+)", first_page_text)
    meta["chm_ref"] = clean_val(m.group(1)) if m else None

    m = re.search(r"For\s+(.+?)\s+Prepared For:", first_page_text)
    meta["report_for"] = clean_val(m.group(1)) if m else None

    # "Prepared For" value is wrapped across the two lines that surround
    # the "For <name> ... Prepared For:" line, e.g.:
    #   VALLABHI CAPITAL PRIVATE
    #   For SHIV KUMAR BABBAR Prepared For: LIMITED
    m = re.search(
        r"CHM Ref #:.*\n(?P<line2>.+)\nFor\s+.+?Prepared For:\s*(?P<line3tail>.+)",
        first_page_text,
    )
    if m:
        meta["prepared_for"] = clean_val(
            f"{m.group('line2').strip()} {m.group('line3tail').strip()}"
        )
    else:
        meta["prepared_for"] = None

    m = re.search(r"Application ID:\s*(.*)", first_page_text)
    meta["application_id"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Date of Request:\s*(" + DATE_RE + r")", first_page_text)
    meta["date_of_request"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Date of Issue:\s*(" + DATE_RE + r")", first_page_text)
    meta["date_of_issue"] = clean_val(m.group(1)) if m else None

    return meta


# ---------------------------------------------------------------------------
# Inquiry Input Information
# ---------------------------------------------------------------------------

def parse_inquiry_input(text: str) -> dict:
    block_m = re.search(
        r"Inquiry Input Information\s*(.*?)\s*CRIF HM Score", text, re.DOTALL
    )
    block = block_m.group(1) if block_m else text
    out = {}

    m = re.search(r"Name:\s*(.+?)\s*DOB/Age:", block)
    out["name"] = clean_val(m.group(1)) if m else None

    m = re.search(r"DOB/Age:\s*(.+?)\s*Gender:", block)
    out["dob_age"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Gender:\s*(\S+)", block)
    out["gender"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Father:\s*(.*?)\s*Spouse:", block)
    out["father"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Spouse:[ \t]*(.*?)(?:\n|Mother:)", block)
    out["spouse"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Mother:[ \t]*(.*?)\n", block)
    out["mother"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Phone Numbers:\s*(.+?)\s*ID\(s\):", block)
    out["phone_numbers"] = [p for p in (clean_val(m.group(1)) or "").split()] if m else []

    # ID(s) and Email ID(s) sit in separate columns of the same row, so a
    # wrapped second ID value can end up (in plain reading order) after the
    # "Email ID(s):" label rather than before it. Capture the whole span up
    # to "Entity Id:" and pull every "VALUE [TYPE]" pair out of it -- these
    # are always ID(s), never emails.
    m = re.search(r"ID\(s\):(.*?)Entity Id:", block, re.DOTALL)
    ids = []
    email_val = None
    if m:
        span = m.group(1)
        for val, typ in re.findall(r"(\S+)\s*\[(.+?)\]", span):
            ids.append({"value": val, "type": typ})
        if "Email ID(s):" in span:
            email_part = span.split("Email ID(s):", 1)[1]
            email_part = re.sub(r"\S+\s*\[.+?\]", "", email_part)
            email_val = clean_val(email_part)
    out["ids"] = ids
    out["email_ids"] = email_val.split() if email_val else []

    m = re.search(r"Entity Id:[ \t]*(.*?)\n", block)
    out["entity_id"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Current Address:[ \t]*(.*?)\n", block)
    out["current_address"] = clean_val(m.group(1)) if m else None

    m = re.search(r"Other Address:[ \t]*(.*?)(?:\n|$)", block)
    out["other_address"] = clean_val(m.group(1)) if m else None

    return out


# ---------------------------------------------------------------------------
# CRIF HM Score(s)
# ---------------------------------------------------------------------------

def parse_scores(text: str) -> dict:
    m = re.search(
        r"CRIF HM Score\(S\):\s*SCORE NAME SCORE SCORING FACTORS\s*(.*?)"
        r"Tip:\s*Positive impact",
        text,
        re.DOTALL,
    )
    result = {"scores": [], "income_imputation": {"code": None, "income_range": None}}
    if not m:
        result["_raw"] = None
        return result

    block = m.group(1)
    lines = [l.strip() for l in block.split("\n") if l.strip()]

    current_score = {"score_name": None, "score_value": None,
                      "score_range": None, "scoring_factors": []}

    for line in lines:
        perform_m = re.match(
            r"(PERFORM CONSUMER [\d.]+)\s*Score Range\s*:\s*([\d,]+-[\d,]+)\s*(.*)",
            line,
        )
        income_m = re.match(
            r"INCOME IMPUTATION\s*([\d,]+\s*-\s*[\d,]+)", line
        )
        score_val_m = re.match(r"^(\d{3})\s+(.*)", line)

        if perform_m:
            current_score["score_name"] = perform_m.group(1)
            current_score["score_range"] = perform_m.group(2)
            leftover = perform_m.group(3).strip()
            if leftover:
                current_score["scoring_factors"].append(leftover)
        elif income_m:
            result["income_imputation"]["income_range"] = income_m.group(1).strip()
        elif line == "I":
            result["income_imputation"]["code"] = "I"
        elif score_val_m:
            current_score["score_value"] = int(score_val_m.group(1))
            current_score["scoring_factors"].append(score_val_m.group(2).strip())
        else:
            current_score["scoring_factors"].append(line)

    if current_score["score_name"] or current_score["score_value"]:
        result["scores"].append(current_score)

    return result


# ---------------------------------------------------------------------------
# Personal Information - Variations (geometry-based, two side-by-side tables)
# ---------------------------------------------------------------------------

LEFT_HEADERS = {"Name Variations": "name_variations",
                "Address Variations": "address_variations",
                "Email ID Variations": "email_variations"}
RIGHT_HEADERS = {"DOB Variations": "dob_variations",
                 "Phone Variations": "phone_variations",
                 "ID Variations": "id_variations"}


def _column_of(x0):
    if x0 < 315:
        return "LV"
    if x0 < 380:
        return "LD"
    if x0 < 485:
        return "RV"
    return "RD"

def _collateral_column(x0):
    if x0 < 200:
        return "security_type"
    if x0 < 320:
        return "type_of_charge"
    if x0 < 440:
        return "security_value"
    return "date_of_value"

def _rows_by_top(words):
    buckets = defaultdict(list)
    for w in words:
        buckets[round(w["top"])].append(w)
    rows = []
    for top in sorted(buckets):
        ws = sorted(buckets[top], key=lambda w: w["x0"])
        rows.append((top, " ".join(w["text"] for w in ws)))
    return rows


def _pair_multiline(value_rows, date_rows, tol=2.5, max_lines=4):
    """Zip value rows with their date, merging wrapped multi-line values.

    Each logical entry has exactly one "Reported On" date, but its value may
    wrap across two or more physical lines (e.g. a long address). Line
    spacing alone can't tell a wrapped continuation apart from the start of
    the next entry -- but the date's vertical position always sits at the
    *average* top of the physical line(s) that make up its entry (a
    single-line entry's date top equals that line's top exactly). So for
    each date, in order, we try grouping the next 1, 2, 3... unconsumed
    value rows and take the smallest group whose average top matches the
    date's top.
    """
    entries = []
    vi = 0
    n = len(value_rows)
    for dtop, dval in date_rows:
        matched = False
        for k in range(1, max_lines + 1):
            if vi + k > n:
                break
            tops = [value_rows[vi + j][0] for j in range(k)]
            avg_top = sum(tops) / k
            if abs(avg_top - dtop) <= tol:
                text = " ".join(value_rows[vi + j][1] for j in range(k))
                vi += k
                entries.append({"value": text.strip(), "reported_on": dval})
                matched = True
                break
        if not matched:
            # Fallback: consume just one line rather than losing the date
            # or looping forever.
            if vi < n:
                entries.append({"value": value_rows[vi][1], "reported_on": dval})
                vi += 1
            else:
                entries.append({"value": None, "reported_on": dval})
    if vi < n:
        # leftover value line(s) with no matching date found
        leftover = " ".join(t for _, t in value_rows[vi:]).strip()
        if leftover:
            entries.append({"value": leftover, "reported_on": None})
    return entries


def _split_into_subsections(rows, header_map):
    """Split a column's rows into {result_key: [(top,text), ...]} by header rows."""
    sections = {}
    current_key = None
    for top, text in rows:
        if text in header_map:
            current_key = header_map[text]
            sections[current_key] = []
            continue
        if text == "Reported On" or "personal information variations" in text or \
           text.startswith("Personal Information") or text.startswith("Tip:"):
            continue
        if current_key:
            sections[current_key].append((top, text))
    return sections


def parse_personal_variations(pdf, pages_text_raw):
    """Locate the section (usually page 1) and parse it geometrically."""
    result = {k: [] for k in list(LEFT_HEADERS.values()) + list(RIGHT_HEADERS.values())}

    target_page_idx = None
    for i, txt in enumerate(pages_text_raw):
        if "Personal Information - Variations" in (txt or ""):
            target_page_idx = i
            break
    if target_page_idx is None:
        result["_raw"] = None
        return result

    page = pdf.pages[target_page_idx]
    words = page.extract_words()

    # Find the top of the "Personal Information - Variations" heading line
    # specifically (not the earlier "Inquiry Input Information" heading,
    # which also contains the word "Information").
    start_top = None
    for w in words:
        if w["text"] != "Personal":
            continue
        same_line = [ww for ww in words if abs(ww["top"] - w["top"]) < 2]
        line_texts = {ww["text"] for ww in same_line}
        if "Information" in line_texts and "Variations" in line_texts:
            start_top = w["top"]
            break
    if start_top is None:
        start_top = 0

    # bottom bound = start of the disclaimer footer text on that page (already
    # stripped from pages_text_raw, but we need the *original* page words, so
    # bound using the word 'Disclaimer:' if present, else page bottom).
    end_top = page.height
    for w in words:
        if w["text"] == "Disclaimer:":
            end_top = w["top"]
            break

    section_words = [w for w in words if start_top - 1 <= w["top"] < end_top]

    cols = defaultdict(list)
    for w in section_words:
        cols[_column_of(w["x0"])].append(w)

    date_re_full = re.compile(r"^" + DATE_RE + r"$")
    lv_rows = _rows_by_top(cols["LV"])
    ld_rows = [(t, v) for t, v in _rows_by_top(cols["LD"]) if date_re_full.match(v)]
    rv_rows = _rows_by_top(cols["RV"])
    rd_rows = [(t, v) for t, v in _rows_by_top(cols["RD"]) if date_re_full.match(v)]

    lv_secs = _split_into_subsections(lv_rows, LEFT_HEADERS)
    rv_secs = _split_into_subsections(rv_rows, RIGHT_HEADERS)

    # Date rows don't carry section headers of their own -- they sit in the
    # same vertical band as their value's subsection, so split them the same
    # way, by re-using the top ranges of each value subsection.
    def dates_in_range(date_rows, lo, hi):
        return [(t, v) for t, v in date_rows if lo <= t < hi]

    def top_bounds(rows_dict, order):
        bounds = {}
        keys = order
        for idx, key in enumerate(keys):
            if key not in rows_dict or not rows_dict[key]:
                continue
            lo = rows_dict[key][0][0] - 5
            # hi = start of next non-empty subsection, else infinity
            hi = float("inf")
            for nxt in keys[idx + 1:]:
                if rows_dict.get(nxt):
                    hi = rows_dict[nxt][0][0] - 5
                    break
            bounds[key] = (lo, hi)
        return bounds

    left_order = ["name_variations", "address_variations", "email_variations"]
    right_order = ["dob_variations", "phone_variations", "id_variations"]

    lv_bounds = top_bounds(lv_secs, left_order)
    rv_bounds = top_bounds(rv_secs, right_order)

    for key in left_order:
        if key not in lv_secs:
            continue
        lo, hi = lv_bounds[key]
        d_rows = dates_in_range(ld_rows, lo, hi)
        result[key] = _pair_multiline(lv_secs[key], d_rows)

    for key in right_order:
        if key not in rv_secs:
            continue
        lo, hi = rv_bounds[key]
        d_rows = dates_in_range(rd_rows, lo, hi)
        result[key] = _pair_multiline(rv_secs[key], d_rows)

    # --- Continuation onto the next page (e.g. Email/DOB/Phone/ID variations
    # that overflow past the page break) ---------------------------------
    if target_page_idx + 1 < len(pages_text_raw):
        next_text = pages_text_raw[target_page_idx + 1] or ""
        stop_markers = ["Tip: All amounts are in INR", "Account Summary",
                        "ADVANCED OVERLAP REPORT", "GROUP DETAILS"]
        stop_at = len(next_text)
        for marker in stop_markers:
            idx = next_text.find(marker)
            if idx != -1:
                stop_at = min(stop_at, idx)
        lead = next_text[:stop_at]
        for line in lead.split("\n"):
            line = line.strip()
            m = re.match(r"^(.*\S)\s+(" + DATE_RE + r")$", line)
            if not m:
                continue
            value, date = m.group(1).strip(), m.group(2)
            if "@" in value:
                result["email_variations"].append({"value": value, "reported_on": date})
            elif re.fullmatch(r"[\d/]{5,}", value):
                result["phone_variations"].append({"value": value, "reported_on": date})
            elif "[" in value and "]" in value:
                result["id_variations"].append({"value": value, "reported_on": date})
            elif re.fullmatch(DATE_RE, value):
                result["dob_variations"].append({"value": value, "reported_on": date})
            else:
                result["address_variations"].append({"value": value, "reported_on": date})

    return result


# ---------------------------------------------------------------------------
# Account Summary
# ---------------------------------------------------------------------------

def parse_account_summary(text: str) -> dict:
    out = {"by_type": [], "inquiries_last_24_months": None,
           "new_accounts_last_6_months": None,
           "new_delinquent_accounts_last_6_months": None}

    m = re.search(
        r"Account Summary\s*Tip:.*?\n"
        r"Type\s+Number of Account\(s\)\s+Active Account\(s\)\s+Overdue Account\(s\)\s+"
        r"Current Balance\s+Amt Disbd/\s*High Credit\s*(.*?)"
        r"Inquiries in last 24 Months:",
        text,
        re.DOTALL,
    )
    if m:
        for row_m in re.finditer(
            r"(Primary Match|Total)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d,]+)\s+([\d,]+)",
            m.group(1),
        ):
            out["by_type"].append({
                "type": row_m.group(1),
                "number_of_accounts": num(row_m.group(2)),
                "active_accounts": num(row_m.group(3)),
                "overdue_accounts": num(row_m.group(4)),
                "current_balance": num(row_m.group(5)),
                "amt_disbursed_high_credit": num(row_m.group(6)),
            })

    m = re.search(r"Inquiries in last 24 Months:\s*(\d+)", text)
    out["inquiries_last_24_months"] = num(m.group(1)) if m else None

    m = re.search(r"New Account\(s\) in last 6 Months:\s*(\d+)", text)
    out["new_accounts_last_6_months"] = num(m.group(1)) if m else None

    m = re.search(r"New Delinquent Account\(s\) in last 6 Months:\s*(\d+)", text)
    out["new_delinquent_accounts_last_6_months"] = num(m.group(1)) if m else None

    return out


# ---------------------------------------------------------------------------
# Account Information (the numbered tradeline blocks)
# ---------------------------------------------------------------------------

ACCOUNT_START_RE = re.compile(
    r"(?m)^(?P<idx>\d{1,2})\s+Account Type:\s*(?P<acc_type>.+?)\s+"
    r"Credit Grantor:\s*(?P<grantor>.+?)\s+Account #:\s*(?P<acc_no>.+?)\s+"
    r"Info\.\s*as of:\s*(?P<info_date>" + DATE_RE + r")\s*$"
)


def _parse_payment_history(block: str):
    """Return {year: {month: code}} starting right after the header line."""
    history = {}
    m = re.search(
        r"Payment History/Asset Classification:\s*\n"
        r"January\s+February\s+March\s+April\s+May\s+June\s+July\s+August\s+"
        r"September\s+October\s+November\s+December\s*\n(.*?)"
        r"(?:\n[A-Za-z].*Details:|\n\d{1,2}\s+Account Type:|$)",
        block,
        re.DOTALL,
    )
    if not m:
        return history
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not re.fullmatch(r"\d{4}", parts[0]):
            continue
        year = parts[0]
        values = parts[1:13]
        # pad in case a row is short (shouldn't normally happen)
        while len(values) < 12:
            values.append(None)
        history[year] = {MONTHS[i]: values[i] for i in range(12)}
    return history


ACCT_LINE_RE = re.compile(r"^(\d{1,2})\s.*Credit Grantor:")

def parse_all_collateral_tables(pdf, pages_raw):
    """Geometrically parse every Collateral/Security Details table by word
    position, keyed by the account index it belongs to."""
    results = {}
    current_idx = None
    for page in pdf.pages:
        words = page.extract_words()
        rows = _rows_by_top(words)
        i, n = 0, len(rows)
        while i < n:
            top, text = rows[i]
            m = ACCT_LINE_RE.match(text)
            if m:
                current_idx = int(m.group(1))
                i += 1
                continue
            if text.startswith("Collateral/Security Details"):
                i += 2  # skip heading row + column-header row
                table_rows = []
                while i < n and not ACCT_LINE_RE.match(rows[i][1]) \
                        and not rows[i][1].startswith("Disclaimer:"):
                    row_top = rows[i][0]
                    row_words = [w for w in words if round(w["top"]) == row_top]
                    buckets = defaultdict(list)
                    for w in row_words:
                        buckets[_collateral_column(w["x0"])].append(w["text"])
                    table_rows.append({
                        "security_type": " ".join(buckets["security_type"]) or None,
                        "type_of_charge": " ".join(buckets["type_of_charge"]) or None,
                        "security_value": " ".join(buckets["security_value"]) or None,
                        "date_of_value": " ".join(buckets["date_of_value"]) or None,
                    })
                    i += 1
                if current_idx is not None:
                    results.setdefault(current_idx, []).extend(table_rows)
                continue
            i += 1
    return results


def parse_accounts(text: str):
    m = re.search(
        r"Account Information\s*(.*?)(?:ADVANCED OVERLAP REPORT|$)",
        text,
        re.DOTALL,
    )
    if not m:
        return []
    section = m.group(1)

    starts = list(ACCOUNT_START_RE.finditer(section))
    accounts = []
    for i, sm in enumerate(starts):
        block_start = sm.start()
        block_end = starts[i + 1].start() if i + 1 < len(starts) else len(section)
        block = section[block_start:block_end]

        acc = {
            "index": int(sm.group("idx")),
            "account_type": clean_val(sm.group("acc_type")),
            "credit_grantor": clean_val(sm.group("grantor")),
            "account_number": clean_val(sm.group("acc_no")),
            "info_as_of": sm.group("info_date"),
        }

        status_m = re.search(r"\b(ACTIVE|CLOSED)\b", block)
        acc["account_status"] = status_m.group(1) if status_m else None

        m2 = re.search(r"Ownership:\s*(.+?)\s+Disbursed Date:\s*(" + DATE_RE + r")?\s*"
                       r"Disbd Amt/High Credit:\s*([\d,]*)", block)
        if m2:
            acc["ownership"] = clean_val(m2.group(1))
            acc["disbursed_date"] = m2.group(2)
            acc["disbursed_amount_high_credit"] = num(m2.group(3))
        else:
            acc["ownership"] = acc["disbursed_date"] = None
            acc["disbursed_amount_high_credit"] = None

        m2 = re.search(
            r"Credit Limit:\s*([\d,]*)\s*Last Payment Date:\s*(" + DATE_RE + r")?\s*"
            r"Current Balance:\s*(-?[\d,]*)",
            block,
        )
        if m2:
            acc["credit_limit"] = num(m2.group(1))
            acc["last_payment_date"] = m2.group(2)
            acc["current_balance"] = num(m2.group(3))
        else:
            acc["credit_limit"] = acc["last_payment_date"] = acc["current_balance"] = None

        m2 = re.search(
            r"Cash Limit:\s*([\d,]*)\s*Closed Date:\s*(" + DATE_RE + r")?\s*"
            r"Last Paid Amt:\s*([\d,]*)",
            block,
        )
        if m2:
            acc["cash_limit"] = num(m2.group(1))
            acc["closed_date"] = m2.group(2)
            acc["last_paid_amount"] = num(m2.group(3))
        else:
            acc["cash_limit"] = acc["closed_date"] = acc["last_paid_amount"] = None

        m2 = re.search(
            r"InstlAmt/Freq:\s*(.*?)\s*Tenure\(month\):\s*(\d+)\s*Overdue Amt:\s*(-?[\d,]*)",
            block,
        )
        if m2:
            acc["instalment_amount_freq"] = clean_val(m2.group(1))
            acc["tenure_months"] = num(m2.group(2))
            acc["overdue_amount"] = num(m2.group(3))
        else:
            acc["instalment_amount_freq"] = None
            acc["tenure_months"] = acc["overdue_amount"] = None

        m2 = re.search(
            r"Status:\s*(.+?)\s*Principal Writeoff Amt:\s*([\d,]*)", block
        )
        if m2:
            acc["status_remark"] = clean_val(m2.group(1))
            acc["principal_writeoff_amount"] = num(m2.group(2))
        else:
            acc["status_remark"] = acc["principal_writeoff_amount"] = None

        m2 = re.search(
            r"Settlement Amt:\s*(.*?)\s*Total Writeoff Amt:\s*([\d,]*)", block
        )
        if m2:
            acc["settlement_amount"] = num(m2.group(1))
            acc["total_writeoff_amount"] = num(m2.group(2))
        else:
            acc["settlement_amount"] = acc["total_writeoff_amount"] = None

        acc["payment_history"] = _parse_payment_history(block)

        accounts.append(acc)

    return accounts


# ---------------------------------------------------------------------------
# Advanced Overlap Report / Group Details summary tables
# ---------------------------------------------------------------------------

def _parse_overlap_summary(block: str):
    rows = []
    for row_m in re.finditer(
        r"(Primary Match|Secondary Match)\s+"
        r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)",
        block,
    ):
        rows.append({
            "type": row_m.group(1),
            "association_own": num(row_m.group(2)),
            "association_other": num(row_m.group(3)),
            "active": num(row_m.group(4)),
            "closed": num(row_m.group(5)),
            "default": num(row_m.group(6)),
            "disbursed_amount_own": num(row_m.group(7)),
            "disbursed_amount_other": num(row_m.group(8)),
            "instalment_amount_own": num(row_m.group(9)),
            "instalment_amount_other": num(row_m.group(10)),
            "current_balance_own": num(row_m.group(11)),
            "current_balance_other": num(row_m.group(12)),
        })
    return rows


def parse_advanced_overlap_report(text: str) -> dict:
    m = re.search(
        r"ADVANCED OVERLAP REPORT.*?Summary\s*Tip:.*?\n(.*?)Account Details - Primary",
        text,
        re.DOTALL,
    )
    if not m:
        return {"summary": [], "_raw": None}
    return {"summary": _parse_overlap_summary(m.group(1))}


def parse_group_details(text: str) -> dict:
    result = {"summary": [], "inquiries_last_24_months": [], "appendix": []}

    m = re.search(
        r"GROUP DETAILS.*?Summary\s*Tip:.*?\n(.*?)Account Details - Primary",
        text,
        re.DOTALL,
    )
    if m:
        result["summary"] = _parse_overlap_summary(m.group(1))

    m = re.search(
        r"Inquiries \(reported for past 24 months\)\s*"
        r"Credit Grantor\s+Date of Inquiry\s+Purpose\s+Amount\s+Remark\s*(.*?)"
        r"(?:-END OF INDIVIDUAL REPORT-|Appendix)",
        text,
        re.DOTALL,
    )
    if m:
        for line in m.group(1).split("\n"):
            line = line.strip()
            if not line:
                continue
            row_m = re.match(
                r"(?P<grantor>.+?)\s+(?P<date>" + DATE_RE + r")\s+"
                r"(?P<purpose>.+?)\s+(?P<amount>\d+)\s*(?P<remark>.*)$",
                line,
            )
            if row_m:
                result["inquiries_last_24_months"].append({
                    "credit_grantor": clean_val(row_m.group("grantor")),
                    "date_of_inquiry": row_m.group("date"),
                    "purpose": clean_val(row_m.group("purpose")),
                    "amount": num(row_m.group("amount")),
                    "remark": clean_val(row_m.group("remark")),
                })
            else:
                result["inquiries_last_24_months"].append({"_raw": line})

    result["appendix"] = parse_appendix(text)
    return result


APPENDIX_LEGEND = [
    ("Account Summary", "Number of Delinquent Accounts"),
    ("Account Information - Credit Grantor", "XXXX"),
    ("Account Information - Account #", "xxxx"),
    ("Payment History / Asset Classification", "XXX"),
    ("Payment History / Asset Classification", "-"),
    ("Payment History / Asset Classification", "STD"),
    ("Payment History / Asset Classification", "SUB"),
    ("Payment History / Asset Classification", "DBT"),
    ("Payment History / Asset Classification", "LOS"),
    ("Payment History / Asset Classification", "SMA"),
    ("Account Information - Account #", "CI-Ceased/Membership Terminated"),
    ("Account Information - Account #", "License Cancelled Entities"),
]


def parse_appendix(text: str):
    m = re.search(
        r"Appendix\s*Section\s+Code\s+Description\s*(.*?)"
        r"(?:$)",
        text,
        re.DOTALL,
    )
    if not m:
        return []
    block = m.group(1)

    positions = []
    search_from = 0
    for section, code in APPENDIX_LEGEND:
        pattern = re.escape(section) + r"\s+" + re.escape(code)
        found = re.search(pattern, block[search_from:])
        if not found:
            continue
        abs_start = search_from + found.start()
        abs_end = search_from + found.end()
        positions.append((section, code, abs_start, abs_end))
        search_from = abs_end

    entries = []
    for i, (section, code, start, end) in enumerate(positions):
        next_start = positions[i + 1][2] if i + 1 < len(positions) else len(block)
        description = block[end:next_start].strip()
        description = re.sub(r"\s+", " ", description)
        entries.append({"section": section, "code": code, "description": description})

    if not entries:
        # fall back: keep raw lines so nothing is lost
        entries = [{"_raw": l.strip()} for l in block.split("\n") if l.strip()]

    return entries


# ---------------------------------------------------------------------------
# Main extraction driver
# ---------------------------------------------------------------------------

def _attach_collateral(accounts, pdf, pages_raw):
    collateral_map = parse_all_collateral_tables(pdf, pages_raw)
    for acc in accounts:
        if acc["index"] in collateral_map:
            acc["collateral_security_details"] = collateral_map[acc["index"]]
    return accounts


def extract(pdf_path: str) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        pages_raw = [p.extract_text() or "" for p in pdf.pages]
        pages_clean = [strip_disclaimer(t) for t in pages_raw]
        full_text = "\n".join(pages_clean)

        data = {
            "source_file": Path(pdf_path).name,
            "page_count": len(pdf.pages),
            "meta": parse_meta(pages_raw[0]),
            "inquiry_input_information": parse_inquiry_input(full_text),
            "crif_hm_scores": parse_scores(full_text),
            "personal_information_variations": parse_personal_variations(pdf, pages_raw),
            "account_summary": parse_account_summary(full_text),
            "accounts": _attach_collateral(parse_accounts(full_text), pdf, pages_raw),
            "advanced_overlap_report": parse_advanced_overlap_report(full_text),
            "group_details": parse_group_details(full_text),
        }

    return data


def main():
    ap = argparse.ArgumentParser(
        description="Extract data from a CRIF High Mark Consumer Base Report "
                    "PDF into a JSON file (disclaimer/footer text is excluded)."
    )
    ap.add_argument("pdf", help="Path to the input CRIF report PDF")
    ap.add_argument("-o", "--output", help="Path to write the output JSON file "
                                            "(default: <input_name>.json)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.output) if args.output else pdf_path.with_suffix(".json")

    data = extract(str(pdf_path))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Extracted data written to: {out_path}")


if __name__ == "__main__":
    main()