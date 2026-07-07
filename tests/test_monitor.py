"""
E2E / unit tests for monitor.py.

Run:  pytest tests/ -v
"""
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py sets fake env vars before this import
sys.path.insert(0, str(Path(__file__).parent.parent))
import monitor


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_info(**kwargs) -> dict:
    defaults = dict(
        course_name="מתמטיקה",
        course_code="88-101",
        lecturer="ד\"ר כהן",
        moed="א'",
        date="01/10/2024",
        grade="—",
        final_grade="—",
        appeal_status="—",
    )
    defaults.update(kwargs)
    return defaults


# ──────────────────────────────────────────────────────────────────────────────
# _is_login_page / _is_on_grades_page
# ──────────────────────────────────────────────────────────────────────────────

class TestUrlHelpers:
    def test_login_page_shibboleth(self):
        assert monitor._is_login_page("https://shibboleth.biu.ac.il/idp/Authn/")

    def test_login_page_adfs(self):
        assert monitor._is_login_page("https://adfs.example.com/adfs/ls/")

    def test_not_login_page(self):
        assert not monitor._is_login_page("https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx")

    def test_on_grades_page_true(self):
        assert monitor._is_on_grades_page("https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx")

    def test_on_grades_page_false_login(self):
        assert not monitor._is_on_grades_page("https://shibboleth.biu.ac.il/idp/Authn/")

    def test_on_grades_page_false_unrelated(self):
        assert not monitor._is_on_grades_page("https://google.com")


# ──────────────────────────────────────────────────────────────────────────────
# _is_empty
# ──────────────────────────────────────────────────────────────────────────────

class TestIsEmpty:
    def test_none_is_empty(self):
        assert monitor._is_empty(None)

    def test_empty_string(self):
        assert monitor._is_empty("")

    def test_dash(self):
        assert monitor._is_empty("—")

    def test_hyphen(self):
        assert monitor._is_empty("-")

    def test_na_hebrew(self):
        assert monitor._is_empty("נ/א")

    def test_nbsp(self):
        # &nbsp; comes through as   from Playwright inner_text
        assert monitor._is_empty(" ")

    def test_nbsp_padded(self):
        assert monitor._is_empty("     ")

    def test_real_grade_not_empty(self):
        assert not monitor._is_empty("85")

    def test_zero_grade_not_empty(self):
        assert not monitor._is_empty("0")

    def test_hundred_not_empty(self):
        assert not monitor._is_empty("100")

    def test_hebrew_text_not_empty(self):
        assert not monitor._is_empty("בקשה בטיפול")


# ──────────────────────────────────────────────────────────────────────────────
# _moed_rank
# ──────────────────────────────────────────────────────────────────────────────

class TestMoedRank:
    def test_bet_rank(self):
        assert monitor._moed_rank("מועד ב'") == 2

    def test_aleph_rank(self):
        assert monitor._moed_rank("מועד א'") == 1

    def test_unknown_rank(self):
        assert monitor._moed_rank("—") == 0

    def test_empty_rank(self):
        assert monitor._moed_rank("") == 0

    def test_bet_beats_aleph(self):
        assert monitor._moed_rank("מועד ב'") > monitor._moed_rank("מועד א'")


# ──────────────────────────────────────────────────────────────────────────────
# _appeal_approved
# ──────────────────────────────────────────────────────────────────────────────

class TestAppealApproved:
    def test_approved_grade_increase(self):
        assert monitor._appeal_approved("אושר", "70", "80")

    def test_rejected_by_keyword_נדחה(self):
        assert not monitor._appeal_approved("נדחה", "70", "70")

    def test_rejected_by_keyword_לא_אושר(self):
        assert not monitor._appeal_approved("לא אושר", "70", "70")

    def test_rejected_by_keyword_נשמר(self):
        assert not monitor._appeal_approved("נשמר", "70", "70")

    def test_approved_no_grade_change(self):
        # Status changed away from בקשה בטיפול but no rejection keyword → approved
        assert monitor._appeal_approved("בוצע", "70", "70")

    def test_grade_zero_no_increase(self):
        # Same grade → still treated as approved if no rejection keyword
        assert monitor._appeal_approved("סיום", "0", "0")

    def test_bad_grade_values_no_crash(self):
        # Non-numeric grades must not raise
        assert monitor._appeal_approved("סיום", "—", "—")


