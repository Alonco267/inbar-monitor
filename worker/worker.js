/**
 * Inbar Grade Monitor — relay Worker
 *
 * Only purpose: forward alert text from each user's userscript to
 * THEIR Telegram chat, via a shared bot.
 *
 * What's stored in KV (TOKENS namespace):
 *   token (random 48-hex) → chat_id (Telegram numeric id)
 *
 * What's NEVER stored or logged:
 *   - Inbar username/password (never sent here)
 *   - Grades or grade values (alert text passes through but is not persisted)
 *   - User's real name, email, or ID
 *
 * Endpoints:
 *   POST /telegram-webhook   — Telegram → us (handles /start <token>)
 *   GET  /status?token=…     — userscript polls until linked
 *   POST /alert {token,text} — userscript pushes alert → forwarded to Telegram
 *
 * Required Worker bindings:
 *   - TOKENS         (KV namespace)
 *   - BOT_TOKEN      (secret: from @BotFather)
 *   - WEBHOOK_SECRET (secret: random string, set on Telegram setWebhook)
 */

import { USERSCRIPT } from './userscript-bundle.js';

const MAX_ALERT_LEN = 1024;
const MAX_SUMMARY_LEN = 4000;
const TOKEN_RE = /^[a-f0-9]{32,64}$/;
const HEARTBEAT_TTL = 60 * 60 * 24 * 90;   // 90 days
const SUMMARY_TTL = HEARTBEAT_TTL;
const OTP_TTL = 10 * 60;                    // OTP pending/code expires in 10 min
const STATE_TTL = HEARTBEAT_TTL;            // daemon state (cookies + last grades) — 90d
const MAX_STATE_BYTES = 512 * 1024;         // 512KB — Playwright storage_state is typically <50KB

const BTN_CONNECTED = '🔌 האם אני מחובר?';
const BTN_GRADES    = '📊 רשימת הציונים';
const BTN_GPA       = '🎓 הממוצע שלי';

const REPLY_KEYBOARD = {
  keyboard: [[{ text: BTN_CONNECTED }], [{ text: BTN_GRADES }], [{ text: BTN_GPA }]],
  resize_keyboard: true,
  is_persistent: true
};

// How stale a heartbeat must be before the cron backstop re-triggers the
// GitHub workflow. A healthy 5-min cadence leaves the heartbeat 5-7 min old
// at check time (the run itself takes 1-2 min), so the threshold must be
// comfortably above one full cycle: two missed cycles + runtime slack.
const DISPATCH_STALE_MS = 12 * 60 * 1000;

// The GitHub workflow cron only runs 05:00-20:55 UTC (Israel daytime).
// The backstop must respect the same quiet window or it would dispatch
// all night against an intentionally idle schedule.
const DISPATCH_UTC_START = 5;
const DISPATCH_UTC_END = 20;   // inclusive

// Direct store links — the free versions never ask for payment from the store
// install flow. (The marketing site tampermonkey.net pushes a paid "Pro"
// upsell which is what you DON'T want to use — go through the store only.)
const MONKEY_LINKS = {
  chrome:  'https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo',
  firefox: 'https://addons.mozilla.org/firefox/addon/tampermonkey/',
  edge:    'https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd',
  safari:  'https://apps.apple.com/app/userscripts/id1463298887'   // Safari: "Userscripts" (free, OSS)
};

// Stale-heartbeat watchdog: how old before we warn the user.
const WATCHDOG_STALE_MS = 60 * 60 * 1000;          // 60 min
// And don't re-warn for the same user more than once per day.
const WATCHDOG_REWARN_TTL = 60 * 60 * 24;           // 24 hours (KV TTL, in seconds)

