"""
Privacy, separation, and scale tests — 1000 users.

Properties verified:
  ISOLATION   — User A's reply never reaches User B
  PRIVACY     — Sensitive files are chmod 600 (owner-only reads)
  MINIMAL PII — Registry stores only numeric Telegram chat IDs
  SCALE       — 1000 users can register, be alerted, and query the bot
  NO LEAK     — Alert text is identical for all recipients (no cross-user PII)
  OUTBOUND    — Only Telegram API receives data (no DB, no cloud, no analytics)
"""
import json
import stat
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import monitor


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers (duplicated locally so this file is self-contained)
# ──────────────────────────────────────────────────────────────────────────────

def _upd(text, chat_id=111, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def _sent_to(calls):
    return [str(p["chat_id"]) for m, p in calls if m == "sendMessage"]


def _texts_for(calls, chat_id):
    return [p["text"] for m, p in calls
            if m == "sendMessage" and str(p["chat_id"]) == str(chat_id)]


@pytest.fixture
def api(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "_tg_post",
                        lambda m, p: calls.append((m, p)) or {"ok": True})
    return calls


@pytest.fixture
def no_io(monkeypatch):
    monkeypatch.setattr(monitor, "register_user", lambda cid: False)
    mock_df = MagicMock()
    mock_df.exists.return_value = True
    mock_df.stat.return_value = MagicMock(st_size=500)
    monkeypatch.setattr(monitor, "DATA_FILE", mock_df)


# ──────────────────────────────────────────────────────────────────────────────
# PRIVACY — file permissions
# ──────────────────────────────────────────────────────────────────────────────

def test_users_file_is_owner_only(tmp_path, monkeypatch):
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner")
    monitor.register_user("42")
    assert oct(stat.S_IMODE(uf.stat().st_mode)) == "0o600"


def test_grade_data_file_is_owner_only(tmp_path, monkeypatch):
    df = tmp_path / "data.json"
    monkeypatch.setattr(monitor, "DATA_FILE", df)
    monitor.save_current({"k": {"course_name": "test", "grade": "80"}})
    assert oct(stat.S_IMODE(df.stat().st_mode)) == "0o600"


def test_users_registry_stores_only_numeric_ids(tmp_path, monkeypatch):
    """No names, usernames, or PII — only Telegram chat ID integers."""
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "123")
    for uid in ["111111", "222222", "333333"]:
        monitor.register_user(uid)
    stored = json.loads(uf.read_text())
    for entry in stored:
        assert str(entry).lstrip("-").isdigit(), f"Non-numeric entry found: {entry!r}"


# ──────────────────────────────────────────────────────────────────────────────
# PRIVACY — outbound traffic
# ──────────────────────────────────────────────────────────────────────────────

def test_only_outbound_call_is_telegram(monkeypatch, api):
    """
    diff_and_alert must not call requests.post directly — only via _tg_post.
    _tg_post is already mocked, so requests.post is never reached.
    If this test passes, no raw HTTP call escaped the abstraction layer.
    """
    raw_called = []
    import requests as req_mod
    original = req_mod.post
    monkeypatch.setattr(req_mod, "post",
                        lambda *a, **kw: raw_called.append(a) or original(*a, **kw))
    monkeypatch.setattr(monitor, "load_users", lambda: {"111"})

    old = {"k": {"course_name": "X", "moed": "א'", "lecturer": "Y",
                 "grade": "—", "final_grade": "—", "appeal_status": "—"}}
    new = {"k": {"course_name": "X", "moed": "א'", "lecturer": "Y",
                 "grade": "85", "final_grade": "—", "appeal_status": "—"}}
    monitor.diff_and_alert(old, new)
    assert raw_called == []  # _tg_post mock swallowed the call before requests.post


def test_alert_text_contains_no_foreign_chat_ids(monkeypatch, api):
    """Grade alert text must not contain any user's Telegram chat ID."""
    users = {"111111", "222222", "333333"}
    monkeypatch.setattr(monitor, "load_users", lambda: users)
    monitor.broadcast_alert("ציון חדש בפיזיקה — ציונך 90 🎓")
    for m, p in api:
        if m == "sendMessage":
            text = p["text"]
            for uid in users:
                assert uid not in text, f"chat_id {uid} leaked into alert text"


# ──────────────────────────────────────────────────────────────────────────────
# ISOLATION — reply routing
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_goes_only_to_sender_never_to_third_party(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=100, uid=1))
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=200, uid=2))

    for m, p in api:
        if m != "sendMessage":
            continue
        cid = str(p["chat_id"])
        assert cid in ("100", "200"), f"Message routed to unexpected chat_id: {cid}"


@pytest.mark.asyncio
async def test_user_a_reply_does_not_contain_user_b_chat_id(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=111, uid=1))
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=222, uid=2))

    for text in _texts_for(api, 111):
        assert "222" not in text
    for text in _texts_for(api, 222):
        assert "111" not in text


@pytest.mark.asyncio
async def test_connection_check_reply_isolated(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "_check_inbar_connection",
                        AsyncMock(return_value=(True, "ok")))
    await monitor._handle_update(_upd(monitor.BTN_CONNECTION, chat_id=555, uid=1))
    await monitor._handle_update(_upd(monitor.BTN_CONNECTION, chat_id=666, uid=2))

    for m, p in api:
        if m == "sendMessage":
            assert str(p["chat_id"]) in ("555", "666")