# ──────────────────────────────────────────────────────────────────────────────
# _detect_columns
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectColumns:
    FULL_HEADERS = [
        "קוד קבוצת קורס",
        "שם קבוצת קורס",
        "שם המרצה",
        "מועד",
        "תאריך בחינה",
        "ציון",
        "ציון סופי",
        "סטטוס ערעור",
    ]

    def test_full_header_detection(self):
        col = monitor._detect_columns(self.FULL_HEADERS)
        assert col["course_code"] == 0
        assert col["course_name"] == 1
        assert col["lecturer"] == 2
        assert col["moed"] == 3
        assert col["date"] == 4
        assert col["grade"] == 5
        assert col["final_grade"] == 6
        assert col["appeal"] == 7

    def test_grade_vs_final_grade_disambiguation(self):
        # "ציון סופי" must NOT be picked as the plain grade column
        col = monitor._detect_columns(["ציון", "ציון סופי"])
        assert col["grade"] == 0
        assert col["final_grade"] == 1

    def test_minimal_headers(self):
        col = monitor._detect_columns(["שם קבוצת קורס", "ציון"])
        assert col["course_name"] == 0
        assert col["grade"] == 1
        assert col["final_grade"] == -1

    def test_empty_headers(self):
        col = monitor._detect_columns([])
        for v in col.values():
            assert v == -1

    def test_fallback_course_name(self):
        # Broad fallback: "שם קורס" should match the course_name fallback
        col = monitor._detect_columns(["שם קורס"])
        assert col["course_name"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# diff_and_alert
# ──────────────────────────────────────────────────────────────────────────────

class TestDiffAndAlert:
    def _run(self, old, new):
        sent = []
        with patch.object(monitor, "broadcast_alert", side_effect=sent.append):
            monitor.diff_and_alert(old, new)
        return sent

    def test_new_grade_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(grade="—")}
        new = {"88-101|א'|01/10/2024": _make_info(grade="85")}
        msgs = self._run(old, new)
        assert len(msgs) == 1
        assert "85" in msgs[0]
        assert "מתמטיקה" in msgs[0]

    def test_existing_unchanged_no_alert(self):
        info = _make_info(grade="85")
        key = "88-101|א'|01/10/2024"
        msgs = self._run({key: info}, {key: info})
        assert msgs == []

    def test_final_grade_appears_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(grade="85", final_grade="—")}
        new = {"88-101|א'|01/10/2024": _make_info(grade="85", final_grade="87")}
        msgs = self._run(old, new)
        assert len(msgs) == 1
        assert "ציון סופי" in msgs[0]
        assert "87" in msgs[0]

    def test_appeal_approved_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(
            grade="70", appeal_status=monitor.APPEAL_IN_PROGRESS
        )}
        new = {"88-101|א'|01/10/2024": _make_info(
            grade="80", appeal_status="אושר"
        )}
        msgs = self._run(old, new)
        assert any("אישר" in m for m in msgs)

    def test_appeal_approved_includes_old_and_new_grade(self):
        # Real Inbar wording: "בקשתך אושרה". Approval with grade increase 94→96.
        old = {"83216-01|ב'|26/04/2026": _make_info(
            grade="94", appeal_status=monitor.APPEAL_IN_PROGRESS
        )}
        new = {"83216-01|ב'|26/04/2026": _make_info(
            grade="96", appeal_status="בקשתך אושרה"
        )}
        msgs = self._run(old, new)
        assert len(msgs) == 1
        assert "אישר" in msgs[0]
        assert "96" in msgs[0]
        assert "94" in msgs[0]

    def test_appeal_approved_grade_unchanged(self):
        # Grader accepts but keeps same grade
        old = {"88-101|א'|01/10/2024": _make_info(
            grade="85", appeal_status=monitor.APPEAL_IN_PROGRESS
        )}
        new = {"88-101|א'|01/10/2024": _make_info(
            grade="85", appeal_status="בקשתך אושרה"
        )}
        msgs = self._run(old, new)
        assert "אישר" in msgs[0]
        assert "נשאר" in msgs[0]

    def test_appeal_rejected_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(
            grade="70", appeal_status=monitor.APPEAL_IN_PROGRESS
        )}
        new = {"88-101|א'|01/10/2024": _make_info(
            grade="70", appeal_status="נדחה"
        )}
        msgs = self._run(old, new)
        assert any("לא אישר" in m for m in msgs)

    def test_appeal_rejected_real_wording(self):
        # Real Inbar wording from screenshot: "בקשתך נדחתה"
        old = {"83216-01|ב'|26/04/2026": _make_info(
            grade="94", appeal_status=monitor.APPEAL_IN_PROGRESS
        )}
        new = {"83216-01|ב'|26/04/2026": _make_info(
            grade="94", appeal_status="בקשתך נדחתה"
        )}
        msgs = self._run(old, new)
        assert len(msgs) == 1
        assert "לא אישר" in msgs[0]
        assert "94" in msgs[0]

    def test_first_run_no_alert(self):
        # Simulated by passing empty old
        new = {"88-101|א'|01/10/2024": _make_info(grade="85")}
        # The main() function handles this; diff_and_alert itself fires if called.
        # Here we verify that passing empty old with a graded new would fire —
        # confirming the first-run guard in main() is what suppresses it.
        msgs = self._run({}, new)
        assert len(msgs) == 1  # diff_and_alert fires; main() must skip calling it

    def test_fail_grade_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(grade="—")}
        new = {"88-101|א'|01/10/2024": _make_info(grade="45")}
        msgs = self._run(old, new)
        assert len(msgs) == 1
        assert "45" in msgs[0]

    def test_grade_zero_fires_alert(self):
        old = {"88-101|א'|01/10/2024": _make_info(grade="—")}
        new = {"88-101|א'|01/10/2024": _make_info(grade="0")}
        msgs = self._run(old, new)
        assert len(msgs) == 1

    def test_multiple_new_courses(self):
        old = {}
        new = {
            "88-101|א'|01/10/2024": _make_info(course_name="מתמטיקה", grade="85"),
            "88-202|א'|01/10/2024": _make_info(
                course_name="פיזיקה", course_code="88-202", grade="90"
            ),
        }
        msgs = self._run(old, new)
        assert len(msgs) == 2

    def test_appeal_not_in_progress_no_spurious_alert(self):
        # appeal was never in progress — status change must not fire
        old = {"88-101|א'|01/10/2024": _make_info(appeal_status="—")}
        new = {"88-101|א'|01/10/2024": _make_info(appeal_status="אושר")}
        msgs = self._run(old, new)
        assert msgs == []

    def test_both_old_and_new_empty(self):
        msgs = self._run({}, {})
        assert msgs == []


