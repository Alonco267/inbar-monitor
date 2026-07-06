#!/usr/bin/env python3
"""
One-shot grade check for cron / GitHub Actions — the PRODUCTION monitoring path.

Pulls browser-session state (Playwright `storage_state`), last-seen grades and
the ever-alerted dedup set from the Cloudflare Worker KV via the inbar-relay
endpoints, runs a headless scrape, sends alerts via the relay, and pushes
updated state back. No local files, no persistent profile — designed for
ephemeral CI runners.

Required env vars:
  RELAY_URL          e.g. https://inbar-relay.example.workers.dev
  DAEMON_TOKEN       the per-user random hex token bound to your Telegram chat
  INBAR_USERNAME     for auto-login when session expires
  INBAR_PASSWORD     ^

Optional env vars:
  INBAR_ID           Israeli ID for the OTP fallback form
  INBAR_PHONE        phone for the OTP fallback form
  INBAR_AVERAGES_URL override for the official "Average Grades" page

Login-failure policy (3-strike):
  Consecutive login failures are counted in relay state. After the 3rd
  consecutive failure ONE Telegram alert is sent; further failures stay
  silent. The cron keeps retrying every run, and on the first successful
  login after a notification a single recovery message is sent.

Exit codes:
  0  success, paused, or handled login failure (retry next cron tick)
  1  unrecoverable error (missing config, repeated scrape crashes)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from inbar.config import CONTEXT_OPTS, TARGET_URL, setup_logging
from inbar.diff import (baseline_dedup_ids, compute_alerts, filter_alerts,
                        migrate_ever_seen)
from inbar.formatting import build_final_grades_message
from inbar.login import LoginFailed, handle_login
from inbar.relay import RelayClient
from inbar.scrape import extract_gpa, extract_grades

log = logging.getLogger("inbar.once")

LOGIN_FAILURE_THRESHOLD = 3
MAX_ATTEMPTS = 3

LOGIN_EXPIRED_MSG = (
    "ההתחברות לאינ-בר פגה 🔑\n"
    "ניסיתי להתחבר מחדש 3 פעמים ברצף ללא הצלחה.\n"
    "אמשיך לנסות ברקע אוטומטית — אם יש בעיה בסיסמה/חשבון, עדכן/י את הסודות."
)
LOGIN_RECOVERED_MSG = "🟢 החיבור לאינ-בר התאושש — הניטור חזר לפעול כרגיל."


def _env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        log.error("missing required env var: %s", name)
        sys.exit(1)
    return v


def _extract_ever_alerted(state: dict) -> dict:
    """Read the dedup set from state, migrating the legacy ever_seen format."""
    if "ever_alerted" in state:
        return dict(state.get("ever_alerted") or {})
    legacy = state.get("ever_seen") or {}
    if legacy:
        log.info("migrating legacy ever_seen (%d entries) → ever_alerted", len(legacy))
        return migrate_ever_seen(legacy)
    return {}


async def _dump_login_page(page) -> None:
    """Save the page we got stuck on so selectors can be fixed offline."""
    try:
        html = await page.content()
        Path("debug_login_page.html").write_text(html, encoding="utf-8")
        log.warning("dumped login page HTML (%d chars)", len(html))
    except Exception as dump_err:
        log.warning("could not dump page: %s", dump_err)


async def _scrape_attempt(relay: RelayClient, username: str, password: str,
                          state: dict) -> dict:
    """Single browser scrape + alert attempt.

    Returns the new state dict to persist. Raises LoginFailed when no login
    method works, or any other exception for retryable failures.
    """
    storage_state = state.get("storage_state")
    old_grades = state.get("grades", {})
    ever_alerted = _extract_ever_alerted(state)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs = dict(CONTEXT_OPTS)
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        ctx = await browser.new_context(**ctx_kwargs)
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2_000)

            def on_grades_url() -> bool:
                return "StudentAssignmentTermList" in urlparse(page.url).path

            if not on_grades_url():
                log.info("not on grades page (at: %s) — attempting login", page.url)
                if not await handle_login(page, username, password, relay):
                    await _dump_login_page(page)
                    raise LoginFailed(f"all login methods failed at {page.url}")
                if not on_grades_url():
                    await page.goto(TARGET_URL, wait_until="domcontentloaded",
                                    timeout=30_000)
                    await page.wait_for_timeout(2_000)

            log.info("extracting grades...")
            grades = await extract_grades(page)
            if not grades:
                raise RuntimeError("no grades extracted — page may have changed")
            log.info("%d row(s) parsed", len(grades))

            is_first_run = not old_grades and not ever_alerted
            if is_first_run:
                log.info("first run — saving baseline without alerting")
                ever_alerted = baseline_dedup_ids(grades)
            else:
                alerts = compute_alerts(old_grades, grades)
                to_send, ever_alerted = filter_alerts(alerts, ever_alerted)
                for alert in to_send:
                    log.info("alert [%s] %s", alert.kind, alert.key)
                    relay.send_alert(alert.message)
                if not to_send:
                    log.info("no new changes")

            # Recovery notice: first success after a login-expired notification.
            if state.get("login_notified"):
                relay.send_alert(LOGIN_RECOVERED_MSG)
                log.info("sent login-recovered notice")

            relay.push_grades_summary(build_final_grades_message(grades))

            # Official GPA — best effort, never fails the run.
            gpa_text = await extract_gpa(page)
            if gpa_text:
                relay.push_gpa(gpa_text)
                log.info("official GPA pushed to relay")
            else:
                log.info("official GPA unavailable this run")

            relay.heartbeat()

            return {
                "storage_state": await ctx.storage_state(),
                "grades": grades,
                "ever_alerted": ever_alerted,
                "login_failures": 0,
                "login_notified": False,
            }
        finally:
            await ctx.close()
            await browser.close()


def _handle_login_failure(relay: RelayClient, state: dict) -> None:
    """3-strike accounting: notify exactly once, keep retrying via cron."""
    failures = int(state.get("login_failures", 0)) + 1
    notified = bool(state.get("login_notified", False))
    log.warning("login failure #%d (notified=%s)", failures, notified)

    if failures >= LOGIN_FAILURE_THRESHOLD and not notified:
        if relay.send_alert(LOGIN_EXPIRED_MSG):
            notified = True
            log.info("sent one-time login-expired alert")

    new_state = dict(state)
    new_state["login_failures"] = failures
    new_state["login_notified"] = notified
    if not relay.save_state(new_state):
        log.warning("could not persist failure counter — may recount next run")


async def run_once() -> int:
    setup_logging()
    relay_url = _env("RELAY_URL")
    token = _env("DAEMON_TOKEN").lower()
    username = _env("INBAR_USERNAME")
    password = _env("INBAR_PASSWORD")

    relay = RelayClient(relay_url, token)
    state, paused = relay.load_state()

    if paused:
        log.info("monitoring is PAUSED (user request) — skipping run")
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            wait = 20 * attempt
            log.info("retry %d/%d — waiting %ds...", attempt, MAX_ATTEMPTS, wait)
            await asyncio.sleep(wait)
        try:
            new_state = await _scrape_attempt(relay, username, password, state)
            if not relay.save_state(new_state):
                log.warning("state save failed — next run may re-login")
            return 0
        except LoginFailed as e:
            # Not retryable within this run — the same login would fail again.
            log.warning("login failed: %s", e)
            _handle_login_failure(relay, state)
            return 0   # cron keeps retrying; Telegram already notified (once)
        except Exception as e:
            log.error("attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, e)

    log.error("all %d attempts failed — giving up this run", MAX_ATTEMPTS)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_once()))
