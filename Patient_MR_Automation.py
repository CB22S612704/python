import base64
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl
import pyotp

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


# ============================================================
# CONFIGURATION  (edit these)
# ============================================================

BASE_URL = "https://pvpm.practicevelocity.com/"

BASE_DIR = Path(r"C:\Users\mages\Desktop\Automation\Patient_MR")

EXCEL_FILE = BASE_DIR / "Paitent_MR.xlsx"
MR_ROOT_FOLDER = BASE_DIR / "MR"

INPUT_SHEET = "Input"
LOGIN_SHEET = "Login"

TIMEOUT = 30
MR_LOAD_TIMEOUT = 90
MR_BUTTON_CHECK_TIMEOUT = 5

SKIP_COMPLETED = True            # skip rows whose Status = "Download And Placed"
# Status match is always strict: find ID# -> match Excel status -> click -> download

# Status values written to the Excel "Status" column
STATUS_DONE = "Download And Placed"
STATUS_NO_MR = "MR Not Available"
STATUS_NOT_FOUND = "Row Not Found"
STATUS_MISMATCH = "Status Mismatch"
STATUS_FAILED = "Failed"

MR_ROOT_FOLDER.mkdir(parents=True, exist_ok=True)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def safe_name(value) -> str:
    value = clean_text(value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = value.rstrip(" .")
    return value or "Unknown"


def format_dos(value) -> str:
    """Excel DOS (datetime / date / text) -> mm/dd/yyyy"""
    if isinstance(value, (datetime, date)):
        return value.strftime("%m/%d/%Y")
    text = clean_text(value)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d-%b-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return text


def is_done_status(status: str) -> bool:
    s = clean_text(status).lower()
    return s.startswith("download") and s.endswith("placed")


def success(message):
    print(f"✓ {message}")


def failure(message):
    print(f"✗ {message}")


def warning(message):
    print(f"⚠ {message}")


def info(message):
    print(message)


def header(message):
    print()
    print("=" * 60)
    print(message)
    print("=" * 60)


# ============================================================
# DATA CLASS
# ============================================================

# Click order from the roadmap: Private, Wrk Cmp, Misc, EPS
CLICK_ORDER = ["Private", "Wrk Cmp", "Misc", "EPS"]


@dataclass
class ClaimRecord:
    excel_row: int
    practice: str
    clinic: str
    dos: str
    arrival_status: str
    patient: str
    id_no: str
    values: Dict[str, str] = field(default_factory=dict)   # column -> Excel value
    status: str = ""

    @property
    def target_column(self) -> Optional[str]:
        for name in CLICK_ORDER:
            if self.values.get(name):
                return name
        return None


# ============================================================
# EXCEL MANAGER
# ============================================================

class ExcelManager:

    def __init__(self, excel_file: Path):
        self.excel_file = excel_file

    def load_workbook(self):
        if not self.excel_file.exists():
            raise FileNotFoundError(f"Excel file not found:\n{self.excel_file}")
        return openpyxl.load_workbook(self.excel_file)

    @staticmethod
    def get_headers(sheet) -> Dict[str, int]:
        headers = {}
        for cell in sheet[1]:
            if cell.value is None:
                continue
            name = clean_text(cell.value)      # strips stray tabs ("ID#\t")
            if name:
                headers[name] = cell.column
        return headers

    # ---------------- LOGIN ----------------

    def read_login(self) -> Tuple[str, str, str]:
        workbook = self.load_workbook()
        try:
            if LOGIN_SHEET not in workbook.sheetnames:
                raise ValueError(f"'{LOGIN_SHEET}' sheet not found.")
            sheet = workbook[LOGIN_SHEET]
            headers = self.get_headers(sheet)

            required = ["Username", "Password", "Security Key"]
            missing = [h for h in required if h not in headers]
            if missing:
                raise ValueError("Missing Login columns: " + ", ".join(missing))

            def cell(name):
                return clean_text(sheet.cell(row=2, column=headers[name]).value)

            username, password, key = cell("Username"), cell("Password"), cell("Security Key")
            if not username:
                raise ValueError("Username is empty (Login sheet, row 2).")
            if not password:
                raise ValueError("Password is empty (Login sheet, row 2).")
            if not key:
                raise ValueError("Security Key is empty (Login sheet, row 2).")
            return username, password, key
        finally:
            workbook.close()

    # ---------------- RECORDS ----------------

    def read_records(self) -> List[ClaimRecord]:
        workbook = self.load_workbook()
        try:
            if INPUT_SHEET not in workbook.sheetnames:
                raise ValueError(f"'{INPUT_SHEET}' sheet not found.")
            sheet = workbook[INPUT_SHEET]
            headers = self.get_headers(sheet)

            required = ["Practice", "Clinic", "DOS", "ID#", "Private",
                        "Wrk Cmp", "EPS", "Misc", "Status"]
            missing = [h for h in required if h not in headers]
            if missing:
                raise ValueError("Missing Input columns: " + ", ".join(missing))

            def get(row, name):
                if name not in headers:
                    return None
                return sheet.cell(row=row, column=headers[name]).value

            records = []
            for r in range(2, sheet.max_row + 1):
                practice = clean_text(get(r, "Practice"))
                clinic = clean_text(get(r, "Clinic"))
                id_no = clean_text(get(r, "ID#"))
                dos = format_dos(get(r, "DOS"))

                if not (practice and clinic and id_no and dos):
                    continue

                records.append(ClaimRecord(
                    excel_row=r,
                    practice=practice,
                    clinic=clinic,
                    dos=dos,
                    arrival_status=clean_text(get(r, "Arrival Status")),
                    patient=clean_text(get(r, "Patient")),
                    id_no=id_no,
                    values={n: clean_text(get(r, n)) for n in CLICK_ORDER},
                    status=clean_text(get(r, "Status")),
                ))
            return records
        finally:
            workbook.close()

    # ---------------- UPDATE ----------------

    def update_status(self, excel_row: int, status: str, remarks: str = ""):
        """Write Status (and Remarks if that column exists). Retries when the
        workbook is open in Excel (PermissionError)."""
        last_error = None
        for _ in range(5):
            try:
                workbook = self.load_workbook()
                try:
                    sheet = workbook[INPUT_SHEET]
                    headers = self.get_headers(sheet)
                    if "Status" not in headers:
                        raise ValueError("Status column not found.")
                    sheet.cell(row=excel_row, column=headers["Status"]).value = status
                    if "Remarks" in headers:
                        sheet.cell(row=excel_row, column=headers["Remarks"]).value = remarks
                    workbook.save(self.excel_file)
                    return
                finally:
                    workbook.close()
            except PermissionError as error:
                last_error = error
                warning("Excel file is open/locked - close it. Retrying in 3s...")
                time.sleep(3)
        raise last_error


# ============================================================
# BROWSER MANAGER
# ============================================================

class BrowserManager:

    def __init__(self):
        self.driver = None

    def start(self):
        options = Options()
        for arg in (
            "--start-maximized",
            "--window-size=1920,1080",
            "--disable-gpu",
            "--disable-notifications",
            "--disable-popup-blocking",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ):
            options.add_argument(arg)

        options.add_experimental_option("prefs", {
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "profile.default_content_settings.popups": 0,
        })

        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(TIMEOUT)
        self.driver.set_script_timeout(TIMEOUT)
        try:
            self.driver.maximize_window()
        except Exception:
            pass
        return self.driver

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ============================================================
# BASE PAGE (shared wait / click helpers)
# ============================================================

class BasePage:

    def __init__(self, driver):
        self.driver = driver

    def wait(self, locator, timeout=TIMEOUT):
        return WebDriverWait(
            self.driver, timeout,
            ignored_exceptions=(StaleElementReferenceException,)
        ).until(EC.presence_of_element_located(locator))

    def click(self, locator, timeout=TIMEOUT):
        element = WebDriverWait(
            self.driver, timeout,
            ignored_exceptions=(StaleElementReferenceException,)
        ).until(EC.element_to_be_clickable(locator))
        self.click_element(element)

    def click_element(self, element):
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", element)
        except Exception:
            pass
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)

    def exists(self, locator) -> bool:
        try:
            return bool(self.driver.find_elements(*locator))
        except Exception:
            return False


