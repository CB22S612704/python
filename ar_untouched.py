"""AR Untouched automation: downloads AGE24 (Invoice + Service date) and FIN18
reports from Practice Velocity for every practice listed in Untouch.xlsx.

See README.md for setup. Settings can be overridden with environment variables
(AR_BASE_DIR, AR_HEADLESS, AR_FORCE_REPROCESS).
"""

import json
import logging
import os
import platform
import re
import shutil
import smtplib
import ssl
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
import pyotp
from openpyxl.styles import PatternFill
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# Optional desktop toast backend - silently disabled if neither is installed
try:
    from plyer import notification as _plyer_notification
    _TOAST_BACKEND = "plyer"
except ImportError:
    try:
        from win10toast import ToastNotifier as _Win10Toast
        _TOAST_BACKEND = "win10toast"
    except ImportError:
        _TOAST_BACKEND = None


# ==========================================================
# CONFIGURATION
# ==========================================================

def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes")


class Config:
    """Centralized configuration."""

    URL = "https://pvpm.practicevelocity.com/"

    # Browser
    HEADLESS = _env_flag("AR_HEADLESS", False)
    WINDOW_SIZE = "1920,1080"
    DISABLE_IMAGES = False
    PAGE_LOAD_STRATEGY = "normal"  # normal / eager
    HEADLESS_STARTUP_DELAY = 1

    # Paths (default: the folder containing this script)
    BASE_DIR = Path(os.getenv("AR_BASE_DIR", Path(__file__).resolve().parent))
    EXCEL_FILE = BASE_DIR / "Untouch.xlsx"
    DOWNLOAD_DIR = BASE_DIR / "Reports"
    LOG_DIR = BASE_DIR / "Logs"
    SCREENSHOT_DIR = LOG_DIR / "Screenshots"
    STATE_FILE = LOG_DIR / "automation_state.json"

    # Timeouts (seconds)
    PAGE_LOAD_TIMEOUT = 180
    IMPLICIT_WAIT = 5
    DOWNLOAD_TIMEOUT = 180
    POPUP_TIMEOUT = 120
    ELEMENT_TIMEOUT = 60
    RETRY_DELAY = 3
    MAX_RETRIES = 3

    # Report file names
    AGE24_INVOICE_NAME = "AGE_24_ByInvcDate"
    AGE24_SERVICE_NAME = "AGE24_Service"  # unused: Service report keeps its downloaded name
    FIN18_NAME = "FIN18"

    # Report code -> exact link text on the search-results page.
    # Unlisted codes fall back to the first result link.
    REPORT_RESULT_LINK_TEXT = {
        "AGE24": "Non-Month End Aging",
    }

    # Notifications
    DESKTOP_NOTIFICATIONS_ENABLED = True
    MESSAGEBOX_ENABLED = True
    EMAIL_ENABLED = False
    EMAIL_SMTP_HOST = "smtp.gmail.com"
    EMAIL_SMTP_PORT = 587
    EMAIL_USERNAME = os.getenv("AR_EMAIL_USERNAME", "")
    EMAIL_PASSWORD = os.getenv("AR_EMAIL_PASSWORD", "")  # never commit this
    EMAIL_FROM = ""
    EMAIL_TO: List[str] = []
    EMAIL_NOTIFY_ON_START = False
    EMAIL_NOTIFY_ON_SUCCESS = True
    EMAIL_NOTIFY_ON_FAILURE = True

    # Stop early when this many practices fail in a row
    CIRCUIT_BREAKER_ENABLED = True
    CIRCUIT_BREAKER_THRESHOLD = 4

    # Ignore Excel Status + saved state and reprocess every row
    FORCE_REPROCESS = _env_flag("AR_FORCE_REPROCESS", False)

    @classmethod
    def ensure_directories(cls):
        for directory in (cls.DOWNLOAD_DIR, cls.LOG_DIR, cls.SCREENSHOT_DIR):
            directory.mkdir(parents=True, exist_ok=True)


# ==========================================================
# LOGGING (console: symbols, no timestamps | file: full detail)
# ==========================================================

# kind -> (logging level, symbol, ANSI colour)
LOG_KINDS = {
    "debug":    (logging.DEBUG,    "·",  "\033[90m"),
    "info":     (logging.INFO,     "➜",  "\033[36m"),
    "success":  (logging.INFO,     "✔",  "\033[32m"),
    "warning":  (logging.WARNING,  "⚠",  "\033[33m"),
    "error":    (logging.ERROR,    "✖",  "\033[31m"),
    "critical": (logging.CRITICAL, "✖✖", "\033[35m"),
}
_RESET = "\033[0m"


class ConsoleFormatter(logging.Formatter):
    """Symbol + coloured message, no timestamp."""

    def format(self, record):
        kind = getattr(record, "kind", None) or record.levelname.lower()
        _, symbol, color = LOG_KINDS.get(kind, LOG_KINDS["info"])
        return f"{color}{symbol} {record.getMessage()}{_RESET}"


def _setup_logging() -> logging.Logger:
    Config.ensure_directories()

    # Enable ANSI colours + UTF-8 symbols in Windows cmd
    if platform.system() == "Windows":
        os.system("")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = []

    file_handler = logging.FileHandler(
        Config.LOG_DIR / f"Automation_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)-20s | %(message)s", "%Y-%m-%d %H:%M:%S"
    ))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return root


_logger = _setup_logging()


def log(message: str, kind: str = "info"):
    """Log a message. kind: debug / info / success / warning / error / critical."""
    level = LOG_KINDS.get(kind, LOG_KINDS["info"])[0]
    _logger.log(level, message, extra={"kind": kind})


def log_header(title: str):
    log("─" * 60)
    log(title)
    log("─" * 60)


# ==========================================================
# ENUMS & DATA CLASSES
# ==========================================================

class Status(Enum):
    PENDING = "Pending"
    DOWNLOADED = "Downloaded"
    FAILED = "Failed"
    RETRY = "Retry"
    SKIPPED = "Skipped"
    PARTIAL = "Partial"


STATUS_COLORS = {
    Status.DOWNLOADED: "92D050",
    Status.FAILED: "FF0000",
    Status.RETRY: "FFFF00",
    Status.SKIPPED: "D9D9D9",
    Status.PENDING: "FFFFFF",
    Status.PARTIAL: "FFC000",
}


class CircuitBreakerTripped(Exception):
    """Too many consecutive practice failures - stop the run early."""


@dataclass
class Credentials:
    username: str
    password: str
    security_key: str


@dataclass
class AGE24Practice:
    practice_name: str
    service_date: str
    status: str = ""
    row_number: int = 0
    invoice_downloaded: bool = False
    service_downloaded: bool = False


@dataclass
class FIN18Practice:
    practice_name: str
    from_date: str
    to_date: str
    status: str = ""
    row_number: int = 0


