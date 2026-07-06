"""Playwright extraction: grades table (all years, all pages) and official GPA."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import (BASE_DIR, DEFAULT_AVERAGES_URL, FILTER_DDL_SEL,
                     GRADES_TABLE_SEL, YEAR_DDL_SEL)

log = logging.getLogger("inbar.scrape")

DEBUG_HTML_FILE = BASE_DIR / "debug_page.html"


def detect_columns(headers: list[str]) -> dict:
    """Map column roles to indices from the GridView header row."""
    col = {k: -1 for k in ("course_code", "course_name", "lecturer", "moed",
                           "grade", "final_grade", "appeal", "date")}
    for i, h in enumerate(headers):
        h = h.strip()
        if "ציון סופי" in h and col["final_grade"] == -1:
            col["final_grade"] = i
        elif h == "ציון" and col["grade"] == -1:
            col["grade"] = i
        elif "שם קבוצת קורס" in h and col["course_name"] == -1:
            col["course_name"] = i
        elif "קוד קבוצת קורס" in h and col["course_code"] == -1:
            col["course_code"] = i
        elif "שם המרצה" in h and col["lecturer"] == -1:
            col["lecturer"] = i
        elif h == "מועד" and col["moed"] == -1:
            col["moed"] = i
        elif "תאריך" in h and col["date"] == -1:
            col["date"] = i
        elif ("ערעור" in h) and col["appeal"] == -1:
            col["appeal"] = i
    # Broad fallbacks
    if col["course_name"] == -1:
        for i, h in enumerate(headers):
            if any(kw in h for kw in ["קורס", "מקצוע"]) and col["course_name"] == -1:
                col["course_name"] = i
    if col["grade"] == -1:
        for i, h in enumerate(headers):
            if "ציון" in h and i != col["final_grade"] and col["grade"] == -1:
                col["grade"] = i
    return col


async def _parse_rows(rows, col: dict) -> dict:
    """Extract grade entries from a list of <tr> elements."""
    grades = {}
    for row in rows[1:]:  # skip header row
        cells = await row.query_selector_all("td")
        if not cells:
            continue
        texts = [(await c.inner_text()).strip() for c in cells]
        if not any(texts):
            continue

        def _get(idx: int) -> str:
            return texts[idx].strip() if 0 <= idx < len(texts) else "—"

        course_name = _get(col["course_name"])
        if not course_name or course_name == "—":
            continue

        course_code = _get(col["course_code"])
        moed = _get(col["moed"])
        date = _get(col["date"])
        id_part = course_code if (course_code and course_code != "—") else course_name
        # Include date in key so retaken courses in different years don't collide
        key = f"{id_part}|{moed}|{date}"

        grades[key] = {
            "course_name": course_name,
            "course_code": course_code,
            "lecturer": _get(col["lecturer"]),
            "moed": moed,
            "date": date,
            "grade": _get(col["grade"]),
            "final_grade": _get(col["final_grade"]),
            "appeal_status": _get(col["appeal"]),
        }
    return grades


async def _save_debug_html(page, path: Path = DEBUG_HTML_FILE) -> None:
    try:
        path.write_text(await page.content(), encoding="utf-8")
        log.info("debug HTML saved → %s", path.name)
    except Exception:
        pass


async def extract_grades(page) -> dict:
    """Scrape ALL grades across ALL academic years and ALL GridView pages.

    Strategy:
      1. Read the year dropdown options.
      2. For each year: select it, set filter to 'הכל', scrape every GridView page.
      3. Merge into one dict keyed by ``{course_code}|{moed}|{date}``.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        pass

    try:
        await page.wait_for_selector(YEAR_DDL_SEL, timeout=20_000)
        year_opts = await page.query_selector_all(f"{YEAR_DDL_SEL} option")
        year_values = [await o.get_attribute("value") for o in year_opts]
    except PlaywrightTimeoutError:
        year_values = []
        await _save_debug_html(page)
        log.warning("year dropdown not found; page URL: %s", page.url)

    if not year_values:
        log.info("scraping current page only (no year dropdown)")
        year_values = ["__current__"]

    all_grades: dict = {}
    col: dict | None = None

    for year in year_values:
        if year != "__current__":
            log.info("selecting year %s", year)
            try:
                await page.select_option(YEAR_DDL_SEL, value=year)
                await page.wait_for_load_state("networkidle", timeout=12_000)
                await page.wait_for_timeout(800)
            except Exception as e:
                log.warning("year %s select failed: %s", year, e)
                continue

        # Set filter to "הכל" (all exams, including past with grades)
        try:
            await page.wait_for_selector(FILTER_DDL_SEL, timeout=5_000)
            await page.select_option(FILTER_DDL_SEL, value="1")
            await page.wait_for_load_state("networkidle", timeout=12_000)
            await page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            pass

        grid_page = 0
        while True:
            grid_page += 1
            try:
                await page.wait_for_selector(GRADES_TABLE_SEL, timeout=10_000)
            except PlaywrightTimeoutError:
                log.warning("year %s: table not found on page %d", year, grid_page)
                break

            rows = await page.query_selector_all(f"{GRADES_TABLE_SEL} tr")
            if len(rows) < 2:
                break

            if col is None:
                header_cells = await rows[0].query_selector_all("th, td")
                headers = [(await c.inner_text()).strip() for c in header_cells]
                col = detect_columns(headers)
                log.info("column map: %s", col)
                if col["course_name"] == -1:
                    log.error("course column not found; headers: %s", headers)
                    await _save_debug_html(page)
                    return {}

            page_grades = await _parse_rows(rows, col)
            log.info("year %s, page %d: %d row(s)", year, grid_page, len(page_grades))
            all_grades.update(page_grades)

            # GridView pager: look for a "next page" link (">")
            next_link = None
            pager_links = await page.query_selector_all(
                f"{GRADES_TABLE_SEL} tr.GridPager td a")
            for link in pager_links:
                txt = (await link.inner_text()).strip()
                if txt in (">", ">>", "הבא"):
                    next_link = link
                    break
            if not next_link:
                break
            await next_link.click()
            await page.wait_for_load_state("networkidle", timeout=12_000)
            await page.wait_for_timeout(500)

    return all_grades


