#!/usr/bin/env python3
"""
One-time bootstrap: open a visible browser, you log in to Inbar manually,
then this script pushes the resulting Playwright `storage_state` to the
Cloudflare Worker KV under your DAEMON_TOKEN. After that, GitHub Actions
runs of monitor_once.py can scrape headlessly without re-logging in.

Usage:
  python bootstrap_kv.py

Required env vars (load from .env or export inline):
  RELAY_URL        e.g. https://inbar-relay.alonco267.workers.dev
  DAEMON_TOKEN     the per-user random hex token bound to your Telegram chat

Re-run any time the session truly expires (cookies aged out) — usually weeks.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

import monitor as m

load_dotenv(Path(__file__).parent / ".env")


async def bootstrap() -> int:
    relay_url = os.environ.get("RELAY_URL", "").strip()
    token     = os.environ.get("DAEMON_TOKEN", "").strip().lower()
    if not relay_url or not token:
        print("Set RELAY_URL and DAEMON_TOKEN in .env (or env vars).", file=sys.stderr)
        return 1

    # Verify the token is actually linked before we waste effort.
    r = requests.get(f"{relay_url}/status", params={"token": token}, timeout=15)
    if r.status_code != 200 or not r.json().get("linked"):
        print(f"Token is not linked to a Telegram chat (got: {r.text}).", file=sys.stderr)
        print("Link first: open https://t.me/InbarGradesBot?start=<token>", file=sys.stderr)
        return 1

    print("Opening browser — log in to Inbar in the window that pops up.")
    print("The script continues automatically once you land on the grades page.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(**m.CONTEXT_OPTS)
        page = await ctx.new_page()
        await page.goto(m.TARGET_URL)
        await m._wait_for_manual_login(page)

        # First scrape so we save a baseline alongside the cookies. This
        # ensures the next monitor_once.py run won't fire alerts for every
        # already-existing grade.
        print("\n[BOOTSTRAP] Extracting initial grades for baseline...")
        grades = await m.extract_grades(page)
        storage_state = await ctx.storage_state()
        await ctx.close()
        await browser.close()

    if not grades:
        print("[BOOTSTRAP] WARN: no grades extracted — saving cookies only.")
        grades = {}

    state = {"storage_state": storage_state, "grades": grades}
    body = {"token": token, "state": json.dumps(state, separators=(",", ":"))}
    r = requests.put(f"{relay_url}/state", json=body, timeout=30)
    if r.status_code != 200:
        print(f"[BOOTSTRAP] state PUT failed: HTTP {r.status_code} — {r.text}",
              file=sys.stderr)
        return 1

    size_kb = len(body["state"]) / 1024
    print(f"\n[BOOTSTRAP] ✅ Saved session + {len(grades)} grade row(s) to KV ({size_kb:.1f} KB).")
    print("[BOOTSTRAP] You can now run `python monitor_once.py` or trigger GitHub Actions.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(bootstrap()))