# ============================================================
# LOGIN MODULE
# ============================================================

class LoginModule(BasePage):

    USERNAME = (By.ID, "txtLogin")
    USERNAME_NEXT = (By.ID, "btnNext")
    CONTINUE_BUTTON = (By.XPATH, "//*[@id='form20']/div[2]/input")
    PASSWORD = (By.ID, "input54")
    PASSWORD_BUTTON = (By.XPATH, "//*[@id='form46']/div[2]/input")
    OTP = (By.ID, "input80")
    OTP_BUTTON = (By.XPATH, "//*[@id='form72']/div[2]/input")

    PATIENT_MENU = (By.XPATH, "//*[@id='tdMenuBarItemPatient']/a")
    PRACTICE_DROPDOWN = (By.XPATH, '//*[@id="ddlPractice"]')

    def login(self, username: str, password: str, security_key: str) -> bool:
        try:
            info("Processing Login...")
            self.driver.get(BASE_URL)

            field_ = self.wait(self.USERNAME)
            field_.clear()
            field_.send_keys(username)
            self.click(self.USERNAME_NEXT)
            self.click(self.CONTINUE_BUTTON)

            field_ = self.wait(self.PASSWORD)
            field_.clear()
            field_.send_keys(password)
            self.click(self.PASSWORD_BUTTON)

            clean_key = security_key.replace(" ", "").replace("-", "").upper()
            otp = pyotp.TOTP(clean_key).now()

            field_ = self.wait(self.OTP)
            field_.clear()
            field_.send_keys(otp)
            self.click(self.OTP_BUTTON)

            WebDriverWait(self.driver, TIMEOUT).until(
                lambda d: self.exists(self.PATIENT_MENU)
                or self.exists(self.PRACTICE_DROPDOWN)
            )

            success("LOGIN SUCCESSFUL")
            return True

        except Exception as error:
            failure(f"LOGIN FAILED: {error}")
            return False


