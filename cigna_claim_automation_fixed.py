#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cigna for HCP - Advanced Claim Status Automation (fixed)
=========================================================
Selenium 4 + openpyxl.

Workbook : C:\\Users\\Automation22\\Desktop\\Automation\\Cigna\\Cigna.xlsx
Sheets   : Login (Username/Password), input, Master (CPT/Remark Codes/Status)

What this script does
---------------------
1. Opens https://cignaforhcp.cigna.com/app/login and closes the OneTrust banner.
2. Reuses a saved session (Chrome profile + cookies + localStorage, 30 days).
   Otherwise: Login > Username > Next > Password > Continue > OTP (typed in the
   CMD window) > Remember me > Continue, then saves the session again.
3. For each pending 'input' row it tries patient search Type 1 (DOB + Cigna ID),
   Type 2 (First + Last + DOB) and Type 3 (First + Last + Cigna ID).
4. Opens the claim whose Date of Service matches the Excel DOS (otherwise the
   newest claim) and checks the input CPT. If the CPT is not in that claim it
   checks the next claim(s) - first claim + last two claims by default.
5. Reads the claim header, procedure lines, remark codes, explanations and
   builds Notes1 (PAID / DND), Notes2, Denied CPT, Remark Codes and Status.
6. Adds CPTs that are missing from 'Master' (CPT / Remark Codes / Status).
   Existing CPTs are never duplicated or overwritten.
7. Saves the workbook after every row and takes screenshots on errors.

Install : pip install -U selenium openpyxl
Run     : python cigna_claim_automation_fixed.py
Options : python cigna_claim_automation_fixed.py --help

Close Cigna.xlsx in Excel and any Chrome using the profile folder before running.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from openpyxl import load_workbook
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
LOGIN_URL = "https://cignaforhcp.cigna.com/app/login"
PORTAL_URL = "https://cignaforhcp.cigna.com/"
DEFAULT_EXCEL = Path(r"C:\Users\Automation22\Desktop\Automation\Cigna\Cigna.xlsx")

WAIT = 25                 # normal element timeout (seconds)
PAGE_LOAD_TIMEOUT = 90
SESSION_VALID_DAYS = 30
MAX_CLAIMS = 3            # first claim + last two claims
ROW_RETRIES = 2

SHEET_INPUT = "input"
SHEET_MASTER = "Master"
SHEET_LOGIN = "Login"

STATUS_NOT_FOUND = "Not Found"
STATUS_CPT_MISSING = "CPT Not Found"
STATUS_ERROR = "Automation Error"

# Optional: XPath of the check number on the claim page.
# Leave empty to auto-detect a "Check number" row in the claim information table.
CHECK_NUMBER_XPATH = ""

# ---------------------------------------------------------------------------
# XPATHS (taken from the workflow notes)
# ---------------------------------------------------------------------------
X_COOKIE_CLOSE = '//*[@id="onetrust-close-btn-container"]/button'

X_LOGIN_BTN = ("/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-public/div/main/"
               "cigna-page/div/div[1]/div/cigna-login-form-auth0/div/div[2]/button")
X_LOGIN_BTN_ANY = [
    X_LOGIN_BTN,
    "//button[contains(translate(normalize-space(.),'LOGIN ','login'),'login')]",
    "//a[contains(translate(normalize-space(.),'LOGIN ','login'),'login')]",
]
X_USERNAME_LABEL = '//*[@id="username-label"]'
X_USERNAME_INPUT = '//*[@id="username"]'
X_NEXT = "/html/body/main/section/div/div/div/div[1]/div/form/div[4]/button"
X_PASSWORD = '//*[@id="password"]'
X_CONTINUE_PWD = "/html/body/main/section/div/div/div/form/div[5]/button"
X_REMEMBER = "/html/body/main/section/div/div/div/div/div/form/div[3]/label/span"
X_OTP = '//*[@id="code"]'
X_CONTINUE_OTP = "/html/body/main/section/div/div/div/div/div/form/div[4]/button"

# Fallbacks for Auth0 buttons (used only when the absolute XPath is not found)
X_NEXT_ANY = [
    X_NEXT,
    "//form//button[@type='submit']",
    "//button[contains(translate(normalize-space(.),'NEXT ','next'),'next')]",
]
X_CONTINUE_PWD_ANY = [
    X_CONTINUE_PWD,
    "//button[contains(translate(normalize-space(.),'CONTINUE ','continue'),'continue')]",
    "//form//button[@type='submit']",
]
X_CONTINUE_OTP_ANY = [
    X_CONTINUE_OTP,
    "//button[contains(translate(normalize-space(.),'CONTINUE ','continue'),'continue')]",
    "//button[contains(translate(normalize-space(.),'VERIFY ','verify'),'verify')]",
    "//form//button[@type='submit']",
]

NAV = ("/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/"
       "div/header/cigna-primary-nav/nav/div[3]")
X_CLAIMS = NAV + "/div/cigna-nav-item/a"
X_SEARCH_CLAIM = NAV + "/cigna-nav-links/div[1]/cigna-nav-item/a"

SEARCH_FORM = ('//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/'
               'div/cigna-search-type-patient/form')
X_TAB_ID_DOB = SEARCH_FORM + "/div[1]/fieldset/div[1]/div[1]/label/i"
X_TAB_NAME_DOB = SEARCH_FORM + "/div[1]/fieldset/div[1]/div[2]/label/i"
X_TAB_NAME_ID = SEARCH_FORM + "/div[1]/fieldset/div[1]/div[3]/label/i"

X_FIRST = '//*[@id="firstName"]'
X_LAST = '//*[@id="lastName"]'
X_DOB = '//*[@id="dob"]'
X_MEMBER_ID = '//*[@id="memberId"]'

X_SEARCH_BTN = SEARCH_FORM + "/div[2]/fieldset/div/div[6]/button[1]"
X_SEARCH_BTN_ANY = [
    X_SEARCH_BTN,
    SEARCH_FORM + "/div[2]/fieldset/div/div[7]/button[1]",
    SEARCH_FORM + ("//button[contains(translate(normalize-space(.),"
                   "'SEARCH','search'),'search')]"),
]

X_RESULT_ROWS = ("/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/"
                 "div/cigna-page/cigna-search-wrapper/cigna-main-screen/"
                 "cigna-results-table/table/tbody/tr")

CD = ("/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/"
      "div/cigna-page/cigna-claim-details-screen/cigna-claims-details/"
      "div/cigna-claims-details-medical")
INFO = CD + "/cigna-medical-claims-information/div/cigna-tab-group/div/div/div"
X_CLAIM_SCREEN = "//cigna-claim-details-screen"
X_CLAIM_NO = INFO + "/div[1]/table/tbody/tr[1]/td"
X_DATE_PROCESSED = INFO + "/div[1]/table/tbody/tr[6]/td"
X_CLAIM_STATUS = INFO + "/div[1]/table/tbody/tr[7]/td/cigna-claim-status/cigna-status-badge/div/div[2]"
X_AMT_DUE = INFO + "/div[2]/table/tbody/tr[1]/td"
X_AMT_PAID = INFO + "/div[2]/table/tbody/tr[2]/td"
X_PROVIDER_PAY = INFO + "/div[2]/table/tbody/tr[3]/td"
X_PATIENT_RESP = INFO + "/div[2]/table/tbody/tr[4]/td"

