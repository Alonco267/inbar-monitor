"""Change-detection engine.

Compares two grade snapshots (dicts keyed by ``{course}|{moed}|{date}``) and
produces a list of :class:`Alert` objects. Pure functions — no I/O, no globals —
so every transition is unit-testable.

Detected events:
    grade_new       — grade appeared (empty → value)
    grade_changed   — grade changed value (either direction)
    final_new       — final grade appeared
    final_changed   — final grade changed value
    appeal_filed    — appeal entered "בקשה בטיפול"
    appeal_resolved — appeal left "בקשה בטיפול" (with approve/reject verdict)
    appeal_changed  — any other appeal-status transition between non-empty states
    row_new         — a brand-new exam row appeared with no grade yet

Each alert carries a ``dedup_id`` that encodes the *value* that triggered it,
so a persistent ever-alerted set guarantees exactly-once delivery even when
the previous-snapshot state is lost between runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .textutils import APPEAL_IN_PROGRESS, appeal_approved, is_empty


@dataclass(frozen=True)
class Alert:
    kind: str
    key: str
    dedup_id: str
    message: str


def _grade_alerts(key: str, old_info: dict, info: dict) -> list[Alert]:
    course = info.get("course_name", "?")
    moed = info.get("moed", "?")
    alerts: list[Alert] = []

    for field in ("grade", "final_grade"):
        new_val = info.get(field, "—")
        old_val = old_info.get(field, "—")
        kind_prefix = "grade" if field == "grade" else "final"
        if is_empty(new_val):
            continue
        if is_empty(old_val):
            if field == "grade":
                message = (
                    f"אל תילחץ אחי אבל עלה ציון ב{course} {moed} - "
                    f"ציונך הוא {new_val} מקווה שאתה מרוצה 🎓"
                )
            else:
                message = (
                    f"אל תילחץ אחי אבל עלה ציון סופי ב{course} - "
                    f"הציון הסופי שלך הוא {new_val} מקווה שאתה מרוצה 🎓"
                )
            alerts.append(Alert(
                kind=f"{kind_prefix}_new",
                key=key,
                dedup_id=f"{key}|{field}|{new_val}",
                message=message,
            ))
        elif new_val != old_val:
            try:
                arrow = "📈" if float(new_val) > float(old_val) else "📉"
            except (ValueError, TypeError):
                arrow = "🔄"
            label = "הציון" if field == "grade" else "הציון הסופי"
            alerts.append(Alert(
                kind=f"{kind_prefix}_changed",
                key=key,
                dedup_id=f"{key}|{field}|{new_val}",
                message=(
                    f"שים לב אחי, {label} ב{course} {moed} עודכן: "
                    f"{old_val} ← {new_val} {arrow}"
                ),
            ))
    return alerts


def _appeal_alerts(key: str, old_info: dict, info: dict) -> list[Alert]:
    course = info.get("course_name", "?")
    lecturer = info.get("lecturer", "?")
    new_ap = info.get("appeal_status", "—")
    old_ap = old_info.get("appeal_status", "—")
    old_grade = old_info.get("grade", "—")
    new_grade = info.get("grade", "—")

    if new_ap == old_ap:
        return []

    # Appeal filed: status became "in progress" (from anything, incl. empty).
    if new_ap == APPEAL_IN_PROGRESS:
        return [Alert(
            kind="appeal_filed",
            key=key,
            dedup_id=f"{key}|appeal|{new_ap}",
            message=f"הערעור שלך בקורס {course} נקלט ונמצא בטיפול ⚖️",
        )]

    # Appeal resolved: left "in progress" → verdict message.
    if old_ap == APPEAL_IN_PROGRESS:
        approved = appeal_approved(new_ap, old_grade, new_grade)
        if approved:
            verdict = "אישר"
            if (not is_empty(new_grade) and new_grade != old_grade
                    and not is_empty(old_grade)):
                tail = f"\nציון חדש: {new_grade} (היה {old_grade})"
            elif not is_empty(new_grade):
                tail = f"\nהציון נשאר {new_grade}"
            else:
                tail = ""
        else:
            verdict = "לא אישר"
            tail = f"\nהציון נשאר {old_grade}" if not is_empty(old_grade) else ""
        return [Alert(
            kind="appeal_resolved",
            key=key,
            dedup_id=f"{key}|appeal|{new_ap}",
            message=f"היי אחי המרצה {lecturer} {verdict} את הערעור שלך בקורס {course} ⚖️{tail}",
        )]

    # Any other transition between two NON-empty statuses is still worth knowing.
    # empty → resolved-status is deliberately silent: it usually means our
    # baseline predates the appeal, and alerting would be noise.
    if not is_empty(old_ap) and not is_empty(new_ap):
        return [Alert(
            kind="appeal_changed",
            key=key,
            dedup_id=f"{key}|appeal|{new_ap}",
            message=f"סטטוס הערעור בקורס {course} עודכן: {old_ap} ← {new_ap} ⚖️",
        )]
    return []


def compute_alerts(old: dict, new: dict) -> list[Alert]:
    """All alert-worthy transitions between two snapshots."""
    alerts: list[Alert] = []
    for key, info in new.items():
        old_info = old.get(key, {})

        grade_alerts = _grade_alerts(key, old_info, info)
        appeal_alerts = _appeal_alerts(key, old_info, info)

        # When an appeal resolution already reports the grade movement,
        # a separate "grade changed" alert would be duplicate noise.
        if any(a.kind == "appeal_resolved" for a in appeal_alerts):
            grade_alerts = [a for a in grade_alerts
                            if a.kind not in ("grade_changed", "final_changed")]

        alerts.extend(grade_alerts)
        alerts.extend(appeal_alerts)

        # Brand-new ungraded exam row (e.g. registered for מועד ב').
        # Only meaningful when we had a real previous snapshot.
        if (old and key not in old and not grade_alerts and not appeal_alerts
                and is_empty(info.get("grade", "—"))
                and is_empty(info.get("final_grade", "—"))):
            course = info.get("course_name", "?")
            moed = info.get("moed", "?")
            date = info.get("date", "?")
            alerts.append(Alert(
                kind="row_new",
                key=key,
                dedup_id=f"{key}|row",
                message=f"נוסף מועד חדש ללוח הבחינות: {course} {moed} ({date}) 📅",
            ))
    return alerts


def filter_alerts(alerts: list[Alert], ever_alerted: dict) -> tuple[list[Alert], dict]:
    """Drop alerts whose dedup_id was already delivered; return the updated set.

    ``ever_alerted`` maps dedup_id → 1 (a dict for JSON friendliness).
    A new dict is returned — the input is never mutated.
    """
    updated = dict(ever_alerted)
    to_send: list[Alert] = []
    for alert in alerts:
        if alert.dedup_id in updated:
            continue
        updated[alert.dedup_id] = 1
        to_send.append(alert)
    return to_send, updated


def baseline_dedup_ids(grades: dict) -> dict:
    """Seed dedup ids for every value already present in a snapshot.

    Used on first run so existing grades never fire alerts, even if the
    previous-snapshot state is later lost.
    """
    ids: dict = {}
    for key, info in grades.items():
        for field in ("grade", "final_grade"):
            val = info.get(field, "—")
            if not is_empty(val):
                ids[f"{key}|{field}|{val}"] = 1
        ap = info.get("appeal_status", "—")
        if not is_empty(ap):
            ids[f"{key}|appeal|{ap}"] = 1
        ids[f"{key}|row"] = 1
    return ids


def migrate_ever_seen(ever_seen: dict) -> dict:
    """Convert legacy ``ever_seen`` state ({key: {grade, final_grade}}) to dedup ids."""
    ids: dict = {}
    for key, seen in ever_seen.items():
        for field in ("grade", "final_grade"):
            val = (seen or {}).get(field, "—")
            if not is_empty(val):
                ids[f"{key}|{field}|{val}"] = 1
        ids[f"{key}|row"] = 1
    return ids