# ============================================================
# MEDICAL RECORD MODULE  (log book + MR download)
# ============================================================

class MedicalRecordModule(BasePage):

    # ---- filter section ----
    PRACTICE_DROPDOWN = (By.XPATH, '//*[@id="ddlPractice"]')
    CLINIC_DROPDOWN = (By.XPATH, '//*[@id="ddlClinic"]')
    DOS_INPUT = (By.XPATH, '//*[@id="txtSvcDate"]')
    GO_BUTTON = (By.XPATH, '//*[@id="datePicker"]/button[1]')
    HIDE_CHARGED_LABEL = (By.XPATH, '//*[@id="log-book"]/div[2]/div[5]/label')

    # ---- log book table ----
    ROWS = (By.XPATH, '//*[@id="log-book"]/div[3]/table/tbody/tr')

    # column -> (td index, relative xpath of the element to click)
    COLUMN_MAP = {
        "Private": (12, "./td[12]/span/span/span"),
        "Wrk Cmp": (13, "./td[13]/span/span/span/span/input"),
        "EPS": (14, "./td[14]/span/span/span/span/input"),
        "Misc": (15, "./td[15]/span/span/span/span/input"),
    }

    # ---- charge screen ----
    MR_BUTTON = (By.XPATH, '//*[@id="btnViewPivotDocs"]')
    CANCEL_BUTTON = (
        By.XPATH,
        '//*[@id="form1"]/div[2]/table[1]/tbody/tr/td[2]/div/div/div[4]/input',
    )

    TIFF_VIEWER_TEXT = "TiffViewer.aspx"

    # ========================================================
    # DROPDOWNS
    # ========================================================

    def _selected_text(self, locator) -> str:
        try:
            dropdown = Select(self.driver.find_element(*locator))
            return clean_text(dropdown.first_selected_option.text)
        except Exception:
            return ""

    def _dropdown_matches(self, locator, target: str) -> bool:
        try:
            dropdown = Select(self.driver.find_element(*locator))
            opt = dropdown.first_selected_option
            t = target.lower()
            return (clean_text(opt.text).lower() == t
                    or clean_text(opt.get_attribute("value")).lower() == t)
        except Exception:
            return False

    def select_dropdown(self, locator, target: str, label: str):
        """Select by exact text/value (falls back to 'contains'). Waits for
        the options to load (the clinic list reloads after the practice)."""
        target_l = clean_text(target).lower()

        def pick(driver):
            try:
                dropdown = Select(driver.find_element(*locator))
                options = dropdown.options
                for exact in (True, False):
                    for opt in options:
                        text = clean_text(opt.text)
                        value = clean_text(opt.get_attribute("value"))
                        if exact:
                            hit = text.lower() == target_l or value.lower() == target_l
                        else:
                            hit = target_l in text.lower() or target_l in value.lower()
                        if hit:
                            dropdown.select_by_visible_text(text)
                            return True
                return False
            except Exception:
                return False

        try:
            WebDriverWait(self.driver, TIMEOUT).until(pick)
        except TimeoutException:
            raise ValueError(f"{label} '{target}' not found in dropdown.")

        # verify (exact or contains)
        def verified(driver):
            sel = self._selected_text(locator).lower()
            return sel == target_l or target_l in sel

        WebDriverWait(self.driver, TIMEOUT).until(verified)
        success(f"{label} Selected: {target}")

    # ========================================================
    # DOS
    # ========================================================

    def _dos_value(self) -> str:
        try:
            return clean_text(self.driver.find_element(*self.DOS_INPUT).get_attribute("value"))
        except Exception:
            return ""

    def enter_dos(self, dos: str):
        field_ = self.wait(self.DOS_INPUT)
        try:
            field_.click()
            field_.send_keys(Keys.CONTROL, "a")
            field_.send_keys(Keys.DELETE)
            field_.send_keys(dos)
        except Exception:
            pass

        if self._dos_value() != dos:
            # fallback for date-picker widgets that ignore typing
            self.driver.execute_script(
                """
                const el = arguments[0];
                el.value = arguments[1];
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.dispatchEvent(new Event('blur',   {bubbles:true}));
                """,
                field_, dos,
            )

        if self._dos_value() != dos:
            raise ValueError(f"Could not set DOS to {dos} (field shows '{self._dos_value()}').")
        success(f"DOS Entered: {dos}")

    # ========================================================
    # HIDE ALL CHARGED VISITS  -> must be UNCHECKED
    # ========================================================

    def _hide_charged_checkbox(self):
        label = self.driver.find_element(*self.HIDE_CHARGED_LABEL)

        inner = label.find_elements(By.XPATH, ".//input[@type='checkbox']")
        if inner:
            return label, inner[0]

        target_id = label.get_attribute("for")
        if target_id:
            found = self.driver.find_elements(By.ID, target_id)
            if found:
                return label, found[0]

        sibling = label.find_elements(
            By.XPATH, "./preceding-sibling::input[@type='checkbox'][1]"
            " | ./following-sibling::input[@type='checkbox'][1]")
        if sibling:
            return label, sibling[0]

        return label, None

    def ensure_hide_charged_unchecked(self):
        try:
            label, checkbox = self._hide_charged_checkbox()
        except Exception:
            warning("'Hide all Charged visits' checkbox not found.")
            return

        if checkbox is None:
            warning("Could not read the 'Hide all Charged visits' state.")
            return

        if checkbox.is_selected():
            self.click_element(label)
            WebDriverWait(self.driver, 10).until(
                lambda d: not self._hide_charged_checkbox()[1].is_selected())
            self.wait_for_rows(timeout=10, quiet=True)
            success("'Hide all Charged visits' Unchecked")

    # ========================================================
    # LOG BOOK SETUP  (practice, clinic, DOS, Go)
    # ========================================================

    def wait_for_rows(self, timeout=TIMEOUT, quiet=False) -> bool:
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: len(d.find_elements(*self.ROWS)) > 0)
            return True
        except TimeoutException:
            if not quiet:
                warning("No log-book rows loaded.")
            return False

    def recover_to_logbook(self):
        """If we are not on the log-book page, reload it."""
        if self.exists(self.PRACTICE_DROPDOWN):
            return
        warning("Log-book page not visible - reloading...")
        self.driver.get(BASE_URL)
        self.wait(self.PRACTICE_DROPDOWN)

    def filters_match(self, rec: ClaimRecord) -> bool:
        return (
            self._dropdown_matches(self.PRACTICE_DROPDOWN, rec.practice)
            and self._selected_text(self.CLINIC_DROPDOWN).lower().find(rec.clinic.lower()) >= 0
            and self._dos_value() == rec.dos
            and self.exists(self.ROWS)
        )

    def load_logbook(self, rec: ClaimRecord):
        info(f"Loading log book | {rec.practice} | {rec.clinic} | {rec.dos}")

        self.select_dropdown(self.PRACTICE_DROPDOWN, rec.practice, "Practice")
        time.sleep(1)
        self.select_dropdown(self.CLINIC_DROPDOWN, rec.clinic, "Clinic")
        self.enter_dos(rec.dos)

        self.click(self.GO_BUTTON)
        time.sleep(1.5)
        self.wait_for_rows(quiet=True)

        self.ensure_hide_charged_unchecked()
        success("Log Book Loaded")

    def ensure_logbook(self, rec: ClaimRecord):
        self.recover_to_logbook()
        if self.filters_match(rec):
            self.ensure_hide_charged_unchecked()
            return
        self.load_logbook(rec)

    # ========================================================
    # FIND + CLICK THE MATCHING CELL
    # ========================================================

    @staticmethod
    def _cell_text(row, td_index: int) -> str:
        """Visible text of a cell, or the value of an input inside it."""
        try:
            cell = row.find_element(By.XPATH, f"./td[{td_index}]")
        except Exception:
            return ""
        text = clean_text(cell.text)
        if text:
            return text
        for inp in cell.find_elements(By.XPATH, ".//input"):
            for attr in ("value", "title", "alt"):
                v = clean_text(inp.get_attribute(attr))
                if v and v.lower() not in ("on", "off", "true", "false"):
                    return v
        for sp in cell.find_elements(By.XPATH, ".//span"):
            v = clean_text(sp.get_attribute("title") or sp.get_attribute("textContent"))
            if v:
                return v
        return ""

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", clean_text(text)).lower()

    def find_target(self, rec: ClaimRecord):
        """STEP 1: find the row that contains the ID#.
        STEP 2: match the Excel status (SIGNED / CHARGED / Charge Pending ...)
                against the Private / Wrk Cmp / Misc / EPS cells of that row.
        STEP 3: return the matching element to click.
        Nothing is clicked unless the status matches exactly."""
        column = rec.target_column
        if column is None:
            raise ValueError("No Private / Wrk Cmp / EPS / Misc value in Excel.")

        excel_value = rec.values[column]
        wanted = self._norm(excel_value)
        pattern = re.compile(rf"(?<!\d){re.escape(rec.id_no)}(?!\d)")

        # ---- STEP 1: ID# ----
        rows = self.driver.find_elements(*self.ROWS)
        id_rows = []
        for row in rows:
            try:
                if pattern.search(row.text or ""):
                    id_rows.append(row)
            except StaleElementReferenceException:
                continue

        if not id_rows:
            raise LookupError(f"ID# {rec.id_no} not found in log book ({len(rows)} rows).")
        
        # ---- STEP 2: status match (Excel column first, then the others) ----
        search_order = [column] + [c for c in CLICK_ORDER if c != column]
        seen = []

        for row in id_rows:
            on_screen = {}
            for col in search_order:
                td_index, _ = self.COLUMN_MAP[col]
                on_screen[col] = self._cell_text(row, td_index)
                
            seen.append(on_screen)

            for col in search_order:
                if self._norm(on_screen[col]) == wanted:
                    td_index, rel_xpath = self.COLUMN_MAP[col]
                    elements = row.find_elements(By.XPATH, rel_xpath)
                    if not elements:
                        elements = row.find_elements(By.XPATH, f"./td[{td_index}]")
                    if elements:
                        return elements[0], col

        raise ValueError(
            f"ID# {rec.id_no} found but status '{excel_value}' not matched. "
            f"On screen: {seen[0] if seen else {}}")

    # ========================================================
    # CHARGE SCREEN: MR BUTTON / CANCEL
    # ========================================================

    def wait_for_charge_screen(self):
        WebDriverWait(self.driver, TIMEOUT).until(
            lambda d: self.exists(self.MR_BUTTON) or self.exists(self.CANCEL_BUTTON))

    def is_mr_button_available(self) -> bool:
        try:
            WebDriverWait(
                self.driver, MR_BUTTON_CHECK_TIMEOUT,
                ignored_exceptions=(StaleElementReferenceException,)
            ).until(EC.presence_of_element_located(self.MR_BUTTON))
        except Exception:
            return False

        for element in self.driver.find_elements(*self.MR_BUTTON):
            try:
                if element.is_displayed() and element.is_enabled():
                    return True
            except StaleElementReferenceException:
                continue
        return False

    def click_cancel(self):
        self.click(self.CANCEL_BUTTON)
        WebDriverWait(self.driver, TIMEOUT).until(
            lambda d: self.exists(self.ROWS) or self.exists(self.PRACTICE_DROPDOWN))
        time.sleep(1)

    # ========================================================
    # MR POPUP
    # ========================================================

    def click_mr(self):
        info("Opening Medical Record...")
        old_handles = set(self.driver.window_handles)

        self.click(self.MR_BUTTON)

        WebDriverWait(self.driver, TIMEOUT).until(
            lambda d: len(d.window_handles) > len(old_handles))

        new_handles = set(self.driver.window_handles) - old_handles
        if not new_handles:
            raise RuntimeError("MR popup did not open.")

        self.driver.switch_to.window(list(new_handles)[0])

        WebDriverWait(self.driver, TIMEOUT).until(
            lambda d: self.TIFF_VIEWER_TEXT.lower() in d.current_url.lower())

        success("Medical Record Opened")

    def wait_for_medical_record(self):
        info("Waiting for MR content...")
        end_time = time.time() + MR_LOAD_TIMEOUT

        while time.time() < end_time:
            try:
                state = self.driver.execute_script("return document.readyState;")
                element_count = self.driver.execute_script(
                    "return document.querySelectorAll('img, canvas, object, embed').length;")
                body_text_length = self.driver.execute_script(
                    "return (document.body && document.body.innerText)"
                    " ? document.body.innerText.length : 0;")

                if state == "complete" and (element_count > 0 or body_text_length > 100):
                    time.sleep(3)
                    success("MR CONTENT LOADED")
                    return True
            except Exception:
                pass
            time.sleep(1)

        raise RuntimeError("Medical Record content did not load.")

    def wait_for_rendered_content(self):
        script = """
        const images = Array.from(document.images || []);
        return {
            imagesComplete: images.every(img => img.complete),
            readyState: document.readyState
        };
        """
        end_time = time.time() + MR_LOAD_TIMEOUT
        while time.time() < end_time:
            try:
                result = self.driver.execute_script(script)
                if result["readyState"] == "complete" and result["imagesComplete"]:
                    time.sleep(2)
                    return
            except Exception:
                pass
            time.sleep(1)
        warning("MR render wait reached timeout.")

    def generate_pdf_with_cdp(self, output_file: Path) -> Path:
        info("Generating PDF...")

        try:
            self.driver.execute_cdp_cmd("Page.enable", {})
        except Exception:
            pass

        self.wait_for_rendered_content()

        result = self.driver.execute_cdp_cmd("Page.printToPDF", {
            "landscape": True,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": False,
            "scale": 1.0,
            "paperWidth": 11.0,
            "paperHeight": 8.5,
            "marginTop": 0.0,
            "marginBottom": 0.0,
            "marginLeft": 0.0,
            "marginRight": 0.0,
        })

        pdf_data = (result or {}).get("data")
        if not pdf_data:
            raise RuntimeError("Chrome did not return PDF data.")

        pdf_bytes = base64.b64decode(pdf_data)
        if not pdf_bytes.startswith(b"%PDF"):
            raise RuntimeError("Returned data is not a valid PDF.")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "wb") as f:
            f.write(pdf_bytes)

        size = output_file.stat().st_size
        if size < 1000:
            try:
                output_file.unlink()
            except Exception:
                pass
            raise RuntimeError(f"PDF is suspiciously small: {size} bytes.")
        
        return output_file

    def close_popups(self, main_window):
        for handle in list(self.driver.window_handles):
            if handle == main_window:
                continue
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
            except Exception:
                pass
        if main_window in self.driver.window_handles:
            self.driver.switch_to.window(main_window)

    # ========================================================
    # DOWNLOAD MR
    # ========================================================

    def download_mr(self, rec: ClaimRecord, main_window: str) -> Path:
        folder = MR_ROOT_FOLDER / safe_name(rec.practice)
        folder.mkdir(parents=True, exist_ok=True)

        final_file = folder / f"{safe_name(rec.id_no)}_MR.pdf"
        if final_file.exists():
            final_file.unlink()

        self.generate_pdf_with_cdp(final_file)
        success(f"MR Downloaded And Placed: {final_file}")

        self.close_popups(main_window)
        return final_file

    # ========================================================
    # FAILURE HANDLING
    # ========================================================

    def try_return_to_logbook(self, main_window):
        """Best effort after a failure: close popups, press Cancel."""
        try:
            if main_window:
                self.close_popups(main_window)
            if self.exists(self.CANCEL_BUTTON):
                self.click_cancel()
        except Exception:
            pass

    # ========================================================
    # PROCESS ONE RECORD  -> (status, remarks)
    # ========================================================

    def process(self, rec: ClaimRecord) -> Tuple[str, str]:
        main_window = None
        try:
            if not self.driver.window_handles:
                raise RuntimeError("Chrome has no active window.")

            self.driver.switch_to.window(self.driver.window_handles[0])
            main_window = self.driver.current_window_handle

            self.ensure_logbook(rec)

            WebDriverWait(self.driver, 15).until(lambda d: len(d.find_elements(*self.ROWS)) > 0)
            time.sleep(3)
            try:
                element, column = self.find_target(rec)
            except LookupError as error:
                warning(str(error))
                return STATUS_NOT_FOUND, str(error)
            except ValueError as error:
                warning(str(error))
                return STATUS_MISMATCH, str(error)
            
            self.click_element(element)
            self.wait_for_charge_screen()

            # ---- MR button ----
            if not self.is_mr_button_available():
                warning(f"MR button not available | ID# {rec.id_no}")
                self.click_cancel()
                return STATUS_NO_MR, "Medical Record button not available."

            self.click_mr()
            self.wait_for_medical_record()
            final_file = self.download_mr(rec, main_window)

            self.click_cancel()

            success("RECORD COMPLETED")
            return STATUS_DONE, ""

        except Exception as error:
            failure(f"RECORD FAILED | ID# {rec.id_no}")
            failure(f"Reason: {error}")
            self.try_return_to_logbook(main_window)
            return STATUS_FAILED, str(error)[:250]


