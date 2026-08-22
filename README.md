# 🛰 Advanced Proxy Checker Telegram Bot

Paste proxies in **any** format — messy text, JSON, `.txt`/`.json` files, with or
without auth or scheme — and the bot extracts, validates, classifies and ranks
them, then forwards the live ones to your group and to the admin.

## Features

- **Messy-input parser** — handles `ip:port`, `ip:port:user:pass`,
  `user:pass@ip:port`, `scheme://…` (http/https/socks4/socks5), JSON objects &
  arrays, and proxies buried in arbitrary text or uploaded files.
- **Advanced async checker** — fully concurrent, **no cooldown, no limits**.
  - Detects the *real* protocol (HTTP / SOCKS4 / SOCKS5) by trying each.
  - Measures **ping (latency)** per proxy.
  - Classifies **residential vs datacenter** (via ISP hosting flag).
  - Detects **rotating** proxies (exit IP changes across requests).
  - Reads country / city / ISP for each live proxy.
- **Grouped, ranked results** — separate sections for SOCKS5, SOCKS4, HTTP,
  residential, datacenter, and rotating; **fastest ping first**; top proxies
  highlighted; full `.txt` export attached.
- **Auto-forwarding** — live proxies pushed to your group and admin DM.
- **Advanced admin panel** (`/admin`) — inline-keyboard control: stats, user
  leaderboard, toggle forwarding (group / admin / residential-only), broadcast
  to all users, ban/unban, and live health.
- **Render 24/7 keep-alive** — built-in health web server + internal
  self-pinger so the free instance never idles out.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/start`, `/help` | everyone | intro + usage |
| *(paste / upload)* | everyone | check proxies |
| `/admin`, `/panel` | admin | open the control panel |
| `/ban <id>` / `/unban <id>` | admin | manage users |
| `/cancel` | admin | abort a broadcast |

## Configuration

All config is via environment variables (see `.env.example`):

| Var | Required | Default | Notes |
| --- | --- | --- | --- |
| `BOT_TOKEN` | ✅ | — | from @BotFather |
| `GROUP_ID` | ✅ | `-1004358364327` | forward target group |
| `ADMIN_ID` | ✅ | `5010778910` | admin user id |
| `RENDER_EXTERNAL_URL` | ⚠️ | — | your public Render URL; enables self-ping |
| `PORT` | auto | `10000` | health server port (Render sets it) |
| `KEEPALIVE_INTERVAL` | | `480` | seconds between self-pings |
| `MAX_CONCURRENCY` | | `200` | max simultaneous checks |
| `CHECK_TIMEOUT` | | `12` | per-proxy timeout (s) |
| `TOP_N` | | `10` | top proxies shown per category |

## Run locally

```bash
cd telegram-proxy-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in BOT_TOKEN
python main.py
```

## Deploy on Render (24/7)

1. Push this folder to a GitHub repo.
2. In Render, **New → Blueprint** and point it at the repo (it reads
   `render.yaml`), **or** create a **Web Service** manually with:
   - Build: `pip install -r requirements.txt`
   - Start: `python main.py`
   - Health check path: `/health`
3. Set the `BOT_TOKEN` env var (mark as secret). `GROUP_ID` / `ADMIN_ID` are
   pre-filled in `render.yaml` — adjust if needed.
4. Deploy. The bot messages the admin when it comes online.

### ⚠️ Staying awake on the free tier (important)

Render's free web services sleep after ~15 minutes without inbound HTTP traffic.
This bot fights that on two fronts:

1. **Internal self-ping** — every `KEEPALIVE_INTERVAL` seconds (default 8 min)
   the bot hits its own `RENDER_EXTERNAL_URL/health`, which counts as inbound
   traffic and resets the idle timer. Render injects `RENDER_EXTERNAL_URL`
   automatically for web services.

2. **External monitor (recommended, most reliable)** — an internal self-ping
   can miss if the instance is *already* asleep when the timer fires. For true
   24/7 uptime, add a free external monitor that hits your health endpoint:
   - [UptimeRobot](https://uptimerobot.com) → HTTP(s) monitor →
     `https://<your-app>.onrender.com/health` → interval **5 minutes**.
   - or [cron-job.org](https://cron-job.org) with the same URL every 5 min.

   The health server exists precisely so an external pinger has a cheap,
   fast endpoint to hit.

> Note: the free tier still has a monthly runtime cap. For guaranteed
> always-on with zero cold starts, Render's paid Starter plan removes sleeping
> entirely — but the setup above keeps the free tier effectively 24/7.

## Security note

Your bot token controls the entire bot. It was shared in plain chat during
setup — **revoke and regenerate it in @BotFather**, then set the new token as
the `BOT_TOKEN` secret in Render.

## Project layout

```
telegram-proxy-bot/
├── main.py         # entry point, handler wiring
├── config.py       # env config
├── parser.py       # messy-input proxy extraction
├── checker.py      # async checking, classification, ranking
├── formatting.py   # Telegram message builders
├── handlers.py     # commands + proxy-check flow
├── admin.py        # inline-keyboard admin panel
├── storage.py      # JSON persistence (users, settings, stats)
├── keepalive.py    # health web server + Render self-ping
├── render.yaml     # Render blueprint
└── requirements.txt
```
