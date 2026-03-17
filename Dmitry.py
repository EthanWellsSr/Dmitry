import krakenex
import time
from collections import deque
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

# EMA periods for trend / regime detection
EMA_FAST_PERIOD = 20
EMA_SLOW_PERIODS = [100, 200, 400]        # EMA20 is scored against all three
PRICE_HISTORY_SIZE = max(EMA_SLOW_PERIODS) + 5

# Volatility
VOL_LOOKBACK = 30
MAX_ENTRY_VOL = 0.04       # skip entry if realized vol is too high (chaotic market)
TARGET_VOL = 0.015         # reference vol for full base-size deployment

# Dynamic entry/exit thresholds (scale with realized volatility)
MIN_BUY_DIP = 0.004        # floor: require at least 0.4% dip from peak
VOL_DIP_MULT = 1.5         # buy_dip  = max(MIN_BUY_DIP,  vol * VOL_DIP_MULT)
MIN_SELL_RISE = 0.010      # floor: require at least 1.0% rise (covers fees + slippage)
VOL_SELL_MULT = 2.0        # sell_rise = max(MIN_SELL_RISE, vol * VOL_SELL_MULT)

# Position sizing (fraction of fiat deployed per trade, scales inversely with vol)
BASE_RISK_FRACTION = 0.50
MAX_RISK_FRACTION = 0.90
MIN_RISK_FRACTION = 0.20

# Exit
TRAILING_STOP_PCT = 0.018  # sell if price drops 1.8% below highest point since entry

# Google Sheets
GOOGLE_KEY_FILE = 'google_key.json'
GOOGLE_SHEET_NAME = 'Dmitry_trades'

# Email alerts
EMAIL_ALERTS = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SEND_STARTUP_EMAIL = True

