#!/usr/bin/env python3
"""
Inbar Grade Monitor — Bar-Ilan University

Two modes:
  python monitor.py          → one-shot check (run via cron)
  python monitor.py --bot    → interactive Telegram bot daemon
"""

import asyncio
import datetime
import json
import os
import sys
import time as _time
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ─── Load credentials from .env (never hardcode secrets) ─────────────────────
load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# --daemon routes through relay; --bot requires tokens; one-shot works without (just no alerts)
if "--bot" in sys.argv and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
    print("[ERROR] --bot mode requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    sys.exit(1)

# ─── Constants ────────────────────────────────────────────────────────────────
TARGET_URL      = "https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx"
BASE_DIR        = Path(__file__).parent
STORAGE_FILE    = BASE_DIR / "session_cookies.json"   # legacy; kept for reference
USER_DATA_DIR   = BASE_DIR / "inbar_profile"          # persistent Chromium profile
DATA_FILE       = BASE_DIR / "data.json"
USERS_FILE      = BASE_DIR / "users.json"
DEBUG_HTML_FILE = BASE_DIR / "debug_page.html"

APPEAL_IN_PROGRESS = "בקשה בטיפול"

# ─── Daemon smart-polling constants ───────────────────────────────────────────
_FAST_SECS      = 10 * 60
_SLOW_SECS      = 30 * 60
_KEEPALIVE_SECS = 12 * 60        # max gap even when nothing pending — keeps server session alive
_DAY_START      = 8
_DAY_END        = 23
_CRIT_DAYS      = 6   # critical window starts 6 days after exam, runs until grade appears
REJECTION_KEYWORDS = [
    "נדחה", "נדחית", "נדחתה",            # rejected (masc/fem variants)
    "לא אושר", "לא אושרה",
    "לא התקבל", "לא התקבלה",
    "נשמר", "אושר הציון המקורי",
]
LOGIN_KEYWORDS     = ["login", "signin", "logon", "shibboleth", "adfs", "auth", "idp", "wayf", "saml"]

CONTEXT_OPTS = dict(
    locale="he-IL",
    timezone_id="Asia/Jerusalem",
    viewport={"width": 1280, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Macintosh; ARM Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
)

# ─── Bot keyboard button labels ───────────────────────────────────────────────
BTN_GRADES     = "📊 רשימת הציונים הסופיים שלי"
BTN_CONNECTION = "🔌 האם אני מחובר?"


# ─── URL helpers ──────────────────────────────────────────────────────────────

def _is_login_page(url: str) -> bool:
    return any(kw in url.lower() for kw in LOGIN_KEYWORDS)


def _is_on_grades_page(url: str) -> bool:
    return "inbar.biu.ac.il" in url.lower() and not _is_login_page(url)


def _is_empty(val: str) -> bool:
    return not val or val.strip().replace(" ", "") in ("", "—", "-", "נ/א")


def _appeal_approved(new_status: str, old_grade: str, new_grade: str) -> bool:
    if any(kw in new_status for kw in REJECTION_KEYWORDS):
        return False
    try:
        if float(new_grade) > float(old_grade):
            return True
    except (ValueError, TypeError):
        pass
    return True


# ─── Telegram helpers ─────────────────────────────────────────────────────────

def _tg_post(method: str, payload: dict) -> dict:
    """Low-level Telegram API call. Returns the parsed JSON response."""
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        json=payload,
        timeout=35,
    )
    return resp.json()


def send_telegram(message: str, chat_id: str = None) -> None:
    """Send a message to one specific chat_id (defaults to owner from .env)."""
    target = str(chat_id) if chat_id else TELEGRAM_CHAT_ID
    try:
        r = _tg_post("sendMessage", {
            "chat_id": target,
            "text": message,
            "parse_mode": "HTML",
        })
        if r.get("ok"):
            print(f"[TELEGRAM] Message sent to {target}.")
        else:
            print(f"[TELEGRAM] API error: {r}")
    except requests.RequestException as e:
        print(f"[TELEGRAM] Failed for {target}: {e}")


def broadcast_alert(message: str) -> None:
    """Send a grade alert to ALL registered users."""
    for uid in load_users():
        send_telegram(message, chat_id=uid)


def _send_with_keyboard(text: str, chat_id: str = None) -> None:
    """Send a message with the persistent reply keyboard to a specific user."""
    target = str(chat_id) if chat_id else TELEGRAM_CHAT_ID
    keyboard = {
        "keyboard": [
            [{"text": BTN_GRADES}],
            [{"text": BTN_CONNECTION}],
        ],
        "resize_keyboard": True,
        "persistent": True,
        "is_persistent": True,
    }
    try:
        r = _tg_post("sendMessage", {
            "chat_id": target,
            "text": text,
            "reply_markup": keyboard,
        })
        if not r.get("ok"):
            print(f"[BOT] sendMessage failed: {r}")
    except requests.RequestException as e:
        print(f"[BOT] sendMessage error: {e}")


def _fetch_updates(offset: int) -> list:
    """Long-poll Telegram for new messages (blocks up to 30 s)."""
    try:
        r = _tg_post("getUpdates", {"offset": offset, "timeout": 30})
        return r.get("result", []) if r.get("ok") else []
    except requests.RequestException:
        return []


# ─── Data persistence ─────────────────────────────────────────────────────────

def load_saved() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}


def _lock(path: Path) -> None:
    """Restrict file to owner read/write only (like an SSH key)."""
    path.chmod(0o600)


def save_current(data: dict) -> None:
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _lock(DATA_FILE)
    print(f"[DATA] {len(data)} row(s) saved → {DATA_FILE.name}")


