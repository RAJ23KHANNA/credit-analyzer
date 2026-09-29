#!/usr/bin/env python3
"""
dashboard_populator.py
======================

Reads the JSON produced by crif_extractor.py and updates the values of the
CIBIL dashboard HTML *in place* (the same file is overwritten - no new HTML
file is created).

Usage
-----
    python3 dashboard_populator.py REPORT.json DASHBOARD.html
    python3 dashboard_populator.py REPORT.json DASHBOARD.html --as-of 2026-09-28
    python3 dashboard_populator.py REPORT.json DASHBOARD.html --backup

Options
-------
  --as-of YYYY-MM-DD   "Current date" used for the 30-day / 3 / 6 / 12 month
                       enquiry windows (default: today's date).
  --backup             Save DASHBOARD.html.bak before overwriting.

What this script populates
---------------------------
  * Header: bureau score, name, latest report date, max DPD (+ severe-DPD
    account count).
  * KYC cards: address / phone / PAN counts, and their "Primary: ..." teaser
    lines.
  * The Address / Phone / PAN popup modals - full lists built from the JSON,
    grouped by distinct value and tagged with the most recently reported one
    as "Primary". The PAN modal raises a red "PAN Mismatch" banner if more
    than one distinct PAN is found in the bureau history.
  * Active Credit Facilities By Category table - rows, footer totals, and an
    enriched "Details" drill-down per category (per-account line items with
    grantor, dates, secured/unsecured tag and status). Categories with no
    active account but with closed accounts on file also get a Details
    drill-down.
  * Credit card limit, current balance and utilization %.
  * Max secured / unsecured loan taken.
  * Written-Off and Settled account summary cards.
  * Bureau Enquiry Velocity (30d / 3m / 6m / 12m) and the enquiries modal -
    ALL enquiries in the JSON are listed, never truncated.
  * A few small "N accounts" pill counters that would otherwise be left
    over from the original sample text.

Data checks
-----------
Before touching the HTML, validate_data() sanity-checks the JSON (required
keys present, totals reconcile, dates parse, PAN consistency, etc.) and
prints a report. These are warnings, not hard failures - a mismatch is
reported but does not stop the run, since a real bureau file can legitimately
have small rounding differences.

How it works
------------
Many elements in the dashboard share identical Tailwind class strings, so a
class name alone can't identify them. Each value is therefore located by
searching for a unique anchor string first (a heading, an id="..." attribute,
a distinctive label) and then applying a regex *after* that point, so the
same class string used in five different places is never confused. Only the
matched inner HTML is replaced - everything else in the file is untouched,
and the script is safe to run repeatedly on the same file (idempotent).

No third-party packages are needed.
"""

import argparse
import calendar
import html as htmllib
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Category table (Active Credit Facilities By Category): only ACTIVE accounts
# are counted for loans / EMI / outstanding / max DPD, so that the category
# rows add up to the "Total Active Outstanding" figure. Set to False to count
# every account regardless of status.
CATEGORY_TABLE_ACTIVE_ONLY = True

# Secured vs unsecured classification (used for "Max Secured / Unsecured Loan"
# and for the "SECURED" tag in the category Details drill-down). A loan whose
# type is in SECURED_TYPES is secured. A loan whose type is in
# UNSECURED_TYPES (or is unknown) is unsecured UNLESS the JSON shows
# collateral / security for that account, in which case it is treated as
# secured.
SECURED_TYPES = {
    "HOUSING LOAN", "PROPERTY LOAN", "LOAN AGAINST PROPERTY", "AUTO LOAN",
    "AUTO LOAN (PERSONAL)", "USED CAR LOAN", "TWO-WHEELER LOAN",
    "COMMERCIAL VEHICLE LOAN", "TRACTOR LOAN", "CONSTRUCTION EQUIPMENT LOAN",
    "GOLD LOAN", "LOAN AGAINST SHARES/SECURITIES", "LOAN AGAINST BANK DEPOSITS",
    "BUSINESS LOAN - SECURED", "LEASING", "SECURED CREDIT CARD",
}
UNSECURED_TYPES = {
    "PERSONAL LOAN", "CONSUMER LOAN", "LOAN ON CREDIT CARD", "EDUCATION LOAN",
    "EDUCATIONAL LOAN", "BUSINESS LOAN - GENERAL", "BUSINESS LOAN - UNSECURED",
    "BUSINESS LOAN", "LOAN TO PROFESSIONAL", "P2P PERSONAL LOAN",
    "MICROFINANCE - PERSONAL LOAN", "OVERDRAFT", "CREDIT CARD",
}
# Revolving card products are not "loans taken" - excluded from both maxima.
NON_LOAN_TYPES = {"CREDIT CARD", "SECURED CREDIT CARD", "CORPORATE CREDIT CARD",
                  "KISSAN CREDIT CARD", "FLEET CARD"}

PAN_ENTITY = {"P": "Individual", "C": "Company", "H": "HUF", "F": "Firm",
              "A": "AOP", "T": "Trust", "B": "BOI", "L": "Local Authority",
              "J": "Artificial Juridical Person", "G": "Government"}

# Colour cycle for category rows (Tailwind colour names).
PALETTE = ["blue", "purple", "teal", "amber", "rose", "indigo", "cyan", "orange"]

# A mask value in the JSON means the grantor/account# was undisclosed
# ("XXXX" / "xxxx" per the CRIF appendix legend).
MASKED = {"XXXX", "xxxx", "-", ""}

WARNINGS = []
APPLIED = []


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def inr(n):
    """1234567 -> ₹12,34,567 (Indian digit grouping)."""
    n = int(round(n or 0))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    return f"{sign}₹{s}"