export default {
  async scheduled(event, env, _ctx) {
    // Runs every 5 minutes (see wrangler.toml). Two duties:
    //   1. Scheduling backstop: GitHub's cron is best-effort (delays of
    //      5–30 min, silent auto-disable after repo inactivity). If any
    //      active user's heartbeat is stale, fire workflow_dispatch.
    //   2. Hourly watchdog: warn users whose monitor went silent —
    //      unless they intentionally paused it (no false alarms).
    const now = Date.now();
    const when = new Date(event?.scheduledTime || now);
    const runWatchdog = when.getMinutes() === 0;   // hourly, on the hour
    const hourUTC = when.getUTCHours();
    const inDispatchWindow =
      hourUTC >= DISPATCH_UTC_START && hourUTC <= DISPATCH_UTC_END;

    let needDispatch = false;
    let cursor;
    do {
      const page = await env.TOKENS.list({ cursor });
      for (const { name: key } of page.keys) {
        // Skip non-token keys (they contain a colon, e.g. `chat:123`).
        if (key.includes(':')) continue;
        if (!TOKEN_RE.test(key)) continue;
        const token = key;
        const chatId = await env.TOKENS.get(token);
        if (!chatId) continue;

        // Intentionally paused → no dispatch, no warnings. This is the fix
        // for false "script stopped" alerts when monitoring is disabled.
        if (await env.TOKENS.get(`paused:${token}`)) continue;

        const hbStr = await env.TOKENS.get(`heartbeat:${token}`);
        if (!hbStr) continue;   // never reported — nothing to act on yet
        const hb = parseInt(hbStr, 10);
        const age = now - hb;

        if (inDispatchWindow && age >= DISPATCH_STALE_MS) needDispatch = true;

        if (!runWatchdog) continue;
        if (age < WATCHDOG_STALE_MS) continue;
        // Suppress if we already warned recently.
        if (await env.TOKENS.get(`warned:${token}`)) continue;

        const ageLabel = formatAgo(age);
        await sendTelegram(env, chatId,
          `⚠️ הסקריפט הפסיק לרוץ.\nעדכון אחרון: ${ageLabel}.\n` +
          `בדוק/י ב-GitHub Actions שהריצות מצליחות, או שאת/ה לא חסום/ה ע"י אינ-בר.\n` +
          `(אם השבתת בכוונה — שלח/י /pause ולא אציק יותר.)`);
        await env.TOKENS.put(`warned:${token}`, String(now),
          { expirationTtl: WATCHDOG_REWARN_TTL });
      }
      cursor = page.list_complete ? null : page.cursor;
    } while (cursor);

    if (needDispatch) await triggerGithubWorkflow(env);
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    if (request.method === 'POST' && url.pathname === '/telegram-webhook') {
      return handleTelegramWebhook(request, env);
    }
    if (request.method === 'GET' && url.pathname === '/status') {
      return handleStatus(url, env);
    }
    if (request.method === 'POST' && url.pathname === '/alert') {
      return handleAlert(request, env);
    }
    if (request.method === 'POST' && url.pathname === '/heartbeat') {
      return handleHeartbeat(request, env);
    }
    if (request.method === 'POST' && url.pathname === '/grades-summary') {
      return handleGradesSummary(request, env);
    }
    if (request.method === 'POST' && url.pathname === '/gpa') {
      return handlePutGpa(request, env);
    }
    if (request.method === 'POST' && url.pathname === '/unlink') {
      return handleUnlink(request, env);
    }
    if (request.method === 'POST' && url.pathname === '/request-otp') {
      return handleRequestOtp(request, env);
    }
    if (request.method === 'GET' && url.pathname === '/poll-otp') {
      return handlePollOtp(url, env);
    }
    if (request.method === 'POST' && url.pathname === '/otp-reply') {
      return handleOtpReply(request, env);
    }
    if (request.method === 'GET' && url.pathname === '/state') {
      return handleGetState(url, env);
    }
    if (request.method === 'PUT' && url.pathname === '/state') {
      return handlePutState(request, env);
    }
    if (url.pathname === '/inbar-monitor.user.js') {
      return new Response(USERSCRIPT, {
        status: 200,
        headers: {
          'Content-Type': 'application/javascript; charset=utf-8',
          'Cache-Control': 'public, max-age=300'
        }
      });
    }
    if (url.pathname === '/health') {
      return new Response('inbar-relay ok', { status: 200 });
    }
    // Admin: re-register this worker's webhook with Telegram.
    // Protected by the bot token — call once if the webhook ever gets wiped.
    if (request.method === 'POST' && url.pathname === '/setup-webhook') {
      return handleSetupWebhook(request, env, url);
    }
    if (url.pathname === '/') {
      return new Response(landingPage(url.origin), {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=utf-8' }
      });
    }
    return new Response('not found', { status: 404 });
  }
};

