#!/usr/bin/env python3
"""
One-shot grade check for cron / GitHub Actions.

Pulls browser-session state (Playwright `storage_state`) and last-seen grades
from the Cloudflare Worker KV via the inbar-relay endpoints, runs a headless
scrape, sends alerts via the relay's /alert endpoint, and pushes updated state
back. No local files, no persistent profile — designed to run on ephemeral
CI runners.

Required env vars:
  RELAY_URL          e.g. https://inbar-relay.alonco267.workers.dev
  DAEMON_TOKEN       the per-user random hex token bound to your Telegram chat
  INBAR_USERNAME     for auto-login when session expires
  INBAR_PASSWORD     ^

Optional env vars (OTP fallback if auto-login alone fails):
  INBAR_ID           Israeli ID for the OTP form
  INBAR_PHONE        phone for the OTP form

Exit codes:
  0  success (scrape ran, alerts dispatched, state saved)
  1  unrecoverable error (missing config, login totally failed, etc.)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# Reuse extraction + diffing logic from the daemon module.
import monitor as m


def _env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"[ONCE] missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _load_state(relay_url: str, token: str) -> dict | None:
    """GET /state — returns the parsed JSON blob, or None if no state yet."""
    try:
        r = requests.get(f"{relay_url}/state", params={"token": token}, timeout=15)
    except requests.RequestException as e:
        print(f"[ONCE] state GET failed: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[ONCE] state GET → HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return None
    blob = r.json().get("state")
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        print("[ONCE] state blob is not valid JSON — ignoring", file=sys.stderr)
        return None


def _save_state(relay_url: str, token: str, state: dict) -> bool:
    body = {"token": token, "state": json.dumps(state, separators=(",", ":"))}
    try:
        r = requests.put(f"{relay_url}/state", json=body, timeout=15)
    except requests.RequestException as e:
        print(f"[ONCE] state PUT failed: {e}", file=sys.stderr)
        return False
    if r.status_code != 200:
        print(f"[ONCE] state PUT → HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return False
    return True


def _send_alert(relay_url: str, token: str, text: str) -> None:
    try:
        r = requests.post(f"{relay_url}/alert", json={"token": token, "text": text}, timeout=15)
        if r.status_code != 200:
            print(f"[ONCE] alert → HTTP {r.status_code}: {r.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[ONCE] alert failed: {e}", file=sys.stderr)


def _push_grades_summary(relay_url: str, token: str, grades: dict) -> None:
    # Reuse the daemon's formatter. It reads from disk (DATA_FILE) so we
    # temporarily overwrite that path with our in-memory grades. Cheaper than
    # duplicating the formatting logic.
    m.DATA_FILE.write_text(json.dumps(grades, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = m._build_final_grades_message()
    try:
        requests.post(f"{relay_url}/grades-summary",
                      json={"token": token, "summary": summary}, timeout=15)
    except requests.RequestException as e:
        print(f"[ONCE] summary push failed: {e}", file=sys.stderr)


async def _scrape_attempt(
    relay_url: str, token: str, username: str, password: str,
    storage_state, old_grades: dict, ever_seen: dict,
) -> int:
    """
    Single browser scrape + alert attempt.
    Raises on unrecoverable errors so the caller can retry.
    Returns 0 on success, 1 on non-retryable failure (e.g. login totally failed).
    """
    from urllib.parse import urlparse

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs = dict(m.CONTEXT_OPTS)
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        ctx = await browser.new_context(**ctx_kwargs)
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        try:
            await page.goto(m.TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2_000)

            def on_grades_url():
                return "StudentAssignmentTermList" in urlparse(page.url).path

            if not on_grades_url():
                print(f"[ONCE] not on grades page (at: {page.url}) — attempting login")
                ok = await m._handle_login(page, username, password, relay_url, token)
                if not ok:
                    try:
                        html = await page.content()
                        Path("debug_login_page.html").write_text(html, encoding="utf-8")
                        print(f"[ONCE] dumped page HTML ({len(html)} chars)", file=sys.stderr)
                        inputs = await page.eval_on_selector_all(
                            "input",
                            "els => els.map(e => ({tag:e.tagName, type:e.type, name:e.name, "
                            "id:e.id, placeholder:e.placeholder, visible:e.offsetParent!==null}))"
                        )
                        for inp in inputs:
                            print(f"  {inp}", file=sys.stderr)
                    except Exception as dump_err:
                        print(f"[ONCE] could not dump page: {dump_err}", file=sys.stderr)
                    print("[ONCE] login failed — non-retryable", file=sys.stderr)
                    return 1   # login failure: don't retry (would just fail again)
                if not on_grades_url():
                    print(f"[ONCE] navigating from {page.url} to grades page")
                    await page.goto(m.TARGET_URL, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2_000)

            print("[ONCE] extracting grades...")
            grades = await m.extract_grades(page)
            if not grades:
                raise RuntimeError("no grades extracted — page may have changed")

            print(f"[ONCE] {len(grades)} row(s) parsed")

            # Build merged_old: old_grades enriched with ever_seen so that grades
            # we've already alerted on look "old" to diff_and_alert — even if
            # the KV state was lost between runs.
            merged_old: dict = dict(old_grades)
            for key, seen in ever_seen.items():
                entry = dict(merged_old.get(key, {}))
                if not m._is_empty(seen.get("grade", "")):
                    entry["grade"] = seen["grade"]
                if not m._is_empty(seen.get("final_grade", "")):
                    entry["final_grade"] = seen["final_grade"]
                merged_old[key] = entry

            if old_grades or ever_seen:
                m.diff_and_alert(
                    merged_old,
                    grades,
                    sender=lambda text: _send_alert(relay_url, token, text),
                )
            else:
                print("[ONCE] first run — saving baseline without alerting")

            # Expand ever_seen with any graded entries from this scrape.
            new_ever_seen: dict = dict(ever_seen)
            for key, info in grades.items():
                grade = info.get("grade", "—")
                fg    = info.get("final_grade", "—")
                if not m._is_empty(grade) or not m._is_empty(fg):
                    new_ever_seen[key] = {"grade": grade, "final_grade": fg}

            _push_grades_summary(relay_url, token, grades)
            requests.post(f"{relay_url}/heartbeat", json={"token": token}, timeout=15)

            new_storage_state = await ctx.storage_state()
            ok = _save_state(relay_url, token, {
                "storage_state": new_storage_state,
                "grades": grades,
                "ever_seen": new_ever_seen,
            })
            if not ok:
                print("[ONCE] WARN: state save failed — next run will re-login",
                      file=sys.stderr)
            return 0
        finally:
            await ctx.close()
            await browser.close()


async def run_once() -> int:
    relay_url = _env("RELAY_URL")
    token     = _env("DAEMON_TOKEN").lower()
    username  = _env("INBAR_USERNAME")
    password  = _env("INBAR_PASSWORD")

    state         = _load_state(relay_url, token) or {}
    storage_state = state.get("storage_state")
    old_grades    = state.get("grades", {})
    ever_seen     = state.get("ever_seen", {})   # grows monotonically, never cleared

    for attempt in range(1, 4):
        if attempt > 1:
            wait = 20 * attempt
            print(f"[ONCE] retry {attempt}/3 — waiting {wait}s...")
            await asyncio.sleep(wait)
        try:
            result = await _scrape_attempt(
                relay_url, token, username, password,
                storage_state, old_grades, ever_seen,
            )
            if result == 1:
                return 1   # non-retryable (login failure)
            return 0
        except Exception as e:
            print(f"[ONCE] attempt {attempt}/3 failed: {e}", file=sys.stderr)

    print("[ONCE] all 3 attempts failed — giving up", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_once()))