X_PROC_ROWS = CD + "/div[4]/cigna-medical-procedure-details/div/table/tbody/tr"
X_EXPL_BLOCKS = CD + "/div[5]/cigna-medical-explanation-details/div[1]/div"
X_RETURN = ("/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/"
            "div/cigna-page/cigna-claim-details-screen/cigna-breadcrumbs/div/div[1]/a")

NO_RESULT_MARKERS = (
    "no results", "no claims", "no patient", "no records", "0 results",
    "no matches", "nothing found",
)

# ---------------------------------------------------------------------------
# EXCEL COLUMN ALIASES (normalized: lower-case, letters + digits only)
# ---------------------------------------------------------------------------
INPUT_ALIASES = {
    "invoice": {"invoice", "invoiceno", "invoicenumber", "invno", "invoicenum"},
    "first": {"patientfirstname", "firstname", "first", "givenname"},
    "last": {"patientlastname", "lastname", "last", "surname", "familyname"},
    "patient_name": {"patientname", "membername", "fullname", "name",
                     "patientnamefirstnamelastname", "patientnamefirstlast"},
    "dob": {"dob", "dateofbirth", "birthdate", "birthday"},
    "insurance": {"insurancename", "insurance", "payer", "plan"},
    "cigna_id": {"cignaid", "cignapatientid", "memberid", "patientid",
                 "membernumber", "id"},
    "dos": {"dos", "dateofservice", "dateofservices", "servicedate", "dosfrom",
            "dateofservicedos"},
    "cpt": {"cptcode", "cpt", "procedurecode", "proccode", "cptcodes"},
    "billed": {"billedamount", "billed", "chargeamount", "amountbilled"},
    "notes1": {"notes1", "note1", "notes"},
    "notes2": {"notes2", "note2"},
    "denied_cpt": {"deniedcpt", "deniedcpts", "denied"},
    "remark_codes": {"remarkcodes", "remarkcode", "remarks", "adjustmentcodes"},
    "status": {"status", "claimstatus", "result", "automationstatus"},
}

MASTER_ALIASES = {
    "cpt": {"cpt", "cptcode", "procedurecode", "code", "proccode"},
    "remark": {"remarkcodes", "remarkcode", "remarks", "remarkreason", "reason",
               "description", "explanation"},
    "status": {"status", "claimstatus", "result"},
}

LOGIN_ALIASES = {
    "username": {"username", "user", "userid", "login", "email"},
    "password": {"password", "pass", "pwd"},
}

OUTPUT_TITLES = {
    "notes1": "Notes1",
    "notes2": "Notes2",
    "denied_cpt": "Denied CPT",
    "remark_codes": "Remark Codes",
    "status": "Status",
}

# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------
def clean(value: Any) -> str:
    """Collapse whitespace and NBSP. None -> ''."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def norm_header(value: Any) -> str:
    """Header normalizer: 'Invoice #' -> 'invoice', 'Cigna ID' -> 'cignaid'."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def el_text(el) -> str:
    try:
        text = el.text
        if not text or not text.strip():
            text = el.get_attribute("textContent")
        return clean(text)
    except (StaleElementReferenceException, WebDriverException):
        return ""


