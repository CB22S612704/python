#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cigna for HCP - claim automation (fixed, production build)
==========================================================

Workbook : C:\\Users\\Automation22\\Desktop\\Automation\\Cigna\\Cigna.xlsx
Sheets   : Login, Input, Master
Portal   : https://cignaforhcp.cigna.com/app/login

What this build fixes
---------------------
* Radio buttons are selected and verified (the old code clicked the <i> icon
  and often stayed on the previous search type).
* Search fields are cleared between Type 1 / Type 2 / Type 3 attempts.
* Stale search results are not re-used after a new search.
* DOS is matched against Excel DOS first; if it does not match, the newest
  claims are inspected for the input CPT and Notes2 is produced from the
  claim's real DOS. A DOS-mismatched note is never written to Notes1.
* Claim selection is: DOS match -> CPT match in recent claims -> latest claim.
* Procedure / remark / explanation columns are located by header text with
  the supplied fixed column numbers as fallback.
* Master lookup uses CPT + Remark Code + explanation, not CPT alone.
* New Master rows never duplicate an existing CPT and never receive an
  automation status such as "Completed - DOS Match".
* Notes1 / Notes2 are written separately (Notes2 has two modes, see
  --notes2-mode).
* The workbook is backed up before the run and saved after every row.
* An Error column (created only when needed) keeps the failure text, so
  Notes1/Notes2 are not overwritten by an exception.
* Resume support: completed rows are skipped, failed rows are retried.

Install:
    pip install -U selenium openpyxl

Run:
    python cigna_automation_final.py --excel "C:\\path\\Cigna.xlsx"
    python cigna_automation_final.py --rows 3 --dry-run
    python cigna_automation_final.py --fresh-login
    python cigna_automation_final.py --notes2-mode marker
    python cigna_automation_final.py --status-style claim

Logs      : logs\\cigna_automation.log
Backups   : backups\\Cigna_<timestamp>.xlsx
Sessions  : %LOCALAPPDATA%\\CignaAutomation\\
Close Cigna.xlsx in Excel before starting (the bot saves after every row).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urljoin

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover - shown before the workbook is read
    raise SystemExit(
        "openpyxl is not installed. Run: pip install -U selenium openpyxl"
    ) from exc

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        ElementNotInteractableException,
        NoSuchWindowException,
        StaleElementReferenceException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError as exc:  # pragma: no cover - shown before the workbook is read
    raise SystemExit(
        "Selenium is not installed. Run: pip install -U selenium openpyxl"
    ) from exc

# ===========================================================================
# CONFIGURATION
# ===========================================================================
APP_DIR = Path(__file__).resolve().parent

DEFAULT_EXCEL_CANDIDATES = (
    Path(r"C:\Users\Automation22\Desktop\Automation\Cigna\Cigna.xlsx"),
    APP_DIR / "Cigna.xlsx",
    Path.home() / "Desktop" / "Cigna.xlsx",
    Path.home() / "Downloads" / "Cigna.xlsx",
)

LOGIN_URL = "https://cignaforhcp.cigna.com/app/login"
PORTAL_URL = "https://cignaforhcp.cigna.com/"

WAIT = 25                  # normal explicit-wait timeout (seconds)
SHORT_WAIT = 8
PAGE_LOAD_TIMEOUT = 90
ROW_RETRIES = 2
SESSION_VALID_DAYS = 30

MAX_CLAIMS = 3             # DOS matches first, then this many recent claims
MAX_PAGES_WARNING = True   # warn when the result table has a Next page

SHEET_INPUT = "Input"
SHEET_MASTER = "Master"
SHEET_LOGIN = "Login"

STATUS_NOT_FOUND = "Not Found"
STATUS_NO_CLAIMS = "No Claims"
STATUS_FAILED = "Failed"

STATUS_DOS = "Completed - DOS Match"
STATUS_CPT_RECENT = "Completed - CPT Match (Recent Claim)"
STATUS_LATEST = "Completed - Latest Claim Fallback"
STATUS_REVIEW = "Review Required - Master Mapping Missing"

NOTES2_NOTE = "note"       # full Notes2 note built with the claim's real DOS
NOTES2_MARKER = "marker"   # Notes2 = "DOS Not Matched"

# Statuses written by earlier versions that should still count as "done".
RETRY_FAILED_HINTS = ("failed", "error", "automation error")
RETRY_NOT_FOUND_HINTS = ("not found", "no claims", "cpt not found")

log = logging.getLogger("cigna")

# ===========================================================================
# XPATHS (the original workflow XPaths, with fallbacks where noted)
# ===========================================================================
X = {
    "cookie_close": '//*[@id="onetrust-close-btn-container"]/button',

    # ---- login -----------------------------------------------------------
    "login_button": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-public/"
        "div/main/cigna-page/div/div[1]/div/cigna-login-form-auth0/div/div[2]/button"
    ),
    "username_label": '//*[@id="username-label"]',
    "username_input": '//*[@id="username"]',
    "username_next": "/html/body/main/section/div/div/div/div[1]/div/form/div[4]/button",
    "password": '//*[@id="password"]',
    "password_continue": "/html/body/main/section/div/div/div/form/div[5]/button",
    "otp": '//*[@id="code"]',
    "remember_me": "/html/body/main/section/div/div/div/div/div/form/div[3]/label/span",
    "otp_continue": "/html/body/main/section/div/div/div/div/div/form/div[4]/button",

    # ---- navigation ------------------------------------------------------
    "claims_menu": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/header/cigna-primary-nav/nav/div[3]/div/cigna-nav-item/a"
    ),
    "search_claim_menu": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/header/cigna-primary-nav/nav/div[3]/cigna-nav-links/"
        "div[1]/cigna-nav-item/a"
    ),
    "search_form": "//cigna-search-form",
    "patient_form": "//cigna-search-type-patient",

    # ---- patient search ---------------------------------------------------
    "type1_tab": (
        '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
        "div/cigna-search-type-patient/form/div[1]/fieldset/div[1]/div[1]/label/i"
    ),
    "type2_tab": (
        '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
        "div/cigna-search-type-patient/form/div[1]/fieldset/div[1]/div[2]/label/i"
    ),
    "type3_tab": (
        '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
        "div/cigna-search-type-patient/form/div[1]/fieldset/div[1]/div[3]/label/i"
    ),
    "dob": '//*[@id="dob"]',
    "member_id": '//*[@id="memberId"]',
    "first_name": '//*[@id="firstName"]',
    "last_name": '//*[@id="lastName"]',
    "patient_search": (
        '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
        "div/cigna-search-type-patient/form/div[2]/fieldset/div/div[6]/button[1]"
    ),

    # ---- claim results ----------------------------------------------------
    "result_rows": "//cigna-results-table//table/tbody/tr",
    "claim_dos_cell": "./td[6]",
    "claim_link_cell": "./td[2]//a",
    "no_results_markers": (
        "no results", "no records", "no claims found", "no patients found",
        "no matching", "no data", "0 results", "0 claims", "there are no",
    ),

    # ---- claim details ----------------------------------------------------
    "claim_screen": "//cigna-claim-details-screen",
    "claim_info": "//cigna-medical-claims-information",
    "claim_reference": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[1]/table/tbody/tr[1]/td"
    ),
    "date_processed": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[1]/table/tbody/tr[6]/td"
    ),
    "claim_status": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[1]/table/tbody/tr[7]/td/cigna-claim-status/cigna-status-badge/"
        "div/div[2]"
    ),
    "amount_due": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[2]/table/tbody/tr[1]/td"
    ),
    "amount_paid": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[2]/table/tbody/tr[2]/td"
    ),
    "provider_payment": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[2]/table/tbody/tr[3]/td"
    ),
    "patient_responsibility": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/"
        "cigna-medical-claims-information/div/cigna-tab-group/div/div/div/"
        "div[2]/table/tbody/tr[4]/td"
    ),
    "procedure_rows": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/div[4]/"
        "cigna-medical-procedure-details/div/table/tbody/tr"
    ),
    "explanation_blocks": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-claims-details/div/cigna-claims-details-medical/div[5]/"
        "cigna-medical-explanation-details/div[1]/div"
    ),
    "return_claims": (
        "/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
        "div/main/div/cigna-page/cigna-claim-details-screen/"
        "cigna-breadcrumbs/div/div[1]/a"
    ),
}

# Extra locators used only when the primary XPath is not present.
X_ALT = {
    "login_button": [
        X["login_button"],
        "//button[contains(translate(normalize-space(.),'LOGIN','login'),'login')]",
        "//a[contains(translate(normalize-space(.),'LOGIN','login'),'login')]",
    ],
    "username": [X["username_input"], X["username_label"]],
    "username_next": [
        X["username_next"],
        "//form//button[@type='submit']",
        "//button[contains(translate(normalize-space(.),'NEXT','next'),'next')]",
    ],
    "password_continue": [
        X["password_continue"],
        "//form//button[@type='submit']",
        "//button[contains(translate(normalize-space(.),'CONTINUE','continue'),'continue')]",
    ],
    "otp_continue": [
        X["otp_continue"],
        "//form//button[@type='submit']",
        "//button[contains(translate(normalize-space(.),'CONTINUE','continue'),'continue')]",
    ],
    "claims_menu": [
        X["claims_menu"],
        "//cigna-primary-nav//a[contains(translate(.,'CLAIMS','claims'),'claims')]",
        "//nav//a[contains(translate(.,'CLAIMS','claims'),'claims')]",
    ],
    "search_claim_menu": [
        X["search_claim_menu"],
        "//cigna-nav-links//a[contains(translate(.,'SEARCH CLAIM','search claim'),'search claim')]",
        "//a[contains(translate(.,'SEARCH CLAIM','search claim'),'search claim')]",
    ],
    "patient_search": [
        X["patient_search"],
        "//cigna-search-type-patient//button[contains(.,'Search')]",
        "//button[contains(translate(normalize-space(.),'SEARCH','search'),'search')]",
    ],
    "return_claims": [
        X["return_claims"],
        "//cigna-breadcrumbs//a[contains(translate(.,'RETURN','return'),'return')]",
        "//cigna-breadcrumbs//a[contains(translate(.,'BACK','back'),'back')]",
        "//a[contains(translate(.,'SEARCH RESULTS','search results'),'search results')]",
    ],
}

# ===========================================================================
# SMALL HELPERS
# ===========================================================================
def clean(value: Any) -> str:
    """Collapse whitespace / non-breaking spaces; None -> ''."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def norm_header(value: Any) -> str:
    """'Invoice #' -> 'invoice'; 'Cigna ID' -> 'cignaid'."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def norm_text(value: Any) -> str:
    """Lower-case, alphanumeric-only text used for remark-code comparison."""
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def el_text(element) -> str:
    try:
        text = element.text
        if not text or not text.strip():
            text = element.get_attribute("textContent")
        return clean(text)
    except (StaleElementReferenceException, WebDriverException):
        return ""


def el_lines(element) -> list[str]:
    try:
        raw = element.get_attribute("textContent") or element.text or ""
    except (StaleElementReferenceException, WebDriverException):
        return []
    return [clean(line) for line in str(raw).splitlines() if clean(line)]


