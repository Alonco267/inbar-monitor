#!/usr/bin/env python3
"""
Inbar Grade Monitor — Bar-Ilan University (local modes + compatibility facade)

The production monitoring path is GitHub Actions → monitor_once.py → Cloudflare
Worker relay. This module keeps the local modes working and re-exports the
shared logic from the ``inbar`` package so existing imports keep functioning.

Modes:
  python monitor.py          → one-shot check + manual login bootstrap
  python monitor.py --daemon → always-on local daemon (legacy path, launchd)
  python monitor.py --bot    → interactive Telegram bot daemon (legacy path)
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

from inbar import diff as _diff
from inbar import formatting as _formatting
from inbar import login as _login
from inbar import scrape as _scrape
from inbar.config import CONTEXT_OPTS, TARGET_URL, setup_logging
from inbar.relay import RelayClient
from inbar.textutils import (APPEAL_IN_PROGRESS, LOGIN_KEYWORDS,
                             REJECTION_KEYWORDS, academic_year, appeal_approved,
                             is_empty, is_login_page, is_on_grades_page,
                             moed_rank, parse_date, semester)

# ─── Load credentials from .env (never hardcode secrets) ─────────────────────
load_dotenv(Path(__file__).parent / ".env")
setup_logging()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# --daemon routes through relay; --bot requires tokens; one-shot works without
if "--bot" in sys.argv and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
    print("[ERROR] --bot mode requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    sys.exit(1)

# ─── Paths & constants ────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
STORAGE_FILE    = BASE_DIR / "session_cookies.json"
USER_DATA_DIR   = BASE_DIR / "inbar_profile"          # persistent Chromium profile
DATA_FILE       = BASE_DIR / "data.json"
EVER_ALERTED_FILE = BASE_DIR / ".ever_alerted.json"   # persistent dedup set
USERS_FILE      = BASE_DIR / "users.json"
DEBUG_HTML_FILE = _scrape.DEBUG_HTML_FILE

# ─── Daemon smart-polling constants ───────────────────────────────────────────
_FAST_SECS      = 10 * 60
_SLOW_SECS      = 30 * 60
_KEEPALIVE_SECS = 12 * 60        # max gap even when nothing pending
_DAY_START      = 8
_DAY_END        = 23
_CRIT_DAYS      = 6   # critical window starts 6 days after exam

# ─── Bot keyboard button labels ───────────────────────────────────────────────
BTN_GRADES     = "📊 רשימת הציונים הסופיים שלי"
BTN_CONNECTION = "🔌 האם אני מחובר?"
BTN_GPA        = "🎓 הממוצע שלי"


# ─── Compatibility re-exports (tests + monitor_once import these) ─────────────

_is_login_page      = is_login_page
_is_on_grades_page  = is_on_grades_page
_is_empty           = is_empty
_appeal_approved    = appeal_approved
_moed_rank          = moed_rank
_parse_date         = parse_date
_academic_year      = academic_year
_semester           = semester
_detect_columns     = _scrape.detect_columns
extract_grades      = _scrape.extract_grades


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
            [{"text": BTN_GPA}],
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
    except requests.RequestException as e:
        # A fast failure (e.g. DNS down) would otherwise busy-loop the caller.
        print(f"[BOT] getUpdates failed ({e.__class__.__name__}) — retrying in 5 s")
        _time.sleep(5)
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


def load_ever_alerted() -> dict:
    """Persistent set of dedup ids already delivered — survives reboots.

    Maps ``dedup_id → 1``. Guarantees exactly-once alerting even when the
    previous grade snapshot is lost or comes back partial after a reconnect.
    """
    if EVER_ALERTED_FILE.exists():
        try:
            return json.loads(EVER_ALERTED_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[DEDUP] ever-alerted file unreadable — starting fresh")
    return {}


def save_ever_alerted(ids: dict) -> None:
    EVER_ALERTED_FILE.write_text(
        json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    _lock(EVER_ALERTED_FILE)


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
    """Diff two snapshots and send one message per detected change.

    Thin wrapper around inbar.diff.compute_alerts. ``sender`` defaults to
    broadcast_alert (resolved at call time so tests can patch it).
    """
    _send = sender if sender is not None else broadcast_alert
    alerts = _diff.compute_alerts(old, new)
    for alert in alerts:
        _send(alert.message)
        print(f"[ALERT] {alert.kind} — {alert.key}")
    if not alerts:
        print("[DIFF] No relevant changes detected.")
    else:
        print(f"[DIFF] {len(alerts)} alert(s) sent.")


# ─── Local OTP provider (Telegram chat, no cloud relay needed) ────────────────

_BOT_OFFSET = {"value": 0}   # shared getUpdates offset (bot loop + OTP waiter)
_OTP_WAIT_SECS = 300


def _poll_chat_for_code(timeout_secs: int = _OTP_WAIT_SECS) -> str | None:
    """Poll Telegram for a 4-8 digit message (the SMS code typed by the user)."""
    import re
    deadline = _time.monotonic() + timeout_secs
    while _time.monotonic() < deadline:
        for u in _fetch_updates(_BOT_OFFSET["value"]):
            _BOT_OFFSET["value"] = u.get("update_id", 0) + 1
            text = (u.get("message", {}).get("text") or "").strip()
            if re.fullmatch(r"\d{4,8}", text):
                print("[LOGIN] OTP code received via Telegram")
                return text
    print("[LOGIN] Timed out waiting for OTP code in Telegram chat")
    return None


async def _telegram_otp_provider() -> str | None:
    """Ask the owner for the SMS code in the Telegram chat and wait for it."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    send_telegram(
        "🔐 נדרש קוד אימות כדי להתחבר לאינ-בר.\n"
        "נשלח אליך SMS — שלח/י לי כאן את הקוד (ספרות בלבד)."
    )
    return await asyncio.to_thread(_poll_chat_for_code)