def plain(value: Any) -> str:
    """Excel value -> string without a trailing '.0' on integer numbers."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    return str(value) if value is not None else ""


def as_id(value: Any) -> str:
    return re.sub(r"\s+", "", plain(value))


def as_code(value: Any) -> str:
    text = clean(plain(value)).upper()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def parse_date(value: Any) -> datetime | None:
    """Parse common US / ISO / Excel date formats. Returns date at midnight."""
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
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y",
                "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
    if match:
        month, day, year = (int(x) for x in match.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day)
        except ValueError:
            return None
    return None


def fmt_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%m/%d/%Y") if parsed else ""


def to_amount(value: Any) -> float:
    match = re.search(r"-?\d[\d,]*\.?\d*", clean(value))
    if not match:
        return 0.0
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return 0.0


def money(value: Any) -> str:
    text = clean(value)
    if "$" in text:
        return text
    return f"${to_amount(text):,.2f}"


def split_codes(value: Any) -> list[str]:
    """'197, 198 - denied' -> ['197', '198']."""
    stopwords = {"DENIED", "DENIAL", "PAID", "PAYMENT", "ADJUSTED", "REASON",
                 "CODES", "CODE", "NOT", "COVERED", "EXPLANATION", "REMARK"}
    text = clean(value).upper()
    if not text:
        return []
    text = re.split(r"\s[-\u2013\u2014]\s", text)[0]
    out: list[str] = []
    for token in re.split(r"[,\s;/|]+", text):
        token = token.strip(" .:()[]")
        if (token and token not in stopwords and len(token) <= 20
                and re.search(r"[A-Z0-9]", token) and token not in out):
            out.append(token)
    return out


def safe_cell(value: Any) -> Any:
    """Make a value safe for openpyxl (no illegal chars / accidental formulas)."""
    if value is None:
        return None
    if isinstance(value, (int, float, datetime, date)):
        return value
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    text = text[:32760]
    if text.startswith("="):
        text = "'" + text
    return text if text else None


# ---------------------------------------------------------------------------
# BROWSER
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, headless: bool, profile_dir: Path, cookie_file: Path,
                 shot_dir: Path, debug: bool = False):
        self.cookie_file = Path(cookie_file)
        self.shot_dir = Path(shot_dir)
        self.debug = debug
        self.shot_dir.mkdir(parents=True, exist_ok=True)

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-features=PasswordLeakDetection,AutofillServerCommunication")
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
                "Could not start Chrome. Close every Chrome window that uses the "
                f"profile folder '{profile_dir}' and try again.\n{exc}"
            ) from exc

        self.d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.d.implicitly_wait(0)
        try:
            self.d.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
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

    def find(self, xpath: str, timeout: float = WAIT):
        return WebDriverWait(self.d, max(0.1, timeout)).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )

    def present(self, xpath: str, timeout: float = 0.5) -> bool:
        try:
            WebDriverWait(self.d, max(0.1, timeout)).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            return True
        except (TimeoutException, WebDriverException):
            return False

    def displayed(self, xpath: str, timeout: float = 0.5) -> bool:
        try:
            WebDriverWait(self.d, max(0.1, timeout)).until(
                EC.visibility_of_element_located((By.XPATH, xpath))
            )
            return True
        except (TimeoutException, WebDriverException):
            return False

    def click(self, xpath: str, timeout: float = WAIT):
        """Click with scroll, retry, stale handling and a JS fallback."""
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                el = WebDriverWait(self.d, 3).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                try:
                    self.d.execute_script(
                        "arguments[0].scrollIntoView({block:'center',inline:'center'});", el
                    )
                except WebDriverException:
                    pass
                try:
                    el.click()
                    return el
                except (ElementClickInterceptedException, ElementNotInteractableException,
                        StaleElementReferenceException, WebDriverException) as exc:
                    last = exc
                    try:
                        self.d.execute_script("arguments[0].click();", el)
                        return el
                    except WebDriverException as exc2:
                        last = exc2
            except (TimeoutException, StaleElementReferenceException, WebDriverException) as exc:
                last = exc
            time.sleep(0.4)
        raise TimeoutException(f"Could not click element: {xpath} ({last})")

    def click_first(self, xpaths: Sequence[str], timeout: float = WAIT) -> bool:
        for xpath in xpaths:
            try:
                self.click(xpath, min(6, max(1, timeout)))
                return True
            except (TimeoutException, WebDriverException):
                continue
        return False

    def _input_for(self, el):
        tag = (el.tag_name or "").lower()
        if tag in ("input", "textarea"):
            return el
        for expr in (".//input[not(@type='hidden')]|.//textarea",
                     "./ancestor::label[1]//input[not(@type='hidden')]",
                     "./following::input[not(@type='hidden')][1]"):
            found = el.find_elements(By.XPATH, expr)
            if found:
                return found[0]
        field_id = el.get_attribute("for")
        if field_id:
            try:
                return self.d.find_element(By.ID, field_id)
            except WebDriverException:
                pass
        return el

    def set_text(self, xpath: str, value: Any, timeout: float = WAIT,
                 human: bool = False) -> None:
        """Type into an input; also accepts a <label> XPath. JS fallback for Angular."""
        text = str(value)
        el = self._input_for(self.find(xpath, timeout))
        try:
            if (el.get_attribute("type") or "").lower() == "date":
                parsed = parse_date(text)
                if parsed:
                    text = parsed.strftime("%Y-%m-%d")   # HTML date input format
        except WebDriverException:
            pass
        try:
            el.click()
            el.send_keys(Keys.CONTROL, "a")
            el.send_keys(Keys.DELETE)
            if human:
                for char in text:
                    el.send_keys(char)
                    time.sleep(0.02)
            else:
                el.send_keys(text)
        except (ElementNotInteractableException, StaleElementReferenceException,
                WebDriverException):
            pass

        current = el.get_attribute("value") or ""
        if clean(current) != clean(text):
            try:
                self.d.execute_script(
                    """
                    const el = arguments[0], val = arguments[1];
                    el.focus();
                    const proto = el.tagName === 'TEXTAREA'
                        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    """,
                    el, text,
                )
            except WebDriverException:
                try:
                    ActionChains(self.d).send_keys(text).perform()
                except WebDriverException:
                    pass

    def text(self, xpath: str, timeout: float = 8) -> str:
        try:
            return el_text(self.find(xpath, timeout))
        except (TimeoutException, StaleElementReferenceException, WebDriverException):
            return ""

    def screenshot(self, name: str) -> None:
        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.d.save_screenshot(str(self.shot_dir / f"{stamp}_{safe}.png"))
        except (WebDriverException, OSError):
            pass

    def dump_html(self, name: str) -> None:
        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (self.shot_dir / f"{stamp}_{safe}.html").write_text(
                self.d.page_source, encoding="utf-8"
            )
        except (WebDriverException, OSError):
            pass

    def close_cookie_banner(self) -> None:
        for _ in range(2):
            try:
                for el in self.d.find_elements(By.XPATH, X_COOKIE_CLOSE):
                    if el.is_displayed():
                        self.d.execute_script("arguments[0].click();", el)
                        time.sleep(0.3)
            except WebDriverException:
                return

    # --------------------------------------------------------------- session
    def save_session(self) -> None:
        try:
            self.d.execute_cdp_cmd("Network.enable", {})
            cookies = self.d.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
            storage: dict[str, str] = {}
            try:
                storage = self.d.execute_script(
                    "return Object.assign({}, window.localStorage);"
                ) or {}
            except WebDriverException:
                pass
            payload = {"saved_at": time.time(), "cookies": cookies,
                       "local_storage": storage, "url": self.d.current_url}
            self.cookie_file.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            log.info("Session saved (%d cookies).", len(cookies))
        except (WebDriverException, OSError) as exc:
            log.warning("Could not save session: %s", exc)

    def load_session(self) -> bool:
        if not self.cookie_file.exists():
            return False
        try:
            payload = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Session file is unreadable (%s) - ignored.", exc)
            return False

        saved_at = float(payload.get("saved_at") or 0)
        if saved_at and time.time() - saved_at > SESSION_VALID_DAYS * 86400:
            log.info("Saved session is older than %d days - ignored.", SESSION_VALID_DAYS)
            return False

        self.go(PORTAL_URL)
        self.close_cookie_banner()
        try:
            self.d.execute_cdp_cmd("Network.enable", {})
            allowed = {"name", "value", "domain", "path", "secure", "httpOnly",
                       "sameSite", "expires", "priority", "sameParty",
                       "sourceScheme", "sourcePort"}
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
                    self.d.execute_cdp_cmd("Network.setCookies", {"cookies": params})
                except WebDriverException:
                    for item in params:            # skip individual bad cookies
                        try:
                            self.d.execute_cdp_cmd("Network.setCookies", {"cookies": [item]})
                        except WebDriverException:
                            pass

            storage = payload.get("local_storage") or {}
            if storage:
                try:
                    self.d.execute_script("""
                        const data = arguments[0];
                        for (const k in data) { try { localStorage.setItem(k, data[k]); } catch (e) {} }
                    """, storage)
                except WebDriverException:
                    pass
            log.info("Loaded saved session (%d cookies).", len(params))
            return True
        except (WebDriverException, ValueError) as exc:
            log.warning("Could not restore session: %s", exc)
            return False

    # ----------------------------------------------------------------- login
    def detect_state(self) -> str:
        if self.present("//cigna-primary-nav", 0.3) or self.present(X_CLAIMS, 0.3):
            return "logged_in"
        if self.displayed(X_OTP, 0.3):
            return "otp"
        if self.displayed(X_PASSWORD, 0.3):
            return "password"
        if self.displayed(X_USERNAME_INPUT, 0.3) or self.displayed(X_USERNAME_LABEL, 0.3):
            return "username"
        if self.present(X_LOGIN_BTN, 0.4):
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

    def wait_logged_in(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.close_cookie_banner()
            if self.is_logged_in():
                return True
            time.sleep(0.5)
        return False

    def session_is_valid(self, timeout: float = 45) -> bool:
        """Open the login page with saved cookies. True if the portal loads."""
        self.go(LOGIN_URL)
        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            self.close_cookie_banner()
            state = self.detect_state()
            if state == "logged_in":
                return True
            if state == "login_btn" and not clicked:
                try:
                    self.click_first(X_LOGIN_BTN_ANY, 12)
                except (TimeoutException, WebDriverException):
                    pass
                clicked = True
            elif state in ("username", "password", "otp"):
                return False
            time.sleep(1)
        return False

    def _prompt_otp(self, attempt: int) -> str:
        print("\n" + "=" * 62)
        print(f" OTP REQUIRED - enter the code from the authenticator (try {attempt}/3).")
        print("=" * 62)
        try:
            return re.sub(r"\D", "", input("OTP: "))
        except EOFError as exc:
            raise RuntimeError(
                "OTP input is not available. Run the script from a CMD/PowerShell "
                "window so the OTP can be typed."
            ) from exc

    def login(self, username: str, password: str, overall_timeout: float = 360) -> None:
        self.go(LOGIN_URL)
        self.close_cookie_banner()
        deadline = time.time() + overall_timeout
        last_state = None
        otp_attempts = 0

        while time.time() < deadline:
            self.close_cookie_banner()
            state = self.detect_state()
            if state != last_state:
                log.info("Login state: %s", state)
                last_state = state

            if state == "logged_in":
                log.info("Logged in successfully.")
                self.save_session()
                return

            if state == "login_btn":
                self.click_first(X_LOGIN_BTN_ANY, 12)
                self.wait_state_change(state, 15)

            elif state == "username":
                target = X_USERNAME_INPUT if self.present(X_USERNAME_INPUT, 0.5) else X_USERNAME_LABEL
                self.set_text(target, re.sub(r"\s+", "", username))
                if not self.click_first(X_NEXT_ANY, 15):
                    raise TimeoutException("Username 'Next' button not found")
                self.wait_state_change(state, 15)

            elif state == "password":
                self.set_text(X_PASSWORD, re.sub(r"\s+", "", password))
                if not self.click_first(X_CONTINUE_PWD_ANY, 15):
                    raise TimeoutException("Password 'Continue' button not found")
                self.wait_state_change(state, 20)

            elif state == "otp":
                otp_attempts += 1
                if otp_attempts > 3:
                    raise RuntimeError("OTP failed three times - stopping.")
                otp = ""
                while not otp:
                    otp = self._prompt_otp(otp_attempts)
                    if not otp:
                        print("OTP cannot be empty.")
                deadline = max(deadline, time.time() + 120)
                self.set_text(X_OTP, otp)
                self.tick_remember_me()
                if not self.click_first(X_CONTINUE_OTP_ANY, 15):
                    raise TimeoutException("OTP 'Continue' button not found")
                self.wait_state_change(state, 20)

            else:
                time.sleep(1)

        raise TimeoutException("Login did not complete within the time limit.")

    def tick_remember_me(self) -> None:
        try:
            boxes = self.d.find_elements(
                By.XPATH, X_REMEMBER + "/ancestor::label//input[@type='checkbox']"
            )
            if boxes:
                if not boxes[0].is_selected():
                    self.d.execute_script("arguments[0].click();", boxes[0])
            else:
                el = self.find(X_REMEMBER, 3)
                self.d.execute_script("arguments[0].click();", el)
            log.info("Remember-me enabled.")
        except (TimeoutException, WebDriverException, StaleElementReferenceException):
            log.info("Remember-me checkbox not found (skipped).")

    def ensure_logged_in(self, username: str, password: str) -> None:
        if self.is_logged_in():
            return
        if self.session_is_valid():
            log.info("Saved session is still valid - credentials not needed.")
            self.save_session()
            return
        log.info("Session missing/expired - starting full login.")
        self.login(username, password)


    # ---------------------------------------------------------- patient search
    def open_search(self) -> None:
        self.close_cookie_banner()
        if self.present(SEARCH_FORM, 1):
            return
        if not self.click_first([X_CLAIMS], WAIT):
            raise TimeoutException("Claims menu item not found - is the session logged in?")
        time.sleep(1)
        if self.present(SEARCH_FORM, 1):
            return
        try:                                    # the sub-menu opens on hover
            ActionChains(self.d).move_to_element(self.find(X_CLAIMS, 5)).perform()
            time.sleep(0.6)
        except (TimeoutException, WebDriverException):
            pass
        if not self.click_first([X_SEARCH_CLAIM], 12):
            self.click_first([X_CLAIMS], 8)
        if not self.present(SEARCH_FORM, 20):
            raise TimeoutException("Search Claim page did not open.")

    def select_search_type(self, kind: str) -> None:
        tabs = {"id_dob": X_TAB_ID_DOB, "name_dob": X_TAB_NAME_DOB,
                "name_id": X_TAB_NAME_ID}
        tab = tabs[kind]
        try:
            self.click(tab, 10)
        except TimeoutException:
            words = {
                "id_dob": "//label[contains(., 'Cigna') and contains(., 'birth')]",
                "name_dob": "//label[contains(., 'Name') and contains(., 'birth')]",
                "name_id": "//label[contains(., 'Name') and contains(., 'Cigna')]",
            }
            self.click(words[kind], 8)
        time.sleep(0.4)

    def fill_search(self, kind: str, patient: dict[str, str]) -> None:
        if kind in ("id_dob", "name_id"):
            self.set_text(X_MEMBER_ID, patient["cigna_id"])
        if kind in ("name_dob", "name_id"):
            self.set_text(X_FIRST, patient["first"])
            self.set_text(X_LAST, patient["last"])
        if kind in ("id_dob", "name_dob"):
            self.set_text(X_DOB, patient["dob"])

    def click_search(self) -> None:
        if not self.click_first(X_SEARCH_BTN_ANY, 15):
            raise TimeoutException("Patient Search button not found.")

    def _results_text(self) -> str:
        try:
            containers = self.d.find_elements(By.ID, "results-top")
            if containers:
                return clean(containers[0].text).lower()
            return clean(self.d.find_element(By.TAG_NAME, "body").text).lower()
        except WebDriverException:
            return ""

    def wait_results(self, timeout: float = WAIT,
                     old_signature: tuple | None = None):
        """Wait for the result table to reload after a search.

        The previous table can still be on screen for a moment, so when a
        signature is supplied we wait until the rows actually change.
        """
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            rows = self.d.find_elements(By.XPATH, X_RESULT_ROWS)
            if rows:
                if old_signature is None:
                    return rows
                signature = self.result_signature(rows)
                if signature and signature != old_signature:
                    return rows
                if time.time() - start > 10:      # identical query / slow table
                    return rows
            else:
                body = self._results_text()
                if any(marker in body for marker in NO_RESULT_MARKERS):
                    if old_signature is None or time.time() - start > 1:
                        return []
            time.sleep(0.5)
        return self.d.find_elements(By.XPATH, X_RESULT_ROWS)

    def result_signature(self, rows=None) -> tuple:
        if rows is None:
            rows = self.d.find_elements(By.XPATH, X_RESULT_ROWS)
        signature = []
        for index in range(1, len(rows) + 1):
            dos = self.text(f"({X_RESULT_ROWS})[{index}]/td[6]", 0.5)
            claim_no = self.text(f"({X_RESULT_ROWS})[{index}]/td[2]", 0.5)
            signature.append((dos, claim_no))
        return tuple(signature)

    def list_results(self, rows=None) -> list["Result"]:
        if rows is None:
            rows = self.d.find_elements(By.XPATH, X_RESULT_ROWS)
        results: list[Result] = []
        for index in range(1, len(rows) + 1):
            dos = self.text(f"({X_RESULT_ROWS})[{index}]/td[6]", 1)
            claim_no = self.text(f"({X_RESULT_ROWS})[{index}]/td[2]", 1)
            results.append(Result(index=index, dos=dos, claim_no=claim_no))
        return results

    def search_patient(self, patient: dict[str, str],
                       forced_type: str | None = None) -> list["Result"]:
        kinds: list[str] = []
        if forced_type:
            kinds = [forced_type]
        else:
            if patient["dob"] and patient["cigna_id"]:
                kinds.append("id_dob")
            if patient["first"] and patient["last"] and patient["dob"]:
                kinds.append("name_dob")
            if patient["first"] and patient["last"] and patient["cigna_id"]:
                kinds.append("name_id")
        if not kinds:
            raise ValueError("Patient row has no usable search keys (DOB / Cigna ID / name).")

        self.open_search()
        for kind in kinds:
            try:
                self.select_search_type(kind)
                self.fill_search(kind, patient)
                old_signature = self.result_signature()
                self.click_search()
                rows = self.wait_results(WAIT, old_signature=old_signature)
                if rows:
                    results = self.list_results(rows)
                    log.info("   search %s -> %d claim row(s)", kind, len(results))
                    return results
                log.info("   search %s -> no results", kind)
            except (TimeoutException, WebDriverException) as exc:
                log.warning("   search %s failed: %s", kind, exc)
                try:
                    self.open_search()
                except (TimeoutException, WebDriverException):
                    pass
        return []

    # ------------------------------------------------------------- claim page
    def claim_href(self, index: int) -> str:
        try:
            link = self.d.find_element(
                By.XPATH, f"({X_RESULT_ROWS})[{index}]/td[2]//a"
            )
            href = link.get_attribute("href") or ""
            return "" if href.lower().startswith("javascript") else href
        except (WebDriverException, StaleElementReferenceException):
            return ""

    def wait_claim_page(self, timeout: float = 30) -> bool:
        try:
            WebDriverWait(self.d, timeout).until(
                EC.presence_of_element_located((By.XPATH, X_CLAIM_SCREEN))
            )
            return True
        except (TimeoutException, WebDriverException):
            return bool(self.d.find_elements(By.XPATH, X_CLAIM_NO))

    def load_claim(self, index: int, patient: dict[str, str]):
        """Open a result row. Returns (claim, needs_back).

        The href is opened in a new tab when possible, so the search results stay
        alive. If that fails the link is clicked and the caller must go back.
        """
        href = self.claim_href(index)
        if href:
            main = self.d.current_window_handle
            opened = False
            try:
                self.d.switch_to.new_window("tab")
                opened = True
                self.go(href)
                if self.wait_claim_page(25):
                    time.sleep(1)
                    claim = self.scrape_claim()
                    if claim.claim_no or claim.procs:
                        return claim, False
            except (TimeoutException, WebDriverException) as exc:
                log.warning("   direct claim link failed (%s) - clicking the row.", exc)
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

        self.click(f"({X_RESULT_ROWS})[{index}]/td[2]/div/a", 15)
        if not self.wait_claim_page(30):
            raise TimeoutException("Claim details page did not open.")
        time.sleep(1)
        return self.scrape_claim(), True

    def scrape_claim(self) -> "Claim":
        claim_no = self.text(X_CLAIM_NO, 10) or self.info_value("claim", "number")
        date_processed = (self.text(X_DATE_PROCESSED, 5)
                          or self.info_value("date", "processed"))
        status = (self.text(X_CLAIM_STATUS, 5)
                  or self.info_value("claim", "status", exclude=("reason",)))
        amt_due = self.text(X_AMT_DUE, 5) or self.info_value("amount", "due")
        amt_paid = self.text(X_AMT_PAID, 5) or self.info_value("amount", "paid")
        provider_pay = (self.text(X_PROVIDER_PAY, 5)
                        or self.info_value("provider", "payment"))
        patient_resp = (self.text(X_PATIENT_RESP, 5)
                        or self.info_value("patient", "responsibility"))

        claim = Claim(
            claim_no=claim_no,
            date_processed=date_processed,
            status=status,
            amt_due=amt_due,
            amt_paid=amt_paid,
            provider_pay=provider_pay,
            patient_resp=patient_resp,
        )
        claim.check_no = self.find_check_number()

        proc_xpath = X_PROC_ROWS
        if not self.d.find_elements(By.XPATH, proc_xpath):
            alt = "//cigna-medical-procedure-details//table/tbody/tr"
            if self.d.find_elements(By.XPATH, alt):
                proc_xpath = alt

        deadline = time.time() + 12
        while time.time() < deadline:
            if self.d.find_elements(By.XPATH, proc_xpath):
                break
            time.sleep(0.5)

        for row in self.d.find_elements(By.XPATH, proc_xpath):
            try:
                cells = row.find_elements(By.XPATH, "./td")
                if not cells:
                    continue
                cpt = as_code(el_text(cells[0]))
                dos = el_text(cells[1]) if len(cells) > 1 else ""
                codes = split_codes(el_text(cells[11])) if len(cells) > 11 else []
                if cpt or codes:
                    claim.procs.append(Procedure(cpt=cpt, dos=dos, codes=codes))
            except (StaleElementReferenceException, WebDriverException):
                continue

        claim.expl = self.scrape_explanations()
        return claim

    def info_value(self, *keywords: str, exclude: Sequence[str] = ()) -> str:
        """Find a claim-information value by words in its row label."""
        try:
            rows = self.d.find_elements(By.XPATH, INFO + "//table/tbody/tr")
            for row in rows:
                cells = row.find_elements(By.XPATH, "./th|./td")
                if len(cells) < 2:
                    continue
                label = el_text(cells[0]).lower()
                if all(word in label for word in keywords) and not any(
                        word in label for word in exclude):
                    value = el_text(cells[-1])
                    if value:
                        return value
        except (StaleElementReferenceException, WebDriverException):
            pass
        return ""

    def scrape_explanations(self) -> dict[str, str]:
        explanations: dict[str, str] = {}
        try:
            blocks = self.d.find_elements(By.XPATH, X_EXPL_BLOCKS)
            if not blocks:
                blocks = self.d.find_elements(
                    By.XPATH, "//cigna-medical-explanation-details/div[1]/div"
                )
        except WebDriverException:
            return explanations
        for block in blocks:
            try:
                raw_code = el_text(block.find_element(By.XPATH, "./div[1]"))
                if not raw_code:
                    continue
                desc_el = block.find_elements(By.XPATH, "./div[2]//p")
                if not desc_el:
                    desc_el = block.find_elements(By.XPATH, "./div[2]")
                desc = " ".join(text for text in (el_text(x) for x in desc_el) if text)
                for code in split_codes(raw_code):
                    explanations.setdefault(code, desc)
            except (StaleElementReferenceException, WebDriverException):
                continue
        return explanations

    def find_check_number(self) -> str:
        if CHECK_NUMBER_XPATH:
            return self.text(CHECK_NUMBER_XPATH, 3)
        candidates: list[str] = []
        try:
            rows = self.d.find_elements(By.XPATH, INFO + "//table/tbody/tr")
            for row in rows:
                cells = row.find_elements(By.XPATH, "./th|./td")
                if len(cells) < 2:
                    continue
                label = el_text(cells[0]).lower()
                if "check" in label and "date" not in label and "amount" not in label:
                    value = el_text(cells[-1])
                    if value:
                        candidates.append(value)
        except (StaleElementReferenceException, WebDriverException):
            pass
        return candidates[0] if candidates else ""

    def back_to_results(self, patient: dict[str, str]) -> bool:
        try:
            self.click(X_RETURN, 10)
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.d.find_elements(By.XPATH, X_RESULT_ROWS):
                    return True
                time.sleep(0.5)
        except (TimeoutException, WebDriverException):
            pass
        try:                                    # recover by running the search again
            return bool(self.search_patient(patient))
        except (TimeoutException, WebDriverException):
            return False


# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------
log = logging.getLogger("cigna")


@dataclass
class Result:
    index: int
    dos: str = ""
    claim_no: str = ""


@dataclass
class Procedure:
    cpt: str
    dos: str = ""
    codes: list[str] = field(default_factory=list)


@dataclass
class Claim:
    claim_no: str = ""
    date_processed: str = ""
    status: str = ""
    amt_due: str = ""
    amt_paid: str = ""
    provider_pay: str = ""
    patient_resp: str = ""
    check_no: str = ""
    procs: list[Procedure] = field(default_factory=list)
    expl: dict[str, str] = field(default_factory=dict)

    def cpt_set(self) -> set[str]:
        return {as_code(p.cpt) for p in self.procs if p.cpt}

    def is_denied(self) -> bool:
        status = clean(self.status).lower()
        raw = [self.amt_due, self.amt_paid, self.provider_pay, self.patient_resp]
        if not any(clean(x) for x in raw):
            return "denied" in status
        return all(to_amount(x) == 0 for x in raw) or "denied" in status


def order_results(results: list[Result], excel_dos: str) -> list[Result]:
    """Excel DOS match first, then newest Date of Service."""
    target = parse_date(excel_dos)
    exact = [r for r in results if target and parse_date(r.dos) == target]
    rest = [r for r in results if r not in exact]
    rest.sort(key=lambda r: parse_date(r.dos) or datetime(1900, 1, 1), reverse=True)
    return exact + rest


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
        "notes1": f"No claim found for {name or 'patient'}{suffix} using search types 1-3.",
        "notes2": "",
        "denied_cpt": "",
        "remark_codes": "",
    }


def build_report(claim: Claim, patient: dict[str, str], book: "Book",
                 cpt_missing: bool = False) -> dict[str, str]:
    target = as_code(patient.get("cpt"))
    denied = claim.is_denied()
    kind = "DND" if denied else "PAID"

    dos = patient.get("dos") or next(
        (fmt_date(p.dos) or clean(p.dos) for p in claim.procs if clean(p.dos)), ""
    )
    paid = claim.amt_paid if to_amount(claim.amt_paid) else claim.provider_pay
    check = claim.check_no or claim.claim_no

    head = f"DOS {dos} CLM PD {money(paid)}"
    if check:
        head += f" THRU CHK #{check}"
    if claim.date_processed:
        head += f" ISSUED ON {claim.date_processed}"
    head += "."

    procs = list(claim.procs)
    procs.sort(key=lambda p: 0 if target and as_code(p.cpt) == target else 1)

    segments: list[str] = []
    for proc in procs:
        master_status = book.master_status(proc.cpt)
        keyword = kind
        if master_status.upper() in ("PAID", "DND", "DENIED"):
            # Master knows this CPT: its Status decides PAID / DND.
            keyword = "DND" if master_status.upper() in ("DND", "DENIED") else "PAID"

        segment = f"CPT {proc.cpt} {keyword}"
        if proc.codes:
            segment += " " + " ".join(proc.codes)
            note = (master_status
                    if master_status and master_status.upper() not in
                    ("PAID", "DND", "DENIED") else "")
            if not note:
                note = next((claim.expl[c] for c in proc.codes if claim.expl.get(c)), "")
            if not note and claim.expl.get(proc.cpt):
                note = claim.expl[proc.cpt]
            if note:
                segment += f" - {note}"
        elif master_status and master_status.upper() not in ("PAID", "DND", "DENIED"):
            segment += f" - {master_status}"
        segments.append(segment)

    notes1 = head
    if segments:
        notes1 += " " + "; ".join(segments)

    codes_all: list[str] = []
    for proc in claim.procs:
        for code in proc.codes:
            if code not in codes_all:
                codes_all.append(code)
    cpts = claim.cpt_set()
    for code in claim.expl:
        if code not in codes_all and code not in cpts:
            codes_all.append(code)

    notes2_lines = []
    for code in codes_all:
        desc = claim.expl.get(code, "")
        notes2_lines.append(f"{code} - {desc}" if desc else code)
    notes2 = "\n".join(notes2_lines)

    status = clean(claim.status) or ("Denied" if denied else "Paid")
    if cpt_missing:
        status = STATUS_CPT_MISSING
        if target:
            notes1 = f"CPT {target} not found in the checked claims. " + notes1

    return {
        "status": status,
        "notes1": notes1,
        "notes2": notes2,
        "denied_cpt": ", ".join(p.cpt for p in claim.procs if p.cpt) if denied else "",
        "remark_codes": ", ".join(codes_all),
    }


def process_patient(bot: Bot, book: "Book", patient: dict[str, str],
                    max_claims: int = MAX_CLAIMS, search_type: str | None = None):
    """Search, choose the best claim, check the CPT and build the report."""
    results = bot.search_patient(patient, forced_type=search_type)
    if not results:
        return not_found_report(patient), None

    ordered = order_results(results, patient.get("dos", ""))
    target = as_code(patient.get("cpt"))
    if patient.get("dos") and not any(
            parse_date(r.dos) == parse_date(patient["dos"]) for r in results):
        log.warning("   Excel DOS %s is not in the result list - newest claim used.",
                    patient["dos"])
    fallback: Claim | None = None
    checked = 0

    for result in ordered[:max_claims]:
        checked += 1
        log.info("   checking claim %s (DOS %s) [%d/%d]",
                 result.claim_no or "?", result.dos or "?", checked,
                 min(max_claims, len(ordered)))
        try:
            claim, needs_back = bot.load_claim(result.index, patient)
        except (TimeoutException, WebDriverException, RuntimeError) as exc:
            log.warning("   could not open claim row %d: %s", result.index, exc)
            try:
                if not bot.present(SEARCH_FORM, 1):
                    bot.back_to_results(patient)
            except (TimeoutException, WebDriverException):
                pass
            continue

        try:
            if fallback is None:
                fallback = claim
            if not target or target in claim.cpt_set():
                return build_report(claim, patient, book), claim
            log.info("   CPT %s not in this claim - trying the next claim", target)
        finally:
            if needs_back:
                bot.back_to_results(patient)

    if fallback is not None:
        log.warning("   CPT %s was not found in %d checked claim(s).", target, checked)
        return build_report(fallback, patient, book, cpt_missing=bool(target)), fallback

    raise RuntimeError("None of the claim result rows could be opened.")


# ---------------------------------------------------------------------------
# EXCEL WORKBOOK
# ---------------------------------------------------------------------------
class Book:
    def __init__(self, path: Path, sheet_input: str = SHEET_INPUT,
                 sheet_master: str = SHEET_MASTER, sheet_login: str = SHEET_LOGIN,
                 dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.wb = load_workbook(str(self.path))
        self.ws_in = self._sheet(sheet_input)
        self.ws_master = self._sheet(sheet_master)
        self.ws_login = self._sheet(sheet_login)

        self.in_header = self._detect_header(self.ws_in, INPUT_ALIASES)
        self.cols = self._map_columns(self.ws_in, self.in_header, INPUT_ALIASES)
        for logical, title in OUTPUT_TITLES.items():
            self._ensure_column(self.ws_in, self.cols, logical, title, self.in_header)

        self.master_header = self._detect_header(self.ws_master, MASTER_ALIASES)
        self.mcols = self._map_columns(self.ws_master, self.master_header, MASTER_ALIASES)
        for logical, title in (("cpt", "CPT"), ("remark", "Remark Codes"),
                               ("status", "Status")):
            self._ensure_column(self.ws_master, self.mcols, logical, title,
                                self.master_header)

        self.master: dict[str, dict[str, Any]] = {}
        for row in range(self.master_header + 1, self.ws_master.max_row + 1):
            cpt = as_code(self._cell(self.ws_master, row, self.mcols.get("cpt")))
            if not cpt:
                continue
            self.master.setdefault(cpt, {
                "row": row,
                "remark": clean(self._cell(self.ws_master, row, self.mcols.get("remark"))),
                "status": clean(self._cell(self.ws_master, row, self.mcols.get("status"))),
            })

        has_name = any(k in self.cols for k in ("first", "last", "patient_name"))
        has_key = any(k in self.cols for k in ("dob", "cigna_id"))
        if not has_name or not has_key:
            raise ValueError(
                f"Sheet '{sheet_input}' needs name columns and DOB / Cigna ID columns. "
                f"Found headers: {[c.value for c in self.ws_in[self.in_header] if c.value]}"
            )

    # -------------------------------------------------------------- workbook
    def _sheet(self, name: str):
        for sheet in self.wb.sheetnames:
            if sheet.strip().lower() == name.strip().lower():
                return self.wb[sheet]
        raise KeyError(f"Sheet '{name}' not found. Available sheets: {self.wb.sheetnames}")

    @staticmethod
    def _detect_header(ws, aliases: dict[str, set[str]]) -> int:
        best_row, best_score = 1, -1
        last = min(max(ws.max_row, 1), 20)
        for row in range(1, last + 1):
            values = {
                norm_header(ws.cell(row, col).value)
                for col in range(1, ws.max_column + 1)
                if ws.cell(row, col).value
            }
            score = sum(1 for names in aliases.values() if values & names)
            if score > best_score:
                best_row, best_score = row, score
        return best_row

    @staticmethod
    def _map_columns(ws, header_row: int, aliases: dict[str, set[str]]) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for col in range(1, ws.max_column + 1):
            key = norm_header(ws.cell(header_row, col).value)
            if not key:
                continue
            for logical, names in aliases.items():
                if logical not in mapping and key in names:
                    mapping[logical] = col
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

    # ------------------------------------------------------------ credentials
    def credentials(self) -> tuple[str, str]:
        header = self._detect_header(self.ws_login, LOGIN_ALIASES)
        mapping = self._map_columns(self.ws_login, header, LOGIN_ALIASES)
        if "username" not in mapping or "password" not in mapping:
            raise ValueError(
                f"Login sheet needs Username and Password columns. "
                f"Found: {[c.value for c in self.ws_login[header] if c.value]}"
            )
        for row in range(header + 1, self.ws_login.max_row + 1):
            username = self.ws_login.cell(row, mapping["username"]).value
            password = self.ws_login.cell(row, mapping["password"]).value
            if username not in (None, "") and password not in (None, ""):
                # The workflow requires credentials with no spaces.
                return (re.sub(r"\s+", "", plain(username)),
                        re.sub(r"\s+", "", plain(password)))
        raise ValueError("Login sheet: no row contains both Username and Password.")

    # ---------------------------------------------------------------- patients
    def patient(self, row: int) -> dict[str, str]:
        first = clean(self.val(row, "first"))
        last = clean(self.val(row, "last"))
        full = clean(self.val(row, "patient_name"))

        if (not first or not last) and full:
            if "," in full:
                part_a, part_b = (clean(x) for x in full.split(",", 1))
            else:
                pieces = full.split()
                part_a = pieces[0] if pieces else ""
                part_b = pieces[-1] if len(pieces) > 1 else ""
            first = first or part_a
            last = last or part_b

        return {
            "invoice": clean(plain(self.val(row, "invoice"))),
            "first": first,
            "last": last,
            "full_name": full,
            "dob": fmt_date(self.val(row, "dob")),
            "insurance": clean(self.val(row, "insurance")),
            "cigna_id": as_id(self.val(row, "cigna_id")),
            "dos": fmt_date(self.val(row, "dos")),
            "cpt": as_code(self.val(row, "cpt")),
            "date_of_service_raw": clean(plain(self.val(row, "dos"))),
            "status": clean(self.val(row, "status")),
        }

    def pending_rows(self, retry_errors: bool = False,
                     limit: int = 0) -> list[tuple[int, dict[str, str]]]:
        retry_statuses = {STATUS_ERROR.lower(), "error", "failed",
                          STATUS_CPT_MISSING.lower()}
        pending: list[tuple[int, dict[str, str]]] = []
        for row in range(self.in_header + 1, self.ws_in.max_row + 1):
            patient = self.patient(row)
            if not any(clean(v) for v in patient.values()):
                continue
            status = patient["status"]
            if status and not (retry_errors and status.lower() in retry_statuses):
                continue

            has_name = bool(patient["first"] and patient["last"]) or bool(patient["full_name"])
            has_lookup = bool(patient["dob"] or patient["cigna_id"])
            if not (has_lookup or (has_name and (patient["dob"] or patient["cigna_id"]))):
                log.warning("Row %d skipped: no usable DOB / Cigna ID / name.", row)
                continue
            if not has_name and not (patient["dob"] and patient["cigna_id"]):
                log.warning("Row %d skipped: Type 1 needs both DOB and Cigna ID.", row)
                continue

            pending.append((row, patient))
            if limit and len(pending) >= limit:
                break
        return pending

    # ----------------------------------------------------------------- master
    def master_status(self, cpt: str) -> str:
        entry = self.master.get(as_code(cpt))
        return clean(entry.get("status")) if entry else ""

    def add_master(self, claim: Claim, status: str) -> int:
        if self.dry_run:
            return 0
        master_status = clean(status)
        if master_status.lower() in {STATUS_CPT_MISSING.lower(), STATUS_NOT_FOUND.lower()}:
            master_status = (clean(claim.status)
                             or ("Denied" if claim.is_denied() else "Paid"))
        added = 0
        for proc in claim.procs:
            cpt = as_code(proc.cpt)
            if not cpt or cpt in self.master:
                continue
            row = self.ws_master.max_row + 1
            self.ws_master.cell(row, self.mcols["cpt"]).value = cpt
            self.ws_master.cell(row, self.mcols["remark"]).value = (
                safe_cell(", ".join(proc.codes)) if proc.codes else None
            )
            self.ws_master.cell(row, self.mcols["status"]).value = (
                safe_cell(master_status) or None
            )
            self.master[cpt] = {
                "row": row,
                "remark": ", ".join(proc.codes),
                "status": master_status,
            }
            added += 1
        return added

    # ------------------------------------------------------------------ write
    def write_result(self, row: int, result: dict[str, str]) -> None:
        for logical, value in result.items():
            col = self.cols.get(logical)
            if not col:
                log.warning("Column '%s' is missing in the input sheet.", logical)
                continue
            self.ws_in.cell(row, col).value = safe_cell(value)

    def save(self) -> None:
        if self.dry_run:
            log.info("[dry-run] workbook not saved.")
            return
        for _ in range(6):
            try:
                self.wb.save(str(self.path))
                return
            except PermissionError:
                log.warning("Cigna.xlsx is open in Excel - close it. Retrying in 5s...")
                time.sleep(5)
        raise PermissionError(f"Cannot save workbook - it is locked: {self.path}")


# ---------------------------------------------------------------------------
# CLI / MAIN
# ---------------------------------------------------------------------------
def setup_logging(log_file: Path, debug: bool = False) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.insert(0, logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cigna for HCP claim status automation (Selenium + openpyxl).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL),
                        help="Path to Cigna.xlsx")
    parser.add_argument("--input-sheet", default=SHEET_INPUT)
    parser.add_argument("--master-sheet", default=SHEET_MASTER)
    parser.add_argument("--login-sheet", default=SHEET_LOGIN)
    parser.add_argument("--profile", default="",
                        help="Chrome profile folder (default: Chrome_Profile next to the workbook)")
    parser.add_argument("--rows", type=int, default=0,
                        help="Process at most N rows (0 = all)")
    parser.add_argument("--max-claims", type=int, default=MAX_CLAIMS,
                        help="How many claim results to inspect per patient")
    parser.add_argument("--search-type", choices=("id_dob", "name_dob", "name_id"),
                        default=None,
                        help="Force one patient search method instead of 1 -> 2 -> 3")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Reprocess rows whose Status is Automation Error/CPT Not Found")
    parser.add_argument("--fresh-login", action="store_true",
                        help="Delete the saved cookie file and log in again")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chrome headless (OTP login needs a visible browser)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Search and scrape but do not write/save the workbook")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose log + save HTML on errors")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    excel = Path(args.excel).expanduser()
    if not excel.is_absolute():
        excel = (Path.cwd() / excel).resolve()
    if not excel.exists():
        print(f"ERROR: Excel file not found: {excel}")
        print("Use: python cigna_claim_automation_fixed.py --excel \"C:\\path\\Cigna.xlsx\"")
        return 2

    base = excel.parent
    profile_dir = Path(args.profile).expanduser() if args.profile else base / "Chrome_Profile"
    cookie_file = base / "cigna_session.json"
    shot_dir = base / "screenshots"
    log_file = base / "cigna_automation.log"

    setup_logging(log_file, args.debug)
    log.info("=" * 68)
    log.info("Cigna claim automation - workbook: %s", excel)
    log.info("=" * 68)

    try:
        book = Book(excel, args.input_sheet, args.master_sheet, args.login_sheet,
                    dry_run=args.dry_run)
        username, password = book.credentials()
    except (KeyError, ValueError, OSError) as exc:
        log.error("Workbook error: %s", exc)
        return 2

    rows = book.pending_rows(retry_errors=args.retry_errors, limit=max(0, args.rows))
    log.info("%d pending row(s).", len(rows))
    if not rows:
        log.info("Nothing to do.")
        return 0

    if args.fresh_login and cookie_file.exists():
        try:
            cookie_file.unlink()
            log.info("Saved cookies removed (--fresh-login).")
        except OSError as exc:
            log.warning("Could not delete %s: %s", cookie_file, exc)

    try:
        bot = Bot(headless=args.headless, profile_dir=profile_dir,
                  cookie_file=cookie_file, shot_dir=shot_dir, debug=args.debug)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    ok = not_found = errors = master_added = 0
    try:
        bot.load_session()
        if not bot.is_logged_in():
            if not bot.session_is_valid():
                log.info("No valid session - credentials are required.")
                bot.login(username, password)

        for number, (row, patient) in enumerate(rows, 1):
            label = (f"[{number}/{len(rows)}] row {row} | Invoice {patient['invoice'] or '-'} | "
                     f"{patient['first']} {patient['last']} | DOB {patient['dob'] or '-'} | "
                     f"Cigna ID {patient['cigna_id'] or '-'} | CPT {patient['cpt'] or '-'}")
            log.info(label)

            report: dict[str, str] | None = None
            claim: Claim | None = None

            for attempt in range(1, ROW_RETRIES + 1):
                try:
                    bot.ensure_logged_in(username, password)
                    report, claim = process_patient(
                        bot, book, patient, max_claims=max(1, args.max_claims),
                        search_type=args.search_type,
                    )
                    added = book.add_master(claim, report["status"]) if claim else 0
                    master_added += added

                    if args.dry_run:
                        log.info("[dry-run] status=%s | codes=%s | new master=%d",
                                 report["status"], report["remark_codes"] or "-", added)
                        log.info("[dry-run] Notes1: %s", report["notes1"])
                    else:
                        book.write_result(row, report)
                        book.save()
                    if report["status"] == STATUS_NOT_FOUND:
                        not_found += 1
                    else:
                        ok += 1
                    log.info("   done: status=%s | codes=%s | new master CPT(s)=%d",
                             report["status"], report["remark_codes"] or "-", added)
                    break

                except Exception as exc:                      # noqa: BLE001 - one bad row must not stop the run
                    log.exception("   attempt %d/%d failed: %s", attempt, ROW_RETRIES, exc)
                    bot.screenshot(f"row{row}_attempt{attempt}")
                    if args.debug:
                        bot.dump_html(f"row{row}_attempt{attempt}")
                    if attempt < ROW_RETRIES:
                        try:
                            bot.ensure_logged_in(username, password)
                        except Exception:                     # noqa: BLE001
                            pass
                        continue
                    errors += 1
                    if not args.dry_run:
                        book.write_result(row, {
                            "notes1": f"Automation error: {clean(exc)[:900]}",
                        })
                        try:
                            book.save()
                        except PermissionError:
                            log.error("Could not save the error note - workbook is locked.")

            try:
                bot.save_session()
            except Exception:                                 # noqa: BLE001
                pass

    except KeyboardInterrupt:
        log.warning("Interrupted by user - saving progress.")
    except Exception as exc:                                  # noqa: BLE001
        log.exception("Fatal error: %s", exc)
        errors += 1
    finally:
        try:
            if bot.is_logged_in():
                bot.save_session()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            bot.d.quit()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            book.save()
        except PermissionError:
            log.error("Final save failed - close Cigna.xlsx and run again.")

    log.info("-" * 68)
    log.info("Finished. processed=%d not_found=%d errors=%d master_added=%d",
             ok, not_found, errors, master_added)
    log.info("Log: %s | Screenshots: %s", log_file, shot_dir)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())