# ─── User registry ────────────────────────────────────────────────────────────

def load_users() -> set:
    """Return set of registered Telegram chat IDs. Falls back to owner."""
    if USERS_FILE.exists():
        return set(str(uid) for uid in json.loads(USERS_FILE.read_text(encoding="utf-8")))
    return {TELEGRAM_CHAT_ID} if TELEGRAM_CHAT_ID else set()


def save_users(users: set) -> None:
    USERS_FILE.write_text(
        json.dumps(sorted(users), ensure_ascii=False),
        encoding="utf-8",
    )
    _lock(USERS_FILE)


def register_user(chat_id: str) -> bool:
    """Register a user. Returns True if newly added."""
    users = load_users()
    cid = str(chat_id)
    if cid not in users:
        users.add(cid)
        save_users(users)
        print(f"[BOT] New user registered: {cid}")
        return True
    return False


# ─── Change detection & alerting ──────────────────────────────────────────────

def diff_and_alert(old: dict, new: dict, sender=None) -> None:
    _send = sender if sender is not None else broadcast_alert
    alerts_sent = 0

    for key, info in new.items():
        course   = info.get("course_name", "?")
        lecturer = info.get("lecturer", "?")
        moed     = info.get("moed", "?")
        grade    = info.get("grade", "—")
        fg       = info.get("final_grade", "—")
        appeal   = info.get("appeal_status", "—")

        old_info   = old.get(key, {})
        old_grade  = old_info.get("grade", "—")
        old_fg     = old_info.get("final_grade", "—")
        old_appeal = old_info.get("appeal_status", "—")

        # ── ציון appeared ─────────────────────────────────────────────────
        if not _is_empty(grade) and _is_empty(old_grade):
            alerts_sent += 1
            _send(
                f"אל תילחץ אחי אבל עלה ציון ב{course} {moed} - "
                f"ציונך הוא {grade} מקווה שאתה מרוצה 🎓"
            )
            print(f"[ALERT] ציון — {course} {moed}: {grade}")

        # ── ציון סופי appeared ────────────────────────────────────────────
        if not _is_empty(fg) and _is_empty(old_fg):
            alerts_sent += 1
            _send(
                f"אל תילחץ אחי אבל עלה ציון סופי ב{course} - "
                f"הציון הסופי שלך הוא {fg} מקווה שאתה מרוצה 🎓"
            )
            print(f"[ALERT] ציון סופי — {course}: {fg}")

        # ── Appeal status changed ─────────────────────────────────────────
        if old_appeal == APPEAL_IN_PROGRESS and appeal != APPEAL_IN_PROGRESS:
            alerts_sent += 1
            approved = _appeal_approved(appeal, old_grade, grade)
            if approved:
                verdict = "אישר"
                if not _is_empty(grade) and grade != old_grade and not _is_empty(old_grade):
                    tail = f"\nציון חדש: {grade} (היה {old_grade})"
                elif not _is_empty(grade):
                    tail = f"\nהציון נשאר {grade}"
                else:
                    tail = ""
            else:
                verdict = "לא אישר"
                tail = f"\nהציון נשאר {old_grade}" if not _is_empty(old_grade) else ""
            _send(
                f"היי אחי המרצה {lecturer} {verdict} את הערעור שלך בקורס {course} ⚖️{tail}"
            )
            print(f"[ALERT] Appeal — {course}: {lecturer} → {verdict}")

    if alerts_sent == 0:
        print("[DIFF] No relevant changes detected.")
    else:
        print(f"[DIFF] {alerts_sent} alert(s) sent.")


# ─── Browser session management ───────────────────────────────────────────────

async def _wait_for_manual_login(page) -> None:
    print("\n[LOGIN] Browser is open — please log in now.")
    print("[LOGIN] The script continues automatically after login.\n")
    while True:
        await asyncio.sleep(3)
        try:
            url = page.url
        except Exception:
            break
        if _is_on_grades_page(url):
            print("[LOGIN] Login detected! Saving session...")
            await asyncio.sleep(2)
            break


async def _open_headless(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(storage_state=str(STORAGE_FILE), **CONTEXT_OPTS)
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = await ctx.new_page()
    return browser, ctx, page


async def get_authenticated_context(playwright):
    if STORAGE_FILE.exists():
        print("[BROWSER] Found saved session. Trying headless...")
        browser, ctx, page = await _open_headless(playwright)
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2_000)
            if _is_on_grades_page(page.url):
                print("[BROWSER] Session valid — running headless.")
                return browser, ctx, page
            print(f"[BROWSER] Session expired (at: {page.url}).")
        except Exception as e:
            print(f"[BROWSER] Headless error: {e}")
        await ctx.close()
        await browser.close()

    print("\n" + "=" * 62)
    print("  MANUAL LOGIN REQUIRED")
    print("=" * 62)
    print("  A browser window will open. Log in to Inbar.")
    print("  The script resumes automatically after login.")
    print("  Do NOT close the window yourself.")
    print("=" * 62)

    browser = await playwright.chromium.launch(headless=False)
    ctx = await browser.new_context(**CONTEXT_OPTS)
    page = await ctx.new_page()
    await page.goto(TARGET_URL)
    await _wait_for_manual_login(page)

    await ctx.storage_state(path=str(STORAGE_FILE))
    _lock(STORAGE_FILE)
    print(f"[SESSION] Cookies saved → {STORAGE_FILE.name}")
    await ctx.close()
    await browser.close()

    print("[BROWSER] Reopening headless with saved cookies...")
    browser, ctx, page = await _open_headless(playwright)
    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
    return browser, ctx, page