// ─── Handlers ────────────────────────────────────────────────────────────

async function handleTelegramWebhook(request, env) {
  // Verify the request is actually from Telegram (set on setWebhook)
  if (env.WEBHOOK_SECRET) {
    const got = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
    if (got !== env.WEBHOOK_SECRET) return new Response('forbidden', { status: 403 });
  }

  let update;
  try { update = await request.json(); }
  catch { return new Response('bad json', { status: 400 }); }

  const msg = update.message;
  if (!msg || !msg.text || !msg.chat || !msg.chat.id) return new Response('OK');

  const chatId = String(msg.chat.id);
  const text = String(msg.text).trim();

  const m = text.match(/^\/start\s+([a-f0-9]{32,64})\b/i);
  if (m) {
    const token = m[1].toLowerCase();
    await env.TOKENS.put(token, chatId);
    await env.TOKENS.put(`chat:${chatId}`, token);   // reverse mapping for button handlers
    await sendTelegram(env, chatId,
      '✅ הבוט מחובר. תקבל התראה אוטומטית כשיעלו ציונים חדשים באינ-בר.\n' +
      'הפרטיות שלך: הסיסמה והציונים שלך נשארים בדפדפן שלך בלבד.',
      { reply_markup: REPLY_KEYBOARD });
    return new Response('OK');
  }

  if (text === '/start' || /^\/help\b/i.test(text)) {
    const origin = new URL(request.url).origin;
    await sendTelegram(env, chatId,
      'שלום! 👋\n\n' +
      'אני שולח לך התראות אוטומטיות כשעולים ציונים חדשים באינ-בר.\n\n' +
      '🔒 הסיסמה והציונים שלך נשארים בדפדפן שלך בלבד — לא נשמרים בשום שרת.\n\n' +
      '💻 חשוב: יש לפתוח את הקישור הבא מהמחשב — ההתקנה לא עובדת מהטלפון.\n\n' +
      '⏱ ההתקנה לוקחת כדקה. לחץ/י על הכפתור להוראות מלאות:',
      {
        reply_markup: {
          inline_keyboard: [
            [{ text: 'ℹ️ הוראות מלאות', url: origin }]
          ]
        }
      });
    return new Response('OK');
  }

  // OTP intercept: if daemon is waiting for a code, this message IS the code
  const otpPending = await env.TOKENS.get(`otp:pending:${chatId}`);
  if (otpPending && /^\d{4,8}$/.test(text)) {
    await env.TOKENS.delete(`otp:pending:${chatId}`);
    await env.TOKENS.put(`otp:code:${chatId}`, text, { expirationTtl: OTP_TTL });
    await sendTelegram(env, chatId, '✅ קוד התקבל — מתחבר לאינ-בר...');
    return new Response('OK');
  }

  // Persistent reply-keyboard button taps
  if (text === BTN_CONNECTED) {
    await handleConnectedButton(env, chatId);
    return new Response('OK');
  }
  if (text === BTN_GRADES) {
    await handleGradesButton(env, chatId);
    return new Response('OK');
  }
  if (text === BTN_GPA || /^\/gpa\b/i.test(text)) {
    await handleGpaButton(env, chatId);
    return new Response('OK');
  }
  if (/^\/pause\b/i.test(text)) {
    await handlePauseCommand(env, chatId);
    return new Response('OK');
  }
  if (/^\/resume\b/i.test(text)) {
    await handleResumeCommand(env, chatId);
    return new Response('OK');
  }

  // Any other message: ignore.
  return new Response('OK');
}

