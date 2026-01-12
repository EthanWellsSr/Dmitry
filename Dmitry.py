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
    EMAIL_PASSWORD = email_keys.get('EMAIL_PASSWORD', '')
    EMAIL_RECEIVER = email_keys.get('EMAIL_RECEIVER', '')
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        print("⚠️ email.key incomplete. Email alerts will be disabled.")
        EMAIL_ALERTS = False
else:
    EMAIL_ALERTS = False
    EMAIL_SENDER = EMAIL_PASSWORD = EMAIL_RECEIVER = ""


def send_email(subject: str, message: str):
    if not EMAIL_ALERTS:
        return
    try:
        msg = MIMEText(message)
        msg['Subject'] = subject
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"📧 Email sent: {subject}")
    except Exception as e:
        print(f"⚠️ Failed to send email: {e}")


def send_alert(subject: str, message: str):
    send_email(subject, message)


def next_top_of_hour(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        dt = datetime.now()
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def kraken_api_call(method, endpoint, data=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            if method == 'public':
                response = api.query_public(endpoint, data or {})
            elif method == 'private':
                response = api.query_private(endpoint, data or {})
            else:
                raise ValueError("Invalid Kraken API method")

            if response.get("error"):
                if any("EAPI:Rate limit exceeded" in err for err in response["error"]):
                    wait_time = 2 ** attempt
                    print(f"Rate limit exceeded. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    return response
            return response
        except Exception as e:
            print(f"Kraken API {method}::{endpoint} failed on attempt {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    print(f"Kraken API {method}::{endpoint} failed after {max_retries} attempts.")
    return None


# ----- API SETUP -----
api = krakenex.API()
try:
    api.load_key('kraken.key')
except FileNotFoundError:
    print("⚠️ No kraken.key file found. Running in SIMULATION mode.")
    SIMULATION = True


def get_price():
    res = kraken_api_call('public', 'Ticker', {'pair': pair})
    if not res or 'result' not in res:
        return None
    return float(res['result'][list(res['result'].keys())[0]]['c'][0])


def get_balance():
    global last_balance_check, balance_cache

    now = time.time()
    if now - last_balance_check < 10:  # Use cached value for 10 seconds
        return balance_cache

    res = kraken_api_call('private', 'Balance')
    if not res or 'result' not in res:
        print(f"⚠️ Failed to get balance response: {res}")
        return balance_cache  # Use last known balance on failure

    try:
        fiat = float(res['result'].get('ZUSD', 0.0))
        crypto = float(res['result'].get('XXRP', 0.0))
        balance_cache = (fiat, crypto)
        last_balance_check = now
        return balance_cache
    except Exception as e:
        print(f"⚠️ Balance parsing error: {e}")
        return balance_cache


# ========================
# ----- GOOGLE SHEETS -----
# ========================
gc = None
doc = None
trade_sheet = None

def init_gspread():
    global gc, doc, trade_sheet
    if gc is not None:
        return

    key_path = os.path.abspath(GOOGLE_KEY_FILE)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_name(key_path, scope)
    gc = gspread.authorize(credentials)

    doc = gc.open(GOOGLE_SHEET_NAME)

    try:
        trade_sheet = doc.worksheet("Trades")
    except gspread.WorksheetNotFound:
        trade_sheet = doc.add_worksheet("Trades", rows="1000", cols="10")
        trade_sheet.append_row(["Time", "Type", "Price", "Volume", "Fiat", "Crypto"])


def log_trade(trade_type, price, volume, fiat_after, crypto_after):
    init_gspread()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        timestamp,
        trade_type,
        round(price, 6),
        round(volume, 6),
        round(fiat_after, 6),
        round(crypto_after, 8)
    ]
    try:
        trade_sheet.append_row(row, value_input_option="RAW")
    except Exception as e:
        print(f"⚠️ Google Sheets trade log failed: {e}")
        send_alert("Trade Log Error", str(e))

# ========================
# ----- RECOVERY -----
# ========================

def get_last_buy_price():
    """Get the price of the most recent buy trade from Kraken trade history"""
    try:
        response = kraken_api_call('private', 'TradesHistory', {'trades': True, 'start': 0})
        if not response or 'result' not in response or 'trades' not in response['result']:
            print("⚠️ Could not get trade history")
            return None
        trades = response['result']['trades']
        sorted_trades = sorted(trades.items(), key=lambda x: float(x[1]['time']), reverse=True)
        for trade_id, trade_data in sorted_trades:
            if trade_data.get('pair') == pair and trade_data.get('type') == 'buy':
                buy_price = float(trade_data['price'])
                trade_time = datetime.fromtimestamp(float(trade_data['time']))
                print(f"📊 Found last buy: {buy_price:.6f} at {trade_time}")
                return buy_price
        print("⚠️ No recent XRP buy trades found in history")
        return None
    except Exception as e:
        print(f"⚠️ Error getting last buy price: {e}")
        return None


def recover_state_from_balance():
    """Recover trading state by checking actual Kraken balances and trade history"""
    global entry_price, mode
    try:
        fiat, crypto = get_balance()
        if crypto > 0.01:  # Using same threshold as your safety check
            mode = 'holding'
            for attempt in range(3):
                entry_price = get_last_buy_price()
                if entry_price:
                    print(f"🔁 Recovered mode: holding, crypto balance: {crypto:.6f}")
                    print(f"✅ Found last buy price from trade history: {entry_price:.6f}")
                    return
                else:
                    print(f"⚠️ Failed to get buy price, attempt {attempt + 1}/3")
                    if attempt < 2:
                        time.sleep(5)
            error_msg = (
                f"CRITICAL ERROR: Could not recover entry price!\n\n"
                f"Crypto balance: {crypto:.6f} XRP\n"
                f"Mode should be: holding\n"
                f"Failed after 3 attempts to get trade history.\n"
                f"Dmitry cannot continue safely without knowing the entry price.\n"
                f"Time: {datetime.now()}\n\n"
                f"Please check manually and restart Dmitry."
            )
            send_alert("Dmitry Recovery FAILED", error_msg)
            raise Exception("Failed to recover entry price after 3 attempts")
        else:
            mode = 'waiting'
            entry_price = None
            print(f"🔁 Recovered mode: waiting, fiat balance: {fiat:.2f}")
    except Exception as e:
        print(f"⚠️ Error recovering state from balance: {e}")
        error_msg = (
            f"CRITICAL ERROR: State recovery failed!\n\n"
            f"Error: {str(e)}\n"
            f"Time: {datetime.now()}\n\n"
            f"Dmitry cannot start safely."
        )
        send_alert("Dmitry Recovery ERROR", error_msg)
        raise


# Initialize logging sheet and recovery
recover_state_from_balance()

# ========================
# ----- CORE FUNCS -----
# ========================



def place_order(order_type, volume, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = api.query_private('AddOrder', {
                'pair': pair,
                'type': order_type,
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
    global sim_fiat, sim_crypto, entry_price
    BUFFER = 0.995  # use ~99.5% of available fiat to avoid EOrder:Insufficient funds

    if SIMULATION:
        volume = round((fiat_balance * BUFFER) / price, 6)
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
        sim_fiat = fiat_after
        sim_crypto = crypto_after

    # Log the trade to Google Sheets
    log_trade("BUY", actual_price, volume, fiat_display, crypto_display)

    # Email notification
    body = f"""Dmitry made a buy!

Pair: {pair}
Entry Price: {actual_price:.6f}
Volume Bought: {volume:.6f}
New Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}
Time: {datetime.now()}"""

    send_email("Dmitry made a buy!", body)



def sell_all(crypto_balance, price):
    """Sell all crypto_balance and email results without gross/net % calculations."""
    global sim_fiat, sim_crypto, trade_count, mode, entry_price

    if SIMULATION:
        sim_fiat = crypto_balance * price
        sim_crypto = 0.0
        fiat_display = sim_fiat
        crypto_display = sim_crypto
    else:
        fiat_before, _ = get_balance()
        if fiat_before is None:
            fiat_before = 0.0

        if not crypto_balance or crypto_balance <= 0:
            if mode == 'holding':
                mode = 'waiting'
                entry_price = None
                print("⚠️ State reset: No crypto to sell, switching to waiting mode")
            return

        if not place_order('sell', crypto_balance):
            send_email("Dmitry sell failed", "⚠️ Sell order failed.")
            return

        fiat_after, crypto_after = get_balance()
        if fiat_after is None:
            fiat_after = 0.0
        if crypto_after is None:
            crypto_after = 0.0

        fiat_display = fiat_after
        crypto_display = crypto_after
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
            if dip_triggered:
                debug_msg = (
                    f"Dmitry dip check triggered:\n"
                    f"Price: {price:.6f}\n"
                    f"Peak: {peak_price:.6f}\n"
                    f"Dip %: {(peak_price - price) / peak_price:.4%}\n"
                    f"Fiat balance: {fiat:.2f}\n"
                    f"Time: {datetime.now()}"
                )
                send_email("Dmitry Dip Triggered ", debug_msg)

            if dip_triggered and fiat > 1:
                pre_fiat, pre_crypto = get_balance()
                buy_all(fiat, price)
                post_fiat, post_crypto = get_balance()

                if abs(pre_fiat - post_fiat) < 1e-6:
                    send_alert("Dmitry Warning", "Buy trade may have failed — still in waiting mode.")
                else:
                    mode = 'holding'
                    peak_price = price

        elif mode == 'holding':
            if price >= entry_price * (1 + sell_rise_threshold):
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