# Heartbeat
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
            self._sheet.append_row(["Time", "Type", "Price", "Volume", "Fiat", "Crypto", "Note"])
        return self._sheet

    def log(self, trade_type: str, price: float, volume: float, fiat: float, crypto: float, note: str = ''):
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_type,
            round(price, 6),
            round(volume, 6),
            round(fiat, 6),
            round(crypto, 8),
            note,
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
        self.entry_high: float = 0.0   # highest price since entry (drives trailing stop)
        self.peak_price: float = 0.0   # highest price seen while waiting (drives dip detection)
        self.trade_count = 0
        self._dip_notified = False

        self.price_history: deque = deque(maxlen=PRICE_HISTORY_SIZE)

        # Only meaningful in simulation mode
        self._sim_fiat = 1000.0
        self._sim_crypto = 0.0

        self._recover_state()

    # ----- BALANCE -----

    def _get_balance(self) -> tuple[float, float]:
        if self.simulation:
            return self._sim_fiat, self._sim_crypto
        return self.kraken.get_balance()

    # ----- INDICATORS -----

    def _ema(self, period: int) -> Optional[float]:
        if len(self.price_history) < period:
            return None
        values = list(self.price_history)[-period:]
        k = 2 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _realized_vol(self) -> Optional[float]:
        if len(self.price_history) < VOL_LOOKBACK + 1:
            return None
        values = list(self.price_history)[-(VOL_LOOKBACK + 1):]
        returns = [abs((values[i] - values[i - 1]) / values[i - 1]) for i in range(1, len(values)) if values[i - 1] > 0]
        return sum(returns) / len(returns) if returns else None

    def _market_regime(self, ema_fast: Optional[float], slow_emas: list[Optional[float]]) -> tuple[str, int]:
        """
        Scores EMA20 against each of EMA100, EMA200, EMA400.
        +1 for each slow EMA that EMA20 is above, 0 otherwise.

        Score 3 → bull       (enter, full sizing)
        Score 2 → mild-bull  (enter, normal sizing)
        Score 1 → caution    (skip — short-term bounce in a downtrend)
        Score 0 → bear       (skip)
        unknown → not enough history yet (enter cautiously)
        """
        if ema_fast is None:
            return 'unknown', -1
        available = [s for s in slow_emas if s is not None]
        if not available:
            return 'unknown', -1
        score = sum(1 for s in available if ema_fast > s)
        if score == len(available):
            return 'bull', score
        if score >= len(available) - 1:
            return 'mild-bull', score
        if score == 0:
            return 'bear', score
        return 'caution', score

    def _dynamic_thresholds(self, vol: Optional[float]) -> tuple[float, float]:
        if vol is None:
            return MIN_BUY_DIP, MIN_SELL_RISE
        return max(MIN_BUY_DIP, vol * VOL_DIP_MULT), max(MIN_SELL_RISE, vol * VOL_SELL_MULT)

    def _position_fraction(self, vol: Optional[float]) -> float:
        """Deploy more capital in calm markets, less when volatile."""
        if vol is None:
            return BASE_RISK_FRACTION
        fraction = BASE_RISK_FRACTION * (TARGET_VOL / max(vol, 0.001))
        return min(MAX_RISK_FRACTION, max(MIN_RISK_FRACTION, fraction))

    # ----- RECOVERY -----

    def _recover_state(self):
        fiat, crypto = self._get_balance()
        if crypto > 0.01:
            self.mode = 'holding'
            for attempt in range(3):
                self.entry_price = self.kraken.get_last_buy_price()
                if self.entry_price:
                    self.entry_high = self.entry_price
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

    def _buy(self, fiat_balance: float, price: float, fraction: float = 1.0, regime: str = ''):
        capital = fiat_balance * max(0.0, min(1.0, fraction))
        if self.simulation:
            volume = round((capital * self.BUY_BUFFER) / price, 6)
            self._sim_crypto = volume
            self._sim_fiat = fiat_balance - (volume * price)
            self.entry_price = price
            actual_price = price
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            fiat_before, _ = self.kraken.get_balance()
            capital = fiat_before * max(0.0, min(1.0, fraction))
            volume = round((capital * self.BUY_BUFFER) / price, 6)
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

        self.entry_high = self.entry_price
        self.logger.log("BUY", actual_price, volume, fiat_display, crypto_display,
                        note=f"regime={regime}, frac={fraction:.0%}")
        self.notifier.send("Dmitry made a buy!", (
            f"Pair: {PAIR}\nRegime: {regime}\nEntry Price: {actual_price:.6f}\nVolume: {volume:.6f}\n"
            f"Capital Deployed: {fraction:.0%}\n"
            f"Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}\n"
            f"Time: {datetime.now()}"
        ))

    def _sell(self, crypto_balance: float, price: float, reason: str = 'take-profit'):
        if self.simulation:
            self._sim_fiat = crypto_balance * price
            self._sim_crypto = 0.0
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            if not crypto_balance or crypto_balance <= 0:
                if self.mode == 'holding':
                    self.mode = 'waiting'
                    self.entry_price = None
                    self.entry_high = 0.0
                    print("⚠️ State reset: No crypto to sell")
                return
            response = self.kraken.place_order('sell', crypto_balance)
            if not response or response.get('error'):
                self.notifier.send("Dmitry sell failed", "⚠️ Sell order failed.")
                return
            fiat_after, crypto_after = self.kraken.get_balance()
            fiat_display = fiat_after or 0.0
            crypto_display = crypto_after or 0.0

        pnl_pct = ((price - self.entry_price) / self.entry_price * 100) if self.entry_price else 0.0
        self.mode = 'waiting'
        self.entry_price = None
        self.entry_high = 0.0
        self.trade_count += 1
        self.logger.log("SELL", price, crypto_balance, fiat_display, crypto_display, note=reason)
        self.notifier.send(f"Dmitry sold ({reason})", (
            f"Trade #: {self.trade_count}\nPair: {PAIR}\nReason: {reason}\n"
            f"Sell Price: {price:.6f}\nP/L: {pnl_pct:+.2f}%\n"
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

                self.price_history.append(price)

                ema_fast = self._ema(EMA_FAST_PERIOD)
                slow_emas = [self._ema(p) for p in EMA_SLOW_PERIODS]
                vol = self._realized_vol()
                regime, regime_score = self._market_regime(ema_fast, slow_emas)
                buy_dip, sell_rise = self._dynamic_thresholds(vol)
                fraction = self._position_fraction(vol)
                vol_ok = vol is None or vol <= MAX_ENTRY_VOL

                fiat, crypto = self._get_balance()

                if self.mode == 'holding' and crypto <= 0.01:
                    print(f"⚠️ Safety reset: holding mode but crypto={crypto:.8f}")
                    self.mode = 'waiting'
                    self.entry_price = None
                    self.entry_high = 0.0

                if self.mode == 'waiting':
                    self.peak_price = max(self.peak_price, price) if self.peak_price else price
                    dip_pct = (self.peak_price - price) / self.peak_price
                    dip_triggered = dip_pct >= buy_dip

                    # Enter if EMA20 is above at least 2 of the 3 slow EMAs (or data is still warming up)
                    regime_ok = regime in ('bull', 'mild-bull', 'unknown')

                    if dip_triggered and not self._dip_notified:
                        self._dip_notified = True
                        ema_labels = [f"EMA{p}={f'{s:.4f}' if s else 'warming'}" for p, s in zip(EMA_SLOW_PERIODS, slow_emas)]
                        self.notifier.send("Dmitry Dip Triggered", (
                            f"Price: {price:.6f}\nPeak: {self.peak_price:.6f}\n"
                            f"Dip: {dip_pct:.4%} (threshold: {buy_dip:.4%})\n"
                            f"EMA{EMA_FAST_PERIOD}={f'{ema_fast:.4f}' if ema_fast else 'warming'}\n"
                            f"{chr(10).join(ema_labels)}\n"
                            f"Regime: {regime} (score {regime_score}/3, OK: {regime_ok})\n"
                            f"Vol: {f'{vol:.4f}' if vol else 'N/A'} (OK: {vol_ok})\n"
                            f"Capital Fraction: {fraction:.0%}\nFiat: {fiat:.2f}\nTime: {now}"
                        ))
                    elif not dip_triggered:
                        self._dip_notified = False

                    if dip_triggered and regime_ok and vol_ok and fiat > 1:
                        pre_fiat, _ = self._get_balance()
                        self._buy(fiat, price, fraction=fraction, regime=regime)
                        post_fiat, _ = self._get_balance()
                        if abs(pre_fiat - post_fiat) < 1e-6:
                            self.notifier.send("Dmitry Warning", "Buy may have failed — still in waiting mode.")
                        else:
                            self.mode = 'holding'
                            self.peak_price = price
                            self._dip_notified = False

                elif self.mode == 'holding' and self.entry_price:
                    self.entry_high = max(self.entry_high, price)
                    take_profit = price >= self.entry_price * (1 + sell_rise)
                    trailing_stop = price <= self.entry_high * (1 - TRAILING_STOP_PCT)

                    if take_profit or trailing_stop:
                        reason = 'take-profit' if take_profit else 'trailing-stop'
                        _, pre_crypto = self._get_balance()
                        self._sell(crypto, price, reason=reason)
                        _, post_crypto = self._get_balance()
                        if abs(pre_crypto - post_crypto) < 1e-6:
                            self.notifier.send("Dmitry Warning", f"Sell ({reason}) may have failed.")
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