# ─── Official GPA / averages page ─────────────────────────────────────────────

async def extract_gpa(page) -> str | None:
    """Read the official averages from Inbar's "Average Grades" page.

    Returns a ready-to-send Hebrew text block, or None if the page could not
    be reached or parsed. Never raises — a GPA failure must not fail the run.

    Discovery order:
      1. INBAR_AVERAGES_URL env var (explicit override)
      2. the default averages URL
      3. a nav link on the current page whose text contains "ממוצע"
    """
    try:
        # Fallback link is resolved from the page we're on now, so grab it
        # BEFORE navigating away to URL candidates.
        fallback_href = None
        link = await page.query_selector('a:has-text("ממוצע")')
        if link:
            fallback_href = await link.get_attribute("href")

        candidates = []
        override = os.environ.get("INBAR_AVERAGES_URL", "").strip()
        if override:
            candidates.append(override)
        if fallback_href:
            candidates.append(urljoin(page.url, fallback_href))
        candidates.append(DEFAULT_AVERAGES_URL)

        for url in candidates:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            except Exception as e:
                log.info("averages URL %s failed: %s", url, e)
                continue
            # The averages table renders via an AJAX postback after the initial
            # page load — wait for its known label instead of a fixed delay.
            try:
                await page.wait_for_selector('text=ממוצע כללי', timeout=10_000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(2_000)
            text = await _read_averages_text(page)
            if text:
                return text

        log.info("no averages page found")
        return None
    except Exception as e:
        log.warning("GPA extraction failed: %s", e)
        return None


async def _read_averages_text(page) -> str | None:
    """Pull average labels+values out of the current page.

    The averages page (StudentAverage.aspx) renders each average as a label
    cell followed by a numeric value cell, e.g.:

        <td><span id="…lblTotalAvg">ממוצע כללי :</span></td>
        <td><span id="…lblTotalAvgVal">85.71</span></td>

    The label and value live in *sibling <td>s* (verified live 2026-07-06),
    so we pair each short label cell mentioning "ממוצע" with the next numeric
    cell. Cells that already contain both label and number are kept as-is
    for layout resilience.
    """
    try:
        lines: list[str] = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const push = (line) => {
                    if (line && !seen.has(line)) { seen.add(line); out.push(line); }
                };
                const clean = (el) =>
                    ((el && el.innerText) || '').trim().replace(/\\s+/g, ' ');
                for (const cell of document.querySelectorAll('td, th')) {
                    const label = clean(cell);
                    if (!label.includes('ממוצע') || label.length > 60) continue;
                    if (/\\d/.test(label)) { push(label); continue; }
                    const val = clean(cell.nextElementSibling);
                    if (/^\\d+(\\.\\d+)?$/.test(val)) {
                        push(label.replace(/\\s*:\\s*$/, '') + ': ' + val);
                    }
                }
                return out;
            }"""
        )
    except Exception as e:
        log.info("averages parse failed: %s", e)
        return None

    if not lines:
        return None
    body = "\n".join(f"• {line}" for line in lines[:10])
    return f"🎓 הממוצע הרשמי מאינ-בר:\n{body}"
