import krakenex
import time
from datetime import datetime, timedelta
import traceback
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import smtplib
from email.mime.text import MIMEText
from typing import Optional
import os

# ========================
# ----- CONFIG -----
# ========================

SIMULATION = False

PAIR = 'XXRPZUSD'
BUY_DROP_THRESHOLD = 0.005
SELL_RISE_THRESHOLD = 0.01

GOOGLE_KEY_FILE = 'google_key.json'
GOOGLE_SHEET_NAME = 'Dmitry_trades'

EMAIL_ALERTS = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SEND_STARTUP_EMAIL = True

HEARTBEAT_ENABLED = True
HEARTBEAT_SUBJECT = "Dmitry Heartbeat"
HEARTBEAT_BODY = "Dmitry is still running."


# ========================
# ----- NOTIFIER -----
# ========================

class Notifier:
    def __init__(self, key_file: str = 'email.key'):
        self.enabled = EMAIL_ALERTS
        self.sender = self.password = self.receiver = ""
        self._load_keys(key_file)

    def _load_keys(self, file_path: str):
        try:
            keys = {}
            with open(file_path) as f:
                for line in f:
                    if '=' in line:
                        k, v = line.strip().split('=', 1)
                        keys[k.strip()] = v.strip()
            self.sender = keys.get('EMAIL_SENDER', '')
            self.password = keys.get('EMAIL_PASSWORD', '')
            self.receiver = keys.get('EMAIL_RECEIVER', '')
            if not (self.sender and self.password and self.receiver):
                print("⚠️ email.key incomplete. Email alerts will be disabled.")
                self.enabled = False
        except FileNotFoundError:
            print("⚠️ email.key not found. Email alerts will be disabled.")
            self.enabled = False

    def send(self, subject: str, message: str):
        if not self.enabled:
            return
        try:
            msg = MIMEText(message)
            msg['Subject'] = subject
            msg['From'] = self.sender
            msg['To'] = self.receiver
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(self.sender, self.password)
                server.send_message(msg)
            print(f"📧 Email sent: {subject}")
        except Exception as e:
            print(f"⚠️ Failed to send email: {e}")


# ========================
# ----- SHEETS LOGGER -----
# ========================

class SheetsLogger:
    def __init__(self, notifier: Notifier):
        self.notifier = notifier
        self._sheet = None

    def _get_sheet(self):
        if self._sheet:
            return self._sheet
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            os.path.abspath(GOOGLE_KEY_FILE), scope
        )
        doc = gspread.authorize(creds).open(GOOGLE_SHEET_NAME)
        try:
            self._sheet = doc.worksheet("Trades")
        except gspread.WorksheetNotFound:
            self._sheet = doc.add_worksheet("Trades", rows="1000", cols="10")
            self._sheet.append_row(["Time", "Type", "Price", "Volume", "Fiat", "Crypto"])
        return self._sheet

    def log(self, trade_type: str, price: float, volume: float, fiat: float, crypto: float):
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_type,
            round(price, 6),
            round(volume, 6),
            round(fiat, 6),
            round(crypto, 8),
        ]
        try:
            self._get_sheet().append_row(row, value_input_option="RAW")
        except Exception as e:
            print(f"⚠️ Google Sheets log failed: {e}")
            self.notifier.send("Trade Log Error", str(e))


# ========================
# ----- KRAKEN CLIENT -----
# ========================