# ============================================================
# MAIN AUTOMATION
# ============================================================

class MedicalRecordAutomation:

    def __init__(self):
        self.excel = ExcelManager(EXCEL_FILE)
        self.browser = BrowserManager()
        self.driver = None

    def save_status(self, rec: ClaimRecord, status: str, remarks: str = ""):
        try:
            self.excel.update_status(rec.excel_row, status, remarks)
            success(f"Excel Status Updated: {status}")
        except Exception as error:
            warning(f"Excel Status update failed: {error}")

    def run(self):
        counts = {"total": 0, "done": 0, "no_mr": 0, "not_found": 0,
                  "mismatch": 0, "failed": 0, "skipped": 0}

        try:
            header("PATIENT MEDICAL RECORD AUTOMATION")

            info("Reading login information...")
            username, password, security_key = self.excel.read_login()
            success("Login information loaded")

            info("Reading Input sheet...")
            records = self.excel.read_records()
            counts["total"] = len(records)
            success(f"Records Found: {counts['total']}")

            if not records:
                warning("No records found.")
                return

            info("Starting Chrome...")
            self.driver = self.browser.start()
            success("Chrome Started")

            login = LoginModule(self.driver)
            mr = MedicalRecordModule(self.driver)

            if not login.login(username, password, security_key):
                failure("Initial Login Failed.")
                return

            for index, rec in enumerate(records, start=1):
                header(f"RECORD {index} / {counts['total']}")
                print(f"Practice : {rec.practice}")
                print(f"Clinic   : {rec.clinic}")
                print(f"DOS      : {rec.dos}")
                print(f"Patient  : {rec.patient}")
                print(f"ID#      : {rec.id_no}")
            

                if SKIP_COMPLETED and is_done_status(rec.status):
                    success("MR Already Downloaded And Placed - Skipping")
                    counts["skipped"] += 1
                    continue

                status, remarks = mr.process(rec)

                key = {
                    STATUS_DONE: "done",
                    STATUS_NO_MR: "no_mr",
                    STATUS_NOT_FOUND: "not_found",
                    STATUS_MISMATCH: "mismatch",
                }.get(status, "failed")
                counts[key] += 1

                self.save_status(rec, status, remarks)

                if status == STATUS_FAILED:
                    warning("Moving to next record. No retry.")

            header("AUTOMATION COMPLETED")
            print(f"Total Records    : {counts['total']}")
            print(f"Downloaded       : {counts['done']}")
            print(f"MR Not Available : {counts['no_mr']}")
            print(f"Row Not Found    : {counts['not_found']}")
            print(f"Status Mismatch  : {counts['mismatch']}")
            print(f"Failed           : {counts['failed']}")
            print(f"Skipped          : {counts['skipped']}")
            print()
            success("ALL RECORDS PROCESSED")

        except Exception as error:
            print()
            failure(f"AUTOMATION ERROR: {error}")

        finally:
            self.browser.close()
            print()
            info("Chrome closed.")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    MedicalRecordAutomation().run()


if __name__ == "__main__":
    main()
