import { describe, it, expect, beforeEach, vi } from 'vitest';
import worker from './worker.js';

// ─── Fakes ─────────────────────────────────────────────────────────────
function makeKV() {
  const store = new Map();
  return {
    async get(k)              { return store.has(k) ? store.get(k) : null; },
    async put(k, v, _opts)    { store.set(k, String(v)); },  // ignore expirationTtl in fake
    async delete(k)           { store.delete(k); },
    _store: store
  };
}

const ENV = () => ({
  TOKENS: makeKV(),
  BOT_TOKEN: 'fake_bot_token',
  WEBHOOK_SECRET: 'webhook_secret_value'
});

function mockTelegramOK() {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ ok: true, result: { message_id: 42 } }),
      { status: 200, headers: { 'Content-Type': 'application/json' } })
  );
}

const URL_BASE = 'https://relay.example.com';
const VALID_TOKEN = 'a'.repeat(48);   // 48 hex chars — matches /^[a-f0-9]{32,64}$/

// ─── Tests ─────────────────────────────────────────────────────────────

describe('static routes', () => {
  it('GET /health returns 200', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/health`), env);
    expect(r.status).toBe(200);
    expect(await r.text()).toMatch(/ok/i);
  });

  it('GET / returns landing page with install steps', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/`), env);
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toMatch(/text\/html/);
    const html = await r.text();
    expect(html).toContain('Tampermonkey');
    expect(html).toContain('Allow user scripts');
    expect(html).toContain('שלב 1');
    expect(html).toContain('שלב 4');
    // privacy promise visible
    expect(html).toContain('פרטיות');
  });

  it('GET / mentions chrome:// extension URL for Allow-User-Scripts step', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/`), env);
    const html = await r.text();
    expect(html).toContain('chrome://extensions');
  });

  it('GET /inbar-monitor.user.js serves the script with JS content-type', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/inbar-monitor.user.js`), env);
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toMatch(/javascript/);
    const body = await r.text();
    expect(body).toContain('==UserScript==');
    expect(body).toContain('@match');
  });

  it('unknown route returns 404', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/nope`), env);
    expect(r.status).toBe(404);
  });
});

describe('GET /status', () => {
  it('unknown token → linked:false', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/status?token=${VALID_TOKEN}`), env);
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ linked: false });
  });

  it('invalid token format → 400', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/status?token=short`), env);
    expect(r.status).toBe(400);
  });

  it('linked token → linked:true', async () => {
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '12345');
    const r = await worker.fetch(new Request(`${URL_BASE}/status?token=${VALID_TOKEN}`), env);
    expect(await r.json()).toEqual({ linked: true });
  });
});

describe('POST /alert', () => {
  it('unknown token → 404 not linked', async () => {
    mockTelegramOK();
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, text: 'hello' })
    }), env);
    expect(r.status).toBe(404);
  });

  it('bad token format → 400', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'bad', text: 'hi' })
    }), env);
    expect(r.status).toBe(400);
  });

  it('empty text → 400', async () => {
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '99');
    const r = await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, text: '' })
    }), env);
    expect(r.status).toBe(400);
  });

  it('linked token + text → forwards to Telegram with correct chat_id', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '777');

    const r = await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, text: 'ציון חדש: 95' })
    }), env);

    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ sent: true });
    expect(tg).toHaveBeenCalledTimes(1);
    const [url, opts] = tg.mock.calls[0];
    expect(url).toContain(`/bot${env.BOT_TOKEN}/sendMessage`);
    const body = JSON.parse(opts.body);
    expect(body.chat_id).toBe('777');
    expect(body.text).toBe('ציון חדש: 95');
  });

  it('alert never includes other users chat_ids (isolation)', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put('a'.repeat(48), '111');
    await env.TOKENS.put('b'.repeat(48), '222');

    await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'a'.repeat(48), text: 'hi' })
    }), env);

    const [, opts] = tg.mock.calls[0];
    expect(JSON.parse(opts.body).chat_id).toBe('111');
    expect(JSON.parse(opts.body).chat_id).not.toBe('222');
  });
});

describe('POST /telegram-webhook (linking)', () => {
  it('without secret header → 403', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: { chat: { id: 1 }, text: '/start' } })
    }), env);
    expect(r.status).toBe(403);
  });

  it('with wrong secret header → 403', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'wrong'
      },
      body: JSON.stringify({ message: { chat: { id: 1 }, text: '/start' } })
    }), env);
    expect(r.status).toBe(403);
  });

  it('/start <token> writes token→chat_id AND reverse chat:<id>→token to KV', async () => {
    mockTelegramOK();
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({
        update_id: 1,
        message: { message_id: 1, chat: { id: 555, type: 'private' }, text: `/start ${VALID_TOKEN}` }
      })
    }), env);
    expect(r.status).toBe(200);
    expect(await env.TOKENS.get(VALID_TOKEN)).toBe('555');
    expect(await env.TOKENS.get('chat:555')).toBe(VALID_TOKEN);
  });

  it('/start <token> reply installs persistent reply keyboard', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({
        message: { chat: { id: 555 }, text: `/start ${VALID_TOKEN}` }
      })
    }), env);
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.reply_markup.keyboard).toEqual([
      [{ text: '🔌 האם אני מחובר?' }],
      [{ text: '📊 רשימת הציונים' }]
    ]);
    expect(body.reply_markup.is_persistent).toBe(true);
  });

  it('/start <token> sends confirmation to user', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({
        message: { chat: { id: 555 }, text: `/start ${VALID_TOKEN}` }
      })
    }), env);
    expect(tg).toHaveBeenCalledTimes(1);
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.chat_id).toBe('555');
    expect(body.text).toMatch(/מחובר/);
  });

  it('bare /start sends welcome with single inline button', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({
        message: { chat: { id: 888 }, text: '/start' }
      })
    }), env);
    expect(tg).toHaveBeenCalledTimes(1);
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/הוראות מלאות|מהמחשב/);
    expect(body.reply_markup.inline_keyboard).toHaveLength(1);
    expect(body.reply_markup.inline_keyboard[0]).toHaveLength(1);
    expect(body.reply_markup.inline_keyboard[0][0].url).toBe(URL_BASE);
  });

  it('bare /start mentions opening from a computer', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({
        message: { chat: { id: 888 }, text: '/start' }
      })
    }), env);
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/מהמחשב/);
  });

  it('garbage message → 200 OK with no Telegram call', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({ update_id: 1, message: { chat: { id: 1 }, text: 'random' } })
    }), env);
    expect(r.status).toBe(200);
    expect(tg).not.toHaveBeenCalled();
  });
});

// ──────────────────────────────────────────────────────────────────────
// /heartbeat + /grades-summary endpoints
// ──────────────────────────────────────────────────────────────────────

describe('POST /heartbeat', () => {
  it('linked token → 200 + writes timestamp under heartbeat:<token>', async () => {
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '111');
    const before = Date.now();
    const r = await worker.fetch(new Request(`${URL_BASE}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN })
    }), env);
    expect(r.status).toBe(200);
    const ts = parseInt(await env.TOKENS.get(`heartbeat:${VALID_TOKEN}`), 10);
    expect(ts).toBeGreaterThanOrEqual(before);
  });

  it('unlinked token → 404', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN })
    }), env);
    expect(r.status).toBe(404);
  });

  it('bad token → 400', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'short' })
    }), env);
    expect(r.status).toBe(400);
  });
});

