import krakenex
import time
from datetime import datetime, timedelta
import traceback
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import deque
import smtplib
from email.mime.text import MIMEText
from typing import Optional
import os

SIMULATION = False

# Trading
symbol = 'XRPUSD'
pair = 'XXRPZUSD'
buy_drop_threshold = 0.005
sell_rise_threshold = 0.01  # 1% rise from buy-in price
trend_window = 5
trend_tolerance = 0.0001
FEE_PCT_POINTS = 0.8  # Net P/L = Gross P/L - 0.8%

# Profitability tuning
EMA_FAST_PERIOD = 20
EMA_SLOW_PERIOD = 100
VOL_LOOKBACK = 30
MAX_ENTRY_VOL = 0.03  # avoid entries during very high short-term volatility
BASE_RISK_FRACTION = 0.35  # baseline fraction of fiat deployed per trade
MAX_RISK_FRACTION = 0.75
MIN_RISK_FRACTION = 0.20
MIN_NET_TARGET = 0.012  # target at least 1.2% to clear fees/slippage

last_balance_check = 0
balance_cache = (0.0, 0.0)
# Google Sheets
GOOGLE_KEY_FILE = 'google_key.json'
GOOGLE_SHEET_NAME = 'Dmitry_trades'

# Email alerts (credentials loaded from email.key)
EMAIL_ALERTS = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SEND_STARTUP_EMAIL = True

# Heartbeat
HEARTBEAT_ENABLED = True
HEARTBEAT_SUBJECT = "Dmitry Heartbeat"
HEARTBEAT_BODY = "Dmitry is still running."
# ========================

# ----- STATE -----
sim_fiat = 1000.0
sim_crypto = 0.0
entry_price = None
trade_count = 0

recent_changes = deque(maxlen=trend_window)
price_history = deque(maxlen=EMA_SLOW_PERIOD + 5)
mode = 'waiting'
peak_price = 0.0

# ========================
# ----- HELPERS -----
# ========================


def load_email_keys(file_path: str = 'email.key'):
    keys = {}
    try:
        with open(file_path, 'r') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    keys[k.strip()] = v.strip()
    except FileNotFoundError:
        print("⚠️ email.key file not found! Email alerts will be disabled.")
        return None
    return keys


email_keys = load_email_keys()
if email_keys:
    EMAIL_SENDER = email_keys.get('EMAIL_SENDER', '')
@@ -307,68 +319,70 @@ def place_order(order_type, volume, max_retries=3):
                'ordertype': 'market',
                'volume': str(volume)
            })

            if response.get('error'):
                print(f"Kraken error on attempt {attempt + 1}: {response['error']}")
                if any("EAPI:Rate limit exceeded" in err for err in response['error']):
                    wait_time = 2 ** attempt
                    print(f"Rate limit hit. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    send_alert(f"Dmitry {order_type.upper()} Error", f"Kraken error:\n{response['error']}")
                    return None

            return response  # Success
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            print(f"Exception during order (attempt {attempt + 1}): {err}")
            time.sleep(2 ** attempt)

    send_alert(f"Dmitry {order_type.upper()} FAILED", f"Order failed after {max_retries} attempts.")
    return None


def buy_all(fiat_balance, price):
def buy_all(fiat_balance, price, allocation_fraction=1.0):
    global sim_fiat, sim_crypto, entry_price
    BUFFER = 0.995  # use ~99.5% of available fiat to avoid EOrder:Insufficient funds

    if SIMULATION:
        volume = round((fiat_balance * BUFFER) / price, 6)
        capital_to_use = fiat_balance * max(0.0, min(1.0, allocation_fraction))
        volume = round((capital_to_use * BUFFER) / price, 6)
        sim_crypto = volume
        sim_fiat = fiat_balance - (volume * price)
        entry_price = price
        crypto_display = sim_crypto
        fiat_display = sim_fiat
        actual_price = price
    else:
        fiat_before, crypto_before = get_balance()
        if fiat_before is None:
            fiat_before = 0.0

        volume = round((fiat_before * BUFFER) / price, 6)
        capital_to_use = fiat_before * max(0.0, min(1.0, allocation_fraction))
        volume = round((capital_to_use * BUFFER) / price, 6)
        response = place_order('buy', volume)

        if (not response) or ('error' in response and response['error']):
            error_msg = (
                f"Buy order failed!\n"
                f"Volume: {volume}\n"
                f"Fiat Before: {fiat_before:.2f}\n"
                f"Pair: {pair}\n"
                f"Kraken Response: {response}\n"
                f"Time: {datetime.now()}"
            )
            print("⚠️ " + error_msg)
            send_alert("Dmitry Buy Order Failed", error_msg)
            return

        fiat_after, crypto_after = get_balance()
        if fiat_after is None or crypto_after is None:
            fiat_after, crypto_after = get_balance()

        # Actual execution price
        actual_price = (fiat_before - fiat_after) / crypto_after if (crypto_after and crypto_after > 0) else price
        entry_price = actual_price

        crypto_display = crypto_after
        fiat_display = fiat_after
@@ -427,110 +441,169 @@ def sell_all(crypto_balance, price):
        sim_fiat = fiat_after
        sim_crypto = crypto_after

    mode = 'waiting'
    entry_price = None
    trade_count += 1

    # Log the trade to Google Sheets
    log_trade("SELL", price, crypto_balance, fiat_display, crypto_display)

    body = f"""Dmitry made a sell!

Trade #: {trade_count}
Pair: {pair}
Sell Price: {price:.6f}
New Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}
Time: {datetime.now()}"""

    send_email("Dmitry made a sell!", body)


def is_downtrend(prices, tolerance=0.0001):
    return all(prices[i] > prices[i + 1] + tolerance for i in range(len(prices) - 1))


def compute_ema(prices, period):
    if len(prices) < period:
        return None
    values = list(prices)[-period:]
    k = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * k) + (ema * (1 - k))
    return ema


