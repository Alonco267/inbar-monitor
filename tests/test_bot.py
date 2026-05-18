"""
E2E tests for the Telegram bot's per-update handler.

All tests call _handle_update() directly — no infinite loop, no real
network calls. _tg_post is always mocked so nothing goes to Telegram.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import monitor


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _upd(text: str, chat_id: int = 111, uid: int = 1) -> dict:
    return {
        "update_id": uid,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def _sent_to(calls: list) -> list[str]:
    return [str(p["chat_id"]) for m, p in calls if m == "sendMessage"]


def _texts_to(calls: list, chat_id) -> list[str]:
    return [p["text"] for m, p in calls
            if m == "sendMessage" and str(p["chat_id"]) == str(chat_id)]


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    """Capture _tg_post calls; return list of (method, payload) tuples."""
    calls = []
    monkeypatch.setattr(monitor, "_tg_post",
                        lambda m, p: calls.append((m, p)) or {"ok": True})
    return calls


@pytest.fixture
def no_io(monkeypatch):
    """Skip file I/O: registration never writes, DATA_FILE always looks ready."""
    monkeypatch.setattr(monitor, "register_user", lambda cid: False)
    mock_df = MagicMock()
    mock_df.exists.return_value = True
    mock_df.stat.return_value = MagicMock(st_size=500)
    monkeypatch.setattr(monitor, "DATA_FILE", mock_df)


# ──────────────────────────────────────────────────────────────────────────────
# Routing: each user gets their own reply
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grades_reply_goes_to_sender_not_owner(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=999))

    recipients = _sent_to(api)
    assert "999" in recipients
    assert monitor.TELEGRAM_CHAT_ID not in recipients


@pytest.mark.asyncio
async def test_connection_reply_goes_to_sender(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "_check_inbar_connection",
                        AsyncMock(return_value=(True, "ok")))
    await monitor._handle_update(_upd(monitor.BTN_CONNECTION, chat_id=888))
    assert "888" in _sent_to(api)


@pytest.mark.asyncio
async def test_two_users_no_cross_contamination(api, no_io, monkeypatch):
    """
    User A and User B both press BTN_GRADES.
    Each receives only their own reply — zero messages bleed across.
    """
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=111, uid=1))
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=222, uid=2))

    texts_111 = _texts_to(api, 111)
    texts_222 = _texts_to(api, 222)
    assert texts_111
    assert texts_222
    # Nothing sent to 111 mentions 222's chat_id and vice versa
    assert all("222" not in t for t in texts_111)
    assert all("111" not in t for t in texts_222)


@pytest.mark.asyncio
async def test_many_users_each_get_own_reply(api, no_io, monkeypatch):
    """Simulate 10 different users pressing buttons sequentially — no collisions."""
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    user_ids = list(range(1001, 1011))
    for i, uid in enumerate(user_ids):
        await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=uid, uid=i))

    recipients = set(_sent_to(api))
    for uid in user_ids:
        assert str(uid) in recipients


# ──────────────────────────────────────────────────────────────────────────────
# Grade alert broadcast
# ──────────────────────────────────────────────────────────────────────────────

def test_broadcast_alert_reaches_all_users(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {"111", "222", "333"})
    monitor.broadcast_alert("ציון חדש!")
    recipients = set(_sent_to(api))
    assert recipients == {"111", "222", "333"}


def test_broadcast_alert_correct_text(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {"111"})
    monitor.broadcast_alert("test message")
    texts = _texts_to(api, 111)
    assert texts == ["test message"]


def test_new_grade_event_sent_to_all_users(monkeypatch, api):
    """Full diff_and_alert: new grade → Hebrew message → all registered users."""
    monkeypatch.setattr(monitor, "load_users", lambda: {"111", "222", "333"})
    old = {"101|א'|01/10/2024": {
        "course_name": "פיזיקה", "moed": "א'", "lecturer": "ד\"ר לוי",
        "grade": "—", "final_grade": "—", "appeal_status": "—",
    }}
    new = {"101|א'|01/10/2024": {
        "course_name": "פיזיקה", "moed": "א'", "lecturer": "ד\"ר לוי",
        "grade": "90", "final_grade": "—", "appeal_status": "—",
    }}
    monitor.diff_and_alert(old, new)

    send_calls = [(m, p) for m, p in api if m == "sendMessage"]
    assert len(send_calls) == 3  # one per user
    for _, p in send_calls:
        assert "פיזיקה" in p["text"]
        assert "90" in p["text"]


def test_final_grade_event_sent_to_all_users(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {"111", "222"})
    old = {"101|א'|01/10/2024": {
        "course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
        "grade": "85", "final_grade": "—", "appeal_status": "—",
    }}
    new = {"101|א'|01/10/2024": {
        "course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
        "grade": "85", "final_grade": "87", "appeal_status": "—",
    }}
    monitor.diff_and_alert(old, new)

    send_calls = [(m, p) for m, p in api if m == "sendMessage"]
    assert len(send_calls) == 2
    for _, p in send_calls:
        assert "ציון סופי" in p["text"]
        assert "87" in p["text"]


def test_appeal_approved_event_content(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {"111"})
    old = {"101|א'|01/10/2024": {
        "course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
        "grade": "70", "final_grade": "—",
        "appeal_status": monitor.APPEAL_IN_PROGRESS,
    }}
    new = {"101|א'|01/10/2024": {
        "course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
        "grade": "80", "final_grade": "—", "appeal_status": "אושר",
    }}
    monitor.diff_and_alert(old, new)
    texts = _texts_to(api, 111)
    assert any("אישר" in t and "מתמטיקה" in t and "ד\"ר כהן" in t for t in texts)


def test_appeal_rejected_event_content(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {"111"})
    old = {"101|א'|01/10/2024": {
        "course_name": "פיזיקה", "moed": "א'", "lecturer": "ד\"ר לוי",
        "grade": "70", "final_grade": "—",
        "appeal_status": monitor.APPEAL_IN_PROGRESS,
    }}
    new = {"101|א'|01/10/2024": {
        "course_name": "פיזיקה", "moed": "א'", "lecturer": "ד\"ר לוי",
        "grade": "70", "final_grade": "—", "appeal_status": "נדחה",
    }}
    monitor.diff_and_alert(old, new)
    texts = _texts_to(api, 111)
    assert any("לא אישר" in t for t in texts)


# ──────────────────────────────────────────────────────────────────────────────
# User auto-registration
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_user_gets_registered(api, monkeypatch):
    registered = []
    monkeypatch.setattr(monitor, "register_user",
                        lambda cid: registered.append(str(cid)) or True)
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    monkeypatch.setattr(monitor, "DATA_FILE", MagicMock(
        **{"exists.return_value": True, "stat.return_value": MagicMock(st_size=0)}))
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=555))
    assert "555" in registered


def test_register_user_adds_new_id(tmp_path, monkeypatch):
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner")
    result = monitor.register_user("999")
    assert result is True
    assert "999" in monitor.load_users()


def test_register_user_idempotent(tmp_path, monkeypatch):
    uf = tmp_path / "users.json"
    uf.write_text(json.dumps(["999"]))
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    assert monitor.register_user("999") is False


def test_load_users_falls_back_to_owner_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "USERS_FILE", tmp_path / "nonexistent.json")
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner_id")
    assert "owner_id" in monitor.load_users()


def test_load_users_reads_all_registered(tmp_path, monkeypatch):
    uf = tmp_path / "users.json"
    uf.write_text(json.dumps(["111", "222", "333"]))
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    assert monitor.load_users() == {"111", "222", "333"}


# ──────────────────────────────────────────────────────────────────────────────
# Bot responses: content verification
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grades_button_shows_course_name(api, no_io, monkeypatch):
    data = {"101|א'|01/01/2024": {
        "course_name": "כימיה", "course_code": "101", "moed": "א'",
        "date": "01/01/2024", "grade": "88", "final_grade": "90",
        "appeal_status": "—", "lecturer": "ד\"ר מזרחי",
    }}
    monkeypatch.setattr(monitor, "load_saved", lambda: data)
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=111))
    texts = _texts_to(api, 111)
    assert any("כימיה" in t for t in texts)
    assert any("90" in t for t in texts)


@pytest.mark.asyncio
async def test_connection_connected_reply(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "_check_inbar_connection",
                        AsyncMock(return_value=(True, "ok")))
    await monitor._handle_update(_upd(monitor.BTN_CONNECTION, chat_id=111))
    texts = _texts_to(api, 111)
    assert any("מחובר" in t for t in texts)


@pytest.mark.asyncio
async def test_connection_disconnected_reply(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "_check_inbar_connection",
                        AsyncMock(return_value=(False, "session פג")))
    await monitor._handle_update(_upd(monitor.BTN_CONNECTION, chat_id=111))
    texts = _texts_to(api, 111)
    assert any("לא מחובר" in t for t in texts)


@pytest.mark.asyncio
async def test_unknown_command_gets_keyboard_back(api, no_io):
    await monitor._handle_update(_upd("שלום!", chat_id=777))
    assert "777" in _sent_to(api)


# ──────────────────────────────────────────────────────────────────────────────
# Offset tracking
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_update_returns_next_offset(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    result = await monitor._handle_update(_upd(monitor.BTN_GRADES, uid=42))
    assert result == 43


@pytest.mark.asyncio
async def test_empty_text_no_reply_correct_offset(api, no_io):
    update = {"update_id": 10, "message": {"chat": {"id": 111}, "text": ""}}
    result = await monitor._handle_update(update)
    assert result == 11
    assert not [c for c in api if c[0] == "sendMessage"]


@pytest.mark.asyncio
async def test_missing_text_field_no_crash(api, no_io):
    update = {"update_id": 5, "message": {"chat": {"id": 111}}}
    result = await monitor._handle_update(update)
    assert result == 6