# ──────────────────────────────────────────────────────────────────────────────
# _build_final_grades_message
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildFinalGradesMessage:
    def _msg(self, data: dict) -> str:
        with patch.object(monitor, "load_saved", return_value=data):
            return monitor._build_final_grades_message()

    def test_no_data(self):
        msg = self._msg({})
        assert "אין נתונים" in msg

    def test_single_course_with_final_grade(self):
        data = {"88-101|א'|01/10/2024": _make_info(grade="85", final_grade="87")}
        msg = self._msg(data)
        assert "מתמטיקה" in msg
        assert "87" in msg

    def test_single_course_no_final_grade(self):
        data = {"88-101|א'|01/10/2024": _make_info(grade="85")}
        msg = self._msg(data)
        assert "טרם פורסם" in msg

    def test_moed_bet_beats_aleph(self):
        # Both have final_grade so moed rank is the tiebreaker
        data = {
            "88-101|א'|01/10/2024": _make_info(moed="א'", grade="70", final_grade="70"),
            "88-101|ב'|01/11/2024": _make_info(moed="ב'", grade="85", final_grade="85"),
        }
        msg = self._msg(data)
        assert "85" in msg
        assert "70" not in msg

    def test_final_grade_beats_moed(self):
        # א' with ציון סופי should beat ב' without one
        data = {
            "88-101|א'|01/10/2024": _make_info(moed="א'", grade="70", final_grade="72"),
            "88-101|ב'|01/11/2024": _make_info(moed="ב'", grade="85"),
        }
        msg = self._msg(data)
        assert "72" in msg

    def test_later_date_beats_earlier_same_moed(self):
        # Both have final_grade; same moed → date is the tiebreaker
        data = {
            "88-101|א'|01/10/2023": _make_info(moed="א'", grade="65", final_grade="65", date="01/10/2023"),
            "88-101|א'|01/10/2024": _make_info(moed="א'", grade="80", final_grade="80", date="01/10/2024"),
        }
        msg = self._msg(data)
        assert "80" in msg
        assert "65" not in msg

    def test_multiple_courses_sorted_alphabetically(self):
        data = {
            "101|א'|01/10/2024": _make_info(course_name="פיזיקה", grade="90", final_grade="90"),
            "102|א'|01/10/2024": _make_info(course_name="מתמטיקה", course_code="102", grade="85", final_grade="85"),
        }
        msg = self._msg(data)
        idx_math = msg.index("מתמטיקה")
        idx_phys = msg.index("פיזיקה")
        assert idx_math < idx_phys  # Hebrew alpha: מ before פ

    def test_grade_zero_shown(self):
        data = {"88-101|א'|01/10/2024": _make_info(grade="0", final_grade="0")}
        msg = self._msg(data)
        assert "0" in msg

    def test_grade_100_shown(self):
        data = {"88-101|א'|01/10/2024": _make_info(grade="100", final_grade="100")}
        msg = self._msg(data)
        assert "100" in msg

    def test_grouped_by_year_newest_first(self):
        data = {
            "A|א'|10/02/2025": _make_info(
                course_name="קורס א", course_code="A",
                date="10/02/2025", final_grade="85"
            ),
            "B|א'|10/02/2026": _make_info(
                course_name="קורס ב", course_code="B",
                date="10/02/2026", final_grade="92"
            ),
        }
        msg = self._msg(data)
        # Newest year (2025-2026) appears before older (2024-2025)
        assert msg.index("2025-2026") < msg.index("2024-2025")

    def test_grouped_by_semester_within_year(self):
        # Same academic year (2025-2026): סמסטר א' (Feb) before סמסטר ב' (June)
        data = {
            "A|א'|10/02/2026": _make_info(
                course_name="קורס א", course_code="A",
                date="10/02/2026", final_grade="80"
            ),
            "B|א'|10/06/2026": _make_info(
                course_name="קורס ב", course_code="B",
                date="10/06/2026", final_grade="90"
            ),
        }
        msg = self._msg(data)
        # Single year, two semesters — סמסטר א' before סמסטר ב'
        idx_a = msg.find("סמסטר א'")
        idx_b = msg.find("סמסטר ב'")
        assert idx_a != -1 and idx_b != -1
        assert idx_a < idx_b

    def test_academic_year_boundary_oct(self):
        # Oct exam belongs to NEXT academic year (תשפ"ו = 2025-2026 for Oct 2025)
        data = {
            "X|א'|15/10/2025": _make_info(
                course_name="קורס X", course_code="X",
                date="15/10/2025", final_grade="77"
            ),
        }
        msg = self._msg(data)
        assert "2025-2026" in msg

    def test_academic_year_boundary_sep(self):
        # Sep exam belongs to current academic year ending that Gregorian year
        data = {
            "Y|ב'|15/09/2025": _make_info(
                course_name="קורס Y", course_code="Y",
                date="15/09/2025", final_grade="88"
            ),
        }
        msg = self._msg(data)
        assert "2024-2025" in msg