# ─── Grade extraction ─────────────────────────────────────────────────────────

def _detect_columns(headers: list[str]) -> dict:
    """Map column roles to indices from the GridView header row."""
    col = {k: -1 for k in ("course_code", "course_name", "lecturer", "moed",
                            "grade", "final_grade", "appeal", "date")}
    for i, h in enumerate(headers):
        h = h.strip()
        if "ציון סופי" in h and col["final_grade"] == -1:
            col["final_grade"] = i
        elif h == "ציון" and col["grade"] == -1:
            col["grade"] = i
        elif "שם קבוצת קורס" in h and col["course_name"] == -1:
            col["course_name"] = i
        elif "קוד קבוצת קורס" in h and col["course_code"] == -1:
            col["course_code"] = i
        elif "שם המרצה" in h and col["lecturer"] == -1:
            col["lecturer"] = i
        elif h == "מועד" and col["moed"] == -1:
            col["moed"] = i
        elif "תאריך" in h and col["date"] == -1:
            col["date"] = i
        elif ("ערעור" in h) and col["appeal"] == -1:
            col["appeal"] = i
    # Broad fallbacks
    if col["course_name"] == -1:
        for i, h in enumerate(headers):
            if any(kw in h for kw in ["קורס", "מקצוע"]) and col["course_name"] == -1:
                col["course_name"] = i
    if col["grade"] == -1:
        for i, h in enumerate(headers):
            if "ציון" in h and i != col["final_grade"] and col["grade"] == -1:
                col["grade"] = i
    return col


async def _parse_rows(rows, col: dict) -> dict:
    """Extract grade entries from a list of <tr> elements."""
    grades = {}
    for row in rows[1:]:  # skip header row
        cells = await row.query_selector_all("td")
        if not cells:
            continue
        texts = [(await c.inner_text()).strip() for c in cells]
        if not any(texts):
            continue

        def _get(idx: int) -> str:
            return texts[idx].strip() if 0 <= idx < len(texts) else "—"

        course_name   = _get(col["course_name"])
        course_code   = _get(col["course_code"])
        lecturer      = _get(col["lecturer"])
        moed          = _get(col["moed"])
        grade         = _get(col["grade"])
        final_grade   = _get(col["final_grade"])
        appeal_status = _get(col["appeal"])
        date          = _get(col["date"])

        if not course_name or course_name == "—":
            continue

        id_part = course_code if (course_code and course_code != "—") else course_name
        # Include date in key so retaken courses in different years don't collide
        key = f"{id_part}|{moed}|{date}"

        grades[key] = {
            "course_name":   course_name,
            "course_code":   course_code,
            "lecturer":      lecturer,
            "moed":          moed,
            "date":          date,
            "grade":         grade,
            "final_grade":   final_grade,
            "appeal_status": appeal_status,
        }
    return grades


async def extract_grades(page) -> dict:
    """
    Scrape ALL grades across ALL academic years and ALL GridView pages.

    Strategy:
      1. Read the year dropdown options.
      2. For each year: select it, set filter to 'הכל', scrape every GridView page.
      3. Merge into one dict keyed by  {course_code}|{moed}|{date}.
    """
    GRADES_TABLE = "#ContentPlaceHolder1_gvStudentAssignmentTermList"
    FILTER_DDL   = "#tbMain_ctl03_ddlExamDateRangeFilter"
    YEAR_DDL     = "#cmbActiveYear"

    async def _save_debug_html():
        try:
            html = await page.content()
            DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
            print(f"[DEBUG] HTML saved → {DEBUG_HTML_FILE.name}")
        except Exception:
            pass

    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        pass

    # Get all academic years available in the dropdown
    try:
        await page.wait_for_selector(YEAR_DDL, timeout=20_000)
        year_opts  = await page.query_selector_all(f"{YEAR_DDL} option")
        year_values = [await o.get_attribute("value") for o in year_opts]
    except PlaywrightTimeoutError:
        year_values = []
        await _save_debug_html()
        print(f"[EXTRACT] Page URL at timeout: {page.url}")

    if not year_values:
        print("[EXTRACT] Year dropdown not found — scraping current page only.")
        year_values = ["__current__"]

    all_grades: dict = {}
    col: dict | None = None

    for year in year_values:
        if year != "__current__":
            print(f"[EXTRACT] Selecting year {year}...")
            try:
                await page.select_option(YEAR_DDL, value=year)
                await page.wait_for_load_state("networkidle", timeout=12_000)
                await page.wait_for_timeout(800)
            except Exception as e:
                print(f"[EXTRACT] Year {year} select failed: {e}")
                continue

        # Set filter to "הכל" (all exams, including past with grades)
        try:
            await page.wait_for_selector(FILTER_DDL, timeout=5_000)
            await page.select_option(FILTER_DDL, value="1")
            await page.wait_for_load_state("networkidle", timeout=12_000)
            await page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            pass

        # Scrape every page of the GridView for this year
        grid_page = 0
        while True:
            grid_page += 1
            try:
                await page.wait_for_selector(GRADES_TABLE, timeout=10_000)
            except PlaywrightTimeoutError:
                print(f"[EXTRACT] Year {year}: table not found on page {grid_page}.")
                break

            rows = await page.query_selector_all(f"{GRADES_TABLE} tr")
            if len(rows) < 2:
                break

            # Detect columns once (layout is the same across all pages/years)
            if col is None:
                header_cells = await rows[0].query_selector_all("th, td")
                headers = [(await c.inner_text()).strip() for c in header_cells]
                col = _detect_columns(headers)
                print(f"[EXTRACT] Headers: {headers}")
                print(f"[EXTRACT] Column map: {col}")
                if col["course_name"] == -1:
                    print("[EXTRACT] Course column not found.")
                    await _save_debug_html()
                    return {}

            page_grades = await _parse_rows(rows, col)
            print(f"[EXTRACT] Year {year}, page {grid_page}: {len(page_grades)} row(s)")
            all_grades.update(page_grades)

            # GridView pager: look for a "next page" link (">")
            next_link = None
            pager_links = await page.query_selector_all(
                f"{GRADES_TABLE} tr.GridPager td a"
            )
            for link in pager_links:
                txt = (await link.inner_text()).strip()
                if txt in (">", ">>", "הבא"):
                    next_link = link
                    break

            if not next_link:
                break  # no more pages for this year

            await next_link.click()
            await page.wait_for_load_state("networkidle", timeout=12_000)
            await page.wait_for_timeout(500)

    return all_grades