async function handlePauseCommand(env, chatId) {
  const token = await env.TOKENS.get(`chat:${chatId}`);
  if (!token) {
    await sendTelegram(env, chatId,
      'עוד לא חיברת את הבוט.\nשלח/י /start כדי להתחיל.');
    return;
  }
  await env.TOKENS.put(`paused:${token}`, '1');
  // A paused user must never get "script stopped" warnings.
  await env.TOKENS.delete(`warned:${token}`);
  await sendTelegram(env, chatId,
    '⏸️ הניטור הושהה.\nלא תישלחנה התראות ולא אזהרות "הסקריפט הפסיק".\n' +
    'שלח/י /resume כדי לחדש.');
}

async function handleResumeCommand(env, chatId) {
  const token = await env.TOKENS.get(`chat:${chatId}`);
  if (!token) {
    await sendTelegram(env, chatId,
      'עוד לא חיברת את הבוט.\nשלח/י /start כדי להתחיל.');
    return;
  }
  await env.TOKENS.delete(`paused:${token}`);
  await sendTelegram(env, chatId,
    '▶️ הניטור חודש! אחזור לעדכן אותך על כל שינוי בציונים.');
}

async function handleGpaButton(env, chatId) {
  const token = await env.TOKENS.get(`chat:${chatId}`);
  if (!token) {
    await sendTelegram(env, chatId,
      'עוד לא חיברת את הבוט.\nשלח/י /start כדי להתחיל.');
    return;
  }
  const gpa = await env.TOKENS.get(`gpa:${token}`);
  if (!gpa) {
    await sendTelegram(env, chatId,
      'אין עדיין ממוצע שמור מדף "ציונים ממוצעים" באינ-בר.\n' +
      'הוא יתעדכן אוטומטית בריצה הבאה של המוניטור.');
    return;
  }
  await sendTelegram(env, chatId, gpa);
}