@dataclass
class AutomationState:
    last_run: Optional[str] = None
    completed_practices: List[str] = field(default_factory=list)
    failed_practices: List[str] = field(default_factory=list)
    partial_practices: List[str] = field(default_factory=list)
    current_module: Optional[str] = None


# ==========================================================
# STATE MANAGEMENT
# ==========================================================

class StateManager:
    """Persists progress so completed practices are skipped on the next run."""

    def __init__(self):
        self.state = AutomationState()
        self._load()

    def _load(self):
        if not Config.STATE_FILE.exists():
            return
        try:
            with open(Config.STATE_FILE, "r") as f:
                self.state = AutomationState(**json.load(f))
            log(f"State loaded: {len(self.state.completed_practices)} completed, "
                f"{len(self.state.failed_practices)} failed")
        except Exception as e:
            log(f"Failed to load state: {e}", "warning")

    def save(self):
        try:
            self.state.last_run = datetime.now().isoformat()
            with open(Config.STATE_FILE, "w") as f:
                json.dump(asdict(self.state), f, indent=2)
        except Exception as e:
            log(f"Failed to save state: {e}", "warning")

    def _mark(self, bucket: List[str], name: str):
        if name not in bucket:
            bucket.append(name)
            self.save()

    def mark_completed(self, name: str):
        self._mark(self.state.completed_practices, name)

    def mark_failed(self, name: str):
        self._mark(self.state.failed_practices, name)

    def mark_partial(self, name: str):
        self._mark(self.state.partial_practices, name)

    def is_completed(self, name: str) -> bool:
        return name in self.state.completed_practices

    def set_module(self, module_name: str):
        self.state.current_module = module_name
        self.save()


state_manager = StateManager()


# ==========================================================
# NOTIFICATIONS (every method fails soft)
# ==========================================================

class NotificationManager:
    """Desktop toast, Windows message box and email notifications."""

    def __init__(self):
        self.toast_available = Config.DESKTOP_NOTIFICATIONS_ENABLED and _TOAST_BACKEND is not None
        if Config.DESKTOP_NOTIFICATIONS_ENABLED and not self.toast_available:
            log("Desktop toast disabled (pip install plyer to enable)", "warning")

    def send_desktop(self, title: str, message: str):
        if not self.toast_available:
            return
        try:
            message = message[:250]
            if _TOAST_BACKEND == "plyer":
                _plyer_notification.notify(title=title, message=message,
                                           app_name="AR Automation", timeout=10)
            else:
                _Win10Toast().show_toast(title, message, duration=10, threaded=True)
        except Exception as e:
            log(f"Desktop notification failed: {e}", "warning")

    def show_messagebox(self, title: str, message: str, icon: str = "info"):
        """Windows popup in a daemon thread so it never blocks shutdown."""
        if not Config.MESSAGEBOX_ENABLED or platform.system() != "Windows":
            return
        try:
            import ctypes
            flags = {"info": 0x40, "warning": 0x30, "error": 0x10}.get(icon, 0x40) | 0x1000

            def _show():
                try:
                    ctypes.windll.user32.MessageBoxW(0, message, title, flags)
                except Exception as e:
                    log(f"Message box failed: {e}", "warning")

            threading.Thread(target=_show, daemon=True).start()
        except Exception as e:
            log(f"Message box failed: {e}", "warning")

    def send_email(self, subject: str, body: str, attachments: Optional[List[Path]] = None):
        if not Config.EMAIL_ENABLED:
            return
        if not Config.EMAIL_USERNAME or not Config.EMAIL_TO:
            log("Email skipped: EMAIL_USERNAME / EMAIL_TO not configured", "warning")
            return
        try:
            msg = MIMEMultipart()
            msg["From"] = Config.EMAIL_FROM or Config.EMAIL_USERNAME
            msg["To"] = ", ".join(Config.EMAIL_TO)
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            for path in attachments or []:
                path = Path(path)
                if path.exists():
                    part = MIMEApplication(path.read_bytes(), Name=path.name)
                    part["Content-Disposition"] = f'attachment; filename="{path.name}"'
                    msg.attach(part)

            with smtplib.SMTP(Config.EMAIL_SMTP_HOST, Config.EMAIL_SMTP_PORT) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(Config.EMAIL_USERNAME, Config.EMAIL_PASSWORD)
                server.sendmail(msg["From"], Config.EMAIL_TO, msg.as_string())
            log(f"Email sent: {subject}", "success")
        except Exception as e:
            log(f"Email failed: {e}", "warning")

    def notify_start(self):
        self.send_desktop("AR Automation Started", "Login successful, processing reports...")
        if Config.EMAIL_NOTIFY_ON_START:
            self.send_email("AR Automation Started", "The AR automation run has started.")

    def notify_run_complete(self, summary: Dict[str, List[str]]):
        completed, partial, failed = (len(summary[k]) for k in ("completed", "partial", "failed"))
        title = "AR Automation Completed" if not failed else "AR Automation Completed (with issues)"
        message = f"Completed: {completed} | Partial: {partial} | Failed: {failed}"
        self.send_desktop(title, message)
        self.show_messagebox(title, message, "info" if not failed else "warning")

        has_issues = failed > 0 or partial > 0
        if (has_issues and Config.EMAIL_NOTIFY_ON_FAILURE) or (not has_issues and Config.EMAIL_NOTIFY_ON_SUCCESS):
            lines = [f"AR Automation finished at {datetime.now():%Y-%m-%d %H:%M:%S}", "",
                     f"Completed: {completed}", f"Partial:   {partial}", f"Failed:    {failed}", ""]
            for label, key in (("Failed practices:", "failed"), ("Partial practices:", "partial")):
                if summary[key]:
                    lines += [label] + [f"  - {p}" for p in summary[key]] + [""]
            self.send_email(title, "\n".join(lines))

    def notify_fatal_error(self, error: Exception, screenshot: Optional[Path] = None):
        title = "AR Automation FAILED"
        message = str(error)[:200]
        self.send_desktop(title, message)
        self.show_messagebox(title, message, "error")
        if Config.EMAIL_NOTIFY_ON_FAILURE:
            body = f"AR Automation stopped with a fatal error:\n\n{error}\n\n{traceback.format_exc()}"
            self.send_email(title, body, attachments=[screenshot] if screenshot else None)


notification_manager = NotificationManager()


# ==========================================================
# CIRCUIT BREAKER
# ==========================================================

class CircuitBreaker:
    """Raises after N consecutive practice failures; one success resets the count."""

    def __init__(self):
        self.threshold = Config.CIRCUIT_BREAKER_THRESHOLD
        self.enabled = Config.CIRCUIT_BREAKER_ENABLED
        self.consecutive_failures = 0

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self, practice_name: str = ""):
        if not self.enabled:
            return
        self.consecutive_failures += 1
        log(f"Circuit breaker: {self.consecutive_failures}/{self.threshold} consecutive failures", "warning")
        if self.consecutive_failures >= self.threshold:
            raise CircuitBreakerTripped(
                f"{self.consecutive_failures} practices failed in a row (last: '{practice_name}'). "
                f"Stopping run - check login/session/site status."
            )