# ─── Daemon↔bot OTP handoff (the bot owns getUpdates; the daemon can't poll) ──
#
# The monitor daemon prompts the owner in Telegram, then waits for the bot
# process (the only getUpdates consumer) to drop the digits it receives into
# a reply file.

_OTP_REQUEST_FILE = BASE_DIR / ".otp_request"
_OTP_REPLY_FILE   = BASE_DIR / ".otp_reply"
_OTP_REQUEST_MAX_AGE = _OTP_WAIT_SECS   # a request older than this is stale


def _otp_request_pending() -> bool:
    """True if the daemon is currently waiting for an SMS code."""
    try:
        age = _time.time() - float(_OTP_REQUEST_FILE.read_text().strip())
        return 0 <= age <= _OTP_REQUEST_MAX_AGE
    except (OSError, ValueError):
        return False


async def _daemon_chat_otp_provider() -> str | None:
    """Daemon-side OTP: prompt the owner in Telegram, wait for the bot process
    to relay the code via the reply file (up to 5 min)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    _OTP_REPLY_FILE.unlink(missing_ok=True)
    _OTP_REQUEST_FILE.write_text(str(_time.time()))
    send_telegram(
        "🔐 נדרש קוד אימות כדי להתחבר לאינ-בר.\n"
        "נשלח אליך SMS לטלפון — שלח/י לי כאן את הקוד (ספרות בלבד)."
    )
    print("[LOGIN] OTP prompt sent — waiting for code via bot (up to 5 min)")
    deadline = _time.monotonic() + _OTP_WAIT_SECS
    try:
        while _time.monotonic() < deadline:
            await asyncio.sleep(2)
            if _OTP_REPLY_FILE.exists():
                code = _OTP_REPLY_FILE.read_text().strip()
                _OTP_REPLY_FILE.unlink(missing_ok=True)
                if code:
                    print("[LOGIN] OTP code received from bot")
                    return code
        print("[LOGIN] Timed out waiting for OTP code")
        return None
    finally:
        _OTP_REQUEST_FILE.unlink(missing_ok=True)


async def _attempt_relogin(page, ctx) -> bool:
    """Automatic re-login with .env credentials; refreshes saved cookies."""
    username = os.environ.get("INBAR_USERNAME", "")
    password = os.environ.get("INBAR_PASSWORD", "")
    print(f"[LOGIN] Session expired (at {page.url[:80]}) — attempting automatic re-login...")
    ok = await _login.handle_login(page, username, password, None,
                                   otp_provider=_telegram_otp_provider)
    if ok:
        await ctx.storage_state(path=str(STORAGE_FILE))
        _lock(STORAGE_FILE)
        print("[LOGIN] Re-login succeeded — refreshed session saved.")
    else:
        print("[LOGIN] Automatic re-login failed — see stages above for the failing step.")
    return ok


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
        if is_on_grades_page(url):
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
            if is_on_grades_page(page.url):
                print("[BROWSER] Session valid — running headless.")
                return browser, ctx, page
            # Session expired — try automatic re-login before bothering the user
            if await _attempt_relogin(page, ctx):
                return browser, ctx, page
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


# ─── Daemon helpers ───────────────────────────────────────────────────────────

def _pending_courses(grades: dict) -> dict:
    """Courses with no grade, no final grade, and no resolved appeal."""
    result = {}
    for key, info in grades.items():
        if not is_empty(info.get("grade", "—")) or not is_empty(info.get("final_grade", "—")):
            continue
        ap = info.get("appeal_status", "")
        if ap and ap != APPEAL_IN_PROGRESS and not is_empty(ap):
            continue
        result[key] = info
    return result


def _in_critical_window(grades: dict) -> bool:
    """True if any pending course's exam was 6+ days ago (grade not yet posted)."""
    today = datetime.date.today()
    for info in _pending_courses(grades).values():
        parsed = parse_date(info.get("date", ""))
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


