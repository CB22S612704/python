"""
Cigna HCP automation - fresh build

PATIENT SEARCH (per Input row, always restarts at Method 1)
    1. Cigna ID + DOB      2. First/Last name + DOB      3. First/Last name + Cigna ID

CLAIM SELECTION
    * Nothing in the search list ................. Status "Patient Not Found"
    * CPT and/or Billed Amount in the Input row .. open the most recent claims
      (latest DOS, then highest claim number, up to --max-claims) and take the
      first claim where the CPT is present AND billed amount == "Claim amount due"
          - CPT in no checked claim .............. "CPT Not Matched"
          - CPT found, billed amount differs ..... "Billed Amount Not Matched"
    * No CPT and no Billed Amount ................ claims whose DOS matches, highest
      claim number wins (no DOS match -> highest claim number, "DOS Mismatch")

OUTPUT
    Status ............ one line per CPT (repeated CPTs as LINE n):
                            99212 - PAID / DENIED / PARTIALLY PAID
                            or, when the plan paid $0 but the patient owes:
                            COPAY / DEDUCTIBLE / COINSURANCE / PATIENT RESPONSIBILITY
    Notes ............. total paid = "Claim amount paid"; Patient Coinsurance is
                        written as COINSURANCE
    Remark Codes ...... denied CPT lines only (Master sheet gets the same)

START CHROME FIRST (once, then log in to Cigna manually):
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
        --remote-debugging-port=9222 --user-data-dir="C:\\ChromeDebug"

RUN:
    python Cigna_Automation_Fresh.py --excel "C:\\...\\Cigna_Updated.xlsx"
    python Cigna_Automation_Fresh.py --excel Cigna_Updated.xlsx --rows 3 --dry-run

Optional Input column "Billed Amount" (or Billed Amt / Billed / Charge Amount).
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from openpyxl import load_workbook

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
DEFAULT_EXCEL = APP_DIR / "Cigna_Updated.xlsx"

DEBUGGER_HOST = "127.0.0.1"
DEBUGGER_PORT = 9222

SHEET_INPUT = "Input"
SHEET_MASTER = "Master"

WAIT = 20            # normal explicit wait (seconds)
SHORT_WAIT = 4
MAX_CLAIMS = 3       # DOS matches first, then this many recent claims

STATUS_NOT_FOUND = "Patient Not Found"          # nothing in the search list
STATUS_CPT_NOT_MATCHED = "CPT Not Matched"      # CPT not in any checked claim
STATUS_BILLED_NOT_MATCHED = "Billed Amount Not Matched"
STATUS_FAILED = "Failed"

# Optional Input column holding the billed amount (compared with the claim's
# "Claim amount due"). If none of these headers exist the check is skipped.
BILLED_HEADERS = ("Billed Amount", "Billed Amt", "Billed", "Charge Amount")

log = logging.getLogger("cigna")      # detailed log -> file only
OK, BAD = "\u2714", "\u2718"           # tick / cross shown in the cmd window
MISMATCH_WAIT = 3                      # seconds before moving to the next claim


def say(message: str = "") -> None:
    """Neat console line (also kept in the log file)."""
    print(message, flush=True)
    log.info(message)

# --------------------------------------------------------------------------
# XPATHS  (exactly as supplied in Cigna_Xpaths.xlsx / the old script)
# --------------------------------------------------------------------------
CLAIM_BASE = (
    "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/"
    "div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/"
    "cigna-claims-details-medical"
)
INFO = (CLAIM_BASE + "/cigna-medical-claims-information/div[2]/"
        "cigna-tab-group/div/div/div")
PROC = CLAIM_BASE + "/div[4]/cigna-medical-procedure-details/div/table"
PROC_TABLES = "//cigna-medical-procedure-details//table"
PROC_ROWS = "//cigna-medical-procedure-details//table//tbody/tr"
EXPL = CLAIM_BASE + "/div[5]/cigna-medical-explanation-details/div[1]/div"
EXPL_BLOCKS = "//cigna-medical-explanation-details//*[self::div or self::li][.//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'remark') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'reason')]]"

NAV = {
    "claims_menu": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/"
        "header/cigna-primary-nav/nav/div[3]/div/cigna-nav-item/a"),
    "search_claim_menu": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/"
        "header/cigna-primary-nav/nav/div[3]/cigna-nav-links/div[1]/"
        "cigna-nav-item/a"),
    "return_claims": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/"
        "main/div/cigna-page/cigna-claim-details-screen/cigna-breadcrumbs/"
        "div/div[1]/a"),
}

_FORM = ('//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
         "div/cigna-search-type-patient/form")
SEARCH = {
    "form": "//cigna-search-form",
    "type1": _FORM + "/div[1]/fieldset/div[1]/div[1]/label/i",   # ID + DOB
    "type2": _FORM + "/div[1]/fieldset/div[1]/div[2]/label/i",   # Name + DOB
    "type3": _FORM + "/div[1]/fieldset/div[1]/div[3]/label/i",   # Name + ID
    "dob": '//*[@id="dob"]',
    "member_id": '//*[@id="memberId"]',
    "first_name": '//*[@id="firstName"]',
    "last_name": '//*[@id="lastName"]',
    "button": _FORM + "/div[2]/fieldset/div/div[6]/button[1]",
    "rows": "//cigna-results-table//table/tbody/tr",
}
NO_RESULT_MARKERS = ("no results", "no records", "no claims found",
                     "no patients found", "no matching", "no data",
                     "0 results", "there are no")

# Claim-page fields: Input column header -> list of XPaths (first hit wins)
FIELD_XPATHS: dict[str, list[str]] = {
    "Claim/Reference Number": [INFO + "/div[1]/table/tbody/tr[1]/td"],
    "Remittance Tracking Number": ['//*[@id="remittance-section"]/table/tbody/tr/td[1]'],
    "Deposit Amount": ['//*[@id="remittance-section"]/table/tbody/tr/td[4]'],
    "Claim Status": [
        INFO + "/div[1]/table/tbody/tr[7]/td/cigna-claim-status/cigna-status-badge/div",
        INFO + "/div[1]/table/tbody/tr[7]/td/cigna-claim-status/cigna-status-badge/div/div[2]",
    ],
    "Date Received": [INFO + "/div[1]/table/tbody/tr[5]/td"],
    "Date Processed": [INFO + "/div[1]/table/tbody/tr[6]/td"],
    "Claim amount due": [INFO + "/div[2]/table/tbody/tr[1]/td"],
    "Claim amount paid": [INFO + "/div[2]/table/tbody/tr[2]/td"],
    "Total provider payment": [INFO + "/div[2]/table/tbody/tr[3]/td"],
    "responsibility": [INFO + "/div[2]/table/tbody/tr[4]/td"],
    "Payee's Name": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[1]'],
    "Payee's Address": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[2]'],
    "Payment Amount": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[3]'],
    "Payment Status": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[5]'],
    "Payment Issued": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[6]'],
    "Payment Method": ['//*[@id="PAYMENT"]/div/table/tbody/tr/td[7]/span'],
}
DATE_FIELDS = {"Date Received", "Date Processed", "Payment Issued"}

# Extra procedure-table columns that make up the "Procedure Details" breakdown
PROC_DETAIL_HEADERS = (
    ("deductible", "Deductible"),
    ("copayapplied", "Copay Applied"),
    ("coveredbalance", "Covered Balance"),
    ("plancoinsurancepaid", "Plan Coinsurance Paid"),
    ("patientcoinsurance", "Patient Coinsurance"),
    ("patientresponsibility", "Patient Responsibility"),
)

# Input-sheet headers (matched ignoring case, spaces and punctuation)
IN_KEYS = {
    "invoice": "Invoice#", "name": "Patient Name", "dob": "DOB",
    "cigna_id": "Cigna ID", "dos": "DOS", "cpt": "CPT Code",
}
OUT_COLUMNS = (
    "Claim/Reference Number", "Remittance Tracking Number", "Deposit Amount",
    "Claim Status", "Date Received", "Date Processed", "Claim amount due",
    "Claim amount paid", "Total provider payment", "responsibility",
    "Payee's Name", "Payee's Address", "Payment Amount", "Payment Status",
    "Payment Issued", "Payment Method", "Procedure Code", "Notes",
    "Remark Codes", "Status",
)
OBSOLETE_COLUMNS = ("CPT Status", "Procedure Details")   # removed from Input


# --------------------------------------------------------------------------
# SMALL HELPERS
# --------------------------------------------------------------------------
def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def norm(value: Any) -> str:
    """'\\nClaim/Reference Number' -> 'claimreferencenumber'."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def plain(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.strftime("%m/%d/%Y")
    return "" if value is None else str(value)


def parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and value > 0:
        return datetime(1899, 12, 30) + timedelta(days=float(value))
    text = clean(value)
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y",
                "%B %d, %Y", "%d-%b-%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if m:
        mo, d, y = (int(p) for p in m.groups())
        y += 2000 if y < 100 else 0
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    return None


def fmt_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%m/%d/%Y") if parsed else clean(plain(value))


def same_date(a: Any, b: Any) -> bool:
    x, y = parse_date(a), parse_date(b)
    return bool(x and y and x == y)


def to_amount(value: Any) -> float:
    text = clean(value)
    m = re.search(r"\d[\d,]*\.?\d*", text)
    if not m:
        return 0.0
    amount = float(m.group(0).replace(",", ""))
    return -amount if text.startswith(("-", "(")) else amount


def as_code(value: Any) -> str:
    text = clean(plain(value)).upper()
    return text[:-2] if text.endswith(".0") else text


def extract_cpt(text: str) -> str:
    m = re.search(r"\b([A-Z]\d{4}|\d{4,5}[A-Z]?)\b", clean(text).upper())
    return m.group(1) if m else ""


def extract_cpts(text: str) -> list[str]:
    """Return every CPT-like token in a cell, preserving order."""
    found = re.findall(r"\b([A-Z]\d{4}|\d{4,5}[A-Z]?)\b", clean(text).upper())
    return list(dict.fromkeys(found))


def split_codes(text: str) -> list[str]:
    """'CO-45, 253 N130' -> ['CO-45', '253', 'N130'] (order kept, no dupes)."""
    out: list[str] = []
    for token in re.split(r"[,\s;/|]+", clean(text).upper()):
        token = token.strip(" .:()[]")
        if (token and re.search(r"\d", token)
                and re.fullmatch(r"(?:[A-Z]{1,2}-)?[A-Z]{0,3}\d{1,5}[A-Z]?", token)
                and not re.fullmatch(r"(?:19|20)\d{2}", token)
                and token not in out):
            out.append(token)
    return out


def code_key(code: str) -> str:
    code = as_code(code)
    return (code.lstrip("0") or "0") if code.isdigit() else code


def safe_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, datetime, date)):
        return value
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))[:32000]
    if text.startswith("="):
        text = "'" + text
    return text or None


# --------------------------------------------------------------------------
# DATA MODEL
# --------------------------------------------------------------------------
@dataclass
class Result:
    index: int
    dos: str = ""
    claim_no: str = ""


@dataclass
class Procedure:
    cpt: str = ""
    codes: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class Claim:
    fields: dict[str, str] = field(default_factory=dict)
    procs: list[Procedure] = field(default_factory=list)
    expl: dict[str, str] = field(default_factory=dict)   # code -> explanation
    result_dos: str = ""
    result_claim_no: str = ""
    dos_mismatch: bool = False          # selected claim DOS != requested DOS

    def get(self, name: str) -> str:
        return self.fields.get(name, "")

    def cpt_set(self) -> set[str]:
        return {as_code(p.cpt) for p in self.procs if p.cpt}


# --------------------------------------------------------------------------
# NOTES
#
#  THREE CASES (matching Availity_BCBS.build_combined_comment):
#
#  1. FULLY PAID / PARTIALLY PAID
#        DOS {dos} CLM PD {total_paid} THRU CHK #{check} ISSUED ON {date}.
#        CLM # {claim}, CPT {cpt} PD {paid} WITH COPAY {x} & COINSURANCE {y}
#        & DEDUCTIBLE {z}, CPT {cpt} PD {paid}, ...
#
#  2. FULLY DND  (every line paid = $0 AND copay/coins/ded = $0)
#        DOS {dos} CLM DND AND PROCESSED ON {processed_date}. CLM # {claim},
#        CPT {cpt} DND AS {reason/codes or master-status}, ...
#
#  3. PARTIAL  (claim paid > 0 but at least one line fully denied)
#        Paid-line format (case 1) with denied lines rendered as
#        "CPT {cpt} DND AS {reason/codes or master-status}".
#
#  The "reason" for a DND line is the remark codes scraped on that row
#  (e.g. "DND AS 45, 97, N19"), or "(NO REMARK CODE)" if there are none.
#
#  Procedure-table cell formats:
#     Deductible / Copay cell  "$135.00 / $0.00" -> DEDUCTIBLE / COPAY
#     Plan Coinsurance Paid    "100%= $85.00"    -> amount PAID = $85.00
#     Patient Coinsurance      "20%= $36.15"     -> COINSURANCE $36.15
# --------------------------------------------------------------------------
def _money(value: Any) -> str:
    """Return '$X,XXX.XX' (or '' when the value is blank/zero)."""
    text = clean(value)
    if not text or text in ("-", "--", "N/A"):
        return ""
    amount = to_amount(text)
    return f"${amount:,.2f}"


def _eq_amount(text: Any) -> float:
    """'100%= $85.00' -> 85.0 ; '0%=$0.00' -> 0.0 ; '$12.50' -> 12.5."""
    raw = clean(text)
    if "=" in raw:
        raw = raw.rsplit("=", 1)[1]
    return abs(to_amount(raw))


def _proc_detail(proc: Procedure, wanted: str) -> str:
    """Fetch a detail column by label, tolerating casing/spaces."""
    key = norm(wanted)
    for label, val in proc.details.items():
        if norm(label) == key:
            return clean(val)
    return ""


def _line_paid_float(proc: Procedure) -> float:
    """Plan paid for the line = the amount after '=' in Plan Coinsurance Paid
    (e.g. '100%= $85.00' -> 85.00)."""
    raw = _proc_detail(proc, "Plan Coinsurance Paid")
    if raw:
        return _eq_amount(raw)
    for key in ("paidamount", "paid"):                # legacy fallback
        for label, val in proc.details.items():
            if norm(label) == key:
                return abs(to_amount(val))
    return 0.0


def _ded_copay(proc: Procedure) -> tuple[float, float]:
    """Deductible / Copay cells look like '$135.00 / $0.00' where the first
    amount is the DEDUCTIBLE and the second is the COPAY (the same split is
    shown in both columns). Fall back to the plain single values."""
    for label in ("Deductible", "Copay Applied"):
        raw = _proc_detail(proc, label)
        if "/" in raw:
            left, right = raw.split("/", 1)
            return abs(to_amount(left)), abs(to_amount(right))
    return (abs(to_amount(_proc_detail(proc, "Deductible"))),
            abs(to_amount(_proc_detail(proc, "Copay Applied"))))


def _claim_paid_float(claim: Claim) -> float:
    return abs(to_amount(claim.get("Claim amount paid")))


def _proc_amounts(proc: Procedure) -> tuple[float, float, float, float]:
    """Return (paid, copay, coins, ded) floats for a procedure line."""
    paid = _line_paid_float(proc)
    ded, copay = _ded_copay(proc)
    coins = _eq_amount(_proc_detail(proc, "Patient Coinsurance"))
    return paid, copay, coins, ded


