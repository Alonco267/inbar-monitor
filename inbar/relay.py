"""HTTP client for the Cloudflare Worker relay (worker/worker.js).

Knowing the token IS the credential — same trust boundary as the Worker's own
endpoints. All methods log failures and degrade gracefully: a relay outage
must never crash a monitoring run.
"""

from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger("inbar.relay")

_TIMEOUT = 15


class RelayClient:
    def __init__(self, relay_url: str, token: str):
        self.relay_url = relay_url.rstrip("/")
        self.token = token

    # ── low level ─────────────────────────────────────────────────────────
    def _post(self, path: str, body: dict) -> requests.Response | None:
        try:
            return requests.post(f"{self.relay_url}{path}", json=body, timeout=_TIMEOUT)
        except requests.RequestException as e:
            log.warning("POST %s failed: %s", path, e)
            return None

    # ── alerts / status ───────────────────────────────────────────────────
    def send_alert(self, text: str) -> bool:
        r = self._post("/alert", {"token": self.token, "text": text})
        ok = r is not None and r.status_code == 200
        if not ok and r is not None:
            log.warning("alert → HTTP %s: %s", r.status_code, r.text[:200])
        return ok

    def heartbeat(self) -> None:
        self._post("/heartbeat", {"token": self.token})

    def push_grades_summary(self, summary: str) -> None:
        self._post("/grades-summary", {"token": self.token, "summary": summary})

    def push_gpa(self, gpa_text: str) -> None:
        self._post("/gpa", {"token": self.token, "gpa": gpa_text})

    # ── OTP relay ─────────────────────────────────────────────────────────
    def request_otp(self) -> bool:
        r = self._post("/request-otp", {"token": self.token})
        return r is not None and r.status_code == 200

    def poll_otp(self) -> str | None:
        try:
            r = requests.get(f"{self.relay_url}/poll-otp",
                             params={"token": self.token}, timeout=10)
            if r.status_code == 200:
                return r.json().get("code") or None
        except requests.RequestException:
            pass
        return None

    # ── daemon state (cookies + snapshots + failure counters) ─────────────
    def load_state(self) -> tuple[dict, bool]:
        """Returns (state_dict, paused). Empty dict when no state stored yet."""
        try:
            r = requests.get(f"{self.relay_url}/state",
                             params={"token": self.token}, timeout=_TIMEOUT)
        except requests.RequestException as e:
            log.warning("state GET failed: %s", e)
            return {}, False
        if r.status_code != 200:
            log.warning("state GET → HTTP %s: %s", r.status_code, r.text[:200])
            return {}, False
        payload = r.json()
        paused = bool(payload.get("paused"))
        blob = payload.get("state")
        if not blob:
            return {}, paused
        try:
            return json.loads(blob), paused
        except json.JSONDecodeError:
            log.warning("state blob is not valid JSON — ignoring")
            return {}, paused

    def save_state(self, state: dict) -> bool:
        body = {"token": self.token,
                "state": json.dumps(state, separators=(",", ":"))}
        try:
            r = requests.put(f"{self.relay_url}/state", json=body, timeout=_TIMEOUT)
        except requests.RequestException as e:
            log.warning("state PUT failed: %s", e)
            return False
        if r.status_code != 200:
            log.warning("state PUT → HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return True
