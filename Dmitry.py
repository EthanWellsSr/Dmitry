"""
Dmitry — a read-only BTC drawdown watchtower.

Dmitry does NOT trade. He holds no keys, places no orders, and cannot touch
your money. His only job is to watch Bitcoin 24/7 and push you an alert when
the price has fallen far below its 1-year high — the "be greedy when others
are fearful" moment — so that YOU can decide whether to load cash and buy.

The signal is dead simple and computed from public price data alone:

    drawdown = (highest daily close in the last 365 days - current price)
               / highest daily close in the last 365 days

When that drawdown crosses an escalating ladder of tiers (-30%, -40%, ...),
Dmitry sends one email alert per tier. He re-arms after a recovery so the
next cycle's crash alerts you again, and he persists just enough state to make
restarts invisible.

This replaces the old round-trip trading bot entirely. There is no buy/sell
logic here by design: timing exits is a losing game, and a watchtower that can
only *alert* cannot lose you money.
"""

import time
import json
import smtplib
import traceback
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText

import krakenex

# ========================
# ----- CONFIG -----
# ========================

BOT_VERSION = "2026-06-30 (Watchtower: read-only BTC drawdown alarm)"

# The single pair Dmitry watches. Kraken's BTC/USD pair key.
PAIR = "XXBTZUSD"

# ---- Signal ----
TRAILING_WINDOW_DAYS = 365     # reference high = highest daily close over this window
PRICE_POLL_SECONDS = 300       # check current price every 5 minutes
HIGH_RECOMPUTE_SECONDS = 86400  # recompute the trailing high once a day

# Drawdown tiers (fraction below the trailing high). One alert per tier, per cycle.
# Escalating urgency: -30% notable, -50%+ "back up the truck".
TIERS = [0.30, 0.40, 0.50, 0.60, 0.70]

# Re-arm: once price recovers to within this fraction of the trailing high, all
# tiers reset so a future crash alerts again. This is the hysteresis band that
# prevents chop around a single tier from spamming you.
REARM_RECOVERY = 0.15

# ---- False-alarm guard ----
# A tier must hold for this many consecutive checks before firing, so a single
# bad tick or API glitch can't trigger a false "back up the truck" alert.
CONFIRM_TICKS = 2
# Reject a trailing-high recompute that jumps more than this in a day (corrupt fetch).
MAX_HIGH_DAILY_MOVE = 0.50

# ---- State ----
STATE_FILE = "watchtower_state.json"

# ---- Email alerts (loaded from a gitignored key file) ----
# email.key format (one per line):
#   EMAIL_SENDER=you@gmail.com
#   EMAIL_PASSWORD=your_gmail_app_password
#   EMAIL_RECEIVER=where_to_alert@example.com
EMAIL_KEY_FILE = "email.key"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# ---- Dead-man's switch (optional) ----
# Put a healthchecks.io (or UptimeRobot) ping URL here, or leave blank to disable.
# Dmitry pings it every loop; if the pings stop, the service emails YOU that he
# went dark — the one failure a self-sent heartbeat can never report.
HEALTHCHECK_URL = ""


# ========================
# ----- NOTIFIER (Gmail SMTP) -----
# ========================

class Notifier:
    """Sends alerts by email via Gmail SMTP. Disables itself gracefully if no
    key file is present, so the watchtower keeps running either way."""

    def __init__(self, key_file: str = EMAIL_KEY_FILE):
        self.enabled = True
        self.sender = self.password = self.receiver = ""
        self._load_keys(key_file)

    def _load_keys(self, file_path: str):
        try:
            keys = {}
            with open(file_path) as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        keys[k.strip()] = v.strip()
            self.sender = keys.get("EMAIL_SENDER", "")
            self.password = keys.get("EMAIL_PASSWORD", "")
            self.receiver = keys.get("EMAIL_RECEIVER", "")
            if not (self.sender and self.password and self.receiver):
                print("⚠️ email.key incomplete. Alerts disabled.")
                self.enabled = False
        except FileNotFoundError:
            print("⚠️ email.key not found. Alerts disabled (running in print-only mode).")
            self.enabled = False

    def send(self, subject: str, message: str, priority: int = 0):
        """priority: 0 normal, 1 high, 2 emergency. Email has no retry-until-ack,
        so higher priorities just flag the subject so it stands out in your inbox."""
        if priority >= 2:
            subject = "🚨 " + subject
        elif priority == 1:
            subject = "⚠️ " + subject
        print(f"🔔 ALERT [{subject}] {message}")
        if not self.enabled:
            return
        try:
            msg = MIMEText(message)
            msg["Subject"] = subject
            msg["From"] = self.sender
            msg["To"] = self.receiver
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(self.sender, self.password)
                server.send_message(msg)
            print(f"📧 Email sent: {subject}")
        except Exception as e:
            print(f"⚠️ Failed to send email: {e}")


