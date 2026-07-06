"""Telegram message builders. Pure functions over grade snapshots."""

from __future__ import annotations

from collections import defaultdict

from .textutils import academic_year, is_empty, moed_rank, semester


def _reverse_date(d: str) -> str:
    """DD/MM/YYYY → YYYY/MM/DD so string comparison orders chronologically."""
    p = d.split("/")
    return "/".join(reversed(p)) if len(p) == 3 else d


def pick_latest_per_course(data: dict) -> dict:
    """One entry per unique course name — latest, most final grade wins.

    Priority: has ציון סופי > later מועד (ב'>א') > later date.
    """
    by_course: dict = {}
    for info in data.values():
        name = info.get("course_name", "?")
        if name not in by_course:
            by_course[name] = info
            continue

        ex = by_course[name]
        has_fg = not is_empty(info.get("final_grade", "—"))
        ex_has_fg = not is_empty(ex.get("final_grade", "—"))

        if has_fg and not ex_has_fg:
            by_course[name] = info
            continue
        if not has_fg and ex_has_fg:
            continue

        rank, ex_rank = moed_rank(info.get("moed", "")), moed_rank(ex.get("moed", ""))
        if rank > ex_rank:
            by_course[name] = info
            continue
        if rank < ex_rank:
            continue

        if _reverse_date(info.get("date", "")) > _reverse_date(ex.get("date", "")):
            by_course[name] = info
    return by_course


def build_final_grades_message(data: dict) -> str:
    """Grouped final-grades summary: newest academic year first, semester א' before ב'."""
    if not data:
        return "אין נתונים שמורים עדיין. הרץ את הסקריפט לפחות פעם אחת."

    by_course = pick_latest_per_course(data)

    groups: dict = defaultdict(list)
    for name in sorted(by_course):
        info = by_course[name]
        date = info.get("date", "")
        year_key, year_label = academic_year(date)
        sem_key, sem_label = semester(date)
        groups[(year_key, sem_key, year_label, sem_label)].append((name, info))

    lines = ["📊 ציונים סופיים:"]
    has_any = False
    for group_key in sorted(groups.keys(), key=lambda k: (-k[0], k[1])):
        _, _, year_label, sem_label = group_key
        lines.append(f"\n📅 {year_label}  •  סמסטר {sem_label}")
        for name, info in groups[group_key]:
            fg = info.get("final_grade", "—")
            if not is_empty(fg):
                lines.append(f"• {name}: {fg}")
                has_any = True
            else:
                lines.append(f"• {name}: טרם פורסם")

    if not has_any:
        lines.append("\nטרם פורסמו ציונים סופיים.")
    return "\n".join(lines)
