"""Login flows: BIU portal SSO → ADFS credentials → SMS OTP via Telegram.

Order matters: the portal route usually rides on long-lived SSO cookies and
avoids the SMS step entirely; direct ADFS covers the case where we already
landed on the ADFS form; OTP-via-Telegram is the last resort because it
requires the user to respond.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .config import BASE_DIR, TARGET_URL
from .relay import RelayClient
from .textutils import is_on_grades_page

log = logging.getLogger("inbar.login")

OTP_DEBUG_FILE = BASE_DIR / "debug_otp_page.html"


class LoginFailed(Exception):
    """All available login methods failed — session cannot be established."""


async def auto_login(page, username: str, password: str) -> bool:
    """Fill the ADFS/Shibboleth login form headlessly and wait for the redirect."""
    try:
        log.info("ADFS login page: %s", page.url)
        await page.wait_for_selector('input[type="password"]', timeout=12_000)

        for sel in ['#userNameInput', 'input[name="UserName"]', 'input[name="username"]',
                    'input[type="email"]', 'input[name="j_username"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(username)
                break

        for sel in ['#passwordInput', 'input[name="Password"]', 'input[name="password"]',
                    'input[type="password"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(password)
                break

        for sel in ['#submitButton', 'input[type="submit"]', 'button[type="submit"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                break

        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)
        await _accept_kmsi(page)

        success = is_on_grades_page(page.url)
        log.info("ADFS login %s — at: %s", "succeeded" if success else "failed", page.url)
        return success
    except Exception as e:
        log.warning("ADFS login error: %s", e)
        return False


async def _accept_kmsi(page) -> None:
    """Microsoft 'Stay signed in?' prompt — always Yes for a longer session."""
    try:
        yes_btn = await page.query_selector('#idSIButton9')
        if yes_btn and await yes_btn.is_visible():
            await yes_btn.click()
            await page.wait_for_load_state('networkidle', timeout=10_000)
    except Exception:
        pass


async def otp_login_via_telegram(page, relay: RelayClient) -> bool:
    """Fill Inbar ID+phone (from env), trigger SMS, get the code via Telegram."""
    inbar_id = os.environ.get('INBAR_ID', '') or os.environ.get('INBAR_USERNAME', '')
    inbar_phone = os.environ.get('INBAR_PHONE', '')

    if not inbar_id or not inbar_phone:
        log.info("INBAR_ID/INBAR_PHONE not set — cannot trigger SMS automatically")
        return False

    try:
        log.info("OTP login page: %s", page.url)

        # Step 1 form: Inbar's own Login.aspx (selectors confirmed from page
        # dump 2026-05-18 — edtUsername + edtMobile + btnLogin).
        for sel in ['#edtUsername', 'input[name="edtUsername"]',
                    '#id', 'input[name="id"]', 'input[name="teudat_zehut"]',
                    'input[placeholder*="ת.ז"]', 'input[placeholder*="מספר זהות"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(inbar_id)
                break

        for sel in ['#edtMobile', 'input[name="edtMobile"]',
                    '#phone', 'input[name="phone"]', 'input[name="cellphone"]',
                    'input[placeholder*="טלפון"]', 'input[placeholder*="נייד"]',
                    'input[type="tel"]']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(inbar_phone)
                break

        for sel in ['#btnLogin', 'input[name="btnLogin"]',
                    'button[type="submit"]', 'input[type="submit"]',
                    '#btnSend', '#submitButton']:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                break
        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(2_000)

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
                break

        if not otp_field:
            log.warning("no OTP input found at %s — dumping page for selector fixes",
                        page.url)
            try:
                OTP_DEBUG_FILE.write_text(await page.content(), encoding="utf-8")
            except Exception as e:
                log.warning("could not dump OTP page: %s", e)
            return False

        relay.request_otp()
        log.info("waiting for OTP from Telegram (up to 5 min)...")

        otp_code = None
        for _ in range(75):
            await asyncio.sleep(4)
            otp_code = relay.poll_otp()
            if otp_code:
                break

        if not otp_code:
            log.warning("timeout waiting for OTP")
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
        await _accept_kmsi(page)

        success = is_on_grades_page(page.url)
        log.info("OTP login %s — at: %s", "succeeded" if success else "failed", page.url)
        return success
    except Exception as e:
        log.warning("OTP login error: %s", e)
        return False


async def portal_login(page, username: str, password: str) -> bool:
    """Route through the 'פורטל בר-אילן שלי' link → BIU central SSO.

    The portal session is much longer-lived than Inbar's, so this skips the
    SMS step most of the time. If the portal session is also expired, the
    redirect lands on ADFS where auto_login fills credentials.
    """
    try:
        link = await page.query_selector('a:has-text("פורטל")')
        if not link or not await link.is_visible():
            log.info("portal link not visible on %s", page.url)
            return False

        await link.click()
        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)
        log.info("after portal click, at: %s", page.url)

        if is_on_grades_page(page.url):
            log.info("SSO cookies valid — already on grades page")
            return True

        if username and password:
            if await auto_login(page, username, password):
                if not is_on_grades_page(page.url):
                    await page.goto(TARGET_URL, wait_until="domcontentloaded",
                                    timeout=30_000)
                    await page.wait_for_timeout(2_000)
                return is_on_grades_page(page.url)
        log.info("no credentials available — portal route cannot continue")
        return False
    except Exception as e:
        log.warning("portal login error: %s", e)
        return False


async def handle_login(page, username: str, password: str,
                       relay: RelayClient | None) -> bool:
    """Try every available login method in order. True if now on grades page."""
    log.info("trying portal SSO route...")
    if await portal_login(page, username, password):
        return True
    if username and password:
        log.info("trying direct ADFS auto-login...")
        if await auto_login(page, username, password):
            return True
    if relay is not None:
        log.info("trying SMS OTP via Telegram...")
        if await otp_login_via_telegram(page, relay):
            return True
    return False
