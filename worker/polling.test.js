import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Pull the pure helper functions out of the userscript source so tests
// run against the SAME code that ships to users.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(
  path.join(__dirname, '..', 'userscript', 'inbar-monitor.user.js'),
  'utf8'
);

function extractFn(name) {
  // Use [^{]* to skip params (handles default args with nested parens like `new Date()`)
  const re = new RegExp(`function\\s+${name}\\s*\\([^{]*\\{[\\s\\S]*?^\\s{2}\\}`, 'm');
  const m = src.match(re);
  if (!m) throw new Error(`function ${name} not found in userscript`);
  return m[0];
}

const sandbox = `
  const FAST_POLL_MS = ${10 * 60 * 1000};
  const SLOW_POLL_MS = ${30 * 60 * 1000};
  const DAILY_POLL_MS = ${24 * 60 * 60 * 1000};
  const DAY_START_HOUR = 8;
  const DAY_END_HOUR = 23;
  const PENDING_CRITICAL_DAYS = 6;
  const APPEAL_IN_PROGRESS = 'בקשה בטיפול';
  ${extractFn('isEmpty')}
  ${extractFn('parseDate')}
  ${extractFn('getPendingCourses')}
  ${extractFn('hasPendingTest')}
  ${extractFn('computePollIntervalMs')}
  return { isEmpty, parseDate, getPendingCourses, hasPendingTest, computePollIntervalMs,
           FAST_POLL_MS, SLOW_POLL_MS, DAILY_POLL_MS };
`;
const { isEmpty, parseDate, getPendingCourses, hasPendingTest, computePollIntervalMs,
        FAST_POLL_MS, SLOW_POLL_MS, DAILY_POLL_MS } = new Function(sandbox)();