# ========================
# ----- KRAKEN CLIENT (public data only) -----
# ========================

class KrakenClient:
    """Read-only access to Kraken's PUBLIC market data. No key, no private
    endpoints, no orders — Dmitry physically cannot trade."""

    def __init__(self):
        self.api = krakenex.API()

    def _call_public(self, endpoint: str, data: dict = None, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                res = self.api.query_public(endpoint, data or {})
                if res.get("error"):
                    if any("EAPI:Rate limit exceeded" in e for e in res["error"]):
                        wait = 2 ** attempt
                        print(f"Rate limit exceeded. Retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                    print(f"Kraken {endpoint} error: {res['error']}")
                return res
            except Exception as e:
                print(f"Kraken {endpoint} attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        print(f"Kraken {endpoint} failed after {max_retries} attempts.")
        return None

    def get_price(self) -> float:
        """Current BTC price (last trade), or None on failure."""
        res = self._call_public("Ticker", {"pair": PAIR})
        if not res or "result" not in res:
            return None
        for key, val in res["result"].items():
            if key == PAIR or PAIR in key:
                try:
                    return float(val["c"][0])
                except (KeyError, ValueError, TypeError):
                    return None
        return None

    def get_daily_closes(self, days: int) -> list:
        """The last `days` completed daily close prices (skips today's open candle)."""
        res = self._call_public("OHLC", {"pair": PAIR, "interval": 1440})
        if not res or "result" not in res:
            return []
        rows = None
        for key, val in res["result"].items():
            if key != "last" and isinstance(val, list):
                rows = val
                break
        if not rows:
            return []
        # Drop the final (still-forming) daily candle, then take the last `days`.
        completed = rows[:-1]
        closes = []
        for row in completed[-days:]:
            try:
                closes.append(float(row[4]))  # column 4 = close
            except (IndexError, ValueError, TypeError):
                continue
        return closes


# ========================
# ----- STATE STORE -----
# ========================

class StateStore:
    """Persists the only state that must survive a restart: the deepest tier
    already alerted this cycle. The trailing high is NOT persisted — it's
    recomputed from Kraken on every startup."""

    def __init__(self, file_path: str = STATE_FILE):
        self.file_path = file_path
        self.last_fired_tier = None  # e.g. 0.40, or None if nothing armed

    def load(self):
        try:
            with open(self.file_path) as f:
                data = json.load(f)
            self.last_fired_tier = data.get("last_fired_tier")
            print(f"💾 State loaded: last_fired_tier={self.last_fired_tier}")
        except FileNotFoundError:
            print("💾 No state file yet — starting fresh.")
        except Exception as e:
            print(f"⚠️ Could not read state file ({e}). Starting fresh.")

    def save(self):
        try:
            with open(self.file_path, "w") as f:
                json.dump({
                    "last_fired_tier": self.last_fired_tier,
                    "version": BOT_VERSION,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                }, f, indent=2)
        except Exception as e:
            print(f"⚠️ State save failed: {e}")


# ========================
# ----- WATCHTOWER -----
# ========================

class Watchtower:
    def __init__(self):
        self.notifier = Notifier()
        self.kraken = KrakenClient()
        self.state = StateStore()
        self.state.load()

        self.trailing_high = None
        self.last_high_compute = 0.0

        # False-alarm confirmation: a candidate tier must hold CONFIRM_TICKS times.
        self._pending_tier = None
        self._pending_count = 0

    # ----- SIGNAL -----

    def _recompute_high(self):
        """Refresh the trailing-365-day high. Rejects corrupt fetches that would
        move the reference by more than MAX_HIGH_DAILY_MOVE in a single day."""
        closes = self.kraken.get_daily_closes(TRAILING_WINDOW_DAYS)
        if not closes:
            print("⚠️ Could not fetch daily closes — keeping previous high.")
            return
        new_high = max(closes)
        if new_high <= 0:
            print("⚠️ Rejected non-positive trailing high.")
            return
        if self.trailing_high is not None:
            move = abs(new_high - self.trailing_high) / self.trailing_high
            if move > MAX_HIGH_DAILY_MOVE:
                print(f"⚠️ Rejected suspicious high jump ({move:.0%}). Keeping {self.trailing_high:.2f}.")
                return
        self.trailing_high = new_high
        self.last_high_compute = time.time()
        print(f"📈 Trailing {TRAILING_WINDOW_DAYS}d high: {self.trailing_high:.2f} "
              f"(from {len(closes)} daily closes)")

    def _deepest_tier_crossed(self, drawdown: float):
        """The deepest tier whose threshold the current drawdown has reached, or None."""
        crossed = [t for t in TIERS if drawdown >= t]
        return max(crossed) if crossed else None

    def _tier_priority(self, tier: float) -> int:
        return 2 if tier >= 0.50 else 1  # -50% and deeper = emergency push

    def _fire(self, tier: float, drawdown: float, price: float):
        pct = int(round(tier * 100))
        tone = {
            30: "Notable dip.",
            40: "Major drawdown.",
            50: "Severe — back up the truck territory.",
            60: "Generational fear.",
            70: "Historic capitulation.",
        }.get(pct, "")
        title = f"BTC down {drawdown * 100:.1f}% (tier -{pct}%)"
        message = (
            f"{tone}\n"
            f"Price: ${price:,.0f}\n"
            f"1yr high: ${self.trailing_high:,.0f}\n"
            f"Drawdown: -{drawdown * 100:.1f}%\n"
            f"Be greedy when others are fearful.\n"
            f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}"
        )
        self.notifier.send(title, message, priority=self._tier_priority(tier))
        self.state.last_fired_tier = tier
        self.state.save()

    def _evaluate(self, price: float):
        drawdown = (self.trailing_high - price) / self.trailing_high

        # Re-arm: recovered close enough to the high → reset the cycle.
        if drawdown <= REARM_RECOVERY and self.state.last_fired_tier is not None:
            print(f"▶ Recovered to -{drawdown * 100:.1f}% — re-arming all tiers.")
            self.state.last_fired_tier = None
            self.state.save()
            self._pending_tier = None
            self._pending_count = 0

        target = self._deepest_tier_crossed(drawdown)
        last = self.state.last_fired_tier

        # Only consider a tier deeper than the deepest already fired this cycle.
        if target is None or (last is not None and target <= last):
            self._pending_tier = None
            self._pending_count = 0
            return

        # False-alarm guard: require CONFIRM_TICKS consecutive readings.
        if target == self._pending_tier:
            self._pending_count += 1
        else:
            self._pending_tier = target
            self._pending_count = 1

        if self._pending_count >= CONFIRM_TICKS:
            self._fire(target, drawdown, price)
            self._pending_tier = None
            self._pending_count = 0
        else:
            print(f"… tier -{int(target * 100)}% pending confirmation "
                  f"({self._pending_count}/{CONFIRM_TICKS})")

    # ----- DEAD-MAN'S SWITCH -----

    def _ping_healthcheck(self):
        if not HEALTHCHECK_URL:
            return
        try:
            urllib.request.urlopen(HEALTHCHECK_URL, timeout=10)
        except Exception as e:
            print(f"⚠️ Healthcheck ping failed: {e}")

    # ----- MAIN LOOP -----

    def run(self):
        self.notifier.send(
            "Dmitry Watchtower online",
            f"Watching {PAIR}. Alert tiers: "
            f"{', '.join(f'-{int(t * 100)}%' for t in TIERS)}.\n"
            f"Version: {BOT_VERSION}",
            priority=0,
        )

        self._recompute_high()

        try:
            while True:
                now = time.time()

                if now - self.last_high_compute >= HIGH_RECOMPUTE_SECONDS:
                    self._recompute_high()

                price = self.kraken.get_price()
                if price is None or price <= 0:
                    print("⚠️ Bad/missing price — skipping this check.")
                elif self.trailing_high is None:
                    print("⏳ Trailing high not ready yet — retrying.")
                    self._recompute_high()
                else:
                    drawdown = (self.trailing_high - price) / self.trailing_high
                    print(f"BTC ${price:,.0f} | 1yr high ${self.trailing_high:,.0f} "
                          f"| drawdown -{drawdown * 100:.1f}% "
                          f"| armed below -{int((self.state.last_fired_tier or 0) * 100)}%")
                    self._evaluate(price)

                self._ping_healthcheck()
                time.sleep(PRICE_POLL_SECONDS)

        except KeyboardInterrupt:
            self.notifier.send("Dmitry Watchtower stopped", "Manually stopped.", priority=0)
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self.notifier.send("Dmitry Watchtower crashed", err[:900], priority=1)
            raise


if __name__ == "__main__":
    Watchtower().run()
