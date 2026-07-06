"""Shared constants and logging setup for the Inbar monitor."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

TARGET_URL = "https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx"

# The "Average Grades" page. The default is Inbar's known averages page name;
# override with INBAR_AVERAGES_URL if the deployment differs. extract_gpa()
# also falls back to following a menu link whose text contains "ממוצע".
DEFAULT_AVERAGES_URL = "https://inbar.biu.ac.il/Live/StudentGradesAverage.aspx"

BASE_DIR = Path(__file__).resolve().parent.parent

CONTEXT_OPTS = dict(
    locale="he-IL",
    timezone_id="Asia/Jerusalem",
    viewport={"width": 1280, "height": 900},
    user_agent=(
        "Mozilla/5.0 (Macintosh; ARM Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
)

GRADES_TABLE_SEL = "#ContentPlaceHolder1_gvStudentAssignmentTermList"
FILTER_DDL_SEL = "#tbMain_ctl03_ddlExamDateRangeFilter"
YEAR_DDL_SEL = "#cmbActiveYear"


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent stdout logging config shared by all entry points."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level)
