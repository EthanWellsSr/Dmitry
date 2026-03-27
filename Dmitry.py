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

# ---- Candle settings ----
# All indicators (EMA, RSI, ATR, vol) use 1-minute OHLC candles from Kraken.
# This prevents the noise that plagued tick-based EMAs (where EMA20 = 20 seconds).
CANDLE_INTERVAL = 1          # 1-minute candles
CANDLE_WARMUP = 260          # candles to fetch per pair on startup (~4.3 hours)
CANDLE_HISTORY_SIZE = 260    # max candles stored per pair
CANDLE_UPDATE_INTERVAL = 60  # seconds between candle refreshes in the main loop

# ---- EMA periods (in 1-minute candles = minutes) ----
EMA_FAST_PERIOD = 20         # 20-minute fast EMA
EMA_SLOW_PERIODS = [50, 100, 200]  # 50m, ~1.7h, ~3.3h slow EMAs
EMA_SLOPE_LOOKBACK = 5       # candles to look back when checking EMA slope direction

# ---- RSI filter ----
# Research confirms RSI + EMA outperforms EMA alone for dip-buying.
# Only enter when RSI < RSI_MAX_ENTRY (price has pulled back, not overbought).
RSI_PERIOD = 14
RSI_MAX_ENTRY = 50           # skip entry if RSI >= 50 on 1-minute chart

# ---- ATR trailing stop (on 1-minute true-range candles) ----
# 2.5x ATR is the research-backed sweet spot for intraday crypto.
ATR_PERIOD = 14
ATR_STOP_MULT = 2.5
TRAILING_STOP_PCT = 0.05     # emergency floor: max 5% loss from entry price
MIN_ATR_STOP_PCT = 0.010     # minimum stop buffer: 1% below entry_high
                              # (prevents stop inside spread noise; gives ~1.5:1 R:R vs 1.5% TP)

# Trailing stop cannot fire within this many seconds of entry.
# Take-profit can still exit anytime.
MIN_HOLD_SECONDS = 1800      # 30 minutes (up from 15 — gives trades time to develop)

# ---- Circuit breaker ----
CONSECUTIVE_STOP_LIMIT = 2   # 2 consecutive trailing stops trigger cooldown
COOLDOWN_HOURS = 6

# Time-of-day filter (UTC hours). Avoid entries during low-liquidity windows.
# Research: spread widens significantly between 22:00–02:00 UTC for crypto.
# All 5 rapid-fire losses on 2026-03-25 occurred at 02:51–03:00 UTC.
LOW_LIQUIDITY_START_UTC = 22  # inclusive
LOW_LIQUIDITY_END_UTC = 2     # exclusive (so block 22, 23, 0, 1)

# ---- Volatility (per 1-minute candle average absolute return) ----
VOL_LOOKBACK = 20            # candles
MAX_ENTRY_VOL = 0.006        # skip entry if avg per-candle move > 0.6%
TARGET_VOL = 0.003           # reference vol for full position sizing

# ---- Entry thresholds ----
MIN_BUY_DIP = 0.004          # price must be >= 0.4% below rolling 20-candle peak
VOL_DIP_MULT = 1.0
MIN_SELL_RISE = 0.015        # 1.5% take-profit floor
VOL_SELL_MULT = 1.5

# Bounce confirmation: require this many consecutive rising candle closes before entry.
# Replaces the old 0.5%-bounce hack — candle closes are far more reliable.
BOUNCE_CANDLES_REQUIRED = 2

# ---- Minimum order sizes (Kraken requirements) ----
MIN_BUY_VOLUME = {
    'XXRPZUSD': 1.0,
    'XETHZUSD': 0.002,
    'XXBTZUSD': 0.0001,
    'SOLUSD':   0.02,
}

# ---- Position sizing ----
BASE_RISK_FRACTION = 0.90
MAX_RISK_FRACTION = 0.90
MIN_RISK_FRACTION = 0.25

# ---- Google Sheets ----
GOOGLE_KEY_FILE = 'google_key.json'
GOOGLE_SHEET_NAME = 'Dmitry_trades'

# ---- Email alerts ----
EMAIL_ALERTS = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SEND_STARTUP_EMAIL = True

