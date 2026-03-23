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

# All tradeable pairs: {kraken_pair: balance_key}
PAIRS = {
    'XXRPZUSD': 'XXRP',
    'XETHZUSD': 'XETH',
    'XXBTZUSD': 'XXBT',
    'SOLUSD':   'SOL',
}

# EMA periods for trend / regime detection
EMA_FAST_PERIOD = 20
EMA_SLOW_PERIODS = [100, 200, 400]        # EMA20 is scored against all three
PRICE_HISTORY_SIZE = max(EMA_SLOW_PERIODS) + 5

# EMA slope filter: EMA_FAST must be rising over this many ticks to confirm uptrend
EMA_SLOPE_LOOKBACK = 10

# ATR-based trailing stop
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0          # stop = entry_high - (ATR_STOP_MULT * atr)
TRAILING_STOP_PCT = 0.018    # fallback when ATR history not yet available

# Consecutive trailing-stop circuit breaker
CONSECUTIVE_STOP_LIMIT = 3   # number of trailing stops before cooldown
COOLDOWN_HOURS = 4           # how long to pause after hitting the limit

# Volatility
VOL_LOOKBACK = 30
MAX_ENTRY_VOL = 0.04         # skip entry if realized vol is too high (chaotic market)
TARGET_VOL = 0.015           # reference vol for full base-size deployment

# Dynamic entry/exit thresholds (scale with realized volatility)
MIN_BUY_DIP = 0.004          # floor: require at least 0.4% dip from peak
VOL_DIP_MULT = 1.5           # buy_dip  = max(MIN_BUY_DIP,  vol * VOL_DIP_MULT)
MIN_SELL_RISE = 0.010        # floor: require at least 1.0% rise (covers fees + slippage)
VOL_SELL_MULT = 2.0          # sell_rise = max(MIN_SELL_RISE, vol * VOL_SELL_MULT)

# Minimum order size per pair (Kraken requirements)
MIN_BUY_VOLUME = {
    'XXRPZUSD': 1.0,
    'XETHZUSD': 0.002,
    'XXBTZUSD': 0.0001,
    'SOLUSD':   0.02,
}