async def _handle_login(page, username: str, password: str,
                        relay_url: str, token: str) -> bool:
    """Legacy signature preserved for monitor_once/daemon callers."""
    relay = RelayClient(relay_url, token) if (relay_url and token) else None
    return await _login.handle_login(page, username, password, relay)


_KEEPALIVE_INTERVAL = 5 * 60   # ping Inbar every 5 min via requests (no browser)


def _keep_alive_ping() -> bool:
    """Lightweight HTTP GET to Inbar using saved cookies — no browser needed."""
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
            "User-Agent": CONTEXT_OPTS["user_agent"],
            "Referer": TARGET_URL,
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })
        resp = s.get(TARGET_URL, timeout=15, allow_redirects=True)
        alive = is_on_grades_page(resp.url)
        print(f"[KEEPALIVE] {'✓ alive' if alive else '✗ expired'} ({resp.url[:60]})")
        return alive
    except Exception as e:
        print(f"[KEEPALIVE] Error: {e}")
        return False


async def run_daemon() -> None:
    """Always-on local daemon (legacy path — production runs via GitHub Actions).

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

    relay = RelayClient(_relay_url, _token) if _token else None
    if relay:
        print(f"[DAEMON] Relay token: {_token[:8]}… → routing alerts via {_relay_url}")
    else:
        print("[DAEMON] No DAEMON_TOKEN — falling back to direct Telegram")

    def _notify(text: str) -> None:
        if relay:
            relay.send_alert(text)
        else:
            send_telegram(text)

    def _heartbeat() -> None:
        if not relay:
            return
        relay.heartbeat()
        relay.push_grades_summary(_build_final_grades_message())

    print("[DAEMON] Inbar Grade Monitor — always-on mode started")

    _EXPIRED_FLAG = BASE_DIR / ".session_expired_notified"
    _MAX_LOGIN_FAILURES = 3   # notify only after 3 consecutive failed re-logins

    next_check = 0.0
    next_ping  = 0.0
    login_failures = 0
    warned_expired = _EXPIRED_FLAG.exists()   # survive restarts — don't re-spam
    ever_alerted = load_ever_alerted()        # persistent dedup — survives reboot
    # "monitoring" greeting and baseline happen only on the genuine first run;
    # once we have a dedup set on disk, a restart is silent (no re-flood).
    notified_running = bool(ever_alerted)

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
                    next_check = 0.0
                elif alive and warned_expired:
                    print("[KEEPALIVE] Session recovered — triggering scrape")
                    next_check = 0.0

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

                    if not is_on_grades_page(page.url):
                        print(f"[DAEMON] Redirected to: {page.url}")
                        # OTP via Telegram chat (bot relays the code); the
                        # Cloudflare relay remains the fallback provider.
                        _otp = (_daemon_chat_otp_provider
                                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else None)
                        logged_in = await _login.handle_login(
                            page, _username, _password, relay, otp_provider=_otp)
                        if not logged_in:
                            login_failures += 1
                            if login_failures >= _MAX_LOGIN_FAILURES and not warned_expired:
                                _notify(
                                    "ההתחברות לאינ-בר פגה 🔑\n"
                                    "פתח/י אינ-בר בדפדפן, התחבר/י — הבוט ימשיך אוטומטית.\n"
                                    "או הוסף/י INBAR_ID ו-INBAR_PHONE ל-.env לחיבור מחדש אוטומטי."
                                )
                                warned_expired = True
                                _EXPIRED_FLAG.touch()
                            next_check = _time.monotonic() + 600
                            print(f"[DAEMON] Re-login failed "
                                  f"({login_failures}/{_MAX_LOGIN_FAILURES} before alert) "
                                  f"— retry in 10 min")
                            continue

                    login_failures = 0
                    if warned_expired:
                        warned_expired = False
                        _EXPIRED_FLAG.unlink(missing_ok=True)

                    fresh = await extract_grades(page)

                    if not fresh:
                        print("[DAEMON] Grade extraction failed — retry in 5 min")
                        next_check = _time.monotonic() + 300
                        continue

                    old = load_saved()
                    # Genuine first run only: seed the dedup set from what's
                    # already on the page so existing grades never alert, even
                    # if data.json is later lost. Mirrors monitor_once.py.
                    if not old and not ever_alerted:
                        print("[DAEMON] First run — seeding dedup baseline (silent).")
                        ever_alerted = _diff.baseline_dedup_ids(fresh)
                        save_ever_alerted(ever_alerted)
                        if not notified_running:
                            _notify("🟢 מנטר ציונים ברקע\nאעדכן אותך אוטומטית על כל שינוי בציונים.")
                            notified_running = True
                    else:
                        alerts = _diff.compute_alerts(old, fresh)
                        to_send, ever_alerted = _diff.filter_alerts(alerts, ever_alerted)
                        for alert in to_send:
                            _notify(alert.message)
                            print(f"[ALERT] {alert.kind} — {alert.key}")
                        # Keep the dedup set a superset of everything currently
                        # visible, so a later data.json loss / partial scrape can
                        # never make already-seen grades look new and re-flood.
                        ever_alerted = {**_diff.baseline_dedup_ids(fresh),
                                        **ever_alerted}
                        save_ever_alerted(ever_alerted)
                        print(f"[DIFF] {len(to_send)} new alert(s) sent."
                              if to_send else
                              "[DIFF] No new changes (already-seen items suppressed).")

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

def _build_final_grades_message() -> str:
    """Final-grades summary from the locally saved snapshot."""
    return _formatting.build_final_grades_message(load_saved())


async def _check_inbar_connection() -> tuple[bool, str]:
    """Verify the Inbar session by actually loading the grades table."""
    if not STORAGE_FILE.exists():
        return False, "אין session שמור. הרץ `python monitor.py` כדי להתחבר."

    async with async_playwright() as pw:
        try:
            browser, ctx, page = await _open_headless(pw)
        except Exception as e:
            print(f"[BOT] Connection check: browser launch failed: {e}")
            return False, f"browser launch failed: {e}"
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1_500)

            if not is_on_grades_page(page.url) and not await _attempt_relogin(page, ctx):
                return False, "session פג וההתחברות האוטומטית נכשלה."

            from inbar.config import FILTER_DDL_SEL, GRADES_TABLE_SEL
            try:
                await page.wait_for_selector(FILTER_DDL_SEL, timeout=8_000)
                await page.select_option(FILTER_DDL_SEL, value="1")
                await page.wait_for_load_state("networkidle", timeout=10_000)
                await page.wait_for_selector(GRADES_TABLE_SEL, timeout=8_000)
            except PlaywrightTimeoutError:
                return False, "הדף נטען אך טבלת הציונים לא הופיעה."
            return True, "ok"
        except Exception as e:
            return False, str(e)
        finally:
            await ctx.close()
            await browser.close()


async def _fetch_gpa_live() -> str | None:
    """Live-scrape the official averages ("הממוצע שלי") using the saved session.

    Mirrors the production path (monitor_once.py → inbar.scrape.extract_gpa) so
    the local bot shows the exact same GPA text. Returns None on any failure —
    a GPA lookup must never crash the bot.
    """
    if not STORAGE_FILE.exists():
        return None

    async with async_playwright() as pw:
        try:
            browser, ctx, page = await _open_headless(pw)
        except Exception as e:
            print(f"[BOT] GPA fetch: browser launch failed: {e}")
            return None
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1_500)
            if not is_on_grades_page(page.url) and not await _attempt_relogin(page, ctx):
                print("[BOT] GPA aborted — no valid session and re-login failed")
                return None
            print("[BOT] Session OK — opening averages page...")
            return await _scrape.extract_gpa(page)
        except Exception as e:
            print(f"[BOT] GPA fetch error: {e}")
            return None
        finally:
            await ctx.close()
            await browser.close()


async def _handle_update(update: dict) -> int:
    """Process one Telegram update. Returns next offset."""
    msg       = update.get("message", {})
    text      = msg.get("text", "").strip()
    update_id = update.get("update_id", 0)

    if not text:
        return update_id + 1

    sender_id = str(msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
    print(f"[BOT] Received from {sender_id}: {text!r}")

    register_user(sender_id)

    # OTP handoff: a digits-only message from the owner is an SMS code.
    # Local daemon first (reply file); otherwise the cloud runner may be
    # waiting — forward to the relay, which reports whether it was pending.
    import re
    if re.fullmatch(r"\d{4,8}", text) and sender_id == TELEGRAM_CHAT_ID:
        if _otp_request_pending():
            _OTP_REPLY_FILE.write_text(text)
            print("[BOT] OTP code relayed to monitor daemon")
            _send_with_keyboard("קיבלתי את הקוד ✅ מתחבר לאינ-בר...", chat_id=sender_id)
            return update_id + 1
        if _forward_otp_to_relay(text):
            print("[BOT] OTP code forwarded to cloud runner via relay")
            _send_with_keyboard("קיבלתי את הקוד ✅ מתחבר לאינ-בר...", chat_id=sender_id)
            return update_id + 1

    if BTN_GPA in text or "ממוצע" in text or text.lower().startswith("/gpa"):
        print("[BOT] Handler: GPA — live-scraping averages page")
        _send_with_keyboard("מחשב את הממוצע שלך... ⏳", chat_id=sender_id)
        gpa = await _fetch_gpa_live()
        print(f"[BOT] GPA result: {'ok' if gpa else 'FAILED'}")
        if gpa:
            _send_with_keyboard(gpa, chat_id=sender_id)
        else:
            _send_with_keyboard(
                "לא הצלחתי לקרוא את הממוצע כרגע.\n"
                "ודא/י שה-session מחובר (הרץ `python monitor.py`) ונסה/י שוב.",
                chat_id=sender_id,
            )

    elif BTN_GRADES in text or "ציונים" in text:
        print("[BOT] Handler: grades list — formatting saved snapshot")
        reply = _build_final_grades_message()
        _send_with_keyboard(reply, chat_id=sender_id)
        print("[BOT] Grades list sent")

    elif BTN_CONNECTION in text or "מחובר" in text:
        print("[BOT] Handler: connection check — verifying Inbar session")
        _send_with_keyboard("בודק חיבור לאינ-בר... ⏳", chat_id=sender_id)
        connected, detail = await _check_inbar_connection()
        print(f"[BOT] Connection check → {'connected' if connected else f'NOT connected ({detail})'}")
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

def _forward_otp_to_relay(code: str) -> bool:
    """Hand an SMS code to the Cloudflare relay for a waiting cloud runner.

    Returns True only when the relay confirms an OTP request was pending —
    otherwise the caller treats the digits as a normal message.
    """
    relay_url = os.environ.get("RELAY_URL", "").strip().rstrip("/")
    token = os.environ.get("DAEMON_TOKEN", "").strip()
    if not relay_url or not token:
        return False
    try:
        r = requests.post(f"{relay_url}/otp-reply",
                          json={"token": token, "code": code}, timeout=10)
        return r.status_code == 200 and bool(r.json().get("pending"))
    except (requests.RequestException, ValueError) as e:
        print(f"[BOT] OTP forward to relay failed: {e}")
        return False


def _rearm_worker_webhook() -> None:
    """Point Telegram back at the Cloudflare worker webhook.

    Called when the bot shuts down (logout/shutdown): while this Mac is off,
    the worker must receive messages directly or SMS codes go nowhere.
    """
    relay_url = os.environ.get("RELAY_URL", "").strip().rstrip("/")
    if not relay_url:
        return
    try:
        r = requests.post(f"{relay_url}/setup-webhook",
                          json={"bot_token": TELEGRAM_BOT_TOKEN}, timeout=10)
        print(f"[BOT] Worker webhook re-armed on shutdown (HTTP {r.status_code})")
    except requests.RequestException as e:
        print(f"[BOT] Webhook re-arm failed: {e}")


async def run_bot() -> None:
    """Interactive Telegram bot daemon. Run with: python monitor.py --bot"""
    import signal

    print("[BOT] Starting Telegram bot daemon...")

    # On logout/shutdown launchd sends SIGTERM — hand Telegram back to the
    # cloud worker's webhook before dying so OTP replies still work PC-off.
    def _on_sigterm(_sig, _frm):
        _rearm_worker_webhook()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    # Take over from the webhook — a set webhook silently blocks getUpdates.
    # Never drop pending updates: they may hold an SMS code sent moments ago.
    try:
        wh = _tg_post("deleteWebhook", {"drop_pending_updates": False})
        print(f"[BOT] Webhook cleared: {wh.get('description', wh)}")
    except requests.RequestException as e:
        print(f"[BOT] Webhook clear failed ({e}) — continuing; getUpdates may retry")

    startup_msg = (
        "בוט ציונים פעיל! 🟢\n"
        "אשלח לך הודעה כשיעלה ציון חדש.\n"
        "בחר פעולה מהתפריט למטה:"
    )
    for uid in load_users():
        _send_with_keyboard(startup_msg, chat_id=uid)
    print("[BOT] Keyboard sent. Listening for commands...")

    while True:
        updates = await asyncio.to_thread(_fetch_updates, _BOT_OFFSET["value"])
        for update in updates:
            # Never let one bad update kill the daemon — skip it and move on.
            try:
                _BOT_OFFSET["value"] = await _handle_update(update)
            except Exception as e:
                _BOT_OFFSET["value"] = update.get("update_id", 0) + 1
                print(f"[BOT] Handler error for update "
                      f"{update.get('update_id')}: {e!r} — update skipped")


# ─── One-shot monitor (manual login/verification mode) ────────────────────────

async def main() -> None:
    print("━" * 62)
    print("  Inbar Grade Monitor — Bar-Ilan University")
    print("━" * 62 + "\n")

    async with async_playwright() as pw:
        browser, ctx, page = await get_authenticated_context(pw)
        try:
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
            # Alerts are the production runner's job.
            diff_and_alert(old_grades, grades, sender=lambda _: None)
            save_current(grades)

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