// Fire the GitHub Actions workflow when the schedule lags. Needs three
// Worker secrets/vars: GITHUB_PAT (fine-grained, actions:write),
// GITHUB_REPO ("owner/repo"), GITHUB_WORKFLOW (file name, e.g. "inbar.yml").
async function triggerGithubWorkflow(env) {
  if (!env.GITHUB_PAT || !env.GITHUB_REPO || !env.GITHUB_WORKFLOW) return false;
  try {
    const r = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.GITHUB_WORKFLOW}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.GITHUB_PAT}`,
          'Accept': 'application/vnd.github+json',
          'User-Agent': 'inbar-relay-worker',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ ref: env.GITHUB_REF || 'main' })
      });
    return r.status === 204;
  } catch {
    return false;
  }
}

async function handleConnectedButton(env, chatId) {
  const token = await env.TOKENS.get(`chat:${chatId}`);
  if (!token) {
    await sendTelegram(env, chatId,
      'עוד לא חיברת את הסקריפט בדפדפן.\nשלח/י /start כדי לקבל הוראות התקנה.');
    return;
  }
  const hbStr = await env.TOKENS.get(`heartbeat:${token}`);
  if (!hbStr) {
    await sendTelegram(env, chatId,
      '⚠️ הסקריפט עוד לא דיווח על פעילות.\n' +
      'פתח/י את לוח הבחינות באינ-בר וחכה כמה שניות.');
    return;
  }
  const hb = parseInt(hbStr, 10);
  const ageMs = Date.now() - hb;
  const ageLabel = formatAgo(ageMs);
  let status;
  // GitHub Actions cron runs every 5 min but can be delayed up to ~30 min
  // under platform load — so be generous with the "active" threshold.
  if (ageMs < 20 * 60 * 1000) {
    status = `✅ מחובר ופעיל. עדכון אחרון: ${ageLabel}.`;
  } else if (ageMs < 60 * 60 * 1000) {
    status = `⏳ הריצה האחרונה הייתה ${ageLabel}.\nGitHub Actions לפעמים מתעכב מעט — נסה/י שוב בעוד דקות.`;
  } else if (ageMs < 24 * 60 * 60 * 1000) {
    status = `⚠️ הסקריפט לא רץ כבר ${ageLabel}.\nבדוק/י ב-GitHub Actions שהריצות עוברות בהצלחה.`;
  } else {
    status = `🔴 לא ראינו את הסקריפט כבר ${ageLabel}.\nודא/י שה-GitHub workflow פעיל ושהסודות תקפים.`;
  }
  await sendTelegram(env, chatId, status);
}

async function handleGradesButton(env, chatId) {
  const token = await env.TOKENS.get(`chat:${chatId}`);
  if (!token) {
    await sendTelegram(env, chatId,
      'עוד לא חיברת את הסקריפט בדפדפן.\nשלח/י /start כדי לקבל הוראות.');
    return;
  }
  const summary = await env.TOKENS.get(`grades:${token}`);
  if (!summary) {
    await sendTelegram(env, chatId,
      'אין עדיין סיכום ציונים שמור.\nפתח/י את לוח הבחינות באינ-בר וחכה כמה שניות — אעדכן אוטומטית.');
    return;
  }
  await sendTelegram(env, chatId, summary);
}

function formatAgo(ms) {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return 'כרגע';
  const min = Math.floor(sec / 60);
  if (min < 60) return `לפני ${min} דקות`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `לפני ${hr} שעות`;
  const days = Math.floor(hr / 24);
  return `לפני ${days} ימים`;
}

async function handleHeartbeat(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!(await env.TOKENS.get(token))) return json({ error: 'not linked' }, 404);
  await env.TOKENS.put(`heartbeat:${token}`, String(Date.now()),
    { expirationTtl: HEARTBEAT_TTL });
  // The script is alive again — clear any pending "you've gone silent" warning
  // so a future outage can trigger a fresh alert.
  await env.TOKENS.delete(`warned:${token}`);
  return json({ ok: true });
}

async function handleUnlink(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  const chatId = await env.TOKENS.get(token);
  if (!chatId) return json({ ok: true, was_linked: false });

  // Wipe all KV entries for this user
  await env.TOKENS.delete(token);
  await env.TOKENS.delete(`chat:${chatId}`);
  await env.TOKENS.delete(`heartbeat:${token}`);
  await env.TOKENS.delete(`grades:${token}`);
  await env.TOKENS.delete(`state:${token}`);
  await env.TOKENS.delete(`gpa:${token}`);
  await env.TOKENS.delete(`paused:${token}`);

  // Notify user + hide the persistent reply keyboard
  await sendTelegram(env, chatId,
    '🔌 הבוט נותק לפי בקשתך.\nתוכל/י לחבר מחדש בכל עת ע"י פתיחת אינ-בר עם הסקריפט.',
    { reply_markup: { remove_keyboard: true } });

  return json({ ok: true, was_linked: true });
}

async function handleGradesSummary(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  const summary = String(body.summary || '');
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!summary || summary.length > MAX_SUMMARY_LEN) return json({ error: 'bad summary' }, 400);
  if (!(await env.TOKENS.get(token))) return json({ error: 'not linked' }, 404);
  await env.TOKENS.put(`grades:${token}`, summary,
    { expirationTtl: SUMMARY_TTL });
  return json({ ok: true });
}

function landingPage(origin) {
  const installUrl = `${origin}/inbar-monitor.user.js`;
  const inbarUrl = 'https://inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx';
  return `<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inbar Grade Monitor</title>