# ──────────────────────────────────────────────────────────────────────────────
# Year / semester helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestAcademicYear:
    def test_oct_starts_new_year(self):
        assert monitor._academic_year("01/10/2025")[1] == "2025-2026"

    def test_sep_ends_current_year(self):
        assert monitor._academic_year("30/09/2026")[1] == "2025-2026"

    def test_jan_is_current_year(self):
        assert monitor._academic_year("15/01/2026")[1] == "2025-2026"

    def test_bad_date_returns_dash(self):
        assert monitor._academic_year("")[1] == "—"
        assert monitor._academic_year("not-a-date")[1] == "—"

    def test_sort_key_is_end_year(self):
        assert monitor._academic_year("15/01/2026")[0] == 2026
        assert monitor._academic_year("15/10/2025")[0] == 2026


class TestSemester:
    def test_jan_is_aleph(self):
        assert monitor._semester("15/01/2026")[1] == "א'"

    def test_apr_is_aleph(self):
        assert monitor._semester("15/04/2026")[1] == "א'"

    def test_may_is_bet(self):
        assert monitor._semester("15/05/2026")[1] == "ב'"

    def test_jul_is_bet(self):
        assert monitor._semester("15/07/2026")[1] == "ב'"

    def test_oct_is_aleph(self):
        assert monitor._semester("15/10/2025")[1] == "א'"

    def test_bad_date(self):
        assert monitor._semester("")[1] == "?"


