# Dmitry — BTC Drawdown Watchtower

Dmitry is a **read-only** Bitcoin watchtower. He does not trade. He holds no
exchange keys, places no orders, and **cannot touch your money**. His one job is
to watch BTC 24/7 and push you an alert when the price has fallen far below its
1-year high — the "be greedy when others are fearful" moment — so that **you**
can decide whether to load cash and buy.

> **Note:** This is a personal, educational project. It is a monitoring/alerting
> tool, not financial advice and not a trading system.

---

## Why this exists (and what changed)

Dmitry used to be an active round-trip trading bot — EMAs, RSI, ATR trailing
stops, take-profits, a circuit breaker, multiple concurrent positions. It lost
money, aggressively, because it was trying to do the one thing that's nearly
impossible to do well: **time short-term entries and exits.**

This version throws all of that out. The insight behind the rewrite:

- **Timing tiny dips is futile**, but **recognizing a generational drawdown is not.**
  "BTC is 50% below its yearly high" is a real, rare, high-conviction signal.
- **You don't need to read the news.** The drawdown percentage *is* the panic
  signal — headlines just narrate a number you can compute directly from price.
- **A bot that can only *alert* cannot lose you money.** The worst it can do is
  send a wrong notification. So Dmitry is read-only by design.

The result is a tiny, robust program whose correct behavior most of the time is
to **do nothing and watch.**

---

## How it works

Every ~5 minutes Dmitry asks Kraken's **public** API for the current BTC price
and compares it to the **highest daily close over the last 365 days**:

```
drawdown = (trailing 1-year high − current price) / trailing 1-year high
```

When that drawdown crosses an escalating ladder of tiers, he sends **one
email alert per tier**:

| Tier   | Meaning                              |
| ------ | ------------------------------------ |
| −30%   | Notable dip                          |
| −40%   | Major drawdown                       |
| −50%   | Severe — "back up the truck"         |
| −60%   | Generational fear                    |
| −70%   | Historic capitulation                |

- **Daily-close high.** The reference is the highest daily *close* (not intraday
  wicks), so a one-second flash spike can't permanently inflate the baseline.
- **One alert per tier.** No spam while you sit in a drawdown; he only speaks
  when crossing into a *new, deeper* tier.
- **Re-arm after recovery.** Once BTC climbs back to within **15%** of the high,
  all tiers reset, so the *next* crash alerts you again. One clean cycle per crash.
- **False-alarm guard.** A tier must hold for **2 consecutive checks** before
  firing, and corrupt data (non-positive prices, or a >50% one-day jump in the
  reference high) is rejected — so a single bad tick can't trigger a false
  "back up the truck" alert.
- **−50% and deeper are flagged 🚨 in the subject** so the deep-crash alerts
  stand out in your inbox.

Dmitry only persists one piece of state — the deepest tier already alerted —
to a small `watchtower_state.json`, so restarts are invisible. The trailing high
is recomputed from Kraken on every startup.

---

## Architecture

A single, self-contained module (`Dmitry.py`) with four small components:

- `KrakenClient` — read-only access to Kraken's **public** market data (price +
  daily OHLC). No private endpoints, no keys, no orders.
- `Notifier` — sends alerts by email via **Gmail SMTP**.
- `StateStore` — persists the deepest-tier-alerted state across restarts.
- `Watchtower` — computes the drawdown, runs the tier/re-arm/confirmation logic,
  and drives the main loop.

---

## Tech Stack

- **Python 3** (standard library: `json`, `smtplib`, `email`, `urllib`, `datetime`, …)
- [`krakenex`](https://github.com/veox/python3-krakenex) — Kraken public REST client
- **Gmail SMTP** for email alerts (standard library — no extra dependency)

Install the one third-party dependency:

```bash
pip install krakenex
```

---

## Getting Started

### 1. Gmail alerts

Dmitry sends alerts by email via Gmail. You'll need a Gmail **app password**
(not your normal password — generate one at
<https://myaccount.google.com/apppasswords> with 2-Step Verification enabled).
Put your credentials in a **gitignored** `email.key` in the project directory:

```
EMAIL_SENDER=you@gmail.com
EMAIL_PASSWORD=your_gmail_app_password
EMAIL_RECEIVER=where_to_alert@example.com
```

If `email.key` is missing or incomplete, Dmitry runs in **print-only mode**
(alerts go to the console only) so you can test safely.

### 2. Dead-man's switch (recommended)

A self-sent heartbeat can never report its own death — if the process crashes or
the Pi loses power, no message goes out. So Dmitry pings an external monitor
instead. Create a free check at [healthchecks.io](https://healthchecks.io) (or
UptimeRobot) and paste its ping URL into `HEALTHCHECK_URL` in `Dmitry.py`. If the
pings stop, **that service** emails you that Dmitry went dark.

### 3. Run

```bash
python Dmitry.py
```

Stop with `Ctrl-C` (sends a "stopped" alert and exits cleanly).

### 4. Run it 24/7 on an always-on Ubuntu machine

For true set-and-forget, run Dmitry as a `systemd` service so he auto-restarts
on crash or reboot. Example unit (`/etc/systemd/system/dmitry.service`) — replace
`youruser` with your Ubuntu username:

```ini
[Unit]
Description=Dmitry BTC Watchtower
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/youruser/Dmitry
ExecStart=/usr/bin/python3 /home/youruser/Dmitry/Dmitry.py
Restart=always
RestartSec=10
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now dmitry
journalctl -u dmitry -f   # follow the logs
```

**Laptop note:** if Dmitry runs on a laptop, make sure it doesn't suspend when
idle or when the lid closes — otherwise it stops watching. The simplest fix:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

(or set *"When the lid is closed"* / *"Automatic Suspend"* to **Do Nothing** in
Settings → Power). The dead-man's switch will catch it if it sleeps anyway.

---

## Configuration

All behavior lives in the `CONFIG` section at the top of `Dmitry.py`:

- `PAIR` — Kraken pair to watch (default `XXBTZUSD`)
- `TRAILING_WINDOW_DAYS` — lookback for the reference high (default 365)
- `TIERS` — drawdown alert ladder (default `-30/-40/-50/-60/-70%`)
- `REARM_RECOVERY` — recovery band that resets the tiers (default 15%)
- `PRICE_POLL_SECONDS` — how often to check price (default 300)
- `HIGH_RECOMPUTE_SECONDS` — how often to refresh the trailing high (default daily)
- `CONFIRM_TICKS` — consecutive readings required before firing (default 2)
- `HEALTHCHECK_URL` — dead-man's-switch ping URL (blank = disabled)
- `BOT_VERSION` — version stamp

---

## What's deliberately *not* here

No order placement. No private keys. No balances. No stop-losses, take-profits,
or circuit breakers. No concurrent positions. Dmitry buys nothing and sells
nothing — that's the entire point. The hardest, highest-stakes decision (whether
to deploy real cash into a crash) stays with **you**; Dmitry just makes sure you
don't miss the moment.

---

## Disclaimer

This project is provided as-is for educational purposes only. It is a price
monitor, not financial advice. Any decision to buy or sell is entirely your own,
and crypto carries significant risk.
