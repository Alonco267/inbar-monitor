import { describe, it, expect, vi, afterEach } from 'vitest';
import worker from './worker.js';

function makeKV() {
  const store = new Map();
  return {
    async get(k)           { return store.has(k) ? store.get(k) : null; },
    async put(k, v, _o)    { store.set(k, String(v)); },
    async delete(k)        { store.delete(k); },
    async list({ cursor } = {}) {
      return { keys: [...store.keys()].map(name => ({ name })), list_complete: true };
    },
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
    new Response(JSON.stringify({ ok: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } })
  );
}

const URL_BASE = 'https://relay.example.com';
const TOKEN = 'a'.repeat(48);
const CHAT = '12345';

async function linkUser(env) {
  await env.TOKENS.put(TOKEN, CHAT);
  await env.TOKENS.put(`chat:${CHAT}`, TOKEN);
}

function tgUpdate(text) {
  return new Request(`${URL_BASE}/telegram-webhook`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Bot-Api-Secret-Token': 'webhook_secret_value'
    },
    body: JSON.stringify({ message: { text, chat: { id: Number(CHAT) } } })
  });
}

afterEach(() => vi.restoreAllMocks());

// ─── /pause & /resume ────────────────────────────────────────────────────

describe('/pause and /resume commands', () => {
  it('/pause sets paused:<token> and clears pending warned flag', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`warned:${TOKEN}`, '123');
    mockTelegramOK();
    const r = await worker.fetch(tgUpdate('/pause'), env);
    expect(r.status).toBe(200);
    expect(env.TOKENS._store.get(`paused:${TOKEN}`)).toBe('1');
    expect(env.TOKENS._store.has(`warned:${TOKEN}`)).toBe(false);
  });

  it('/resume deletes paused:<token>', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`paused:${TOKEN}`, '1');
    mockTelegramOK();
    await worker.fetch(tgUpdate('/resume'), env);
    expect(env.TOKENS._store.has(`paused:${TOKEN}`)).toBe(false);
  });

  it('/pause without linked token instructs /start', async () => {
    const env = ENV();
    const spy = mockTelegramOK();
    await worker.fetch(tgUpdate('/pause'), env);
    const body = JSON.parse(spy.mock.calls[0][1].body);
    expect(body.text).toContain('/start');
  });
});

// ─── GET /state exposes paused ───────────────────────────────────────────

describe('GET /state paused flag', () => {
  it('paused:false by default', async () => {
    const env = ENV();
    await linkUser(env);
    const r = await worker.fetch(
      new Request(`${URL_BASE}/state?token=${TOKEN}`), env);
    expect((await r.json()).paused).toBe(false);
  });

  it('paused:true after /pause', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`paused:${TOKEN}`, '1');
    const r = await worker.fetch(
      new Request(`${URL_BASE}/state?token=${TOKEN}`), env);
    expect((await r.json()).paused).toBe(true);
  });
});

// ─── GPA endpoint + button ───────────────────────────────────────────────

describe('POST /gpa and GPA button', () => {
  it('stores gpa:<token> for a linked user', async () => {
    const env = ENV();
    await linkUser(env);
    const r = await worker.fetch(new Request(`${URL_BASE}/gpa`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN, gpa: '🎓 ממוצע: 88.5' })
    }), env);
    expect(r.status).toBe(200);
    expect(env.TOKENS._store.get(`gpa:${TOKEN}`)).toContain('88.5');
  });

  it('rejects unlinked token with 404', async () => {
    const env = ENV();
    const r = await worker.fetch(new Request(`${URL_BASE}/gpa`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN, gpa: 'x' })
    }), env);
    expect(r.status).toBe(404);
  });

  it('button returns stored GPA verbatim', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`gpa:${TOKEN}`, '🎓 ממוצע: 91.2');
    const spy = mockTelegramOK();
    await worker.fetch(tgUpdate('🎓 הממוצע שלי'), env);
    const body = JSON.parse(spy.mock.calls[0][1].body);
    expect(body.text).toContain('91.2');
  });

  it('button without stored GPA explains it updates automatically', async () => {
    const env = ENV();
    await linkUser(env);
    const spy = mockTelegramOK();
    await worker.fetch(tgUpdate('/gpa'), env);
    const body = JSON.parse(spy.mock.calls[0][1].body);
    expect(body.text).toContain('אין עדיין ממוצע');
  });
});

