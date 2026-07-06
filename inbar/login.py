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


GRADES_URL_MARKER = "StudentAssignmentTermList"


async def ensure_grades_page(page) -> bool:
    """After a successful sign-in we may land on Main.aspx or the portal —
    always finish on the grades list page so callers can scrape immediately."""
    if GRADES_URL_MARKER in page.url and is_on_grades_page(page.url):
        return True
    log.info("logged in but at %s — navigating to grades page", page.url)
    try:
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_500)
    except Exception as e:
        log.warning("navigation to grades page failed: %s", e)
        return False
    ok = is_on_grades_page(page.url)
    log.info("grades page %s — now at: %s", "reached" if ok else "NOT reached", page.url)
    return ok


async def _click_first(page, selectors: list[str]) -> bool:
    for sel in selectors:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            await el.click()
            return True
    return False


async def _microsoft_login(page, username: str, password: str,
                           otp_provider=None) -> bool:
    """Microsoft Entra converged sign-in: email → password → MFA (SMS)."""
    try:
        email = await page.query_selector('input[name="loginfmt"]')
        if email and await email.is_visible():
            log.info("MS sign-in: filling account name")
            await email.fill(username)
            await _click_first(page, ['#idSIButton9', 'input[type="submit"]'])
            await page.wait_for_load_state('networkidle', timeout=20_000)
            await page.wait_for_timeout(1_000)

        pw_field = None
        try:
            pw_field = await page.wait_for_selector(
                'input[name="passwd"], input[type="password"]', timeout=12_000)
        except Exception:
            pass
        if pw_field and await pw_field.is_visible():
            log.info("MS sign-in: filling password")
            await pw_field.fill(password)
            await _click_first(page, ['#idSIButton9', 'input[type="submit"]',
                                      'button[type="submit"]'])
            await page.wait_for_load_state('networkidle', timeout=20_000)
            await page.wait_for_timeout(1_500)

        err = await page.query_selector('#passwordError, #usernameError')
        if err and await err.is_visible():
            log.warning("MS sign-in rejected credentials: %s",
                        (await err.inner_text()).strip())
            return False

        await _handle_ms_mfa(page, otp_provider)
        await _accept_kmsi(page)

        success = is_on_grades_page(page.url)
        log.info("MS sign-in %s — at: %s", "succeeded" if success else "failed", page.url)
        return success
    except Exception as e:
        log.warning("MS sign-in error at %s: %s", page.url, e)
        return False


async def _handle_ms_mfa(page, otp_provider) -> None:
    """Handle the 'Verify your identity' proof picker + SMS code entry."""
    proofs = await page.query_selector('#idDiv_SAOTCS_Proofs')
    if proofs:
        log.info("MFA: proof-selection page — choosing SMS")
        clicked = await _click_first(page, [
            'div[data-value="OneWaySMS"]',
            '#idDiv_SAOTCS_Proofs div[role="button"]',
            '#idDiv_SAOTCS_Proofs .table',
        ])
        if not clicked:
            log.warning("MFA: no clickable proof option found at %s", page.url)
        await page.wait_for_load_state('networkidle', timeout=20_000)
        await page.wait_for_timeout(1_500)

    otp_field = await page.query_selector('#idTxtBx_SAOTCC_OTC')
    if not (otp_field and await otp_field.is_visible()):
        return

    if otp_provider is None:
        log.warning("MFA: SMS code required but no OTP provider configured — "
                    "set TELEGRAM_BOT_TOKEN (bot asks in chat) or DAEMON_TOKEN (relay)")
        return

    log.info("MFA: SMS sent — waiting for code from OTP provider (up to 5 min)")
    code = await otp_provider()
    if not code:
        log.warning("MFA: no code received — cannot finish sign-in")
        return
    log.info("MFA: code received — submitting")
    await otp_field.fill(code)
    try:
        chk = await page.query_selector('#idChkBx_SAOTCC_TD')   # "don't ask again"
        if chk and await chk.is_visible():
            await chk.check()
    except Exception:
        pass
    await _click_first(page, ['#idSubmit_SAOTCC_Continue', 'input[type="submit"]',
                              'button[type="submit"]'])
    await page.wait_for_load_state('networkidle', timeout=20_000)
    await page.wait_for_timeout(1_500)


async def auto_login(page, username: str, password: str, otp_provider=None) -> bool:
    """Fill the SSO login form headlessly and wait for the redirect.

    Handles both the Microsoft Entra converged UI (login.microsoftonline.com)
    and the legacy ADFS/Shibboleth form.
    """
    try:
        log.info("credential login — page: %s", page.url)
        if ("login.microsoftonline.com" in page.url
                or await page.query_selector('input[name="loginfmt"]')):
            return await _microsoft_login(page, username, password, otp_provider)

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


async def otp_login_via_telegram(page, otp_provider) -> bool:
    """Fill Inbar ID+phone (from env), trigger SMS, get the code via provider."""
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

        log.info("waiting for OTP code (up to 5 min)...")
        otp_code = await otp_provider()
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


async def portal_login(page, username: str, password: str,
                       otp_provider=None) -> bool:
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
            log.info("SSO cookies valid — signed in without credentials")
            return await ensure_grades_page(page)

        if username and password:
            if await auto_login(page, username, password, otp_provider):
                return await ensure_grades_page(page)
        log.info("no credentials available — portal route cannot continue")
        return False
    except Exception as e:
        log.warning("portal login error: %s", e)
        return False


def _relay_otp_provider(relay: RelayClient):
    """Adapt the Cloudflare-relay OTP flow to the async otp_provider protocol."""
    async def _provider() -> str | None:
        relay.request_otp()
        for _ in range(75):
            await asyncio.sleep(4)
            code = relay.poll_otp()
            if code:
                return code
        return None
    return _provider


async def handle_login(page, username: str, password: str,
                       relay: RelayClient | None,
                       otp_provider=None) -> bool:
    """Try every available login method in order. True if now on grades page.

    ``otp_provider`` is an async callable returning the SMS code (or None on
    timeout). When omitted and a relay is given, the relay OTP flow is used.
    """
    if otp_provider is None and relay is not None:
        otp_provider = _relay_otp_provider(relay)

    log.info("login: trying portal SSO route...")
    if await portal_login(page, username, password, otp_provider):
        return True
    if username and password:
        log.info("login: trying direct credential sign-in...")
        if await auto_login(page, username, password, otp_provider):
            return await ensure_grades_page(page)
    if otp_provider is not None:
        log.info("login: trying Inbar SMS-OTP form...")
        if await otp_login_via_telegram(page, otp_provider):
            return await ensure_grades_page(page)
    log.warning("login: all methods failed — still at %s", page.url)
    return False
