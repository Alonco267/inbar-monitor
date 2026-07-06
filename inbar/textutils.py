"""Pure text/date helpers shared by every monitoring path. No I/O here."""

from __future__ import annotations

import re

APPEAL_IN_PROGRESS = "בקשה בטיפול"

REJECTION_KEYWORDS = [
    "נדחה", "נדחית", "נדחתה",            # rejected (masc/fem variants)
    "לא אושר", "לא אושרה",
    "לא התקבל", "לא התקבלה",
    "נשמר", "אושר הציון המקורי",
]

LOGIN_KEYWORDS = ["login", "signin", "logon", "shibboleth", "adfs", "auth",
                  "idp", "wayf", "saml"]

_STRIP_RE = re.compile(r"[\s ‎‏]+")


def is_empty(val: str | None) -> bool:
    """True for missing/placeholder cell values (—, -, נ/א, whitespace/nbsp)."""
    if not val:
        return True
    return _STRIP_RE.sub("", val) in ("", "—", "-", "נ/א")


def is_login_page(url: str) -> bool:
    return any(kw in url.lower() for kw in LOGIN_KEYWORDS)


def is_on_grades_page(url: str) -> bool:
    return "inbar.biu.ac.il" in url.lower() and not is_login_page(url)


def appeal_approved(new_status: str, old_grade: str, new_grade: str) -> bool:
    """Heuristic: an appeal resolution is approved unless a rejection keyword appears."""
    if any(kw in new_status for kw in REJECTION_KEYWORDS):
        return False
    try:
        if float(new_grade) > float(old_grade):
            return True
    except (ValueError, TypeError):
        pass
    return True


def moed_rank(moed: str) -> int:
    """Higher = more final. מועד ב' > מועד א'."""
    if "ב'" in moed:
        return 2
    if "א'" in moed:
        return 1
    return 0


def parse_date(date_str: str) -> tuple[int, int, int] | None:
    """Return (day, month, year) from DD/MM/YYYY, or None on bad input."""
    try:
        parts = (date_str or "").split("/")
        if len(parts) != 3:
            return None
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, AttributeError):
        return None


def academic_year(date_str: str) -> tuple[int, str]:
    """Bar-Ilan academic year (Oct→Sep). Returns (sort_key, label like '2025-2026')."""
    parsed = parse_date(date_str)
    if not parsed:
        return (0, "—")
    _, month, year = parsed
    end_year = year if month <= 9 else year + 1
    return (end_year, f"{end_year - 1}-{end_year}")


def semester(date_str: str) -> tuple[int, str]:
    """Derive semester from exam date. Returns (sort_key, Hebrew label)."""
    parsed = parse_date(date_str)
    if not parsed:
        return (9, "?")
    _, month, _ = parsed
    # Winter exams (סמסטר א'): Jan–Apr, Oct–Dec. Spring/summer (ב'): May–Sep.
    if month in (1, 2, 3, 4, 10, 11, 12):
        return (1, "א'")
    return (2, "ב'")
