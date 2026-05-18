# inbar-relay — Cloudflare Worker

Tiny relay between users' browsers (userscript) and the shared Telegram bot.

**No credentials, no grades, no PII stored.** KV holds only:
```
token (random 48 hex) → telegram_chat_id (numeric)
```

## Endpoints

| Method | Path                | Purpose                                            |
|--------|---------------------|----------------------------------------------------|
| POST   | `/telegram-webhook` | Telegram → worker. Handles `/start <token>` link.  |
| GET    | `/status?token=…`   | Userscript polls until linked.                     |
| POST   | `/alert`            | Userscript pushes `{token, text}` → forwarded.     |
| GET    | `/health`           | Returns 200.                                       |

## Deploy

```bash
# 1. Install Wrangler (free, no account fee)
npm i -g wrangler
wrangler login

# 2. Create the KV namespace
wrangler kv namespace create TOKENS
# → copy the printed `id` into wrangler.toml (replace REPLACE_WITH_KV_ID)

# 3. Push secrets
wrangler secret put BOT_TOKEN           # paste @BotFather token
wrangler secret put WEBHOOK_SECRET      # paste a random 32+ char string

# 4. Deploy
wrangler deploy
# → returns a URL like https://inbar-relay.<your-subdomain>.workers.dev

# 5. Point Telegram at the worker
BOT_TOKEN=...        # same one from step 3
WEBHOOK_SECRET=...   # same one from step 3
WORKER_URL=https://inbar-relay.<your-subdomain>.workers.dev

curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}/telegram-webhook" \
  -d "secret_token=${WEBHOOK_SECRET}"
```

## Cost

Free tier limits (sufficient for ~1000 users):
- 100,000 requests/day  (only used on alerts + linking; idle scrapes never touch the worker)
- KV: 100,000 reads/day, 1,000 writes/day
  - Writes only on first link (one per user, one-time).
  - Reads on each alert and each `/status` poll.

If you exceed the free tier, Workers Paid is $5/mo for 10M req/mo.

## Privacy posture

- No `console.log` calls in production. Workers logs only show errors via `wrangler tail` (opt-in).
- BOT_TOKEN and WEBHOOK_SECRET are never returned in any response.
- `/telegram-webhook` rejects any request lacking the secret header — protects against forged updates.
- Tokens are validated against `/^[a-f0-9]{32,64}$/` before any KV operation.
