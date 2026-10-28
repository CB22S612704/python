"""
Cigna Claims Automation
----------------------
Workflow:
1. Read Login, Input, and Master worksheets from Cigna.xlsx.
2. Sign in to Cigna for Healthcare Professionals (OTP is entered in the CMD prompt if requested).
3. Reuse a persistent Chrome profile so the website can retain its own 30-day login session/cookies.
4. For each Input row, search patient (DOB + Cigna ID; fall back to First/Last Name + DOB).
5. Open Claims > Search Claim, match Date of Service (DOS), inspect claim details, and compare CPTs.
6. Generate Notes1 / Notes2, update the row Status, and save Excel after each row.

IMPORTANT:
- Run only with an account you are authorized to use.
- First run is best tested with HEADLESS = False.
- The website's DOM can change. If a selector fails, update the XPath constants below.
- This script does not bypass MFA. It asks you to enter the code delivered by your normal authentication method.
"""

import os
import re
import time
import traceback
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ============================= USER SETTINGS =============================

EXCEL_PATH = r"C:\Users\Automation22\Desktop\Automation\Cigna\Cigna.xlsx"
LOGIN_URL = "https://cignaforhcp.cigna.com/app/login"

# Keep this folder between runs to reuse the same Chrome profile/session.
CHROME_PROFILE_DIR = os.path.join(os.path.dirname(EXCEL_PATH), "Chrome_Profile")

HEADLESS = False
WAIT_SECONDS = 25
PAGE_LOAD_TIMEOUT = 60
SAVE_AFTER_EACH_ROW = True

# Set True to pause and let you inspect each completed row in the browser.
PAUSE_AFTER_EACH_ROW = False

# Input worksheet column names (matching is case-insensitive and ignores extra spaces).
SHEET_INPUT = "Input"
SHEET_MASTER = "Master"
SHEET_LOGIN = "Login"

# Status values written to Input[Status].
STATUS_DONE = "Completed"
STATUS_NOT_FOUND = "Patient/Claim Not Found"
STATUS_ERROR = "Failed"

# ================================ XPATHS ================================

X = {
    "cookie_close": '//*[@id="onetrust-close-btn-container"]/button',

    # Login / MFA
    "login_button": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-public/div/main/cigna-page/div/div[1]/div/cigna-login-form-auth0/div/div[2]/button',
    "username": '//*[@id="username-label"]',
    "username_next": '/html/body/main/section/div/div/div/div[1]/div/form/div[4]/button',
    "password": '//*[@id="password"]',
    "password_continue": '/html/body/main/section/div/div/div/form/div[5]/button',
    "otp": '//*[@id="code"]',
    "remember_me": '/html/body/main/section/div/div/div/div/div/form/div[3]/label/span',
    "otp_continue": '/html/body/main/section/div/div/div/div/div/form/div[4]/button',

    # Patient search modes and fields
    "patient_type_dob_id": '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/div/cigna-search-type-patient/form/div[1]/fieldset/div[1]/div[1]/label/i',
    "patient_type_name_dob": '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/div/cigna-search-type-patient/form/div[1]/fieldset/div[1]/div[2]/label/i',
    "patient_type_name_id": '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/div/cigna-search-type-patient/form/div[3]/fieldset/div/div[7]/button[1]',
    "dob": '//*[@id="dob"]',
    "member_id": '//*[@id="memberId"]',
    "first_name": '//*[@id="firstName"]',
    "last_name": '//*[@id="lastName"]',
    "patient_search": '//*[@id="results-top"]/div[1]/cigna-search-form/div/cigna-tab-group/div/cigna-search-type-patient/form/div[2]/fieldset/div/div[6]/button[1]',

    # Secure navigation
    "claims_nav": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/header/cigna-primary-nav/nav/div[3]/div/cigna-nav-item/a',
    "search_claim_nav": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/header/cigna-primary-nav/nav/div[3]/cigna-nav-links/div[1]/cigna-nav-item/a',

    # Claim details
    "claim_reference": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[1]/table/tbody/tr[1]/td',
    "date_processed": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[1]/table/tbody/tr[6]/td',
    "claim_status": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[1]/table/tbody/tr[7]/td/cigna-claim-status/cigna-status-badge/div/div[2]',
    "amount_due": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[2]/table/tbody/tr[1]/td',
    "amount_paid": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[2]/table/tbody/tr[2]/td',
    "provider_payment": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[2]/table/tbody/tr[3]/td',
    "patient_responsibility": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/cigna-medical-claims-information/div/cigna-tab-group/div/div/div/div[2]/table/tbody/tr[4]/td',
    "procedure_table": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-claims-details/div/cigna-claims-details-medical/div[4]/cigna-medical-procedure-details/div/table/tbody/tr',
    "return_claims": '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/cigna-page/cigna-claim-details-screen/cigna-breadcrumbs/div/div[1]/a',
}