// ─── scheduled(): pause-aware watchdog + dispatch backstop ───────────────

describe('scheduled()', () => {
  const onTheHour = { scheduledTime: new Date('2026-07-06T10:00:00Z').getTime() };
  const offHour   = { scheduledTime: new Date('2026-07-06T10:05:00Z').getTime() };

  it('does NOT warn a paused user with a stale heartbeat (false-alert fix)', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`heartbeat:${TOKEN}`, String(Date.now() - 3 * 60 * 60 * 1000));
    await env.TOKENS.put(`paused:${TOKEN}`, '1');
    const spy = mockTelegramOK();
    await worker.scheduled(onTheHour, env, {});
    expect(spy).not.toHaveBeenCalled();
  });

  it('warns an active user with a stale heartbeat on the hour', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`heartbeat:${TOKEN}`, String(Date.now() - 3 * 60 * 60 * 1000));
    const spy = mockTelegramOK();
    await worker.scheduled(onTheHour, env, {});
    const tgCalls = spy.mock.calls.filter(c => String(c[0]).includes('telegram'));
    expect(tgCalls.length).toBe(1);
    expect(env.TOKENS._store.has(`warned:${TOKEN}`)).toBe(true);
  });

  it('off-hour tick does not warn but still dispatches GitHub workflow when stale', async () => {
    const env = ENV();
    env.GITHUB_PAT = 'pat'; env.GITHUB_REPO = 'o/r'; env.GITHUB_WORKFLOW = 'inbar.yml';
    await linkUser(env);
    await env.TOKENS.put(`heartbeat:${TOKEN}`, String(Date.now() - 20 * 60 * 1000));
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }));
    await worker.scheduled(offHour, env, {});
    const urls = spy.mock.calls.map(c => String(c[0]));
    expect(urls.some(u => u.includes('api.github.com'))).toBe(true);
    expect(urls.some(u => u.includes('telegram'))).toBe(false);
  });

  it('heartbeat within one healthy cycle (7 min) → no dispatch', async () => {
    // Regression: a 4-min threshold would double-trigger a healthy schedule,
    // because the heartbeat is routinely 5-7 min old at check time.
    const env = ENV();
    env.GITHUB_PAT = 'pat'; env.GITHUB_REPO = 'o/r'; env.GITHUB_WORKFLOW = 'inbar.yml';
    await linkUser(env);
    await env.TOKENS.put(`heartbeat:${TOKEN}`, String(Date.now() - 7 * 60 * 1000));
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }));
    await worker.scheduled(offHour, env, {});
    expect(spy).not.toHaveBeenCalled();
  });

  it('night tick (outside 05-20 UTC window) → no dispatch even when stale', async () => {
    // Regression: the GitHub cron is intentionally idle at night; the
    // backstop must not fire workflow_dispatch every 5 min all night.
    const night = { scheduledTime: new Date('2026-07-06T02:05:00Z').getTime() };
    const env = ENV();
    env.GITHUB_PAT = 'pat'; env.GITHUB_REPO = 'o/r'; env.GITHUB_WORKFLOW = 'inbar.yml';
    await linkUser(env);
    await env.TOKENS.put(`heartbeat:${TOKEN}`, String(Date.now() - 5 * 60 * 60 * 1000));
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }));
    await worker.scheduled(night, env, {});
    expect(spy.mock.calls.map(c => String(c[0]))
      .some(u => u.includes('api.github.com'))).toBe(false);
  });

  it('unlink wipes gpa and paused keys too', async () => {
    const env = ENV();
    await linkUser(env);
    await env.TOKENS.put(`gpa:${TOKEN}`, 'x');
    await env.TOKENS.put(`paused:${TOKEN}`, '1');
    mockTelegramOK();
    await worker.fetch(new Request(`${URL_BASE}/unlink`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: TOKEN })
    }), env);
    expect(env.TOKENS._store.has(`gpa:${TOKEN}`)).toBe(false);
    expect(env.TOKENS._store.has(`paused:${TOKEN}`)).toBe(false);
  });
});