def _line_is_zero(proc: Procedure) -> bool:
    """True if line paid = $0 AND copay/coins/ded = $0."""
    return all(v < 0.005 for v in _proc_amounts(proc))


def claim_is_fully_dnd(claim: Claim) -> bool:
    """Every line zero-paid AND claim-level paid = $0.00."""
    if not claim.procs:
        return False
    if not all(_line_is_zero(p) for p in claim.procs):
        return False
    return _claim_paid_float(claim) < 0.005


def claim_is_partial(claim: Claim) -> bool:
    """Claim has some payment but at least one fully-denied line."""
    if not claim.procs:
        return False
    if _claim_paid_float(claim) < 0.005:
        return False
    return any(_line_is_zero(p) for p in claim.procs)


def _dnd_processed_date(claim: Claim) -> str:
    """Processed date used in the fully-DND prefix (falls back to issued)."""
    for name in ("Date Processed", "Payment Issued", "Date Received"):
        value = fmt_date(claim.get(name))
        if value:
            return value
    return ""


def _dnd_reason(claim: Claim, proc: Procedure,
                master_statuses: dict[str, str] | None) -> str:
    """Denied line reason = the remark codes on that line, e.g.
    '45, 97, N19, 00P43, 00H20' (or '(NO REMARK CODE)')."""
    codes = list(proc.codes)
    return ", ".join(codes) if codes else "(NO REMARK CODE)"


def cpt_labels(procs: Sequence[Procedure]) -> list[str]:
    """One label per line; a CPT that repeats gets '(LINE n)' so every
    occurrence is reported:  99212 (LINE 1), 99212 (LINE 2)."""
    totals = Counter(as_code(p.cpt) or "N/A" for p in procs)
    seen: Counter = Counter()
    labels: list[str] = []
    for p in procs:
        code = as_code(p.cpt) or "N/A"
        seen[code] += 1
        labels.append(f"{code} (LINE {seen[code]})" if totals[code] > 1 else code)
    return labels


def line_status(proc: Procedure) -> str:
    """Status of one CPT line.

    DENIED          paid, copay, coinsurance and deductible are all $0
    COPAY / DEDUCTIBLE / COINSURANCE / PATIENT RESPONSIBILITY
                    plan paid $0 but the patient owes (names joined by ", ")
    PARTIALLY PAID  plan paid > 0 but covered balance is not fully settled
    PAID            everything else
    """
    if _line_is_zero(proc):
        return "DENIED"
    paid, copay, coins, ded = _proc_amounts(proc)
    if paid < 0.005:
        names = [n for n, v in (("COPAY", copay), ("DEDUCTIBLE", ded),
                                ("COINSURANCE", coins)) if v > 0.005]
        return ", ".join(names) or "PATIENT RESPONSIBILITY"
    covered = _eq_amount(_proc_detail(proc, "Covered Balance"))
    if covered - paid - coins - copay - ded > 0.005:
        return "PARTIALLY PAID"
    return "PAID"


def build_status(claim: Claim) -> str:
    """Status column: one line per CPT, e.g.
        99212 (LINE 1) - PAID
        99212 (LINE 2) - DEDUCTIBLE
        85025 - DENIED"""
    if not claim.procs:
        return "SCRAPE INCOMPLETE"
    lines = [f"{lbl} - {line_status(proc)}" for proc, lbl in
             zip(claim.procs, cpt_labels(claim.procs))]
    if claim.dos_mismatch:
        lines.insert(0, "DOS MISMATCH")
    return "\n".join(lines)


def _paid_line_fragment(claim: Claim, proc: Procedure, label: str = "") -> str:
    """'CPT 99214 PD $144.60 WITH COINSURANCE $36.15'."""
    cpt = label or as_code(proc.cpt) or "N/A"
    paid, copay, coins, ded = _proc_amounts(proc)
    extras: list[str] = []
    if copay > 0.005:
        extras.append(f"COPAY {_money(copay)}")
    if coins > 0.005:
        extras.append(f"COINSURANCE {_money(coins)}")
    if ded > 0.005:
        extras.append(f"DEDUCTIBLE {_money(ded)}")
    line = f"CPT {cpt} PD {_money(paid)}"
    return line + (" WITH " + " & ".join(extras) if extras else "")


def _dnd_line_fragment(claim: Claim, proc: Procedure,
                       master_statuses: dict[str, str] | None,
                       label: str = "") -> str:
    cpt = label or as_code(proc.cpt) or "N/A"
    return f"CPT {cpt} DND AS {_dnd_reason(claim, proc, master_statuses)}"


def build_notes(claim: Claim, patient: dict[str, str],
                master_statuses: dict[str, str] | None = None) -> str:
    """Availity-BCBS style compact note with three cases (see header)."""
    dos = fmt_date(patient.get("dos")) or fmt_date(claim.result_dos) or ""
    claim_number = (claim.get("Claim/Reference Number")
                    or claim.result_claim_no or "")

    procs = list(claim.procs)
    target = as_code(patient.get("cpt"))
    if target:                                     # put target CPT first
        procs.sort(key=lambda p: 0 if as_code(p.cpt) == target else 1)

    labels = cpt_labels(procs)          # repeated CPTs -> "(LINE n)"
    fully_dnd = claim_is_fully_dnd(claim)
    partial = claim_is_partial(claim)

    # ---- CASE 2: fully DND ------------------------------------------------
    if fully_dnd:
        processed = _dnd_processed_date(claim)
        prefix = (f"DOS {dos} CLM DND AND PROCESSED ON {processed}. "
                  f"CLM # {claim_number}"
                  if processed else
                  f"DOS {dos} CLM DND. CLM # {claim_number}")
        fragments = [_dnd_line_fragment(claim, proc, master_statuses, lbl)
                     for proc, lbl in zip(procs, labels)]
        return prefix + (", " + ", ".join(fragments) if fragments else "")

    # ---- CASE 1 & 3: paid / partial ---------------------------------------
    total_paid = _money(claim.get("Claim amount paid")) or "$0.00"
    check_number = clean(claim.get("Remittance Tracking Number"))
    check_date = fmt_date(claim.get("Payment Issued"))
    prefix = f"DOS {dos} CLM PD {total_paid}"
    if check_number:
        prefix += f" THRU CHK #{check_number}"
        if check_date:
            prefix += f" ISSUED ON {check_date}"
    prefix += f". CLM # {claim_number}"

    fragments: list[str] = []
    for proc, lbl in zip(procs, labels):
        if partial and _line_is_zero(proc):
            fragments.append(
                _dnd_line_fragment(claim, proc, master_statuses, lbl))
        else:
            fragments.append(_paid_line_fragment(claim, proc, lbl))
    return prefix + (", " + ", ".join(f for f in fragments if f)
                     if fragments else "")


def build_remark_text(claim: Claim) -> str:
    """'45 - Charge exceeds fee schedule' one per line - DENIED CPT lines only."""
    codes: list[str] = []
    for proc in claim.procs:
        if line_status(proc) != "DENIED":
            continue
        for c in proc.codes:
            if c not in codes:
                codes.append(c)
    lines = []
    for c in codes:
        desc = claim.expl.get(c) or claim.expl.get(code_key(c)) or ""
        lines.append(f"{c} - {desc}" if desc else c)
    return "\n".join(lines)


def build_row_output(claim: Claim, patient: dict[str, str],
                     master_statuses: dict[str, str] | None = None
                     ) -> dict[str, str]:
    dos_mismatch = claim.dos_mismatch
    out = {name: claim.get(name) for name in FIELD_XPATHS}
    if not out.get("Claim/Reference Number"):
        out["Claim/Reference Number"] = claim.result_claim_no
    out["Procedure Code"] = ", ".join(
        cpt_labels([p for p in claim.procs if p.cpt]))
    if dos_mismatch:
        used = dict(patient)
        used["dos"] = fmt_date(claim.result_dos) or patient.get("dos", "")
        notes = (f"DOS MISMATCH - REQUESTED DOS {patient.get('dos') or '-'} "
                 f"NOT FOUND, USED RECENT CLAIM. "
                 + build_notes(claim, used, master_statuses))
    else:
        notes = build_notes(claim, patient, master_statuses)
    out["Notes"] = notes
    out["Remark Codes"] = build_remark_text(claim)
    out["Status"] = build_status(claim)
    return out