# ──────────────────────────────────────────────────────────────────────────────
# Persistent dedup set (anti-flood on reconnect / reboot)
# ──────────────────────────────────────────────────────────────────────────────

def test_ever_alerted_roundtrip_and_lock(tmp_path):
    """save_ever_alerted persists JSON and locks the file to owner-only."""
    target = tmp_path / ".ever_alerted.json"
    with patch.object(monitor, "EVER_ALERTED_FILE", target):
        monitor.save_ever_alerted({"מתמטיקה|א|01/06|grade|95": 1})
        assert monitor.load_ever_alerted() == {"מתמטיקה|א|01/06|grade|95": 1}
        assert (target.stat().st_mode & 0o777) == 0o600


def test_load_ever_alerted_missing_and_corrupt(tmp_path):
    target = tmp_path / ".ever_alerted.json"
    with patch.object(monitor, "EVER_ALERTED_FILE", target):
        assert monitor.load_ever_alerted() == {}          # missing → empty
        target.write_text("{not json", encoding="utf-8")
        assert monitor.load_ever_alerted() == {}          # corrupt → empty, no raise


def test_reconnect_with_lost_snapshot_does_not_reflood():
    """The core guarantee: a full scrape after data.json loss re-sends nothing."""
    from inbar import diff as _diff
    grades = {
        "מתמטיקה|א|01/06/2027": _make_info(course_name="מתמטיקה", grade="95", final_grade="95"),
        "פיזיקה|ב|10/06/2027": _make_info(course_name="פיזיקה", grade="88", final_grade="88"),
    }
    ever = _diff.baseline_dedup_ids(grades)
    # old={} simulates a lost/partial snapshot; every grade looks "new"
    raw = _diff.compute_alerts({}, grades)
    to_send, _ = _diff.filter_alerts(raw, ever)
    assert raw, "sanity: raw diff wants to alert"
    assert to_send == [], "already-seen grades must be suppressed after reconnect"