# ==========================================================
# DOWNLOAD MANAGEMENT
# ==========================================================

class DownloadManager:
    """Monitors downloads and files them into per-practice folders."""

    REPORT_EXTENSIONS = (".xlsx", ".xls", ".pdf")

    def __init__(self, download_dir: Path):
        self.download_dir = download_dir

    @staticmethod
    def _strip_duplicate_suffix(filename: str) -> str:
        """'Report (1).xlsx' -> 'Report.xlsx'."""
        path = Path(filename)
        clean_stem = re.sub(r"\s*\(\d+\)$", "", path.stem).strip()
        return f"{clean_stem}{path.suffix}"

    def clean_download_folder(self):
        removed = 0
        for file in self.download_dir.iterdir():
            if file.suffix.lower() in self.REPORT_EXTENSIONS + (".crdownload", ".tmp"):
                try:
                    file.unlink()
                    removed += 1
                except OSError as e:
                    log(f"Could not remove {file.name}: {e}", "warning")
        log(f"Cleaned {removed} old files from download folder")

    def get_existing_files(self) -> set:
        return {f.name for f in self.download_dir.iterdir()
                if f.suffix.lower() in self.REPORT_EXTENSIONS}

    def wait_for_download(self, existing_files: set, timeout: int = Config.DOWNLOAD_TIMEOUT) -> str:
        end_time = time.time() + timeout
        while time.time() < end_time:
            if list(self.download_dir.glob("*.crdownload")):
                time.sleep(1)
                continue

            new_files = self.get_existing_files() - existing_files
            if new_files:
                time.sleep(2)  # let Chrome finish writing
                newest = max(new_files, key=lambda f: (self.download_dir / f).stat().st_ctime)
                log(f"Download completed: {newest}", "success")
                return newest
            time.sleep(1)

        raise TimeoutException(f"Download timeout after {timeout}s")

    def move_report(self, filename: str, practice: str, report_name: str,
                    keep_original_name: bool = False) -> Path:
        source = self.download_dir / filename
        if not source.exists():
            raise FileNotFoundError(f"Downloaded file missing: {filename}")

        practice_folder = self.download_dir / practice
        practice_folder.mkdir(exist_ok=True)

        if keep_original_name:
            destination = practice_folder / self._strip_duplicate_suffix(filename)
        else:
            destination = practice_folder / f"{report_name}{source.suffix}"

        for attempt in range(3):  # overwrite any previous copy
            if not destination.exists():
                break
            try:
                destination.unlink()
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(1)

        shutil.move(str(source), str(destination))
        log(f"Saved: {destination.relative_to(self.download_dir)}")
        return destination


# ==========================================================
# EXCEL MANAGEMENT
# ==========================================================

def _format_date(value: Any) -> str:
    """Normalize an Excel cell to MM/DD/YYYY (or '' if empty)."""
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%m/%d/%Y").strftime("%m/%d/%Y")
    except ValueError:
        return text


