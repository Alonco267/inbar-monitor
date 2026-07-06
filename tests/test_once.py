"""Tests for monitor_once: 3-strike login-failure policy, state migration, pause."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor_once as mo


class FakeRelay:
    def __init__(self, paused=False, state=None):
        self._paused = paused
        self._state = state or {}
        self.alerts: list[str] = []
        self.saved_states: list[dict] = []

    def load_state(self):
        return self._state, self._paused

    def save_state(self, state):
        self.saved_states.append(state)
        return True

    def send_alert(self, text):
        self.alerts.append(text)
        return True


# ── 3-strike login failure policy ─────────────────────────────────────────────

def test_first_two_failures_are_silent():
    relay = FakeRelay()
    mo._handle_login_failure(relay, {"login_failures": 0})
    mo._handle_login_failure(relay, {"login_failures": 1})
    assert relay.alerts == []
    assert relay.saved_states[-1]["login_failures"] == 2


def test_third_failure_notifies_exactly_once():
    relay = FakeRelay()
    mo._handle_login_failure(relay, {"login_failures": 2, "login_notified": False})
    assert relay.alerts == [mo.LOGIN_EXPIRED_MSG]
    assert relay.saved_states[-1]["login_notified"] is True


def test_failures_after_notification_stay_silent():
    relay = FakeRelay()
    mo._handle_login_failure(relay, {"login_failures": 7, "login_notified": True})
    assert relay.alerts == []
    assert relay.saved_states[-1]["login_failures"] == 8


def test_failure_counter_preserves_other_state():
    relay = FakeRelay()
    state = {"login_failures": 0, "grades": {"k": {"grade": "90"}},
             "ever_alerted": {"x": 1}}
    mo._handle_login_failure(relay, state)
    saved = relay.saved_states[-1]
    assert saved["grades"] == {"k": {"grade": "90"}}
    assert saved["ever_alerted"] == {"x": 1}
    # input state not mutated
    assert state["login_failures"] == 0


# ── ever_alerted extraction / migration ───────────────────────────────────────

def test_extract_prefers_new_format():
    state = {"ever_alerted": {"a|grade|90": 1}, "ever_seen": {"a": {"grade": "80"}}}
    assert mo._extract_ever_alerted(state) == {"a|grade|90": 1}


def test_extract_migrates_legacy_ever_seen():
    state = {"ever_seen": {"k|א'|01/01/2025": {"grade": "85", "final_grade": "—"}}}
    ids = mo._extract_ever_alerted(state)
    assert "k|א'|01/01/2025|grade|85" in ids


def test_extract_empty_state():
    assert mo._extract_ever_alerted({}) == {}


# ── pause flag short-circuits the run ─────────────────────────────────────────

def test_paused_run_skips_scrape_and_exits_zero(monkeypatch):
    monkeypatch.setenv("RELAY_URL", "https://relay.test")
    monkeypatch.setenv("DAEMON_TOKEN", "a" * 48)
    monkeypatch.setenv("INBAR_USERNAME", "user")
    monkeypatch.setenv("INBAR_PASSWORD", "pass")

    fake = FakeRelay(paused=True)
    scrape = MagicMock(side_effect=AssertionError("must not scrape while paused"))
    with patch.object(mo, "RelayClient", return_value=fake), \
         patch.object(mo, "_scrape_attempt", scrape):
        rc = asyncio.run(mo.run_once())
    assert rc == 0
    scrape.assert_not_called()