# ─── Daemon helpers ───────────────────────────────────────────────────────────

def _pending_courses(grades: dict) -> dict:
    """Courses with no grade, no final grade, and no resolved appeal."""
    result = {}
    for key, info in grades.items():
        if not _is_empty(info.get("grade", "—")) or not _is_empty(info.get("final_grade", "—")):
            continue
        ap = info.get("appeal_status", "")
        if ap and ap != APPEAL_IN_PROGRESS and not _is_empty(ap):
            continue
        result[key] = info
    return result


def _in_critical_window(grades: dict) -> bool:
    """True if any pending course's exam was 6+ days ago (grade not yet posted)."""
    today = datetime.date.today()
    for info in _pending_courses(grades).values():
        parsed = _parse_date(info.get("date", ""))
        if not parsed:
            continue
        d, m, y = parsed
        try:
            days_since = (today - datetime.date(y, m, d)).days
        except ValueError:
            continue
        if days_since >= _CRIT_DAYS:
            return True
    return False


def _poll_secs(grades: dict) -> int:
    """Smart polling interval matching the userscript logic."""
    if not _pending_courses(grades):
        return _KEEPALIVE_SECS
    hour = datetime.datetime.now().hour
    if _DAY_START <= hour < _DAY_END and _in_critical_window(grades):
        return _FAST_SECS
    return _SLOW_SECS