describe('POST /grades-summary', () => {
  it('linked token + summary → 200 + writes grades:<token>', async () => {
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '111');
    const summary = '📊 ציוני סופי:\n\n📅 2025-2026  •  סמסטר א\'\n• מתמטיקה: 95';
    const r = await worker.fetch(new Request(`${URL_BASE}/grades-summary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, summary })
    }), env);
    expect(r.status).toBe(200);
    expect(await env.TOKENS.get(`grades:${VALID_TOKEN}`)).toBe(summary);
  });

  it('empty summary → 400', async () => {
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '111');
    const r = await worker.fetch(new Request(`${URL_BASE}/grades-summary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, summary: '' })
    }), env);
    expect(r.status).toBe(400);
  });
});

describe('POST /unlink', () => {
  it('linked token → wipes all 4 KV entries + replies on Telegram', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '999');
    await env.TOKENS.put('chat:999', VALID_TOKEN);
    await env.TOKENS.put(`heartbeat:${VALID_TOKEN}`, '12345');
    await env.TOKENS.put(`grades:${VALID_TOKEN}`, 'summary text');

    const r = await worker.fetch(new Request(`${URL_BASE}/unlink`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN })
    }), env);
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ ok: true, was_linked: true });

    // All four KV keys gone
    expect(await env.TOKENS.get(VALID_TOKEN)).toBeNull();
    expect(await env.TOKENS.get('chat:999')).toBeNull();
    expect(await env.TOKENS.get(`heartbeat:${VALID_TOKEN}`)).toBeNull();
    expect(await env.TOKENS.get(`grades:${VALID_TOKEN}`)).toBeNull();

    // Goodbye message sent with remove_keyboard
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.chat_id).toBe('999');
    expect(body.text).toMatch(/נותק/);
    expect(body.reply_markup.remove_keyboard).toBe(true);
  });

  it('unknown token → 200 was_linked:false, no Telegram call', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/unlink`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN })
    }), env);
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ ok: true, was_linked: false });
    expect(tg).not.toHaveBeenCalled();
  });

  it('bad token → 400', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/unlink`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: 'short' })
    }), env);
    expect(r.status).toBe(400);
  });
});

