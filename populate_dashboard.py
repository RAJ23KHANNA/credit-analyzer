"""
populate_dashboard.py
----------------------
Fills the CIBIL dashboard HTML with live values from the extractor's JSON
output, based on the exact class/context mapping specified by the user.

DESIGN PRINCIPLE (financial-data safety):
- Every target element is located by class attributes AND structural
  context (nearby heading text, parent <tr> label, button onclick id,
  sibling relationship) - never by class alone, because several classes
  in this template are reused for 2-4 different values.
- Every substitution is logged with old value -> new value -> the JSON
  field(s) and formula used to compute it.
- If an anchor can't be found, or the JSON field needed is missing/None,
  the element is left UNTOUCHED and logged as SKIPPED with a reason.
  Nothing is ever guessed or silently defaulted.
- Re-running this script against a different JSON of the same schema
  re-derives every value fresh from that JSON - nothing is hardcoded.

IN-PLACE UPDATE (changed from the original version):
- This version overwrites the SAME dashboard HTML file you pass in,
  rather than producing a separate output file. Before writing, it
  copies the original to a timestamped backup
  (e.g. dashboard.html.bak-20260925-153012) so nothing is ever lost.
- The audit log is still written alongside it, as
  <dashboard>_audit_log.json, so you can see exactly what changed.

KNOWN DISCREPANCIES BETWEEN THE USER'S MAPPING TEXT AND THE ACTUAL HTML
(flagged rather than silently resolved - see NOTES printed at the end):
  - Item 19 ("past 30 days ... font-mono") - the real element has NO
    font-mono class. It is one of FOUR siblings sharing the identical
    class "text-2xl font-black text-slate-900", disambiguated here by
    the adjacent "Past 30 Days" / "Last 12 Months" label text instead.
  - Item 13's "/month" suffix - the real HTML says "/ mo", not "/month".
    The existing suffix text is preserved verbatim; only the number is
    replaced.
  - Items 15/16 ("collateral not there" / "collateral present") are
    computed by STRICT presence/absence of the `collateral_value` field
    in the JSON, exactly as specified. This is worth double-checking:
    Account #3 (Business Loan, Rs 28,00,000) has NO collateral fields
    reported in the source PDF, so by this literal rule it falls in the
    "unsecured" bucket even though a business loan that size might
    intuitively be assumed secured. See the printed NOTES section.

DETAIL POPUPS / DROPDOWNS (added in this version):
- Addresses / Phone Numbers / PAN Cards popups: the static sample cards
  are replaced with one card per entry in the JSON.
- "Active Credit Facilities By Category" -> Details dropdowns: rebuilt
  per category from the ACTIVE accounts in the JSON (account no., EMI,
  balance, overdue, max DPD, credit-facility status, collateral).
- Enquiry ledger popup ("Inspect All N Enquiries"): rebuilt from the
  JSON 'enquiries' list. If the JSON has no 'enquiries' list yet, this
  section is SKIPPED and the existing popup is left untouched.
- These blocks are rebuilt from scratch on every run, so re-running is
  safe (no duplicated rows). All inserted text is HTML-escaped.

REFERENCE DATE ("as of") - used for every "current month DPD" and every
enquiry window (30 days / 3 / 6 / 12 / 24 months):
- Default = the bureau REPORT DATE stored in the JSON (13/02/2026 for this
  report). That makes the numbers reproducible and directly comparable with
  the bureau's own summary lines.
- Use  --as-of today  to measure from the machine's current date, or
  --as-of DD/MM/YYYY  for any specific date.

ADDED IN THIS VERSION: bureau score (header), Max-DPD card captions,
current-month DPD per account (+12M max / lifetime max), account ownership,
per-category loan counts / outstanding / DPD, Total Active Outstanding,
Total Overdue (cross-checked), max unsecured / max secured captions,
credit-card utilisation, Written-Off and Settled cards, enquiry velocity
(30d / 3m / 6m / 12m) computed from the full enquiry ledger, and a
post-write check that the enquiry popup shows every enquiry in the JSON.

Usage: python3 populate_dashboard.py data.json dashboard.html [--as-of report|today|DD/MM/YYYY]
       (dashboard.html is updated in place; a .bak copy is made first)
"""

import sys
import os
import json
import re
import shutil
import argparse
import calendar
from datetime import datetime, timedelta
from html import escape as esc
from itertools import combinations
from bs4 import BeautifulSoup, Tag, NavigableString

AUDIT_LOG = []  # list of dicts: {field, status, old, new, reason}


def log(field, status, old=None, new=None, reason=None):
    AUDIT_LOG.append({"field": field, "status": status, "old": old, "new": new, "reason": reason})


# ---------- helpers ----------

def format_inr(amount):
    """Format an integer amount using Indian digit grouping (e.g. 4384372 -> '43,84,372')."""
    if amount is None:
        return None
    n = int(round(amount))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    last3, rest = s[-3:], s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return sign + ",".join(parts) + "," + last3


