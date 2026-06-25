# Dmitry

Dmitry is a Python-based automated trading system built to explore real-time decision logic, API integration, and system reliability, all while pushing the limits of Ai assisted development.  
The project focuses on software architecture, automation, logging, and fault tolerance rather than trading performance.

> **Note:** This project is for educational and experimental purposes only.

---

## Overview

Dmitry is designed as a continuously running service that:
- Interfaces with the **Kraken** exchange API for market data and order execution
- Operates in both **simulation** and **live** modes
- Executes decisions based on configurable, indicator-driven thresholds
- Logs all activity to Google Sheets for later analysis and verification
- Sends automated email alerts and hourly health heartbeats

The primary goal of the project is to practice building **maintainable, testable, and resilient software systems**.

---

## Key Features

- **Python-based architecture** in a single, self-contained module
- **Simulation mode** for safe testing and validation (auto-engages when no API key is present)
- **Live mode** using real Kraken account balances
- **Candle-based indicators** — EMA, RSI, ATR, and volatility computed on 1-minute OHLC candles
- **Multi-pair trading** across XRP, ETH, BTC, and SOL with up to 3 concurrent positions
- **Automated logging** to Google Sheets for traceability (auto-stamped with the active code version)
- **Email alerts** for trade execution, errors, and system heartbeat
- **Graceful error handling**, crash notification, and open-position recovery on restart
- **Config-driven behavior** (no hardcoded parameters in the trading logic)

---

## Tech Stack

- **Python 3** (uses `dataclasses`, `typing`, `collections` from the standard library)
- [`krakenex`](https://github.com/veox/python3-krakenex) — Kraken REST API client
- [`gspread`](https://github.com/burnash/gspread) + `oauth2client` — Google Sheets trade logging
- `smtplib` / `email` — SMTP email alerts (standard library)

Install the third-party dependencies with:

```bash
pip install krakenex gspread oauth2client
```

---

## Trading Strategy

Dmitry implements a trend-following, dip-buying strategy. All indicators are computed on **1-minute OHLC candles** (not raw ticks) to reduce noise.

**Entry** (a pair must pass every filter):
- **Bull regime only** — fast EMA (20) above all slow EMAs (50 / 100 / 200), with a slope check
- **RSI filter** — RSI(14) below 50 (buy pullbacks, not overbought peaks)
- **Volatility cap** — skip entries when per-candle volatility is too high
- **Dip threshold** — price has pulled back a minimum % below its rolling 20-candle peak
- **Bounce confirmation** — requires consecutive rising candle closes before buying
- **Time-of-day filter** — no new entries during low-liquidity UTC hours (22:00–02:00)

**Exit** (per position):
- **Trailing take-profit** — arms once price clears the profit threshold, then trails the peak and sells on a defined retracement (lets winners ride)
- **ATR trailing stop** — 2.5× ATR below the running high, with a nose-dive confirmation so normal volatility doesn't trigger it
- **Hard stop floor** — a catastrophic-drop safeguard a fixed % below entry that always exits

**Position sizing & risk:**
- Volatility-adjusted capital fraction per trade
- Up to **3 concurrent positions** across uncorrelated pairs, each consuming ~1/N of free fiat
- **Circuit breaker** — two consecutive trailing-stop exits trigger a multi-hour cooldown

---

## System Architecture

- Core logic written in Python, organized into focused components:
  - `KrakenClient` — market data, balances, and order execution (with retry/back-off)
  - `TradingBot` — indicators, regime detection, entry/exit logic, and the main loop
  - `SheetsLogger` — appends every trade to Google Sheets
  - `Notifier` — SMTP email alerts and heartbeats
  - `Position` — per-pair position state (entry, high-water mark, profit-lock)
- Separation of concerns between decision logic, execution, and logging/notifications
- Designed to run unattended for extended periods, recovering open positions after a restart

---

## Getting Started

### 1. Credentials

Dmitry expects three credential files in the project directory. **All are gitignored and must never be committed.**

| File              | Purpose                          | Format                                                                 |
| ----------------- | -------------------------------- | ---------------------------------------------------------------------- |
| `kraken.key`      | Kraken API access (live mode)    | krakenex format: API key on line 1, private key on line 2              |
| `email.key`       | SMTP email alerts                | `EMAIL_SENDER=...`, `EMAIL_PASSWORD=...`, `EMAIL_RECEIVER=...` (one per line) |
| `google_key.json` | Google Sheets service account    | Google service-account JSON key                                        |

If `kraken.key` is missing, Dmitry automatically forces **simulation mode**. If `email.key` is missing or incomplete, email alerts are disabled gracefully.

### 2. Google Sheet

Create a Google Sheet named `Dmitry_trades` and share it with the service account's email. Dmitry creates a `Trades` worksheet on first run with columns:

`Time | Type | Price | Volume | Fiat | Crypto | Note | Push Date`

### 3. Run

```bash
python Dmitry.py
```

Stop with `Ctrl-C` (sends a "Dmitry Stopped" alert and exits cleanly).

---

## Configuration

All behavior is controlled by constants in the `CONFIG` section at the top of `Dmitry.py`. Notable options:

- `SIMULATION` — force simulation mode regardless of credentials
- `PAIRS` — the tradeable Kraken pairs and their balance keys
- `MAX_CONCURRENT_POSITIONS` — number of simultaneous open positions
- EMA / RSI / ATR / volatility periods and thresholds
- `MIN_HOLD_SECONDS`, `COOLDOWN_HOURS`, and the low-liquidity UTC window
- `EMAIL_ALERTS`, `HEARTBEAT_ENABLED`, and SMTP settings
- `BOT_VERSION` — version stamp written to each logged trade (bump on every push)

---

## Why This Project Exists

This project was built to strengthen skills in:
- API integration
- State management
- Automation and monitoring
- Defensive programming
- Long-running service design

It is **not** intended as a financial product or recommendation.

---

## Status

Active personal project.  
Continuously refactored and improved as part of ongoing software development practice.

---

## Disclaimer

This project is provided as-is for educational purposes only.  
No financial advice is given or implied. Automated trading carries significant financial risk; use live mode entirely at your own risk.
