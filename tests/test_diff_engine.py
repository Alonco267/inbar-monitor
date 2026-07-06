"""Unit tests for inbar.diff — the change-detection engine.

Every transition class gets a test, including the regressions this engine was
built to fix: grade UPDATES (not just appearance), appeal filed, generic
appeal-status changes, and value-keyed dedup that survives state loss.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from inbar.diff import (Alert, baseline_dedup_ids, compute_alerts,
                        filter_alerts, migrate_ever_seen)
from inbar.textutils import APPEAL_IN_PROGRESS

KEY = "88-101|א'|01/10/2024"


def _info(**kwargs) -> dict:
    defaults = dict(
        course_name="מתמטיקה", course_code="88-101", lecturer="ד\"ר כהן",
        moed="א'", date="01/10/2024", grade="—", final_grade="—",
        appeal_status="—",
    )
    defaults.update(kwargs)
    return defaults


def _kinds(alerts: list[Alert]) -> list[str]:
    return [a.kind for a in alerts]


# ── grade appearance (legacy behavior preserved) ─────────────────────────────

def test_grade_appears():
    alerts = compute_alerts({KEY: _info()}, {KEY: _info(grade="85")})
    assert _kinds(alerts) == ["grade_new"]
    assert "85" in alerts[0].message


def test_final_grade_appears():
    alerts = compute_alerts({KEY: _info(grade="85")},
                            {KEY: _info(grade="85", final_grade="87")})
    assert _kinds(alerts) == ["final_new"]
    assert "ציון סופי" in alerts[0].message


def test_unchanged_no_alert():
    info = _info(grade="85", final_grade="87")
    assert compute_alerts({KEY: info}, {KEY: info}) == []


# ── grade UPDATES — the main regression this engine fixes ────────────────────

def test_grade_increase_fires_update_alert():
    alerts = compute_alerts({KEY: _info(grade="78")}, {KEY: _info(grade="85")})
    assert _kinds(alerts) == ["grade_changed"]
    assert "78" in alerts[0].message and "85" in alerts[0].message
    assert "📈" in alerts[0].message


def test_grade_decrease_fires_update_alert():
    alerts = compute_alerts({KEY: _info(grade="85")}, {KEY: _info(grade="78")})
    assert _kinds(alerts) == ["grade_changed"]
    assert "📉" in alerts[0].message


def test_final_grade_change_fires_alert():
    alerts = compute_alerts({KEY: _info(final_grade="80")},
                            {KEY: _info(final_grade="90")})
    assert _kinds(alerts) == ["final_changed"]


def test_grade_removed_no_alert():
    # value → empty is not alert-worthy (usually a scrape glitch)
    alerts = compute_alerts({KEY: _info(grade="85")}, {KEY: _info(grade="—")})
    assert alerts == []


# ── appeal transitions ────────────────────────────────────────────────────────

def test_appeal_filed_fires_alert():
    alerts = compute_alerts(
        {KEY: _info(grade="70")},
        {KEY: _info(grade="70", appeal_status=APPEAL_IN_PROGRESS)})
    assert _kinds(alerts) == ["appeal_filed"]
    assert "בטיפול" in alerts[0].message


def test_appeal_resolved_approved():
    alerts = compute_alerts(
        {KEY: _info(grade="70", appeal_status=APPEAL_IN_PROGRESS)},
        {KEY: _info(grade="80", appeal_status="בקשתך אושרה")})
    # grade_changed is suppressed — the appeal message reports the movement
    assert _kinds(alerts) == ["appeal_resolved"]
    assert "אישר" in alerts[0].message
    assert "80" in alerts[0].message and "70" in alerts[0].message


def test_appeal_resolved_rejected():
    alerts = compute_alerts(
        {KEY: _info(grade="70", appeal_status=APPEAL_IN_PROGRESS)},
        {KEY: _info(grade="70", appeal_status="בקשתך נדחתה")})
    assert _kinds(alerts) == ["appeal_resolved"]
    assert "לא אישר" in alerts[0].message


def test_appeal_generic_status_change():
    alerts = compute_alerts(
        {KEY: _info(appeal_status="הועבר למרצה")},
        {KEY: _info(appeal_status="ממתין לאישור סופי")})
    assert _kinds(alerts) == ["appeal_changed"]


def test_appeal_refiled_after_rejection():
    alerts = compute_alerts(
        {KEY: _info(appeal_status="נדחה")},
        {KEY: _info(appeal_status=APPEAL_IN_PROGRESS)})
    assert _kinds(alerts) == ["appeal_filed"]


def test_appeal_empty_to_resolved_stays_silent():
    # Baseline predates appeal — legacy behavior kept to avoid noise.
    alerts = compute_alerts({KEY: _info(appeal_status="—")},
                            {KEY: _info(appeal_status="אושר")})
    assert alerts == []


# ── new exam rows ─────────────────────────────────────────────────────────────

def test_new_ungraded_row_fires_informational_alert():
    other = "88-202|א'|01/11/2024"
    alerts = compute_alerts(
        {KEY: _info(grade="85")},
        {KEY: _info(grade="85"),
         other: _info(course_code="88-202", course_name="פיזיקה", moed="ב'")})
    assert _kinds(alerts) == ["row_new"]
    assert "פיזיקה" in alerts[0].message


def test_new_graded_row_fires_grade_alert_not_row_alert():
    other = "88-202|א'|01/11/2024"
    alerts = compute_alerts(
        {KEY: _info(grade="85")},
        {KEY: _info(grade="85"),
         other: _info(course_code="88-202", course_name="פיזיקה", grade="90")})
    assert _kinds(alerts) == ["grade_new"]


def test_no_row_alert_on_empty_baseline():
    alerts = compute_alerts({}, {KEY: _info()})
    assert alerts == []


# ── dedup / ever_alerted ──────────────────────────────────────────────────────

def test_filter_alerts_drops_already_sent():
    alerts = compute_alerts({KEY: _info()}, {KEY: _info(grade="85")})
    to_send, ever = filter_alerts(alerts, {})
    assert len(to_send) == 1
    # Same alert computed again (e.g. state lost) → suppressed
    to_send2, _ = filter_alerts(alerts, ever)
    assert to_send2 == []


def test_filter_alerts_allows_new_value_for_same_key():
    a1 = compute_alerts({KEY: _info()}, {KEY: _info(grade="85")})
    _, ever = filter_alerts(a1, {})
    a2 = compute_alerts({KEY: _info(grade="85")}, {KEY: _info(grade="90")})
    to_send, _ = filter_alerts(a2, ever)
    assert [a.kind for a in to_send] == ["grade_changed"]


def test_filter_alerts_does_not_mutate_input():
    alerts = compute_alerts({KEY: _info()}, {KEY: _info(grade="85")})
    original: dict = {}
    filter_alerts(alerts, original)
    assert original == {}


def test_baseline_dedup_ids_cover_existing_values():
    grades = {KEY: _info(grade="85", final_grade="87", appeal_status="נדחה")}
    ids = baseline_dedup_ids(grades)
    assert f"{KEY}|grade|85" in ids
    assert f"{KEY}|final_grade|87" in ids
    assert f"{KEY}|appeal|נדחה" in ids
    assert f"{KEY}|row" in ids
    # Baseline must silence a re-diff from an empty snapshot
    alerts = compute_alerts({}, grades)
    to_send, _ = filter_alerts(alerts, ids)
    assert to_send == []


def test_migrate_ever_seen_prevents_realerts():
    legacy = {KEY: {"grade": "85", "final_grade": "—"}}
    ids = migrate_ever_seen(legacy)
    alerts = compute_alerts({}, {KEY: _info(grade="85")})
    to_send, _ = filter_alerts(alerts, ids)
    assert to_send == []
