// ==UserScript==
// @name         Inbar Grade Monitor (Bar-Ilan)
// @namespace    https://github.com/aloncohen/inbar-monitor
// @version      1.7.3
// @description  התראה ב-Telegram כשעולים ציונים באינ-בר. הציונים והסיסמה שלך נשארים בדפדפן בלבד.
// @author       Alon Cohen
// @match        https://inbar.biu.ac.il/*
// @match        https://*.biu.ac.il/*Login*
// @match        https://*.biu.ac.il/*signin*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      inbar-relay.alonco267.workers.dev
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * Privacy guarantees:
 *   1. Your Inbar username/password NEVER leave the browser. The script
 *      only reads the rendered grades table — same DOM you already see.
 *   2. Your grades are stored ONLY in this browser via GM_setValue (per-user).
 *   3. The only data sent off-device is the alert text (e.g.
 *      "עלה ציון ב-X: 95") plus a random token, sent to the relay which
 *      forwards it to YOUR Telegram chat. The relay never sees your name,
 *      ID, or password.
 */

(function () {
  'use strict';

  const LOG_PREFIX = '[InbarMonitor]';
  const log = (...a) => console.log(LOG_PREFIX, ...a);

  // ─── CRITICAL NETWORK ERROR GUARD ─────────────────────────────────────
  // If Chrome crashes or loses connection, it serves an internal offline page.
  // Abort execution immediately to prevent unsafe cross-origin frame exceptions.
  if (window.location.href.startsWith('chrome-error://') || window.location.hostname === '') {
    log('❌ Network Error: Internal browser offline page detected. Aborting run.');
    return;
  }

  log('script loaded v1.7.3 on', location.href);

  // ─── Configuration (set during build/deploy) ──────────────────────────
  const RELAY_URL = 'https://inbar-relay.alonco267.workers.dev';
  const BOT_USERNAME = 'InbarGradesBot';
  const KEEPALIVE_MS  = 5 * 60 * 1000;      //  5 min — hard cap: reload at least this often to keep session alive
  const FAST_POLL_MS  = 5 * 60 * 1000;      //  5 min — pending test 6+ days ago, daytime
  const SLOW_POLL_MS  = 5 * 60 * 1000;      //  5 min — pending but outside critical window
  const DAILY_POLL_MS = 30 * 60 * 1000;     // 30 min — no pending courses at all
  const DAY_START_HOUR = 8;
  const DAY_END_HOUR = 23;
  const PENDING_CRITICAL_DAYS = 6;          // critical window starts 6 days after exam, until grade posts
  const RELOGIN_THROTTLE_MS = 12 * 60 * 60 * 1000;
  const TABLE_SEL = '#ContentPlaceHolder1_gvStudentAssignmentTermList';
  const GRADES_PATH = '/Live/StudentAssignmentTermList.aspx';
  const LOGIN_KW = ['login', 'signin', 'logon', 'shibboleth', 'adfs', 'saml', 'idp', 'authn'];
  const APPEAL_IN_PROGRESS = 'בקשה בטיפול';
  const REJECTION_KW = [
    'נדחה', 'נדחית', 'נדחתה',
    'לא אושר', 'לא אושרה',
    'לא התקבל', 'לא התקבלה',
    'נשמר', 'אושר הציון המקורי'
  ];

  // ─── Per-browser random token (links this browser ↔ user's Telegram) ──
  function getToken() {
    let t = GM_getValue('token', '');
    if (!t) {
      const b = new Uint8Array(24);
      crypto.getRandomValues(b);
      t = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
      GM_setValue('token', t);
    }
    return t;
  }

  // ─── Empty-value detection (mirrors monitor.py _is_empty) ─────────────
  function isEmpty(v) {
    if (!v) return true;
    const s = v.replace(/ /g, ' ').replace(/\s/g, '');
    return s === '' || s === '—' || s === '-' || s === 'נ/א';
  }

  // ─── Column detection from Hebrew headers ─────────────────────────────
  function detectColumns(headers) {
    const col = {
      course_code: -1, course_name: -1, lecturer: -1, moed: -1,
      grade: -1, final_grade: -1, appeal: -1, date: -1
    };
    headers.forEach((raw, i) => {
      const h = raw.trim();
      if (h.includes('ציון סופי') && col.final_grade === -1) col.final_grade = i;
      else if (h === 'ציון' && col.grade === -1) col.grade = i;
      else if (h.includes('שם קבוצת קורס') && col.course_name === -1) col.course_name = i;
      else if (h.includes('קוד קבוצת קורס') && col.course_code === -1) col.course_code = i;
      else if (h.includes('שם המרצה') && col.lecturer === -1) col.lecturer = i;
      else if (h === 'מועד' && col.moed === -1) col.moed = i;
      else if (h.includes('תאריך') && col.date === -1) col.date = i;
      else if (h.includes('ערעור') && col.appeal === -1) col.appeal = i;
    });
    // Fallback for course name
    if (col.course_name === -1) {
      headers.forEach((h, i) => {
        if ((h.includes('קורס') || h.includes('מקצוע')) && col.course_name === -1) {
          col.course_name = i;
        }
      });
    }
    if (col.grade === -1) {
      headers.forEach((h, i) => {
        if (h.includes('ציון') && i !== col.final_grade && col.grade === -1) col.grade = i;
      });
    }
    return col;
  }

  // ─── Scrape the visible grades table ──────────────────────────────────
  function scrape() {
    const table = document.querySelector(TABLE_SEL);
    if (!table) return null;
    const rows = Array.from(table.querySelectorAll('tr'));
    if (rows.length < 2) return {};

    const headers = Array.from(rows[0].querySelectorAll('th, td'))
      .map(c => c.innerText.trim());
    const col = detectColumns(headers);
    if (col.course_name === -1) return null;

    const get = (texts, idx) => (idx >= 0 && idx < texts.length) ? texts[idx].trim() : '—';

    const grades = {};
    for (let i = 1; i < rows.length; i++) {
      const row = rows[i];
      if (row.classList.contains('GridPager')) continue;
      const cells = Array.from(row.querySelectorAll('td'));
      if (!cells.length) continue;
      const texts = cells.map(c => c.innerText.trim());
      if (!texts.some(t => t)) continue;

      const courseName = get(texts, col.course_name);
      if (!courseName || courseName === '—') continue;

      const courseCode = get(texts, col.course_code);
      const moed = get(texts, col.moed);
      const date = get(texts, col.date);
      const idPart = (courseCode && courseCode !== '—') ? courseCode : courseName;
      const key = `${idPart}|${moed}|${date}`;

      grades[key] = {
        course_name: courseName,
        course_code: courseCode,
        lecturer: get(texts, col.lecturer),
        moed,
        date,
        grade: get(texts, col.grade),
        final_grade: get(texts, col.final_grade),
        appeal_status: get(texts, col.appeal)
      };
    }
    return grades;
  }

  // ─── Grade-list message (mirror of Python _build_final_grades_message) ─
  function parseDate(s) {
    const m = (s || '').match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
    return m ? { d: +m[1], m: +m[2], y: +m[3] } : null;
  }
  function academicYear(date) {
    const p = parseDate(date);
    if (!p) return { key: 0, label: '—' };
    const end = p.m <= 9 ? p.y : p.y + 1;
    return { key: end, label: `${end - 1}-${end}` };
  }
  function semester(date) {
    const p = parseDate(date);
    if (!p) return { key: 9, label: '?' };
    if ([1, 2, 3, 4, 10, 11, 12].includes(p.m)) return { key: 1, label: "א'" };
    return { key: 2, label: "ב'" };
  }
  function moedRank(moed) {
    if (moed && moed.includes("ב'")) return 2;
    if (moed && moed.includes("א'")) return 1;
    return 0;
  }
  function reverseDate(s) {
    const p = (s || '').split('/');
    return p.length === 3 ? [p[2], p[1], p[0]].join('/') : (s || '');
  }
  function buildFinalGradesMessage(data) {
    if (!data || Object.keys(data).length === 0) {
      return 'אין נתונים שמורים עדיין. פתח/י את לוח הבחינות באינ-בר וחכה כמה שניות.';
    }
    // Dedup: latest, most-final per course name
    const byCourse = {};
    for (const info of Object.values(data)) {
      const name = info.course_name || '?';
      if (!byCourse[name]) { byCourse[name] = info; continue; }
      const ex = byCourse[name];
      const hasF = !isEmpty(info.final_grade), exHasF = !isEmpty(ex.final_grade);
      if (hasF && !exHasF) { byCourse[name] = info; continue; }
      if (!hasF && exHasF) continue;
      if (moedRank(info.moed) > moedRank(ex.moed)) { byCourse[name] = info; continue; }
      if (moedRank(info.moed) < moedRank(ex.moed)) continue;
      if (reverseDate(info.date) > reverseDate(ex.date)) byCourse[name] = info;
    }
    // Group by year + semester
    const groups = {};
    for (const name of Object.keys(byCourse).sort()) {
      const info = byCourse[name];
      const ay = academicYear(info.date), sm = semester(info.date);
      const key = `${ay.key}|${sm.key}|${ay.label}|${sm.label}`;
      (groups[key] ||= []).push([name, info]);
    }
    const sortedKeys = Object.keys(groups).sort((a, b) => {
      const [yA, sA] = a.split('|').map(Number);
      const [yB, sB] = b.split('|').map(Number);
      if (yB !== yA) return yB - yA;
      return sA - sB;
    });
    const lines = ['📊 ציונים סופיים:'];
    let hasAny = false;
    for (const k of sortedKeys) {
      const [, , yl, sl] = k.split('|');
      lines.push(`\n📅 ${yl}  •  סמסטר ${sl}`);
      for (const [name, info] of groups[k]) {
        const fg = info.final_grade;
        if (!isEmpty(fg)) { lines.push(`• ${name}: ${fg}`); hasAny = true; }
        else              { lines.push(`• ${name}: טרם פורסם`); }
      }
    }
    if (!hasAny) lines.push('\nטרם פורסמו ציונים סופיים.');
    return lines.join('\n');
  }

  // ─── Diff & alert generation ──────────────────────────────────────────
  function appealApproved(newStatus, oldGrade, newGrade) {
    if (REJECTION_KW.some(k => newStatus.includes(k))) return false;
    const og = parseFloat(oldGrade), ng = parseFloat(newGrade);
    if (!isNaN(og) && !isNaN(ng) && ng > og) return true;
    return true;
  }

  function diff(oldData, newData) {
    const alerts = [];
    for (const [key, info] of Object.entries(newData)) {
      const old = oldData[key] || {};
      const c = info.course_name;
      const m = info.moed || '';
      const g = info.grade;
      const fg = info.final_grade;
      const ap = info.appeal_status || '';
      const oldAp = old.appeal_status || '';

      if (!isEmpty(g) && isEmpty(old.grade)) {
        alerts.push(`אל תילחץ אחי אבל עלה ציון ב${c} ${m} - ציונך הוא ${g} מקווה שאתה מרוצה 🎓`);
      }
      if (!isEmpty(fg) && isEmpty(old.final_grade)) {
        alerts.push(`אל תילחץ אחי אבל עלה ציון סופי ב${c} - הציון הסופי שלך הוא ${fg} מקווה שאתה מרוצה 🎓`);
      }
      if (oldAp === APPEAL_IN_PROGRESS && ap !== APPEAL_IN_PROGRESS) {
        const approved = appealApproved(ap, old.grade, g);
        let tail = '';
        if (approved) {
          if (!isEmpty(g) && g !== old.grade && !isEmpty(old.grade)) {
            tail = `\nציון חדש: ${g} (היה ${old.grade})`;
          } else if (!isEmpty(g)) {
            tail = `\nהציון נשאר ${g}`;
          }
        } else if (!isEmpty(old.grade)) {
          tail = `\nהציון נשאר ${old.grade}`;
        }
        const verdict = approved ? 'אישר' : 'לא אישר';
        alerts.push(`היי אחי המרצה ${info.lecturer} ${verdict} את הערעור שלך בקורס ${c} ⚖️${tail}`);
      }
    }
    return alerts;
  }

  // ─── Relay HTTP (uses GM_xmlhttpRequest to bypass CORS) ───────────────
  function relayPost(path, body) {
    return new Promise(resolve => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: `${RELAY_URL}${path}`,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(body),
        onload: r => resolve({ ok: r.status >= 200 && r.status < 300, body: r.responseText }),
        onerror: () => resolve({ ok: false }),
        ontimeout: () => resolve({ ok: false }),
        timeout: 15000
      });
    });
  }

  function relayGet(path) {
    return new Promise(resolve => {
      GM_xmlhttpRequest({
        method: 'GET',
        url: `${RELAY_URL}${path}`,
        onload: r => {
          try { resolve(JSON.parse(r.responseText)); }
          catch { resolve(null); }
        },
        onerror: () => resolve(null),
        ontimeout: () => resolve(null),
        timeout: 15000
      });
    });
  }

  async function isLinked(token) {
    const r = await relayGet(`/status?token=${encodeURIComponent(token)}`);
    return !!(r && r.linked);
  }

  // ─── In-page banner UI ────────────────────────────────────────────────
  GM_addStyle(`
    #inbar-monitor-chip {
      position: fixed !important; bottom: 18px !important; left: 18px !important;
      z-index: 2147483647 !important;
      background:#fff !important; border-radius: 999px !important;
      box-shadow: 0 2px 10px rgba(0,0,0,0.14) !important;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
      font-size: 13px !important; padding: 7px 14px !important; cursor: pointer !important;
      direction: rtl !important; color: #222 !important;
      border: 2px solid #1a7f37 !important;
      transition: all 0.15s ease !important;
    }
    #inbar-monitor-chip.disconnected { border-color: #d62828 !important; }
    #inbar-monitor-chip:hover { transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0,0,0,0.2) !important; }
    #inbar-monitor-chip .chip-dot { display:inline-block; width:8px; height:8px; border-radius:50%;
        background:#1a7f37; margin-inline-end:6px; vertical-align:middle; }
    #inbar-monitor-chip.disconnected .chip-dot { background:#d62828; }
    #inbar-monitor-banner {
      position: fixed !important; bottom: 20px !important; left: 20px !important;
      z-index: 2147483647 !important;
      background: #fff !important; border: 3px solid #d62828 !important; border-radius: 12px;
      padding: 14px 18px 14px 30px; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      box-shadow: 0 4px 16px rgba(0,0,0,0.15); max-width: 360px;
      direction: rtl; text-align: right; color: #222;
    }
    #inbar-monitor-banner a { text-decoration: none; }
    #inbar-monitor-banner button.go {
      background: #1d72b8; color: #fff; border: 0; padding: 8px 18px;
      border-radius: 6px; cursor: pointer; font-size: 14px; margin-top: 10px;
    }
    #inbar-monitor-banner button.go.secondary {
      background: #fff; color: #1d72b8; border: 1px solid #1d72b8;
    }
    #inbar-monitor-banner .manual {
      margin-top: 10px; padding: 8px 10px; background: #f5f5f5; border-radius: 6px;
      font-size: 12px; color: #444; line-height: 1.5; text-align: right;
    }
    #inbar-monitor-banner .manual code {
      display: inline-block; background: #fff; border: 1px solid #ddd;
      padding: 2px 6px; border-radius: 4px; font-size: 11.5px;
      direction: ltr; max-width: 100%; word-break: break-all; user-select: all;
    }
    #inbar-monitor-banner .manual button.copy {
      background: #eee; color: #222; border: 1px solid #ccc; border-radius: 4px;
      padding: 2px 8px; font-size: 11.5px; cursor: pointer; margin-inline-start: 6px;
    }
    #inbar-monitor-banner .close-x {
      position: absolute; top: 4px; left: 8px; cursor: pointer;
      background: none; border: 0; padding: 0; color: #999; font-size: 18px;
    }
  `);

  function showBanner(html) {
    log('showBanner called');
    const old = document.getElementById('inbar-monitor-banner');
    if (old) old.remove();
    const div = document.createElement('div');
    div.id = 'inbar-monitor-banner';
    div.innerHTML = html;
    const target = document.body || document.documentElement;
    if (!target) {
      log('ERROR: no body/html to attach banner');
      return null;
    }
    target.appendChild(div);
    log('banner attached to', target.tagName);
    return div;
  }
  function hideBanner() {
    const old = document.getElementById('inbar-monitor-banner');
    if (old) old.remove();
  }

  function showChip({ connected, onClick }) {
    const old = document.getElementById('inbar-monitor-chip');
    if (old) old.remove();
    const chip = document.createElement('div');
    chip.id = 'inbar-monitor-chip';
    chip.className = connected ? 'connected' : 'disconnected';
    chip.innerHTML = connected
      ? '<span class="chip-dot"></span>הבוט מחובר • לחץ/י לניתוק'
      : '<span class="chip-dot"></span>הבוט לא מחובר • לחץ/י לחיבור';
    chip.onclick = onClick;
    (document.body || document.documentElement).appendChild(chip);
    return chip;
  }

  async function unlinkBot(token) {
    if (!confirm('לנתק את הבוט מאינ-בר?\n(תוכל/י לחבר מחדש בכל עת)')) return false;
    log('unlinking…');
    const r = await relayPost('/unlink', { token });
    if (!r.ok) {
      alert('הניתוק נכשל. נסה/י שוב.');
      return false;
    }
    // Clear local state so banner returns next reload
    GM_setValue('token', '');
    GM_setValue('link_confirmation_sent', false);
    GM_setValue('first_scrape_done', false);
    GM_setValue('grades', {});
    GM_setValue('last_relogin_alert', 0);
    alert('בוצע. הסקריפט ינסה לחבר מחדש בריענון הבא.');
    location.reload();
    return true;
  }

  // ─── Smart polling interval ───────────────────────────────────────────
  function getPendingCourses(grades) {
    const result = {};
    for (const [key, info] of Object.entries(grades || {})) {
      if (!isEmpty(info.grade) || !isEmpty(info.final_grade)) continue;
      const ap = info.appeal_status || '';
      if (ap && ap !== APPEAL_IN_PROGRESS && !isEmpty(ap)) continue;
      result[key] = info;
    }
    return result;
  }

  function hasPendingTest(grades, now = new Date()) {
    const nowMs = now.getTime();
    const criticalMs = PENDING_CRITICAL_DAYS * 24 * 60 * 60 * 1000;
    for (const info of Object.values(getPendingCourses(grades))) {
      const d = parseDate(info.date);
      if (!d) continue;
      const examMs = new Date(d.y, d.m - 1, d.d).getTime();
      if (examMs > nowMs) continue;
      if (nowMs - examMs < criticalMs) continue;
      return true;
    }
    return false;
  }

  function computePollIntervalMs(grades, now = new Date()) {
    if (Object.keys(getPendingCourses(grades)).length === 0) return DAILY_POLL_MS;
    const hour = now.getHours();
    const isDaytime = hour >= DAY_START_HOUR && hour < DAY_END_HOUR;
    if (isDaytime && hasPendingTest(grades, now)) return FAST_POLL_MS;
    return SLOW_POLL_MS;
  }

  // ─── Page-type detection ──────────────────────────────────────────────
  function isOnGradesPage() {
    return location.pathname.toLowerCase().includes(GRADES_PATH.toLowerCase());
  }
  function isOnLoginPage() {
    const u = location.href.toLowerCase();
    return LOGIN_KW.some(k => u.includes(k));
  }

  // ─── Main flow ────────────────────────────────────────────────────────
  async function checkAndAlert(token) {
    let tries = 0;
    while (!document.querySelector(TABLE_SEL)) {
      if (++tries > 40) return;
      await new Promise(r => setTimeout(r, 500));
    }
    await new Promise(r => setTimeout(r, 1500));

    const fresh = scrape();
    if (!fresh || Object.keys(fresh).length === 0) return;

    await pushHeartbeatAndSummary(token, fresh);

    if (!GM_getValue('first_scrape_done', false)) {
      GM_setValue('grades', fresh);
      GM_setValue('first_scrape_done', true);
      GM_setValue('last_check', Date.now());
      return;
    }

    const stored = GM_getValue('grades', {});
    const alerts = diff(stored, fresh);
    for (const text of alerts) await relayPost('/alert', { token, text });
    GM_setValue('grades', fresh);
    GM_setValue('last_check', Date.now());
  }

  async function pushHeartbeatAndSummary(token, fresh) {
    try {
      await relayPost('/heartbeat', { token });
      const summary = buildFinalGradesMessage(fresh);
      await relayPost('/grades-summary', { token, summary });
    } catch (e) { log('heartbeat/summary push failed:', e); }
  }

  async function ensureLinkConfirmation(token) {
    if (GM_getValue('link_confirmation_sent', false)) return;
    log('sending one-time link-confirmation alert');
    const r = await relayPost('/alert', {
      token,
      text: '✅ הסקריפט פעיל ומחובר!\nמעכשיו תקבל/י התראה אוטומטית על כל שינוי בציונים באינ-בר 🎓'
    });
    if (r.ok) GM_setValue('link_confirmation_sent', true);
  }

  async function maybeSendReloginAlert(token) {
    const last = GM_getValue('last_relogin_alert', 0);
    if (Date.now() - last < RELOGIN_THROTTLE_MS) return;
    await relayPost('/alert', {
      token,
      text: 'ההתחברות שלך לאינ-בר פגה 🔑\nהתחבר/י מחדש, ואחזור לעדכן אותך אוטומטית על ציונים חדשים.'
    });
    GM_setValue('last_relogin_alert', Date.now());
  }

  async function main() {
    const token = getToken();
    log('token:', token.slice(0, 8) + '…', 'gradesPage?', isOnGradesPage(), 'loginPage?', isOnLoginPage());
    const linked = await isLinked(token);
    log('linked?', linked);

    const appLink = `tg://resolve?domain=${BOT_USERNAME}&start=${token}`;
    const webLink = `https://t.me/${BOT_USERNAME}?start=${token}`;
    const manualCmd = `/start ${token}`;

    showChip({
      connected: linked,
      onClick: () => linked ? unlinkBot(token) : window.open(appLink, '_blank')
    });

    if (!linked) {
      const gradesHint = isOnGradesPage() ? '' : `
        <div style="margin-top:10px;padding:8px 10px;background:#fff7e6;border-radius:6px;font-size:12.5px;color:#5b3b00;">
          ⚠️ אחרי החיבור, נווט/י ל
          <a href="https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx" style="color:#1d72b8;font-weight:600;">לוח בחינות</a>
          כדי שאוכל לזהות את הציונים שלך.
        </div>`;
      const banner = showBanner(`
        <button class="close-x" title="סגור">✕</button>
        <div style="font-weight:700;font-size:16px;margin-bottom:6px;">📡 חבר את Telegram</div>
        <div style="font-size:13px;color:#444;line-height:1.5;">
          לחץ על הכפתור — ייפתח הבוט באפליקציית Telegram, לחץ על <b>Start</b>.<br>
          כל הציונים שלך נשארים בדפדפן הזה.
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <a href="${appLink}"><button class="go">פתח באפליקציה</button></a>
          <a href="${webLink}" target="_blank" rel="noopener noreferrer"><button class="go secondary">פתח בדפדפן</button></a>
        </div>
        <div class="manual">
          לא נפתח? פתח/י את הבוט
          <a href="${webLink}" target="_blank" rel="noopener noreferrer" style="color:#1d72b8;font-weight:600;">@${BOT_USERNAME}</a>
          ידנית ושלח/י:<br>
          <code>${manualCmd}</code>
          <button class="copy" type="button">העתק</button>
        </div>
        ${gradesHint}
      `);
      banner.querySelector('.close-x').onclick = () => banner.remove();
      const copyBtn = banner.querySelector('.copy');
      copyBtn.onclick = async () => {
        try { await navigator.clipboard.writeText(manualCmd); copyBtn.textContent = 'הועתק ✓'; }
        catch { copyBtn.textContent = 'שגיאה'; }
        setTimeout(() => { copyBtn.textContent = 'העתק'; }, 2000);
      };

      const iv = setInterval(async () => {
        if (await isLinked(token)) {
          clearInterval(iv);
          showBanner(`
            <button class="close-x" title="סגור">✕</button>
            <div style="font-weight:700;color:#1a7f37;">✅ Telegram מחובר</div>
            <div style="font-size:13px;margin-top:4px;">בודק את הציונים שלך…</div>
          `).querySelector('.close-x').onclick = hideBanner;
          setTimeout(hideBanner, 6000);
          showChip({ connected: true, onClick: () => unlinkBot(token) });
          await ensureLinkConfirmation(token);
          checkAndAlert(token);
        }
      }, 5000);
      return;
    }

    await ensureLinkConfirmation(token);
    await relayPost('/heartbeat', { token });

    if (isOnLoginPage()) {
      await maybeSendReloginAlert(token);
      setTimeout(() => location.reload(), KEEPALIVE_MS);
      return;
    }
    if (!isOnGradesPage()) {
      setTimeout(() => location.reload(), KEEPALIVE_MS);
      return;
    }

    await checkAndAlert(token);
    const grades = GM_getValue('grades', {});
    const ms = Math.min(KEEPALIVE_MS, computePollIntervalMs(grades));
    const pending = Object.keys(getPendingCourses(grades)).length;
    log('next reload in', Math.round(ms / 60000), 'min',
        '(pending courses:', pending + ',', 'critical window:', hasPendingTest(grades) + ',', 'hour:', new Date().getHours() + ')');
    setTimeout(() => location.reload(), ms);
  }

  main();
})();