# ---- Heartbeat ----
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
        """Fetch current bid prices for all pairs in one API call."""
        res = self._call('public', 'Ticker', {'pair': ','.join(PAIRS.keys())})
        if not res or 'result' not in res:
            return {}
        return {
            pair: float(data['c'][0])
            for pair, data in res['result'].items()
            if pair in PAIRS
        }

    def get_ohlc(self, pair: str, interval: int = 1, since: int = None) -> list:
        """
        Fetch OHLC candle data from Kraken.
        Returns list of [time, open, high, low, close, vwap, volume, count].
        The last entry is always the current (incomplete) candle — callers should skip it.
        """
        params = {'pair': pair, 'interval': interval}
        if since is not None:
            params['since'] = since
        res = self._call('public', 'OHLC', params)
        if not res or 'result' not in res:
            return []
        for key, val in res['result'].items():
            if key != 'last' and isinstance(val, list):
                return val
        return []

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

        # 1-minute OHLC candle histories per pair (dicts with t/o/h/l/c/v keys)
        self.candle_histories: dict[str, deque] = {
            pair: deque(maxlen=CANDLE_HISTORY_SIZE) for pair in PAIRS
        }
        self.last_candle_update: float = 0.0

        # Entry timing (used to enforce MIN_HOLD_SECONDS before trailing stop can fire)
        self.entry_time: Optional[datetime] = None

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

        # Warm up candle histories BEFORE recovery so ATR/EMAs are ready immediately.
        # This is especially important when restarting with an open position.
        if not self.simulation:
            self._warmup_candles()

        self._recover_state()

    # ----- CANDLE DATA -----

    def _warmup_candles(self):
        """Fetch CANDLE_WARMUP historical 1-minute candles per pair from Kraken on startup."""
        since = int(time.time()) - (CANDLE_WARMUP + 15) * CANDLE_INTERVAL * 60
        print(f"📊 Warming up {CANDLE_WARMUP} candles per pair (~{CANDLE_WARMUP} minutes of history)...")
        for pair in PAIRS:
            raw = self.kraken.get_ohlc(pair, CANDLE_INTERVAL, since=since)
            if not raw:
                print(f"  ⚠️ Candle warmup failed for {pair}")
                continue
            # Skip the last entry — it's the current (incomplete) candle
            for row in raw[:-1]:
                self.candle_histories[pair].append({
                    't': int(row[0]),
                    'o': float(row[1]),
                    'h': float(row[2]),
                    'l': float(row[3]),
                    'c': float(row[4]),
                    'v': float(row[6]),
                })
            n = len(self.candle_histories[pair])
            print(f"  {pair}: {n} candles loaded (EMA200 needs 200; have {'✅' if n >= 200 else f'⚠️ only {n}'})")
            time.sleep(0.5)  # avoid Kraken rate limit
        self.last_candle_update = time.time()

    def _update_candles(self):
        """Append any new completed 1-minute candles. Called every CANDLE_UPDATE_INTERVAL seconds."""
        for pair in PAIRS:
            hist = self.candle_histories[pair]
            since = hist[-1]['t'] if hist else int(time.time()) - 180
            raw = self.kraken.get_ohlc(pair, CANDLE_INTERVAL, since=since)
            if not raw or len(raw) < 2:
                continue
            # Skip the last entry (current incomplete candle)
            for row in raw[:-1]:
                candle_t = int(row[0])
                if hist and hist[-1]['t'] >= candle_t:
                    continue  # already stored
                hist.append({
                    't': candle_t,
                    'o': float(row[1]),
                    'h': float(row[2]),
                    'l': float(row[3]),
                    'c': float(row[4]),
                    'v': float(row[6]),
                })
        self.last_candle_update = time.time()

    # ----- INDICATORS (all candle-based) -----

    def _ema_from_candles(self, period: int, pair: str) -> Optional[float]:
        """EMA computed over all available candle close prices for maximum accuracy."""
        hist = self.candle_histories[pair]
        if len(hist) < period:
            return None
        closes = [c['c'] for c in hist]
        k = 2 / (period + 1)
        ema = closes[0]
        for v in closes[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _rsi(self, pair: str) -> Optional[float]:
        """RSI(14) on 1-minute candle closes. Only enter when RSI < RSI_MAX_ENTRY."""
        hist = self.candle_histories[pair]
        need = RSI_PERIOD + 1
        if len(hist) < need:
            return None
        closes = [c['c'] for c in list(hist)[-need:]]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains) / RSI_PERIOD
        avg_loss = sum(losses) / RSI_PERIOD
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _true_atr(self, pair: str) -> Optional[float]:
        """
        True Average True Range on 1-minute candles.
        True range = max(H-L, |H-prevClose|, |L-prevClose|).
        Far more accurate than the old close-to-close tick ATR.
        """
        hist = self.candle_histories[pair]
        need = ATR_PERIOD + 1
        if len(hist) < need:
            return None
        candles = list(hist)[-need:]
        trs = []
        for i in range(1, len(candles)):
            h = candles[i]['h']
            l = candles[i]['l']
            pc = candles[i - 1]['c']
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs)

    def _candle_vol(self, pair: str) -> Optional[float]:
        """Average absolute per-candle return over VOL_LOOKBACK candles."""
        hist = self.candle_histories[pair]
        need = VOL_LOOKBACK + 1
        if len(hist) < need:
            return None
        candles = list(hist)[-need:]
        returns = []
        for i in range(1, len(candles)):
            pc = candles[i - 1]['c']
            cc = candles[i]['c']
            if pc > 0:
                returns.append(abs((cc - pc) / pc))
        return sum(returns) / len(returns) if returns else None

    def _candle_slope_rising(self, pair: str) -> Optional[bool]:
        """True if EMA_FAST is higher now than EMA_SLOPE_LOOKBACK candles ago."""
        hist = self.candle_histories[pair]
        need = EMA_FAST_PERIOD + EMA_SLOPE_LOOKBACK
        if len(hist) < need:
            return None
        candles = list(hist)
        k = 2 / (EMA_FAST_PERIOD + 1)

        recent = [c['c'] for c in candles[-EMA_FAST_PERIOD:]]
        ema_now = recent[0]
        for v in recent[1:]:
            ema_now = v * k + ema_now * (1 - k)

        past_end = len(candles) - EMA_SLOPE_LOOKBACK
        past = [c['c'] for c in candles[past_end - EMA_FAST_PERIOD:past_end]]
        ema_past = past[0]
        for v in past[1:]:
            ema_past = v * k + ema_past * (1 - k)

        return ema_now > ema_past

    def _has_bounce_confirmation(self, pair: str) -> bool:
        """
        Require BOUNCE_CANDLES_REQUIRED consecutive higher candle closes.
        Far more reliable than the old 0.5% price-tick bounce hack.
        """
        hist = self.candle_histories[pair]
        need = BOUNCE_CANDLES_REQUIRED + 1
        if len(hist) < need:
            return False
        recent = [c['c'] for c in list(hist)[-need:]]
        return all(recent[i] > recent[i - 1] for i in range(1, len(recent)))

    def _rolling_peak(self, pair: str, lookback: int = 20) -> Optional[float]:
        """Highest close price among the last `lookback` candles."""
        hist = self.candle_histories[pair]
        if len(hist) < lookback:
            if len(hist) == 0:
                return None
            lookback = len(hist)
        return max(c['c'] for c in list(hist)[-lookback:])

    # ----- REGIME & THRESHOLDS -----

    def _market_regime(self, ema_fast: Optional[float], slow_emas: list[Optional[float]], pair: str) -> tuple[str, int]:
        """
        Scores EMA_FAST against each slow EMA (computed on 1-minute candles).
        +1 for each slow EMA that EMA_FAST is above.
        EMA slope downgrade: if EMA_FAST is falling, subtract 1.

        Score == len(slow_emas) -> bull       (enter)
        Score == len(slow_emas)-1 -> mild-bull (skipped — too risky in real markets)
        Score <= len(slow_emas)-2 -> caution/bear (skip)
        """
        if ema_fast is None:
            return 'unknown', -1
        available = [s for s in slow_emas if s is not None]
        if not available:
            return 'unknown', -1

        score = sum(1 for s in available if ema_fast > s)

        slope_rising = self._candle_slope_rising(pair)
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
        fraction = BASE_RISK_FRACTION * (TARGET_VOL / max(vol, 0.0001))
        return min(MAX_RISK_FRACTION, max(MIN_RISK_FRACTION, fraction))

    # ----- BALANCE -----

    def _get_balance(self, crypto_key: str = '') -> tuple[float, float]:
        if self.simulation:
            return self._sim_fiat, self._sim_crypto
        return self.kraken.get_balance(crypto_key)

    # ----- RECOVERY -----

    def _recover_state(self):
        """
        On startup, check if we already hold a position from a previous run.
        Because _warmup_candles() was called first, candle indicators are ready
        and the ATR/stop calculations will be accurate from tick 1.
        """
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
                        # Set entry_time far enough in the past so the trailing stop
                        # can fire if the position has already moved against us.
                        # The 30-min hold time is waived on recovery — we don't know
                        # how long this position has been open.
                        self.entry_time = datetime.now() - timedelta(seconds=MIN_HOLD_SECONDS)
                        print(f"🔁 Recovered: holding {pair}, crypto={crypto:.6f}, entry={self.entry_price:.6f}")
                        atr = self._true_atr(pair)
                        print(f"   ATR(14) on 1m candles: {atr:.6f}" if atr else "   ATR not yet available")
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
        self.entry_time = datetime.now()
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
        self.entry_time = None
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
            self.notifier.send("Dmitry Started", (
                "Dmitry just started successfully.\n\n"
                "Key changes in this version:\n"
                "- All indicators now use 1-minute OHLC candles (not 1-second ticks)\n"
                "- RSI(14) filter added: only enter when RSI < 50\n"
                "- Bull regime required: EMA20 > EMA50 > EMA100 > EMA200 (all on 1-minute)\n"
                "- Bounce confirmation: 2 consecutive higher candle closes required\n"
                "- Min hold time: 30 minutes\n"
                "- Stop floor: 1% below entry high (up from 1.5%)\n"
            ))

        next_heartbeat = _next_top_of_hour()

        try:
            while True:
                now = datetime.now()

                if HEARTBEAT_ENABLED and now >= next_heartbeat:
                    self.notifier.send(HEARTBEAT_SUBJECT, f"{HEARTBEAT_BODY}\nTime: {now}")
                    next_heartbeat = _next_top_of_hour(now)

                # Refresh candle data every CANDLE_UPDATE_INTERVAL seconds.
                # This keeps EMAs, RSI, and ATR current without hammering the API.
                if not self.simulation and (time.time() - self.last_candle_update >= CANDLE_UPDATE_INTERVAL):
                    self._update_candles()

                prices = self.kraken.get_prices()
                if not prices:
                    time.sleep(2)
                    continue

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
                    vol = self._candle_vol(self.active_pair)
                    _, sell_rise = self._dynamic_thresholds(vol)
                    atr = self._true_atr(self.active_pair)

                    take_profit = price >= self.entry_price * (1 + sell_rise)
                    stop_floor = self.entry_price * (1 - TRAILING_STOP_PCT)

                    if atr is not None:
                        # Use whichever ATR distance is larger: computed ATR or the 1% floor.
                        # 1-minute ATR is meaningful (unlike old 14-second ATR) so 2.5x is real.
                        atr_distance = max(ATR_STOP_MULT * atr, self.entry_high * MIN_ATR_STOP_PCT)
                        atr_stop = self.entry_high - atr_distance
                        trailing_stop = price <= max(atr_stop, stop_floor)
                    else:
                        trailing_stop = price <= stop_floor

                    # Trailing stop cannot fire within MIN_HOLD_SECONDS of entry.
                    # Take-profit can still exit anytime.
                    time_held = (now - self.entry_time).total_seconds() if self.entry_time else MIN_HOLD_SECONDS
                    if trailing_stop and time_held < MIN_HOLD_SECONDS:
                        trailing_stop = False

                    if take_profit or trailing_stop:
                        reason = 'take-profit' if take_profit else 'trailing-stop'
                        pair_selling = self.active_pair
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
                            self._sell_warn_sent = False

                # ----- WAITING MODE -----

                elif self.mode == 'waiting':
                    if self.cooldown_until:
                        if now >= self.cooldown_until:
                            print(f"▶ Cooldown expired. Resuming trading.")
                            self.cooldown_until = None
                            self.consecutive_stops = 0
                        else:
                            time.sleep(1)
                            continue

                    # Time-of-day filter: skip entries during low-liquidity hours (UTC).
                    # Spreads widen and thin order books produce noise-based false signals.
                    utc_hour = datetime.utcnow().hour
                    in_low_liquidity = (
                        utc_hour >= LOW_LIQUIDITY_START_UTC or utc_hour < LOW_LIQUIDITY_END_UTC
                    )
                    if in_low_liquidity:
                        time.sleep(1)
                        continue

                    fiat, _ = self._get_balance()

                    best_pair = None
                    best_effective_score = -2
                    best_regime = ''

                    for pair in PAIRS:
                        if pair not in prices:
                            continue
                        price = prices[pair]

                        # --- Candle-based indicators ---
                        ema_fast = self._ema_from_candles(EMA_FAST_PERIOD, pair)
                        slow_emas = [self._ema_from_candles(p, pair) for p in EMA_SLOW_PERIODS]
                        vol = self._candle_vol(pair)
                        regime, score = self._market_regime(ema_fast, slow_emas, pair)

                        # Only enter in confirmed bull regime (all EMAs aligned on 1-minute candles).
                        # This is the single most important filter — it prevents buying in downtrends.
                        if regime != 'bull':
                            self._dip_notified[pair] = False
                            continue

                        # Skip if volatility is too high (chaotic market)
                        if vol is not None and vol > MAX_ENTRY_VOL:
                            self._dip_notified[pair] = False
                            continue

                        # --- RSI filter ---
                        # Don't enter if RSI >= RSI_MAX_ENTRY. This ensures we only buy
                        # when price has genuinely pulled back, not when it's still elevated.
                        rsi = self._rsi(pair)
                        if rsi is not None and rsi >= RSI_MAX_ENTRY:
                            self._dip_notified[pair] = False
                            continue

                        # --- Dip detection (rolling 20-candle peak) ---
                        rolling_peak = self._rolling_peak(pair, lookback=20)
                        if rolling_peak is None:
                            continue
                        buy_dip, _ = self._dynamic_thresholds(vol)
                        dip_pct = (rolling_peak - price) / rolling_peak

                        if dip_pct < buy_dip:
                            self._dip_notified[pair] = False
                            continue

                        # --- Bounce confirmation ---
                        # Require BOUNCE_CANDLES_REQUIRED consecutive higher candle closes.
                        # This confirms the dip has reversed and avoids catching a falling knife.
                        if not self._has_bounce_confirmation(pair):
                            continue

                        effective_score = score if regime != 'unknown' else -1
                        if effective_score > best_effective_score:
                            best_effective_score = effective_score
                            best_pair = pair
                            best_regime = regime

                    if best_pair and fiat > 1:
                        price = prices[best_pair]
                        vol = self._candle_vol(best_pair)
                        fraction = self._position_fraction(vol)

                        if not self._dip_notified[best_pair]:
                            self._dip_notified[best_pair] = True
                            ema_fast = self._ema_from_candles(EMA_FAST_PERIOD, best_pair)
                            slow_emas = [self._ema_from_candles(p, best_pair) for p in EMA_SLOW_PERIODS]
                            _, score = self._market_regime(ema_fast, slow_emas, best_pair)
                            ema_labels = [
                                f"EMA{p}={f'{s:.4f}' if s else 'warming'}"
                                for p, s in zip(EMA_SLOW_PERIODS, slow_emas)
                            ]
                            buy_dip, _ = self._dynamic_thresholds(vol)
                            rolling_peak = self._rolling_peak(best_pair, lookback=20) or price
                            dip_pct = (rolling_peak - price) / rolling_peak
                            rsi = self._rsi(best_pair)
                            self.notifier.send("Dmitry Dip Triggered", (
                                f"Pair: {best_pair}\nPrice: {price:.6f}\n"
                                f"Rolling Peak (20c): {rolling_peak:.6f}\n"
                                f"Dip: {dip_pct:.4%} (threshold: {buy_dip:.4%})\n"
                                f"RSI(14): {f'{rsi:.1f}' if rsi is not None else 'N/A'}\n"
                                f"EMA{EMA_FAST_PERIOD}={f'{ema_fast:.4f}' if ema_fast else 'warming'}\n"
                                f"{chr(10).join(ema_labels)}\n"
                                f"Regime: {best_regime} (score {score}/{len(EMA_SLOW_PERIODS)})\n"
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