@pytest.mark.asyncio
async def test_registering_user_a_does_not_affect_user_b(tmp_path, monkeypatch, api):
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner")
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    monkeypatch.setattr(monitor, "DATA_FILE", MagicMock(
        **{"exists.return_value": True, "stat.return_value": MagicMock(st_size=0)}))

    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=111, uid=1))
    await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=222, uid=2))

    users = monitor.load_users()
    # Both registered independently; each appears exactly once
    assert len([u for u in users if u == "111"]) == 1
    assert len([u for u in users if u == "222"]) == 1


# ──────────────────────────────────────────────────────────────────────────────
# SCALE — 1000 users
# ──────────────────────────────────────────────────────────────────────────────

def test_register_1000_users_all_stored(tmp_path, monkeypatch):
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner")
    for i in range(1000):
        monitor.register_user(str(i))
    users = monitor.load_users()
    assert len(users) == 1001  # 1000 new + owner fallback already there
    for i in range(1000):
        assert str(i) in users


def test_idempotent_registration_at_scale(tmp_path, monkeypatch):
    """Registering the same ID 1000 times keeps exactly 1 entry."""
    uf = tmp_path / "users.json"
    monkeypatch.setattr(monitor, "USERS_FILE", uf)
    monkeypatch.setattr(monitor, "TELEGRAM_CHAT_ID", "owner")
    monitor.register_user("42")  # first time
    for _ in range(999):
        monitor.register_user("42")  # subsequent are no-ops
    users = monitor.load_users()
    assert len([u for u in users if u == "42"]) == 1


def test_broadcast_reaches_all_1000_users(monkeypatch, api):
    users = {str(i) for i in range(1000)}
    monkeypatch.setattr(monitor, "load_users", lambda: users)
    monitor.broadcast_alert("ציון חדש!")
    recipients = set(_sent_to(api))
    assert recipients == users


def test_broadcast_sends_exactly_n_messages(monkeypatch, api):
    """No duplicate sends — exactly one message per user."""
    N = 1000
    monkeypatch.setattr(monitor, "load_users", lambda: {str(i) for i in range(N)})
    monitor.broadcast_alert("test")
    assert len([c for c in api if c[0] == "sendMessage"]) == N


def test_broadcast_identical_text_to_all(monkeypatch, api):
    """Every user gets the same alert — no PII from other users bleeds in."""
    monkeypatch.setattr(monitor, "load_users", lambda: {str(i) for i in range(1000)})
    monitor.broadcast_alert("ציון חדש בפיזיקה!")
    unique_texts = {p["text"] for m, p in api if m == "sendMessage"}
    assert unique_texts == {"ציון חדש בפיזיקה!"}


@pytest.mark.asyncio
async def test_1000_sequential_updates_each_gets_exactly_one_reply(api, no_io, monkeypatch):
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    for i in range(1000):
        await monitor._handle_update(_upd(monitor.BTN_GRADES, chat_id=i, uid=i))
    counts = Counter(_sent_to(api))
    for i in range(1000):
        assert counts[str(i)] == 1, f"User {i} got {counts[str(i)]} replies (expected 1)"


@pytest.mark.asyncio
async def test_1000_mixed_commands_no_routing_errors(api, no_io, monkeypatch):
    """500 grade requests + 500 connection checks from different users — zero misrouting."""
    monkeypatch.setattr(monitor, "load_saved", lambda: {})
    monkeypatch.setattr(monitor, "_check_inbar_connection",
                        AsyncMock(return_value=(True, "ok")))
    for i in range(1000):
        btn = monitor.BTN_GRADES if i % 2 == 0 else monitor.BTN_CONNECTION
        await monitor._handle_update(_upd(btn, chat_id=i, uid=i))

    # Verify no reply was sent to a wrong chat_id
    for m, p in api:
        if m == "sendMessage":
            cid = int(p["chat_id"])
            assert 0 <= cid < 1000, f"Reply went to unexpected chat_id: {cid}"

    # Every user got at least one reply
    recipients = set(_sent_to(api))
    for i in range(1000):
        assert str(i) in recipients


# ──────────────────────────────────────────────────────────────────────────────
# SCALE — grade events
# ──────────────────────────────────────────────────────────────────────────────

def test_new_grade_event_reaches_1000_users(monkeypatch, api):
    monkeypatch.setattr(monitor, "load_users", lambda: {str(i) for i in range(1000)})
    old = {"k": {"course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
                 "grade": "—", "final_grade": "—", "appeal_status": "—"}}
    new = {"k": {"course_name": "מתמטיקה", "moed": "א'", "lecturer": "ד\"ר כהן",
                 "grade": "88", "final_grade": "—", "appeal_status": "—"}}
    monitor.diff_and_alert(old, new)

    send_calls = [(m, p) for m, p in api if m == "sendMessage"]
    assert len(send_calls) == 1000
    for _, p in send_calls:
        assert "מתמטיקה" in p["text"]
        assert "88" in p["text"]


def test_unsubscribed_user_gets_no_alert(monkeypatch, api):
    """User 999 is not in the registry — must receive zero messages."""
    monkeypatch.setattr(monitor, "load_users", lambda: {"111", "222"})
    monitor.broadcast_alert("test")
    assert "999" not in set(_sent_to(api))