// Helpers
const at = (iso) => new Date(iso);
const today = (offsetDays = 0) => {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}`;
};
const info = (extra = {}) => ({
  course_name: 'מתמטיקה', course_code: '88-101', lecturer: 'X',
  moed: "א'", date: today(-5), grade: '—', final_grade: '—', appeal_status: '—',
  ...extra
});

// ──────────────────────────────────────────────────────────────────────

describe('getPendingCourses', () => {
  it('empty grades → empty', () => {
    expect(getPendingCourses({})).toEqual({});
  });

  it('graded test → excluded', () => {
    expect(getPendingCourses({ k: info({ grade: '85' }) })).toEqual({});
  });

  it('final_grade set → excluded', () => {
    expect(getPendingCourses({ k: info({ final_grade: '90' }) })).toEqual({});
  });

  it('no grade, no final → included', () => {
    const g = { k: info({ grade: '—', final_grade: '—' }) };
    expect(Object.keys(getPendingCourses(g))).toHaveLength(1);
  });

  it('appeal in progress → included (still waiting)', () => {
    const g = { k: info({ grade: '—', final_grade: '—', appeal_status: 'בקשה בטיפול' }) };
    expect(Object.keys(getPendingCourses(g))).toHaveLength(1);
  });

  it('appeal resolved (approved) → excluded', () => {
    const g = { k: info({ grade: '90', final_grade: '—', appeal_status: 'אושר' }) };
    expect(getPendingCourses(g)).toEqual({});
  });

  it('mix: one graded, one pending → only pending returned', () => {
    const grades = {
      a: info({ grade: '85' }),
      b: info({ grade: '—', final_grade: '—' }),
    };
    const result = getPendingCourses(grades);
    expect(Object.keys(result)).toEqual(['b']);
  });
});

// Critical window: starts 6 days AFTER exam, runs until grade is posted.
// < 6 days after exam = too soon (grade rarely appears that fast).
// >= 6 days after exam, grade still missing = critical, poll fast.
describe('hasPendingTest', () => {
  it('empty grades → not critical', () => {
    expect(hasPendingTest({})).toBe(false);
  });

  it('graded test → not critical', () => {
    expect(hasPendingTest({ k: info({ grade: '85' }) })).toBe(false);
  });

  it('only final_grade set → not critical', () => {
    expect(hasPendingTest({ k: info({ final_grade: '90' }) })).toBe(false);
  });

  it('test 3 days ago, no grade → NOT critical (< 6 days, too soon)', () => {
    expect(hasPendingTest({ k: info({ date: today(-3), grade: '—' }) })).toBe(false);
  });

  it('test 5 days ago, no grade → NOT critical (< 6 days, too soon)', () => {
    expect(hasPendingTest({ k: info({ date: today(-5), grade: '—' }) })).toBe(false);
  });

  it('test exactly 6 days ago → critical (window starts at day 6)', () => {
    expect(hasPendingTest({ k: info({ date: today(-6), grade: '—' }) })).toBe(true);
  });

  it('test 7 days ago, no grade → critical (6+ days, waiting for grade)', () => {
    expect(hasPendingTest({ k: info({ date: today(-7), grade: '—' }) })).toBe(true);
  });

  it('test 90 days ago, no grade → critical (long wait, still no grade)', () => {
    expect(hasPendingTest({ k: info({ date: today(-90), grade: '—' }) })).toBe(true);
  });

  it('future exam → not critical', () => {
    expect(hasPendingTest({ k: info({ date: today(+5) }) })).toBe(false);
  });

  it('mix: 5 days (not critical) + 30 days (critical) → critical', () => {
    expect(hasPendingTest({
      a: info({ date: today(-5), grade: '—' }),
      b: info({ date: today(-30), grade: '—' }),
    })).toBe(true);
  });

  it('mix of pending + graded → critical if any ungraded is 6+ days old', () => {
    expect(hasPendingTest({
      a: info({ grade: '85' }),
      b: info({ date: today(-10), grade: '—' }),
    })).toBe(true);
  });

  it('invalid date format → ignored', () => {
    expect(hasPendingTest({ k: info({ date: 'not-a-date' }) })).toBe(false);
  });

  it('nbsp grade counts as empty → critical when 10 days old', () => {
    expect(hasPendingTest({ k: info({ date: today(-10), grade: ' ' }) })).toBe(true);
  });
});

describe('computePollIntervalMs', () => {
  const dayHour = at('2026-05-17T10:00:00');      // 10:00 — daytime
  const nightHour = at('2026-05-17T02:00:00');    // 02:00 — nighttime
  const earlyMorning = at('2026-05-17T07:30:00'); // 07:30 — before window
  const lateNight = at('2026-05-17T23:30:00');    // 23:30 — after window

  it('no grades at all → DAILY_POLL_MS (nothing to wait for)', () => {
    expect(computePollIntervalMs({}, dayHour)).toBe(DAILY_POLL_MS);
  });

  it('all courses graded → DAILY_POLL_MS (daytime)', () => {
    expect(computePollIntervalMs({ k: info({ grade: '85' }) }, dayHour)).toBe(DAILY_POLL_MS);
  });

  it('all courses graded → DAILY_POLL_MS (nighttime)', () => {
    expect(computePollIntervalMs({ k: info({ grade: '85' }) }, nightHour)).toBe(DAILY_POLL_MS);
  });

  it('pending but < 6 days since exam + daytime → SLOW_POLL_MS (not critical yet)', () => {
    const grades = { k: info({ date: today(-3), grade: '—' }) };
    expect(computePollIntervalMs(grades, dayHour)).toBe(SLOW_POLL_MS);
  });

  it('pending, >= 6 days since exam + nighttime → SLOW_POLL_MS (no fast polling at night)', () => {
    const grades = { k: info({ date: today(-30), grade: '—' }) };
    expect(computePollIntervalMs(grades, nightHour)).toBe(SLOW_POLL_MS);
  });

  it('pending, >= 6 days since exam + daytime → FAST_POLL_MS', () => {
    const grades = { k: info({ date: today(-30), grade: '—' }) };
    expect(computePollIntervalMs(grades, dayHour)).toBe(FAST_POLL_MS);
  });

  it('pending, >= 6 days + 07:30 → SLOW (window starts at 8)', () => {
    const grades = { k: info({ date: today(-10), grade: '—' }) };
    expect(computePollIntervalMs(grades, earlyMorning)).toBe(SLOW_POLL_MS);
  });

  it('pending, >= 6 days + 23:30 → SLOW (window ends at 23)', () => {
    const grades = { k: info({ date: today(-10), grade: '—' }) };
    expect(computePollIntervalMs(grades, lateNight)).toBe(SLOW_POLL_MS);
  });

  it('FAST < SLOW < DAILY (sanity)', () => {
    expect(FAST_POLL_MS).toBeLessThan(SLOW_POLL_MS);
    expect(SLOW_POLL_MS).toBeLessThan(DAILY_POLL_MS);
  });
});