class ExcelManager:
    """Reads practice lists and writes colour-coded status back to Untouch.xlsx."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._workbook: Optional[openpyxl.Workbook] = None

    def load(self) -> "ExcelManager":
        if not self.file_path.exists():
            raise FileNotFoundError(
                f"Excel file not found: {self.file_path}\n"
                f"Run 'python create_template.py' to generate one, then fill it in."
            )
        self._workbook = openpyxl.load_workbook(self.file_path)
        log(f"Excel loaded: {self.file_path.name}")
        return self

    def get_sheet(self, name: str):
        if name not in self._workbook.sheetnames:
            raise ValueError(f"Sheet '{name}' not found. Available: {self._workbook.sheetnames}")
        return self._workbook[name]

    def read_login(self) -> Credentials:
        """Row 1 = headers (Username, Password, Security Key), row 2 = values."""
        ws = self.get_sheet("Login")
        headers = [str(c.value or "").strip() for c in ws[1]]
        values = [str(c.value or "").strip() for c in ws[2]]
        data = dict(zip(headers, values))

        missing = [k for k in ("Username", "Password", "Security Key") if not data.get(k)]
        if missing:
            raise ValueError(f"Login sheet is missing: {', '.join(missing)}")

        return Credentials(data["Username"], data["Password"], data["Security Key"])

    def get_report_code(self, sheet_name: str, default: str) -> str:
        """Search code = first non-empty Type cell (column A) of the sheet."""
        try:
            ws = self.get_sheet(sheet_name)
            for row in range(2, ws.max_row + 1):
                value = str(ws.cell(row, 1).value or "").strip()
                if value:
                    return value.upper()
        except Exception as e:
            log(f"Could not read Type from '{sheet_name}' ({e})", "warning")
        return default

    def read_age24_practices(self) -> List[AGE24Practice]:
        """Columns: A=Type, B=Practice, C=Date, D=Status."""
        ws = self.get_sheet("AGE24")
        practices = []

        for row in range(2, ws.max_row + 1):
            row_type = str(ws.cell(row, 1).value or "").strip()
            practice = str(ws.cell(row, 2).value or "").strip()
            status = str(ws.cell(row, 4).value or "").strip().lower()

            if not practice:
                continue
            if row_type and row_type.upper() != "AGE24":
                log(f"AGE24 row {row}: Type='{row_type}' is not AGE24 - skipped", "warning")
                continue

            if Config.FORCE_REPROCESS:
                status, invoice_done, service_done = "", False, False
            else:
                invoice_done = "invoice" in status or "downloaded" in status
                service_done = "service" in status or "downloaded" in status

            practices.append(AGE24Practice(
                practice_name=practice,
                service_date=_format_date(ws.cell(row, 3).value),
                status=status,
                row_number=row,
                invoice_downloaded=invoice_done,
                service_downloaded=service_done,
            ))

        log(f"Loaded {len(practices)} AGE24 practices")
        return practices

    def read_fin18_practices(self) -> List[FIN18Practice]:
        """Columns: A=Type, B=Practice, C=From Date, D=To Date, E=Status."""
        ws = self.get_sheet("FIN18")
        practices = []

        for row in range(2, ws.max_row + 1):
            row_type = str(ws.cell(row, 1).value or "").strip()
            practice = str(ws.cell(row, 2).value or "").strip()
            status = str(ws.cell(row, 5).value or "").strip().lower()

            if not practice:
                continue
            if row_type and row_type.upper() != "FIN18":
                log(f"FIN18 row {row}: Type='{row_type}' is not FIN18 - skipped", "warning")
                continue

            from_date = _format_date(ws.cell(row, 3).value)
            to_date = _format_date(ws.cell(row, 4).value)
            if not from_date or not to_date:
                log(f"FIN18 row {row} ({practice}): missing From/To date - skipped", "warning")
                continue

            practices.append(FIN18Practice(
                practice_name=practice,
                from_date=from_date,
                to_date=to_date,
                status="" if Config.FORCE_REPROCESS else status,
                row_number=row,
            ))

        log(f"Loaded {len(practices)} FIN18 practices")
        return practices

    def update_status(self, sheet_name: str, row: int, column: int, status: Status):
        cell = self.get_sheet(sheet_name).cell(row, column)
        cell.value = status.value
        cell.fill = PatternFill(fill_type="solid", fgColor=STATUS_COLORS[status])
        self.save()

    def save(self):
        if self._workbook:
            self._workbook.save(self.file_path)

    def close(self):
        if self._workbook:
            self._workbook.close()
            self._workbook = None


# ==========================================================
# BROWSER MANAGER
# ==========================================================

class BrowserManager:
    """Chrome WebDriver lifecycle."""

    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self.wait_utils: Optional["WaitUtils"] = None
        self.download_manager = DownloadManager(Config.DOWNLOAD_DIR)

    def create_driver(self) -> webdriver.Chrome:
        options = Options()
        options.page_load_strategy = Config.PAGE_LOAD_STRATEGY
        options.add_experimental_option("prefs", {
            "download.default_directory": str(Config.DOWNLOAD_DIR),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
            "safebrowsing.enabled": True,
            "profile.default_content_setting_values.automatic_downloads": 1,
        })

        if Config.HEADLESS:
            options.add_argument("--headless=new")
            options.add_argument(f"--window-size={Config.WINDOW_SIZE}")
        else:
            options.add_argument("--start-maximized")

        for arg in ("--disable-notifications", "--disable-popup-blocking", "--disable-infobars",
                    "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                    "--disable-extensions", "--remote-allow-origins=*",
                    "--disable-blink-features=AutomationControlled",
                    "--js-flags=--max-old-space-size=2048"):
            options.add_argument(arg)
        if Config.DISABLE_IMAGES:
            options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)

        try:
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            self.driver.set_page_load_timeout(Config.PAGE_LOAD_TIMEOUT)
            self.driver.implicitly_wait(Config.IMPLICIT_WAIT)
            if not Config.HEADLESS:
                self.driver.maximize_window()

            self._configure_download_behavior()
            self._hide_webdriver_flag()
            self.download_manager.clean_download_folder()

            log(f"Chrome started (headless={Config.HEADLESS})", "success")
            return self.driver
        except Exception as e:
            log(f"Failed to create Chrome driver: {e}", "error")
            raise

    def _configure_download_behavior(self):
        try:
            self.driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(Config.DOWNLOAD_DIR)})
        except Exception as e:
            log(f"Could not configure download behavior: {e}", "warning")

    def _hide_webdriver_flag(self):
        try:
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception:
            pass
        if Config.HEADLESS and Config.HEADLESS_STARTUP_DELAY > 0:
            time.sleep(Config.HEADLESS_STARTUP_DELAY)

    def quit(self):
        if not self.driver:
            return
        try:
            self.close_extra_windows()
            self.driver.quit()
            log("Chrome closed")
        except Exception as e:
            log(f"Error quitting driver: {e}", "warning")
        finally:
            self.driver = None

    def close_extra_windows(self):
        try:
            if not self.driver or len(self.driver.window_handles) <= 1:
                return
            main = self.driver.window_handles[0]
            for handle in self.driver.window_handles[1:]:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except Exception:
                    pass
            self.driver.switch_to.window(main)
        except Exception as e:
            log(f"close_extra_windows skipped: {e}", "warning")

    def reset_frame(self):
        try:
            self.driver.switch_to.default_content()
            time.sleep(0.5)
        except Exception:
            pass

    def save_screenshot(self, name: str) -> Optional[Path]:
        try:
            Config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = Config.SCREENSHOT_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
            self.driver.save_screenshot(str(path))
            log(f"Screenshot saved: {path.name}")
            return path
        except Exception as e:
            log(f"Screenshot failed: {e}", "warning")
            return None

    def hard_reset(self):
        log("Hard reset...", "warning")
        try:
            self.close_extra_windows()
            self.reset_frame()
            if "practicevelocity" not in self.driver.current_url:
                self.driver.get(Config.URL)
                time.sleep(3)
        except Exception as e:
            log(f"Hard reset failed: {e}", "error")


# ==========================================================
# WAIT UTILITIES
# ==========================================================

class WaitUtils:
    """Selenium wait / click / input helpers."""

    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver

    def wait(self, timeout: int = Config.ELEMENT_TIMEOUT) -> WebDriverWait:
        return WebDriverWait(self.driver, timeout)

    def safe_click(self, locator: Tuple, timeout: int = Config.ELEMENT_TIMEOUT):
        """Normal click first, JavaScript click as fallback."""
        try:
            element = self.wait(timeout).until(EC.element_to_be_clickable(locator))
            element.click()
            return element
        except Exception as e:
            log(f"Standard click failed, trying JS click: {str(e)[:80]}", "warning")

        element = self.wait(timeout).until(EC.presence_of_element_located(locator))
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.5)
        self.driver.execute_script("arguments[0].click();", element)
        return element

    def set_value(self, element, value: str, blur: bool = False) -> str:
        """Clear a field and set its value via JS (avoids stray '0' prefixes).
        Returns the value the field actually holds afterwards."""
        element.click()
        time.sleep(0.3)
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
        time.sleep(0.3)

        self.driver.execute_script("arguments[0].value = arguments[1];", element, value)
        for event in ("input", "change"):
            self.driver.execute_script(
                "arguments[0].dispatchEvent(new Event(arguments[1], {bubbles: true}));", element, event)
        if blur:  # some sites only run their date validation on blur
            self.driver.execute_script("arguments[0].blur();", element)
        time.sleep(0.3)
        return element.get_attribute("value") or ""

    def wait_for_popup(self, timeout: int = Config.POPUP_TIMEOUT) -> str:
        """Wait for the report popup, switch to it, return the main window handle."""
        self.wait(timeout).until(lambda d: len(d.window_handles) > 1)
        main = self.driver.current_window_handle
        popup = next(h for h in self.driver.window_handles if h != main)
        self.driver.switch_to.window(popup)
        self.wait().until(lambda d: d.execute_script("return document.readyState") == "complete")
        log("Report popup loaded")
        return main

    def switch_frame(self, locator: Tuple, timeout: int = Config.ELEMENT_TIMEOUT):
        self.wait(timeout).until(EC.frame_to_be_available_and_switch_to_it(locator))


# ==========================================================
# LOGIN (single attempt - no retry)
# ==========================================================

class LoginModule:
    """Username -> password -> TOTP login."""

    def __init__(self, driver: webdriver.Chrome, wait_utils: WaitUtils):
        self.driver = driver
        self.wait = wait_utils

    def _fill(self, field_id: str, value: str, secret: bool = False):
        """Type into a field, verify it, and fall back to JS if it doesn't match."""
        field = self.wait.wait().until(EC.presence_of_element_located((By.ID, field_id)))
        field.clear()
        time.sleep(0.5)
        field.send_keys(value)
        time.sleep(0.3)

        actual = field.get_attribute("value") or ""
        matches = len(actual) == len(value) if secret else actual == value
        if not matches:
            log(f"Field '{field_id}' mismatch - setting via JS", "warning")
            field.clear()
            self.driver.execute_script("arguments[0].value = arguments[1];", field, value)
            self.driver.execute_script("arguments[0].dispatchEvent(new Event('input'));", field)

    def login(self, credentials: Credentials) -> bool:
        self.driver.get(Config.URL)
        otp = pyotp.TOTP(credentials.security_key.replace(" ", "").upper()).now()
        log(f"Logging in as: {credentials.username}")

        self._fill("txtLogin", credentials.username)
        self.wait.safe_click((By.ID, "btnNext"))
        self.wait.safe_click((By.XPATH, "//*[@id='form20']/div[2]/input"))

        self._fill("input54", credentials.password, secret=True)
        self.wait.safe_click((By.XPATH, "//*[@id='form46']/div[2]/input"))

        self._fill("input80", otp)
        self.wait.safe_click((By.XPATH, "//*[@id='form72']/div[2]/input"))

        self.wait.wait(Config.POPUP_TIMEOUT).until(
            EC.presence_of_element_located((By.XPATH, "//*[@id='tdMenuBarItemPatient']/a")))
        log("Login successful", "success")
        return True