async def _auto_login(page, username: str, password: str) -> bool:
    """
    Fill the ADFS/Shibboleth login form headlessly and wait for redirect back to Inbar.
    Credentials are read from .env — never sent anywhere except the university login page.
    """
    try:
        print(f"[AUTO-LOGIN] Login page: {page.url}")
        await page.wait_for_selector('input[type="password"]', timeout=12_000)

        # Username — try Microsoft ADFS selectors, then generic fallbacks
        for sel in ['#userNameInput', 'input[name="UserName"]', 'input[name="username"]',
                    'input[type="email"]', 'input[name="j_username"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(username)
                break

        # Password
        for sel in ['#passwordInput', 'input[name="Password"]', 'input[name="password"]',
                    'input[type="password"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(password)
                break

        # Submit
        for sel in ['#submitButton', 'input[type="submit"]', 'button[type="submit"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                break

        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)

        # "Stay signed in?" (Microsoft KMSI prompt) — always say Yes for longer session
        try:
            yes_btn = await page.query_selector('#idSIButton9')
            if yes_btn and await yes_btn.is_visible():
                await yes_btn.click()
                await page.wait_for_load_state('networkidle', timeout=10_000)
        except Exception:
            pass

        success = _is_on_grades_page(page.url)
        print(f"[AUTO-LOGIN] {'✓ success' if success else '✗ failed'} — at: {page.url}")
        return success

    except Exception as e:
        print(f"[AUTO-LOGIN] Error: {e}")
        return False


async def _otp_login_via_telegram(page, relay_url: str, token: str) -> bool:
    """
    When session expires: fill Inbar ID+phone (from .env), trigger SMS,
    then get the OTP code from the user via Telegram and complete login.
    Requires INBAR_ID and INBAR_PHONE in .env — without them returns False immediately.
    """
    # The Inbar form labels its first field 'edtUsername', but at BIU students
    # log in with their 9-digit ID. Prefer INBAR_ID; fall back to INBAR_USERNAME.
    inbar_id    = os.environ.get('INBAR_ID', '') or os.environ.get('INBAR_USERNAME', '')
    inbar_phone = os.environ.get('INBAR_PHONE', '')

    if not inbar_id or not inbar_phone:
        print("[OTP-LOGIN] INBAR_ID/INBAR_PHONE not in .env — cannot trigger SMS automatically")
        return False

    try:
        print(f"[OTP-LOGIN] Login page: {page.url}")

        # Step 1 form: Inbar's own Login.aspx (selectors confirmed from page dump
        # 2026-05-18 — edtUsername + edtMobile + btnLogin).
        for sel in ['#edtUsername', 'input[name="edtUsername"]',
                    '#id', 'input[name="id"]', 'input[name="teudat_zehut"]',
                    'input[placeholder*="ת.ז"]', 'input[placeholder*="מספר זהות"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(inbar_id)
                print(f"[OTP-LOGIN] Filled ID into {sel}")
                break

        for sel in ['#edtMobile', 'input[name="edtMobile"]',
                    '#phone', 'input[name="phone"]', 'input[name="cellphone"]',
                    'input[placeholder*="טלפון"]', 'input[placeholder*="נייד"]',
                    'input[type="tel"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(inbar_phone)
                print(f"[OTP-LOGIN] Filled phone into {sel}")
                break

        for sel in ['#btnLogin', 'input[name="btnLogin"]',
                    'button[type="submit"]', 'input[type="submit"]',
                    '#btnSend', '#submitButton']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                print(f"[OTP-LOGIN] Clicking submit: {sel}")
                await el.click()
                break
        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(2_000)

        # Step 2 page: OTP input. Selectors are guesses — if none match we dump
        # the page so we can see the real names.
        otp_selectors = [
            '#edtOTP', '#edtCode', '#edtSmsCode', '#edtPin',
            'input[name="edtOTP"]', 'input[name="edtCode"]', 'input[name="edtSmsCode"]',
            '#idTxtBx_SAOTCC_OTC',          # Microsoft ADFS fallback
            'input[name*="otp"]', 'input[name*="code"]', 'input[name*="OTC"]',
            'input[placeholder*="קוד"]',
            'input[maxlength="6"]', 'input[maxlength="8"]',
        ]
        otp_field = None
        for sel in otp_selectors:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                otp_field = el
                print(f"[OTP-LOGIN] Found OTP field: {sel}")
                break

        if not otp_field:
            print(f"[OTP-LOGIN] No OTP input found — dumping page so we can fix selectors")
            print(f"[OTP-LOGIN] Now at: {page.url}")
            try:
                from pathlib import Path
                Path("debug_otp_page.html").write_text(await page.content(), encoding="utf-8")
                inputs = await page.eval_on_selector_all(
                    "input",
                    "els => els.map(e => ({type:e.type, name:e.name, id:e.id, "
                    "maxlength:e.maxLength, placeholder:e.placeholder, "
                    "visible:e.offsetParent!==null}))"
                )
                print(f"[OTP-LOGIN] inputs on step-2 page:")
                for inp in inputs:
                    print(f"  {inp}")
            except Exception as e:
                print(f"[OTP-LOGIN] could not dump page: {e}")
            return False

        # Request code from user via Telegram
        _relay_post('/request-otp', {'token': token}, relay_url)
        print("[OTP-LOGIN] Waiting for OTP from Telegram (up to 5 min)...")

        # Poll relay every 4 seconds for up to 5 minutes
        otp_code = None
        for _ in range(75):
            await asyncio.sleep(4)
            try:
                r = requests.get(f"{relay_url}/poll-otp?token={token}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if data.get('code'):
                        otp_code = data['code']
                        break
            except requests.RequestException:
                pass

        if not otp_code:
            print("[OTP-LOGIN] Timeout waiting for OTP")
            return False

        await otp_field.fill(otp_code)
        for sel in ['#idSubmit_SAOTCC_Continue', 'button[type="submit"]',
                    'input[type="submit"]', '#btnVerify']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                break

        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)

        # KMSI ("Stay signed in?") prompt
        try:
            yes_btn = await page.query_selector('#idSIButton9')
            if yes_btn and await yes_btn.is_visible():
                await yes_btn.click()
                await page.wait_for_load_state('networkidle', timeout=10_000)
        except Exception:
            pass

        success = _is_on_grades_page(page.url)
        print(f"[OTP-LOGIN] {'✓ success' if success else '✗ failed'} — at: {page.url}")
        return success

    except Exception as e:
        print(f"[OTP-LOGIN] Error: {e}")
        return False


async def _portal_login(page, username: str, password: str) -> bool:
    """
    Click the 'פורטל בר-אילן שלי' link on Inbar's Login.aspx → routes through
    BIU's central SSO. The portal session is much longer-lived than Inbar's,
    so this skips the SMS step most of the time. If the portal session is
    also expired, the redirect lands on ADFS where _auto_login fills creds.
    """
    try:
        print(f"[PORTAL-LOGIN] Looking for portal link on: {page.url}")
        link = await page.query_selector('a:has-text("פורטל")')
        if not link or not await link.is_visible():
            print("[PORTAL-LOGIN] Portal link not visible on this page")
            return False

        await link.click()
        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)
        print(f"[PORTAL-LOGIN] After click, at: {page.url}")

        # Fast path: SSO cookies still valid → straight to grades.
        if _is_on_grades_page(page.url):
            print("[PORTAL-LOGIN] ✓ SSO cookies valid — already on grades page")
            return True

        # Portal asks for credentials → use the ADFS auto-login.
        if username and password:
            print("[PORTAL-LOGIN] Portal asking for credentials — running ADFS auto-login")
            if await _auto_login(page, username, password):
                # ADFS may not redirect us all the way to grades — nudge it.
                if not _is_on_grades_page(page.url):
                    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2_000)
                return _is_on_grades_page(page.url)
        print("[PORTAL-LOGIN] No credentials available — cannot continue")
        return False
    except Exception as e:
        print(f"[PORTAL-LOGIN] Error: {e}")
        return False


async def _handle_login(page, username: str, password: str,
                        relay_url: str, token: str) -> bool:
    """Try every available login method in order. Returns True if now on grades page."""
    # First choice: BIU portal SSO. No SMS needed if portal cookies are fresh.
    print("[DAEMON] Trying portal SSO route...")
    if await _portal_login(page, username, password):
        return True
    # Second choice: plain ADFS auto-login (in case we're already at the ADFS page)
    if username and password:
        print("[DAEMON] Trying direct ADFS auto-login...")
        if await _auto_login(page, username, password):
            return True
    # Last resort: SMS OTP via Telegram (annoying — user has to respond).
    if relay_url and token:
        print("[DAEMON] Trying SMS OTP via Telegram...")
        if await _otp_login_via_telegram(page, relay_url, token):
            return True
    return False




def _relay_post(path: str, body: dict, relay_url: str) -> bool:
    """POST to the Cloudflare Worker relay. Returns True on success."""
    try:
        r = requests.post(f"{relay_url}{path}", json=body, timeout=15)
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"[RELAY] {path} failed: {e}")
        return False


_KEEPALIVE_INTERVAL = 5 * 60   # ping Inbar every 5 min via requests (no browser)


def _keep_alive_ping() -> bool:
    """
    Lightweight HTTP GET to Inbar using saved cookies — no Playwright browser needed.
    Resets the ASP.NET session idle timer. Returns True if still logged in.
    Called every 5 min between Playwright scrape cycles.
    """
    if not STORAGE_FILE.exists():
        return False
    try:
        state = json.loads(STORAGE_FILE.read_text())
        s = requests.Session()
        for c in state.get("cookies", []):
            s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ""), path=c.get("path", "/"),
            )
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": TARGET_URL,
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })
        resp = s.get(TARGET_URL, timeout=15, allow_redirects=True)
        alive = _is_on_grades_page(resp.url)
        print(f"[KEEPALIVE] {'✓ alive' if alive else '✗ expired'} ({resp.url[:60]})")
        return alive
    except Exception as e:
        print(f"[KEEPALIVE] Error: {e}")
        return False