# Position sizing (fraction of fiat deployed per trade, scales inversely with vol)
BASE_RISK_FRACTION = 0.50
MAX_RISK_FRACTION = 0.90
MIN_RISK_FRACTION = 0.20

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
        self._balance_cache: dict[str, float] = {}
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

    def get_prices(self) -> dict[str, float]:
        """Fetch current prices for all pairs in one API call."""
        res = self._call('public', 'Ticker', {'pair': ','.join(PAIRS.keys())})
        if not res or 'result' not in res:
            return {}
        return {
            pair: float(data['c'][0])
            for pair, data in res['result'].items()
            if pair in PAIRS
        }

    def get_all_balances(self) -> dict[str, float]:
        now = time.time()
        if now - self._last_balance_check < self.BALANCE_TTL:
            return self._balance_cache
        res = self._call('private', 'Balance')
        if not res or 'result' not in res:
            print(f"⚠️ Failed to get balance: {res}")
            return self._balance_cache
        try:
            self._balance_cache = {k: float(v) for k, v in res['result'].items()}
            self._last_balance_check = now
            return self._balance_cache
        except Exception as e:
            print(f"⚠️ Balance parse error: {e}")
            return self._balance_cache

    def get_balance(self, crypto_key: str = '') -> tuple[float, float]:
        balances = self.get_all_balances()
        fiat = balances.get('ZUSD', 0.0)
        crypto = balances.get(crypto_key, 0.0) if crypto_key else 0.0
        return fiat, crypto

    def place_order(self, order_type: str, volume: float, pair: str):
        return self._call('private', 'AddOrder', {
            'pair': pair,
            'type': order_type,
            'ordertype': 'market',
            'volume': str(volume),
        })

    def get_last_buy_price(self, pair: str) -> Optional[float]:
        try:
            res = self._call('private', 'TradesHistory', {'trades': True, 'start': 0})
            if not res or 'result' not in res or 'trades' not in res['result']:
                print("⚠️ Could not get trade history")
                return None
            trades = res['result']['trades']
            for _, trade in sorted(trades.items(), key=lambda x: float(x[1]['time']), reverse=True):
                if trade.get('pair') == pair and trade.get('type') == 'buy':
                    price = float(trade['price'])
                    when = datetime.fromtimestamp(float(trade['time']))
                    print(f"📊 Found last buy: {price:.6f} at {when}")
                    return price
            print(f"⚠️ No recent buy trades found for {pair}")
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
        self.active_pair: Optional[str] = None
        self.entry_price: Optional[float] = None
        self.entry_high: float = 0.0
        self.trade_count = 0

        # Per-pair price history and dip tracking
        self.pair_histories: dict[str, deque] = {
            pair: deque(maxlen=PRICE_HISTORY_SIZE) for pair in PAIRS
        }
        self.pair_peaks: dict[str, float] = {pair: 0.0 for pair in PAIRS}

        # Circuit breaker state
        self.consecutive_stops: int = 0
        self.cooldown_until: Optional[datetime] = None

        # Notification flags
        self._dip_notified: dict[str, bool] = {pair: False for pair in PAIRS}
        self._min_vol_notified = False
        self._buy_failed_notified = False
        self._sell_failed_notified = False
        self._buy_warn_sent = False
        self._sell_warn_sent = False

        # Simulation state
        self._sim_fiat = 1000.0
        self._sim_crypto = 0.0

        self._recover_state()

    # ----- BALANCE -----

    def _get_balance(self, crypto_key: str = '') -> tuple[float, float]:
        if self.simulation:
            return self._sim_fiat, self._sim_crypto
        return self.kraken.get_balance(crypto_key)

    # ----- INDICATORS -----

    def _ema(self, period: int, pair: str) -> Optional[float]:
        history = self.pair_histories[pair]
        if len(history) < period:
            return None
        values = list(history)[-period:]
        k = 2 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _realized_vol(self, pair: str) -> Optional[float]:
        history = self.pair_histories[pair]
        if len(history) < VOL_LOOKBACK + 1:
            return None
        values = list(history)[-(VOL_LOOKBACK + 1):]
        returns = [abs((values[i] - values[i - 1]) / values[i - 1]) for i in range(1, len(values)) if values[i - 1] > 0]
        return sum(returns) / len(returns) if returns else None

    def _atr(self, pair: str) -> Optional[float]:
        """Approximate ATR using close-to-close absolute price moves."""
        history = self.pair_histories[pair]
        if len(history) < ATR_PERIOD + 1:
            return None
        values = list(history)[-(ATR_PERIOD + 1):]
        return sum(abs(values[i] - values[i - 1]) for i in range(1, len(values))) / ATR_PERIOD

    def _ema_slope_rising(self, pair: str) -> Optional[bool]:
        """
        True if EMA_FAST is higher now than EMA_SLOPE_LOOKBACK ticks ago.
        Returns None if not enough data yet.
        """
        history = self.pair_histories[pair]
        if len(history) < EMA_FAST_PERIOD + EMA_SLOPE_LOOKBACK:
            return None
        values = list(history)
        k = 2 / (EMA_FAST_PERIOD + 1)

        recent = values[-EMA_FAST_PERIOD:]
        ema_now = recent[0]
        for v in recent[1:]:
            ema_now = v * k + ema_now * (1 - k)

        past = values[-(EMA_FAST_PERIOD + EMA_SLOPE_LOOKBACK):-EMA_SLOPE_LOOKBACK]
        ema_past = past[0]
        for v in past[1:]:
            ema_past = v * k + ema_past * (1 - k)

        return ema_now > ema_past

    def _market_regime(self, ema_fast: Optional[float], slow_emas: list[Optional[float]], pair: str) -> tuple[str, int]:
        """
        Scores EMA20 against each of EMA100, EMA200, EMA400.
        +1 for each slow EMA that EMA20 is above.

        If EMA20 slope is falling, score is reduced by 1 (downgrade regime).

        Score 3 -> bull       (enter, full sizing)
        Score 2 -> mild-bull  (enter, normal sizing)
        Score 1 -> caution    (skip)
        Score 0 -> bear       (skip)
        unknown -> not enough history yet
        """
        if ema_fast is None:
            return 'unknown', -1
        available = [s for s in slow_emas if s is not None]
        if not available:
            return 'unknown', -1

        score = sum(1 for s in available if ema_fast > s)

        # EMA slope filter: a falling EMA20 downgrades the regime by one step
        slope_rising = self._ema_slope_rising(pair)
        if slope_rising is False:
            score = max(0, score - 1)

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
        if vol is None:
            return BASE_RISK_FRACTION
        fraction = BASE_RISK_FRACTION * (TARGET_VOL / max(vol, 0.001))
        return min(MAX_RISK_FRACTION, max(MIN_RISK_FRACTION, fraction))

    # ----- RECOVERY -----

    def _recover_state(self):
        if self.simulation:
            self.mode = 'waiting'
            print(f"🔁 Recovered: waiting (simulation), fiat={self._sim_fiat:.2f}")
            return

        balances = self.kraken.get_all_balances()
        fiat = balances.get('ZUSD', 0.0)

        for pair, crypto_key in PAIRS.items():
            crypto = balances.get(crypto_key, 0.0)
            min_vol = MIN_BUY_VOLUME.get(pair, 0.0)
            if crypto > min_vol * 0.5:
                self.mode = 'holding'
                self.active_pair = pair
                for attempt in range(3):
                    self.entry_price = self.kraken.get_last_buy_price(pair)
                    if self.entry_price:
                        self.entry_high = self.entry_price
                        print(f"🔁 Recovered: holding {pair}, crypto={crypto:.6f}, entry={self.entry_price:.6f}")
                        return
                    print(f"⚠️ Buy price recovery attempt {attempt + 1}/3 failed")
                    if attempt < 2:
                        time.sleep(5)
                msg = (
                    f"CRITICAL: Could not recover entry price!\n"
                    f"Pair: {pair}\nCrypto: {crypto:.6f}\n"
                    f"Failed after 3 attempts.\nTime: {datetime.now()}\n\n"
                    f"Please check manually and restart Dmitry."
                )
                self.notifier.send("Dmitry Recovery FAILED", msg)
                raise RuntimeError(f"Failed to recover entry price for {pair} after 3 attempts")

        self.mode = 'waiting'
        print(f"🔁 Recovered: waiting, fiat={fiat:.2f}")

    # ----- TRADING -----

    def _buy(self, pair: str, fiat_balance: float, price: float, fraction: float = 1.0, regime: str = ''):
        crypto_key = PAIRS[pair]
        min_vol = MIN_BUY_VOLUME.get(pair, 1.0)
        capital = fiat_balance * max(0.0, min(1.0, fraction))

        if self.simulation:
            volume = round((capital * self.BUY_BUFFER) / price, 6)
            if volume < min_vol:
                print(f"⚠️ Buy skipped: volume {volume:.6f} {crypto_key} is below minimum {min_vol}")
                return
            self._sim_crypto = volume
            self._sim_fiat = fiat_balance - (volume * price)
            self.entry_price = price
            actual_price = price
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            fiat_before, _ = self.kraken.get_balance(crypto_key)
            capital = fiat_before * max(0.0, min(1.0, fraction))
            volume = round((capital * self.BUY_BUFFER) / price, 6)
            if volume < min_vol:
                if not self._min_vol_notified:
                    msg = (
                        f"Buy skipped: volume {volume:.6f} {crypto_key} is below minimum {min_vol}\n"
                        f"Pair: {pair}\nFiat: {fiat_before:.2f}, Price: {price:.6f}, Fraction: {fraction:.0%}\n"
                        f"Time: {datetime.now()}"
                    )
                    print(f"⚠️ {msg}")
                    self.notifier.send("Dmitry Buy Skipped (Min Volume)", msg)
                    self._min_vol_notified = True
                else:
                    print(f"⚠️ Buy skipped: volume {volume:.6f} {crypto_key} below minimum {min_vol} (email already sent)")
                return
            self._min_vol_notified = False
            response = self.kraken.place_order('buy', volume, pair)
            if not response or response.get('error'):
                if not self._buy_failed_notified:
                    msg = (
                        f"Buy order failed!\nPair: {pair}\nVolume: {volume}\nFiat Before: {fiat_before:.2f}\n"
                        f"Response: {response}\nTime: {datetime.now()}"
                    )
                    print("⚠️ " + msg)
                    self.notifier.send("Dmitry Buy Order Failed", msg)
                    self._buy_failed_notified = True
                else:
                    print(f"⚠️ Buy order failed again (email already sent). Response: {response}")
                return
            self._buy_failed_notified = False
            print("⏳ Waiting 60s for buy order to settle...")
            time.sleep(60)
            fiat_after, crypto_after = self.kraken.get_balance(crypto_key)
            if fiat_after is None or crypto_after is None:
                fiat_after, crypto_after = self.kraken.get_balance(crypto_key)
            actual_price = (fiat_before - fiat_after) / crypto_after if crypto_after else price
            self.entry_price = actual_price
            fiat_display, crypto_display = fiat_after, crypto_after

        self.entry_high = self.entry_price
        self.logger.log("BUY", actual_price, volume, fiat_display, crypto_display,
                        note=f"pair={pair}, regime={regime}, frac={fraction:.0%}")
        self.notifier.send("Dmitry made a buy!", (
            f"Pair: {pair}\nRegime: {regime}\nEntry Price: {actual_price:.6f}\nVolume: {volume:.6f}\n"
            f"Capital Deployed: {fraction:.0%}\n"
            f"Balances -> Fiat: {fiat_display:.2f}, Crypto: {crypto_display:.8f}\n"
            f"Time: {datetime.now()}"
        ))

    def _sell(self, pair: str, crypto_balance: float, price: float, reason: str = 'take-profit'):
        crypto_key = PAIRS[pair]

        if self.simulation:
            self._sim_fiat = crypto_balance * price
            self._sim_crypto = 0.0
            fiat_display, crypto_display = self._sim_fiat, self._sim_crypto
        else:
            if not crypto_balance or crypto_balance <= 0:
                if self.mode == 'holding':
                    self.mode = 'waiting'
                    self.active_pair = None
                    self.entry_price = None
                    self.entry_high = 0.0
                    print("⚠️ State reset: No crypto to sell")
                return
            response = self.kraken.place_order('sell', crypto_balance, pair)
            if not response or response.get('error'):
                if not self._sell_failed_notified:
                    self.notifier.send("Dmitry sell failed", (
                        f"⚠️ Sell order failed.\nPair: {pair}\nResponse: {response}\nTime: {datetime.now()}"
                    ))
                    self._sell_failed_notified = True
                else:
                    print(f"⚠️ Sell order failed again (email already sent). Response: {response}")
                return
            self._sell_failed_notified = False
            print("⏳ Waiting 60s for sell order to settle...")
            time.sleep(60)
            fiat_after, crypto_after = self.kraken.get_balance(crypto_key)
            fiat_display = fiat_after or 0.0
            crypto_display = crypto_after or 0.0

        pnl_pct = ((price - self.entry_price) / self.entry_price * 100) if self.entry_price else 0.0
        self.mode = 'waiting'
        self.active_pair = None
        self.entry_price = None
        self.entry_high = 0.0
        self.trade_count += 1
        self.logger.log("SELL", price, crypto_balance, fiat_display, crypto_display,
                        note=f"{reason}, pair={pair}")
        self.notifier.send(f"Dmitry sold ({reason})", (
            f"Trade #: {self.trade_count}\nPair: {pair}\nReason: {reason}\n"
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

                prices = self.kraken.get_prices()
                if not prices:
                    time.sleep(2)
                    continue

                # Update all pair price histories every tick (keeps EMAs warm)
                for pair, price in prices.items():
                    self.pair_histories[pair].append(price)

                # ----- HOLDING MODE -----

                if self.mode == 'holding' and self.active_pair:
                    crypto_key = PAIRS[self.active_pair]
                    _, crypto = self._get_balance(crypto_key)
                    min_vol = MIN_BUY_VOLUME.get(self.active_pair, 0.0)
                    if crypto < min_vol * 0.5:
                        print(f"⚠️ Safety reset: holding mode but {crypto_key}={crypto:.8f}")
                        self.mode = 'waiting'
                        self.active_pair = None
                        self.entry_price = None
                        self.entry_high = 0.0

                if self.mode == 'holding' and self.active_pair and self.entry_price:
                    price = prices.get(self.active_pair)
                    if price is None:
                        time.sleep(1)
                        continue

                    self.entry_high = max(self.entry_high, price)
                    vol = self._realized_vol(self.active_pair)
                    _, sell_rise = self._dynamic_thresholds(vol)
                    atr = self._atr(self.active_pair)

                    take_profit = price >= self.entry_price * (1 + sell_rise)
                    if atr is not None:
                        trailing_stop = price <= self.entry_high - ATR_STOP_MULT * atr
                    else:
                        trailing_stop = price <= self.entry_high * (1 - TRAILING_STOP_PCT)

                    if take_profit or trailing_stop:
                        reason = 'take-profit' if take_profit else 'trailing-stop'
                        pair_selling = self.active_pair  # capture before _sell clears it
                        crypto_key = PAIRS[pair_selling]
                        _, pre_crypto = self._get_balance(crypto_key)
                        self._sell(pair_selling, pre_crypto, price, reason=reason)
                        _, post_crypto = self._get_balance(crypto_key)

                        if abs(pre_crypto - post_crypto) < 1e-6:
                            if not self._sell_warn_sent:
                                self.notifier.send("Dmitry Warning", f"Sell ({reason}) may have failed.")
                                self._sell_warn_sent = True
                            else:
                                print(f"⚠️ Sell ({reason}) may have failed (warning email already sent)")
                        else:
                            # Update circuit breaker
                            if reason == 'trailing-stop':
                                self.consecutive_stops += 1
                                if self.consecutive_stops >= CONSECUTIVE_STOP_LIMIT:
                                    self.cooldown_until = now + timedelta(hours=COOLDOWN_HOURS)
                                    msg = (
                                        f"{self.consecutive_stops} consecutive trailing stops hit.\n"
                                        f"Cooling down until {self.cooldown_until.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                        f"Last pair: {pair_selling}\nTime: {now}"
                                    )
                                    print(f"🛑 Circuit breaker triggered. {msg}")
                                    self.notifier.send("Dmitry Circuit Breaker", msg)
                            else:
                                self.consecutive_stops = 0

                            self.pair_peaks[pair_selling] = price
                            self._sell_warn_sent = False

                # ----- WAITING MODE -----

                elif self.mode == 'waiting':
                    # Check circuit breaker cooldown
                    if self.cooldown_until:
                        if now >= self.cooldown_until:
                            print(f"▶ Cooldown expired. Resuming trading.")
                            self.cooldown_until = None
                            self.consecutive_stops = 0
                        else:
                            time.sleep(1)
                            continue

                    fiat, _ = self._get_balance()

                    # Score all pairs and pick the best candidate
                    best_pair = None
                    best_effective_score = -2
                    best_regime = ''

                    for pair in PAIRS:
                        if pair not in prices:
                            continue
                        price = prices[pair]
                        ema_fast = self._ema(EMA_FAST_PERIOD, pair)
                        slow_emas = [self._ema(p, pair) for p in EMA_SLOW_PERIODS]
                        vol = self._realized_vol(pair)
                        regime, score = self._market_regime(ema_fast, slow_emas, pair)
                        vol_ok = vol is None or vol <= MAX_ENTRY_VOL

                        if not vol_ok or regime not in ('bull', 'mild-bull', 'unknown'):
                            self._dip_notified[pair] = False
                            continue

                        self.pair_peaks[pair] = max(self.pair_peaks[pair], price) if self.pair_peaks[pair] else price
                        buy_dip, _ = self._dynamic_thresholds(vol)
                        dip_pct = (self.pair_peaks[pair] - price) / self.pair_peaks[pair]

                        if dip_pct < buy_dip:
                            self._dip_notified[pair] = False
                            continue

                        # Known regimes rank above 'unknown' (startup warmup fallback)
                        effective_score = score if regime != 'unknown' else -1
                        if effective_score > best_effective_score:
                            best_effective_score = effective_score
                            best_pair = pair
                            best_regime = regime

                    if best_pair and fiat > 1:
                        price = prices[best_pair]
                        vol = self._realized_vol(best_pair)
                        fraction = self._position_fraction(vol)

                        if not self._dip_notified[best_pair]:
                            self._dip_notified[best_pair] = True
                            ema_fast = self._ema(EMA_FAST_PERIOD, best_pair)
                            slow_emas = [self._ema(p, best_pair) for p in EMA_SLOW_PERIODS]
                            _, score = self._market_regime(ema_fast, slow_emas, best_pair)
                            ema_labels = [f"EMA{p}={f'{s:.4f}' if s else 'warming'}" for p, s in zip(EMA_SLOW_PERIODS, slow_emas)]
                            buy_dip, _ = self._dynamic_thresholds(vol)
                            dip_pct = (self.pair_peaks[best_pair] - price) / self.pair_peaks[best_pair]
                            self.notifier.send("Dmitry Dip Triggered", (
                                f"Pair: {best_pair}\nPrice: {price:.6f}\nPeak: {self.pair_peaks[best_pair]:.6f}\n"
                                f"Dip: {dip_pct:.4%} (threshold: {buy_dip:.4%})\n"
                                f"EMA{EMA_FAST_PERIOD}={f'{ema_fast:.4f}' if ema_fast else 'warming'}\n"
                                f"{chr(10).join(ema_labels)}\n"
                                f"Regime: {best_regime} (score {score}/3)\n"
                                f"Vol: {f'{vol:.4f}' if vol else 'N/A'}\n"
                                f"Capital Fraction: {fraction:.0%}\nFiat: {fiat:.2f}\nTime: {now}"
                            ))

                        pre_fiat, _ = self._get_balance()
                        self._buy(best_pair, pre_fiat, price, fraction=fraction, regime=best_regime)
                        post_fiat, _ = self._get_balance()

                        if abs(pre_fiat - post_fiat) < 1e-6:
                            if not self._buy_warn_sent:
                                self.notifier.send("Dmitry Warning", "Buy may have failed — still in waiting mode.")
                                self._buy_warn_sent = True
                            else:
                                print("⚠️ Buy may have failed (warning email already sent)")
                        else:
                            self.mode = 'holding'
                            self.active_pair = best_pair
                            self.pair_peaks[best_pair] = price
                            self._dip_notified[best_pair] = False
                            self._min_vol_notified = False
                            self._buy_failed_notified = False
                            self._buy_warn_sent = False

                time.sleep(1)

        except KeyboardInterrupt:
            self.notifier.send("Dmitry Stopped", "Dmitry was manually stopped.")
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self.notifier.send("Dmitry Crashed!", f"Dmitry crashed:\n\n{err}")
            raise


if __name__ == '__main__':
    TradingBot().run()