# ==========================================================
# NAVIGATION
# ==========================================================

class NavigationModule:
    """Menu navigation, frame switching and report search."""

    def __init__(self, driver: webdriver.Chrome, wait_utils: WaitUtils):
        self.driver = driver
        self.wait = wait_utils

    def open_administration(self):
        self.driver.switch_to.default_content()
        self.wait.safe_click((By.XPATH, "//*[@id='tdMenuBarItemAdministration']/a"))
        time.sleep(1)
        self.wait.safe_click((By.XPATH, "//*[@id='menu_Administration_Reports']"))
        log("Administration > Reports opened")

    def _switch_report_frame(self):
        self.driver.switch_to.default_content()
        self.wait.switch_frame((By.ID, "reportMainWindow"))

    def _switch_nav_frame(self):
        self._switch_report_frame()
        self.wait.switch_frame((By.NAME, "NavFrame"))

    def _switch_main_frame(self):
        self._switch_report_frame()
        self.wait.switch_frame((By.NAME, "PVRC_MainStage"))

    def search_report(self, report_code: str):
        """Search a report code and open the report page (practice selector visible)."""
        self.driver.switch_to.default_content()
        time.sleep(1)
        self._switch_nav_frame()

        search_box = self.wait.wait().until(EC.presence_of_element_located((By.ID, "userSearch")))
        actual = self.wait.set_value(search_box, report_code)
        if actual != report_code:
            log(f"Search box mismatch! Expected '{report_code}', got '{actual}'", "warning")

        self.wait.safe_click((By.ID, "dosearch"))
        log(f"Searched report: {report_code}")
        self._switch_main_frame()

        # A search normally lands on a results LIST first; some load the report directly.
        try:
            self.wait.wait(6).until(EC.presence_of_element_located((By.ID, "subpracticeselect")))
            return
        except TimeoutException:
            pass

        self._click_search_result(report_code)
        self.wait.wait().until(EC.presence_of_element_located((By.ID, "subpracticeselect")))
        log(f"{report_code} loaded")

    def _click_search_result(self, report_code: str):
        link_text = Config.REPORT_RESULT_LINK_TEXT.get(report_code)

        if link_text:
            try:
                link = self.wait.wait(10).until(EC.element_to_be_clickable(
                    (By.XPATH, f"//a[normalize-space()='{link_text}']")))
                link.click()
                log(f"Opened result: '{link_text}'")
                return
            except Exception as e:
                log(f"Result link '{link_text}' not found ({str(e)[:60]}) - using first result", "warning")

        try:
            first = self.wait.wait(10).until(EC.element_to_be_clickable(
                (By.XPATH, "(//div[@id='AllResults']//a | //*[@id='ResultTable']//a)[1]")))
        except TimeoutException:
            first = self.wait.wait(10).until(EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(text(), 'Search for')]/following::a[1]")))
        log(f"Opened first result: '{first.text.strip()}'")
        first.click()


# ==========================================================
# REPORT OPERATIONS
# ==========================================================