async def run_daemon() -> None:
    """
    Always-on daemon — run as macOS LaunchAgent.
    Keep-alive: lightweight requests ping every 5 min (no browser, survives Mac sleep).
    Scraping: Playwright launched per-check, closed after — no persistent browser needed.
    On session expiry: OTP via Telegram (Method 2) or manual re-login alert.
    Usage: python monitor.py --daemon
    """
    _username = os.environ.get('INBAR_USERNAME', '')
    _password = os.environ.get('INBAR_PASSWORD', '')
    _inbar_id = os.environ.get('INBAR_ID', '')
    _relay_url = os.environ.get('RELAY_URL', 'https://inbar-relay.alonco267.workers.dev')
    _token     = os.environ.get('DAEMON_TOKEN', '')

    if not STORAGE_FILE.exists():
        print("[DAEMON] No session — run: python monitor.py  to log in first.")
        return

    if _inbar_id:
        print(f"[DAEMON] OTP login enabled — ID: {_inbar_id[:4]}…")
    elif _username:
        print(f"[DAEMON] Credential login enabled for: {_username}")
    else:
        print("[DAEMON] No credentials in .env — OTP via Telegram if session expires")
    if _token:
        print(f"[DAEMON] Relay token: {_token[:8]}… → routing alerts via {_relay_url}")
    else:
        print("[DAEMON] No DAEMON_TOKEN — falling back to direct Telegram")

    def _notify(text: str) -> None:
        if _token:
            _relay_post('/alert', {'token': _token, 'text': text}, _relay_url)
        else:
            send_telegram(text)

    def _heartbeat() -> None:
        if not _token:
            return
        _relay_post('/heartbeat', {'token': _token}, _relay_url)
        summary = _build_final_grades_message()
        _relay_post('/grades-summary', {'token': _token, 'summary': summary}, _relay_url)

    print("[DAEMON] Inbar Grade Monitor — always-on mode started")

    _EXPIRED_FLAG = BASE_DIR / ".session_expired_notified"

    next_check = 0.0
    next_ping  = 0.0
    warned_expired = _EXPIRED_FLAG.exists()   # survive restarts — don't re-spam
    baseline_saved = False
    notified_running = False                  # send "monitoring" only after first success

    while True:
        try:
            now = _time.monotonic()

            # ── Keep-alive ping (every 5 min, no browser) ──────────────────────
            if now >= next_ping:
                alive = await asyncio.get_event_loop().run_in_executor(
                    None, _keep_alive_ping
                )
                next_ping = _time.monotonic() + _KEEPALIVE_INTERVAL
                if not alive and now < next_check:
                    print("[KEEPALIVE] Session lost — triggering immediate scrape")
                    next_check = 0.0          # pull forward the Playwright check
                elif alive and warned_expired:
                    print("[KEEPALIVE] Session recovered — triggering scrape")
                    next_check = 0.0          # fresh cookies appeared (user re-logged in)

            if _time.monotonic() < next_check:
                wake = min(next_check, next_ping) - _time.monotonic()
                await asyncio.sleep(max(1.0, min(30.0, wake)))
                continue

            # ── Full Playwright scrape ──────────────────────────────────────────
            grades_cache = load_saved()
            pending_n = len(_pending_courses(grades_cache))
            print(f"\n[DAEMON] Scraping ({pending_n} pending)...")

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    storage_state=str(STORAGE_FILE), **CONTEXT_OPTS
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await ctx.new_page()
                try:
                    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2_000)

                    if not _is_on_grades_page(page.url):
                        print(f"[DAEMON] Redirected to: {page.url}")
                        logged_in = await _handle_login(page, _username, _password,
                                                        _relay_url, _token)
                        if not logged_in:
                            if not warned_expired:
                                _notify(
                                    "ההתחברות לאינ-בר פגה 🔑\n"
                                    "פתח/י אינ-בר בדפדפן, התחבר/י — הבוט ימשיך אוטומטית.\n"
                                    "או הוסף/י INBAR_ID ו-INBAR_PHONE ל-.env לחיבור מחדש אוטומטי."
                                )
                                warned_expired = True
                                _EXPIRED_FLAG.touch()
                            next_check = _time.monotonic() + 600
                            print("[DAEMON] Session expired — retry in 10 min")
                            continue

                    if warned_expired:
                        warned_expired = False
                        _EXPIRED_FLAG.unlink(missing_ok=True)

                    fresh = await extract_grades(page)

                    if fresh is None:
                        print("[DAEMON] Grade extraction failed — retry in 5 min")
                        next_check = _time.monotonic() + 300
                        continue

                    if not baseline_saved:
                        print("[DAEMON] Startup baseline saved — monitoring from next check.")
                        baseline_saved = True
                        if not notified_running:
                            _notify("🟢 מנטר ציונים ברקע\nאעדכן אותך אוטומטית על כל שינוי בציונים.")
                            notified_running = True
                    else:
                        old = load_saved()
                        diff_and_alert(old, fresh, sender=_notify)

                    save_current(fresh)
                    await ctx.storage_state(path=str(STORAGE_FILE))
                    _heartbeat()

                    interval = _poll_secs(fresh)
                    next_check = _time.monotonic() + interval
                    next_ping  = _time.monotonic() + _KEEPALIVE_INTERVAL
                    label = "pending" if _pending_courses(fresh) else "all graded"
                    print(f"[DAEMON] Next check in {interval // 60} min ({label})")

                finally:
                    await ctx.close()
                    await browser.close()

        except Exception as e:
            print(f"[DAEMON] Unexpected error: {e}")
            await asyncio.sleep(60)


