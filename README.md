# HeroSMS Reseller Telegram Bot

A Telegram bot that **resells HeroSMS virtual numbers** to your customers. You hold
one HeroSMS API key; each Telegram user gets an in-bot wallet, picks a service +
country, buys a number, and the SMS code is delivered to them automatically. Your
selling price = HeroSMS cost × your markup.

Built with **Python + aiogram 3**, SQLite, and **Crypto Pay (@CryptoBot)** for
automatic crypto top-ups.

## Features

- 📲 Buy a number: pick a service (Telegram, WhatsApp, Instagram, OpenAI, …) and a
  country, with live price + stock pulled from HeroSMS.
- 💬 Automatic SMS code delivery (background poller) — no manual checking.
- 👛 Per-user wallet with **automatic crypto top-up** (USDT etc. via @CryptoBot).
- ♻️ Automatic **refund** when an order is cancelled or expires without a code.
- 📈 Admin-configurable **percentage markup** (live, no restart).
- 🛠 Admin panel: stats (revenue / cost / profit), manual top-up, set markup, broadcast.
- 🔒 Money-safe: atomic debits (a user can never go negative), top-ups credited once.

## How the HeroSMS integration works

HeroSMS exposes an **SMS-Activate-compatible** API:

```
GET https://hero-sms.com/stubs/handler_api.php?api_key=KEY&action=ACTION&...params
```

The bot uses: `getBalance`, `getNumberV2`, `getStatus`, `setStatus`, `getPrices`,
`getCountries`, `getServicesList`. See `herosms/client.py`.

> Tip: any software that supports SMS-Activate works with HeroSMS by replacing the
> host `https://api.sms-activate.ae` with `https://hero-sms.com` and using your
> HeroSMS API key.

## Setup

1. **Install Python 3.11+** and the dependencies:

   ```bash
   python -m venv .venv
   # Windows:  .venv\Scripts\activate
   # Linux/Mac: source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Create the bot** with [@BotFather](https://t.me/BotFather) and copy the token.

3. **Get your HeroSMS API key** from your HeroSMS account dashboard.

4. **(Optional) Crypto payments:** open [@CryptoBot](https://t.me/CryptoBot) →
   *Crypto Pay* → *Create App* → copy the API token. For testing use
   [@CryptoTestnetBot](https://t.me/CryptoTestnetBot) and set `CRYPTOBOT_TESTNET=true`.

5. **Configure:** copy `.env.example` to `.env` and fill it in:

   ```bash
   cp .env.example .env
   ```

   Required: `BOT_TOKEN`, `HEROSMS_API_KEY`, `ADMIN_IDS` (your Telegram user id).

6. **Run:**

   ```bash
   python bot.py
   ```

   Open your bot in Telegram and send `/start`.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Register + open the main menu |
| `/menu` | Re-open the main menu |
| `/balance` | Show your wallet balance |
| `/help` | How the bot works |

The buy flow is **service → country → confirmation screen → buy**. The confirmation
screen shows the price, your balance and stock before any charge, so a stray tap
never spends money.

## Deployment (run it 24/7)

A reseller bot must stay online. Two easy options:

**Docker (recommended):**

```bash
# on a small VPS, with your filled-in .env in the folder
docker compose up -d --build
docker compose logs -f        # watch it connect
```

The SQLite database is stored in `./data` (a mounted volume) so it survives restarts.

**Plain Python on a VPS (systemd):** create `/etc/systemd/system/smsbot.service`:

```ini
[Unit]
Description=HeroSMS reseller bot
After=network.target

[Service]
WorkingDirectory=/opt/smsbot
ExecStart=/opt/smsbot/.venv/bin/python bot.py
Restart=always
User=smsbot

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now smsbot` and `journalctl -u smsbot -f` to watch logs.

## Configuration reference (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `ADMIN_IDS` | — | Comma-separated admin Telegram user IDs |
| `HEROSMS_API_KEY` | — | Your HeroSMS API key |
| `HEROSMS_BASE_URL` | `https://hero-sms.com/stubs/handler_api.php` | API endpoint |
| `MARKUP_PERCENT` | `30` | Selling markup over HeroSMS cost (editable in admin panel) |
| `CRYPTOBOT_TOKEN` | _empty_ | Crypto Pay token; empty disables crypto top-ups |
| `CRYPTOBOT_TESTNET` | `false` | Use the @CryptoTestnetBot network |
| `CRYPTOBOT_ASSET` | `USDT` | Asset customers pay in |
| `ORDER_TIMEOUT_MIN` | `20` | Minutes to wait for a code before auto-refund |
| `POLL_INTERVAL_SEC` | `5` | How often to poll HeroSMS for codes |
| `PAYMENT_POLL_INTERVAL_SEC` | `12` | How often to poll for paid invoices |
| `MIN_TOPUP` | `1` | Minimum top-up (USD) |
| `DB_URL` | `sqlite+aiosqlite:///smsbot.db` | Database URL |

## Admin panel

Send `/start` as an admin, then **🛠 Admin**:

- **📊 Stats** — users, customer balances, gross sales, HeroSMS cost, profit, and your
  live HeroSMS master-account balance.
- **💵 Give balance** — `user_id amount` (negative to deduct). Works without crypto.
- **📈 Set markup** — change the markup % live.
- **📣 Broadcast** — message all users.

## Pricing model

`customer price = round_up(herosms_cost × (1 + markup% / 100))`

The markup is rounded **up** to the cent so a rounding edge never eats your margin.
Make sure your HeroSMS master account stays funded — that's what actually buys the
numbers your customers order.

## Project layout

```
bot.py                 entrypoint: wires clients, starts polling + background loops
config.py              env-based settings
herosms/client.py      HeroSMS (SMS-Activate-compatible) API client
db/                    SQLAlchemy models + async repo (users, orders, payments)
services/
  catalog.py           cached services/countries/prices
  pricing.py           markup + selling price
  orders.py            purchase / cancel / refund / complete lifecycle
  payments/cryptobot.py Crypto Pay client
  pollers.py           code delivery, refunds, payment crediting
handlers/              aiogram routers: start, buy, orders, wallet, admin
keyboards/             inline keyboards + callback factories
```

## Notes & next steps

- Payment status is **polled** (no public webhook needed). HeroSMS also supports
  webhooks for incoming SMS — you can wire those later to make delivery instant
  instead of polling.
- The payment layer is abstracted; **Cryptomus** (which HeroSMS itself uses) can be
  added as a second provider alongside Crypto Pay.
- Default storage is SQLite — fine for a single instance. For scale, point `DB_URL`
  at PostgreSQL (`postgresql+asyncpg://…`) and switch the FSM storage to Redis.

## Disclaimer

You are responsible for complying with HeroSMS's terms, your local laws, and for
how customers use the numbers.