class KrakenClient:
    BALANCE_TTL = 10  # seconds

    def __init__(self):
        self.api = krakenex.API()
        self.forced_simulation = False
        try:
            self.api.load_key('kraken.key')
        except FileNotFoundError:
            print("⚠️ No kraken.key found. Forcing SIMULATION mode.")
            self.forced_simulation = True
        self._balance_cache: tuple[float, float] = (0.0, 0.0)
        self._last_balance_check = 0.0

    def _call(self, method: str, endpoint: str, data: dict = None, max_retries: int = 3):
        fn = self.api.query_public if method == 'public' else self.api.query_private
        for attempt in range(max_retries):
            try:
                response = fn(endpoint, data or {})
                if response.get('error'):
                    if any('EAPI:Rate limit exceeded' in e for e in response['error']):
                        wait = 2 ** attempt
                        print(f"Rate limit exceeded. Retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                return response
            except Exception as e:
                print(f"Kraken {method}::{endpoint} attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        print(f"Kraken {method}::{endpoint} failed after {max_retries} attempts.")
        return None

    def get_price(self) -> Optional[float]:
        res = self._call('public', 'Ticker', {'pair': PAIR})
        if not res or 'result' not in res:
            return None
        return float(res['result'][list(res['result'].keys())[0]]['c'][0])

    def get_balance(self) -> tuple[float, float]:
        now = time.time()
        if now - self._last_balance_check < self.BALANCE_TTL:
            return self._balance_cache
        res = self._call('private', 'Balance')
        if not res or 'result' not in res:
            print(f"⚠️ Failed to get balance: {res}")
            return self._balance_cache
        try:
            fiat = float(res['result'].get('ZUSD', 0.0))
            crypto = float(res['result'].get('XXRP', 0.0))
            self._balance_cache = (fiat, crypto)
            self._last_balance_check = now
            return self._balance_cache
        except Exception as e:
            print(f"⚠️ Balance parse error: {e}")
            return self._balance_cache

    def place_order(self, order_type: str, volume: float):
        return self._call('private', 'AddOrder', {
            'pair': PAIR,
            'type': order_type,
            'ordertype': 'market',
            'volume': str(volume),
        })

    def get_last_buy_price(self) -> Optional[float]:
        try:
            res = self._call('private', 'TradesHistory', {'trades': True, 'start': 0})
            if not res or 'result' not in res or 'trades' not in res['result']:
                print("⚠️ Could not get trade history")
                return None
            trades = res['result']['trades']
            for _, trade in sorted(trades.items(), key=lambda x: float(x[1]['time']), reverse=True):
                if trade.get('pair') == PAIR and trade.get('type') == 'buy':
                    price = float(trade['price'])
                    when = datetime.fromtimestamp(float(trade['time']))
                    print(f"📊 Found last buy: {price:.6f} at {when}")
                    return price
            print("⚠️ No recent buy trades found in history")
            return None
        except Exception as e:
            print(f"⚠️ Error getting last buy price: {e}")
            return None


# ========================
# ----- TRADING BOT -----
# ========================

def _next_top_of_hour(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        dt = datetime.now()
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


class TradingBot:
    BUY_BUFFER = 0.995  # use ~99.5% of fiat to avoid EOrder:Insufficient funds

    def __init__(self):
        self.notifier = Notifier()
        self.logger = SheetsLogger(self.notifier)
        self.kraken = KrakenClient()
        self.simulation = SIMULATION or self.kraken.forced_simulation

        self.mode = 'waiting'
        self.entry_price: Optional[float] = None
        self.peak_price = 0.0
        self.trade_count = 0

        # Only meaningful in simulation mode
        self._sim_fiat = 1000.0
        self._sim_crypto = 0.0

        self._recover_state()

    # ----- BALANCE -----

    def _get_balance(self) -> tuple[float, float]:
        if self.simulation:
            return self._sim_fiat, self._sim_crypto
        return self.kraken.get_balance()

    # ----- RECOVERY -----

    def _recover_state(self):
        fiat, crypto = self._get_balance()
        if crypto > 0.01:
            self.mode = 'holding'
            for attempt in range(3):
                self.entry_price = self.kraken.get_last_buy_price()
                if self.entry_price:
                    print(f"🔁 Recovered: holding, crypto={crypto:.6f}, entry={self.entry_price:.6f}")
                    return
                print(f"⚠️ Buy price recovery attempt {attempt + 1}/3 failed")
                if attempt < 2:
                    time.sleep(5)
            msg = (
                f"CRITICAL: Could not recover entry price!\n"
                f"Crypto: {crypto:.6f} XRP\n"
                f"Failed after 3 attempts.\nTime: {datetime.now()}\n\n"
                f"Please check manually and restart Dmitry."
            )
            self.notifier.send("Dmitry Recovery FAILED", msg)
            raise RuntimeError("Failed to recover entry price after 3 attempts")
        else:
            self.mode = 'waiting'
            self.entry_price = None
            print(f"🔁 Recovered: waiting, fiat={fiat:.2f}")

    # ----- TRADING -----

    def _buy(self, fiat_balance: float, price: float):
        if self.simulation:
            volume = round((fiat_balance * self.BUY_BUFFER) / price, 6)
            self._sim_crypto = volume
            self._sim_fiat = fiat_balance - (volume * price)
            self.entry_price = price
            actual_price = price
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            fiat_before, _ = self.kraken.get_balance()
            volume = round((fiat_before * self.BUY_BUFFER) / price, 6)
            response = self.kraken.place_order('buy', volume)
            if not response or response.get('error'):
                msg = (
                    f"Buy order failed!\nVolume: {volume}\nFiat Before: {fiat_before:.2f}\n"
                    f"Pair: {PAIR}\nResponse: {response}\nTime: {datetime.now()}"
                )
                print("⚠️ " + msg)
                self.notifier.send("Dmitry Buy Order Failed", msg)
                return
            fiat_after, crypto_after = self.kraken.get_balance()
            if fiat_after is None or crypto_after is None:
                fiat_after, crypto_after = self.kraken.get_balance()
            actual_price = (fiat_before - fiat_after) / crypto_after if crypto_after else price
            self.entry_price = actual_price
            fiat_display, crypto_display = fiat_after, crypto_after

        self.logger.log("BUY", actual_price, volume, fiat_display, crypto_display)
        self.notifier.send("Dmitry made a buy!", (
            f"Pair: {PAIR}\nEntry Price: {actual_price:.6f}\nVolume: {volume:.6f}\n"
            f"Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}\n"
            f"Time: {datetime.now()}"
        ))

    def _sell(self, crypto_balance: float, price: float):
        if self.simulation:
            self._sim_fiat = crypto_balance * price
            self._sim_crypto = 0.0
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            if not crypto_balance or crypto_balance <= 0:
                if self.mode == 'holding':
                    self.mode = 'waiting'
                    self.entry_price = None
                    print("⚠️ State reset: No crypto to sell")
                return
            response = self.kraken.place_order('sell', crypto_balance)
            if not response or response.get('error'):
                self.notifier.send("Dmitry sell failed", "⚠️ Sell order failed.")
                return
            fiat_after, crypto_after = self.kraken.get_balance()
            fiat_display = fiat_after or 0.0
            crypto_display = crypto_after or 0.0

        self.mode = 'waiting'
        self.entry_price = None
        self.trade_count += 1
        self.logger.log("SELL", price, crypto_balance, fiat_display, crypto_display)
        self.notifier.send("Dmitry made a sell!", (
            f"Trade #: {self.trade_count}\nPair: {PAIR}\nSell Price: {price:.6f}\n"
            f"Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}\n"
            f"Time: {datetime.now()}"
        ))

    # ----- MAIN LOOP -----

    def run(self):
        if SEND_STARTUP_EMAIL:
            self.notifier.send("Dmitry Started", "Dmitry just started successfully.")

        next_heartbeat = _next_top_of_hour()

        try:
            while True:
                now = datetime.now()

                if HEARTBEAT_ENABLED and now >= next_heartbeat:
                    self.notifier.send(HEARTBEAT_SUBJECT, f"{HEARTBEAT_BODY}\nTime: {now}")
                    next_heartbeat = _next_top_of_hour(now)

                price = self.kraken.get_price()
                if price is None:
                    time.sleep(2)
                    continue

                fiat, crypto = self._get_balance()

                if self.mode == 'holding' and crypto <= 0.01:
                    print(f"⚠️ Safety reset: holding mode but crypto={crypto:.8f}")
                    self.mode = 'waiting'
                    self.entry_price = None

                if self.mode == 'waiting':
                    self.peak_price = max(self.peak_price, price) if self.peak_price else price
                    dip_pct = (self.peak_price - price) / self.peak_price
                    dip_triggered = dip_pct >= BUY_DROP_THRESHOLD

                    if dip_triggered:
                        self.notifier.send("Dmitry Dip Triggered", (
                            f"Price: {price:.6f}\nPeak: {self.peak_price:.6f}\n"
                            f"Dip: {dip_pct:.4%}\nFiat: {fiat:.2f}\nTime: {now}"
                        ))

                    if dip_triggered and fiat > 1:
                        pre_fiat, _ = self._get_balance()
                        self._buy(fiat, price)
                        post_fiat, _ = self._get_balance()
                        if abs(pre_fiat - post_fiat) < 1e-6:
                            self.notifier.send("Dmitry Warning", "Buy may have failed — still in waiting mode.")
                        else:
                            self.mode = 'holding'
                            self.peak_price = price

                elif self.mode == 'holding' and self.entry_price:
                    if price >= self.entry_price * (1 + SELL_RISE_THRESHOLD):
                        _, pre_crypto = self._get_balance()
                        self._sell(crypto, price)
                        _, post_crypto = self._get_balance()
                        if abs(pre_crypto - post_crypto) < 1e-6:
                            self.notifier.send("Dmitry Warning", "Sell may have failed — still in holding mode.")
                        else:
                            self.mode = 'waiting'
                            self.peak_price = price

                time.sleep(1)

        except KeyboardInterrupt:
            self.notifier.send("Dmitry Stopped", "Dmitry was manually stopped.")
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self.notifier.send("Dmitry Crashed!", f"Dmitry crashed:\n\n{err}")
            raise


if __name__ == '__main__':
    TradingBot().run()