def parse_amount(v):
    """'32,537/Monthly' / '1,000' / 1000 / None -> number (0 if absent)."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return v
    m = re.match(r"\s*(-?[\d,]+(?:\.\d+)?)", str(v))
    return float(m.group(1).replace(",", "")) if m else 0


def parse_date(s):
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def dpd_of(code):
    """'900/XXX' -> 900, '188/LOS' -> 188, 'XXX/XXX' / '-' / None -> None."""
    m = re.match(r"^\s*(\d+)\s*/", str(code)) if code is not None else None
    return int(m.group(1)) if m else None


def account_max_dpd(acc):
    vals = [dpd_of(c) for months in (acc.get("payment_history") or {}).values()
            for c in months.values()]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def months_ago(d, n):
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def nice_type(t):
    return (t or "Unknown").title().replace(" On ", " on ")


def esc(s):
    return htmllib.escape(str(s), quote=False)


def is_active(acc):
    return (acc.get("account_status") or "").upper() == "ACTIVE"


def has_collateral(acc):
    for row in acc.get("collateral_security_details") or []:
        st = (row.get("security_type") or "").strip().upper()
        if parse_amount(row.get("security_value")) > 0 or (st and st != "NO COLLATERAL"):
            return True
    return False


def is_secured(acc):
    t = (acc.get("account_type") or "").upper()
    if t in SECURED_TYPES:
        return True
    return has_collateral(acc)          # unsecured-type (or unknown) + collateral


def is_written_off(acc):
    remark = (acc.get("status_remark") or "").upper()
    return "WRITTEN-OFF" in remark or "WRITTEN OFF" in remark or \
        parse_amount(acc.get("total_writeoff_amount")) > 0


def is_settled(acc):
    return parse_amount(acc.get("settlement_amount")) > 0


def masked(v):
    return (v or "").strip() in MASKED


def grantor_label(acc):
    return "Not Disclosed" if masked(acc.get("credit_grantor")) else acc.get("credit_grantor")


def fmt_d(s):
    d = parse_date(s)
    return d.strftime("%d/%m/%Y") if d else "N/A"


def split_id_value(v):
    """'AMHPS9420E [PAN]' -> ('AMHPS9420E', 'PAN')."""
    m = re.match(r"^(.*\S)\s*\[(.+?)\]\s*$", v or "")
    return (m.group(1), m.group(2)) if m else ((v or "").strip(), "")


def format_phone(v):
    digits = re.sub(r"\D", "", v or "")
    if len(digits) == 10:
        return f"+91 {digits[:5]} {digits[5:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    return v


def group_by_value(entries):
    """[{'value':v,'reported_on':d}, ...] -> OrderedDict{value: [dates...]},
    ordered by the most recent reported_on of each group, descending."""
    grouped = OrderedDict()
    for e in entries or []:
        v = (e.get("value") or "").strip()
        if not v:
            continue
        grouped.setdefault(v, []).append(parse_date(e.get("reported_on")))
    ordered = sorted(grouped.items(),
                      key=lambda kv: max([d for d in kv[1] if d] or [date.min]),
                      reverse=True)
    return OrderedDict(ordered)


# ---------------------------------------------------------------------------
# Data checks
# ---------------------------------------------------------------------------

def validate_data(data):
    """Sanity-check the JSON. Returns a list of (ok: bool, message) tuples."""
    checks = []

    def check(ok, msg):
        checks.append((ok, msg))

    required = ["inquiry_input_information", "crif_hm_scores",
                "personal_information_variations", "account_summary",
                "accounts", "group_details"]
    missing = [k for k in required if k not in data]
    check(not missing, "Required top-level keys present"
          if not missing else f"Missing top-level keys: {missing}")

    accounts = data.get("accounts", [])
    check(len(accounts) > 0, f"{len(accounts)} account(s) found")

    bad_idx = [a for a in accounts if a.get("index") is None]
    check(not bad_idx, "All accounts have an index"
          if not bad_idx else f"{len(bad_idx)} account(s) missing an index")

    no_type = [a for a in accounts if not a.get("account_type")]
    check(not no_type, "All accounts have an account_type"
          if not no_type else f"{len(no_type)} account(s) missing account_type")

    # Total row reconciliation
    summary = data.get("account_summary", {})
    total_row = next((r for r in summary.get("by_type", []) if r.get("type") == "Total"), None)
    if total_row:
        computed_bal = sum(parse_amount(a.get("current_balance")) for a in accounts if is_active(a))
        reported_bal = parse_amount(total_row.get("current_balance"))
        ok = abs(computed_bal - reported_bal) < 1
        check(ok, f"Total current balance reconciles (₹{reported_bal:,.0f})" if ok else
              f"Total current balance MISMATCH: summary says ₹{reported_bal:,.0f}, "
              f"sum of active accounts is ₹{computed_bal:,.0f}")

        computed_overdue = len([a for a in accounts if parse_amount(a.get("overdue_amount")) > 0])
        reported_overdue = total_row.get("overdue_accounts")
        ok = reported_overdue is None or computed_overdue == reported_overdue
        check(ok, f"Overdue account count reconciles ({reported_overdue})" if ok else
              f"Overdue account count MISMATCH: summary says {reported_overdue}, "
              f"found {computed_overdue} account(s) with overdue_amount > 0")
    else:
        check(False, "No 'Total' row in account_summary.by_type")

    # Date sanity
    bad_dates = [a.get("index") for a in accounts
                 if a.get("info_as_of") and not parse_date(a.get("info_as_of"))]
    check(not bad_dates, "All account info_as_of dates parse"
          if not bad_dates else f"Unparseable info_as_of dates on account(s): {bad_dates}")

    inq = data.get("group_details", {}).get("inquiries_last_24_months", [])
    bad_inq_dates = [i for i in inq if i.get("date_of_inquiry") and not parse_date(i.get("date_of_inquiry"))]
    check(not bad_inq_dates, f"All {len(inq)} enquiry dates parse"
          if not bad_inq_dates else f"{len(bad_inq_dates)} enquiry date(s) unparseable")

    # PAN consistency
    pv = data.get("personal_information_variations", {})
    pans = set()
    for v in pv.get("id_variations", []):
        root, typ = split_id_value(v.get("value"))
        if typ.upper() == "PAN" and root:
            pans.add(root.upper())
    if len(pans) > 1:
        check(False, f"Multiple distinct PAN numbers found in bureau history: {sorted(pans)} "
                     f"- flagged in the PAN modal")
    else:
        check(True, "PAN is consistent across bureau records" if pans else "No PAN found in id_variations")

    return checks


# ---------------------------------------------------------------------------
# HTML patching
# ---------------------------------------------------------------------------

def patch_bounded(html, start_anchor, end_anchor, pattern, new, label, required=True):
    """Like patch(), but the search is confined to the region between
    start_anchor and end_anchor (exclusive), so a pattern that stops matching
    on a re-run (e.g. because the target text already changed) can never
    "leak" into a similar-looking element further down the document."""
    s = html.find(start_anchor)
    if s < 0:
        if required:
            WARNINGS.append(f"{label}: start anchor not found -> {start_anchor!r}")
        return html
    e = html.find(end_anchor, s) if end_anchor else len(html)
    if e < 0:
        e = len(html)
    region = html[s:e]
    new_region = patch(region, [start_anchor], pattern, new, label, required=required)
    return html[:s] + new_region + html[e:]


def patch(html, anchors, pattern, new, label, required=True):
    """
    Find each anchor text in order (each search starts after the previous
    hit), then apply `pattern` at the first place it matches after the last
    anchor. `pattern` must have exactly 3 groups: (prefix)(old)(suffix); only
    group 2 is replaced with `new`. Group 1 and group 3 are left untouched
    (they only exist to anchor/scope the match).
    """
    pos = 0
    for a in anchors:
        i = html.find(a, pos)
        if i < 0:
            if required:
                WARNINGS.append(f"{label}: anchor not found -> {a!r}")
            return html
        pos = i
    m = re.compile(pattern, re.S).search(html, pos)
    if not m:
        if required:
            WARNINGS.append(f"{label}: target element not found after {anchors[-1]!r}")
        return html
    APPLIED.append((label, new.strip() if isinstance(new, str) else new))
    return html[:m.start(2)] + new + html[m.end(2):]


# ---------------------------------------------------------------------------
# Category table (Active Credit Facilities By Category)
# ---------------------------------------------------------------------------

def account_detail_line(a):
    """One drill-down row for an active account, in the category Details panel."""
    od = parse_amount(a.get("overdue_amount"))
    d = account_max_dpd(a)
    opened = fmt_d(a.get("disbursed_date"))
    sanction = parse_amount(a.get("disbursed_amount_high_credit"))
    emi = parse_amount(a.get("instalment_amount_freq"))
    grantor = grantor_label(a)
    secured_tag = ('<span class="text-[10px] font-bold text-indigo-600 bg-indigo-50 '
                   'px-1.5 py-0.5 rounded ml-1">SECURED</span>' if is_secured(a) else "")
    if od > 0:
        status = (f'<span class="text-red-600 font-bold">Overdue: {inr(od)} '
                  f'({d if d is not None else 0} DPD)</span>')
    elif is_written_off(a):
        status = '<span class="text-red-600 font-bold">Written-Off</span>'
    else:
        status = f'<span class="text-emerald-600 font-bold">Regular ({d or 0} DPD)</span>'
    return f'''
                  <div class="p-3 bg-white border border-slate-200 rounded-xl flex flex-wrap justify-between items-center gap-2">
                    <div>
                      <span class="font-bold text-slate-800">Account #{a.get("index")} • {esc((a.get("ownership") or "N/A").upper())}{secured_tag}</span>
                      <span class="text-slate-500 ml-2">Grantor: {esc(grantor)} | Opened: {esc(opened)} | Sanction: {inr(sanction) if sanction else "N/A"}</span>
                    </div>
                    <div class="flex items-center gap-4">
                      <span>EMI: <strong>{inr(emi) if emi else "N/A"}</strong></span>
                      <span>Balance: <strong>{inr(parse_amount(a.get("current_balance")))}</strong></span>
                      {status}
                    </div>
                  </div>'''


def closed_account_line(a):
    """One drill-down row for a closed/inactive account."""
    closed = fmt_d(a.get("closed_date"))
    sanction = parse_amount(a.get("disbursed_amount_high_credit"))
    remark = a.get("status_remark") or ("Written-Off" if is_written_off(a) else "Closed Regular")
    return f'''
                  <div class="p-3 bg-white border border-slate-200 rounded-xl flex flex-wrap justify-between items-center gap-2 text-xs">
                    <div>
                      <span class="font-bold text-slate-800">Account #{a.get("index")} • {esc((a.get("ownership") or "N/A").upper())}</span>
                      <span class="text-slate-500 ml-2">Grantor: {esc(grantor_label(a))} | Closed: {esc(closed)} | Sanction: {inr(sanction) if sanction else "N/A"}</span>
                    </div>
                    <span class="text-slate-600 font-semibold">{esc(remark)}</span>
                  </div>'''


def build_category_section(accounts):
    """Return (tbody_inner_html, totals dict)."""
    scope = [a for a in accounts if is_active(a)] if CATEGORY_TABLE_ACTIVE_ONLY else accounts

    order = []
    for a in accounts:                                  # order of first appearance
        t = a.get("account_type") or "UNKNOWN"
        if t not in order:
            order.append(t)

    cats = {}
    for t in order:
        in_scope = [a for a in scope if (a.get("account_type") or "UNKNOWN") == t]
        closed = [a for a in accounts if (a.get("account_type") or "UNKNOWN") == t and not is_active(a)]
        dpds = [d for d in (account_max_dpd(a) for a in in_scope) if d is not None]
        cats[t] = {
            "accounts": in_scope,
            "closed_accounts": closed,
            "balance": sum(parse_amount(a.get("current_balance")) for a in in_scope),
            "emi": sum(parse_amount(a.get("instalment_amount_freq")) for a in in_scope),
            "dpd": max(dpds) if dpds else None,
        }

    live = sorted([t for t in order if cats[t]["accounts"]],
                  key=lambda t: -cats[t]["balance"])
    dead = [t for t in order if not cats[t]["accounts"]]

    def dpd_badge(d):
        if d is None:
            return '<span class="px-2 py-0.5 text-xs font-bold bg-slate-100 text-slate-500 rounded-md">N/A</span>'
        col = "emerald" if d == 0 else ("amber" if d <= 90 else "red")
        return (f'<span class="px-2 py-0.5 text-xs font-bold bg-{col}-100 text-{col}-700 '
                f'rounded-md">DPD {d}</span>')

    rows = []
    for i, t in enumerate(live):
        c, col = cats[t], PALETTE[i % len(PALETTE)]
        did = "cat" + re.sub(r"\W+", "", nice_type(t)) + "Detail"
        if c["emi"] > 0:
            emi_td = f'<td class="py-3.5 px-4 text-right font-mono text-slate-700 font-medium">{inr(c["emi"])}</td>'
        elif "CARD" in t.upper():
            emi_td = '<td class="py-3.5 px-4 text-right font-mono text-slate-400">Revolving</td>'
        else:
            emi_td = ('<td class="py-3.5 px-4 text-right font-mono text-slate-400">₹0 '
                      '<span class="text-[10px] text-slate-400">(N/A)</span></td>')

        rows.append(f'''
            <!-- Category: {esc(nice_type(t))} -->
            <tr class="hover:bg-slate-50/80 transition-colors">
              <td class="py-3.5 px-4 font-semibold text-slate-900 flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-full bg-{col}-500"></span>
                {esc(nice_type(t))}
              </td>
              <td class="py-3.5 px-4 text-center font-bold text-slate-800">
                <span class="inline-block px-2.5 py-0.5 bg-{col}-50 text-{col}-700 rounded-full text-xs font-bold">{len(c["accounts"])}</span>
              </td>
              {emi_td}
              <td class="py-3.5 px-4 text-right font-mono font-bold text-slate-900">{inr(c["balance"])}</td>
              <td class="py-3.5 px-4 text-center">
                {dpd_badge(c["dpd"])}
              </td>
              <td class="py-3.5 px-4 text-center">
                <button onclick="toggleCategoryDetail('{did}')" class="text-xs text-sky-600 hover:text-sky-800 font-medium hover:underline inline-flex items-center gap-1">
                  Details <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                </button>
              </td>
            </tr>''')

        items = "".join(account_detail_line(a) for a in c["accounts"])
        if c["closed_accounts"]:
            items += (f'\n                  <div class="text-[11px] font-semibold text-slate-400 '
                      f'uppercase tracking-wide pt-1">Closed / Prior ({len(c["closed_accounts"])})</div>'
                      + "".join(closed_account_line(a) for a in c["closed_accounts"]))
        rows.append(f'''
            <tr id="{did}" class="hidden bg-slate-50/50">
              <td colspan="6" class="p-4">
                <div class="space-y-2 text-xs">{items}
                </div>
              </td>
            </tr>''')

    for t in dead:                                        # categories with no active account
        n_closed = len(cats[t]["closed_accounts"])
        note = f'{n_closed} Closed Prior' if n_closed else "No Active Account"
        did = "cat" + re.sub(r"\W+", "", nice_type(t)) + "Detail"
        action_cell = (f'''<td class="py-3 px-4 text-center">
                <button onclick="toggleCategoryDetail('{did}')" class="text-xs text-sky-600 hover:text-sky-800 font-medium hover:underline">
                  Details
                </button>
              </td>''' if n_closed else
                       '<td class="py-3 px-4 text-center text-xs text-slate-400">—</td>')
        rows.append(f'''
            <tr class="opacity-60 bg-slate-50/30">
              <td class="py-3 px-4 font-medium text-slate-500 flex items-center gap-2">
                <span class="w-2 h-2 rounded-full bg-slate-300"></span>
                {esc(nice_type(t))}
              </td>
              <td class="py-3 px-4 text-center text-slate-400 font-mono">0</td>
              <td class="py-3 px-4 text-right text-slate-400 font-mono">—</td>
              <td class="py-3 px-4 text-right text-slate-400 font-mono">₹0</td>
              <td class="py-3 px-4 text-center"><span class="text-xs text-slate-400">{note}</span></td>
              {action_cell}
            </tr>''')
        if n_closed:
            rows.append(f'''
            <tr id="{did}" class="hidden bg-slate-50/50">
              <td colspan="6" class="p-4">
                <div class="space-y-2 text-xs">{"".join(closed_account_line(a) for a in cats[t]["closed_accounts"])}
                </div>
              </td>
            </tr>''')

    totals = {"emi": sum(c["emi"] for c in cats.values()), "categories": len(order)}
    return "\n" + "\n".join(rows) + "\n          ", totals


def best_loan(accounts, want_secured):
    best = None
    for a in accounts:
        if (a.get("account_type") or "").upper() in NON_LOAN_TYPES:
            continue
        if is_secured(a) != want_secured:
            continue
        amt = parse_amount(a.get("disbursed_amount_high_credit"))
        if best is None or amt > best[0]:
            best = (amt, a)
    return best


# ---------------------------------------------------------------------------
# KYC modals: Addresses / Phones / PAN
# ---------------------------------------------------------------------------

def build_address_modal(pv):
    groups = group_by_value(pv.get("address_variations", []))
    if not groups:
        return '\n        <p class="text-xs text-slate-500">No addresses reported.</p>\n      ', 0
    cards = []
    for i, (addr, dates) in enumerate(groups.items()):
        valid = sorted([d for d in dates if d], reverse=True)
        latest = valid[0].strftime("%d %b %Y") if valid else "N/A"
        if i == 0:
            tag = "Primary / Most Recently Reported"
            tag_cls = "text-xs font-bold text-sky-700 bg-sky-100 px-2 py-0.5 rounded"
        else:
            tag = f"Historical Address ({len(dates)} report{'s' if len(dates) != 1 else ''})"
            tag_cls = "text-xs font-semibold text-slate-600 bg-slate-200 px-2 py-0.5 rounded"
        cards.append(f'''
        <div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200">
          <div class="flex items-center justify-between mb-1.5">
            <span class="{tag_cls}">{esc(tag)}</span>
            <span class="text-[11px] text-slate-400">Latest Reported: {esc(latest)}</span>
          </div>
          <p class="text-xs text-slate-700 leading-relaxed{" font-medium" if i == 0 else ""}">
            {esc(addr)}
          </p>
        </div>''')
    return "\n" + "\n".join(cards) + "\n      ", len(groups)


def build_phone_modal(pv):
    groups = group_by_value(pv.get("phone_variations", []))
    if not groups:
        return '\n        <p class="text-xs text-slate-500">No phone numbers reported.</p>\n      ', 0
    cards = []
    for i, (num, dates) in enumerate(groups.items()):
        valid = sorted([d for d in dates if d], reverse=True)
        latest = valid[0].strftime("%d %b %Y") if valid else "N/A"
        if i == 0:
            tag, tag_cls = "Primary Mobile", 'text-xs font-bold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded'
        else:
            tag = f"Reported {len(dates)}×"
            tag_cls = 'text-xs font-semibold text-slate-600 bg-slate-200 px-2 py-0.5 rounded'
        cards.append(f'''
        <div class="p-3.5 bg-slate-50 rounded-xl border border-slate-200">
          <div class="flex items-center justify-between">
            <span class="{tag_cls}">{esc(tag)}</span>
            <span class="text-[11px] text-slate-400 font-mono">Latest: {esc(latest)}</span>
          </div>
          <div class="mt-2 text-base font-bold font-mono text-slate-900">
            {esc(format_phone(num))}
          </div>
        </div>''')
    return "\n" + "\n".join(cards) + "\n      ", len(groups)


def build_pan_identity_block(inp, pv):
    """Returns (identity_html, footer_text, n_distinct_pans)."""
    pan_groups = OrderedDict()
    for v in pv.get("id_variations", []):
        root, typ = split_id_value(v.get("value"))
        if typ.upper() == "PAN" and root:
            pan_groups.setdefault(root.upper(), []).append(parse_date(v.get("reported_on")))
    ordered = sorted(pan_groups.items(),
                      key=lambda kv: max([d for d in kv[1] if d] or [date.min]), reverse=True)

    if not ordered:
        return ('\n      <p class="text-xs text-slate-500">No PAN reported in bureau records.</p>\n      ',
                "No PAN found in bureau records for this consumer.", 0)

    name = inp.get("name") or "N/A"
    primary, primary_dates = ordered[0]
    entity_code = primary[3:4].upper() if len(primary) > 3 else "?"
    entity = PAN_ENTITY.get(entity_code, "Unknown")
    mismatch = len(ordered) > 1

    banner = ""
    if mismatch:
        others = ", ".join(p for p, _ in ordered[1:])
        banner = f'''
      <div class="p-3 bg-red-50 border border-red-200 rounded-xl flex items-start gap-2">
        <span class="text-red-600 font-bold text-xs whitespace-nowrap">⚠ PAN Mismatch</span>
        <span class="text-xs text-red-700">{len(ordered)} distinct PAN numbers found in bureau history
          ({esc(primary)}, {esc(others)}) - verify identity with the consumer.</span>
      </div>'''

    valid = sorted([d for d in primary_dates if d], reverse=True)
    latest = valid[0].strftime("%d %b %Y") if valid else "N/A"

    block = banner + f'''
      <div class="p-4 bg-amber-50/50 rounded-xl border border-amber-200">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-amber-800 uppercase tracking-wide">Bureau Primary Identity</span>
          <span class="text-xs font-bold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full flex items-center gap-1">
            <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/></svg>
            Reported: {esc(latest)}
          </span>
        </div>
        <div class="mt-3 text-2xl font-black font-mono text-slate-900 tracking-widest">
          {esc(primary)}
        </div>
        <div class="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-600 border-t border-amber-200/60 pt-3">
          <div>
            <span class="text-slate-400 text-[11px] block">Holder Name</span>
            <span class="font-bold text-slate-800">{esc(name)}</span>
          </div>
          <div>
            <span class="text-slate-400 text-[11px] block">Entity Type</span>
            <span class="font-bold text-slate-800">{esc(entity)} ({esc(entity_code)})</span>
          </div>
        </div>
      </div>
      '''

    n_accounts_note = "{n_accounts}"  # filled by caller
    if mismatch:
        footer = (f"{len(ordered)} distinct PAN values found across {{n_accounts}} accounts and "
                  f"{{n_enquiries}} enquiry record(s) in this bureau file. Review the flagged mismatch above.")
    else:
        footer = (f"Consistent single PAN verified across all {{n_accounts}} accounts and "
                  f"{{n_enquiries}} enquiry record(s). No duplicate or conflicting PAN identified.")
    return block, footer, len(ordered)


# ---------------------------------------------------------------------------
# Enquiries modal (ALL enquiries, never truncated)
# ---------------------------------------------------------------------------

def build_enquiries_table(inq_all):
    with_date, without_date = [], []
    for i in inq_all:
        d = parse_date(i.get("date_of_inquiry"))
        (with_date if d else without_date).append((d, i))
    with_date.sort(key=lambda x: x[0], reverse=True)
    ordered = with_date + [(None, i) for _, i in without_date]

    if not ordered:
        return ('\n            <tr><td colspan="4" class="py-4 px-3 text-center text-slate-400">'
                'No enquiries reported.</td></tr>\n          ')

    trs = []
    for d, i in ordered:
        date_txt = d.strftime("%d/%m/%Y") if d else esc(i.get("date_of_inquiry") or "N/A")
        purpose = esc((i.get("purpose") or "N/A").upper())
        amount = parse_amount(i.get("amount"))
        amount_txt = inr(amount) if amount else "₹0"
        member = "NOT DISCLOSED" if masked(i.get("credit_grantor")) else esc(i.get("credit_grantor") or "NOT DISCLOSED")
        trs.append(f'''
            <tr class="hover:bg-slate-50">
              <td class="py-2 px-3 font-mono font-medium">{date_txt}</td>
              <td class="py-2 px-3 font-semibold text-slate-800">{purpose}</td>
              <td class="py-2 px-3 text-right font-mono font-bold">{amount_txt}</td>
              <td class="py-2 px-3 text-center text-slate-400">{member}</td>
            </tr>''')
    return "\n" + "\n".join(trs) + "\n          "


# ---------------------------------------------------------------------------
# Main population logic
# ---------------------------------------------------------------------------

def populate(html, data, as_of):
    accounts = data.get("accounts", [])
    summary = data.get("account_summary", {})
    total_row = next((r for r in summary.get("by_type", []) if r.get("type") == "Total"), {})
    pv = data.get("personal_information_variations", {})
    inq_all = data.get("group_details", {}).get("inquiries_last_24_months", [])
    inp = data.get("inquiry_input_information", {})

    active_accounts = [a for a in accounts if is_active(a)]
    inactive_accounts = [a for a in accounts if not is_active(a)]

    # === HEADER =================================================================================
    scores = data.get("crif_hm_scores", {}).get("scores", [])
    if scores and scores[0].get("score_value") is not None:
        html = patch(html, ["Bureau Score"],
                     r'(<span class="text-base font-bold text-amber-400 leading-none">)([^<]*)(<span)',
                     f'{scores[0]["score_value"]} ', "Bureau score")

    if inp.get("name"):
        html = patch(html, ["Consumer Profile"],
                     r'(<h2 class="text-xl font-bold text-slate-900 leading-snug">)(.*?)(</h2>)',
                     esc(inp["name"]), "Name")

    info_dates = [d for d in (parse_date(a.get("info_as_of")) for a in accounts) if d]
    if info_dates:
        html = patch(html, ["Bureau Report Date"],
                     r'(<h2 class="text-xl font-bold text-slate-900 leading-snug">)(.*?)(</h2>)',
                     max(info_dates).strftime("%d %b %Y"), "Latest info_as_of")

    all_dpd = [d for d in (account_max_dpd(a) for a in accounts) if d is not None]
    if all_dpd:
        html = patch(html, ["Max DPD Across Accounts"],
                     r'(<h2 class="text-2xl font-black text-red-600 tracking-tight">)([^<]*)(<span)',
                     f"{max(all_dpd)} ", "Max DPD")
        severe = len([a for a in active_accounts if (account_max_dpd(a) or 0) >= 180])
        html = patch(html, ["Max DPD Across Accounts"],
                     r'(<p class="mt-2 text-xs text-red-700/80">)(.*?)(</p>)',
                     f"{severe} Active Account{'s' if severe != 1 else ''} in severe default (&gt;180+ DPD)",
                     "Max DPD - severe count")

    # === KYC CARDS (counts + teaser lines) ======================================================
    num_span = r'(<span class="text-2xl font-bold text-slate-900">)(.*?)(</span>)'
    html = patch(html, ["Addresses Across Accounts"], num_span,
                 str(len(pv.get("address_variations", []))), "Address count")
    html = patch(html, ["Phone Numbers Across Accounts"], num_span,
                 str(len(pv.get("phone_variations", []))), "Phone count")
    pan_var = [v for v in pv.get("id_variations", []) if str(v.get("value", "")).strip().endswith("[PAN]")]
    html = patch(html, ["PAN Numbers Across Accounts"], num_span, str(len(pan_var)), "PAN count")

    pans_hdr = [i["value"] for i in inp.get("ids", []) if str(i.get("type", "")).upper() == "PAN"]
    if pans_hdr:
        label = ", ".join(f"{p} ({PAN_ENTITY.get(p[3:4].upper(), 'PAN')})" if len(p) > 3 else p for p in pans_hdr)
        html = patch(html, ["PAN Numbers Across Accounts"],
                     r'(<p class="mt-2 text-xs font-mono font-bold text-slate-800 tracking-wide[a-z]*">)(.*?)(</p>)',
                     f"\n            {esc(label)}\n          ", "Primary PAN (KYC card)")

    addr_groups = group_by_value(pv.get("address_variations", []))
    if addr_groups:
        top_addr = next(iter(addr_groups))
        short = (top_addr[:47] + "...") if len(top_addr) > 50 else top_addr
        html = patch(html, ["Addresses Across Accounts"],
                     r'(<p class="mt-2 text-xs text-slate-500 truncate" title=")(.*?)(")',
                     esc(top_addr), "Address teaser (title)")
        html = patch(html, ["Addresses Across Accounts"],
                     r'(truncate" title="[^"]*">\s*Primary: )(.*?)(\s*</p>)',
                     esc(short), "Address teaser (text)")
    phone_groups = group_by_value(pv.get("phone_variations", []))
    if phone_groups:
        top_phone = next(iter(phone_groups))
        html = patch(html, ["Phone Numbers Across Accounts"],
                     r'(<p class="mt-2 text-xs text-slate-500 font-mono">\s*Primary: )(.*?)(\s*</p>)',
                     esc(format_phone(top_phone)), "Phone teaser")

    # === KYC MODALS ==============================================================================
    ADDR_H4 = '<h4 class="text-base font-bold text-slate-900">Addresses Across All Accounts</h4>'
    addr_block, n_addr = build_address_modal(pv)
    html = patch(html, [ADDR_H4], r'(<p class="text-xs text-slate-500">)(.*?)(</p>)',
                 f"{n_addr} Verified Bureau Address{'es' if n_addr != 1 else ''}",
                 "Address modal - subtitle")
    html = patch(html, [ADDR_H4],
                 r'(<div class="space-y-3">)(.*?)(\n      </div>\s*<div class="pt-2 text-right">)',
                 addr_block, "Address modal - list")

    PHONE_H4 = '<h4 class="text-base font-bold text-slate-900">Phone Numbers Across Accounts</h4>'
    phone_block, n_phone = build_phone_modal(pv)
    html = patch(html, [PHONE_H4],
                 r'(<div class="space-y-3">)(.*?)(\n      </div>\s*<div class="pt-2 text-right">)',
                 phone_block, "Phone modal - list")

    PAN_H4 = 'PAN Cards Across Accounts'
    pan_block, pan_footer_tmpl, n_pan = build_pan_identity_block(inp, pv)
    # The optional leading group absorbs a PAN-mismatch banner left by a *previous* run of this
    # script, so re-running replaces the whole (banner + identity) block instead of stacking a
    # second banner in front of it.
    html = patch(html, [PAN_H4],
                 r'()((?:<div class="p-3 bg-red-50 border border-red-200 rounded-xl flex items-start gap-2">.*?</div>\s*)?'
                 r'<div class="p-4 bg-amber-50/50 rounded-xl border border-amber-200">.*?</div>\s*)'
                 r'(<p class="text-xs text-slate-500">)',
                 pan_block, "PAN modal - identity block")
    pan_footer = pan_footer_tmpl.format(n_accounts=len(accounts), n_enquiries=len(inq_all))
    html = patch(html, [PAN_H4], r'(<p class="text-xs text-slate-500">\s*)(.*?)(\s*</p>)',
                 pan_footer, "PAN modal - footer note")

    # === ACTIVE CREDIT FACILITIES BY CATEGORY ===================================================
    tot_bal = total_row.get("current_balance")
    if tot_bal is None:
        tot_bal = sum(parse_amount(a.get("current_balance")) for a in active_accounts)

    html = patch(html, ["Total Active Outstanding:"],
                 r'(<span class="text-base font-black text-slate-900 font-mono">)(.*?)(</span>)',
                 inr(tot_bal), "Total active outstanding (header)")

    html = patch(html, ["Active Credit Facilities By Category"],
                 r'(<span class="px-2 py-0.5 text-xs font-bold bg-sky-100 text-sky-800 rounded-full">)(.*?)(</span>)',
                 f"{total_row.get('active_accounts', len(active_accounts))} Active Accounts",
                 "Active accounts pill")

    tbody, tot = build_category_section(accounts)
    html = patch(html, ["Active Credit Facilities By Category"],
                 r'(<tbody[^>]*>)(.*?)(</tbody>)', tbody, f"Category rows ({tot['categories']} categories)")

    tf_start = html.find("Active Credit Facilities By Category")
    tf = re.search(r"<tfoot.*?</tfoot>", html[tf_start:], re.S)
    if tf:
        s, e = tf_start + tf.start(), tf_start + tf.end()
        foot = html[s:e]
        od_total = sum(parse_amount(a.get("overdue_amount")) for a in accounts)
        cells = [
            (r'(<td class="py-3.5 px-4 text-center text-slate-900">)(.*?)(</td>)',
             f'{total_row.get("overdue_accounts", 0)} Accounts', "Total: overdue accounts"),
            (r'(<td class="py-3.5 px-4 text-right text-slate-900 font-mono">)(.*?)(</td>)',
             f'{inr(tot["emi"])} / mo', "Total: monthly EMI"),
            (r'(<td class="py-3.5 px-4 text-right text-slate-900 font-mono text-base">)(.*?)(</td>)',
             inr(tot_bal), "Total: outstanding"),
            (r'(<td colspan="2" class="py-3.5 px-4 text-center text-xs text-red-600 font-bold">)(.*?)(</td>)',
             f"Total Overdue: {inr(od_total)}", "Total: overdue amount"),
        ]
        for pat, new, label in cells:
            foot = patch(foot, ["<tr"], pat, new, label)
        html = html[:s] + foot + html[e:]
    else:
        WARNINGS.append("Category table footer (tfoot) not found")

    # === CREDIT CARD LIMIT / USAGE ===============================================================
    # Headline = highest credit_limit ever reported on ANY credit card account (active or closed),
    # per spec. Usage/utilization, however, only makes sense against a limit that is *currently*
    # disclosed on an *active* card - some bureau files (like this one) only carry a credit_limit
    # figure on closed cards, in which case a computed % against that stale limit would be
    # misleading, so it falls back to "Limit N/A" rather than fabricating a number.
    cc_accounts = [a for a in accounts if (a.get("account_type") or "").upper() == "CREDIT CARD"]
    max_limit = max((parse_amount(a.get("credit_limit")) for a in cc_accounts), default=0)
    active_cc = [a for a in cc_accounts if is_active(a)]
    active_limit_sum = sum(parse_amount(a.get("credit_limit")) for a in active_cc
                           if parse_amount(a.get("credit_limit")) > 0)
    cc_balance = sum(parse_amount(a.get("current_balance")) for a in active_cc)
    util_txt = f"{cc_balance / active_limit_sum * 100:.1f}% Utilized" if active_limit_sum else "Limit N/A"

    html = patch(html, ["Credit Card Limit & Usage"],
                 r'(<span class="text-2xl font-black text-slate-900 font-mono">)(.*?)(</span>)',
                 inr(max_limit), "Credit card limit (max)")
    html = patch(html, ["Credit Card Limit & Usage"],
                 r'(<span class="text-xs font-semibold text-\w+-600 bg-\w+-50 px-2 py-0\.5 rounded">)(.*?)(</span>)',
                 util_txt, "Credit card utilization")
    html = patch(html, ["Credit Card Limit & Usage"],
                 r'(<p class="text-xs text-slate-600 mt-1 font-medium">Current Balance: )(.*?)(</p>)',
                 inr(cc_balance), "Credit card current balance")

    # === MAX SECURED / UNSECURED LOAN ============================================================
    amt_pat = r'(<span class="text-2xl font-black text-slate-900 font-mono">)(.*?)(</span>)'
    sub_pat = r'(<p class="text-xs text-slate-500 mt-1 font-medium">)(.*?)(</p>)'
    for anchor, secured, label in (("Max Unsecured Loan Taken", False, "Max unsecured loan"),
                                   ("Max Secured Loan Taken", True, "Max secured loan")):
        best = best_loan(accounts, secured)
        amount = best[0] if best else 0
        html = patch(html, [anchor], amt_pat, inr(amount), label)
        html = patch(html, [anchor], sub_pat,
                     f'{esc(nice_type(best[1].get("account_type")))} (Account #{best[1].get("index")})'
                     if best else "None reported", label + " - detail")
        if secured:
            closed = [a for a in accounts if not is_active(a) and is_secured(a)
                      and (a.get("account_type") or "").upper() not in NON_LOAN_TYPES]
            peak = max(closed, key=lambda a: parse_amount(a.get("disbursed_amount_high_credit")), default=None)
            txt = (f'Inactive secured peak: {inr(parse_amount(peak.get("disbursed_amount_high_credit")))} '
                   f'({esc(nice_type(peak.get("account_type")))} #{peak.get("index")})'
                   if peak else "No inactive secured loans")
            html = patch(html, [anchor], r'(<p class="text-\[11px\] text-slate-400 mt-0\.5">)(.*?)(</p>)',
                         txt, label + " - inactive peak")

    # === WRITTEN-OFF / SETTLED ACCOUNT CARDS =====================================================
    # Both cards share identical markup/classes, so every patch here is bounded to the region
    # between this card's own heading and the next one - otherwise a pattern that stops matching
    # its own (already-updated) card on a re-run could "leak" forward and corrupt the next card.
    written_off = [a for a in accounts if is_written_off(a)]
    settled = [a for a in accounts if is_settled(a)]
    card_bounds = [
        ("Details of Written-Off Accounts", written_off, "Written-off", "total_writeoff_amount",
         "Details of Settled Accounts"),
        ("Details of Settled Accounts", settled, "Settled", "settlement_amount",
         "Bureau Enquiry Velocity"),
    ]
    for anchor, items, prefix, amt_key, end_anchor in card_bounds:
        n = len(items)
        html = patch_bounded(html, anchor, end_anchor,
                              r'(<span class="text-xs font-semibold px-2 py-0\.5 )(bg-emerald-50 text-emerald-700 border border-emerald-200|bg-red-50 text-red-700 border border-red-200)(\s*rounded-md">\s*)',
                              "bg-red-50 text-red-700 border border-red-200" if n else
                              "bg-emerald-50 text-emerald-700 border border-emerald-200",
                              f"{prefix} - badge colour")
        html = patch_bounded(html, anchor, end_anchor,
                              r'(rounded-md">\s*)(\d+ Accounts? Flagged)(\s*</span>)',
                              f"{n} Account{'s' if n != 1 else ''} Flagged", f"{prefix} - count")
        if n:
            first = items[0]
            typ = nice_type(first.get("account_type")) + (f" (+{n - 1} more)" if n > 1 else "")
            amt = sum(parse_amount(a.get(amt_key)) for a in items)          # aggregate, not just item 1
            dts = [parse_date(a.get("closed_date") or a.get("last_payment_date")) for a in items]
            dts = [d for d in dts if d]
            when = max(dts).strftime("%d/%m/%Y") if dts else "N/A"          # most recent event
            html = patch_bounded(html, anchor, end_anchor,
                                  r'(<span class="text-slate-400 block text-\[11px\]">Type</span>\s*<span class="font-medium">)(.*?)(</span>)',
                                  esc(typ), f"{prefix} - type")
            html = patch_bounded(html, anchor, end_anchor,
                                  r'(<span class="font-medium font-mono">)(.*?)(</span>)',
                                  inr(amt), f"{prefix} - amount")
            html = patch_bounded(html, anchor, end_anchor,
                                  r'(<span class="text-slate-400 block text-\[11px\]">(?:Date|Settlement Date)</span>\s*<span class="font-medium">)(.*?)(</span>)',
                                  esc(when), f"{prefix} - date")

    # === INACTIVE ACCOUNTS PILL ==================================================================
    html = patch(html, ["Inactive Accounts & Risk Exposures"],
                 r'(<span class="text-xs font-semibold text-slate-500 bg-slate-100 px-2\.5 py-1 rounded-full border border-slate-200">\s*)(\d+ Total Inactive / Settled Accounts)(\s*</span>)',
                 f"{len(inactive_accounts)} Total Inactive / Settled Accounts", "Inactive accounts pill")

    # === ENQUIRIES ===============================================================================
    inq_dates = [d for d in (parse_date(i.get("date_of_inquiry")) for i in inq_all) if d]
    windows = [("Past 30 Days", as_of - timedelta(days=30)),
               ("Last 3 Months", months_ago(as_of, 3)),
               ("Last 6 Months", months_ago(as_of, 6)),
               ("Last 12 Months", months_ago(as_of, 12))]
    for label, cutoff in windows:
        n = sum(1 for d in inq_dates if cutoff <= d <= as_of)
        html = patch(html, ["Bureau Enquiry Velocity", label],
                     r'(<span class="text-2xl font-black text-slate-900">)(.*?)(</span>)',
                     str(n), f"Enquiries - {label}")

    n_total_inq = len(inq_all)
    html = patch(html, ["Bureau Enquiry Velocity"],
                 r'(<span class="text-xs bg-slate-100 text-slate-700 font-semibold px-2 py-0\.5 rounded-full">)(\d+ Total Bureau Enquiries)(</span>)',
                 f"{n_total_inq} Total Bureau Enquiries", "Total enquiries pill")
    html = patch(html, ["openModal('enquiriesModal')"],
                 r'(Inspect All )(\d+ Enquir(?:y|ies))',
                 f"{n_total_inq} Enquir{'y' if n_total_inq == 1 else 'ies'}", "Enquiry button count")

    # Enquiries modal: header count, ALL rows (never truncated), footer note.
    html = patch(html, ['id="enquiriesModal"'],
                 r'(<h4 class="text-base font-bold text-slate-900">Enquiry History Log \()(\d+ Enquiries)(\))',
                 f"{n_total_inq} Enquir{'y' if n_total_inq == 1 else 'ies'}", "Enquiries modal - header count")
    html = patch(html, ['id="enquiriesModal"'],
                 r'(<tbody class="divide-y divide-slate-100">)(.*?)(</tbody>)',
                 build_enquiries_table(inq_all), f"Enquiries modal - rows ({n_total_inq} shown)")
    html = patch(html, ['id="enquiriesModal"'],
                 r'(<span class="text-xs text-slate-400">)(.*?)(</span>)',
                 f"Showing all {n_total_inq} enquir{'y' if n_total_inq == 1 else 'ies'}",
                 "Enquiries modal - footer note")

    return html


def main():
    ap = argparse.ArgumentParser(description="Populate the CIBIL dashboard HTML in place from the CRIF JSON.")
    ap.add_argument("json_path", help="JSON produced by crif_extractor.py")
    ap.add_argument("html_path", help="Dashboard HTML to update in place")
    ap.add_argument("--as-of", help="Reference date YYYY-MM-DD for enquiry windows (default: today)")
    ap.add_argument("--backup", action="store_true", help="Write <html>.bak before overwriting")
    args = ap.parse_args()

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()

    with open(args.json_path, encoding="utf-8") as f:
        data = json.load(f)
    with open(args.html_path, encoding="utf-8", newline="") as f:
        original = f.read()

    print("Running data checks...")
    checks = validate_data(data)
    for ok, msg in checks:
        print(f"  {'✔' if ok else '⚠'} {msg}")
    n_fail = sum(1 for ok, _ in checks if not ok)
    print(f"{len(checks) - n_fail}/{len(checks)} checks passed.\n")

    updated = populate(original, data, as_of)

    if args.backup:
        with open(args.html_path + ".bak", "w", encoding="utf-8", newline="") as f:
            f.write(original)
    with open(args.html_path, "w", encoding="utf-8", newline="") as f:
        f.write(updated)

    print(f"Dashboard updated in place: {args.html_path}  (enquiry windows as of {as_of})")
    for label, val in APPLIED:
        shown = " ".join(str(val).split())
        print(f"  ✔ {label}: {shown[:70]}{'…' if len(shown) > 70 else ''}")
    if WARNINGS:
        print(f"\n{len(WARNINGS)} warning(s):", file=sys.stderr)
        for w in WARNINGS:
            print(f"  ⚠ {w}", file=sys.stderr)


if __name__ == "__main__":
    main()