class ReportOperations:
    """Report criteria entry, execution and export."""

    DATE_TYPE_XPATHS = {
        "invoice": '//*[@id="rightcol"]/div[1]/div[5]/div/div/div/ul/li[1]/input',
        "service": '//*[@id="rightcol"]/div[1]/div[5]/div/div/div/ul/li[2]/input',
    }

    def __init__(self, driver: webdriver.Chrome, wait_utils: WaitUtils):
        self.driver = driver
        self.wait = wait_utils

    # ---------------- Practice selection ----------------

    def select_practice(self, practice: str):
        """Select via the classic dropdown, or the 'Clinics' checkbox panel (Super Practice)."""
        try:
            dropdown = self.wait.wait(6).until(EC.presence_of_element_located((By.ID, "subpracticeselect")))
        except TimeoutException:
            log("No practice dropdown - using Clinics checkbox panel")
            self._select_clinic_checkbox(practice)
            return

        time.sleep(1)
        select = Select(dropdown)
        try:
            select.select_by_visible_text(practice)
        except NoSuchElementException:
            options = [opt.text.strip() for opt in select.options]
            matching = [opt for opt in options if practice.lower() in opt.lower()]
            if not matching:
                raise Exception(f"Practice '{practice}' not found in dropdown. Available: {options[:10]}...")
            select.select_by_visible_text(matching[0])
            log(f"Practice matched partially: {matching[0]}")

        log(f"Practice selected: {practice}")
        time.sleep(1)

    def _find_clinic_checkbox(self, practice: str):
        """Locate the Clinics-panel checkbox for a practice, or None."""
        cb = "input[@type='checkbox']"
        strategies = [
            f"//{cb}[@value='{practice}']",
            f"//{cb}[@id='{practice}']",
            f"//{cb}[contains(@id, '{practice}')]",
            f"//{cb}[following-sibling::text()[1][normalize-space()='{practice}']]",
            f"//td[normalize-space(text())='{practice}']/preceding-sibling::td[1]//{cb}",
            f"//td[normalize-space(text())='{practice}']//{cb}",
            f"//label[normalize-space(text())='{practice}']/{cb}",
            f"//label[normalize-space(text())='{practice}']/preceding-sibling::{cb}[1]",
            f"//*[normalize-space(text())='{practice}']/preceding::{cb}[1]",
        ]
        for xpath in strategies:
            try:
                return self.driver.find_element(By.XPATH, xpath)
            except NoSuchElementException:
                continue
        return None

    def _select_clinic_checkbox(self, practice: str):
        """Uncheck all clinics, then tick only the target."""
        try:
            uncheck_all = self.wait.wait(10).until(EC.element_to_be_clickable((
                By.XPATH,
                "(//input[@value='Uncheck All'] | //button[normalize-space()='Uncheck All'] "
                "| //a[normalize-space()='Uncheck All'])[1]")))
            uncheck_all.click()
            time.sleep(0.5)
        except Exception as e:
            log(f"Could not click 'Uncheck All' (continuing): {str(e)[:80]}", "warning")

        try:
            checkbox = self.wait.wait(10).until(lambda d: self._find_clinic_checkbox(practice))
        except TimeoutException:
            raise Exception(
                f"No Clinics checkbox found for '{practice}'. Inspect a checkbox (F12) and "
                f"update the selector list in _find_clinic_checkbox().")

        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
        time.sleep(0.3)
        if not checkbox.is_selected():
            try:
                checkbox.click()
            except (ElementClickInterceptedException, ElementNotInteractableException):
                self.driver.execute_script("arguments[0].click();", checkbox)
        time.sleep(0.5)
        log(f"Practice selected (checkbox): {practice}")

    def verify_practice(self, practice: str, max_attempts: int = 20):
        for attempt in range(1, max_attempts + 1):
            try:
                try:
                    dropdown = self.driver.find_element(By.ID, "subpracticeselect")
                    selected = Select(dropdown).first_selected_option.text.strip()
                    if practice.lower() in selected.lower():
                        log(f"Practice verified: {selected}", "success")
                        return
                except NoSuchElementException:
                    pass

                checkbox = self._find_clinic_checkbox(practice)
                if checkbox is not None and checkbox.is_selected():
                    log(f"Practice verified (checkbox): {practice}", "success")
                    return
            except StaleElementReferenceException:
                log(f"Stale element on attempt {attempt}, retrying", "warning")
            except Exception as e:
                log(f"Verify error on attempt {attempt}: {e}", "warning")
            time.sleep(1)

        raise Exception(f"Practice not changed to: {practice} after {max_attempts} attempts")

    # ---------------- Criteria ----------------

    def open_criteria(self):
        """Open the criteria panel (older layout). No-op if it's already open."""
        try:
            self.wait.safe_click((By.ID, "mainbutton1"), timeout=8)
            time.sleep(1)
        except Exception:
            log("Criteria toggle not found - assuming Parameters panel is open")

    def enter_service_date(self, date: str):
        field = self.wait.wait().until(EC.presence_of_element_located((By.ID, "ServiceDate")))
        actual = self.wait.set_value(field, date)
        if actual != date:
            log(f"ServiceDate mismatch! Expected '{date}', got '{actual}'", "warning")
        log(f"Service date set: {actual}")

    def enter_date_range(self, from_date: str, to_date: str):
        """Set From/To dates. Some pages auto-sync one field when the other changes,
        so both are re-checked after being set and re-applied if they drifted."""
        targets = {"FromServiceDate": from_date, "ToServiceDate": to_date}

        def set_field(field_id: str) -> str:
            field = self.wait.wait().until(EC.presence_of_element_located((By.ID, field_id)))
            return self.wait.set_value(field, targets[field_id], blur=True)

        for field_id in targets:
            set_field(field_id)

        for field_id, expected in targets.items():
            actual = self.driver.find_element(By.ID, field_id).get_attribute("value") or ""
            if actual != expected:
                log(f"{field_id} drifted to '{actual}' - reapplying", "warning")
                actual = set_field(field_id)
            if actual != expected:
                log(f"{field_id} mismatch! Expected '{expected}', got '{actual}'", "warning")

        log(f"Date range set: {from_date} to {to_date}")

    def select_date_type(self, which: str):
        """Tick the 'invoice' or 'service' date option and untick the other."""
        other = "service" if which == "invoice" else "invoice"
        try:
            target_el = self.wait.wait().until(
                EC.presence_of_element_located((By.XPATH, self.DATE_TYPE_XPATHS[which])))
            other_el = self.driver.find_element(By.XPATH, self.DATE_TYPE_XPATHS[other])

            if other_el.is_selected():
                self.driver.execute_script("arguments[0].click();", other_el)
                time.sleep(0.5)
            if not target_el.is_selected():
                self.driver.execute_script("arguments[0].click();", target_el)
                time.sleep(0.5)
            log(f"{which.capitalize()} date selected")
        except Exception as e:
            log(f"{which.capitalize()} date selection issue: {e}", "warning")
            self._toggle_date_option_by_label(which)

    def _toggle_date_option_by_label(self, which: str):
        label_text = "Invoice Date" if which == "invoice" else "Service Date"
        try:
            label = self.driver.find_element(
                By.XPATH, f"//label[contains(text(), '{label_text}') or contains(@for, '{which.capitalize()}')]")
            self.driver.execute_script("arguments[0].click();", label)
        except Exception as e:
            log(f"Label toggle also failed: {e}", "warning")

    # ---------------- Run / export ----------------

    def run_report(self):
        self.wait.safe_click((By.XPATH, '//*[@id="rightcol"]/div[2]/input'))
        log("Run report clicked")
        time.sleep(2)

    def export_excel(self, download_manager: DownloadManager) -> str:
        current_url = self.driver.current_url
        if "ReportViewer.aspx" not in current_url:
            raise Exception("Not on ReportViewer page")

        export_url = current_url if "rs:Format" in current_url else current_url + "&rs:Format=EXCELOPENXML"
        existing_files = download_manager.get_existing_files()

        self.driver.execute_script("window.location.href = arguments[0];", export_url)
        log("Download triggered")
        time.sleep(3)
        return download_manager.wait_for_download(existing_files)


# ==========================================================
# DOWNLOAD MODULE
# ==========================================================

class DownloadModule:
    """Runs a report, exports it, files it and validates the result."""

    def __init__(self, browser: BrowserManager, report_ops: ReportOperations,
                 download_manager: DownloadManager):
        self.browser = browser
        self.report_ops = report_ops
        self.download_manager = download_manager
        self.driver = browser.driver

    def download_report(self, practice: str, report_name: str, keep_original_name: bool = False) -> Path:
        log(f"Downloading {report_name} for {practice}")

        self.report_ops.run_report()
        main_window = self.browser.wait_utils.wait_for_popup()

        downloaded = self.report_ops.export_excel(self.download_manager)
        destination = self.download_manager.move_report(downloaded, practice, report_name, keep_original_name)

        self._validate_excel(destination)
        self._close_popup(main_window)
        return destination

    @staticmethod
    def _validate_excel(path: Path):
        if not path.exists():
            raise FileNotFoundError("Downloaded file missing")
        if path.stat().st_size == 0:
            raise ValueError("Downloaded file is empty")
        try:
            openpyxl.load_workbook(path).close()
        except Exception:
            raise ValueError("Corrupted Excel file")

    def _close_popup(self, main_window: str):
        try:
            self.driver.close()
        except Exception:
            pass
        self.driver.switch_to.window(main_window)
        self.driver.switch_to.default_content()
        time.sleep(1)