// ──────────────────────────────────────────────────────────────────────
// Persistent reply-keyboard button handlers
// ──────────────────────────────────────────────────────────────────────

async function fireButton(env, chatId, text) {
  return worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
    },
    body: JSON.stringify({ message: { chat: { id: chatId }, text } })
  }), env);
}

describe('reply-keyboard button: "🔌 האם אני מחובר?"', () => {
  it('not linked → instructs user to /start', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await fireButton(env, 7777, '🔌 האם אני מחובר?');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/start/i);
  });

  it('linked + recent heartbeat → connected status', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '7777');
    await env.TOKENS.put('chat:7777', VALID_TOKEN);
    await env.TOKENS.put(`heartbeat:${VALID_TOKEN}`, String(Date.now() - 60_000));
    await fireButton(env, 7777, '🔌 האם אני מחובר?');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/מחובר ופעיל/);
  });

  it('linked + heartbeat 30 min ago → "not running right now"', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '7777');
    await env.TOKENS.put('chat:7777', VALID_TOKEN);
    await env.TOKENS.put(`heartbeat:${VALID_TOKEN}`, String(Date.now() - 30 * 60_000));
    await fireButton(env, 7777, '🔌 האם אני מחובר?');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/לא רץ ברגע זה/);
  });

  it('linked + heartbeat 3 days ago → "not seen for X days"', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '7777');
    await env.TOKENS.put('chat:7777', VALID_TOKEN);
    await env.TOKENS.put(`heartbeat:${VALID_TOKEN}`, String(Date.now() - 3 * 24 * 60 * 60_000));
    await fireButton(env, 7777, '🔌 האם אני מחובר?');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/לא ראינו/);
    expect(body.text).toMatch(/ימים/);
  });

  it('linked but no heartbeat ever → "open Inbar to refresh"', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '7777');
    await env.TOKENS.put('chat:7777', VALID_TOKEN);
    await fireButton(env, 7777, '🔌 האם אני מחובר?');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/עוד לא דיווח/);
  });
});

describe('reply-keyboard button: "📊 רשימת הציונים"', () => {
  it('linked + stored summary → bot replies with summary verbatim', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '8888');
    await env.TOKENS.put('chat:8888', VALID_TOKEN);
    const summary = '📊 ציוני סופי:\n\n📅 2025-2026  •  סמסטר א\'\n• מתמטיקה: 95';
    await env.TOKENS.put(`grades:${VALID_TOKEN}`, summary);
    await fireButton(env, 8888, '📊 רשימת הציונים');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toBe(summary);
  });

  it('linked + no summary yet → instructs to open Inbar', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '8888');
    await env.TOKENS.put('chat:8888', VALID_TOKEN);
    await fireButton(env, 8888, '📊 רשימת הציונים');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/אין עדיין סיכום/);
  });

  it('not linked → instructs to /start', async () => {
    const tg = mockTelegramOK();
    const env = ENV();
    await fireButton(env, 8888, '📊 רשימת הציונים');
    const body = JSON.parse(tg.mock.calls[0][1].body);
    expect(body.text).toMatch(/start/i);
  });
});

describe('privacy — KV never stores anything besides token↔chat_id', () => {
  it('after /start <token>, KV has only the two ID mappings — no name/username', async () => {
    mockTelegramOK();
    const env = ENV();
    await worker.fetch(new Request(`${URL_BASE}/telegram-webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': env.WEBHOOK_SECRET
      },
      body: JSON.stringify({ message: { chat: { id: 42, first_name: 'Avi', username: 'avi' }, text: `/start ${VALID_TOKEN}` } })
    }), env);

    // Exactly two opaque ID mappings — no first_name, username, or grades.
    const keys = [...env.TOKENS._store.keys()].sort();
    expect(keys).toEqual([VALID_TOKEN, 'chat:42'].sort());
    expect(env.TOKENS._store.get(VALID_TOKEN)).toBe('42');
    expect(env.TOKENS._store.get('chat:42')).toBe(VALID_TOKEN);
    // No PII leaked into any value
    for (const v of env.TOKENS._store.values()) {
      expect(v).not.toMatch(/Avi/i);
      expect(v).not.toMatch(/avi/);
    }
  });

  it('after sending alert, KV is unchanged (no logging of grades)', async () => {
    mockTelegramOK();
    const env = ENV();
    await env.TOKENS.put(VALID_TOKEN, '42');
    const before = new Map(env.TOKENS._store);

    await worker.fetch(new Request(`${URL_BASE}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: VALID_TOKEN, text: 'ציון 95 במתמטיקה' })
    }), env);

    // KV must be byte-identical to before
    expect([...env.TOKENS._store.entries()]).toEqual([...before.entries()]);
  });
});