# ─── Bot command handlers ──────────────────────────────────────────────────────

def _moed_rank(moed: str) -> int:
    """Higher = more final. מועד ב' > מועד א'."""
    if "ב'" in moed:
        return 2
    if "א'" in moed:
        return 1
    return 0


def _parse_date(date_str: str) -> tuple[int, int, int] | None:
    """Return (day, month, year) from DD/MM/YYYY, or None on bad input."""
    try:
        parts = (date_str or "").split("/")
        if len(parts) != 3:
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, AttributeError):
        return None


def _academic_year(date_str: str) -> tuple[int, str]:
    """Bar-Ilan academic year (Oct→Sep). Returns (sort_key, label like '2025-2026')."""
    parsed = _parse_date(date_str)
    if not parsed:
        return (0, "—")
    _, month, year = parsed
    end_year = year if month <= 9 else year + 1
    return (end_year, f"{end_year - 1}-{end_year}")


def _semester(date_str: str) -> tuple[int, str]:
    """Derive semester from exam date. Returns (sort_key, Hebrew label)."""
    parsed = _parse_date(date_str)
    if not parsed:
        return (9, "?")
    _, month, _ = parsed
    # Winter exams (סמסטר א'): Jan–Apr, Oct–Dec.
    # Spring/summer exams (סמסטר ב'): May–Sep.
    if month in (1, 2, 3, 4, 10, 11, 12):
        return (1, "א'")
    return (2, "ב'")


def _build_final_grades_message() -> str:
    """
    One line per unique course name — latest, most final grade wins.
    Priority: has ציון סופי > later מועד (ב'>א') > later date.
    """
    data = load_saved()
    if not data:
        return "אין נתונים שמורים עדיין. הרץ את הסקריפט לפחות פעם אחת."

    by_course: dict = {}
    for info in data.values():
        name = info.get("course_name", "?")
        fg   = info.get("final_grade", "—")
        moed = info.get("moed", "")
        date = info.get("date", "")

        if name not in by_course:
            by_course[name] = info
            continue

        ex      = by_course[name]
        ex_fg   = ex.get("final_grade", "—")
        ex_moed = ex.get("moed", "")
        ex_date = ex.get("date", "")

        has_fg, ex_has_fg = not _is_empty(fg), not _is_empty(ex_fg)

        # 1. Prefer entry with a final grade
        if has_fg and not ex_has_fg:
            by_course[name] = info; continue
        if not has_fg and ex_has_fg:
            continue

        # 2. Prefer later מועד
        if _moed_rank(moed) > _moed_rank(ex_moed):
            by_course[name] = info; continue
        if _moed_rank(moed) < _moed_rank(ex_moed):
            continue

        # 3. Prefer later date (DD/MM/YYYY → compare as YYYY/MM/DD)
        def _rev(d: str) -> str:
            p = d.split("/")
            return "/".join(reversed(p)) if len(p) == 3 else d

        if _rev(date) > _rev(ex_date):
            by_course[name] = info

    # Group by (academic year, semester); sort newest year first, סמסטר א' before ב'.
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for name in sorted(by_course):
        info = by_course[name]
        date = info.get("date", "")
        year_key, year_label = _academic_year(date)
        sem_key,  sem_label  = _semester(date)
        groups[(year_key, sem_key, year_label, sem_label)].append((name, info))

    lines = ["📊 ציונים סופיים:"]
    has_any = False
    for key in sorted(groups.keys(), key=lambda k: (-k[0], k[1])):
        _, _, year_label, sem_label = key
        lines.append(f"\n📅 {year_label}  •  סמסטר {sem_label}")
        for name, info in groups[key]:
            fg = info.get("final_grade", "—")
            if not _is_empty(fg):
                lines.append(f"• {name}: {fg}")
                has_any = True
            else:
                lines.append(f"• {name}: טרם פורסם")

    if not has_any:
        lines.append("\nטרם פורסמו ציונים סופיים.")

    return "\n".join(lines)


