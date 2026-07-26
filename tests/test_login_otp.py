"""Tests for the relay OTP provider — single-shot per run to fit the CI budget.

Regression guard for the 8-minute-timeout bug: the MS-MFA path and the Inbar
OTP-form path both ask the provider, and two back-to-back 5-minute waits blew
past the job timeout (every scheduled run got cancelled) and re-sent the SMS.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from inbar import login


class FakeRelay:
    def __init__(self, code=None):
        self.code = code
        self.request_count = 0

    def request_otp(self):
        self.request_count += 1
        return True

    def poll_otp(self):
        return self.code


async def _instant_sleep(seconds):
    """Drop-in for asyncio.sleep so poll loops don't burn wall-clock in tests."""
    return None


def test_provider_returns_code_on_first_call(monkeypatch):
    monkeypatch.setattr(login.asyncio, "sleep", _instant_sleep)
    relay = FakeRelay(code="123456")
    provider = login._relay_otp_provider(relay)
    assert asyncio.run(provider()) == "123456"
    assert relay.request_count == 1


def test_provider_is_single_shot(monkeypatch):
    """Second call in the same run returns None without a second SMS/wait."""
    monkeypatch.setattr(login.asyncio, "sleep", _instant_sleep)
    relay = FakeRelay(code="123456")
    provider = login._relay_otp_provider(relay)

    first = asyncio.run(provider())
    second = asyncio.run(provider())

    assert first == "123456"
    assert second is None
    assert relay.request_count == 1  # SMS sent exactly once


def test_second_call_does_not_wait(monkeypatch):
    """The skipped second call must not sleep — that's the whole point."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(login.asyncio, "sleep", fake_sleep)
    relay = FakeRelay(code="000000")
    provider = login._relay_otp_provider(relay)

    asyncio.run(provider())          # consumes the single shot (code immediate)
    slept.clear()
    asyncio.run(provider())          # must skip entirely
    assert slept == []


def test_wait_budget_fits_ci_timeout():
    """Total worst-case OTP wait must stay under the 8-min job budget."""
    worst_case_seconds = login.OTP_POLL_ATTEMPTS * login.OTP_POLL_INTERVAL_S
    assert worst_case_seconds <= 5 * 60  # comfortably under 8-min timeout