# ==========================================================
# REPORT MODULES
# ==========================================================

class BaseReportModule:
    """Shared plumbing for the AGE24 and FIN18 modules."""

    def __init__(self, browser: BrowserManager, navigation: NavigationModule,
                 report_ops: ReportOperations, download_module: DownloadModule,
                 excel: ExcelManager, auto_fix: "AutoFixEngine",
                 circuit_breaker: CircuitBreaker, report_code: str):
        self.browser = browser
        self.navigation = navigation
        self.report_ops = report_ops
        self.download_module = download_module
        self.excel = excel
        self.auto_fix = auto_fix
        self.circuit_breaker = circuit_breaker
        self.report_code = report_code  # search term, taken from the sheet's Type column
        self.last_exception: Optional[Exception] = None

    def _prepare_common(self, practice: str):
        # Always re-search: after a download the browser is left on the RESULTS page,
        # which has no practice selector.
        self.navigation.search_report(self.report_code)
        self.report_ops.select_practice(practice)
        self.report_ops.verify_practice(practice)
        self.report_ops.open_criteria()

    def _handle_failure(self, label: str, practice_name: str, e: Exception):
        log(f"{label} failed for {practice_name}: {e}", "error")
        self.last_exception = e
        self.browser.save_screenshot(f"{label}_{practice_name}")
        self.browser.close_extra_windows()
        self.browser.reset_frame()

    def _prepare_next_attempt(self, label: str, practice_name: str):
        """Diagnose the last error, back off, and return to the report search page."""
        if self.last_exception:
            self.auto_fix.diagnose_and_fix(self.last_exception, context=f"{label}_{practice_name}")
        time.sleep(Config.RETRY_DELAY)
        self.navigation.search_report(self.report_code)

    def _record_result(self, ok: bool, practice_name: str):
        if ok:
            self.circuit_breaker.record_success()
        else:
            log(f"All retries failed for {practice_name}", "error")
            self.circuit_breaker.record_failure(practice_name)  # may raise CircuitBreakerTripped

    def _already_done(self, label: str, practice_name: str, status: str, sheet: str,
                      row: int, status_col: int) -> bool:
        """True if this practice should be skipped (done per Excel or per saved state)."""
        if Config.FORCE_REPROCESS:
            return False
        if status == "downloaded":
            log(f"Skipping (already downloaded): {practice_name}")
            return True
        if state_manager.is_completed(f"{label}_{practice_name}"):
            log(f"Skipping (completed in previous run): {practice_name}")
            self.excel.update_status(sheet, row, status_col, Status.DOWNLOADED)
            return True
        return False


class AGE24Module(BaseReportModule):
    """AGE24: downloads BOTH the Invoice Date and Service Date reports."""

    SHEET, STATUS_COL = "AGE24", 4

    def _download(self, practice: AGE24Practice, which: str) -> bool:
        try:
            log(f"{which.upper()} DATE report - {practice.practice_name}")
            self._prepare_common(practice.practice_name)
            self.report_ops.enter_service_date(practice.service_date)
            self.report_ops.select_date_type(which)

            if which == "invoice":
                self.download_module.download_report(
                    practice.practice_name, Config.AGE24_INVOICE_NAME, keep_original_name=False)
            else:  # Service Date report keeps its original filename
                self.download_module.download_report(
                    practice.practice_name, Config.AGE24_SERVICE_NAME, keep_original_name=True)

            log(f"{which.capitalize()} Date report done for {practice.practice_name}", "success")
            return True
        except Exception as e:
            self.last_exception = e
            log(f"{which.capitalize()} Date download failed: {e}", "error")
            return False

    def process_practice(self, practice: AGE24Practice) -> Tuple[bool, bool]:
        name = practice.practice_name
        log_header(f"AGE24 | {name} | Service Date: {practice.service_date}")

        try:
            invoice_ok = practice.invoice_downloaded or self._download(practice, "invoice")
            service_ok = practice.service_downloaded or self._download(practice, "service")

            key = f"AGE24_{name}"
            if invoice_ok and service_ok:
                self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.DOWNLOADED)
                state_manager.mark_completed(key)
                log(f"AGE24 fully completed: {name}", "success")
            elif invoice_ok or service_ok:
                self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.PARTIAL)
                state_manager.mark_partial(key)
                log(f"AGE24 partially completed: {name}", "warning")
            else:
                self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.FAILED)
                state_manager.mark_failed(key)
                log(f"AGE24 failed: {name}", "error")
            return invoice_ok, service_ok

        except Exception as e:
            self._handle_failure("AGE24", name, e)
            self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.FAILED)
            state_manager.mark_failed(f"AGE24_{name}")
            return False, False

    def run(self):
        state_manager.set_module("AGE24")
        log_header("AGE24 module (Invoice Date + Service Date)")

        self.navigation.search_report(self.report_code)

        for practice in self.excel.read_age24_practices():
            if self._already_done("AGE24", practice.practice_name, practice.status,
                                  self.SHEET, practice.row_number, self.STATUS_COL):
                continue

            invoice_ok = service_ok = False
            self.last_exception = None

            for attempt in range(1, Config.MAX_RETRIES + 1):
                log(f"Attempt {attempt}/{Config.MAX_RETRIES}")
                invoice_ok, service_ok = self.process_practice(practice)
                if invoice_ok and service_ok:
                    break
                if attempt < Config.MAX_RETRIES:
                    # only retry whichever report is still missing
                    practice.invoice_downloaded = invoice_ok
                    practice.service_downloaded = service_ok
                    self._prepare_next_attempt("AGE24", practice.practice_name)

            self._record_result(invoice_ok and service_ok, practice.practice_name)

        log("AGE24 module completed", "success")


class FIN18Module(BaseReportModule):
    """FIN18: one report per practice, keeps the original filename."""

    SHEET, STATUS_COL = "FIN18", 5

    def process_practice(self, practice: FIN18Practice) -> bool:
        name = practice.practice_name
        log_header(f"FIN18 | {name} | {practice.from_date} to {practice.to_date}")

        try:
            self._prepare_common(name)
            self.report_ops.enter_date_range(practice.from_date, practice.to_date)
            self.download_module.download_report(name, Config.FIN18_NAME, keep_original_name=True)

            self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.DOWNLOADED)
            state_manager.mark_completed(f"FIN18_{name}")
            log(f"FIN18 completed: {name}", "success")
            return True

        except Exception as e:
            self._handle_failure("FIN18", name, e)
            self.excel.update_status(self.SHEET, practice.row_number, self.STATUS_COL, Status.FAILED)
            state_manager.mark_failed(f"FIN18_{name}")
            return False

    def run(self):
        state_manager.set_module("FIN18")
        log_header("FIN18 module")

        self.navigation.search_report(self.report_code)

        for practice in self.excel.read_fin18_practices():
            if self._already_done("FIN18", practice.practice_name, practice.status,
                                  self.SHEET, practice.row_number, self.STATUS_COL):
                continue

            success = False
            self.last_exception = None

            for attempt in range(1, Config.MAX_RETRIES + 1):
                log(f"Attempt {attempt}/{Config.MAX_RETRIES}")
                success = self.process_practice(practice)
                if success:
                    break
                if attempt < Config.MAX_RETRIES:
                    self._prepare_next_attempt("FIN18", practice.practice_name)

            self._record_result(success, practice.practice_name)

        log("FIN18 module completed", "success")