<style>
  :root { --blue:#1d72b8; --green:#1a7f37; --bg:#fafbfc; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 680px; margin: 32px auto; padding: 0 18px; color:#222;
         line-height:1.6; background:var(--bg); }
  h1 { color:var(--blue); margin-bottom: 4px; }
  .lede { color:#555; margin-top: 0; font-size: 17px; }
  .step { background:#fff; border-radius:12px; padding:18px 22px; margin:14px 0;
          box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .step h3 { margin-top: 0; color: var(--blue); }
  .btn { display:inline-block; background:var(--blue); color:#fff; text-decoration:none;
         padding:10px 22px; border-radius:8px; font-weight:600; margin-top:8px; font-size:15px; }
  .btn.green { background: var(--green); }
  .browser-note { color:#666; font-size:13px; margin-top:8px; }
  .alt details { margin-top: 10px; color:#666; font-size:14px; }
  .alt summary { cursor:pointer; }
  .alt ul { padding-inline-start: 18px; margin:8px 0; }
  .privacy { background:#eef6ff; border-right:4px solid var(--blue); border-radius:8px;
             padding:12px 16px; margin-top:24px; font-size:14px; color:#234; }
  code { background:#eef2f6; padding:1px 6px; border-radius:4px; font-size:13px; }
</style>
</head>
<body>
<h1>📊 Inbar Grade Monitor</h1>
<p class="lede">התראות אוטומטיות ב-Telegram כשעולים לך ציונים חדשים באינ-בר.</p>

<div id="mobile-warn" style="display:none;background:#fee;border:2px solid #d62828;
     border-radius:10px;padding:16px;margin:16px 0;color:#5b1010;font-weight:600;">
  ⚠️ זוהה טלפון נייד. ההתקנה מתבצעת רק <u>ממחשב</u> (Chrome / Firefox / Edge / Safari על Mac).<br>
  פתח/י את הקישור הזה מהמחשב.
</div>

<div class="step">
  <h3>שלב 1: התקן מנהל-סקריפטים לדפדפן (חינם)</h3>
  <p>זה מאפשר לסקריפט הקטן שלנו לרוץ בדפדפן שלך.</p>
  <a id="primary-btn" class="btn" href="${MONKEY_LINKS.chrome}" target="_blank" rel="noopener">📥 התקן Tampermonkey</a>
  <div id="browser-note" class="browser-note"></div>
  <div style="background:#fff7e6;border-right:4px solid #d97706;border-radius:8px;
              padding:10px 14px;margin-top:12px;font-size:13.5px;color:#5b3b00;">
    ⚠️ <b>חשוב:</b> התוסף הבסיסי <u>חינמי לחלוטין</u>. בלחיצה על הקישור למעלה
    מגיעים ישירות ל-Chrome Web Store ולוחצים <b>"Add to Chrome"</b> — זהו.<br>
    אם נשלחתם לאתר tampermonkey.net המקורי, הם מנסים למכור גרסת "Pro" —
    <b>אין צורך! הגרסה החינמית עושה הכל.</b>
  </div>
  <div class="alt">
    <details>
      <summary>אני בדפדפן אחר</summary>
      <ul>
        <li><a href="${MONKEY_LINKS.chrome}" target="_blank">Chrome — Tampermonkey (חינם)</a></li>
        <li><a href="${MONKEY_LINKS.firefox}" target="_blank">Firefox — Tampermonkey (חינם)</a></li>
        <li><a href="${MONKEY_LINKS.edge}" target="_blank">Edge — Tampermonkey (חינם)</a></li>
        <li><a href="${MONKEY_LINKS.safari}" target="_blank">Safari — Userscripts (חינם)</a></li>
      </ul>
    </details>
  </div>
</div>

<div id="step-allow" class="step">
  <h3>שלב 2: אפשר ל-Tampermonkey להריץ סקריפטים</h3>
  <p style="color:#b03030;font-weight:600;">⚠️ בלי השלב הזה — שום דבר לא יקרה. Chrome חוסם סקריפטים כברירת מחדל.</p>
  <ol style="margin:8px 0;padding-inline-start:20px;">
    <li style="margin:6px 0;">
      פתח/י בלשונית חדשה את הקישור:
      <div style="background:#222;color:#fff;padding:8px 12px;border-radius:6px;margin:6px 0;
                  font-family:Menlo,Consolas,monospace;font-size:13.5px;user-select:all;direction:ltr;text-align:left;">
        <span id="ext-url">chrome://extensions/?id=dhdgffkkebhmkfjojejmpbldmpobfkfo</span>
      </div>
      <small style="color:#666;">(לחיצה כפולה על השורה תבחר אותה, ואז Cmd+C / Ctrl+C כדי להעתיק)</small>
    </li>
    <li style="margin:6px 0;">
      בפינה הימנית-עליונה של הדף הזה — הפעל/י את <b>"Developer mode"</b> (מצב מפתחים) אם הוא כבוי.
    </li>
    <li style="margin:6px 0;">
      גלול/י למטה ומצא/י את ההגדרה <b>"Allow user scripts"</b> (אפשר תסריטי משתמש) — הדלק/י אותה ✅.
    </li>
  </ol>
  <small style="color:#666;">
    <span id="step-allow-fox" style="display:none;">ב-Firefox — אין צורך בשלב הזה, דלג/י לשלב 3.</span>
  </small>
</div>

<div class="step">
  <h3>שלב 3: התקן את הסקריפט</h3>
  <p>אחרי שאפשרת user-scripts, לחץ/י כאן — Tampermonkey יציג חלון "Install".</p>
  <a class="btn green" href="${installUrl}">🚀 התקן את הסקריפט</a>
</div>

<div class="step">
  <h3>שלב 4: היכנס לאינ-בר</h3>
  <p>פתח את לוח הבחינות באינ-בר והתחבר כרגיל:</p>
  <a class="btn" href="${inbarUrl}" target="_blank" rel="noopener">פתח את אינ-בר</a>
  <p style="margin-top:14px;">
    בפינה השמאלית התחתונה תופיע הודעה <b>"חבר את Telegram"</b> — לחץ עליה, אז על Start בבוט.<br>
    תוך כמה שניות תקבל מהבוט הודעה <b>"✅ הסקריפט פעיל בהצלחה"</b> — סיימת!
  </p>
</div>

<div class="privacy">
  🔒 <b>פרטיות מלאה:</b> הסיסמה שלך לאינ-בר ושמות הציונים נשארים בדפדפן שלך בלבד.
  השרת הזה רק מעביר את הטקסט של ההתראה ל-Telegram שלך — לא שומר ציונים, לא שומר שמות,
  לא יודע מי אתה.
</div>

<script>
  const ua = navigator.userAgent;
  const btn  = document.getElementById('primary-btn');
  const note = document.getElementById('browser-note');
  const stepAllow = document.getElementById('step-allow');
  const stepAllowFox = document.getElementById('step-allow-fox');
  const extUrl = document.getElementById('ext-url');
  const mobileWarn = document.getElementById('mobile-warn');

  const isMobile = /Android|iPhone|iPad|iPod|Mobile|Opera Mini/i.test(ua);
  if (isMobile) mobileWarn.style.display = 'block';

  if (/Firefox/.test(ua)) {
    btn.href = ${JSON.stringify(MONKEY_LINKS.firefox)};
    note.textContent = 'זוהה דפדפן Firefox.';
    // Firefox does NOT need the Chrome "Allow user scripts" toggle
    stepAllow.style.display = 'none';
  } else if (/Edg/.test(ua)) {
    btn.href = ${JSON.stringify(MONKEY_LINKS.edge)};
    note.textContent = 'זוהה דפדפן Edge.';
    extUrl.textContent = 'edge://extensions/?id=iikmkjmpaadaobahmlepeloendndfphd';
  } else if (/Safari/.test(ua) && !/Chrome/.test(ua) && !/Chromium/.test(ua)) {
    btn.href = ${JSON.stringify(MONKEY_LINKS.safari)};
    btn.textContent = '📥 התקן Userscripts (Safari)';
    note.textContent = 'זוהה דפדפן Safari — Userscripts הוא החינמי המומלץ.';
    // Safari Userscripts app handles permissions internally
    stepAllow.style.display = 'none';
  } else {
    note.textContent = 'זוהה דפדפן Chrome/Chromium.';
  }
</script>
</body>
</html>`;
}

async function handleStatus(url, env) {
  const token = (url.searchParams.get('token') || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ linked: false }, 400);
  const chatId = await env.TOKENS.get(token);
  return json({ linked: !!chatId });
}

async function handleAlert(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }

  const token = String(body.token || '').toLowerCase();
  const text = String(body.text || '');
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!text || text.length > MAX_ALERT_LEN) return json({ error: 'bad text' }, 400);

  const chatId = await env.TOKENS.get(token);
  if (!chatId) return json({ error: 'not linked' }, 404);

  const tg = await sendTelegram(env, chatId, text);
  if (!tg.ok) return json({ error: 'telegram failed', detail: tg.detail }, 502);
  return json({ sent: true });
}

// ─── Helpers ─────────────────────────────────────────────────────────────

async function sendTelegram(env, chatId, text, extra = {}) {
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text, ...extra })
    });
    const j = await r.json();
    return { ok: !!j.ok, detail: j.description || null };
  } catch (e) {
    return { ok: false, detail: String(e) };
  }
}

async function handleRequestOtp(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  const chatId = await env.TOKENS.get(token);
  if (!chatId) return json({ error: 'not linked' }, 404);
  await env.TOKENS.put(`otp:pending:${chatId}`, '1', { expirationTtl: OTP_TTL });
  await sendTelegram(env, chatId,
    '🔐 נדרש קוד אימות כדי להתחבר לאינ-בר.\n' +
    'נשלח אליך SMS לטלפון — שלח/י לי כאן את הקוד (ספרות בלבד):');
  return json({ ok: true });
}

// The local Telegram bot owns getUpdates while the user's Mac is on, so the
// webhook never sees the reply. The bot forwards the digits here instead.
// Returns pending:false when no OTP request is waiting (bot then treats the
// message as a normal command).
async function handleOtpReply(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  const code = String(body.code || '');
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!/^\d{4,8}$/.test(code)) return json({ error: 'bad code' }, 400);
  const chatId = await env.TOKENS.get(token);
  if (!chatId) return json({ error: 'not linked' }, 404);
  const pending = !!(await env.TOKENS.get(`otp:pending:${chatId}`));
  if (pending) {
    await env.TOKENS.delete(`otp:pending:${chatId}`);
    await env.TOKENS.put(`otp:code:${chatId}`, code, { expirationTtl: OTP_TTL });
  }
  return json({ ok: true, pending });
}

async function handlePollOtp(url, env) {
  const token = (url.searchParams.get('token') || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  const chatId = await env.TOKENS.get(token);
  if (!chatId) return json({ error: 'not linked' }, 404);
  const code = await env.TOKENS.get(`otp:code:${chatId}`);
  if (code) {
    await env.TOKENS.delete(`otp:code:${chatId}`);
    return json({ code });
  }
  return json({ code: null });
}

// Daemon state (browser cookies + last-seen grades). Auth model: knowing the
// token is the credential — same trust boundary as /alert. Stored under
// `state:<token>` in KV as an opaque string blob.
async function handleGetState(url, env) {
  const token = (url.searchParams.get('token') || '').toLowerCase();
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!(await env.TOKENS.get(token))) return json({ error: 'not linked' }, 404);
  const blob = await env.TOKENS.get(`state:${token}`);
  const paused = !!(await env.TOKENS.get(`paused:${token}`));
  return json({ state: blob || null, paused });
}

// Official GPA text pushed by the Python monitor after each successful run.
async function handlePutGpa(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  const gpa = String(body.gpa || '');
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (!gpa || gpa.length > MAX_SUMMARY_LEN) return json({ error: 'bad gpa' }, 400);
  if (!(await env.TOKENS.get(token))) return json({ error: 'not linked' }, 404);
  await env.TOKENS.put(`gpa:${token}`, gpa, { expirationTtl: SUMMARY_TTL });
  return json({ ok: true });
}

async function handlePutState(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'bad json' }, 400); }
  const token = String(body.token || '').toLowerCase();
  const state = body.state;
  if (!TOKEN_RE.test(token)) return json({ error: 'bad token' }, 400);
  if (typeof state !== 'string' || !state) return json({ error: 'bad state' }, 400);
  if (state.length > MAX_STATE_BYTES) return json({ error: 'state too big' }, 413);
  if (!(await env.TOKENS.get(token))) return json({ error: 'not linked' }, 404);
  await env.TOKENS.put(`state:${token}`, state, { expirationTtl: STATE_TTL });
  return json({ ok: true });
}

async function handleSetupWebhook(request, env, url) {
  let body = {};
  try { body = await request.json(); } catch {}
  if (!env.BOT_TOKEN || body.bot_token !== env.BOT_TOKEN) {
    return new Response('forbidden', { status: 403 });
  }
  const webhookUrl = `${url.origin}/telegram-webhook`;
  const payload = { url: webhookUrl, drop_pending_updates: true };
  if (env.WEBHOOK_SECRET) payload.secret_token = env.WEBHOOK_SECRET;
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/setWebhook`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
  };
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() }
  });
}