def parse_amount(s):
    """'₹28,00,000' -> 2800000. Returns None for None/empty input."""
    if not s:
        return None
    cleaned = s.replace("₹", "").replace(",", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_ddmmyyyy(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def format_day_month_year(dt):
    """'%-d %b %Y' (no leading zero on day) in a way that works on both
    Windows (which needs '%#d') and Linux/Mac (which use '%-d')."""
    try:
        return dt.strftime("%-d %b %Y")
    except ValueError:
        return dt.strftime("%#d %b %Y")


def find_exact(soup, class_string):
    """Elements whose class attribute is EXACTLY this set (not a superset match,
    which is what CSS '.a.b.c' selectors do and which causes false positives
    when Tailwind classes overlap between unrelated elements)."""
    target = set(class_string.split())
    return [el for el in soup.find_all(class_=True) if set(el.get("class", [])) == target]


def replace_leading_number(el, new_value, field_name):
    """Replace only the leading integer in an element's text, keeping any
    trailing text (e.g. ' Active Accounts') exactly as-is."""
    old_text = el.get_text()
    m = re.match(r"^(\s*)(\d+)(.*)$", old_text, re.S)
    if not m:
        log(field_name, "SKIPPED", old=old_text, reason="No leading number found to replace")
        return
    new_text = f"{m.group(1)}{new_value}{m.group(3)}"
    old_full = el.get_text(strip=True)
    el.clear()
    el.append(new_text.strip() if old_text.strip() == old_text else new_text)
    # Preserve original surrounding whitespace structure by just setting string
    el.string = new_text.strip()
    log(field_name, "FILLED", old=old_full, new=el.get_text(strip=True))


# ---------- main population ----------

def populate(data, soup):

    accounts = data.get("accounts", [])

    # ---- 1. Name ----
    field = "Name (Consumer Profile card)"
    els = find_exact(soup, "text-xl font-bold text-slate-900 leading-snug")
    name_el = next((e for e in els if e.find_previous_sibling("p") and
                     "Consumer Profile" in e.find_previous_sibling("p").get_text()), None)
    if name_el is None:
        log(field, "SKIPPED", reason="Anchor (Consumer Profile heading) not found")
    elif not data.get("name"):
        log(field, "SKIPPED", reason="JSON field 'name' is missing/None")
    else:
        old = name_el.get_text(strip=True)
        new_name = data["name"].title()
        name_el.string = new_name
        log(field, "FILLED", old=old, new=new_name,
            reason="Display-cased from raw JSON value (no data change)")

    # ---- 2. DOB ----
    field = "DOB badge"
    els = find_exact(soup, "bg-slate-100 px-2 py-0.5 rounded font-mono font-medium text-slate-700")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    elif not data.get("dob"):
        log(field, "SKIPPED", reason="JSON field 'dob' is missing/None")
    else:
        el = els[0]
        old = el.get_text(strip=True)
        new_text = f"DOB: {data['dob']}"
        el.string = new_text
        log(field, "FILLED", old=old, new=new_text)

    # ---- 3. DATE REPORTED & CERTIFIED (Bureau Report Date card) ----
    field = "Date Reported & Certified (Bureau Report Date card)"
    els = find_exact(soup, "text-xl font-bold text-slate-900 leading-snug")
    date_el = next((e for e in els if e.find_previous_sibling("p") and
                     "Bureau Report Date" in e.find_previous_sibling("p").get_text()), None)
    dates = [parse_ddmmyyyy(a.get("date_reported_certified")) for a in accounts]
    dates = [d for d in dates if d]
    if date_el is None:
        log(field, "SKIPPED", reason="Anchor (Bureau Report Date heading) not found")
    elif not dates:
        log(field, "SKIPPED", reason="No account has a date_reported_certified value")
    else:
        most_recent = max(dates)
        old = date_el.get_text(strip=True)
        new_text = format_day_month_year(most_recent)
        date_el.string = new_text
        log(field, "FILLED", old=old, new=new_text,
            reason="max(date_reported_certified) across all accounts")

    # ---- 4. Oldest account date (span after 'Latest Account Updated') ----
    field = "Oldest account date"
    anchor_els = find_exact(soup, "inline-flex items-center text-emerald-600 font-medium")
    opened_dates = [parse_ddmmyyyy(a.get("date_opened")) for a in accounts]
    opened_dates = [d for d in opened_dates if d]
    if not anchor_els:
        log(field, "SKIPPED", reason="Anchor span ('Latest Account Updated') not found")
    else:
        sib = anchor_els[0].find_next_sibling("span")
        if sib is None:
            log(field, "SKIPPED", reason="Expected sibling <span> ('• Oldest: ...') not found")
        elif not opened_dates:
            log(field, "SKIPPED", reason="No account has a valid date_opened value")
        else:
            oldest = min(opened_dates)
            old = sib.get_text(strip=True)
            new_text = f"• Oldest: {oldest.strftime('%b %Y')}"
            sib.string = new_text
            log(field, "FILLED", old=old, new=new_text,
                reason="min(date_opened) across all accounts")

    # ---- 6/7/8. Addresses / Phones / PAN counts ----
    count_targets = {
        "addressModal": ("Total addresses", len(data.get("addresses", []))),
        "phoneModal": ("Total phone numbers", len(data.get("phone_numbers", {}).get("unique_numbers", []))),
        "panModal": ("Total PAN numbers", len(data.get("pan_numbers", []))),
    }
    els = find_exact(soup, "text-2xl font-bold text-slate-900")
    for el in els:
        btn = el.find_parent("button")
        onclick = btn.get("onclick", "") if btn else ""
        matched_key = next((k for k in count_targets if k in onclick), None)
        if matched_key is None:
            continue
        field, count = count_targets[matched_key]
        old = el.get_text(strip=True)
        el.string = str(count)
        log(field, "FILLED", old=old, new=str(count))

    if data.get("pan_numbers") and len(data["pan_numbers"]) > 1:
        log("PAN count data-quality flag", "NOTE",
            new=f"{len(data['pan_numbers'])} distinct PANs found: {data['pan_numbers']}",
            reason="Source PDF reports two different PAN numbers for this consumer "
                   "(header vs identification table) - not a single verified PAN. "
                   "The PAN count reflects this and the PAN popup now lists every "
                   "PAN with a warning. NOTE: the 'Verified Bureau Match' badge on the "
                   "PAN card is static template text and is NOT updated by this script "
                   "- review it manually.")

    # ---- 9. Primary PAN ----
    field = "Primary PAN"
    els = find_exact(soup, "mt-2 text-xs font-mono font-bold text-slate-800 tracking-wider")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    elif not data.get("primary_pan"):
        log(field, "SKIPPED", reason="JSON field 'primary_pan' is missing/None")
    else:
        el = els[0]
        old = el.get_text(strip=True)
        # Preserve any trailing "(...)" descriptor text verbatim; replace only the PAN token
        new_text = re.sub(r"^[A-Z]{5}\d{4}[A-Z]", data["primary_pan"], old)
        if new_text == old and data["primary_pan"] not in old:
            # no PAN-shaped token found to replace - don't guess where to put it
            log(field, "SKIPPED", old=old, reason="Could not locate a PAN-shaped token to replace safely")
        else:
            el.string = new_text
            log(field, "FILLED", old=old, new=new_text)

    # ---- Primary address ----
    field = "Primary address"
    els = find_exact(soup, "mt-2 text-xs text-slate-500 truncate")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    elif not data.get("primary_address"):
        log(field, "SKIPPED", reason="JSON field 'primary_address' is missing/None")
    else:
        el = els[0]
        old = el.get_text(strip=True)
        addr = data["primary_address"]
        short = (addr[:55] + "...") if len(addr) > 55 else addr
        new_text = f"Primary: {short}"
        el.string = new_text
        el["title"] = addr
        log(field, "FILLED", old=old, new=new_text)

    # ---- Primary phone ----
    field = "Primary phone"
    els = find_exact(soup, "mt-2 text-xs text-slate-500 font-mono")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    elif not data.get("primary_phone"):
        log(field, "SKIPPED", reason="JSON field 'primary_phone' is missing/None")
    else:
        el = els[0]
        old = el.get_text(strip=True)
        num = data["primary_phone"]
        formatted = _fmt_phone(num)
        new_text = f"Primary: {formatted} (Mobile)"
        el.string = new_text
        log(field, "FILLED", old=old, new=new_text)

    # ---- 10. Active accounts badge ----
    field = "Active accounts badge"
    active_accounts = [a for a in accounts if a.get("status") == "ACTIVE"]
    els = find_exact(soup, "px-2 py-0.5 text-xs font-bold bg-sky-100 text-sky-800 rounded-full")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    else:
        old = els[0].get_text(strip=True)
        new_text = f"{len(active_accounts)} Active Accounts"
        els[0].string = new_text
        log(field, "FILLED", old=old, new=new_text)

    # ---- 11. tfoot 'N Accounts' cell ----
    field = "Total portfolio active account count (tfoot)"
    els = find_exact(soup, "py-3.5 px-4 text-center text-slate-900")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    else:
        old = els[0].get_text(strip=True)
        new_text = f"{len(active_accounts)} Accounts"
        els[0].string = new_text
        log(field, "FILLED", old=old, new=new_text)

    # ---- 12/18. Per-category active EMI totals ----
    field_base = "Category active-EMI total"
    category_map = {
        "Housing Loan": "HOUSING LOAN",
        "Business Loan": "BUSINESS LOAN – GENERAL",
        "Consumer Loan": "CONSUMER LOAN",
    }
    els = find_exact(soup, "py-3.5 px-4 text-right font-mono text-slate-700 font-medium")
    for el in els:
        tr = el.find_parent("tr")
        row_label = tr.find("td").get_text(strip=True) if tr else None
        loan_type = category_map.get(row_label)
        field = f"{field_base}: {row_label}"
        if loan_type is None:
            log(field, "SKIPPED", reason=f"Row label '{row_label}' not in known category map - refusing to guess")
            continue
        matching = [a for a in active_accounts if a.get("loan_type") == loan_type]
        emis = [parse_amount(a.get("reported_emi")) for a in matching]
        emis = [e for e in emis if e is not None]
        old = el.get_text(strip=True)
        if not emis and matching:
            log(field, "SKIPPED", old=old,
                reason=f"{len(matching)} active {loan_type} account(s) found but none has a reported_emi")
            continue
        if not matching:
            log(field, "SKIPPED", old=old, reason=f"No active accounts of type {loan_type}")
            continue
        total = sum(emis)
        new_text = f"₹{format_inr(total)}"
        el.string = new_text
        log(field, "FILLED", old=old, new=new_text,
            reason=f"sum(reported_emi) over {len(emis)} active {loan_type} account(s)")

    # ---- 13. Grand total monthly EMI (all active accounts, all categories) ----
    field = "Grand total active EMI / month"
    els = find_exact(soup, "py-3.5 px-4 text-right text-slate-900 font-mono")
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    else:
        el = els[0]
        old = el.get_text(strip=True)
        suffix_match = re.search(r"(/\s*\S*)\s*$", old)  # preserve exact existing suffix e.g. "/ mo"
        suffix = suffix_match.group(1) if suffix_match else ""
        emis = [parse_amount(a.get("reported_emi")) for a in active_accounts]
        emis = [e for e in emis if e is not None]
        if not emis:
            log(field, "SKIPPED", old=old, reason="No active account has a reported_emi value")
        else:
            total = sum(emis)
            new_text = f"₹{format_inr(total)} {suffix}".strip()
            el.string = new_text
            log(field, "FILLED", old=old, new=new_text,
                reason=f"sum(reported_emi) over {len(emis)} of {len(active_accounts)} "
                       f"active account(s) that report an EMI "
                       f"(suffix '{suffix}' preserved verbatim from template)")

    # ---- 14. Total inactive/settled accounts ----
    field = "Total inactive/settled accounts"
    els = find_exact(soup, "text-xs font-semibold text-slate-500 bg-slate-100 px-2.5 py-1 rounded-full border border-slate-200")
    inactive_count = len([a for a in accounts if a.get("status") == "INACTIVE"])
    if not els:
        log(field, "SKIPPED", reason="Anchor not found")
    else:
        old = els[0].get_text(strip=True)
        new_text = f"{inactive_count} Total Inactive / Settled Accounts"
        els[0].string = new_text
        log(field, "FILLED", old=old, new=new_text)



# =====================================================================
# DETAIL POPUPS / DROPDOWNS
# =====================================================================

DASH = "\u2014"  # em dash placeholder (kept out of f-string expressions for py<3.12)

PAN_ENTITY_TYPES = {
    "P": "Individual (P)", "C": "Company (C)", "H": "HUF (H)", "F": "Firm (F)",
    "A": "Association of Persons (A)", "T": "Trust (T)", "B": "Body of Individuals (B)",
    "L": "Local Authority (L)", "J": "Artificial Juridical Person (J)", "G": "Government (G)",
}

# dropdown row id in the HTML -> (label, predicate on JSON loan_type)
CATEGORY_DETAIL_ROWS = {
    "housingDetail":  ("Housing Loan",  lambda t: t == "HOUSING LOAN"),
    "businessDetail": ("Business Loan", lambda t: t.startswith("BUSINESS LOAN")),
    "consumerDetail": ("Consumer Loan", lambda t: t == "CONSUMER LOAN"),
    "personalDetail": ("Personal Loan", lambda t: t == "PERSONAL LOAN"),
}


def _frag(markup):
    """Parse an HTML fragment into a list of top-level nodes."""
    return list(BeautifulSoup(markup, "html.parser").contents)


def _child_tags(el):
    return [c for c in el.children if isinstance(c, Tag)]


def _fmt_phone(num):
    if not num or not num.isdigit():
        return num
    if len(num) == 12 and num.startswith("91"):
        num = num[2:]
    return f"+91 {num[:5]} {num[5:]}" if len(num) == 10 else num


def _swap_modal_body(soup, modal_id, field, new_markup, subtitle=None):
    """Replace everything between a modal's header row and its footer (Close)
    row with new_markup. Refuses to touch the modal unless its structure is
    exactly header / body... / footer-with-closeModal-button.
    Returns the number of old body blocks removed, or None if skipped."""
    modal = soup.find(id=modal_id)
    if modal is None:
        log(field, "SKIPPED", reason=f"Anchor (#{modal_id}) not found")
        return None
    card = next(iter(_child_tags(modal)), None)
    kids = _child_tags(card) if card is not None else []
    if len(kids) < 3 or kids[-1].find("button", onclick=re.compile(r"closeModal")) is None:
        log(field, "SKIPPED",
            reason=f"#{modal_id} does not have the expected header/body/footer structure - refusing to guess")
        return None
    header, footer = kids[0], kids[-1]
    for k in kids[1:-1]:
        k.decompose()
    # drop the blank whitespace nodes the removed blocks leave behind so
    # repeated runs produce byte-identical output
    for n in list(header.next_siblings):
        if n is footer:
            break
        if isinstance(n, NavigableString) and not n.strip():
            n.extract()
    for node in _frag(new_markup):
        footer.insert_before(node)
    if subtitle is not None:
        p = header.find("p")
        if p is not None:
            p.string = subtitle
    return len(kids) - 2


# ---------- Addresses popup ----------

def populate_address_modal(data, soup):
    field = "Addresses popup (all addresses)"
    addrs = data.get("addresses") or []
    if not addrs:
        log(field, "SKIPPED", reason="JSON field 'addresses' is missing/empty")
        return
    cards = []
    for a in addrs:
        code = a.get("residence_code")
        tag = (a.get("category") or "Address") + (f" \u2022 {code}" if code and code != "-" else "")
        cards.append(
            '<div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200">'
            '<div class="flex items-center justify-between mb-1.5">'
            f'<span class="text-xs font-semibold text-slate-600 bg-slate-200 px-2 py-0.5 rounded">{esc(tag)}</span>'
            f'<span class="text-[11px] text-slate-400">Reported: {esc(a.get("date_reported") or DASH)}</span>'
            '</div>'
            f'<p class="text-xs text-slate-700 leading-relaxed">{esc(a.get("address") or "")}</p>'
            '</div>'
        )
    markup = '<div class="space-y-3">' + "".join(cards) + "</div>"
    n = len(addrs)
    if _swap_modal_body(soup, "addressModal", field, markup,
                        subtitle=f"{n} Bureau Reported Address{'es' if n != 1 else ''}") is not None:
        log(field, "FILLED", old="static sample address cards", new=f"{n} address card(s)",
            reason="one card per entry in JSON 'addresses' (report order preserved)")


# ---------- Phone numbers popup ----------

def populate_phone_modal(data, soup):
    field = "Phone numbers popup (all phone numbers)"
    pn = data.get("phone_numbers") or {}
    grouped = {}  # number -> [types], first-seen order
    for p in pn.get("by_type", []):
        if p.get("number"):
            grouped.setdefault(p["number"], []).append(p.get("type") or "Reported")
    for num in pn.get("unique_numbers", []):
        grouped.setdefault(num, [])
    if not grouped:
        log(field, "SKIPPED", reason="JSON has no phone numbers (phone_numbers.by_type / unique_numbers)")
        return
    primary = data.get("primary_phone")
    cards = []
    for num, types in grouped.items():
        uniq_types = list(dict.fromkeys(types))
        badges = ""
        if num == primary:
            badges += ('<span class="text-xs font-bold text-emerald-700 bg-emerald-100 '
                       'px-2 py-0.5 rounded mr-1.5">Primary</span>')
        badges += ('<span class="text-xs font-semibold text-slate-600 bg-slate-200 px-2 py-0.5 rounded">'
                   f'{esc(" / ".join(uniq_types) if uniq_types else "Reported")}</span>')
        seen = f'<span class="text-[11px] text-slate-400 font-mono">Listed {len(types)}\u00d7</span>' if len(types) > 1 else ""
        cards.append(
            '<div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200">'
            f'<div class="flex items-center justify-between"><div>{badges}</div>{seen}</div>'
            f'<div class="mt-2 text-base font-bold font-mono text-slate-900">{esc(_fmt_phone(num))}</div>'
            '</div>'
        )
    markup = '<div class="space-y-3">' + "".join(cards) + "</div>"
    n = len(grouped)
    if _swap_modal_body(soup, "phoneModal", field, markup,
                        subtitle=f"{n} unique number{'s' if n != 1 else ''} reported by bureau") is not None:
        log(field, "FILLED", old="static sample phone cards", new=f"{n} phone card(s)",
            reason="one card per unique number in JSON 'phone_numbers'; report types merged per number")


# ---------- PAN popup ----------

def populate_pan_modal(data, soup):
    field = "PAN popup (all PAN numbers)"
    pans = data.get("pan_numbers") or []
    if not pans:
        log(field, "SKIPPED", reason="JSON field 'pan_numbers' is missing/empty")
        return
    primary = data.get("primary_pan")
    ordered = sorted(pans, key=lambda p: (p != primary, p))
    holder = (data.get("name") or "").title()
    cards = []
    for pan in ordered:
        entity = PAN_ENTITY_TYPES.get(pan[3:4]) if len(pan) == 10 else None
        is_primary = pan == primary
        cells = ""
        if is_primary and holder:
            cells += ('<div><span class="text-slate-400 text-[11px] block">Holder Name</span>'
                      f'<span class="font-bold text-slate-800">{esc(holder)}</span></div>')
        if entity:
            cells += ('<div><span class="text-slate-400 text-[11px] block">Entity Type</span>'
                      f'<span class="font-bold text-slate-800">{esc(entity)}</span></div>')
        grid = (f'<div class="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-600 border-t '
                f'{"border-amber-200/60" if is_primary else "border-slate-200"} pt-3">{cells}</div>') if cells else ""
        label = "Bureau Primary Identity" if is_primary else "Also Reported In This Report"
        box = ("p-4 bg-amber-50/50 rounded-xl border border-amber-200" if is_primary
               else "p-4 bg-slate-50 rounded-xl border border-slate-200")
        lab_cls = "text-amber-800" if is_primary else "text-slate-600"
        cards.append(
            f'<div class="{box}">'
            f'<span class="text-xs font-semibold {lab_cls} uppercase tracking-wide">{label}</span>'
            f'<div class="mt-3 text-2xl font-black font-mono text-slate-900 tracking-widest">{esc(pan)}</div>'
            f'{grid}</div>'
        )

    if len(pans) == 1:
        note = ('<p class="text-xs text-slate-500">Single PAN found in the report; '
                'no conflicting PAN identified.</p>')
    else:
        near = [d for a, b in combinations(pans, 2) if len(a) == len(b)
                for d in [sum(x != y for x, y in zip(a, b))] if d <= 2]
        typo = (f" The PANs differ by only {min(near)} character(s), which can indicate a "
                "data-entry or reporting error in one of the records.") if near else ""
        note = ('<p class="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2.5">'
                f'{len(pans)} different PAN numbers are reported for this consumer. '
                f'{"The first-listed PAN (" + esc(primary) + ") is shown as primary. " if primary else ""}'
                f'Verify against the source report before relying on either.{typo}</p>')
    if _swap_modal_body(soup, "panModal", field, "".join(cards) + note) is not None:
        log(field, "FILLED", old="static sample PAN card + 'single PAN verified' note",
            new=f"{len(pans)} PAN card(s) + data-driven note",
            reason="one card per entry in JSON 'pan_numbers'; primary = JSON 'primary_pan'")


# ---------- Active credit facilities: category Details dropdowns ----------

def _account_card(a):
    as_of = CTX["as_of"]
    overdue_amt = parse_amount(a.get("overdue_amount")) or 0
    cur = current_dpd(a, as_of)
    life = a.get("highest_dpd_value")
    m12 = max_dpd_last_months(a, as_of, 12)

    if a.get("reported_emi"):
        emi = a["reported_emi"]
    elif a.get("calculated_emi"):
        emi = f"\u20b9{format_inr(a['calculated_emi'])} (calc.)"
    else:
        emi = "N/A"

    meta = [f"Opened: {a.get('date_opened') or DASH}",
            f"Sanction: {a.get('sanctioned_amount') or DASH}"]
    if a.get("interest_rate_pct") is not None:
        meta.append(f"Rate: {a['interest_rate_pct']}%")
    if a.get("collateral_value"):
        ct = f" ({a['collateral_type']})" if a.get("collateral_type") else ""
        meta.append(f"Collateral: {a['collateral_value']}{ct}")

    own = a.get("ownership")
    own_cls = {"GUARANTOR": "bg-amber-100 text-amber-800", "JOINT": "bg-sky-100 text-sky-800"}.get(own, "bg-slate-100 text-slate-600")
    own_badge = (f'<span class="ml-2 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase {own_cls}" '
                 f'title="Account ownership per bureau">{esc(own.title())}</span>') if own else ""

    # current-month DPD (as of the reference date) + 12M / lifetime max
    if cur is None:
        dpd_txt, dpd_cls = "DPD now: \u2014 (no history)", "text-slate-400 font-semibold"
    else:
        shown = str(cur["num"]) if cur["num"] is not None else cur["value"]
        dpd_txt = f"DPD {mon_yy(cur['when'])}: {shown}"
        if cur["months_behind"] > 2:
            dpd_txt += " (stale)"
        tone = dpd_tone(cur["num"])
        dpd_cls = {"red": "text-red-600 font-bold", "amber": "text-amber-600 font-bold",
                   "emerald": "text-emerald-600 font-bold", "slate": "text-slate-500 font-semibold"}[tone]
    dpd_title = (f"Current-month DPD as of {mon_yy(as_of)}: the latest reported cell on/before that month. "
                 "'stale' = last reported more than 2 months earlier.")
    hist_txt = f"12M max: {m12 if m12 is not None else DASH} \u00b7 Lifetime max: {life if life is not None else DASH}"

    overdue_span = (f'<span class="text-red-600 font-bold">Overdue: {esc(a["overdue_amount"])}</span>'
                    if overdue_amt > 0 else "")

    badge = ""
    cfs = a.get("credit_facility_status")
    if cfs:
        extra = f" \u2022 {a['written_off_total']}" if a.get("written_off_total") else ""
        cls = "bg-red-100 text-red-700" if cfs == "WRITTEN-OFF" else "bg-amber-100 text-amber-700"
        badge = f'<span class="px-2 py-0.5 rounded-md font-bold {cls}">{esc(cfs + extra)}</span>'

    return (
        '<div class="p-3 bg-white border border-slate-200 rounded-xl flex flex-wrap justify-between items-center gap-2">'
        f'<div><span class="font-bold text-slate-800">Account #{esc(str(a.get("account_no")))}</span>{own_badge}'
        f'<span class="text-slate-500 ml-2">{esc(" | ".join(meta))}</span></div>'
        '<div class="flex flex-wrap items-center gap-4">'
        f'<span>EMI: <strong>{esc(emi)}</strong></span>'
        f'<span>Balance: <strong>{esc(a.get("current_balance") or DASH)}</strong></span>'
        f'{overdue_span}'
        f'<span class="{dpd_cls}" title="{esc(dpd_title)}">{esc(dpd_txt)}</span>'
        f'<span class="text-slate-500">{esc(hist_txt)}</span>'
        f'{badge}</div></div>'
    )


def populate_category_details(data, soup):
    active = [a for a in data.get("accounts", []) if a.get("status") == "ACTIVE"]
    for row_id, (label, matches) in CATEGORY_DETAIL_ROWS.items():
        field = f"Category dropdown: {label}"
        row = soup.find("tr", id=row_id)
        td = row.find("td") if row is not None else None
        if td is None:
            log(field, "SKIPPED", reason=f"Anchor (<tr id='{row_id}'>) not found")
            continue
        accs = sorted((a for a in active if matches(a.get("loan_type") or "")),
                      key=lambda a: a.get("account_no") or 0)
        if accs:
            markup = '<div class="space-y-2 text-xs">' + "".join(_account_card(a) for a in accs) + "</div>"
        else:
            markup = '<p class="text-xs text-slate-500">No active accounts of this type in the report.</p>'
        old_n = len(td.find_all("div", class_=lambda c: c and "border-slate-200" in c))
        td.clear()
        for node in _frag(markup):
            td.append(node)
        log(field, "FILLED", old=f"{old_n} account card(s)",
            new=f"{len(accs)} account card(s): " + (", ".join(f"#{a['account_no']}" for a in accs) or "none"),
            reason=f"ACTIVE accounts whose loan_type maps to '{label}'")


# ---------- Enquiry ledger popup ----------

def populate_enquiries(data, soup):
    field = "Enquiry ledger popup (all enquiries)"
    enq = data.get("enquiries")
    if not enq:
        log(field, "SKIPPED",
            reason="JSON has no 'enquiries' list - the extractor needs the extract_enquiries() addition "
                   "before this popup can be filled. Existing popup left untouched.")
        return
    modal = soup.find(id="enquiriesModal")
    tbody = modal.find("tbody") if modal is not None else None
    if tbody is None:
        log(field, "SKIPPED", reason="Anchor (#enquiriesModal table body) not found")
        return

    rows = []
    for e in enq:
        member = e.get("member_name") or "\u2014"
        mem_cls = "text-slate-400" if member.upper() == "NOT DISCLOSED" else "text-slate-800 font-semibold"
        rows.append(
            '<tr class="hover:bg-slate-50">'
            f'<td class="py-2 px-3 font-mono font-medium">{esc(e.get("enquiry_date") or DASH)}</td>'
            f'<td class="py-2 px-3">{esc(e.get("enquiry_purpose") or DASH)}</td>'
            f'<td class="py-2 px-3 text-right font-mono">{esc(e.get("enquiry_amount") or DASH)}</td>'
            f'<td class="py-2 px-3 text-center {mem_cls}">{esc(member)}</td></tr>'
        )
    old_rows = len(tbody.find_all("tr"))
    tbody.clear()
    for node in _frag("".join(rows)):
        tbody.append(node)
    n = len(enq)

    # count labels tied to the ledger
    h4 = next((h for h in modal.find_all("h4") if "Enquiry History Log" in h.get_text()), None)
    if h4 is not None:
        h4.string = f"Enquiry History Log ({n} Enquiries)"
    card = next(iter(_child_tags(modal)), None)
    foot = _child_tags(card)[-1] if card is not None and _child_tags(card) else None
    span = foot.find("span") if foot is not None else None
    if span is not None:
        span.string = f"Showing all {n} enquiries"
    btn = soup.find("button", onclick=re.compile(r"openModal\('enquiriesModal'\)"))
    txt = btn.find(string=re.compile(r"Inspect All")) if btn is not None else None
    if txt is not None:
        txt.replace_with(re.sub(r"Inspect All \d+ Enquiries", f"Inspect All {n} Enquiries", str(txt)))

    log(field, "FILLED", old=f"{old_rows} sample rows", new=f"{n} rows",
        reason="one row per entry in JSON 'enquiries' (bureau order preserved); "
               "title, footer and 'Inspect All N Enquiries' button count updated")

    total = (data.get("enquiry_summary") or {}).get("total_enquiries")
    if total is not None and total != n:
        log("Enquiry count cross-check", "NOTE", new=f"list has {n} rows but summary total is {total}",
            reason="Extractor may have missed or mis-parsed some enquiry rows - compare against the source PDF.")


# =====================================================================
# REFERENCE DATE, DPD + WINDOW HELPERS
# =====================================================================

CTX = {"as_of": None, "as_of_src": None}

TONES = {  # tone -> tailwind classes for solid badges
    "red": "bg-red-100 text-red-700",
    "amber": "bg-amber-100 text-amber-700",
    "emerald": "bg-emerald-100 text-emerald-700",
    "slate": "bg-slate-100 text-slate-500",
}
TONES_SOFT = {  # tone -> classes for the lighter "-50" badges
    "red": "text-red-600 bg-red-50",
    "amber": "text-amber-600 bg-amber-50",
    "emerald": "text-emerald-600 bg-emerald-50",
    "slate": "text-slate-500 bg-slate-100",
}
COLOR_RE = re.compile(r"\b(text|bg|border)-(?:red|amber|emerald|slate|sky|orange|rose)-(\d{2,3})(/\d+)?")


def swap_color(el, color):
    """Re-colour an element's tailwind classes, keeping shades/opacity."""
    if el is None:
        return
    cls = " ".join(el.get("class", []))
    el["class"] = COLOR_RE.sub(lambda m: f"{m.group(1)}-{color}-{m.group(2)}{m.group(3) or ''}", cls).split()


def add_months(dt, n):
    t = dt.year * 12 + (dt.month - 1) + n
    y, m0 = divmod(t, 12)
    return dt.replace(year=y, month=m0 + 1, day=min(dt.day, calendar.monthrange(y, m0 + 1)[1]))


def mon_yy(dt):
    return dt.strftime("%b '%y")


def pretty_type(t):
    return t.title() if t else DASH


def resolve_as_of(arg, data):
    arg = (arg or "report").strip()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if arg.lower() == "today":
        return today, "today's date"
    if arg.lower() == "report":
        rd = parse_ddmmyyyy((data.get("report_meta") or {}).get("report_date"))
        if rd:
            return rd, "bureau report date"
        return today, "today's date (JSON has no report_meta.report_date)"
    d = parse_ddmmyyyy(arg)
    if d:
        return d, "date supplied with --as-of"
    raise SystemExit(f"--as-of must be 'report', 'today' or DD/MM/YYYY (got {arg!r})")


def dpd_num(v):
    if v is None:
        return None
    if re.fullmatch(r"\d{3}", v):
        return int(v)
    return 0 if v == "STD" else None


def current_dpd(a, as_of):
    """Most recent REPORTED (not XXX) DPD cell on or before the as-of month."""
    cutoff = (as_of.year, as_of.month)
    hist = [h for h in (a.get("dpd_history") or [])
            if (h["year"], h["month"]) <= cutoff and h["value"] != "XXX"]
    if not hist:
        return None
    last = max(hist, key=lambda h: (h["year"], h["month"]))
    behind = (as_of.year - last["year"]) * 12 + as_of.month - last["month"]
    return {"value": last["value"], "num": dpd_num(last["value"]),
            "when": datetime(last["year"], last["month"], 1), "months_behind": behind}


def max_dpd_last_months(a, as_of, months):
    lo = add_months(as_of.replace(day=1), -(months - 1))
    lo_k, hi_k = (lo.year, lo.month), (as_of.year, as_of.month)
    vals = [dpd_num(h["value"]) for h in (a.get("dpd_history") or [])
            if lo_k <= (h["year"], h["month"]) <= hi_k]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def dpd_tone(n):
    if n is None:
        return "slate"
    return "emerald" if n == 0 else ("amber" if n < 90 else "red")


def dpd_band(n):
    if n is None:
        return "No DPD Data", "slate"
    if n >= 180:
        return "Critical DPD", "red"
    if n >= 90:
        return "Severe DPD", "red"
    if n >= 30:
        return "Moderate DPD", "amber"
    if n >= 1:
        return "Minor DPD", "amber"
    return "No DPD", "emerald"


def _active(data):
    return [a for a in data.get("accounts", []) if a.get("status") == "ACTIVE"]


def _card_of(el):
    return el.find_parent("div", class_=lambda c: c and "rounded-2xl" in c)


# =====================================================================
# BUREAU SCORE, MAX DPD CARD
# =====================================================================

def populate_bureau_score(data, soup):
    field = "Bureau score (header)"
    bs = data.get("bureau_score") or {}
    score = bs.get("score")
    lab = soup.find("span", string=lambda s: s and s.strip() == "Bureau Score")
    el = lab.find_next_sibling("span") if lab else None
    if el is None:
        log(field, "SKIPPED", reason="Anchor ('Bureau Score' label + value span) not found")
        return
    if score is None:
        log(field, "SKIPPED", reason="JSON bureau_score.score is missing/None (re-run the extractor)")
        return
    old = el.get_text(" ", strip=True)
    first = next(el.children, None)
    if first is None or first.name is not None:
        log(field, "SKIPPED", old=old, reason="Unexpected element structure - refusing to guess")
        return
    first.replace_with(f"{score} ")
    rmax = bs.get("range_max") or 900
    inner = el.find("span")
    if inner is not None:
        inner.string = f"/ {rmax}"
    color = "emerald" if score >= 750 else ("amber" if score >= 650 else "red")
    swap_color(el, color)
    factors = bs.get("scoring_factors") or []
    el["title"] = (f"{bs.get('model') or 'Bureau'} score {score} ({bs.get('range_min', 300)}-{rmax})"
                   + (". Scoring factors: " + "; ".join(factors) if factors else ""))
    log(field, "FILLED", old=old, new=el.get_text(" ", strip=True),
        reason=f"JSON bureau_score.score; colour band: >=750 green, 650-749 amber, <650 red -> {color}")


def populate_max_dpd(data, soup):
    field = "Max DPD card (value, badge, caption)"
    accounts = data.get("accounts", [])
    label = soup.find(lambda t: t.name == "p" and "Max DPD Across Accounts" in t.get_text())
    row = label.find_next_sibling("div") if label else None
    h2 = row.find("h2") if row else None
    badge = h2.find_next_sibling("span") if h2 else None
    caption = h2.parent.find_next_sibling("p") if h2 else None
    if h2 is None or badge is None or caption is None:
        log(field, "SKIPPED", reason="Anchor (Max DPD Across Accounts card) not found")
        return
    vals = [a["highest_dpd_value"] for a in accounts if a.get("highest_dpd_value") is not None]
    if not vals:
        log(field, "SKIPPED", reason="No account has a highest_dpd_value")
        return
    max_dpd = max(vals)
    old = (h2.get_text(" ", strip=True), badge.get_text(strip=True), caption.get_text(strip=True))

    first = next(h2.children, None)
    if first is None or first.name is not None:
        log(field, "SKIPPED", old=str(old), reason="Unexpected element structure - refusing to guess")
        return
    first.replace_with(f"{max_dpd} ")

    band, tone = dpd_band(max_dpd)
    badge.string = band

    as_of = CTX["as_of"]
    active = _active(data)
    cur = [(a, current_dpd(a, as_of)) for a in active]
    severe = [a for a, c in cur if c and c["num"] is not None and c["num"] > 180]
    now_vals = [c["num"] for _, c in cur if c and c["num"] is not None]
    if severe:
        n = len(severe)
        cap = f"{n} Active Account{'s' if n != 1 else ''} in severe default (>180 DPD)"
    elif now_vals and max(now_vals) > 0:
        cap = f"No active account above 180 DPD (highest current: {max(now_vals)} DPD)"
    else:
        cap = "No active account currently past due"
    caption.string = cap
    for el in (h2, h2.find("span"), badge, caption):
        swap_color(el, tone)
    log(field, "FILLED", old=str(old),
        new=str((h2.get_text(" ", strip=True), band, cap)),
        reason=f"value = max(highest_dpd_value) over all accounts; badge band: >=180 Critical, >=90 Severe, "
               f">=30 Moderate, >=1 Minor, 0 None; caption counts ACTIVE accounts whose current-month DPD "
               f"(as of {mon_yy(as_of)}) is >180 -> " + (", ".join(f"#{a['account_no']}" for a in severe) or "none"))


# =====================================================================
# ACTIVE FACILITIES TABLE: per-category counts / outstanding / DPD, totals
# =====================================================================

CATEGORY_TABLE = [  # (row label in HTML, matches(loan_type), row styled as "active" in template)
    ("Housing Loan", lambda t: t == "HOUSING LOAN", True),
    ("Business Loan", lambda t: t.startswith("BUSINESS LOAN"), True),
    ("Consumer Loan", lambda t: t == "CONSUMER LOAN", True),
    ("Personal Loan", lambda t: t == "PERSONAL LOAN", True),
    ("Credit Card", lambda t: t == "CREDIT CARD", True),
    ("Loan Against Property (LAP)", lambda t: t == "PROPERTY LOAN", False),
    ("Vehicle / Two-Wheeler", lambda t: t in ("TWO-WHEELER LOAN", "AUTO LOAN (PERSONAL)", "USED CAR LOAN"), False),
    ("Gold Loan", lambda t: t == "GOLD LOAN", False),
]


def _bal(a):
    return parse_amount(a.get("current_balance")) or 0


def populate_category_table(data, soup):
    as_of = CTX["as_of"]
    accounts = data.get("accounts", [])
    active = _active(data)

    h3 = soup.find("h3", string=lambda s: s and "Active Credit Facilities By Category" in s)
    section = h3.find_parent("section") if h3 else None
    table = section.find("table") if section else None
    tbody = table.find("tbody") if table else None
    tfoot = table.find("tfoot") if table else None
    if tbody is None:
        log("Category table", "SKIPPED", reason="Anchor (Active Credit Facilities By Category table) not found")
        return

    rows = {}
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) == 6:
            rows[re.sub(r"\s+", " ", tds[0].get_text()).strip()] = tds

    mapped_ids, cat_total = set(), 0
    for label, matches, _styled_active in CATEGORY_TABLE:
        tds = rows.get(label)
        if tds is None:
            log(f"Category row: {label}", "SKIPPED", reason="Row label not found in table")
            continue
        accs = [a for a in active if matches(a.get("loan_type") or "")]
        closed = [a for a in accounts if a.get("status") == "INACTIVE" and matches(a.get("loan_type") or "")]
        mapped_ids.update(a["account_no"] for a in accs)
        outstanding = sum(_bal(a) for a in accs)
        cat_total += outstanding

        # --- count
        cnt_cell = tds[1].find("span") or tds[1]
        old_cnt = cnt_cell.get_text(strip=True)
        cnt_cell.string = str(len(accs))
        log(f"Category count: {label}", "FILLED", old=old_cnt, new=str(len(accs)),
            reason=f"number of ACTIVE accounts of this type ({', '.join('#' + str(a['account_no']) for a in accs) or 'none'})")

        # --- total outstanding
        old_out = tds[3].get_text(strip=True)
        tds[3].string = f"\u20b9{format_inr(outstanding)}"
        log(f"Category outstanding: {label}", "FILLED", old=old_out, new=tds[3].get_text(strip=True),
            reason="sum(current_balance) over the ACTIVE accounts counted above")

        # --- status / DPD
        span = tds[4].find("span")
        if span is not None:
            old_st = span.get_text(strip=True)
            curs = [current_dpd(a, as_of) for a in accs]
            nums = [c["num"] for c in curs if c and c["num"] is not None]
            life = [a["highest_dpd_value"] for a in accs if a.get("highest_dpd_value") is not None]
            if accs:
                dpd = max(nums) if nums else None
                text = f"DPD {dpd}" if dpd is not None else "DPD \u2014"
                span["class"] = ["px-2", "py-0.5", "text-xs", "font-bold", "rounded-md"] + TONES[dpd_tone(dpd)].split()
                stale = [f"#{a['account_no']} (last reported {mon_yy(c['when'])})"
                         for a, c in zip(accs, curs) if c and c["months_behind"] > 2]
                span["title"] = (f"Current-month DPD as of {mon_yy(as_of)} (worst active account). "
                                 f"Lifetime max: {max(life) if life else DASH}."
                                 + (f" Stale reporting: {', '.join(stale)}." if stale else ""))
                if stale:
                    log(f"Category DPD staleness: {label}", "NOTE", new=", ".join(stale),
                        reason="ACTIVE account(s) whose latest DPD cell is >2 months before the as-of date; "
                               "the DPD shown is the last reported value, not a current one.")
            elif closed:
                text = f"{len(closed)} Closed Prior"
                span["class"] = ["text-xs", "text-slate-400"]
            else:
                text = "No Active Account"
                span["class"] = ["text-xs", "text-slate-400"]
            span.string = text
            log(f"Category status: {label}", "FILLED", old=old_st, new=text,
                reason=f"worst current-month DPD (as of {mon_yy(as_of)}) among active accounts; "
                       "closed-prior count shown when there are no active accounts")

        # --- credit-card limit caption
        if label == "Credit Card":
            lim = tds[5].find("span")
            limits = [parse_amount(a.get("sanctioned_amount")) or 0 for a in accs]
            if lim is not None and accs:
                old_l = lim.get_text(strip=True)
                lim.string = f"Limit: \u20b9{format_inr(sum(limits))}"
                log("Credit card row: limit caption", "FILLED", old=old_l, new=lim.get_text(strip=True),
                    reason="sum(high-credit amount) over ACTIVE cards")

        if not _styled_active and accs:
            log(f"Category row style: {label}", "NOTE", new=f"{len(accs)} active account(s) in a row styled as empty",
                reason="This template row is greyed-out (built for 0 active accounts). Values were filled, "
                       "but the row has no Details dropdown / active styling - review manually.")

    unmapped = [a for a in active if a["account_no"] not in mapped_ids]
    if unmapped:
        log("Category coverage", "NOTE",
            new="ACTIVE accounts with no category row: " + ", ".join(
                f"#{a['account_no']} ({a.get('loan_type')})" for a in unmapped),
            reason="They are included in Total Active Outstanding but not in any category row.")

    # ----- totals
    total_out = sum(_bal(a) for a in active)
    summ_cur = parse_amount((data.get("account_summary") or {}).get("total_current_balance"))

    lab = soup.find("span", string=lambda s: s and "Total Active Outstanding" in s)
    hdr = lab.find_next_sibling("span") if lab else None
    if hdr is not None:
        old = hdr.get_text(strip=True)
        hdr.string = f"\u20b9{format_inr(total_out)}"
        log("Total Active Outstanding (header)", "FILLED", old=old, new=hdr.get_text(strip=True),
            reason="sum(current_balance) over ACTIVE accounts (inactive accounts excluded)")
    else:
        log("Total Active Outstanding (header)", "SKIPPED", reason="Anchor not found")

    if tfoot is not None:
        cells = tfoot.find_all("td")
        tot_cell = next((c for c in cells if "text-base" in c.get("class", [])), None)
        if tot_cell is not None:
            old = tot_cell.get_text(strip=True)
            tot_cell.string = f"\u20b9{format_inr(total_out)}"
            log("Total Active Outstanding (table footer)", "FILLED", old=old, new=tot_cell.get_text(strip=True),
                reason="same value as the header figure")
        else:
            log("Total Active Outstanding (table footer)", "SKIPPED", reason="Footer cell not found")

    if summ_cur is not None:
        if summ_cur == total_out:
            log("Total outstanding cross-check", "VERIFIED",
                new=f"\u20b9{format_inr(total_out)} == bureau summary 'Current' balance",
                reason="sum of ACTIVE account balances equals the bureau's summary total")
        else:
            log("Total outstanding cross-check", "MISMATCH",
                new=f"computed \u20b9{format_inr(total_out)} vs bureau summary \u20b9{format_inr(summ_cur)}",
                reason="Investigate: extractor may have missed/mis-read an account balance.")
    if cat_total != total_out:
        log("Category sum cross-check", "NOTE",
            new=f"category rows sum to \u20b9{format_inr(cat_total)}, total is \u20b9{format_inr(total_out)}",
            reason="Difference = active accounts outside the listed categories.")
    else:
        log("Category sum cross-check", "VERIFIED", new="category rows add up to Total Active Outstanding")

    # ----- Total Overdue (table footer)
    overdue_sum = sum(parse_amount(a.get("overdue_amount")) or 0 for a in data.get("accounts", []))
    summ_od = parse_amount((data.get("account_summary") or {}).get("total_overdue_amount"))
    od_val = summ_od if summ_od is not None else overdue_sum
    od_cell = tfoot.find("td", string=lambda s: s and "Total Overdue" in s) if tfoot is not None else None
    if od_cell is None:
        log("Total Overdue (table footer)", "SKIPPED", reason="Anchor ('Total Overdue: ...' cell) not found")
    else:
        old = od_cell.get_text(strip=True)
        od_cell.string = f"Total Overdue: \u20b9{format_inr(od_val)}"
        guarantor = sum(parse_amount(a.get("overdue_amount")) or 0 for a in data.get("accounts", [])
                        if a.get("ownership") == "GUARANTOR")
        if guarantor:
            od_cell["title"] = (f"\u20b9{format_inr(guarantor)} of this overdue sits on accounts where the "
                                "consumer is GUARANTOR, not primary borrower.")
        log("Total Overdue (table footer)", "FILLED", old=old, new=od_cell.get_text(strip=True),
            reason="bureau summary 'Overdue' amount (falls back to sum of account overdues)")
        if summ_od is None or summ_od == overdue_sum:
            log("Total Overdue cross-check", "VERIFIED",
                new=f"\u20b9{format_inr(overdue_sum)} = sum of per-account overdue amounts",
                reason="matches the bureau summary" if summ_od is not None else "no summary to compare")
        else:
            log("Total Overdue cross-check", "MISMATCH",
                new=f"summary \u20b9{format_inr(summ_od)} vs account sum \u20b9{format_inr(overdue_sum)}")
        if guarantor:
            log("Overdue held as guarantor", "NOTE", new=f"\u20b9{format_inr(guarantor)} of \u20b9{format_inr(od_val)}",
                reason="Accounts with OWNERSHIP = GUARANTOR: " + ", ".join(
                    f"#{a['account_no']}" for a in data['accounts']
                    if a.get("ownership") == "GUARANTOR" and (parse_amount(a.get("overdue_amount")) or 0) > 0))


# =====================================================================
# MAX UNSECURED / MAX SECURED / CREDIT CARD CARDS
# =====================================================================

def _acct_caption(a):
    own = a.get("ownership")
    return f"{pretty_type(a.get('loan_type'))} (Account #{a['account_no']}" + (f" \u2022 {own.title()})" if own else ")")


def populate_exposure_cards(data, soup):
    accounts = data.get("accounts", [])
    els = find_exact(soup, "text-2xl font-black text-slate-900 font-mono")
    label_map = {}
    for el in els:
        card = el.find_parent("div", class_=lambda c: c and "border-slate-200" in c)
        lab = card.find("span", class_=lambda c: c and "uppercase" in c) if card else None
        label_map[lab.get_text(strip=True) if lab else None] = (el, card)

    pairs = [(a, parse_amount(a.get("sanctioned_amount"))) for a in accounts]
    pairs = [(a, amt) for a, amt in pairs if amt is not None]

    def fill(label, field, pool, pool_desc, other_caption):
        el, card = label_map.get(label, (None, None))
        if el is None:
            log(field, "SKIPPED", reason=f"Anchor ('{label}' card) not found")
            return
        if not pool:
            log(field, "SKIPPED", reason=f"No account qualifies ({pool_desc})")
            return
        best, amt = max(pool, key=lambda x: x[1])
        ps = card.find_all("p")
        old = (el.get_text(strip=True), [p.get_text(strip=True) for p in ps[:2]])
        el.string = f"\u20b9{format_inr(amt)}"
        if len(ps) >= 2:
            ps[0].string = _acct_caption(best)
            ps[1].string = other_caption(best, pool)
        log(field, "FILLED", old=str(old), new=str((el.get_text(strip=True), [p.get_text(strip=True) for p in ps[:2]])),
            reason=f"max(sanctioned_amount) where {pool_desc} -> Account #{best['account_no']} "
                   f"({best.get('loan_type')}, {best.get('ownership')}, {best.get('status')})")

    unsecured = [(a, x) for a, x in pairs if a.get("collateral_value") is None]
    secured = [(a, x) for a, x in pairs if a.get("collateral_value") is not None]

    def unsec_cap(best, pool):
        return (f"Highest of {len(pool)} accounts with no collateral reported \u2022 "
                f"{'Active' if best.get('status') == 'ACTIVE' else 'Closed'}")

    def sec_cap(best, pool):
        want_active = best.get("status") != "ACTIVE"
        rest = [(a, x) for a, x in pool if (a.get("status") == "ACTIVE") == want_active]
        word = "Active" if want_active else "Closed"
        if not rest:
            return f"No {word.lower()} secured facility"
        b2, x2 = max(rest, key=lambda x: x[1])
        return f"{word} secured peak: \u20b9{format_inr(x2)} ({pretty_type(b2.get('loan_type'))} #{b2['account_no']})"

    fill("Max Unsecured Loan Taken", "Max unsecured loan (value + captions)", unsecured,
         "collateral_value is None", unsec_cap)
    fill("Max Secured Loan Taken", "Max secured loan (value + captions)", secured,
         "collateral_value is present", sec_cap)

    # ---- credit card block
    field = "Credit card limit / utilisation / balance"
    el, card = label_map.get("Credit Card Limit & Usage", (None, None))
    cards = [a for a in accounts if a.get("loan_type") == "CREDIT CARD"]
    act = [a for a in cards if a.get("status") == "ACTIVE"]
    use = act or cards
    if el is None:
        log(field, "SKIPPED", reason="Anchor ('Credit Card Limit & Usage' card) not found")
        return
    if not use:
        log(field, "SKIPPED", reason="No CREDIT CARD account in the JSON")
        return
    limit = sum(parse_amount(a.get("sanctioned_amount")) or 0 for a in use)
    bal = sum(_bal(a) for a in use)
    util = (bal / limit * 100) if limit else None
    util_span = el.find_next_sibling("span")
    ps = card.find_all("p")
    old = (el.get_text(strip=True), util_span.get_text(strip=True) if util_span else None,
           ps[0].get_text(strip=True) if ps else None)
    el.string = f"\u20b9{format_inr(limit)}"
    if util_span is not None and util is not None:
        util_span.string = f"{util:.1f}% Utilized"
        tone = "emerald" if util < 30 else ("amber" if util < 60 else "red")
        util_span["class"] = ["text-xs", "font-semibold", "px-2", "py-0.5", "rounded"] + TONES_SOFT[tone].split()
    if ps:
        ps[0].string = f"Current Balance: \u20b9{format_inr(bal)}"
    over = [a for a in use if (parse_amount(a.get("sanctioned_amount")) or 0) and _bal(a) > parse_amount(a["sanctioned_amount"])]
    if len(ps) >= 2:
        node = ps[1].find(string=re.compile("Exhaustion History"))
        if node is not None:
            node.replace_with(f" Exhaustion History: {len(over)} Over-limit flag{'s' if len(over) != 1 else ''} "
                              "(balance above high-credit) in this report")
    flagged = [f"#{a['account_no']} {a['credit_facility_status']}" for a in use if a.get("credit_facility_status")]
    if flagged:
        card["title"] = "Card status per bureau: " + ", ".join(flagged)
    log(field, "FILLED", old=str(old),
        new=str((el.get_text(strip=True), util_span.get_text(strip=True) if util_span else None,
                 ps[0].get_text(strip=True) if ps else None)),
        reason=f"limit = sum(high-credit) and balance = sum(current_balance) over "
               f"{'ACTIVE' if act else 'all (none active)'} card(s) "
               f"({', '.join('#' + str(a['account_no']) for a in use)}); utilisation = balance / limit")
    if flagged:
        log("Credit card status flag", "NOTE", new=", ".join(flagged),
            reason="The bureau flags this card's credit-facility status. 'High Credit Amount' is the highest "
                   "amount ever drawn, not necessarily a live limit - treat the utilisation % accordingly.")


# =====================================================================
# WRITTEN-OFF / SETTLED CARDS
# =====================================================================

def _wo_box(a, kind):
    status = a.get("credit_facility_status") or DASH
    own = a.get("ownership")
    closed = a.get("status") != "ACTIVE"
    badge_cls = "bg-red-100 text-red-700" if "WRITTEN" in status else "bg-amber-100 text-amber-700"
    lab = '<span class="text-slate-400 block text-[11px]">{}</span>'
    sub = '<span class="block text-[10px] text-slate-400">{}</span>'

    own_prefix = (own.title() + " \u2022 ") if own else ""
    life_txt = own_prefix + ("Closed" if closed else "Active")
    type_cell = (lab.format("Type") + f'<span class="font-medium">{esc(pretty_type(a.get("loan_type")))}</span>'
                 + sub.format(esc(life_txt)))
    if kind == "wo":
        if a.get("written_off_total"):
            p = a.get("written_off_principle")
            extra = sub.format(esc(f"Principal {p}")) if p and p != a["written_off_total"] else ""
            amt_cell = (lab.format("Amount") + f'<span class="font-medium font-mono">{esc(a["written_off_total"])}</span>' + extra)
        else:
            amt_cell = (lab.format("Amount") + '<span class="font-medium text-slate-400">Not reported</span>'
                        + sub.format(esc(f"Balance {a.get('current_balance') or DASH}")))
        date_lbl = "Date"
        d, dsub = ((a.get("date_closed"), "Account closed") if closed and a.get("date_closed")
                   else (a.get("date_reported_certified"), "Last reported"))
    else:
        if a.get("settlement_amount"):
            amt_cell = (lab.format("Settled Amount") + f'<span class="font-medium font-mono">{esc(a["settlement_amount"])}</span>')
        else:
            amt_cell = (lab.format("Settled Amount") + '<span class="font-medium text-slate-400">Not reported</span>'
                        + sub.format(esc(f"Sanctioned {a.get('sanctioned_amount') or DASH}")))
        date_lbl = "Settlement Date"
        d, dsub = ((a.get("date_closed"), "Account closed") if a.get("date_closed")
                   else (a.get("date_reported_certified"), "Last reported"))
    date_cell = lab.format(date_lbl) + f'<span class="font-medium">{esc(d or "N/A")}</span>' + sub.format(dsub)

    return ('<div class="bg-slate-50 rounded-xl p-3 border border-slate-100 text-xs">'
            '<div class="flex items-center justify-between mb-2">'
            f'<span class="font-bold text-slate-800">Account #{esc(str(a.get("account_no")))}</span>'
            f'<span class="px-2 py-0.5 rounded-md font-bold {badge_cls}">{esc(status)}</span></div>'
            f'<div class="grid grid-cols-3 gap-2 text-slate-600"><div>{type_cell}</div><div>{amt_cell}</div>'
            f'<div>{date_cell}</div></div></div>')


def _fill_flag_card(soup, title, kind, accs, empty_labels, footer_text, badge_tone):
    field = f"{title} card"
    h4 = soup.find("h4", string=lambda s: s and title in s)
    card = h4.find_parent("div", class_=lambda c: c == "rounded-2xl") if h4 else None
    kids = [k for k in card.children if isinstance(k, Tag)] if card is not None else []
    if len(kids) < 3 or kids[-1].name != "p" or kids[0].find("h4") is None:
        log(field, "SKIPPED", reason="Card does not have the expected header / body / footnote structure")
        return
    header, foot = kids[0], kids[-1]
    badge = header.find("span", recursive=False)
    for k in kids[1:-1]:
        k.decompose()
    for n in list(header.next_siblings):
        if n is foot:
            break
        if isinstance(n, NavigableString) and not n.strip():
            n.extract()
    if accs:
        markup = '<div class="space-y-2">' + "".join(_wo_box(a, kind) for a in accs) + "</div>"
    else:
        markup = ('<div class="bg-slate-50 rounded-xl p-3 border border-slate-100 text-xs">'
                  '<div class="grid grid-cols-3 gap-2 text-slate-600">'
                  + "".join(f'<div><span class="text-slate-400 block text-[11px]">{esc(l)}</span>'
                            f'<span class="font-medium">{esc(v)}</span></div>' for l, v in empty_labels)
                  + "</div></div>")
    for node in _frag(markup):
        foot.insert_before(node)
    foot.string = footer_text
    old_badge = badge.get_text(strip=True) if badge is not None else None
    if badge is not None:
        n = len(accs)
        badge.string = f"{n} Account{'s' if n != 1 else ''} Flagged"
        tone = badge_tone if n else "emerald"
        base = {"red": "bg-red-50 text-red-700 border-red-200", "amber": "bg-amber-50 text-amber-700 border-amber-200",
                "emerald": "bg-emerald-50 text-emerald-700 border-emerald-200"}[tone]
        badge["class"] = ["text-xs", "font-semibold", "px-2", "py-0.5", "border", "rounded-md"] + base.split()
    log(field, "FILLED", old=old_badge, new=f"{len(accs)} account(s): " + (", ".join(f"#{a['account_no']}" for a in accs) or "none"))


def populate_wo_settled(data, soup):
    accounts = sorted(data.get("accounts", []), key=lambda a: a.get("account_no") or 0)
    wo = [a for a in accounts if a.get("is_written_off") or a.get("credit_facility_status") in ("WRITTEN-OFF", "POST (WO) SETTLED")
          or a.get("written_off_total")]
    st = [a for a in accounts if a.get("is_settled") or a.get("credit_facility_status") in ("SETTLED", "POST (WO) SETTLED")]

    with_amt = [a for a in wo if a.get("written_off_total")]
    no_amt = [a for a in wo if not a.get("written_off_total")]
    total = sum(parse_amount(a["written_off_total"]) or 0 for a in with_amt)
    if wo:
        foot = (f"Bureau-reported written-off total: \u20b9{format_inr(total)} across {len(with_amt)} of {len(wo)} account(s). "
                + (f"No written-off amount is reported for {', '.join('#' + str(a['account_no']) for a in no_amt)} "
                   "(their current balance is shown instead). " if no_amt else "")
                + ("Post-write-off settled accounts also appear under Settled." if any(a.get('is_settled') for a in wo) else ""))
    else:
        foot = "No active or inactive facility contains a written-off principle/total reported status in this CIBIL file."
    _fill_flag_card(soup, "Details of Written-Off Accounts", "wo", wo,
                    [("Type", "None Reported"), ("Amount", "\u20b90.00"), ("Date", "N/A")], foot, "red")

    if st:
        have = [a for a in st if a.get("settlement_amount")]
        foot2 = ("Settled / post-write-off-settled facilities per the bureau's credit-facility status. "
                 + ("" if have else "The bureau does not report a settlement amount for these accounts; sanctioned amount and "
                                    "account-closure date are shown instead."))
    else:
        foot2 = "No facility carries a Settled / Post-WO-Settled credit-facility status in this CIBIL file."
    _fill_flag_card(soup, "Details of Settled Accounts", "settled", st,
                    [("Type", "None Flagged"), ("Settled Amount", "\u20b90.00"), ("Settlement Date", "N/A")], foot2, "amber")
    log("Written-off vs settled overlap", "NOTE",
        new=f"{len(wo)} written-off ({', '.join('#' + str(a['account_no']) for a in wo)}); "
            f"{len(st)} settled ({', '.join('#' + str(a['account_no']) for a in st)})",
        reason="'POST (WO) SETTLED' is both written-off and settled, so it is listed in both cards.")


# =====================================================================
# ENQUIRY VELOCITY (30d / 3m / 6m / 12m) - computed from the full ledger
# =====================================================================

def enquiry_window_counts(enq, as_of):
    starts = {"30d": as_of - timedelta(days=30), "3m": add_months(as_of, -3), "6m": add_months(as_of, -6),
              "12m": add_months(as_of, -12), "24m": add_months(as_of, -24)}
    dated = [(e, parse_ddmmyyyy(e.get("enquiry_date"))) for e in enq]
    valid = [(e, d) for e, d in dated if d and d <= as_of]
    counts = {k: sum(1 for _, d in valid if s <= d) for k, s in starts.items()}
    return starts, counts, valid, [e for e, d in dated if d is None or d > as_of]


def populate_enquiry_velocity(data, soup):
    field = "Enquiry velocity"
    enq = data.get("enquiries") or []
    if not enq:
        log(field, "SKIPPED", reason="JSON has no 'enquiries' list - re-run the extractor")
        return
    as_of = CTX["as_of"]
    starts, counts, valid, excluded = enquiry_window_counts(enq, as_of)
    if excluded:
        log("Enquiry velocity: excluded rows", "NOTE", new=f"{len(excluded)} row(s) with no/unparseable date or dated after {as_of:%d/%m/%Y}",
            reason="Excluded from the window counts; still listed in the popup.")

    h4 = soup.find("h4", string=lambda s: s and "Bureau Enquiry Velocity" in s)
    box = h4.find_parent("div", class_=lambda c: c == "rounded-2xl") if h4 else None
    grid = box.find("div", class_=lambda c: c == "sm:grid-cols-4") if box else None
    if grid is None:
        log(field, "SKIPPED", reason="Anchor (Bureau Enquiry Velocity card grid) not found")
        return

    total_badge = box.find("span", string=re.compile(r"Total Bureau Enquiries"))
    if total_badge is not None:
        old = total_badge.get_text(strip=True)
        total_badge.string = f"{len(enq)} Total Bureau Enquiries"
        log("Enquiries - total badge", "FILLED", old=old, new=total_badge.get_text(strip=True),
            reason="number of rows in JSON 'enquiries'")

    most_recent = max((d for _, d in valid), default=None)
    b = data.get("enquiry_summary") or {}

    def top_purposes(k, n=2):
        c = {}
        for e, d in valid:
            if starts[k] <= d:
                c[pretty_type(e.get("enquiry_purpose"))] = c.get(pretty_type(e.get("enquiry_purpose")), 0) + 1
        return " \u00b7 ".join(f"{p} \u00d7{v}" for p, v in sorted(c.items(), key=lambda x: (-x[1], x[0]))[:n]) or "No enquiries"

    def rng(k):
        return f"{mon_yy(starts[k])} \u2013 {mon_yy(as_of)}"

    n30, n3, n6, n12, n24 = (counts[k] for k in ("30d", "3m", "6m", "12m", "24m"))
    mism = (b.get("past_12_months") not in (None, n12)) or (b.get("past_24_months") not in (None, n24))
    plans = {
        "Past 30 Days": (n30, ("soft", "No Activity" if n30 == 0 else ("Moderate" if n30 < 3 else "High Activity"),
                               "emerald" if n30 == 0 else ("amber" if n30 < 3 else "red")),
                         f"Recent: {most_recent:%d/%m/%Y}" if most_recent else "No enquiries", "30d"),
        "Last 3 Months": (n3, ("plain", rng("3m"), None), top_purposes("3m"), "3m"),
        "Last 6 Months": (n6, ("plain", rng("6m"), None), f"Avg {n6 / 6:.1f} per month \u00b7 {top_purposes('6m', 1)}", "6m"),
        "Last 12 Months": (n12, ("soft", f"{n12} Inquiries", "emerald" if n12 == 0 else ("amber" if n12 < 6 else "red")),
                           f"{n24} in last 24 months" + (f" \u2022 Bureau summary: {b.get('past_12_months')} / {b.get('past_24_months')}" if mism else ""),
                           "12m"),
    }
    for card in [c for c in grid.children if isinstance(c, Tag)]:
        lab = card.find("span")
        label = lab.get_text(strip=True) if lab else None
        if label not in plans:
            log(f"Enquiries - {label}", "SKIPPED", reason="Unrecognised card label")
            continue
        n, (style, badge_text, tone), foot_text, key = plans[label]
        flex = card.find("div")
        spans = flex.find_all("span") if flex else []
        p = card.find("p")
        if len(spans) < 2 or p is None:
            log(f"Enquiries - {label}", "SKIPPED", reason="Unexpected card structure - refusing to guess")
            continue
        old = (spans[0].get_text(strip=True), spans[1].get_text(strip=True), p.get_text(strip=True))
        spans[0].string = str(n)
        spans[1].string = badge_text
        spans[1]["class"] = (["text-xs", "font-bold", "px-2", "py-0.5", "rounded"] + TONES_SOFT[tone].split()
                             if style == "soft" else ["text-xs", "font-medium", "text-slate-500"])
        p.string = foot_text
        card["title"] = f"{starts[key]:%d/%m/%Y} \u2013 {as_of:%d/%m/%Y} ({CTX['as_of_src']}), counted from the {len(enq)}-row enquiry ledger"
        log(f"Enquiries - {label}", "FILLED", old=str(old), new=str((str(n), badge_text, foot_text)),
            reason=f"count of ledger enquiries dated {starts[key]:%d/%m/%Y} to {as_of:%d/%m/%Y} ({CTX['as_of_src']})")

    for key, jk in (("30d", "past_30_days"), ("12m", "past_12_months"), ("24m", "past_24_months")):
        want = b.get(jk)
        if want is None:
            continue
        if want == counts[key]:
            log(f"Enquiry window cross-check ({key})", "VERIFIED", new=f"ledger count {counts[key]} == bureau summary {want}")
        else:
            log(f"Enquiry window cross-check ({key})", "MISMATCH",
                new=f"ledger count {counts[key]} vs bureau summary {want}",
                reason="The dashboard uses the ledger count (dated rows). The bureau's summary line uses its own "
                       "counting rule, which the report does not explain - compare against the source PDF.")


def verify_enquiry_display(data, html_text):
    """Re-parse the HTML exactly as written to disk and prove the enquiry popup
    shows every enquiry in the JSON, in order, with matching counts."""
    enq = data.get("enquiries") or []
    if not enq:
        return
    soup = BeautifulSoup(html_text, "html.parser")
    modal = soup.find(id="enquiriesModal")
    tbody = modal.find("tbody") if modal else None
    if tbody is None:
        log("Enquiry display check", "MISMATCH", reason="Popup table not found in written HTML")
        return
    shown = [[td.get_text(strip=True) for td in tr.find_all("td")] for tr in tbody.find_all("tr")]
    want = [[e.get("enquiry_date") or DASH, e.get("enquiry_purpose") or DASH, e.get("enquiry_amount") or DASH,
             e.get("member_name") or DASH] for e in enq]
    n = len(enq)
    problems = []
    if len(shown) != n:
        problems.append(f"popup has {len(shown)} rows, JSON has {n}")
    elif shown != want:
        bad = next(i for i, (a, b) in enumerate(zip(shown, want)) if a != b)
        problems.append(f"row {bad + 1} differs: popup {shown[bad]} vs JSON {want[bad]}")
    txt = soup.get_text(" ")
    for needle in (f"Enquiry History Log ({n} Enquiries)", f"Inspect All {n} Enquiries", f"Showing all {n} enquiries",
                   f"{n} Total Bureau Enquiries"):
        if needle not in re.sub(r"\s+", " ", txt):
            problems.append(f"label missing/incorrect: '{needle}'")
    tot = (data.get("enquiry_summary") or {}).get("total_enquiries")
    if tot is not None and tot != n:
        problems.append(f"JSON list has {n} rows but bureau summary total is {tot}")
    if problems:
        log("Enquiry display check", "MISMATCH", new="; ".join(problems))
    else:
        log("Enquiry display check", "VERIFIED",
            new=f"popup shows all {n} enquiries, identical to the JSON (date, purpose, amount, member, order); "
                f"title, footer, button and total badge all say {n}; equals bureau total {tot}")



def populate_details(data, soup):
    for fn in (populate_bureau_score, populate_max_dpd, populate_category_table,
               populate_exposure_cards, populate_wo_settled, populate_enquiry_velocity,
               populate_address_modal, populate_phone_modal, populate_pan_modal,
               populate_category_details, populate_enquiries):
        try:
            fn(data, soup)
        except Exception as exc:  # never let one section abort the whole run
            log(fn.__name__, "SKIPPED", reason=f"Unexpected error: {exc!r}")


def print_audit_report():
    by = lambda s: [r for r in AUDIT_LOG if r["status"] == s]
    filled, skipped, notes, ok, bad = by("FILLED"), by("SKIPPED"), by("NOTE"), by("VERIFIED"), by("MISMATCH")

    print("=" * 100)
    print(f"AUDIT REPORT: {len(filled)} filled, {len(skipped)} skipped, {len(ok)} verified, "
          f"{len(bad)} MISMATCH, {len(notes)} notes")
    print("=" * 100)
    print("\n--- FILLED ---")
    for r in filled:
        print(f"[FILLED] {r['field']}")
        print(f"    old: {r['old']!r}")
        print(f"    new: {r['new']!r}")
        if r["reason"]:
            print(f"    basis: {r['reason']}")
    print("\n--- SKIPPED (left untouched) ---")
    for r in skipped:
        print(f"[SKIPPED] {r['field']}")
        print(f"    reason: {r['reason']}")
    if ok:
        print("\n--- VERIFIED (cross-checks that passed) ---")
        for r in ok:
            print(f"[OK] {r['field']}: {r['new']}")
    if bad:
        print("\n--- MISMATCH (needs your attention) ---")
        for r in bad:
            print(f"[MISMATCH] {r['field']}: {r['new']}")
            if r["reason"]:
                print(f"    {r['reason']}")
    if notes:
        print("\n--- DATA-QUALITY NOTES ---")
        for r in notes:
            print(f"[NOTE] {r['field']}: {r['new']}")
            print(f"    {r['reason']}")


def main(json_path, html_path, as_of_arg="report"):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    with open(html_path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    CTX["as_of"], CTX["as_of_src"] = resolve_as_of(as_of_arg, data)
    log("Reference date (as-of)", "NOTE", new=f"{CTX['as_of']:%d/%m/%Y} ({CTX['as_of_src']})",
        reason="All current-month DPD figures and enquiry windows are measured from this date. "
               "Use --as-of today or --as-of DD/MM/YYYY to change it.")

    # Safety net: back up the original file before touching it, so an
    # in-place update is never a one-way door.
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{html_path}.bak-{timestamp}"
    shutil.copy2(html_path, backup_path)

    populate(data, soup)
    populate_details(data, soup)

    html_out = str(soup)
    verify_enquiry_display(data, html_out)   # checks the HTML exactly as it will be written

    # Overwrite the SAME file with the populated version.
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    audit_path = os.path.splitext(html_path)[0] + "_audit_log.json"
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(AUDIT_LOG, f, indent=2, ensure_ascii=False)

    print_audit_report()
    print(f"\nUpdated in place: {html_path}")
    print(f"Backup of original: {backup_path}")
    print(f"Wrote audit log: {audit_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Populate the CIBIL dashboard HTML from extractor JSON (in place).")
    ap.add_argument("json_path")
    ap.add_argument("html_path")
    ap.add_argument("--as-of", default="report", metavar="report|today|DD/MM/YYYY",
                    help="reference date for current-month DPD and enquiry windows (default: bureau report date)")
    args = ap.parse_args()
    main(args.json_path, args.html_path, args.as_of)