async def _check_inbar_connection() -> tuple[bool, str]:
    """
    Verify the Inbar session by actually loading the grades table.
    Returns (is_connected, detail_message).
    """
    if not STORAGE_FILE.exists():
        return False, "אין session שמור. הרץ `python monitor.py` כדי להתחבר."

    async with async_playwright() as pw:
        try:
            browser, ctx, page = await _open_headless(pw)
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1_500)

            if not _is_on_grades_page(page.url):
                await ctx.close()
                await browser.close()
                return False, "מנותב לדף התחברות — session פג."

            # Verify the actual grades table loads (not just the shell page)
            GRADES_TABLE = "#ContentPlaceHolder1_gvStudentAssignmentTermList"
            FILTER_DDL   = "#tbMain_ctl03_ddlExamDateRangeFilter"
            try:
                await page.wait_for_selector(FILTER_DDL, timeout=8_000)
                await page.select_option(FILTER_DDL, value="1")
                await page.wait_for_load_state("networkidle", timeout=10_000)
                await page.wait_for_selector(GRADES_TABLE, timeout=8_000)
                table_found = True
            except PlaywrightTimeoutError:
                table_found = False

            await ctx.close()
            await browser.close()

            if table_found:
                return True, "ok"
            return False, "הדף נטען אך טבלת הציונים לא הופיעה."

        except Exception as e:
            return False, str(e)


# ─── Bot message handler (extracted for testability) ─────────────────────────

async def _handle_update(update: dict) -> int:
    """
    Process one Telegram update. Returns next offset.
    Responds to the sender's chat_id — supports multiple concurrent users.
    """
    msg       = update.get("message", {})
    text      = msg.get("text", "").strip()
    update_id = update.get("update_id", 0)

    if not text:
        return update_id + 1

    sender_id = str(msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
    print(f"[BOT] Received from {sender_id}: {text!r}")

    register_user(sender_id)

    if BTN_GRADES in text or "ציונים" in text:
        reply = _build_final_grades_message()
        _send_with_keyboard(reply, chat_id=sender_id)

    elif BTN_CONNECTION in text or "מחובר" in text:
        _send_with_keyboard("בודק חיבור לאינ-בר... ⏳", chat_id=sender_id)
        connected, detail = await _check_inbar_connection()
        if connected:
            has_cache = DATA_FILE.exists() and DATA_FILE.stat().st_size > 10
            cache_line = (
                "✅ יש ציונים שמורים — אעדכן אותך על שינויים."
                if has_cache else
                "⚠️ אין ציונים שמורים עדיין — הרץ `python monitor.py` פעם אחת."
            )
            _send_with_keyboard(f"כן, מחובר לאינ-בר! 🟢\n{cache_line}", chat_id=sender_id)
        else:
            _send_with_keyboard(
                f"לא מחובר ❌\n"
                f"הרץ `python monitor.py` במחשב כדי להתחבר מחדש.\n"
                f"({detail})",
                chat_id=sender_id,
            )

    else:
        _send_with_keyboard("בחר פעולה מהתפריט 👇", chat_id=sender_id)

    return update_id + 1


# ─── Bot daemon ───────────────────────────────────────────────────────────────

async def run_bot() -> None:
    """
    Interactive Telegram bot daemon.
    Run with:  python monitor.py --bot

    Responds to two keyboard buttons:
      📊 רשימת הציונים הסופיים שלי  →  final-grade list from data.json
      🔌 האם אני מחובר?              →  live Inbar connection check
    """
    print("[BOT] Starting Telegram bot daemon...")

    # Delete any webhook — a set webhook silently blocks getUpdates
    wh = _tg_post("deleteWebhook", {"drop_pending_updates": True})
    print(f"[BOT] Webhook cleared: {wh.get('description', wh)}")

    startup_msg = (
        "בוט ציונים פעיל! 🟢\n"
        "אשלח לך הודעה כשיעלה ציון חדש.\n"
        "בחר פעולה מהתפריט למטה:"
    )
    for uid in load_users():
        _send_with_keyboard(startup_msg, chat_id=uid)
    print("[BOT] Keyboard sent. Listening for commands...")

    offset = 0
    while True:
        updates = await asyncio.to_thread(_fetch_updates, offset)
        for update in updates:
            offset = await _handle_update(update)


# ─── One-shot monitor (cron mode) ─────────────────────────────────────────────

async def main() -> None:
    print("━" * 62)
    print("  Inbar Grade Monitor — Bar-Ilan University")
    print("━" * 62 + "\n")

    async with async_playwright() as pw:
        browser, ctx, page = await get_authenticated_context(pw)
        try:
            # get_authenticated_context already navigated to TARGET_URL — no second goto needed
            print("[EXTRACT] Parsing grades table...")
            grades = await extract_grades(page)

            if not grades:
                print("[ERROR] No grades extracted.")
                print(f"        Check {DEBUG_HTML_FILE.name} if it was created.")
                sys.exit(1)

            print(f"\n[RESULT] {len(grades)} row(s):")
            for info in grades.values():
                print(
                    f"  • {info['course_name']} | {info['moed']} "
                    f"| ציון: {info['grade']} "
                    f"| ציון סופי: {info['final_grade']} "
                    f"| ערעור: {info['appeal_status']}"
                )

            print()
            old_grades = load_saved()
            # Manual mode: never send alerts — just print changes silently.
            # Alerts are daemon's job. This mode is for login/verification only.
            diff_and_alert(old_grades, grades, sender=lambda _: None)
            save_current(grades)
            print("[DIFF] No relevant changes detected." if old_grades else "[DIFF] First run — baseline saved.")

        finally:
            await ctx.close()
            await browser.close()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--daemon" in sys.argv:
        asyncio.run(run_daemon())
    elif "--bot" in sys.argv:
        asyncio.run(run_bot())
    else:
        asyncio.run(main())