CLAIM_RESULTS_TABLE_ROWS = (
    '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/'
    'cigna-page/cigna-search-wrapper/cigna-main-screen/cigna-results-table/table/tbody/tr'
)
# Based on the provided page notes: result table column 2 is claim number, column 6 is DOS.
CLAIM_RESULT_CLAIM_LINK = "./td[2]/div/a"
CLAIM_RESULT_DOS = "./td[6]"

# Procedure detail table: source notes specify CPT in td[1], DOS in td[2], remark code in td[12].
PROC_CPT_CELL = "./td[1]"
PROC_DOS_CELL = "./td[2]"
PROC_REMARK_CELL = "./td[12]"

GREEN_FILL = PatternFill(fill_type="solid", fgColor="C6EFCE")
RED_FILL = PatternFill(fill_type="solid", fgColor="FFC7CE")


# =============================== UTILITIES ===============================

def norm(value):
    """Normalize a header/value for loose matching."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_date(value):
    """Convert common Excel/site date representations to mm/dd/yyyy when possible."""
    if value is None or clean(value) == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    text = clean(value)
    for fmt in (
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y",
        "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            pass
    # Preserve the original if the format is not recognized.
    return text


def normalize_money(value):
    text = clean(value)
    if not text:
        return 0.0
    text = text.replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    text = re.sub(r"[^\d.\-]", "", text)
    try:
        return float(text) if text not in ("", "-", ".", "-.") else 0.0
    except ValueError:
        return 0.0


def money_text(value):
    return f"{value:.2f}"


def safe_click(driver, element):
    try:
        element.click()
    except (ElementClickInterceptedException, WebDriverException):
        driver.execute_script("arguments[0].click();", element)


def wait_click(driver, xpath, timeout=WAIT_SECONDS, optional=False):
    try:
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        safe_click(driver, element)
        return True
    except TimeoutException:
        if optional:
            return False
        raise


def wait_element(driver, xpath, timeout=WAIT_SECONDS, optional=False):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
    except TimeoutException:
        if optional:
            return None
        raise


def get_text(driver, xpath, optional=True):
    element = wait_element(driver, xpath, timeout=8, optional=optional)
    return clean(element.text) if element else ""


def set_input(driver, xpath, value, optional=False):
    element = wait_element(driver, xpath, optional=optional)
    if element is None:
        return False
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(clean(value))
    return True


def find_header(ws, aliases, required=False):
    """Return 1-based column index using case-insensitive header aliases."""
    aliases = {norm(a) for a in aliases}
    for col in range(1, ws.max_column + 1):
        if norm(ws.cell(1, col).value) in aliases:
            return col
    if required:
        raise ValueError(
            f"Required column not found in sheet '{ws.title}'. "
            f"Expected one of: {', '.join(sorted(aliases))}"
        )
    return None


def row_value(ws, row, aliases, default=""):
    col = find_header(ws, aliases, required=False)
    return ws.cell(row, col).value if col else default


def write_value(ws, row, aliases, value, create_header=None):
    col = find_header(ws, aliases, required=False)
    if col is None:
        if not create_header:
            return False
        col = ws.max_column + 1
        ws.cell(1, col).value = create_header
    ws.cell(row, col).value = value
    return True


def get_login_credentials(wb):
    ws = wb[SHEET_LOGIN]
    username = clean(row_value(ws, 2, ["username", "user name", "login username"]))
    password = clean(row_value(ws, 2, ["password", "pass word", "login password"]))
    if not username or not password:
        raise ValueError(
            "Login sheet must contain Username and Password headers in row 1, "
            "with credentials in row 2."
        )
    return username, password


def close_cookie_banner(driver):
    try:
        wait_click(driver, X["cookie_close"], timeout=3, optional=True)
    except Exception:
        pass


def start_browser():
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    options = Options()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })
    if HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.get(LOGIN_URL)
    return driver


# ================================ LOGIN ==================================

def login_if_needed(driver, username, password):
    """
    Reuses the persistent Chrome profile. If the authenticated session is still
    valid, the login page may redirect directly into the secure application.
    Otherwise, completes username/password and requests an OTP from the user.
    """
    wait = WebDriverWait(driver, WAIT_SECONDS)
    time.sleep(2)
    close_cookie_banner(driver)

    # If already authenticated, the secure Claims navigation should be present.
    if wait_element(driver, X["claims_nav"], timeout=5, optional=True):
        print("Existing authenticated session detected.")
        return

    # Start the Auth0 login flow.
    if not wait_click(driver, X["login_button"], timeout=12, optional=True):
        # Some sessions land directly on the Auth0 username page.
        print("Login button not found; checking whether the username form is already open.")

    username_el = wait_element(driver, X["username"], timeout=WAIT_SECONDS)
    username_el.clear()
    username_el.send_keys(username)
    wait_click(driver, X["username_next"])

    password_el = wait_element(driver, X["password"], timeout=WAIT_SECONDS)
    password_el.clear()
    password_el.send_keys(password)
    wait_click(driver, X["password_continue"])

    # MFA is intentionally completed by the account holder, not bypassed.
    otp_el = wait_element(driver, X["otp"], timeout=12, optional=True)
    if otp_el:
        otp = input("Enter the Cigna authentication code shown/sent to you, then press Enter: ").strip()
        if not otp:
            raise ValueError("No authentication code entered.")
        otp_el.clear()
        otp_el.send_keys(otp)

        # Remember-me may be present. Click only if not already selected.
        try:
            remember = driver.find_element(By.XPATH, X["remember_me"])
            parent = remember.find_element(By.XPATH, "..")
            if "checked" not in (parent.get_attribute("class") or "").lower():
                safe_click(driver, remember)
        except Exception:
            pass

        wait_click(driver, X["otp_continue"])

    # Wait for secure app navigation. If a secondary challenge appears, fail with a useful message.
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, X["claims_nav"])))
    except TimeoutException:
        print("Login did not reach the secure Claims page.")
        print("If Cigna displays another security challenge, complete it manually in the browser.")
        input("After completing the challenge and reaching the secure portal, press Enter here...")
        wait.until(EC.presence_of_element_located((By.XPATH, X["claims_nav"])))

    print("Login successful. Chrome profile will retain the website session where supported.")


# ============================= PATIENT SEARCH ============================

def search_patient_type1(driver, dob, member_id):
    wait_click(driver, X["patient_type_dob_id"], timeout=10, optional=True)
    if not set_input(driver, X["dob"], normalize_date(dob), optional=True):
        return False
    if not set_input(driver, X["member_id"], member_id, optional=True):
        return False
    wait_click(driver, X["patient_search"])
    time.sleep(2)
    return patient_results_present(driver)


def search_patient_type2(driver, dob, patient_name):
    parts = clean(patient_name).split()
    if len(parts) < 2:
        first_name = parts[0] if parts else ""
        last_name = ""
    else:
        first_name = parts[0]
        last_name = " ".join(parts[1:])

    wait_click(driver, X["patient_type_name_dob"], timeout=10, optional=True)
    if not set_input(driver, X["first_name"], first_name, optional=True):
        return False
    if not set_input(driver, X["last_name"], last_name, optional=True):
        return False
    if not set_input(driver, X["dob"], normalize_date(dob), optional=True):
        return False
    wait_click(driver, X["patient_search"])
    time.sleep(2)
    return patient_results_present(driver)


def patient_results_present(driver):
    # Check for the results table or a result row. The site may render patient results differently.
    possible = [
        '//*[@id="results-top"]',
        '//*[contains(translate(., "NO PATIENT", "no patient"), "no patient")]',
    ]
    for xpath in possible:
        if wait_element(driver, xpath, timeout=3, optional=True):
            # results-top exists on the search form too; only treat the explicit no-result text as false.
            if "no patient" in xpath:
                return False
    # Patient search is considered submitted if the page is still responsive.
    return True


def navigate_to_claim_search(driver):
    wait_click(driver, X["claims_nav"])
    wait_click(driver, X["search_claim_nav"])
    # Wait until results/search page content is present.
    wait_element(driver, CLAIM_RESULTS_TABLE_ROWS, timeout=10, optional=True)


# =============================== CLAIMS ==================================

def date_matches(site_date, input_date):
    left = normalize_date(site_date)
    right = normalize_date(input_date)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def get_claim_result_rows(driver):
    try:
        return driver.find_elements(By.XPATH, CLAIM_RESULTS_TABLE_ROWS)
    except Exception:
        return []


def open_claim_matching_dos(driver, target_dos, preferred_cpt=""):
    """
    Walk through visible claim result rows and open the first claim whose DOS matches.
    If preferred_cpt is supplied, prefer a claim with that CPT when rows can be inspected.
    """
    rows = get_claim_result_rows(driver)
    if not rows:
        return False

    candidates = []
    for row in rows:
        try:
            dos_text = clean(row.find_element(By.XPATH, CLAIM_RESULT_DOS).text)
            link = row.find_element(By.XPATH, CLAIM_RESULT_CLAIM_LINK)
            if date_matches(dos_text, target_dos):
                candidates.append((row, link, dos_text))
        except (NoSuchElementException, StaleElementReferenceException):
            continue

    if not candidates:
        return False

    # The provided requirements prioritize matching DOS; CPT is compared after opening.
    row, link, _ = candidates[0]
    safe_click(driver, link)
    wait_element(driver, X["claim_reference"], timeout=WAIT_SECONDS)
    return True


def extract_procedures(driver):
    procedures = []
    rows = driver.find_elements(By.XPATH, X["procedure_table"])
    for row in rows:
        try:
            cpt = clean(row.find_element(By.XPATH, PROC_CPT_CELL).text)
            dos = clean(row.find_element(By.XPATH, PROC_DOS_CELL).text)
            remark = clean(row.find_element(By.XPATH, PROC_REMARK_CELL).text)
            if cpt or dos or remark:
                procedures.append({
                    "cpt": cpt,
                    "dos": normalize_date(dos),
                    "remark": remark,
                })
        except (NoSuchElementException, StaleElementReferenceException):
            continue
    return procedures


def extract_claim_details(driver):
    procedures = extract_procedures(driver)
    explanations = []
    # Explanation rows are dynamic. Gather their visible text without relying on one exact row count.
    explanation_xpath = (
        '/html/body/cigna-root/cigna-layout-wrapper/cigna-layout-secure/div/main/div/'
        'cigna-page/cigna-claim-details-screen/cigna-claims-details/div/'
        'cigna-claims-details-medical/div[5]/cigna-medical-explanation-details'
    )
    try:
        explanation = driver.find_element(By.XPATH, explanation_xpath)
        explanations = [line.strip() for line in explanation.text.splitlines() if line.strip()]
    except NoSuchElementException:
        pass

    return {
        "claim_number": get_text(driver, X["claim_reference"]),
        "date_processed": normalize_date(get_text(driver, X["date_processed"])),
        "amount_due": normalize_money(get_text(driver, X["amount_due"])),
        "amount_paid": normalize_money(get_text(driver, X["amount_paid"])),
        "provider_payment": normalize_money(get_text(driver, X["provider_payment"])),
        "patient_responsibility": normalize_money(get_text(driver, X["patient_responsibility"])),
        "claim_status": get_text(driver, X["claim_status"]),
        "procedures": procedures,
        "explanations": explanations,
    }


def return_to_claim_results(driver):
    if wait_click(driver, X["return_claims"], timeout=8, optional=True):
        time.sleep(1)
        return True
    driver.back()
    time.sleep(2)
    return True


# =============================== NOTES ===================================

def build_master_map(ws_master):
    cpt_col = find_header(ws_master, ["CPT"], required=True)
    remark_col = find_header(ws_master, ["Remark Codes", "Remark Code"], required=False)
    status_col = find_header(ws_master, ["Status"], required=False)
    mapping = {}
    for row in range(2, ws_master.max_row + 1):
        cpt = clean(ws_master.cell(row, cpt_col).value)
        if not cpt:
            continue
        mapping[norm(cpt)] = {
            "remark": clean(ws_master.cell(row, remark_col).value) if remark_col else "",
            "status": clean(ws_master.cell(row, status_col).value) if status_col else "",
        }
    return mapping


def split_cpts(value):
    text = clean(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;/\n]+", text) if part.strip()]


def procedure_match(procedures, input_cpts, target_dos):
    """Prefer procedures matching both DOS and an input CPT; otherwise use DOS, then first row."""
    if not procedures:
        return []
    dos_procs = [p for p in procedures if not target_dos or not p["dos"] or date_matches(p["dos"], target_dos)]
    pool = dos_procs or procedures
    if input_cpts:
        matched = [p for p in pool if norm(p["cpt"]) in {norm(c) for c in input_cpts}]
        if matched:
            return matched
    return pool


def build_note(dos, claim, selected_procedures, master_map):
    amount = claim["amount_paid"]
    is_denied = abs(amount) < 0.005
    note_type = "DND" if is_denied else "PAID"
    cpt_texts = []
    remark_texts = []

    for proc in selected_procedures:
        cpt = proc["cpt"] or "CPT Not Found"
        cpt_texts.append(cpt)
        master = master_map.get(norm(cpt), {})
        # Source requirement: if CPT exists in Master, use its Status; otherwise use the CPT code.
        status_or_cpt = master.get("status") or cpt
        remark = proc["remark"] or master.get("remark") or ""
        if remark:
            remark_texts.append(remark)
        elif status_or_cpt:
            remark_texts.append(status_or_cpt)

    cpts = ", ".join(dict.fromkeys(cpt_texts)) or "CPT Not Found"
    remarks = ", ".join(dict.fromkeys(remark_texts)) or "Remark Not Found"
    note_dos = normalize_date(dos) or next((p["dos"] for p in selected_procedures if p["dos"]), "")
    claim_number = claim["claim_number"] or "Claim Number Not Found"
    processed = claim["date_processed"] or "Date Processed Not Found"
    return (
        f"DOS {note_dos} CLM PD {money_text(amount)} THRU CHK #{claim_number} "
        f"ISSUED ON {processed}. CPT {cpts} {note_type} ({remarks})"
    )


def make_notes_for_row(driver, ws_input, row_num, master_map):
    dos = normalize_date(row_value(ws_input, row_num, ["DOS", "Date of Service"]))
    member_id = clean(row_value(ws_input, row_num, ["Cigna ID", "Cigna Patient ID", "Member ID", "Patient ID"]))
    dob = normalize_date(row_value(ws_input, row_num, ["DOB", "Date of Birth"]))
    patient_name = clean(row_value(ws_input, row_num, ["Patient Name", "Patient Name (first name, Last Name)", "Name"]))
    input_cpts = split_cpts(row_value(ws_input, row_num, ["CPT", "CPT Code"]))

    # Patient search type 1, then fallback to type 2 as specified in the notes.
    found = search_patient_type1(driver, dob, member_id) if (dob and member_id) else False
    if not found:
        found = search_patient_type2(driver, dob, patient_name) if (dob and patient_name) else False
    if not found:
        return None, None, STATUS_NOT_FOUND

    navigate_to_claim_search(driver)
    if not open_claim_matching_dos(driver, dos, input_cpts[0] if input_cpts else ""):
        return None, None, STATUS_NOT_FOUND

    claim = extract_claim_details(driver)
    selected_procs = procedure_match(claim["procedures"], input_cpts, dos)

    # If requested CPT was not in this claim, return to results and inspect other claims with the same DOS.
    # This follows the source note to check other claim results when the first claim does not contain the CPT.
    if input_cpts and not any(norm(p["cpt"]) in {norm(c) for c in input_cpts} for p in selected_procs):
        return_to_claim_results(driver)
        rows = get_claim_result_rows(driver)
        for index in range(1, len(rows)):
            try:
                row = get_claim_result_rows(driver)[index]
                site_dos = clean(row.find_element(By.XPATH, CLAIM_RESULT_DOS).text)
                if not date_matches(site_dos, dos):
                    continue
                link = row.find_element(By.XPATH, CLAIM_RESULT_CLAIM_LINK)
                safe_click(driver, link)
                wait_element(driver, X["claim_reference"], timeout=WAIT_SECONDS)
                other_claim = extract_claim_details(driver)
                other_selected = procedure_match(other_claim["procedures"], input_cpts, dos)
                if any(norm(p["cpt"]) in {norm(c) for c in input_cpts} for p in other_selected):
                    claim, selected_procs = other_claim, other_selected
                    break
                return_to_claim_results(driver)
            except (NoSuchElementException, StaleElementReferenceException, TimeoutException):
                try:
                    return_to_claim_results(driver)
                except Exception:
                    pass

    note = build_note(dos, claim, selected_procs, master_map)
    # Notes2 is reserved for a second claim/CPT result. Only write it when another claim was selected.
    notes2 = ""
    return note, notes2, STATUS_DONE


# ================================ MAIN ===================================

def validate_workbook(wb):
    for sheet in (SHEET_INPUT, SHEET_MASTER, SHEET_LOGIN):
        if sheet not in wb.sheetnames:
            raise ValueError(
                f"Missing worksheet '{sheet}'. Workbook has: {', '.join(wb.sheetnames)}"
            )
    ws_input = wb[SHEET_INPUT]
    # Validate key input headers; Notes/Status columns are created if absent.
    find_header(ws_input, ["Patient Name", "Patient Name (first name, Last Name)", "Name"], required=True)
    find_header(ws_input, ["DOB", "Date of Birth"], required=True)
    find_header(ws_input, ["Cigna ID", "Cigna Patient ID", "Member ID", "Patient ID"], required=True)
    find_header(ws_input, ["DOS", "Date of Service"], required=True)
    find_header(ws_input, ["CPT", "CPT Code"], required=True)
    write_value(ws_input, 1, ["Notes1"], "", create_header="Notes1")
    write_value(ws_input, 1, ["Notes2"], "", create_header="Notes2")
    write_value(ws_input, 1, ["Status"], "", create_header="Status")


def main():
    if not os.path.isfile(EXCEL_PATH):
        raise FileNotFoundError(f"Excel workbook not found: {EXCEL_PATH}")

    wb = openpyxl.load_workbook(EXCEL_PATH)
    validate_workbook(wb)
    ws_input = wb[SHEET_INPUT]
    ws_master = wb[SHEET_MASTER]
    master_map = build_master_map(ws_master)
    username, password = get_login_credentials(wb)

    driver = None
    try:
        driver = start_browser()
        login_if_needed(driver, username, password)

        # Process one input row at a time; save each result before moving to the next.
        for row_num in range(2, ws_input.max_row + 1):
            invoice = clean(row_value(ws_input, row_num, ["Invoice#", "Invoice #", "Invoice", "Claim Number"]))
            existing_status = clean(row_value(ws_input, row_num, ["Status"]))
            if existing_status.casefold() == STATUS_DONE.casefold():
                print(f"Row {row_num}: already completed; skipping.")
                continue

            print(f"\nRow {row_num}: processing invoice {invoice or '(no invoice value)'}")
            try:
                close_cookie_banner(driver)
                notes1, notes2, status = make_notes_for_row(driver, ws_input, row_num, master_map)
                write_value(ws_input, row_num, ["Notes1"], notes1 or "", create_header="Notes1")
                write_value(ws_input, row_num, ["Notes2"], notes2 or "", create_header="Notes2")
                write_value(ws_input, row_num, ["Status"], status, create_header="Status")

                status_col = find_header(ws_input, ["Status"], required=True)
                ws_input.cell(row_num, status_col).fill = GREEN_FILL if status == STATUS_DONE else RED_FILL

                if SAVE_AFTER_EACH_ROW:
                    wb.save(EXCEL_PATH)
                print(f"Row {row_num}: {status}")

                if PAUSE_AFTER_EACH_ROW:
                    input("Inspect this row in the browser, then press Enter to continue...")

            except PermissionError:
                print("Excel save failed: close Cigna.xlsx in Excel and run the script again.")
                raise
            except Exception as exc:
                error_text = f"{STATUS_ERROR}: {str(exc)[:250]}"
                write_value(ws_input, row_num, ["Status"], error_text, create_header="Status")
                status_col = find_header(ws_input, ["Status"], required=True)
                ws_input.cell(row_num, status_col).fill = RED_FILL
                if SAVE_AFTER_EACH_ROW:
                    wb.save(EXCEL_PATH)
                print(f"Row {row_num}: {error_text}")
                traceback.print_exc()

                # Attempt to return to the secure app for the next input row.
                try:
                    driver.get(LOGIN_URL)
                    time.sleep(2)
                    close_cookie_banner(driver)
                    if not wait_element(driver, X["claims_nav"], timeout=5, optional=True):
                        login_if_needed(driver, username, password)
                except Exception:
                    print("Could not recover browser automatically; stopping to avoid incorrect row processing.")
                    break

    finally:
        if driver:
            # Leave browser open so the user can see the final state/session.
            try:
                driver.quit()
            except Exception:
                pass
        try:
            wb.save(EXCEL_PATH)
        except PermissionError:
            print("Final Excel save failed. Close the workbook in Excel and rerun.")
    print("\nAutomation finished.")


if __name__ == "__main__":
    main()