# --------------------------------------------------------------------------
# WORKBOOK
# --------------------------------------------------------------------------
class Book:
    def __init__(self, path: Path, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.wb = load_workbook(str(self.path))
        self.ws = self._sheet(SHEET_INPUT)
        try:
            self.master = self._sheet(SHEET_MASTER)
        except KeyError:
            self.master = self.wb.create_sheet(SHEET_MASTER)
            for i, t in enumerate(("Remark", "Remark Reason", "Status"), 1):
                self.master.cell(1, i).value = t

        for key in [norm(t) for t in OBSOLETE_COLUMNS]:     # drop old columns
            cols = [c for c in range(1, self.ws.max_column + 1)
                    if norm(self.ws.cell(1, c).value) == key]
            for c in reversed(cols):
                self.ws.delete_cols(c)
                say(f"Removed column '{key}' from Input")
        self.col = self._header_map(self.ws)
        for title in OUT_COLUMNS:
            if norm(title) not in self.col:
                self._add_column(self.ws, self.col, title)

        missing = [h for h in IN_KEYS.values() if norm(h) not in self.col]
        if missing:
            raise ValueError(f"Input sheet is missing columns: {missing}. "
                             f"Found: {list(self.col)}")

        self.mcol = self._header_map(self.master)
        for key, title in (("remark", "Remark"), ("remarkreason", "Remark Reason"),
                           ("status", "Status")):
            if key not in self.mcol:
                self._add_column(self.master, self.mcol, title)
        self.master_codes: dict[str, int] = {}
        self.master_reasons: dict[str, str] = {}
        self.master_statuses: dict[str, str] = {}
        self._load_master()

    # ---- sheet helpers ----------------------------------------------------
    def _sheet(self, name: str):
        for s in self.wb.sheetnames:
            if s.strip().lower() == name.lower():
                return self.wb[s]
        raise KeyError(f"Sheet '{name}' not found: {self.wb.sheetnames}")

    @staticmethod
    def _header_map(ws) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            key = norm(ws.cell(1, c).value)
            if key and key not in mapping:
                mapping[key] = c
        return mapping

    @staticmethod
    def _add_column(ws, mapping: dict[str, int], title: str) -> None:
        c = max(ws.max_column, 1) + 1
        ws.cell(1, c).value = title
        mapping[norm(title)] = c

    def get(self, row: int, header: str) -> Any:
        return self.ws.cell(row, self.col[norm(header)]).value

    # ---- master -----------------------------------------------------------
    def _load_master(self) -> None:
        rc = self.mcol["remark"]
        status_col = self.mcol.get("status")
        reason_col = self.mcol.get("remarkreason")
        for r in range(2, self.master.max_row + 1):
            text = clean(self.master.cell(r, rc).value)
            # Row 2 of the supplied workbook holds XPaths, not remark codes.
            if not text or text.startswith(("/", "//")):
                continue
            reason = clean(self.master.cell(r, reason_col).value) if reason_col else ""
            if not reason and " - " in text:
                reason = clean(text.split(" - ", 1)[1])
            status = clean(self.master.cell(r, status_col).value) if status_col else ""
            for code in split_codes(text.split(" - ")[0]) or [text.upper()]:
                key = code_key(code)
                self.master_codes.setdefault(key, r)
                if reason:
                    self.master_reasons.setdefault(key, reason)
                if status:
                    self.master_statuses.setdefault(key, status)

    def apply_master_reasons(self, claim: Claim) -> None:
        """Use Master.Remark + Master.Remark Reason to complete blank explanations."""
        for key, reason in self.master_reasons.items():
            if reason:
                claim.expl.setdefault(key, reason)
        for proc in claim.procs:
            for code in proc.codes:
                reason = self.master_reasons.get(code_key(code))
                if reason:
                    claim.expl.setdefault(code, reason)

    def sync_master(self, claim: Claim) -> list[str]:
        """Add unseen remark codes of DENIED CPT lines: Remark | Reason | Status."""
        if self.dry_run:
            return []
        added: list[str] = []
        for proc in claim.procs:
            if line_status(proc) != "DENIED":
                continue
            for code in proc.codes:
                key = code_key(code)
                reason = (claim.expl.get(code) or claim.expl.get(key) or "")
                if key in self.master_codes:
                    row = self.master_codes[key]
                    col = self.mcol["remarkreason"]
                    if not clean(self.master.cell(row, col).value) and reason:
                        self.master.cell(row, col).value = safe_cell(reason)
                        self.master_reasons[key] = reason
                    continue
                row = self._next_master_row()
                self.master.cell(row, self.mcol["remark"]).value = code
                self.master.cell(row, self.mcol["remarkreason"]).value = safe_cell(reason)
                self.master.cell(row, self.mcol["status"]).value = "DENIED"
                if reason:
                    self.master_reasons[key] = reason
                self.master_statuses.setdefault(key, "DENIED")
                self.master_codes[key] = row
                added.append(code)
        return added

    def _next_master_row(self) -> int:
        for r in range(2, self.master.max_row + 2):
            if all(not clean(self.master.cell(r, c).value)
                   for c in range(1, self.master.max_column + 1)):
                return r
        return self.master.max_row + 1

    # ---- patients ---------------------------------------------------------
    def patient(self, row: int) -> dict[str, str]:
        full = clean(self.get(row, "Patient Name"))
        first = last = ""
        if "," in full:
            last, first = (clean(p) for p in full.split(",", 1))
        elif full:
            parts = full.split()
            first, last = " ".join(parts[:-1]), parts[-1]
            if len(parts) == 1:
                first, last = parts[0], ""
        billed = ""
        for header in BILLED_HEADERS:             # optional column
            col = self.col.get(norm(header))
            if col:
                billed = clean(plain(self.ws.cell(row, col).value))
                break
        return {
            "billed": billed,
            "invoice": clean(plain(self.get(row, "Invoice#"))),
            "name": full, "first": first, "last": last,
            "dob": fmt_date(self.get(row, "DOB")),
            "cigna_id": re.sub(r"\s+", "", plain(self.get(row, "Cigna ID"))),
            "dos": fmt_date(self.get(row, "DOS")),
            "cpt": as_code(self.get(row, "CPT Code")),
        }

    def pending(self, limit: int = 0, start_row: int = 0):
        items = []
        for r in range(max(2, start_row), self.ws.max_row + 1):
            p = self.patient(r)
            if not (p["name"] or p["cigna_id"] or p["dob"]):
                continue
            if clean(self.get(r, "Status")):
                continue
            items.append((r, p))
            if limit and len(items) >= limit:
                break
        return items

    def write(self, row: int, values: dict[str, str]) -> None:
        for header, value in values.items():
            c = self.col.get(norm(header))
            if c:
                self.ws.cell(row, c).value = safe_cell(value)

    def save(self) -> None:
        if self.dry_run:
            return
        tmp = self.path.with_name(self.path.stem + ".tmp" + self.path.suffix)
        for attempt in range(6):
            try:
                self.wb.save(str(tmp))
                os.replace(str(tmp), str(self.path))
                return
            except PermissionError:
                say(f"   {BAD} Excel file is open - close it. Retrying...")
                time.sleep(3 + attempt * 2)
        raise PermissionError(f"Cannot save, workbook locked: {self.path}")


# --------------------------------------------------------------------------
# BROWSER  (attaches to Chrome on port 9222)
# --------------------------------------------------------------------------
def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


class Bot:
    def __init__(self, host: str = DEBUGGER_HOST, port: int = DEBUGGER_PORT):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        if not port_open(host, port):
            raise RuntimeError(
                f"Chrome is not listening on {host}:{port}.\n"
                "Start Chrome with --remote-debugging-port=9222 and log in "
                "to Cigna first (see the top of this file).")
        opts = Options()
        opts.debugger_address = f"{host}:{port}"
        self.d = webdriver.Chrome(options=opts)
        self.d.implicitly_wait(0)
        self._pick_cigna_tab()

    def _pick_cigna_tab(self) -> None:
        for handle in self.d.window_handles:
            try:
                self.d.switch_to.window(handle)
                if "cigna" in (self.d.current_url or "").lower():
                    self.main = handle
                    return
            except Exception:
                continue
        self.main = self.d.window_handles[0]
        self.d.switch_to.window(self.main)
        log.warning("No Cigna tab found - using the first tab.")

    # ---- low level --------------------------------------------------------
    def els(self, xpath: str):
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import WebDriverException
        try:
            return self.d.find_elements(By.XPATH, xpath)
        except WebDriverException:
            return []

    def wait_for(self, xpath: str, timeout: float = WAIT, visible: bool = True):
        end = time.time() + timeout
        while time.time() < end:
            for el in self.els(xpath):
                try:
                    if not visible or el.is_displayed():
                        return el
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def shown(self, xpath: str, timeout: float = 0.5) -> bool:
        return self.wait_for(xpath, timeout) is not None

    def text(self, xpath: str, timeout: float = 0.5) -> str:
        el = self.wait_for(xpath, timeout, visible=False)
        if el is None:
            return ""
        try:
            return clean(el.text or el.get_attribute("textContent"))
        except Exception:
            return ""

    def click(self, xpath: str, timeout: float = WAIT) -> bool:
        el = self.wait_for(xpath, timeout)
        if el is None:
            return False
        try:
            self.d.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
        except Exception:
            try:
                self.d.execute_script("arguments[0].click();", el)
            except Exception:
                return False
        return True

    def type_into(self, xpath: str, value: str) -> None:
        from selenium.webdriver.common.keys import Keys
        el = self.wait_for(xpath, WAIT)
        if el is None:
            raise TimeoutError(f"Field not found: {xpath}")
        try:
            el.click()
            el.send_keys(Keys.CONTROL, "a", Keys.DELETE)
            el.send_keys(value)
        except Exception:
            pass
        if clean(el.get_attribute("value")) != clean(value):   # JS fallback
            self.d.execute_script(
                "const e=arguments[0],v=arguments[1];e.focus();"
                "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"
                "'value').set.call(e,v);"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));",
                el, value)

    def clear(self, xpath: str) -> None:
        from selenium.webdriver.common.keys import Keys
        el = self.wait_for(xpath, 1)
        if el is not None:
            try:
                el.click()
                el.send_keys(Keys.CONTROL, "a", Keys.DELETE)
            except Exception:
                pass

    def idle(self, timeout: float = 6) -> None:
        end = time.time() + timeout
        busy = ("//cigna-loading | //*[@aria-busy='true'] | "
                "//*[contains(@class,'spinner')]")
        while time.time() < end:
            if not any(self._vis(e) for e in self.els(busy)):
                return
            time.sleep(0.3)

    @staticmethod
    def _vis(el) -> bool:
        try:
            return el.is_displayed()
        except Exception:
            return False

    def screenshot(self, name: str) -> None:
        try:
            folder = APP_DIR / "screenshots"
            folder.mkdir(exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            self.d.save_screenshot(
                str(folder / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.png"))
        except Exception:
            pass

    # ---- patient search ---------------------------------------------------
    def open_search(self) -> None:
        if self.shown(SEARCH["form"], 1):
            return
        if not self.click(NAV["claims_menu"], 10):
            raise RuntimeError("Claims menu not found - is Chrome logged in?")
        time.sleep(0.5)
        if not self.shown(SEARCH["form"], 2):
            self.click(NAV["search_claim_menu"], 8)
        if not self.shown(SEARCH["form"], 15):
            raise RuntimeError("Search Claim page did not open.")

    FIELDS = {
        "id_dob": ("member_id", "dob"),
        "name_dob": ("first_name", "last_name", "dob"),
        "name_id": ("first_name", "last_name", "member_id"),
    }
    TABS = {"id_dob": "type1", "name_dob": "type2", "name_id": "type3"}

    def select_method(self, kind: str) -> None:
        """Select a patient-search method and verify its inputs are visible."""
        radio_xpath = SEARCH[self.TABS[kind]]
        selected = False
        for el in self.els(radio_xpath):
            try:
                if not el.is_displayed():
                    continue
                self.d.execute_script(
                    "const e=arguments[0]; const t=e.closest('label') || "
                    "e.closest('[role=radio]') || e; t.click();", el)
                selected = True
                break
            except Exception:
                continue
        if not selected:
            selected = self.click(radio_xpath, 5)
        if not selected:
            raise RuntimeError(f"Search method '{kind}' option could not be selected.")

        end = time.time() + 15
        while time.time() < end:
            self.idle(1)
            ready = True
            for field in self.FIELDS[kind]:
                el = self.wait_for(SEARCH[field], 0.3, visible=True)
                if el is None:
                    ready = False
                    break
                try:
                    if not el.is_enabled():
                        ready = False
                        break
                except Exception:
                    pass
            if ready:
                return
            time.sleep(0.3)
        raise RuntimeError(f"Search method '{kind}' fields did not become visible after selecting it.")

    def fill(self, kind: str, p: dict[str, str]) -> None:
        for f in ("dob", "member_id", "first_name", "last_name"):
            self.clear(SEARCH[f])
        if "member_id" in self.FIELDS[kind]:
            self.type_into(SEARCH["member_id"], p["cigna_id"])
        if "first_name" in self.FIELDS[kind]:
            self.type_into(SEARCH["first_name"], p["first"])
            self.type_into(SEARCH["last_name"], p["last"])
        if "dob" in self.FIELDS[kind]:
            self.type_into(SEARCH["dob"], p["dob"])

    def signature(self) -> tuple:
        return tuple((r.dos, r.claim_no) for r in self.results())

    def results(self) -> list[Result]:
        """Read claim number and DOS from the search-result row using headings."""
        out: list[Result] = []
        headers = []
        for header in self.els("//cigna-results-table//table/thead/tr[1]/*"):
            try:
                headers.append(norm(header.get_attribute("textContent") or header.text))
            except Exception:
                headers.append("")

        dos_idx = next(
            (i for i, h in enumerate(headers)
             if h in ("dos", "dateofservice") or "dateofservice" in h),
            5,
        )
        claim_idx = next(
            (i for i, h in enumerate(headers)
             if "claimnumber" in h or "claimreferencenumber" in h
             or h in ("claim", "claimid")),
            1,
        )

        for i, row in enumerate(self.els(SEARCH["rows"]), 1):
            try:
                tds = row.find_elements("xpath", "./td")
                if not tds:
                    continue
                claim_no = ""
                if claim_idx < len(tds):
                    cell = tds[claim_idx]
                    cell_text = clean(
                        cell.get_attribute("innerText") or
                        cell.get_attribute("textContent") or cell.text
                    )
                    number_match = re.search(r"\b\d{8,20}\b", cell_text)
                    if number_match:
                        claim_no = number_match.group(0)
                    else:
                        links = cell.find_elements("xpath", ".//a")
                        if links:
                            claim_no = clean(" ".join(
                                a.get_attribute("innerText") or
                                a.get_attribute("textContent") or a.text for a in links
                            ))
                        else:
                            claim_no = cell_text
                if not claim_no:
                    links = row.find_elements("xpath", ".//a")
                    if links:
                        claim_no = clean(
                            links[0].get_attribute("innerText") or
                            links[0].get_attribute("textContent") or links[0].text
                        )
                dos_value = ""
                if dos_idx < len(tds):
                    cell = tds[dos_idx]
                    dos_value = clean(
                        cell.get_attribute("innerText") or
                        cell.get_attribute("textContent") or cell.text
                    )
                if claim_no or dos_value:
                    out.append(Result(i, dos_value, claim_no))
            except Exception:
                continue
        return out

    def run_search(self) -> list[Result]:
        before = self.signature()
        if not self.click(SEARCH["button"], 15):
            raise RuntimeError("Patient Search button not found.")
        start = time.time()
        while time.time() - start < WAIT:
            self.idle(2)
            rows = self.results()
            if rows and (not before or tuple((r.dos, r.claim_no) for r in rows)
                         != before or time.time() - start > 8):
                return rows
            if not rows:
                body = self.text("//*[@id='results-top']", 0.5).lower()
                if any(m in body for m in NO_RESULT_MARKERS) or time.time() - start > 8:
                    return []
            time.sleep(0.5)
        return self.results()

    def search_patient(self, p: dict[str, str],
                       forced: str | None = None) -> list[Result]:
        ok = {
            "id_dob": bool(p["cigna_id"] and p["dob"]),
            "name_dob": bool(p["first"] and p["last"] and p["dob"]),
            "name_id": bool(p["first"] and p["last"] and p["cigna_id"]),
        }
        # Every patient ALWAYS starts again from Method 1 (Cigna ID + DOB);
        # Method 2 / 3 are only fallbacks when the earlier one finds nothing.
        kinds = [forced] if forced else [k for k in ("id_dob", "name_dob",
                                                     "name_id") if ok[k]]
        if not kinds:
            raise ValueError("No usable search keys (Cigna ID / DOB / name).")
        if not forced and not ok["id_dob"]:
            log.info("   Cigna ID / DOB missing - Method 1 skipped")
        self.open_search()
        for kind in kinds:
            try:
                log.info("   searching with %s", kind)
                self.select_method(kind)          # re-select the option each time
                self.fill(kind, p)
                rows = self.run_search()
                log.info("   search %-8s -> %d row(s)", kind, len(rows))
                if rows:
                    return rows
            except Exception as exc:  # try the next method
                log.warning("   search %s failed: %s", kind, exc)
                try:
                    self.open_search()
                except Exception:
                    pass
        return []

    # ---- claim page -------------------------------------------------------
    def wait_claim(self, timeout: float = 40) -> bool:
        ok = self.wait_for("//cigna-claim-details-screen", timeout)
        if ok is None:
            return False
        self.idle(8)
        end = time.time() + timeout
        previous = -1
        stable = 0
        while time.time() < end:
            self.idle(2)
            rows = len(self.els(PROC_ROWS))
            if rows > 0 and rows == previous:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            previous = rows
            time.sleep(0.5)
        return True

    def wait_results_page(self, timeout: float = 20) -> bool:
        """Verify we are on the Claims results page before opening a result."""
        end = time.time() + timeout
        while time.time() < end:
            self.idle(1)
            rows = self.els(SEARCH["rows"])
            if rows:
                try:
                    if any(r.is_displayed() for r in rows):
                        return True
                except Exception:
                    return True
            if self.shown(SEARCH["form"], 0.2):
                return True
            time.sleep(0.3)
        return False

    def _click_result_link(self, index: int) -> bool:
        """Re-find the current result row and click its claim link safely."""
        row_xpath = f"({SEARCH['rows']})[{index}]"
        end = time.time() + 12
        while time.time() < end:
            row_items = self.els(row_xpath)
            if not row_items:
                time.sleep(0.3)
                continue
            row = row_items[0]
            try:
                if not row.is_displayed():
                    time.sleep(0.2)
                    continue
                self.d.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", row)
                links = row.find_elements("xpath", ".//td[2]//a | .//a")
                for link in links:
                    try:
                        if link.is_displayed() and link.is_enabled():
                            self.d.execute_script("arguments[0].click();", link)
                            return True
                    except Exception:
                        continue
                cells = row.find_elements("xpath", "./td[2] | .")
                for cell in cells:
                    try:
                        if cell.is_displayed():
                            self.d.execute_script("arguments[0].click();", cell)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def open_claim(self, res: Result) -> tuple[Claim, bool]:
        """Verify Claims results, open selected claim, then verify claim page."""
        if not self.wait_results_page(20):
            raise RuntimeError("Claims results page is not ready; claim was not clicked.")

        rows = self.results()
        if not rows or not any(r.index == res.index for r in rows):
            raise RuntimeError(f"Claim result row {res.index} is no longer available.")

        old = set(self.d.window_handles)
        if not self._click_result_link(res.index):
            self.screenshot(f"claim_click_failed_row_{res.index}")
            raise RuntimeError("Claim link could not be clicked after verifying the Claims results page.")

        end = time.time() + 25
        new_handle = None
        while time.time() < end:
            new = [h for h in self.d.window_handles if h not in old]
            if new:
                new_handle = new[-1]
                break
            if self.shown("//cigna-claim-details-screen", 0.3):
                break
            time.sleep(0.3)

        if new_handle:
            self.d.switch_to.window(new_handle)
            try:
                if not self.wait_claim(35):
                    self.screenshot(f"claim_page_not_loaded_{res.index}")
                    raise RuntimeError("Claim link was clicked, but the claim details page did not load.")
                return self._scrape_checked(res), False
            finally:
                try:
                    self.d.close()
                finally:
                    self.d.switch_to.window(self.main)

        if not self.wait_claim(35):
            self.screenshot(f"claim_page_not_loaded_{res.index}")
            raise RuntimeError("Claim link was clicked, but the claim details page did not load.")
        return self._scrape_checked(res), True

    def _scrape_checked(self, res: Result) -> Claim:
        if not self.wait_claim(30):
            raise RuntimeError("Claim details page did not open.")
        claim = self.scrape_claim()
        claim.result_dos = res.dos
        claim.result_claim_no = res.claim_no
        if not claim.get("Claim/Reference Number") and res.claim_no:
            claim.fields["Claim/Reference Number"] = res.claim_no
        got = re.sub(r"\D", "", claim.get("Claim/Reference Number"))
        want = re.sub(r"\D", "", res.claim_no)
        if got and want and got != want:
            raise RuntimeError(f"Opened claim {got} instead of {want}.")
        return claim

    def _first_text(self, xpaths: Sequence[str]) -> str:
        """Read the first populated XPath; tolerate Angular late rendering."""
        for xp in xpaths:
            value = self.text(xp, 1.2)
            if value:
                return value
        return ""

    def _click_claim_tab(self, words: tuple[str, ...]) -> None:
        """Click only real tab/button controls; never click ordinary links."""
        xp = ("//cigna-claim-details-screen//*[@role='tab'] | "
              "//cigna-claim-details-screen//button | "
              "//cigna-claim-details-screen//cigna-tab")
        for el in self.els(xp):
            try:
                label = norm(el.get_attribute("textContent") or el.text)
                if any(word in label for word in words) and el.is_displayed():
                    self.d.execute_script("arguments[0].click();", el)
                    self.idle(2)
                    time.sleep(0.5)
                    return
            except Exception:
                continue

    def _reveal_payment_tab(self) -> None:
        """Reveal payment/remittance tabs when their data panels are not visible."""
        payment = '//*[@id="PAYMENT"]'
        remittance = '//*[@id="remittance-section"]'
        if not any(self._vis(e) for e in self.els(payment)):
            self._click_claim_tab(("paymentinformation", "payment"))
        if not any(self._vis(e) for e in self.els(remittance)):
            self._click_claim_tab(("remittance",))

    def _label_value(self, header: str) -> str:
        """Fallback for pages where Cigna changes table row positions."""
        aliases = {
            "Claim/Reference Number": ("claimreferencenumber", "claimnumber"),
            "Remittance Tracking Number": ("remittancetrackingnumber",),
            "Deposit Amount": ("depositamount",),
            "Claim Status": ("claimstatus", "status"),
            "Date Received": ("datereceived",),
            "Date Processed": ("dateprocessed",),
            "Claim amount due": ("claimamountdue", "amountdue"),
            "Claim amount paid": ("claimamountpaid", "amountpaid"),
            "Total provider payment": ("totalproviderpayment", "providerpayment"),
            "responsibility": ("responsibility", "patientresponsibility"),
        }
        wanted = aliases.get(header, (norm(header),))
        for row in self.els(INFO + "//table//tr"):
            try:
                cells = row.find_elements("xpath", "./th|./td")
                values = [clean(c.get_attribute("textContent") or c.text) for c in cells]
                for i, label in enumerate(values):
                    key = norm(label)
                    if key and any(key == a or key.startswith(a) for a in wanted):
                        for value in values[i + 1:]:
                            if value:
                                return value
            except Exception:
                continue
        return ""

    def _wait_claim_fields(self, timeout: float = 35) -> None:
        """Wait for claim/payment panels to populate, not just the page shell."""
        end = time.time() + timeout
        previous = None
        stable = 0
        while time.time() < end:
            self.idle(1)
            snapshot = tuple(
                self._first_text(FIELD_XPATHS[name])
                for name in ("Claim/Reference Number", "Claim Status",
                             "Claim amount due", "Claim amount paid",
                             "Remittance Tracking Number", "Deposit Amount")
            )
            if any(snapshot) and snapshot == previous:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            previous = snapshot
            time.sleep(0.7)

    def scrape_claim(self) -> Claim:
        claim = Claim()
        self.idle(3)
        self._wait_claim_fields(20)
        self._reveal_payment_tab()
        self._wait_claim_fields(15)
        for header, xpaths in FIELD_XPATHS.items():
            value = self._first_text(xpaths)
            if not value:
                value = self._label_value(header)
            claim.fields[header] = fmt_date(value) if header in DATE_FIELDS else value

        payee_cells = self.els('//*[@id="PAYMENT"]//table//tbody/tr[1]/td[1]')
        if payee_cells:
            try:
                raw_name = self.d.execute_script(
                    "return (arguments[0].innerText || arguments[0].textContent || '').trim();",
                    payee_cells[0],
                )
                full_name = clean(raw_name)
                if full_name:
                    claim.fields["Payee's Name"] = full_name
            except Exception:
                pass

        deadline = time.time() + WAIT
        while time.time() < deadline and not self.els(PROC_ROWS):
            self.idle(1)
            time.sleep(0.35)
        claim.procs = self.scrape_procedures()
        claim.expl = self.scrape_explanations()
        if not claim.procs:
            raise RuntimeError(
                "Claim page loaded but no procedure rows were scraped; "
                "refusing to classify/write an empty claim."
            )
        log.info("   claim %s | %s | %d procedure(s) | %d explanation(s)",
                 claim.get("Claim/Reference Number") or "?",
                 claim.get("Claim Status") or "?", len(claim.procs),
                 len(claim.expl))
        return claim

    def scrape_procedures(self) -> list[Procedure]:
        table = self.wait_for(PROC_TABLES, 8)
        if table is None:
            table = self.wait_for(PROC, 3)
        tables = self.els(PROC_TABLES) or ([table] if table else [])
        procs: list[Procedure] = []
        seen_rows: set = set()
        for tbl in tables:
            try:
                heads = [norm(clean(h.get_attribute("textContent")))
                         for h in tbl.find_elements("xpath", ".//thead//th")]
                remark_idx = next((i for i, h in enumerate(heads) if "remark" in h), None)
                detail_idx = {}
                for key, label in PROC_DETAIL_HEADERS:
                    idx = next((i for i, h in enumerate(heads) if key in h), None)
                    if idx is not None:
                        detail_idx[label] = idx
                rows = tbl.find_elements("xpath", ".//tbody/tr")
            except Exception:
                continue

            for row in rows:
                # Same DOM row must never be read twice, but genuinely repeated
                # CPT lines (different rows) are all kept.
                row_id = getattr(row, "id", None)
                if row_id in seen_rows:
                    continue
                seen_rows.add(row_id)
                try:
                    tds = [clean(td.get_attribute("textContent"))
                           for td in row.find_elements("xpath", "./td")]
                except Exception:
                    continue
                if not tds:
                    continue
                cpts = extract_cpts(tds[0])
                if not cpts:
                    for cell in tds[1:]:
                        cpts = extract_cpts(cell)
                        if cpts:
                            break
                if remark_idx is None:
                    remark_idx = 11 if len(tds) > 11 else None
                remark_text = tds[remark_idx] if remark_idx is not None and remark_idx < len(tds) else ""
                codes = split_codes(remark_text)
                details = {lbl: tds[i] for lbl, i in detail_idx.items() if i < len(tds)}
                for cpt in cpts:
                    procs.append(Procedure(cpt=cpt, codes=list(codes), details=dict(details)))
                if not cpts and codes:
                    procs.append(Procedure(cpt="", codes=codes, details=details))
        return procs          # repeated CPT lines are intentionally kept

    def scrape_explanations(self) -> dict[str, str]:
        expl: dict[str, str] = {}
        blocks = self.els(EXPL) or self.els("//cigna-medical-explanation-details//div")
        for block in blocks:
            try:
                code_el = (block.find_elements("xpath", "./div[1]")
                           or block.find_elements("xpath", "./strong[1]"))
                if not code_el:
                    continue
                desc_el = (block.find_elements("xpath", "./div[2]//p")
                           or block.find_elements("xpath", "./div[2]"))
                desc = " ".join(clean(e.get_attribute("textContent"))
                                for e in desc_el).strip()
                for code in split_codes(code_el[0].get_attribute("textContent")):
                    expl.setdefault(code, desc)
            except Exception:
                continue
        return expl

    def back_to_results(self, p: dict[str, str]) -> None:
        if self.click(NAV["return_claims"], 6) and self.wait_for(SEARCH["rows"], 5):
            return
        try:
            self.d.back()
            if self.wait_for(SEARCH["rows"], 5):
                return
        except Exception:
            pass
        self.search_patient(p)


# --------------------------------------------------------------------------
# PER-PATIENT WORKFLOW
# --------------------------------------------------------------------------
def _claim_no_int(res: Result) -> int:
    digits = re.sub(r"\D", "", res.claim_no or "")
    return int(digits) if digits else 0


def _billed_matches(claim: Claim, billed: str) -> bool:
    """Billed amount from Input vs the claim page's 'Claim amount due'."""
    got = claim.get("Claim amount due")
    if not clean(got):
        return False
    return abs(abs(to_amount(got)) - abs(to_amount(billed))) < 0.005


def process_patient(bot: Bot, p: dict[str, str], max_claims: int,
                    forced: str | None,
                    master_statuses: dict[str, str] | None = None
                    ) -> tuple[dict[str, str], Claim | None]:
    """CLAIM SELECTION
    * nothing in the search list              -> Patient Not Found
    * CPT and/or Billed Amount supplied       -> open the most recent claims
      (latest DOS, then highest claim number, up to max_claims) and pick the
      first one where the CPT is present AND the billed amount matches
          - CPT in no checked claim           -> CPT Not Matched
          - CPT found, billed amount differs  -> Billed Amount Not Matched
    * neither CPT nor Billed Amount supplied  -> keep the claims whose DOS
      matches and take the highest claim number (no DOS match -> highest
      claim number overall, flagged DOS Mismatch)
    """
    results = bot.search_patient(p, forced)
    if not results:
        who = clean(f"{p['first']} {p['last']}") or p["name"] or "patient"
        return {"Status": STATUS_NOT_FOUND,
                "Notes": f"No claim found for {who} (DOB {p['dob'] or '-'}, "
                         f"Cigna ID {p['cigna_id'] or '-'})"}, None

    dos, target, billed = p["dos"], p["cpt"], p.get("billed", "")
    needs_match = bool(target or billed)

    if needs_match:
        candidates = sorted(
            results,
            key=lambda r: (parse_date(r.dos) or datetime(1900, 1, 1),
                           _claim_no_int(r)),
            reverse=True)[:max_claims]
        log.info("   CPT %s / billed %s supplied - checking %d recent claim(s)",
                 target or "-", billed or "-", len(candidates))
    else:
        same = [r for r in results if dos and same_date(r.dos, dos)]
        if dos and not same:
            log.warning("   DOS mismatch: requested %s; results show %s - "
                        "using highest claim number (DOS fallback)", dos,
                        ", ".join(r.dos or "blank DOS" for r in results))
        candidates = sorted(same or results, key=_claim_no_int,
                            reverse=True)[:max_claims]
        log.info("   No CPT / billed amount - DOS match, highest claim number")

    chosen: tuple[Claim, Result] | None = None
    opened = 0
    cpt_hit = False
    for i, res in enumerate(candidates, 1):
        say(f"   Checking claim {res.claim_no or '?'} (DOS {res.dos or '?'}) "
            f"[{i}/{len(candidates)}]")
        try:
            claim, needs_back = bot.open_claim(res)
        except Exception as exc:
            log.warning("   claim row %d failed: %s", res.index, exc)
            bot.screenshot(f"claim_row{res.index}")
            try:
                if not bot.shown(SEARCH["rows"], 1):
                    bot.back_to_results(p)
            except Exception:
                pass
            continue
        opened += 1
        try:
            if not needs_match:
                chosen = (claim, res)
                break
            cpt_ok = (not target) or target in claim.cpt_set()
            billed_ok = (not billed) or _billed_matches(claim, billed)
            cpt_hit = cpt_hit or (bool(target) and cpt_ok)
            if cpt_ok and billed_ok:
                chosen = (claim, res)
                break
            why = []
            if target:
                why.append(f"CPT {OK if cpt_ok else BAD}")
            if billed:
                why.append(f"Billed {OK if billed_ok else BAD}")
            say(f"   {BAD} Claim {res.claim_no or '?'} not matched "
                f"({' | '.join(why)})")
        finally:
            if needs_back:
                bot.back_to_results(p)
        if i < len(candidates):
            say("   Moving to next claim...")
            time.sleep(MISMATCH_WAIT)

    if opened == 0:
        raise RuntimeError("None of the claim rows could be opened.")

    if chosen is None:
        if target and not cpt_hit:
            log.error("   CPT %s not found in any checked claim.", target)
            return {"Status": STATUS_CPT_NOT_MATCHED,
                    "Notes": (f"Requested CPT {target} was not found in the "
                              f"{opened} checked claim(s). No claim details "
                              f"were written.")}, None
        log.error("   Billed amount %s did not match any checked claim.", billed)
        return {"Status": STATUS_BILLED_NOT_MATCHED,
                "Notes": (f"CPT {target or '-'} found but billed amount "
                          f"{billed} did not match the claim amount in the "
                          f"{opened} checked claim(s). No claim details "
                          f"were written.")}, None

    claim, res = chosen
    claim.dos_mismatch = bool(dos) and not same_date(res.dos, dos)
    return build_row_output(claim, p, master_statuses), claim


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def setup_logging() -> None:
    """No log file and no logging output - the cmd window uses say() only."""
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.propagate = False
    if os.name == "nt":                # let cmd show the tick / cross symbols
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def show_details(out: dict[str, str], claim: Claim) -> None:
    """Print the retrieved claim details line by line in the cmd window."""
    def row(label: str, value: Any) -> None:
        value = clean(value)
        if value:
            say(f"      {label:<16}: {value}")

    say(f"   Selected claim {out.get('Claim/Reference Number') or '?'}")
    row("DOS", fmt_date(claim.result_dos))
    row("Claim Status", out.get("Claim Status"))
    row("Date Processed", out.get("Date Processed"))
    row("Amount Due", out.get("Claim amount due"))
    row("Amount Paid", out.get("Claim amount paid"))
    row("Provider Payment", out.get("Total provider payment"))
    row("Responsibility", out.get("responsibility"))
    row("Check / Tracking #", out.get("Remittance Tracking Number"))
    row("Payment Issued", out.get("Payment Issued"))
    for line in (out.get("Status") or "").splitlines():
        say(f"      {line}")
    for line in (out.get("Remark Codes") or "").splitlines():
        row("Remark", line)
    row("Notes", out.get("Notes"))


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Cigna claim automation")
    ap.add_argument("--excel", type=Path, default=DEFAULT_EXCEL)
    ap.add_argument("--port", type=int, default=DEBUGGER_PORT,
                    help="Chrome remote-debugging port")
    ap.add_argument("--rows", type=int, default=0, help="max rows (0 = all)")
    ap.add_argument("--start-row", type=int, default=0)
    ap.add_argument("--max-claims", type=int, default=MAX_CLAIMS)
    ap.add_argument("--search-type", choices=("id_dob", "name_dob", "name_id"))
    ap.add_argument("--dry-run", action="store_true",
                    help="do not write or save the workbook")
    ap.add_argument("--no-backup", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging()
    if not args.excel.exists():
        say(f"{BAD} Workbook not found")
        return 2

    if not args.dry_run and not args.no_backup:
        target = args.excel.with_name(f"{args.excel.stem} - backup{args.excel.suffix}")
        shutil.copy2(args.excel, target)          # same folder, overwritten each run

    try:
        book = Book(args.excel, dry_run=args.dry_run)
    except Exception as exc:
        say(f"{BAD} Cannot open workbook: {exc}")
        return 2

    pending = book.pending(args.rows, args.start_row)
    total = len(pending)
    if not pending:
        return 0

    try:
        bot = Bot(port=args.port)
    except Exception as exc:
        say(f"{BAD} {exc}")
        return 1
    say(f"{OK} Chrome connected")
    say("")

    done = failed = 0
    try:
        for n, (row, p) in enumerate(pending, 1):
            say("=" * 19)
            say(f"Processing {n}/{total}")
            say("=" * 19)
            say(f"   {p['name'] or 'No name'} | "
                f"DOS {p['dos'] or '-'} | CPT {p['cpt'] or '-'}")
            try:
                out, claim = process_patient(
                    bot, p, args.max_claims, args.search_type,
                    book.master_statuses,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error("Row %d failed: %s", row, exc)
                bot.screenshot(f"row{row}_failed")
                book.write(row, {"Status": STATUS_FAILED,
                                 "Notes": f"{type(exc).__name__}: {exc}"[:500]})
                book.save()
                failed += 1
                say(f"   {BAD} Failed: {str(exc)[:90]}")
                try:
                    bot.open_search()
                except Exception:
                    pass
                continue

            if claim is not None:
                added = book.sync_master(claim)
                book.apply_master_reasons(claim)
                out = build_row_output(claim, p, book.master_statuses)
                if added:
                    log.info("Master: added %s", ", ".join(added))
            if not args.dry_run:
                book.write(row, out)
                book.save()
            status = out.get("Status", "")
            log.info("Notes: %s", out.get("Notes", ""))
            shown = status.replace("\n", "  |  ")
            if claim is None:                    # not found / not matched
                failed += 1
                say(f"   {BAD} {shown}")
            else:
                done += 1
                say(f"   {OK} Claim retrieved")
                show_details(out, claim)
    except KeyboardInterrupt:
        say(f"\n{BAD} Stopped - progress already saved.")
        return 130
    # Chrome is deliberately left open - it is your logged-in session.

    say("")
    say(f"Finished: {OK} {done} done   {BAD} {failed} not done   (total {total})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())