# ==========================================================
# AUTO-FIX ENGINE
# ==========================================================

class AutoFixEngine:
    """Applies a recovery step based on the error message before a retry."""

    def __init__(self, browser: BrowserManager):
        self.browser = browser
        self.driver = browser.driver
        self.fixes_applied: List[str] = []

    def diagnose_and_fix(self, error: Exception, context: str = "") -> bool:
        error_str = str(error).lower()
        log(f"AutoFix diagnosing [{context}]: {error_str[:120]}", "warning")

        if "stale" in error_str:
            return self._fix_stale_element()
        if "interactable" in error_str or "intercepted" in error_str:
            return self._fix_element_blocked()
        if "frame" in error_str:
            return self._fix_frame_issue()
        if "session" in error_str or "disconnected" in error_str:
            log("AutoFix: session lost - needs a fresh run", "error")
            self.fixes_applied.append("session_relogin_needed")
            return False
        if "timeout" in error_str:
            return self._fix_timeout()
        if "practice" in error_str and ("not found" in error_str or "dropdown" in error_str):
            return self._fix_practice_dropdown()

        log("No specific auto-fix available", "warning")
        return False

    def _fix_stale_element(self) -> bool:
        time.sleep(2)
        self.browser.reset_frame()
        time.sleep(1)
        self.fixes_applied.append("stale_element_refresh")
        return True

    def _fix_element_blocked(self) -> bool:
        self.browser.close_extra_windows()
        self.browser.reset_frame()
        try:
            self.driver.execute_script(
                "document.querySelectorAll('.modal, .overlay, .popup, [class*=\"modal\"], "
                "[class*=\"overlay\"]').forEach(o => o.style.display = 'none');")
        except Exception:
            pass
        time.sleep(1)
        self.fixes_applied.append("element_blocked_overlay")
        return True

    def _fix_frame_issue(self) -> bool:
        self.browser.hard_reset()
        self.fixes_applied.append("frame_hard_reset")
        return True

    def _fix_timeout(self) -> bool:
        time.sleep(5)
        self.fixes_applied.append("timeout_extended_wait")
        return True

    def _fix_practice_dropdown(self) -> bool:
        self.browser.reset_frame()
        time.sleep(2)
        self.fixes_applied.append("practice_dropdown_research")
        return True

    def get_fix_summary(self) -> str:
        if not self.fixes_applied:
            return "No auto-fixes applied"
        return f"Auto-fixes applied: {', '.join(self.fixes_applied)}"


# ==========================================================
# MAIN CONTROLLER
# ==========================================================

class AutomationController:
    """Wires everything together and runs the full automation."""

    def __init__(self):
        self.browser: Optional[BrowserManager] = None
        self.excel: Optional[ExcelManager] = None
        self.login: Optional[LoginModule] = None
        self.navigation: Optional[NavigationModule] = None
        self.age24: Optional[AGE24Module] = None
        self.fin18: Optional[FIN18Module] = None
        self.auto_fix: Optional[AutoFixEngine] = None

    def initialize(self):
        log_header("AR Automation starting")
        Config.ensure_directories()

        self.excel = ExcelManager(Config.EXCEL_FILE).load()
        credentials = self.excel.read_login()  # fail fast, before the browser opens
        log(f"Credentials loaded for: {credentials.username}")

        self.browser = BrowserManager()
        driver = self.browser.create_driver()
        self.browser.wait_utils = WaitUtils(driver)
        wait_utils = self.browser.wait_utils

        self.login = LoginModule(driver, wait_utils)
        self.navigation = NavigationModule(driver, wait_utils)
        report_ops = ReportOperations(driver, wait_utils)
        download_module = DownloadModule(self.browser, report_ops, DownloadManager(Config.DOWNLOAD_DIR))

        self.auto_fix = AutoFixEngine(self.browser)
        circuit_breaker = CircuitBreaker()

        def build(module_cls, sheet: str):
            return module_cls(
                self.browser, self.navigation, report_ops, download_module, self.excel,
                self.auto_fix, circuit_breaker,
                report_code=self.excel.get_report_code(sheet, default=sheet),
            )

        self.age24 = build(AGE24Module, "AGE24")
        self.fin18 = build(FIN18Module, "FIN18")

        self.login.login(credentials)
        notification_manager.notify_start()

    def run(self):
        try:
            self.initialize()
            self.navigation.open_administration()
            self.age24.run()
            self.fin18.run()
            self.excel.save()

            log_header("Automation completed successfully")
            log(self.auto_fix.get_fix_summary())
            notification_manager.notify_run_complete(self._build_summary())

        except CircuitBreakerTripped as e:
            log(f"Circuit breaker tripped - run stopped early: {e}", "critical")
            if self.excel:
                self.excel.save()
            screenshot = self.browser.save_screenshot("Circuit_Breaker_Tripped") if self.browser else None
            notification_manager.notify_fatal_error(e, screenshot=screenshot)
            raise

        except Exception as e:
            log(f"Fatal error: {e}", "critical")
            traceback.print_exc()
            if self.auto_fix:
                self.auto_fix.diagnose_and_fix(e, "main_controller")
            screenshot = self.browser.save_screenshot("Fatal_Error") if self.browser else None
            notification_manager.notify_fatal_error(e, screenshot=screenshot)
            raise

        finally:
            self.cleanup()

    @staticmethod
    def _build_summary() -> Dict[str, List[str]]:
        state = state_manager.state
        return {
            "completed": list(state.completed_practices),
            "failed": list(state.failed_practices),
            "partial": list(state.partial_practices),
        }

    def cleanup(self):
        log("Cleaning up...")
        if self.excel:
            try:
                self.excel.save()
                self.excel.close()
            except Exception as e:
                log(f"Excel cleanup error: {e}", "warning")
        if self.browser:
            self.browser.quit()
        state_manager.save()
        log("Cleanup complete", "success")


# ==========================================================
# ENTRY POINT
# ==========================================================

def main():
    controller = AutomationController()
    try:
        controller.run()
    except KeyboardInterrupt:
        log("Interrupted by user", "warning")
        controller.cleanup()
        sys.exit(1)
    except Exception as e:
        log(f"Unhandled exception: {e}", "critical")
        sys.exit(1)


if __name__ == "__main__":
    main()