def realized_volatility(prices, lookback=30):
    if len(prices) < lookback + 1:
        return None
    values = list(prices)[-(lookback + 1):]
    returns = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        curr = values[i]
        if prev > 0:
            returns.append(abs((curr - prev) / prev))
    if not returns:
        return None
    return sum(returns) / len(returns)


def get_dynamic_thresholds(vol):
    if vol is None:
        return buy_drop_threshold, max(sell_rise_threshold, MIN_NET_TARGET)

    dynamic_buy = max(buy_drop_threshold, vol * 1.6)
    dynamic_sell = max(sell_rise_threshold, MIN_NET_TARGET, vol * 2.0)
    return dynamic_buy, dynamic_sell


def position_size_fraction(vol):
    if vol is None:
        return BASE_RISK_FRACTION

    # deploy less capital as volatility rises
    scaled = BASE_RISK_FRACTION * (0.015 / max(vol, 0.002))
    return min(MAX_RISK_FRACTION, max(MIN_RISK_FRACTION, scaled))


# ========================
# ----- MAIN LOOP -----
# ========================
if SEND_STARTUP_EMAIL:
    send_alert("Dmitry Started", "Dmitry just started successfully.")

next_heartbeat = next_top_of_hour()

try:
    while True:
        now = datetime.now()

        if HEARTBEAT_ENABLED and now >= next_heartbeat:
            send_email(HEARTBEAT_SUBJECT, f"{HEARTBEAT_BODY}\nTime: {now}")
            next_heartbeat = next_top_of_hour(now)

        price = get_price()
        if price is None:
            time.sleep(2)
            continue

        price_history.append(price)

        ema_fast = compute_ema(price_history, EMA_FAST_PERIOD)
        ema_slow = compute_ema(price_history, EMA_SLOW_PERIOD)
        vol = realized_volatility(price_history, VOL_LOOKBACK)
        dynamic_buy_drop, dynamic_sell_rise = get_dynamic_thresholds(vol)

        fiat, crypto = get_balance()

        if mode == 'holding' and crypto <= 0.01:  # 0.01 XRP threshold
            print(f"⚠️ Safety reset: In holding mode but crypto balance is {crypto:.8f}")
            mode = 'waiting'
            entry_price = None


        if mode == 'waiting':
            if peak_price == 0.0:
                peak_price = price
            else:
                peak_price = max(peak_price, price)

            dip_triggered = peak_price and (peak_price - price) / peak_price >= buy_drop_threshold
            dip_triggered = peak_price and (peak_price - price) / peak_price >= dynamic_buy_drop
            trend_ok = (ema_fast is not None and ema_slow is not None and ema_fast > ema_slow and price > ema_slow)
            vol_ok = (vol is None) or (vol <= MAX_ENTRY_VOL)
            allocation_fraction = position_size_fraction(vol)
            if dip_triggered:
                debug_msg = (
                    f"Dmitry dip check triggered:\n"
                    f"Price: {price:.6f}\n"
                    f"Peak: {peak_price:.6f}\n"
                    f"Dip %: {(peak_price - price) / peak_price:.4%}\n"
                    f"Required Dip %: {dynamic_buy_drop:.4%}\n"
                    f"Trend OK: {trend_ok}\n"
                    f"Volatility: {vol if vol is not None else 'N/A'}\n"
                    f"Vol OK: {vol_ok}\n"
                    f"Capital Fraction: {allocation_fraction:.2%}\n"
                    f"Fiat balance: {fiat:.2f}\n"
                    f"Time: {datetime.now()}"
                )
                send_email("Dmitry Dip Triggered ", debug_msg)

            if dip_triggered and fiat > 1:
            if dip_triggered and trend_ok and vol_ok and fiat > 1:
                pre_fiat, pre_crypto = get_balance()
                buy_all(fiat, price)
                buy_all(fiat, price, allocation_fraction=allocation_fraction)
                post_fiat, post_crypto = get_balance()

                if abs(pre_fiat - post_fiat) < 1e-6:
                    send_alert("Dmitry Warning", "Buy trade may have failed — still in waiting mode.")
                else:
                    mode = 'holding'
                    peak_price = price

        elif mode == 'holding':
            if price >= entry_price * (1 + sell_rise_threshold):
            if price >= entry_price * (1 + dynamic_sell_rise):
                pre_fiat, pre_crypto = get_balance()
                sell_all(crypto, price)
                post_fiat, post_crypto = get_balance()

                if abs(pre_crypto - post_crypto) < 1e-6:
                    send_alert("Dmitry Warning", "Sell trade may have failed — still in holding mode.")
                else:
                    mode = 'waiting'
                    peak_price = price

        if len(recent_changes) == trend_window:
            recent_changes.popleft()
        if peak_price != 0:
            recent_changes.append((price - peak_price) / peak_price)

        time.sleep(1)

except KeyboardInterrupt:
    send_alert("Dmitry Stopped", "Dmitry was manually stopped by the user.")

except Exception as e:
    err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    send_alert("Dmitry Crashed!", f"Dmitry crashed with error:\n\n{err}")
    raise