def plain(value: Any) -> str:
    """Excel value -> text (no trailing .0 on whole numbers)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    return str(value) if value is not None else ""


def as_id(value: Any) -> str:
    """Cigna member id / invoice: remove every whitespace character."""
    return re.sub(r"\s+", "", plain(value))


def as_code(value: Any) -> str:
    """CPT / remark code: upper-case, drop an Excel '.0' suffix."""
    text = clean(plain(value)).upper()
    return text[:-2] if text.endswith(".0") else text


def extract_cpt(value: Any) -> str:
    """'99213 - Office visit' -> '99213'; 'J1234' stays 'J1234'."""
    text = clean(plain(value)).upper()
    if not text:
        return ""
    match = re.search(r"\b([A-Z]\d{4}|\d{4,5}[A-Z]?)\b", text)
    if match:
        return match.group(1)
    # A short alphanumeric cell may itself be the code; a header such as
    # "Procedure Code" must not become a fake CPT.
    if re.fullmatch(r"[A-Z0-9]{1,7}", text) and re.search(r"\d", text):
        return text
    return ""


def parse_date(value: Any) -> datetime | None:
    """Parse Excel / portal dates safely. Returns a date at midnight."""
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime(1899, 12, 30) + timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None

    text = clean(value)
    if not text:
        return None

    # 8 digits: MMDDYYYY or YYYYMMDD (only when it is unambiguous).
    if re.fullmatch(r"\d{8}", text):
        for fmt in ("%m%d%Y", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass

    formats = (
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d",
        "%m-%d-%Y", "%m-%d-%y", "%d-%b-%Y", "%d %b %Y",
        "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if match:
        month, day, year = (int(part) for part in match.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day)
        except ValueError:
            return None
    return None


def fmt_date(value: Any) -> str:
    parsed = parse_date(value)
    if parsed:
        return parsed.strftime("%m/%d/%Y")
    return clean(plain(value))


def to_amount(value: Any) -> float:
    """'$1,234.50' -> 1234.5; '($50.00)' -> -50.0; blank -> 0.0."""
    text = clean(value)
    if not text:
        return 0.0
    negative = (
        (text.startswith("(") and text.endswith(")"))
        or text.startswith("-")
        or bool(re.search(r"\bCR\b", text.upper()))
    )
    match = re.search(r"\d[\d,]*\.?\d*", text)
    if not match:
        return 0.0
    try:
        amount = float(match.group(0).replace(",", ""))
    except ValueError:
        return 0.0
    return -amount if negative else amount


def money(value: Any) -> str:
    amount = to_amount(value)
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _looks_like_code(token: str) -> bool:
    token = token.strip(" .:()[]{}")
    if not token or len(token) > 9 or not re.search(r"\d", token):
        return False
    # 197, N130, M127, B7, CO-197
    if not re.fullmatch(r"(?:[A-Z]{1,2}-)?[A-Z]{0,3}\d{1,5}[A-Z]?", token):
        return False
    # A bare year is not a remark code.
    if re.fullmatch(r"(?:19|20)\d{2}", token):
        return False
    return True


def _normalise_remark_text(value: Any) -> str:
    """'197-Service included' -> '197 - Service included'."""
    text = clean(value)
    return re.sub(
        r"(?<=\d)\s*[-\u2013\u2014]\s*(?=[A-Za-z])", " - ", text
    )


def _remark_segments(value: Any) -> list[tuple[str, str]]:
    """Split '197 - one; 198 - two' into [(head, explanation), ...]."""
    segments: list[tuple[str, str]] = []
    for segment in re.split(r"[;\n]+", _normalise_remark_text(value)):
        segment = segment.strip()
        if not segment:
            continue
        parts = re.split(
            r"\s*:\s*|\s+-\s+|\s*[\u2013\u2014]\s*", segment, maxsplit=1
        )
        segments.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return segments


def _codes_in_head(head: str) -> list[str]:
    head = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " ", head)
    codes: list[str] = []
    for token in re.split(r"[,\s/|]+", head):
        token = token.strip(" .:()[]{}")
        if _looks_like_code(token) and token not in codes:
            codes.append(token)
    return codes


def split_codes(value: Any) -> list[str]:
    """Extract remark codes from '197, 198 - denied' -> ['197', '198'].

    The old implementation accepted ordinary words as codes after a missing
    separator; this one requires a digit and a code-shaped token.
    """
    out: list[str] = []
    for head, _ in _remark_segments(value):
        for code in _codes_in_head(head):
            if code not in out:
                out.append(code)
    if not out and _looks_like_code(clean(value).upper()):
        out.append(clean(value).upper())
    return out


def remark_pairs(value: Any) -> dict[str, str]:
    """{'197': 'service included', '198': 'other'} for one remark cell."""
    pairs: dict[str, str] = {}
    for head, tail in _remark_segments(value):
        explanation = norm_text(tail)
        for code in _codes_in_head(head):
            if code not in pairs or (not pairs[code] and explanation):
                pairs[code] = explanation
    return pairs


def same_date(a: Any, b: Any) -> bool:
    first, second = parse_date(a), parse_date(b)
    if first and second:
        return first == second
    return clean(plain(a)) == clean(plain(b)) and bool(clean(plain(a)))


def safe_cell(value: Any) -> Any:
    """Make a value safe for openpyxl (no control chars / formula injection)."""
    if value is None:
        return None
    if isinstance(value, (int, float, datetime, date)):
        return value
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    text = text[:32760]
    if text.startswith("="):
        text = "'" + text
    return text or None


KNOWN_LABELS = (
    "claim/reference number", "claim/reference", "claim number",
    "date processed", "claim status", "claim amount due", "claim amount paid",
    "total provider payment", "patient responsibility", "check number",
    "check/eft number", "procedure code", "date of service", "remark code",
)


def strip_label(value: Any) -> str:
    """Remove a leading 'Label:' / 'Label' from a scraped cell."""
    text = clean(value)
    if not text:
        return ""
    if ":" in text:
        left, right = text.split(":", 1)
        if len(left) <= 60 and clean(right):
            return clean(right)
    low = text.lower()
    for label in sorted(KNOWN_LABELS, key=len, reverse=True):
        if low.startswith(label):
            return clean(text[len(label):].lstrip(" :-#"))
    return text

# ===========================================================================
# EXCEL HEADER ALIASES
# ===========================================================================
INPUT_ALIASES = {
    "invoice": {"invoice", "invoiceno", "invoicenumber", "invno", "invoicenum"},
    "patient_name": {"patientname", "membername", "fullname", "name"},
    "first": {"patientfirstname", "firstname", "first", "givenname"},
    "last": {"patientlastname", "lastname", "last", "surname", "familyname"},
    "dob": {"dob", "dateofbirth", "birthdate", "birthday"},
    "insurance": {"insurancename", "insurance", "payer", "plan"},
    "cigna_id": {"cignaid", "cignapatientid", "memberid", "patientid",
                 "membernumber", "id"},
    "dos": {"dos", "dateofservice", "dateofservices", "servicedate",
            "dosfrom", "dateofservicedos"},
    "target_cpt": {"cptcode", "cptcodes", "inputcptcode", "requestedcpt",
                   "cptcodeinput", "cptprocedurecode", "procedurecodeinput"},
    "billed": {"billedamount", "billed", "chargeamount", "amountbilled"},
    "notes1": {"notes1", "note1"},
    "notes2": {"notes2", "note2"},
    "cpt": {"cpt", "procedurecode", "proccode", "claimcpt", "cptoutput",
            "cptprocedurecode", "outputcpt"},
    "remark_codes": {"remarkcodes", "remarkcode", "remarks", "adjustmentcodes",
                     "explanationofremarkcodes"},
    "status": {"status", "claimstatus", "result", "automationstatus"},
    "denied_cpt": {"deniedcpt", "deniedcpts", "denied"},
    "error": {"error", "errormessage", "automationerror"},
}
MASTER_ALIASES = {
    "cpt": {"cpt", "cptcode", "procedurecode", "code", "proccode"},
    "remark": {"remarkcodes", "remarkcode", "remarks", "remarkreason",
               "reason", "description", "explanation"},
    "status": {"status", "claimstatus", "result"},
}
LOGIN_ALIASES = {
    "username": {"username", "user", "userid", "login", "email"},
    "password": {"password", "pass", "pwd"},
}
OUTPUT_COLUMNS = {
    "notes1": "Notes1",
    "notes2": "Notes2",
    "cpt": "CPT",
    "remark_codes": "Remark Codes",
    "status": "Status",
}

# ===========================================================================
# DATA MODEL
# ===========================================================================
@dataclass
class Result:
    index: int
    dos: str = ""
    claim_no: str = ""


@dataclass
class Procedure:
    cpt: str = ""
    dos: str = ""
    codes: list[str] = field(default_factory=list)
    raw_remark: str = ""


@dataclass
class Claim:
    claim_no: str = ""
    date_processed: str = ""
    status: str = ""
    amount_due: str = ""
    amount_paid: str = ""
    provider_payment: str = ""
    patient_resp: str = ""
    check_no: str = ""
    procs: list[Procedure] = field(default_factory=list)
    expl: dict[str, str] = field(default_factory=dict)
    result_dos: str = ""
    source_index: int = 0

    def cpt_set(self) -> set[str]:
        return {as_code(p.cpt) for p in self.procs if p.cpt}

    def is_denied(self) -> bool:
        status = clean(self.status).lower()
        if "denied" in status or "denial" in status:
            return True
        raw = [self.amount_due, self.amount_paid,
               self.provider_payment, self.patient_resp]
        if any(clean(x) for x in raw):
            return all(abs(to_amount(x)) < 0.005 for x in raw)
        return False


@dataclass
class MasterEntry:
    row: int
    cpt: str
    remark: str
    status: str

# ===========================================================================
# WORKBOOK
# ===========================================================================
class Book:
    def __init__(self, path: Path, dry_run: bool = False,
                 sheet_input: str = SHEET_INPUT):
        self.path = Path(path)
        self.dry_run = dry_run
        keep_vba = self.path.suffix.lower() == ".xlsm"
        self.wb = load_workbook(str(self.path), keep_vba=keep_vba)
        self.ws_in = self._sheet(sheet_input)
        self.ws_login = self._sheet(SHEET_LOGIN)
        self.ws_master = self._sheet_or_create_master()

        self.in_header = self._detect_header(self.ws_in, INPUT_ALIASES)
        self.cols = self._map_columns(self.ws_in, self.in_header, INPUT_ALIASES)
        for logical, title in OUTPUT_COLUMNS.items():
            self._ensure_column(self.ws_in, self.cols, logical, title,
                                self.in_header)

        self.master_header = self._detect_header(self.ws_master, MASTER_ALIASES)
        self.mcols = self._map_columns(
            self.ws_master, self.master_header, MASTER_ALIASES
        )
        for logical, title in (("cpt", "CPT"), ("remark", "Remark Codes"),
                               ("status", "Status")):
            self._ensure_column(self.ws_master, self.mcols, logical, title,
                                self.master_header)

        self.master_rows: list[MasterEntry] = []
        self.master_by_cpt: dict[str, list[MasterEntry]] = defaultdict(list)
        self._load_master()

        if "target_cpt" not in self.cols:
            found = [self.ws_in.cell(self.in_header, c).value
                     for c in range(1, self.ws_in.max_column + 1)
                     if self.ws_in.cell(self.in_header, c).value]
            raise ValueError(
                "Input sheet has no 'CPT Code' (input) column. Add a header "
                "named 'CPT Code'; the 'CPT' column is reserved for the "
                f"result. Headers found: {found}"
            )
        if not any(k in self.cols for k in ("first", "last", "patient_name")):
            raise ValueError("Input sheet has no patient name column.")
        if not any(k in self.cols for k in ("dob", "cigna_id")):
            raise ValueError("Input sheet has no DOB / Cigna ID column.")

    # -------------------------------------------------------------- workbook
    def _sheet(self, name: str):
        for sheet in self.wb.sheetnames:
            if sheet.strip().lower() == name.strip().lower():
                return self.wb[sheet]
        raise KeyError(
            f"Sheet '{name}' not found. Available: {self.wb.sheetnames}"
        )

    def _sheet_or_create_master(self):
        try:
            return self._sheet(SHEET_MASTER)
        except KeyError:
            ws = self.wb.create_sheet(SHEET_MASTER)
            for col, title in enumerate(("CPT", "Remark Codes", "Status"), 1):
                ws.cell(1, col).value = title
            log.info("Master sheet was missing - created with CPT/Remark/Status.")
            return ws

    @staticmethod
    def _header_keys(value: Any) -> set[str]:
        """Normalized header names; a parenthetical description is ignored.

        'Remark Codes(Remark Explanation)' -> {'remarkcodes'}.
        """
        text = str(value or "")
        keys = {norm_header(text)}
        stripped = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
        keys.add(norm_header(stripped))
        return {key for key in keys if key}

    @classmethod
    def _detect_header(cls, ws, aliases: dict[str, set[str]]) -> int:
        best_row, best_score = 1, -1
        last = min(max(ws.max_row, 1), 20)
        for row in range(1, last + 1):
            values: set[str] = set()
            for col in range(1, ws.max_column + 1):
                values |= cls._header_keys(ws.cell(row, col).value)
            score = sum(1 for names in aliases.values() if values & names)
            if score > best_score:
                best_row, best_score = row, score
        return best_row

    @classmethod
    def _map_columns(cls, ws, header_row: int,
                     aliases: dict[str, set[str]]) -> dict[str, int]:
        mapping: dict[str, int] = {}
        headers: dict[int, set[str]] = {}
        for col in range(1, ws.max_column + 1):
            keys = cls._header_keys(ws.cell(header_row, col).value)
            if keys:
                headers[col] = keys

        # Exact / parenthetical-stripped match first.
        for col, keys in headers.items():
            for logical, names in aliases.items():
                if logical in mapping:
                    continue
                if keys & names:
                    mapping[logical] = col
                    break

        # Then a long-prefix match: 'Remark Codes / Explanation'.
        used = set(mapping.values())
        for col, keys in headers.items():
            if col in used:
                continue
            for logical, names in aliases.items():
                if logical in mapping:
                    continue
                if any(
                    len(name) >= 6
                    and any(key.startswith(name) for key in keys)
                    for name in names
                ):
                    mapping[logical] = col
                    used.add(col)
                    break
        return mapping

    @staticmethod
    def _ensure_column(ws, mapping: dict[str, int], logical: str,
                       title: str, header_row: int) -> None:
        if logical in mapping:
            return
        col = max(ws.max_column, 1) + 1
        ws.cell(header_row, col).value = title
        mapping[logical] = col

    @staticmethod
    def _cell(ws, row: int, col: int | None):
        return ws.cell(row, col).value if col else None

    def val(self, row: int, logical: str):
        return self._cell(self.ws_in, row, self.cols.get(logical))

    # --------------------------------------------------------------- master
    def _load_master(self) -> None:
        self.master_rows.clear()
        self.master_by_cpt.clear()
        for row in range(self.master_header + 1,
                         self.ws_master.max_row + 1):
            cpt = as_code(self._cell(
                self.ws_master, row, self.mcols.get("cpt")))
            if not cpt:
                continue
            entry = MasterEntry(
                row=row,
                cpt=cpt,
                remark=clean(self._cell(
                    self.ws_master, row, self.mcols.get("remark"))),
                status=clean(self._cell(
                    self.ws_master, row, self.mcols.get("status"))),
            )
            self.master_rows.append(entry)
            self.master_by_cpt[cpt].append(entry)

    @staticmethod
    def _remark_matches(entry_remark: str, claim_remark: str) -> bool:
        """Exact CPT + Remark Code + Explanation comparison."""
        if not clean(entry_remark):
            # A CPT-only Master row applies to every remark for that CPT.
            return True
        entry_pairs = remark_pairs(entry_remark)
        claim_pairs = remark_pairs(claim_remark)
        if entry_pairs and claim_pairs:
            if set(entry_pairs) != set(claim_pairs):
                return False
            for code, entry_expl in entry_pairs.items():
                claim_expl = claim_pairs[code]
                # A missing explanation on either side is still a code match.
                if entry_expl and claim_expl and entry_expl != claim_expl:
                    return False
            return True
        return norm_text(entry_remark) == norm_text(claim_remark)

    def master_lookup(self, cpt: Any, remark_text: Any = "") -> tuple[str, bool]:
        """Return (Master Status, exact_match). Exact = CPT + remark."""
        code = as_code(cpt)
        entries = self.master_by_cpt.get(code, [])
        if not entries:
            return "", False

        # Prefer a row whose remark/explanation actually matches.
        for entry in entries:
            if self._remark_matches(entry.remark, clean(remark_text)):
                return entry.status, True

        # CPT exists, but the remark differs: use it only as a warning fallback.
        for entry in entries:
            if entry.status:
                return entry.status, False
        return "", False

    def _next_master_row(self) -> int:
        last = max(self.ws_master.max_row, self.master_header)
        for row in range(self.master_header + 1, last + 1):
            if all(not clean(self.ws_master.cell(row, col).value)
                   for col in range(1, self.ws_master.max_column + 1)):
                return row
        return last + 1

    def add_master(self, claim: Claim) -> list[str]:
        """Add CPTs that are not in Master yet. Never duplicate an existing CPT."""
        if self.dry_run:
            return []
        added: list[str] = []
        status = "DND" if claim.is_denied() else "PAID"
        for proc in claim.procs:
            cpt = as_code(proc.cpt)
            if not cpt or cpt in self.master_by_cpt:
                continue
            remark = procedure_remark(claim, proc)
            target_row = self._next_master_row()
            self.ws_master.cell(target_row, self.mcols["cpt"]).value = cpt
            self.ws_master.cell(target_row, self.mcols["remark"]).value = (
                safe_cell(remark) if remark else None
            )
            self.ws_master.cell(target_row, self.mcols["status"]).value = status
            entry = MasterEntry(target_row, cpt, remark, status)
            self.master_rows.append(entry)
            self.master_by_cpt[cpt].append(entry)
            added.append(cpt)
        return added

    # ------------------------------------------------------------ credentials
    def credentials(self) -> tuple[str, str]:
        header = self._detect_header(self.ws_login, LOGIN_ALIASES)
        mapping = self._map_columns(self.ws_login, header, LOGIN_ALIASES)
        if "username" not in mapping or "password" not in mapping:
            raise ValueError(
                "Login sheet needs Username and Password columns. Found: "
                f"{[c.value for c in self.ws_login[header] if c.value]}"
            )
        for row in range(header + 1, self.ws_login.max_row + 1):
            username = self.ws_login.cell(row, mapping["username"]).value
            password = self.ws_login.cell(row, mapping["password"]).value
            if username not in (None, "") and password not in (None, ""):
                # Preserve internal characters; only trim accidental edge spaces.
                return (plain(username).strip(), plain(password).strip())
        raise ValueError("Login sheet: no row contains both Username and Password.")

    # ---------------------------------------------------------------- patients
    def patient(self, row: int) -> dict[str, str]:
        first = clean(self.val(row, "first"))
        last = clean(self.val(row, "last"))
        full = clean(self.val(row, "patient_name"))
        target_raw = self.val(row, "target_cpt")

        if full and (not first or not last):
            if "," in full:
                # The actual Input sheet uses "Last, First".
                part_a, part_b = (clean(x) for x in full.split(",", 1))
                last = last or part_a
                first = first or part_b
            else:
                pieces = full.split()
                if len(pieces) == 1:
                    first = first or pieces[0]
                else:
                    first = first or " ".join(pieces[:-1])
                    last = last or pieces[-1]

        return {
            "invoice": clean(plain(self.val(row, "invoice"))),
            "first": first,
            "last": last,
            "full_name": full,
            "dob": fmt_date(self.val(row, "dob")),
            "insurance": clean(self.val(row, "insurance")),
            "cigna_id": as_id(self.val(row, "cigna_id")),
            "dos": fmt_date(self.val(row, "dos")),
            "target_cpt": extract_cpt(target_raw) or as_code(target_raw),
            "billed": clean(plain(self.val(row, "billed"))),
            "status": clean(self.val(row, "status")),
        }

    def pending_rows(self, retry_failed: bool = True,
                     retry_not_found: bool = False,
                     limit: int = 0,
                     start_row: int = 0) -> list[tuple[int, dict[str, str]]]:
        pending: list[tuple[int, dict[str, str]]] = []
        first = max(self.in_header + 1, start_row or 0)
        for row in range(first, self.ws_in.max_row + 1):
            patient = self.patient(row)
            data_values = [
                value for key, value in patient.items()
                if key not in ("status", "full_name")
            ]
            if not any(clean(value) for value in data_values):
                continue

            status = patient["status"]
            if status:
                low = status.lower()
                if any(hint in low for hint in RETRY_FAILED_HINTS):
                    if not retry_failed:
                        continue
                elif any(hint in low for hint in RETRY_NOT_FOUND_HINTS):
                    if not retry_not_found:
                        continue
                else:
                    continue

            has_name = bool(patient["first"] and patient["last"]) or bool(
                patient["full_name"])
            has_key = bool(patient["dob"] or patient["cigna_id"])
            if not has_name and not has_key:
                log.warning("Row %d skipped: no usable patient data.", row)
                continue
            pending.append((row, patient))
            if limit and len(pending) >= limit:
                break
        return pending

    # ------------------------------------------------------------------ write
    def _error_column(self) -> int | None:
        col = self.cols.get("error")
        if col:
            return col
        self._ensure_column(self.ws_in, self.cols, "error", "Error",
                            self.in_header)
        return self.cols.get("error")

    def write_result(self, row: int, result: dict[str, str]) -> None:
        for logical, value in result.items():
            if logical == "error":
                continue
            col = self.cols.get(logical)
            if not col:
                continue
            self.ws_in.cell(row, col).value = safe_cell(value)

        if result.get("error"):
            col = self._error_column()
            if col:
                self.ws_in.cell(row, col).value = safe_cell(result["error"])
        else:
            col = self.cols.get("error")
            if col:
                self.ws_in.cell(row, col).value = None

    def save(self) -> None:
        if self.dry_run:
            log.info("[dry-run] workbook not saved.")
            return
        # Write to a temporary file first; if Excel has the workbook open the
        # replace fails cleanly and the original file is left intact.
        tmp = self.path.with_name(
            f"{self.path.stem}.tmp{self.path.suffix}"
        )
        for attempt in range(6):
            try:
                self.wb.save(str(tmp))
                os.replace(str(tmp), str(self.path))
                return
            except PermissionError:
                log.warning(
                    "Cigna.xlsx is open in Excel (or locked). Close it - "
                    "retrying in %ds...", 3 + attempt * 2
                )
                time.sleep(3 + attempt * 2)
            except OSError as exc:
                log.warning("Workbook save failed (%s) - retrying.", exc)
                time.sleep(3)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
        raise PermissionError(
            f"Cannot save workbook - it is locked: {self.path}"
        )

# ===========================================================================
# BROWSER
# ===========================================================================
class Bot:
    def __init__(self, session_file: Path, profile_dir: Path,
                 screenshot_dir: Path, headless: bool = False,
                 debug: bool = False):
        self.session_file = Path(session_file)
        self.profile_dir = Path(profile_dir)
        self.screenshot_dir = Path(screenshot_dir)
        self.debug = debug
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={self.profile_dir}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "--disable-features=PasswordLeakDetection,AutofillServerCommunication"
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.password_manager_leak_detection": False,
        })
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")

        try:
            self.d = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise RuntimeError(
                "Could not start Chrome. Close every Chrome window that uses "
                f"'{self.profile_dir}' and try again.\n{exc}"
            ) from exc

        self.d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.d.implicitly_wait(0)
        try:
            self.d.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined});"},
            )
        except WebDriverException:
            pass

    # ------------------------------------------------------------------ base
    def go(self, url: str) -> None:
        try:
            self.d.get(url)
        except TimeoutException:
            try:
                self.d.execute_script("window.stop();")
            except WebDriverException:
                pass
        self.wait_ready()

    def wait_ready(self, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.d.execute_script("return document.readyState") == "complete":
                    return
            except WebDriverException:
                return
            time.sleep(0.2)

    def wait_idle(self, timeout: float = 10) -> None:
        """Wait for common Angular loading overlays to disappear."""
        selectors = (
            "//cigna-loading",
            "//*[@aria-busy='true']",
            "//*[contains(@class,'loading-overlay')]",
            "//*[contains(@class,'spinner')]",
        )
        deadline = time.time() + max(0.5, timeout)
        while time.time() < deadline:
            busy = False
            for xpath in selectors:
                try:
                    for element in self.d.find_elements(By.XPATH, xpath):
                        if element.is_displayed():
                            busy = True
                            break
                except (StaleElementReferenceException, WebDriverException):
                    continue
                if busy:
                    break
            if not busy:
                return
            time.sleep(0.3)

    def _best_element(self, xpath: str, timeout: float = WAIT,
                      require_visible: bool = False):
        """Find the first visible match (falls back to the first match)."""
        deadline = time.time() + max(0.1, timeout)
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                elements = self.d.find_elements(By.XPATH, xpath)
            except (StaleElementReferenceException, WebDriverException) as exc:
                last_error = exc
                elements = []
            if elements:
                if not require_visible:
                    for element in elements:
                        try:
                            if element.is_displayed():
                                return element
                        except (StaleElementReferenceException, WebDriverException):
                            continue
                else:
                    for element in elements:
                        try:
                            if element.is_displayed() and element.is_enabled():
                                return element
                        except (StaleElementReferenceException, WebDriverException):
                            continue
                if not require_visible:
                    return elements[0]
            time.sleep(0.15)
        raise TimeoutException(f"Element not found: {xpath} ({last_error})")

    def find(self, xpath: str, timeout: float = WAIT):
        return self._best_element(xpath, timeout)

    def present(self, xpath: str, timeout: float = 0.5) -> bool:
        try:
            self._best_element(xpath, timeout)
            return True
        except (TimeoutException, WebDriverException):
            return False

    def displayed(self, xpath: str, timeout: float = 0.5) -> bool:
        try:
            self._best_element(xpath, timeout, require_visible=True)
            return True
        except (TimeoutException, WebDriverException):
            return False

    def click(self, xpath: str, timeout: float = WAIT):
        """Click with scroll, stale handling and a JavaScript fallback."""
        deadline = time.time() + max(1, timeout)
        last: Exception | None = None
        self.close_cookie_banner()
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            try:
                element = self._best_element(xpath, min(3, remaining),
                                             require_visible=True)
            except TimeoutException:
                try:
                    element = self._best_element(xpath, min(2, remaining))
                except TimeoutException as exc:
                    last = exc
                    time.sleep(0.3)
                    continue
            try:
                self.d.execute_script(
                    "arguments[0].scrollIntoView({block:'center',"
                    "inline:'center'});", element,
                )
            except WebDriverException:
                pass
            try:
                element.click()
                return element
            except (ElementClickInterceptedException,
                    ElementNotInteractableException,
                    StaleElementReferenceException, WebDriverException) as exc:
                last = exc
                try:
                    self.d.execute_script("arguments[0].click();", element)
                    return element
                except WebDriverException as exc2:
                    last = exc2
            time.sleep(0.3)
        raise TimeoutException(f"Could not click element: {xpath} ({last})")

    def click_first(self, xpaths: Sequence[str], timeout: float = WAIT) -> bool:
        for xpath in xpaths:
            try:
                self.click(xpath, min(6, max(1, timeout)))
                return True
            except (TimeoutException, WebDriverException):
                continue
        return False

    def _input_for(self, element):
        tag = (getattr(element, "tag_name", "") or "").lower()
        if tag in ("input", "textarea"):
            return element
        for expr in (".//input[not(@type='hidden')]|.//textarea",
                     "./ancestor::label[1]//input[not(@type='hidden')]",
                     "./ancestor::*[self::label or self::div][1]//input[not(@type='hidden')]",
                     "./following::input[not(@type='hidden')][1]"):
            try:
                found = element.find_elements(By.XPATH, expr)
            except (StaleElementReferenceException, WebDriverException):
                found = []
            if found:
                return found[0]
        field_id = None
        try:
            field_id = element.get_attribute("for")
        except WebDriverException:
            pass
        if field_id:
            try:
                return self.d.find_element(By.ID, field_id)
            except WebDriverException:
                pass
        return element

    def _js_set(self, element, text: str) -> None:
        self.d.execute_script("""
            const el = arguments[0], val = arguments[1];
            el.focus();
            const proto = el.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        """, element, text)

    def set_text(self, xpath: str, value: Any, timeout: float = WAIT) -> None:
        """Type into an input; also accepts a <label> / <i> XPath."""
        text = "" if value is None else str(value)
        element = self._input_for(self._best_element(xpath, timeout))
        try:
            if (element.get_attribute("type") or "").lower() == "date":
                parsed = parse_date(text)
                if parsed:
                    text = parsed.strftime("%Y-%m-%d")
        except WebDriverException:
            pass

        try:
            element.click()
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(Keys.DELETE)
            if text:
                element.send_keys(text)
        except (ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException):
            pass

        try:
            current = element.get_attribute("value") or ""
        except WebDriverException:
            current = ""
        if clean(current) != clean(text):
            try:
                self._js_set(element, text)
            except WebDriverException:
                try:
                    ActionChains(self.d).send_keys(text).perform()
                except WebDriverException:
                    pass

    def clear_text(self, xpath: str, timeout: float = 3) -> None:
        try:
            element = self._input_for(self._best_element(xpath, timeout))
            try:
                element.click()
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.DELETE)
            except (ElementNotInteractableException,
                    StaleElementReferenceException, WebDriverException):
                pass
            try:
                if clean(element.get_attribute("value") or ""):
                    self._js_set(element, "")
            except WebDriverException:
                pass
        except (TimeoutException, WebDriverException):
            pass

    def text(self, xpath: str, timeout: float = SHORT_WAIT) -> str:
        try:
            return el_text(self._best_element(xpath, timeout))
        except (TimeoutException, StaleElementReferenceException,
                WebDriverException):
            return ""

    def text_if_label(self, xpath: str, keywords: Sequence[str],
                      exclude: Sequence[str] = (),
                      timeout: float = 5) -> str:
        """Read a fixed-XPath cell only when its row label matches."""
        try:
            element = self._best_element(xpath, timeout)
        except (TimeoutException, WebDriverException):
            return ""
        label = ""
        try:
            row = element.find_element(By.XPATH, "./ancestor::tr[1]")
            cells = row.find_elements(By.XPATH, "./th|./td")
            if cells:
                label = el_text(cells[0]).lower()
        except (StaleElementReferenceException, WebDriverException):
            pass
        if keywords and label:
            if not all(word in label for word in keywords):
                return ""
        if exclude and any(word in label for word in exclude):
            return ""
        return strip_label(el_text(element))

    def info_value(self, *keywords: str,
                   exclude: Sequence[str] = ()) -> str:
        """Read a value by its row label in the claim information tables."""
        try:
            rows = self.d.find_elements(
                By.XPATH,
                "//cigna-medical-claims-information//table/tbody/tr",
            )
        except WebDriverException:
            return ""
        for row in rows:
            try:
                cells = row.find_elements(By.XPATH, "./th|./td")
                if not cells:
                    continue
                label = el_text(cells[0]).lower()
                if len(cells) == 1:
                    raw = el_text(cells[0])
                    if ":" not in raw:
                        continue
                    left, right = raw.split(":", 1)
                    if (all(word in left.lower() for word in keywords)
                            and not any(word in left.lower() for word in exclude)
                            and clean(right)):
                        return clean(right)
                    continue
                if not all(word in label for word in keywords):
                    continue
                if any(word in label for word in exclude):
                    continue
                value = el_text(cells[-1])
                if not value:
                    value = el_text(cells[0])
                return strip_label(value)
            except (StaleElementReferenceException, WebDriverException):
                continue
        return ""

    def screenshot(self, name: str) -> None:
        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.d.save_screenshot(
                str(self.screenshot_dir / f"{stamp}_{safe}.png")
            )
        except (WebDriverException, OSError):
            pass

    def dump_html(self, name: str) -> None:
        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (self.screenshot_dir / f"{stamp}_{safe}.html").write_text(
                self.d.page_source, encoding="utf-8"
            )
        except (WebDriverException, OSError):
            pass

    def close_cookie_banner(self) -> None:
        try:
            for element in self.d.find_elements(By.XPATH, X["cookie_close"]):
                if element.is_displayed():
                    self.d.execute_script("arguments[0].click();", element)
                    time.sleep(0.2)
        except WebDriverException:
            return

    # --------------------------------------------------------------- session
    def save_session(self) -> None:
        try:
            self.d.execute_cdp_cmd("Network.enable", {})
            cookies = self.d.execute_cdp_cmd(
                "Network.getAllCookies", {}
            ).get("cookies", [])
            try:
                storage = self.d.execute_script(
                    "return Object.assign({}, window.localStorage);"
                ) or {}
            except WebDriverException:
                storage = {}
            payload = {
                "saved_at": time.time(),
                "cookies": cookies,
                "local_storage": storage,
                "url": self.d.current_url,
            }
            self.session_file.write_text(
                json.dumps(payload, indent=1), encoding="utf-8"
            )
            log.info("Session saved (%d cookies).", len(cookies))
        except (WebDriverException, OSError) as exc:
            log.warning("Could not save session: %s", exc)

    def load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Session file is unreadable (%s) - ignored.", exc)
            return False

        saved_at = payload.get("saved_at") or 0
        try:
            saved_ts = float(saved_at)
        except (TypeError, ValueError):
            try:
                saved_ts = datetime.fromisoformat(str(saved_at)).timestamp()
            except ValueError:
                saved_ts = 0
        if saved_ts and time.time() - saved_ts > SESSION_VALID_DAYS * 86400:
            log.info("Saved session is older than %d days - ignored.",
                     SESSION_VALID_DAYS)
            return False

        self.go(PORTAL_URL)
        self.close_cookie_banner()
        try:
            self.d.execute_cdp_cmd("Network.enable", {})
            allowed = {"name", "value", "domain", "path", "secure",
                       "httpOnly", "sameSite", "expires", "priority",
                       "sameParty", "sourceScheme", "sourcePort"}
            now = time.time()
            params = []
            for cookie in payload.get("cookies", []):
                expires = cookie.get("expires", -1)
                if expires and expires > 0 and expires < now:
                    continue
                item = {k: v for k, v in cookie.items() if k in allowed}
                if item.get("expires", -1) in (-1, 0, None):
                    item.pop("expires", None)
                if item.get("name"):
                    params.append(item)
            if params:
                try:
                    self.d.execute_cdp_cmd(
                        "Network.setCookies", {"cookies": params}
                    )
                except WebDriverException:
                    for item in params:
                        try:
                            self.d.execute_cdp_cmd(
                                "Network.setCookies", {"cookies": [item]}
                            )
                        except WebDriverException:
                            pass
            storage = payload.get("local_storage") or {}
            if storage:
                try:
                    self.d.execute_script("""
                        const data = arguments[0];
                        for (const k in data) {
                            try { localStorage.setItem(k, data[k]); } catch (e) {}
                        }
                    """, storage)
                except WebDriverException:
                    pass
            self.go(PORTAL_URL)
            log.info("Loaded saved session (%d cookies).", len(params))
            return True
        except (WebDriverException, ValueError) as exc:
            log.warning("Could not restore session: %s", exc)
            return False

    # ----------------------------------------------------------------- login
    def detect_state(self) -> str:
        if self.displayed(X["search_form"], 0.3) or self.present(
                "//cigna-layout-secure//cigna-primary-nav", 0.3) or self.present(
                X["claims_menu"], 0.3):
            return "logged_in"
        if self.displayed(X["otp"], 0.3):
            return "otp"
        if self.displayed(X["password"], 0.3):
            return "password"
        if self.displayed(X["username_input"], 0.3) or self.displayed(
                X["username_label"], 0.3):
            return "username"
        if self.present(X["login_button"], 0.4):
            return "login_btn"
        return "unknown"

    def is_logged_in(self) -> bool:
        return self.detect_state() == "logged_in"

    def wait_state_change(self, old_state: str, timeout: float = 10) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.detect_state()
            if state != old_state:
                return state
            time.sleep(0.4)
        return old_state

    def _login_error(self) -> str:
        try:
            elements = self.d.find_elements(
                By.XPATH,
                "//*[@role='alert' or contains(@class,'error-msg')"
                " or contains(@class,'error-message')"
                " or contains(@class,'ulp-field-error')"
                " or contains(@class,'auth0-lock-error')]",
            )
        except WebDriverException:
            return ""
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                text = clean(el_text(element))
                low = text.lower()
                if text and any(word in low for word in (
                        "invalid", "incorrect", "wrong", "error", "required",
                        "try again", "too many")):
                    return text[:300]
            except (StaleElementReferenceException, WebDriverException):
                continue
        return ""

    def _prompt_otp(self, attempt: int) -> str:
        print("\n" + "=" * 62)
        print(f" OTP REQUIRED - enter the authenticator code (try {attempt}/3).")
        print("=" * 62)
        try:
            return re.sub(r"\D", "", input("OTP: "))
        except EOFError as exc:
            raise RuntimeError(
                "OTP input is unavailable. Run the script from CMD/PowerShell "
                "so the OTP can be typed."
            ) from exc

    def login(self, username: str, password: str,
              overall_timeout: float = 360) -> None:
        self.go(LOGIN_URL)
        self.close_cookie_banner()
        deadline = time.time() + overall_timeout
        last_state: str | None = None
        state_since = time.time()
        last_action: dict[str, float] = {}
        otp_attempts = 0

        while time.time() < deadline:
            self.close_cookie_banner()
            state = self.detect_state()
            if state != last_state:
                log.info("Login state: %s", state)
                last_state = state
                state_since = time.time()

            if state == "logged_in":
                log.info("Logged in successfully.")
                self.save_session()
                return

            error = self._login_error()
            if error and state in ("username", "password", "otp"):
                log.warning("Portal login message: %s", error)
                if time.time() - state_since > 8:
                    raise RuntimeError(f"Login failed: {error}")

            if time.time() - state_since > 75:
                raise TimeoutException(
                    f"Login is stuck on state '{state}' (75s)."
                )

            action_gap = 4.0
            now = time.time()
            if state == "login_btn":
                if now - last_action.get(state, 0) >= action_gap:
                    last_action[state] = now
                    self.click_first(X_ALT["login_button"], 12)
                    self.wait_state_change(state, 15)

            elif state == "username":
                if now - last_action.get(state, 0) >= action_gap:
                    last_action[state] = now
                    target = (X["username_input"] if self.displayed(
                        X["username_input"], 0.5) else X["username_label"])
                    self.set_text(target, username)
                    if not self.click_first(X_ALT["username_next"], 15):
                        raise TimeoutException("Username 'Next' button not found")
                    self.wait_state_change(state, 15)

            elif state == "password":
                if now - last_action.get(state, 0) >= action_gap:
                    last_action[state] = now
                    self.set_text(X["password"], password)
                    self.tick_remember_me()
                    if not self.click_first(X_ALT["password_continue"], 15):
                        raise TimeoutException("Password 'Continue' button not found")
                    self.wait_state_change(state, 20)

            elif state == "otp":
                if now - last_action.get(state, 0) >= action_gap:
                    otp_attempts += 1
                    if otp_attempts > 3:
                        raise RuntimeError("OTP failed three times - stopping.")
                    last_action[state] = now
                    otp = ""
                    while not otp:
                        otp = self._prompt_otp(otp_attempts)
                        if not otp:
                            print("OTP cannot be empty.")
                    deadline = max(deadline, time.time() + 120)
                    self.set_text(X["otp"], otp)
                    self.tick_remember_me()
                    if not self.click_first(X_ALT["otp_continue"], 15):
                        raise TimeoutException("OTP 'Continue' button not found")
                    self.wait_state_change(state, 20)

            else:
                time.sleep(1)

        raise TimeoutException("Login did not complete within the time limit.")

    def tick_remember_me(self) -> None:
        try:
            boxes = self.d.find_elements(
                By.XPATH,
                X["remember_me"] + "/ancestor::label//input[@type='checkbox']",
            )
            if boxes:
                if not boxes[0].is_selected():
                    self.d.execute_script("arguments[0].click();", boxes[0])
            elif self.present(X["remember_me"], 1):
                element = self.find(X["remember_me"], 1)
                self.d.execute_script("arguments[0].click();", element)
        except (TimeoutException, WebDriverException,
                StaleElementReferenceException):
            pass

    def session_is_valid(self, timeout: float = 45) -> bool:
        self.go(LOGIN_URL)
        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            self.close_cookie_banner()
            state = self.detect_state()
            if state == "logged_in":
                return True
            if state == "login_btn" and not clicked:
                self.click_first(X_ALT["login_button"], 12)
                clicked = True
            elif state in ("username", "password", "otp"):
                return False
            time.sleep(1)
        return False

    def ensure_logged_in(self, username: str, password: str,
                         fresh: bool = False) -> None:
        if fresh:
            try:
                self.go(PORTAL_URL)
                self.d.delete_all_cookies()
            except WebDriverException:
                pass
            if self.session_file.exists():
                try:
                    self.session_file.unlink()
                except OSError:
                    pass
            log.info("Fresh login requested - starting full login.")
            self.login(username, password)
            return
        if self.is_logged_in():
            return
        if self.session_is_valid():
            log.info("Saved browser session is still valid - credentials not needed.")
            self.save_session()
            return
        if self.session_file.exists():
            if self.load_session() and self.is_logged_in():
                log.info("Saved session file restored - credentials not needed.")
                self.save_session()
                return
        log.info("Session missing/expired - starting full login.")
        self.login(username, password)

    # --------------------------------------------------------- patient search
    def open_search(self) -> None:
        self.close_cookie_banner()
        if self.displayed(X["search_form"], 1):
            return
        if not self.click_first(X_ALT["claims_menu"], WAIT):
            raise TimeoutException(
                "Claims menu item not found - is the session logged in?"
            )
        self.wait_idle(5)
        if self.displayed(X["search_form"], 2):
            return
        try:
            ActionChains(self.d).move_to_element(
                self.find(X["claims_menu"], 5)
            ).perform()
            time.sleep(0.5)
        except (TimeoutException, WebDriverException):
            pass
        if not self.click_first(X_ALT["search_claim_menu"], 12):
            self.click_first(X_ALT["claims_menu"], 8)
        if not self.displayed(X["search_form"], 20):
            raise TimeoutException("Search Claim page did not open.")

    def _search_type_spec(self, kind: str) -> tuple[str, tuple[str, ...]]:
        specs = {
            "id_dob": (X["type1_tab"], ("cigna", "birth")),
            "name_dob": (X["type2_tab"], ("name", "birth")),
            "name_id": (X["type3_tab"], ("name", "cigna")),
        }
        return specs[kind]

    @staticmethod
    def _kind_field(kind: str) -> str:
        return X["member_id"] if kind in ("id_dob", "name_id") else X["first_name"]

    def select_search_type(self, kind: str) -> None:
        """Select a search radio and verify that it is actually selected."""
        tab_xpath, words = self._search_type_spec(kind)
        radios = []
        try:
            radios = self.d.find_elements(
                By.XPATH, X["patient_form"] + "//input[@type='radio']"
            )
        except WebDriverException:
            pass

        if radios:
            index = {"id_dob": 0, "name_dob": 1, "name_id": 2}.get(kind, 0)
            scores: list[int] = []
            for radio in radios:
                text = ""
                try:
                    label = radio.find_element(
                        By.XPATH, "./ancestor::label[1]")
                    text = el_text(label).lower()
                except (StaleElementReferenceException, WebDriverException):
                    pass
                scores.append(sum(1 for word in words if word in text))
            if scores and max(scores) > 0:
                index = scores.index(max(scores))
            index = min(index, len(radios) - 1)
            radio = radios[index]
            try:
                selected = radio.is_selected()
            except WebDriverException:
                selected = False
            if not selected:
                try:
                    label = radio.find_element(
                        By.XPATH, "./ancestor::label[1]")
                    self.d.execute_script("arguments[0].click();", label)
                except (StaleElementReferenceException, WebDriverException):
                    pass
            try:
                if not radio.is_selected():
                    self.d.execute_script("arguments[0].click();", radio)
            except (StaleElementReferenceException, WebDriverException):
                pass
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    if radio.is_selected():
                        break
                except (StaleElementReferenceException, WebDriverException):
                    break
                time.sleep(0.15)
            try:
                if radio.is_selected():
                    self.wait_idle(2)
                    if self.displayed(self._kind_field(kind), 2):
                        return
            except WebDriverException:
                pass

        # Fallback: click the supplied tab XPath and verify a field appeared.
        try:
            self.click(tab_xpath, 10)
        except TimeoutException:
            labels = {
                "id_dob": "//label[contains(.,'Cigna') and contains(.,'birth')]",
                "name_dob": "//label[contains(.,'Name') and contains(.,'birth')]",
                "name_id": "//label[contains(.,'Name') and contains(.,'Cigna')]",
            }
            self.click(labels[kind], 8)
        self.wait_idle(2)
        if not self.displayed(self._kind_field(kind), 2):
            try:
                element = self.find(tab_xpath, 3)
                label = element.find_element(By.XPATH, "./ancestor::label[1]")
                self.d.execute_script("arguments[0].click();", label)
                self.wait_idle(2)
            except (TimeoutException, StaleElementReferenceException,
                    WebDriverException):
                pass
        if not self.displayed(self._kind_field(kind), 2):
            log.warning("   search type %s may not be selected.", kind)
        time.sleep(0.2)

    def fill_search(self, kind: str, patient: dict[str, str]) -> None:
        """Clear every field first so values never leak between methods."""
        for xpath in (X["dob"], X["member_id"], X["first_name"],
                      X["last_name"]):
            self.clear_text(xpath, 2)

        if kind in ("id_dob", "name_id"):
            if not patient.get("cigna_id"):
                raise ValueError("search method needs a Cigna ID")
            self.set_text(X["member_id"], patient["cigna_id"])
        if kind in ("name_dob", "name_id"):
            if not (patient.get("first") and patient.get("last")):
                raise ValueError("search method needs first and last name")
            self.set_text(X["first_name"], patient["first"])
            self.set_text(X["last_name"], patient["last"])
        if kind in ("id_dob", "name_dob"):
            if not patient.get("dob"):
                raise ValueError("search method needs a DOB")
            self.set_text(X["dob"], patient["dob"])
        time.sleep(0.2)

    def click_search(self) -> None:
        if not self.click_first(X_ALT["patient_search"], 15):
            raise TimeoutException("Patient Search button not found.")
        self.wait_idle(4)

    def _results_text(self) -> str:
        try:
            containers = self.d.find_elements(By.ID, "results-top")
            if containers:
                return clean(containers[0].text).lower()
            return clean(self.d.find_element(By.TAG_NAME, "body").text).lower()
        except WebDriverException:
            return ""

    @staticmethod
    def _row_result(row, index: int) -> Result | None:
        try:
            cells = row.find_elements(By.XPATH, "./td")
        except (StaleElementReferenceException, WebDriverException):
            return None
        if not cells:
            return None
        texts = [el_text(cell) for cell in cells]
        dos = texts[5] if len(texts) >= 6 else ""
        if not parse_date(dos):
            for text in texts:
                if re.search(r"[/-]", text) and parse_date(text):
                    dos = text
                    break
        claim_no = ""
        try:
            links = row.find_elements(By.XPATH, "./td[2]//a")
            if links:
                claim_no = el_text(links[0])
        except (StaleElementReferenceException, WebDriverException):
            pass
        if not claim_no and len(texts) >= 2:
            claim_no = texts[1]
        if not dos and not claim_no:
            return None
        return Result(index=index, dos=dos, claim_no=claim_no)

    def result_signature(self, rows=None) -> tuple:
        if rows is None:
            rows = self.d.find_elements(By.XPATH, X["result_rows"])
        signature = []
        for index, row in enumerate(rows, 1):
            result = self._row_result(row, index)
            signature.append(
                (result.dos, result.claim_no) if result else ("", "")
            )
        return tuple(signature)

    def list_results(self, rows=None) -> list[Result]:
        if rows is None:
            rows = self.d.find_elements(By.XPATH, X["result_rows"])
        results: list[Result] = []
        for index, row in enumerate(rows, 1):
            result = self._row_result(row, index)
            if result:
                results.append(result)
        return results

    def wait_results(self, timeout: float = WAIT,
                     old_signature: tuple | None = None,
                     old_text: str | None = None):
        """Wait for a *new* result table; never trust a stale table.

        The table's claim numbers + DOS form the signature. When it stays
        exactly the same (and never disappears while the search runs) the
        previous patient's rows are treated as stale instead of being
        assigned to the new patient.
        """
        start = time.time()
        deadline = start + max(2, timeout)
        old_known = bool(old_signature)
        old_rows_vanished = False
        while time.time() < deadline:
            self.wait_idle(1)
            rows = self.d.find_elements(By.XPATH, X["result_rows"])
            if rows:
                signature = self.result_signature(rows)
                if not old_known:
                    return rows
                if signature != old_signature:
                    return rows
                if old_text is not None and self._results_text() != old_text:
                    return rows
                if old_rows_vanished:
                    # The table was re-rendered with identical data.
                    return rows
            else:
                if old_known:
                    old_rows_vanished = True
                body = self._results_text()
                if any(marker in body for marker in X["no_results_markers"]):
                    return []
            time.sleep(0.5)
        if old_known and not old_rows_vanished:
            log.warning(
                "Result table never changed after Search - ignoring it as "
                "stale results from the previous patient."
            )
            return []
        return self.d.find_elements(By.XPATH, X["result_rows"])

    def _warn_if_paginated(self) -> None:
        if not MAX_PAGES_WARNING:
            return
        try:
            candidates = self.d.find_elements(
                By.XPATH,
                "//cigna-results-table//button[not(@disabled)]"
                " | //cigna-results-table//a[not(@disabled)]",
            )
        except WebDriverException:
            return
        for element in candidates:
            try:
                text = el_text(element).lower()
                if element.is_displayed() and "next" in text:
                    log.warning(
                        "The result table has a Next page; only the current "
                        "page is inspected. Increase rows per page or use "
                        "--max-claims deliberately."
                    )
                    return
            except (StaleElementReferenceException, WebDriverException):
                continue

    def search_patient(self, patient: dict[str, str],
                       forced_type: str | None = None) -> list[Result]:
        if forced_type:
            kinds = [forced_type]
        else:
            available = {
                "id_dob": bool(patient["dob"] and patient["cigna_id"]),
                "name_dob": bool(patient["first"] and patient["last"]
                                 and patient["dob"]),
                "name_id": bool(patient["first"] and patient["last"]
                                and patient["cigna_id"]),
            }
            kinds = [kind for kind in ("id_dob", "name_dob", "name_id")
                     if available[kind]]
        if not kinds:
            raise ValueError(
                "Patient row has no usable search keys (DOB / Cigna ID / name)."
            )

        self.open_search()
        for kind in kinds:
            for attempt in (1, 2):
                try:
                    self.select_search_type(kind)
                    self.fill_search(kind, patient)
                    old_signature = self.result_signature()
                    old_text = self._results_text()
                    self.click_search()
                    rows = self.wait_results(WAIT, old_signature, old_text)
                    results = self.list_results(rows)
                    if results:
                        log.info("   search %s -> %d claim row(s)",
                                 kind, len(results))
                        self._warn_if_paginated()
                        return results
                    log.info("   search %s -> no results", kind)
                    break
                except (TimeoutException, WebDriverException,
                        ValueError) as exc:
                    log.warning("   search %s attempt %d failed: %s",
                                kind, attempt, exc)
                    if attempt == 1:
                        try:
                            self.open_search()
                        except (TimeoutException, WebDriverException):
                            pass
                        continue
                    break
        return []

    # ------------------------------------------------------------- claim page
    def claim_href(self, index: int) -> str:
        for xpath in (
            f"({X['result_rows']})[{index}]" + X["claim_link_cell"],
            f"({X['result_rows']})[{index}]//a[1]",
        ):
            try:
                link = self.d.find_element(By.XPATH, xpath)
                href = clean(link.get_attribute("href") or "")
                if not href or href.lower().startswith("javascript"):
                    continue
                href = urljoin(self.d.current_url, href)
                if href.rstrip("/") == self.d.current_url.rstrip("/"):
                    continue
                return href
            except (WebDriverException, StaleElementReferenceException):
                continue
        return ""

    def wait_claim_page(self, timeout: float = 30) -> bool:
        deadline = time.time() + max(1, timeout)
        while time.time() < deadline:
            for xpath in (X["claim_screen"], X["claim_info"]):
                try:
                    for element in self.d.find_elements(By.XPATH, xpath):
                        if element.is_displayed():
                            self.wait_idle(3)
                            return True
                except (StaleElementReferenceException, WebDriverException):
                    continue
            time.sleep(0.4)
        return False

    def _window_after(self, old_handles: set[str],
                      timeout: float = 5) -> list[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                new = [handle for handle in self.d.window_handles
                       if handle not in old_handles]
            except WebDriverException:
                return []
            if new:
                return new
            time.sleep(0.3)
        return []

    def load_claim(self, index: int, patient: dict[str, str],
                   result_dos: str = ""):
        """Open one result row. Returns (Claim, needs_back)."""
        href = self.claim_href(index)
        if href:
            main = self.d.current_window_handle
            opened = False
            try:
                self.d.switch_to.new_window("tab")
                opened = True
                self.go(href)
                if self.wait_claim_page(25):
                    claim = self.scrape_claim()
                    claim.result_dos = result_dos
                    claim.source_index = index
                    if claim.claim_no or claim.procs:
                        return claim, False
            except (TimeoutException, WebDriverException) as exc:
                log.warning("   direct claim link failed (%s) - clicking row.",
                            exc)
            finally:
                if opened:
                    try:
                        self.d.close()
                    except WebDriverException:
                        pass
                    try:
                        self.d.switch_to.window(main)
                    except WebDriverException:
                        pass

        # Click the row/link in the current tab.
        main = self.d.current_window_handle
        old_handles = set(self.d.window_handles)
        clicked = self.click_first((
            f"({X['result_rows']})[{index}]" + X["claim_link_cell"],
            f"({X['result_rows']})[{index}]//a[1]",
            f"({X['result_rows']})[{index}]",
        ), 15)
        if not clicked:
            raise TimeoutException("Claim result row could not be clicked.")

        new_windows = self._window_after(old_handles, 5)
        if new_windows:
            try:
                self.d.switch_to.window(new_windows[-1])
            except WebDriverException as exc:
                raise TimeoutException(
                    "New claim tab could not be selected."
                ) from exc
            if not self.wait_claim_page(30):
                raise TimeoutException("Claim details page did not open.")
            claim = self.scrape_claim()
            claim.result_dos = result_dos
            claim.source_index = index
            try:
                self.d.close()
            except WebDriverException:
                pass
            try:
                self.d.switch_to.window(main)
            except WebDriverException:
                pass
            return claim, False

        if not self.wait_claim_page(30):
            raise TimeoutException("Claim details page did not open.")
        claim = self.scrape_claim()
        claim.result_dos = result_dos
        claim.source_index = index
        return claim, True

    def scrape_claim(self) -> Claim:
        if not self.wait_claim_page(10):
            log.warning("   claim screen is slow; trying to scrape anyway.")

        claim_no = (self.info_value("claim", "number")
                    or self.text_if_label(X["claim_reference"],
                                          ("claim", "reference"))
                    or self.text_if_label(X["claim_reference"], ("claim",)))
        date_processed = (self.info_value("date", "processed")
                          or self.text_if_label(X["date_processed"],
                                                ("date", "processed")))
        status = (self.info_value("claim", "status", exclude=("reason",))
                  or self.text_if_label(X["claim_status"],
                                        ("claim", "status"),
                                        exclude=("reason",)))
        amount_due = (self.info_value("amount", "due")
                      or self.text_if_label(X["amount_due"],
                                            ("amount", "due")))
        amount_paid = (self.info_value("amount", "paid", exclude=("due",))
                       or self.text_if_label(X["amount_paid"],
                                             ("amount", "paid"),
                                             exclude=("due",)))
        provider_payment = (self.info_value("provider", "payment")
                            or self.text_if_label(X["provider_payment"],
                                                  ("provider", "payment")))
        patient_resp = (self.info_value("patient", "responsibility")
                        or self.text_if_label(X["patient_responsibility"],
                                              ("patient", "responsibility")))

        claim = Claim(
            claim_no=strip_label(claim_no),
            date_processed=fmt_date(date_processed),
            status=clean(status),
            amount_due=clean(amount_due),
            amount_paid=clean(amount_paid),
            provider_payment=clean(provider_payment),
            patient_resp=clean(patient_resp),
        )
        claim.check_no = self.find_check_number()
        claim.procs = self.scrape_procedures()
        claim.expl = self.scrape_explanations()
        log.info(
            "   claim %s | status %s | %d procedure(s) | %d explanation(s)",
            claim.claim_no or "?", claim.status or "?",
            len(claim.procs), len(claim.expl),
        )
        return claim

    def _procedure_column_map(
            self, header_texts: Sequence[str]) -> tuple[int, int, int]:
        cpt_idx, dos_idx, rem_idx = 0, 1, 11
        for index, text in enumerate(header_texts):
            name = norm_header(text)
            if not name:
                continue
            if ("remark" in name or "adjustment" in name
                    or "reason" in name) and "date" not in name:
                rem_idx = index
            elif ("dateofservice" in name or name == "dos"
                    or ("date" in name and "service" in name)):
                dos_idx = index
            elif ("procedure" in name or "cpt" in name or name == "code"):
                if ("remark" not in name and "type" not in name
                        and "modifier" not in name
                        and "description" not in name):
                    cpt_idx = index
        return cpt_idx, dos_idx, rem_idx

    def scrape_procedures(self) -> list[Procedure]:
        rows = []
        for xpath in (X["procedure_rows"],
                      "//cigna-medical-procedure-details//table//tbody/tr",
                      "//*[contains(@class,'procedure')]//table//tbody/tr"):
            try:
                rows = self.d.find_elements(By.XPATH, xpath)
            except WebDriverException:
                rows = []
            if rows:
                break
        if not rows:
            return []

        header_texts: list[str] = []
        try:
            headers = self.d.find_elements(
                By.XPATH,
                "//cigna-medical-procedure-details//table//thead//th",
            )
            header_texts = [el_text(th) for th in headers]
        except WebDriverException:
            pass
        if not header_texts:
            try:
                first_cells = rows[0].find_elements(By.XPATH, "./th")
                header_texts = [el_text(th) for th in first_cells]
            except (StaleElementReferenceException, WebDriverException):
                pass

        cpt_idx, dos_idx, rem_idx = self._procedure_column_map(header_texts)
        procedures: list[Procedure] = []
        seen: set[tuple] = set()
        for row in rows:
            try:
                cells = row.find_elements(By.XPATH, "./td")
            except (StaleElementReferenceException, WebDriverException):
                continue
            if len(cells) < 2:
                continue
            texts = [el_text(cell) for cell in cells]
            cpt_raw = texts[cpt_idx] if cpt_idx < len(texts) else texts[0]
            dos_raw = texts[dos_idx] if dos_idx < len(texts) else ""
            remark_raw = texts[rem_idx] if rem_idx < len(texts) else ""
            cpt = extract_cpt(cpt_raw)
            codes = split_codes(remark_raw)

            # If the table has fewer columns than expected, find the remark cell.
            if not codes:
                for index, text in enumerate(texts):
                    if index in (cpt_idx, dos_idx):
                        continue
                    candidate = split_codes(text)
                    if not candidate:
                        continue
                    # Do not mistake a bare amount ("150") for a remark code.
                    if (re.search(r"[A-Z]", clean(text).upper())
                            or re.search(r"\s[-\u2013\u2014]\s", text)
                            or len(candidate) > 1 or "," in text):
                        remark_raw = text
                        codes = candidate
                        break

            if not cpt and not codes:
                continue
            key = (cpt, fmt_date(dos_raw), tuple(codes))
            if key in seen:
                continue
            seen.add(key)
            procedures.append(Procedure(
                cpt=cpt,
                dos=fmt_date(dos_raw),
                codes=codes,
                raw_remark=remark_raw,
            ))
        return procedures

    def scrape_explanations(self) -> dict[str, str]:
        explanations: dict[str, str] = {}

        def put(code: str, desc: str) -> None:
            code = as_code(code)
            desc = clean(desc)
            if not code or not _looks_like_code(code):
                return
            if code not in explanations or (not explanations[code] and desc):
                explanations[code] = desc

        # 1) A normal explanation table.
        try:
            rows = self.d.find_elements(
                By.XPATH,
                "//cigna-medical-explanation-details//table//tbody/tr",
            )
        except WebDriverException:
            rows = []
        for row in rows:
            try:
                cells = row.find_elements(By.XPATH, "./td|./th")
            except (StaleElementReferenceException, WebDriverException):
                continue
            if len(cells) < 2:
                continue
            texts = [el_text(cell) for cell in cells]
            for code in split_codes(texts[0]):
                put(code, " ".join(text for text in texts[1:] if text))

        # 2) The div/strong pair structure from the supplied workflow.
        blocks = []
        for xpath in (X["explanation_blocks"],
                      "//cigna-medical-explanation-details/div[1]/div"):
            try:
                blocks = self.d.find_elements(By.XPATH, xpath)
            except WebDriverException:
                blocks = []
            if blocks:
                break
        for block in blocks:
            try:
                code_el = block.find_elements(By.XPATH, "./div[1]")
                if not code_el:
                    code_el = block.find_elements(By.XPATH, "./strong[1]")
                if not code_el:
                    continue
                code_text = el_text(code_el[0])
                desc_el = block.find_elements(By.XPATH, "./div[2]//p")
                if not desc_el:
                    desc_el = block.find_elements(By.XPATH, "./div[2]")
                desc = " ".join(
                    text for text in (el_text(x) for x in desc_el) if text
                )
                for code in split_codes(code_text):
                    put(code, desc)
            except (StaleElementReferenceException, WebDriverException):
                continue

        # 3) Any <strong> code followed by a <p> explanation.
        try:
            containers = self.d.find_elements(
                By.XPATH, "//cigna-medical-explanation-details")
        except WebDriverException:
            containers = []
        for container in containers:
            try:
                strongs = container.find_elements(By.XPATH, ".//strong")
            except (StaleElementReferenceException, WebDriverException):
                continue
            for strong in strongs:
                try:
                    codes = split_codes(el_text(strong))
                    if not codes:
                        continue
                    desc = ""
                    for expr in ("./following-sibling::p[1]",
                                 "./following::p[1]"):
                        found = strong.find_elements(By.XPATH, expr)
                        if found:
                            desc = el_text(found[0])
                            break
                    for code in codes:
                        put(code, desc)
                except (StaleElementReferenceException, WebDriverException):
                    continue

        # 4) Last resort: read line-by-line text from an unusual layout.
        for container in containers:
            try:
                raw = container.get_attribute("textContent") or ""
            except (StaleElementReferenceException, WebDriverException):
                continue
            for line in str(raw).splitlines():
                line = clean(line)
                if not line or (" - " not in line and ":" not in line):
                    continue
                for code, desc in remark_pairs(line).items():
                    if desc:
                        put(code, desc)
        return explanations

    def find_check_number(self) -> str:
        for keywords in (("check", "number"), ("eft", "number"),
                         ("check",)):
            value = self.info_value(
                *keywords, exclude=("date", "amount")
            )
            if value and value.upper() not in ("N/A", "NA", "-"):
                return value
        try:
            rows = self.d.find_elements(
                By.XPATH,
                "//cigna-medical-claims-information//table/tbody/tr",
            )
        except WebDriverException:
            rows = []
        for row in rows:
            try:
                cells = row.find_elements(By.XPATH, "./th|./td")
                if len(cells) < 2:
                    continue
                label = el_text(cells[0]).lower()
                if (("check" in label or "eft" in label)
                        and "date" not in label and "amount" not in label):
                    value = el_text(cells[-1])
                    if value:
                        return value
            except (StaleElementReferenceException, WebDriverException):
                continue
        return ""

    def _wait_result_rows(self, timeout: float = 20) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.d.find_elements(By.XPATH, X["result_rows"]):
                    self.wait_idle(1)
                    return True
            except WebDriverException:
                pass
            time.sleep(0.4)
        return False

    def back_to_results(self, patient: dict[str, str] | None = None) -> bool:
        if self.click_first(X_ALT["return_claims"], 8):
            if self._wait_result_rows(20):
                return True
        try:
            self.d.back()
            if self._wait_result_rows(15):
                return True
        except WebDriverException:
            pass
        if patient:
            try:
                return bool(self.search_patient(patient))
            except (TimeoutException, WebDriverException, ValueError):
                return False
        return False

# ===========================================================================
# REPORT BUILDING
# ===========================================================================
def procedure_remark(claim: Claim, proc: Procedure) -> str:
    """'197 - explanation; 198 - explanation' for one procedure row."""
    parts: list[str] = []
    for code in proc.codes:
        desc = clean(claim.expl.get(code, ""))
        parts.append(f"{code} - {desc}" if desc else code)
    if parts:
        return "; ".join(parts)
    return clean(proc.raw_remark)


def not_found_report(patient: dict[str, str]) -> dict[str, str]:
    name = clean(f"{patient.get('first', '')} {patient.get('last', '')}")
    detail = []
    if patient.get("dob"):
        detail.append(f"DOB {patient['dob']}")
    if patient.get("cigna_id"):
        detail.append(f"Cigna ID {patient['cigna_id']}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    return {
        "status": STATUS_NOT_FOUND,
        "notes1": (f"No claim found for {name or 'patient'}{suffix} "
                   "using search types 1-3."),
        "notes2": "",
        "cpt": "",
        "remark_codes": "",
        "denied_cpt": "",
    }


def no_claims_report(patient: dict[str, str]) -> dict[str, str]:
    name = clean(f"{patient.get('first', '')} {patient.get('last', '')}")
    return {
        "status": STATUS_NO_CLAIMS,
        "notes1": f"Patient {name or 'record'} was found but no claim rows "
                  "were returned.",
        "notes2": "",
        "cpt": "",
        "remark_codes": "",
        "denied_cpt": "",
    }


def order_results(results: list[Result], excel_dos: str) -> list[Result]:
    """DOS matches first; remaining rows newest DOS first."""
    target = parse_date(excel_dos)
    if target:
        matched = [r for r in results if parse_date(r.dos) == target]
    else:
        matched = []
    rest = [r for r in results if r not in matched]
    rest.sort(key=lambda r: parse_date(r.dos) or datetime(1900, 1, 1),
              reverse=True)
    return matched + rest


def build_report(claim: Claim, patient: dict[str, str], book: Book,
                 cpt_missing: bool = False,
                 dos_mismatch: bool = False,
                 selection_reason: str = "Latest Claim Fallback",
                 result_dos: str = "",
                 notes2_mode: str = NOTES2_NOTE,
                 status_style: str = "workflow",
                 note_cpt_fallback: str = "claim") -> dict[str, str]:
    target = as_code(patient.get("target_cpt"))
    patient_dos = clean(patient.get("dos"))
    denied = claim.is_denied()
    base_keyword = "DND" if denied else "PAID"

    claim_dos = (fmt_date(result_dos)
                 or fmt_date(claim.result_dos)
                 or next((fmt_date(p.dos) for p in claim.procs if p.dos), "")
                 or fmt_date(claim.date_processed))
    dos = claim_dos if (dos_mismatch or not patient_dos) else patient_dos

    paid = claim.amount_paid
    if abs(to_amount(paid)) < 0.005:
        paid = claim.provider_payment
    check = clean(claim.check_no) or clean(claim.claim_no)
    processed = fmt_date(claim.date_processed)

    head = f"DOS {dos or 'N/A'} CLM PD {money(paid)}"
    if check:
        head += f" THRU CHK #{check}"
    if processed:
        head += f" ISSUED ON {processed}"
    head += "."

    procedures = list(claim.procs)
    if not procedures and (target or claim_dos):
        procedures = [Procedure(cpt=target, dos=claim_dos,
                                codes=list(claim.expl))]
    procedures.sort(key=lambda p: 0 if target and as_code(p.cpt) == target
                    else 1)

    segments: list[str] = []
    all_codes: list[str] = []
    missing_mapping = False
    for proc in procedures:
        cpt = as_code(proc.cpt) or target
        remark_text = procedure_remark(claim, proc)
        master_status, exact = book.master_lookup(cpt, remark_text)
        cpt_known = bool(book.master_by_cpt.get(cpt))

        keyword = base_keyword
        master_upper = master_status.upper()
        if exact and master_upper in ("PAID", "PIF", "PAID IN FULL"):
            keyword = "PAID"
        elif exact and master_upper in ("DND", "DENIED", "DENIAL"):
            keyword = "DND"

        remark_parts: list[str] = []
        for code in proc.codes:
            desc = clean(claim.expl.get(code, ""))
            remark_parts.append(f"{code} - {desc}" if desc else code)
            if code not in all_codes:
                all_codes.append(code)
        if not remark_parts and remark_text:
            remark_parts.append(remark_text)
        if not remark_parts and exact and master_status:
            remark_parts.append(master_status)

        # Workflow 6.4: a CPT row with a different remark is not an exact
        # mapping. New CPTs are added to Master later, so they are not flagged.
        if cpt_known and not exact:
            missing_mapping = True

        note_cpt = cpt
        if not exact and cpt_known and note_cpt_fallback == "input" and target:
            note_cpt = target

        segment = f"CPT ({note_cpt or 'N/A'}) {keyword}"
        segment += f" ({'; '.join(remark_parts) or 'N/A'})"
        segments.append(segment)

    # Explanation-only codes (not tied to a procedure row) are still retained.
    cpts = claim.cpt_set()
    for code, desc in claim.expl.items():
        if code not in all_codes and code not in cpts:
            all_codes.append(code)

    explanation_lines: list[str] = []
    for code in all_codes:
        desc = clean(claim.expl.get(code, ""))
        explanation_lines.append(f"{code} - {desc}" if desc else code)

    note = head
    if segments:
        note += " " + " ".join(segments)
    if cpt_missing and target:
        note = f"CPT {target} not found in the checked claims. " + note

    if dos_mismatch:
        notes1 = ""
        if notes2_mode == NOTES2_MARKER:
            notes2 = "DOS Not Matched"
            if cpt_missing and target:
                notes2 += f" (CPT {target} not found in checked claims)"
        else:
            notes2 = note
    else:
        notes1 = note
        notes2 = "No"

    if status_style == "claim":
        if cpt_missing:
            status = "CPT Not Found"
        else:
            status = (clean(claim.status)
                      or ("Denied" if denied else "Paid"))
    else:
        base_status = {
            "DOS Match": STATUS_DOS,
            "CPT Match (Recent Claim)": STATUS_CPT_RECENT,
            "CPT Match": STATUS_CPT_RECENT,
        }.get(selection_reason, STATUS_LATEST)
        status_parts = [base_status]
        if cpt_missing:
            status_parts.append("CPT Not Found")
        if missing_mapping:
            status_parts.append(STATUS_REVIEW)
        status = " | ".join(status_parts)

    cpt_output: list[str] = []
    for proc in claim.procs:
        code = as_code(proc.cpt)
        if code and code not in cpt_output:
            cpt_output.append(code)
    if not cpt_output and target:
        cpt_output = [target]

    return {
        "status": status,
        "notes1": notes1,
        "notes2": notes2,
        "cpt": ", ".join(cpt_output),
        "remark_codes": "; ".join(explanation_lines),
        "denied_cpt": (", ".join(cpt_output) if denied else ""),
    }


def process_patient(bot: Bot, book: Book, patient: dict[str, str],
                    max_claims: int = MAX_CLAIMS,
                    forced_type: str | None = None,
                    notes2_mode: str = NOTES2_NOTE,
                    status_style: str = "workflow",
                    note_cpt_fallback: str = "claim"):
    """Search, choose the best claim, validate the CPT, build the report."""
    results = bot.search_patient(patient, forced_type=forced_type)
    if not results:
        return not_found_report(patient), None

    excel_dos = clean(patient.get("dos"))
    target = as_code(patient.get("target_cpt"))
    ordered = order_results(results, excel_dos)
    matched = [r for r in ordered
               if excel_dos and same_date(r.dos, excel_dos)]
    rest = [r for r in ordered if r not in matched]
    max_claims = max(1, max_claims)
    if matched:
        candidates = matched + rest[:max(0, max_claims - len(matched))]
    else:
        candidates = rest[:max_claims]
    if not candidates:
        return no_claims_report(patient), None

    selected_claim: Claim | None = None
    selected_result: Result | None = None
    fallback_claim: Claim | None = None
    fallback_result: Result | None = None
    selection_reason = "Latest Claim Fallback"
    checked = 0

    for result in candidates:
        checked += 1
        log.info("   checking claim %s (DOS %s) [%d/%d]",
                 result.claim_no or "?", result.dos or "?",
                 checked, len(candidates))
        try:
            claim, needs_back = bot.load_claim(
                result.index, patient, result_dos=result.dos
            )
        except (TimeoutException, WebDriverException, RuntimeError) as exc:
            log.warning("   could not open claim row %d: %s",
                        result.index, exc)
            try:
                if not bot.displayed(X["search_form"], 1):
                    bot.back_to_results(patient)
            except (TimeoutException, WebDriverException):
                pass
            continue

        try:
            if fallback_claim is None:
                fallback_claim, fallback_result = claim, result
            is_dos_match = bool(
                excel_dos and same_date(result.dos, excel_dos)
            )
            if not target:
                selected_claim, selected_result = claim, result
                selection_reason = ("DOS Match" if is_dos_match
                                    else "Latest Claim Fallback")
                break
            if target in claim.cpt_set():
                selected_claim, selected_result = claim, result
                if is_dos_match:
                    selection_reason = "DOS Match"
                elif excel_dos:
                    selection_reason = "CPT Match (Recent Claim)"
                else:
                    selection_reason = "CPT Match"
                break
            log.info("   CPT %s not in this claim - trying the next claim",
                     target)
        finally:
            # Only go back when another result row has to be inspected; the
            # next patient starts with its own search anyway.
            if needs_back and selected_claim is None:
                bot.back_to_results(patient)

    if selected_claim is None:
        selected_claim, selected_result = fallback_claim, fallback_result
        if selected_claim is None:
            raise RuntimeError("None of the claim result rows could be opened.")
        if (excel_dos and selected_result
                and same_date(selected_result.dos, excel_dos)):
            selection_reason = "DOS Match"
        else:
            selection_reason = "Latest Claim Fallback"
        log.warning("   CPT %s was not found in %d checked claim(s); "
                    "using the %s claim.", target or "-", checked,
                    selection_reason)

    cpt_missing = bool(target) and target not in selected_claim.cpt_set()
    dos_mismatch = bool(
        excel_dos and selected_result
        and not same_date(selected_result.dos, excel_dos)
    )
    report = build_report(
        selected_claim, patient, book,
        cpt_missing=cpt_missing,
        dos_mismatch=dos_mismatch,
        selection_reason=selection_reason,
        result_dos=selected_result.dos if selected_result else "",
        notes2_mode=notes2_mode,
        status_style=status_style,
        note_cpt_fallback=note_cpt_fallback,
    )
    report["_selection_reason"] = selection_reason
    report["_claim_no"] = selected_claim.claim_no
    return report, selected_claim


# ===========================================================================
# ERROR REPORTS
# ===========================================================================
def error_report(exc: BaseException, attempt: int = 1) -> dict[str, str]:
    """Failure text goes to Status + Error; Notes1/Notes2 are left alone."""
    message = clean(f"{type(exc).__name__}: {exc}")
    if len(message) > 1500:
        message = message[:1500] + "..."
    if attempt > 1:
        message = f"[attempt {attempt}] {message}"
    return {"status": STATUS_FAILED, "error": message}


# ===========================================================================
# RUN HELPERS
# ===========================================================================
def local_data_dir() -> Path:
    """Chrome profile / session belong outside OneDrive-synced folders."""
    base = os.environ.get("LOCALAPPDATA")
    root = (Path(base) / "CignaAutomation") if base else (APP_DIR / ".cigna")
    root.mkdir(parents=True, exist_ok=True)
    return root


def setup_logging(log_dir: Path, debug: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cigna_automation.log"
    logger = logging.getLogger("cigna")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=4 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def backup_workbook(path: Path, backup_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{path.stem}_{stamp}{path.suffix}"
    try:
        shutil.copy2(str(path), str(target))
        log.info("Backup: %s", target)
        return target
    except OSError as exc:
        log.warning("Could not create backup (%s) - continuing.", exc)
        return None


def default_excel() -> Path | None:
    for candidate in DEFAULT_EXCEL_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cigna for HCP claim automation (fixed build).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--excel", type=Path, default=None,
        help="Path to Cigna.xlsx (auto-detected when omitted).",
    )
    parser.add_argument(
        "--sheet", default=SHEET_INPUT,
        help="Input worksheet name.",
    )
    parser.add_argument(
        "--rows", type=int, default=0,
        help="Process at most N pending rows (0 = all).",
    )
    parser.add_argument(
        "--start-row", type=int, default=0,
        help="First worksheet row to consider (0 = first data row).",
    )
    parser.add_argument(
        "--max-claims", type=int, default=MAX_CLAIMS,
        help="Maximum claim rows inspected per patient.",
    )
    parser.add_argument(
        "--search-type", choices=("id_dob", "name_dob", "name_id"),
        default=None,
        help="Force one search method instead of Method 1 -> 2 -> 3.",
    )
    parser.add_argument(
        "--notes2-mode", choices=(NOTES2_NOTE, NOTES2_MARKER),
        default=NOTES2_NOTE,
        help="DOS-mismatch note: full note with the claim's real DOS, "
             "or the literal 'DOS Not Matched' marker.",
    )
    parser.add_argument(
        "--status-style", choices=("workflow", "claim"),
        default="workflow",
        help="'workflow' writes Completed/Not Found statuses; "
             "'claim' writes the portal claim status.",
    )
    parser.add_argument(
        "--note-cpt-fallback", choices=("claim", "input"),
        default="claim",
        help="CPT shown in a note when no exact Master mapping exists.",
    )
    parser.add_argument(
        "--no-retry-errors", dest="retry_errors", action="store_false",
        help="Skip rows whose Status is Failed.",
    )
    parser.add_argument(
        "--retry-errors", dest="retry_errors", action="store_true",
        help="Retry rows whose Status is Failed (default).",
    )
    parser.set_defaults(retry_errors=True)
    parser.add_argument(
        "--retry-not-found", action="store_true",
        help="Also retry rows with Status Not Found / No Claims.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Open the portal but do not write or save the workbook.",
    )
    parser.add_argument(
        "--fresh-login", action="store_true",
        help="Ignore the saved session and log in from scratch.",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run Chrome without a visible window (MFA still prompts in CMD).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Verbose logging and extra Selenium noise.",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Do not copy the workbook before the run.",
    )
    parser.add_argument("--session-file", type=Path, default=None,
                        help="Saved-session JSON path.")
    parser.add_argument("--profile-dir", type=Path, default=None,
                        help="Chrome user-data directory.")
    parser.add_argument("--screenshot-dir", type=Path, default=None,
                        help="Folder for screenshots / HTML dumps.")
    parser.add_argument("--backup-dir", type=Path, default=None,
                        help="Folder for workbook backups.")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help="Folder for the log file.")
    return parser.parse_args(argv)


# ===========================================================================
# MAIN
# ===========================================================================
def run(args: argparse.Namespace) -> int:
    excel = Path(args.excel) if args.excel else default_excel()
    if not excel or not excel.exists():
        print(
            "Cigna.xlsx was not found.\n"
            "Pass it explicitly:\n"
            '  python cigna_automation_final.py --excel "C:\\path\\Cigna.xlsx"',
            file=sys.stderr,
        )
        return 2

    log_dir = args.log_dir or (APP_DIR / "logs")
    log_file = setup_logging(log_dir, args.debug)
    log.info("=" * 70)
    log.info("Cigna automation - %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Workbook: %s", excel)
    log.info("Log file: %s", log_file)

    data_dir = local_data_dir()
    session_file = args.session_file or (data_dir / "cigna_session.json")
    profile_dir = args.profile_dir or (data_dir / "chrome_profile")
    screenshot_dir = args.screenshot_dir or (APP_DIR / "screenshots")
    backup_dir = args.backup_dir or (APP_DIR / "backups")

    try:
        book = Book(excel, dry_run=args.dry_run, sheet_input=args.sheet)
    except Exception as exc:  # noqa: BLE001 - report workbook problems cleanly
        log.error("Cannot open the workbook: %s", exc)
        return 2

    pending = book.pending_rows(
        retry_failed=args.retry_errors,
        retry_not_found=args.retry_not_found,
        limit=max(0, args.rows),
        start_row=max(0, args.start_row),
    )
    if not pending:
        log.info("No pending rows. Nothing to do.")
        return 0
    log.info("%d row(s) to process.", len(pending))

    if not args.dry_run and not args.no_backup:
        backup_workbook(excel, backup_dir)

    try:
        username, password = book.credentials()
    except (OSError, ValueError, KeyError) as exc:
        log.error("Login sheet problem: %s", exc)
        return 2

    bot: Bot | None = None
    completed = 0
    failed = 0
    try:
        bot = Bot(
            session_file=session_file,
            profile_dir=profile_dir,
            screenshot_dir=screenshot_dir,
            headless=args.headless,
            debug=args.debug,
        )
        bot.ensure_logged_in(username, password, fresh=args.fresh_login)

        for position, (row, patient) in enumerate(pending, 1):
            label = clean(
                f"Row {row} ({position}/{len(pending)}) "
                f"{patient.get('first', '')} {patient.get('last', '')}"
            )
            label += (
                f" | DOS {patient.get('dos') or '-'}"
                f" | CPT {patient.get('target_cpt') or '-'}"
            )
            log.info("-" * 70)
            log.info(label)
            if args.dry_run:
                log.info("[dry-run] changes are not saved.")

            report: dict[str, str] | None = None
            claim: Claim | None = None
            last_error: BaseException | None = None

            for attempt in range(1, ROW_RETRIES + 2):
                try:
                    report, claim = process_patient(
                        bot, book, patient,
                        max_claims=args.max_claims,
                        forced_type=args.search_type,
                        notes2_mode=args.notes2_mode,
                        status_style=args.status_style,
                        note_cpt_fallback=args.note_cpt_fallback,
                    )
                    last_error = None
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - one row must not stop the run
                    last_error = exc
                    log.warning("Row %d attempt %d failed: %s", row,
                                attempt, exc)
                    if attempt <= ROW_RETRIES:
                        try:
                            bot.screenshot(f"row{row}_attempt{attempt}")
                        except Exception:
                            pass
                        time.sleep(2 * attempt)
                        continue
                    break

            if last_error is not None or report is None:
                exc = last_error or RuntimeError("no report was produced")
                log.error("Row %d failed: %s", row, exc)
                if not args.dry_run:
                    book.write_result(row, error_report(exc))
                    book.save()
                failed += 1
                continue

            try:
                if claim is not None:
                    added = book.add_master(claim)
                    if added:
                        log.info("   Master: added %s", ", ".join(added))
                if not args.dry_run:
                    book.write_result(row, report)
                    book.save()
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the row marked failed
                log.error("Row %d could not be written: %s", row, exc)
                failed += 1
                continue

            completed += 1
            log.info(
                "   %s | claim %s | %s",
                report.get("status", ""),
                report.get("_claim_no", "") or "?",
                report.get("_selection_reason", ""),
            )
            if report.get("notes1"):
                log.info("   Notes1: %s", report["notes1"])
            if report.get("notes2"):
                log.info("   Notes2: %s", report["notes2"])

    except KeyboardInterrupt:
        log.warning("Interrupted by user - progress is saved after every row.")
        return 130
    except (RuntimeError, TimeoutException, WebDriverException) as exc:
        log.error("Stopped: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - show a clean message, keep the log
        log.exception("Stopped by an unexpected error: %s", exc)
        return 1
    finally:
        if bot is not None:
            try:
                bot.d.quit()
            except WebDriverException:
                pass

    log.info("=" * 70)
    log.info("Done: %d completed, %d failed (of %d).",
             completed, failed, len(pending))
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
