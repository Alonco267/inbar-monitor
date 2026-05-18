# Production deployment — Inbar Grade Monitor (multi-user, free)

Goal: let up to ~1000 friends use **one** shared Telegram bot,
with **zero credential storage on any server**.

```
┌──────── Friend's browser ────────┐    HTTPS    ┌──── Cloudflare ────┐    HTTPS    ┌── Telegram ──┐
│  Tampermonkey + userscript        │ ─────────► │  Worker (relay)    │ ─────────► │  shared bot   │
│  - reads grades from DOM          │            │  KV: token↔chat_id │            │  @InbarBot    │
│  - diffs vs local storage         │            │  no credentials    │            └──────────────┘
│  - posts only alert TEXT          │            │  no grades         │
└───────────────────────────────────┘            └────────────────────┘
```

Three components to deploy. **Total cost: $0.**

---

## 1. Create the shared Telegram bot (one-time, you only)

1. Open Telegram → message **@BotFather** → `/newbot`
2. Choose a name + username (e.g. `InbarGradesBot`)
3. Copy the bot token (format `123456:ABC…`). Keep secret.
4. (Optional) `/setdescription` + `/setuserpic` to make it look legit.

---

## 2. Deploy the relay Worker (one-time, you only)

```bash
cd "worker"
npm i -g wrangler        # free, no account fee
wrangler login           # opens browser, free CF account

wrangler kv namespace create TOKENS
# → copy printed `id` into wrangler.toml, replacing REPLACE_WITH_KV_ID

wrangler secret put BOT_TOKEN       # paste BotFather token
wrangler secret put WEBHOOK_SECRET  # any random 32+ char string

wrangler deploy
# → returns https://inbar-relay.<your-subdomain>.workers.dev
```

Point Telegram at the worker:

```bash
BOT_TOKEN=…              # from BotFather
WEBHOOK_SECRET=…         # from step above
WORKER_URL=https://inbar-relay.<your-subdomain>.workers.dev

curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}/telegram-webhook" \
  -d "secret_token=${WEBHOOK_SECRET}"
```

Verify with: `curl https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo`

---

## 3. Configure & publish the userscript

Edit [userscript/inbar-monitor.user.js](userscript/inbar-monitor.user.js):

- Replace `RELAY_HOST_PLACEHOLDER` (appears in `@connect` AND in `RELAY_URL`) with your Worker host, e.g. `inbar-relay.alon.workers.dev`.
- Replace `REPO_PLACEHOLDER` in `@updateURL` / `@downloadURL` with your GitHub user/repo (e.g. `aloncohen/inbar-monitor`).
- Replace `BOT_USERNAME` if your bot is not `InbarGradesBot`.

Then push the file to a public GitHub repo. The "raw" URL is what friends install:

```
https://raw.githubusercontent.com/<USER>/<REPO>/main/userscript/inbar-monitor.user.js
```

(Tampermonkey detects `.user.js` URLs and prompts to install on click.)

---

## 4. Tell your friends (one-time per friend, ~60 seconds)

Send them this:

> **התקנה ב-60 שניות:**
>
> 1. התקן את Tampermonkey: https://www.tampermonkey.net (לחץ על הדפדפן שלך → Install)
> 2. לחץ על הקישור: `https://raw.githubusercontent.com/<USER>/<REPO>/main/userscript/inbar-monitor.user.js`
> 3. Tampermonkey יציג חלון "Install" — אשר.
> 4. פתח https://inbar.biu.ac.il והתחבר רגיל.
> 5. בפינה השמאלית התחתונה תופיע הודעה "חבר את Telegram" — לחץ.
> 6. ייפתח הבוט ב-Telegram — לחץ **Start**.
> 7. סיימת. כל ציון חדש → התראה ב-Telegram תוך 30 דקות.

That's it. They never run anything in Terminal, never give us their password, never trust a website with their grades.

---

## Privacy guarantees (what to tell your friends)

| Item                        | Where it lives                                  |
|-----------------------------|-------------------------------------------------|
| Inbar username/password     | Their browser only — never sent anywhere       |
| Their grades                | Tampermonkey local storage in their browser    |
| Their name / Inbar ID       | Never collected                                |
| Token ↔ Telegram chat id    | Cloudflare KV (no other data attached)         |
| Alert text (e.g. "ציון: 95")| Passes through worker → Telegram, not persisted |

The userscript runs **only** on `inbar.biu.ac.il/Live/StudentAssignmentTermList.aspx` — Tampermonkey enforces the `@match` rule, so it can't read any other site.

---

## How alerts work (mental model)

1. Friend keeps an Inbar tab open (or visits the grades page).
2. Userscript scrapes the rendered table → builds a map `{course|moed|date: {grade, final_grade, appeal_status, …}}`.
3. Compares to last-seen map in Tampermonkey storage.
4. On diff (new grade / new final grade / appeal resolved), generates alert text in Hebrew.
5. `GM_xmlhttpRequest POST /alert` with `{token, text}`.
6. Worker looks up `token → chat_id` in KV, calls Telegram `sendMessage`.
7. Friend gets push notification.
8. Userscript reloads the page every 30 min while tab is open — keeps the polling loop alive.

Single-message flow. No background process needed. Closing the tab pauses monitoring (resumes on next visit).

---

## Scaling notes

- **1000 friends**: ~1000 KV writes total at signup (one-time, well under free 1k/day).
  Alerts trigger only when grades actually change — typically a handful per friend per semester.
  Worker request budget: 100k/day free. You'll use far less.
- **Abuse**: Token is per-browser. To get alerts to a stranger's Telegram you'd need to steal both their token AND have them re-link. Rate limiting can be added later (KV counter per token).
- **Bot updates**: Updating the userscript is one git push. Tampermonkey auto-fetches `@updateURL` daily.

---

## Falling back to the per-user Python install (advanced/private)

The Python script in this repo still works for anyone who wants **everything** local — no worker, no shared bot, their own private bot. They run `./Install.command` once. Use this path if you don't want to deploy a worker.
