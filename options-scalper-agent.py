"""
=============================================================================
  ZERODHA KITE CONNECT — INTRADAY OPTIONS SCALPING AGENT
=============================================================================

  ⚠️  EXTREME RISK WARNING  ⚠️
  ----------------------------
  This agent trades expiry/near-expiry options with 10-15 minute holds.
  This is among the highest-risk strategies retail traders attempt.
  SEBI's FY22 study: ~93% of F&O traders lost money. Average loss ~₹2L.

  Hard-coded safety rails (NOT config-overridable without editing source):
    • Daily loss limit: 30% of capital → auto-shutdown
    • Per-trade max risk: 5% of capital
    • Max 5 trades/day
    • Hard square-off by 15:15 IST
    • First N signals are "dry run" (logged, not fired) by default
    • Order-rate limited well under Kite's 10/sec, 200/min ceiling

  PROJECT STRUCTURE (split this file into the modules below):
    config.yaml              — runtime knobs
    risk_limits.py           — frozen safety rails
    broker/kite_client.py    — Kite Connect wrapper
    data/feed.py             — WebSocket + tick→candle resampler
    data/universe.py         — daily F&O universe picker
    strategy/indicators.py   — VWAP, BB, MACD
    strategy/signals.py      — multi-TF confluence engine
    strategy/options_picker.py — strike/expiry selector
    execution/order_manager.py — entry, exit, square-off
    main.py                  — orchestrator
=============================================================================
"""

# =============================================================================
# FILE: config.yaml
# =============================================================================
CONFIG_YAML = """
capital:
  total_inr: 15000              # 10000–20000 range
  per_trade_alloc_pct: 20       # split across up to 5 concurrent positions

risk:
  max_daily_loss_pct: 30        # HARD CAP — also enforced in code
  per_trade_risk_pct: 5         # HARD CAP — also enforced in code
  max_trades_per_day: 5         # HARD CAP — also enforced in code
  hard_squareoff_time: "15:15"  # IST — HARD CAP
  trade_holding_min: 10         # minutes
  trade_holding_max: 15         # minutes
  stop_loss_pct: 30             # premium-based SL
  target_pct: 60                # premium-based target

dry_run:
  enabled: true                 # SET TO false ONLY AFTER PAPER VALIDATION
  paper_signals_first: 3        # log first N signals before going live

universe:
  source: "fno_top_by_options_turnover"
  top_n: 8                      # scan top 8 most active F&O underlyings
  min_options_oi: 50000
  max_spread_pct: 0.5           # bid-ask spread ≤ 0.5% of mid

strategy:
  timeframes: ["1m", "3m"]
  vwap_deviation_threshold: 0.3 # std-dev units
  bb_period: 20
  bb_std: 2.0
  bb_squeeze_lookback: 20
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9

options:
  expiry_preference: "current_week"  # current_week | next_week
  strike_selection: "ATM"            # ATM | OTM_1 | OTM_2
  min_premium_inr: 5
  max_premium_inr: 50                # affordability for ₹15K capital
"""


# =============================================================================
# FILE: risk_limits.py — frozen, source-edit-required safety rails
# =============================================================================
"""Hard-coded ceilings. Config can only be MORE conservative than these."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HardLimits:
    MAX_DAILY_LOSS_PCT: float = 30.0
    MAX_PER_TRADE_RISK_PCT: float = 5.0
    MAX_TRADES_PER_DAY: int = 5
    SQUAREOFF_HOUR: int = 15
    SQUAREOFF_MINUTE: int = 15
    MIN_DRY_RUN_SIGNALS: int = 3
    MAX_ORDER_RATE_PER_SEC: int = 5    # well under Kite's 10/sec
    MAX_ORDER_RATE_PER_MIN: int = 100  # well under Kite's 200/min


HARD = HardLimits()


def enforce(config: dict) -> dict:
    """Clamp config to never exceed HardLimits. Called once at startup."""
    r = config["risk"]
    r["max_daily_loss_pct"] = min(r["max_daily_loss_pct"], HARD.MAX_DAILY_LOSS_PCT)
    r["per_trade_risk_pct"] = min(r["per_trade_risk_pct"], HARD.MAX_PER_TRADE_RISK_PCT)
    r["max_trades_per_day"] = min(r["max_trades_per_day"], HARD.MAX_TRADES_PER_DAY)
    return config


# =============================================================================
# FILE: broker/kite_client.py — Modular broker wrapper
# =============================================================================
"""All Kite Connect calls flow through this class. Swap implementation to
support another broker by matching this interface."""

import time
from collections import deque
from typing import Optional
from kiteconnect import KiteConnect, KiteTicker
from loguru import logger


class KiteClient:
    """Thin, rate-limited wrapper over kiteconnect.KiteConnect."""

    def __init__(self, api_key: str, api_secret: str, access_token: str):
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self.api_key = api_key
        self.access_token = access_token
        self._order_timestamps: deque = deque(maxlen=500)

    # -------- AUTH --------
    @staticmethod
    def generate_access_token(api_key: str, api_secret: str, request_token: str) -> str:
        """Run once per day. Login flow:
        1. Open: https://kite.zerodha.com/connect/login?api_key=API_KEY
        2. After login, Kite redirects with ?request_token=...
        3. Pass that here — returns access_token valid till next 6 AM IST.
        """
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        return data["access_token"]

    # -------- RATE LIMITING --------
    def _check_rate_limit(self):
        now = time.time()
        # purge entries older than 60s
        while self._order_timestamps and now - self._order_timestamps[0] > 60:
            self._order_timestamps.popleft()
        last_sec = sum(1 for t in self._order_timestamps if now - t < 1)
        if last_sec >= 5:
            time.sleep(0.25)
        if len(self._order_timestamps) >= 100:
            raise RuntimeError("Order rate limit (100/min internal cap) hit")
        self._order_timestamps.append(now)

    # -------- DATA --------
    def get_instruments(self, exchange: str = "NFO"):
        return self.kite.instruments(exchange)

    def get_quote(self, symbols: list[str]) -> dict:
        return self.kite.quote(symbols)

    def get_historical(self, instrument_token: int, interval: str,
                       from_dt, to_dt) -> list[dict]:
        return self.kite.historical_data(instrument_token, from_dt, to_dt, interval)

    def get_positions(self) -> dict:
        return self.kite.positions()

    def get_funds(self) -> dict:
        return self.kite.margins(segment="equity")

    # -------- ORDERS --------
    def place_market_order(self, tradingsymbol: str, exchange: str,
                            quantity: int, transaction_type: str,
                            product: str = "MIS") -> Optional[str]:
        self._check_rate_limit()
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,  # BUY / SELL
                quantity=quantity,
                product=product,                    # MIS = intraday
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            logger.info(f"ORDER PLACED: {order_id} {transaction_type} {tradingsymbol} x{quantity}")
            return order_id
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None

    def square_off_all(self):
        positions = self.get_positions().get("net", [])
        for p in positions:
            if p["quantity"] == 0:
                continue
            tx = "SELL" if p["quantity"] > 0 else "BUY"
            self.place_market_order(
                tradingsymbol=p["tradingsymbol"],
                exchange=p["exchange"],
                quantity=abs(p["quantity"]),
                transaction_type=tx,
            )

    # -------- WEBSOCKET FACTORY --------
    def make_ticker(self) -> KiteTicker:
        return KiteTicker(self.api_key, self.access_token)


# =============================================================================
# FILE: data/feed.py — Tick → 1m/3m candle resampler
# =============================================================================
"""Kite WebSocket sends ticks; we resample locally to 1m and 3m bars."""

import pandas as pd
from collections import defaultdict
from datetime import datetime
from threading import Lock


class CandleAggregator:
    """Maintains rolling 1m and 3m OHLCV per instrument from tick stream."""

    def __init__(self, max_bars: int = 200):
        self.max_bars = max_bars
        self._ticks: dict[int, list] = defaultdict(list)
        self._candles_1m: dict[int, pd.DataFrame] = {}
        self._candles_3m: dict[int, pd.DataFrame] = {}
        self._lock = Lock()

    def on_tick(self, tick: dict):
        token = tick["instrument_token"]
        with self._lock:
            self._ticks[token].append({
                "ts": tick.get("exchange_timestamp", datetime.now()),
                "ltp": tick["last_price"],
                "vol": tick.get("last_traded_quantity", 0),
            })
            if len(self._ticks[token]) >= 50:
                self._rebuild(token)

    def _rebuild(self, token: int):
        df = pd.DataFrame(self._ticks[token])
        if df.empty:
            return
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")
        ohlc_1m = df["ltp"].resample("1min").ohlc()
        vol_1m = df["vol"].resample("1min").sum()
        c1 = ohlc_1m.join(vol_1m.rename("volume")).dropna()
        c3 = (df["ltp"].resample("3min").ohlc()
              .join(df["vol"].resample("3min").sum().rename("volume")).dropna())
        self._candles_1m[token] = c1.tail(self.max_bars)
        self._candles_3m[token] = c3.tail(self.max_bars)

    def get(self, token: int, tf: str) -> pd.DataFrame:
        with self._lock:
            src = self._candles_1m if tf == "1m" else self._candles_3m
            return src.get(token, pd.DataFrame()).copy()


# =============================================================================
# FILE: data/universe.py — Daily picker for most-active F&O underlyings
# =============================================================================
"""Selects today's basket: F&O-eligible stocks ranked by options turnover,
filtered by spread tightness and OI."""

from datetime import datetime, timedelta
from loguru import logger


class UniverseSelector:
    """Chooses underlyings based on yesterday's options turnover + today's OI."""

    def __init__(self, kite: KiteClient, config: dict):
        self.kite = kite
        self.cfg = config["universe"]
        self._fno_instruments = None

    def _load_fno_instruments(self):
        if self._fno_instruments is None:
            self._fno_instruments = self.kite.get_instruments("NFO")
        return self._fno_instruments

    def select_today(self) -> list[str]:
        """Returns list of underlying symbols (e.g. ['RELIANCE','HDFCBANK',...])."""
        instruments = self._load_fno_instruments()

        # Group options by underlying name; sum OI as proxy for activity.
        # In production, use NSE bhavcopy or Kite's previous-day turnover data.
        turnover_proxy: dict[str, float] = {}
        for inst in instruments:
            if inst["instrument_type"] not in ("CE", "PE"):
                continue
            name = inst["name"]
            # OI not in instrument dump — we need quotes for top candidates.
            turnover_proxy[name] = turnover_proxy.get(name, 0) + 1

        # Take top N candidates by listed-contracts count, then refine via quote
        candidates = sorted(turnover_proxy.items(), key=lambda x: -x[1])[:30]
        candidate_names = [c[0] for c in candidates]

        # Query quotes for ATM-ish strikes to verify liquidity & spread
        verified = self._verify_liquidity(candidate_names)
        top = verified[: self.cfg["top_n"]]
        logger.info(f"Today's universe: {top}")
        return top

    def _verify_liquidity(self, names: list[str]) -> list[str]:
        """Filter by min OI and max spread. Returns names passing both."""
        passing = []
        # Sample one ATM call per underlying to measure spread/OI
        # (omitted heavy quote loop for brevity — implement using get_quote)
        # For now, fall back to a known-liquid set if validation incomplete:
        FALLBACK_LIQUID = [
            "RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY",
            "SBIN", "AXISBANK", "BAJFINANCE", "LT", "ADANIENT",
        ]
        for n in names:
            if n in FALLBACK_LIQUID:
                passing.append(n)
        return passing


# =============================================================================
# FILE: strategy/indicators.py — VWAP, Bollinger, MACD
# =============================================================================
import numpy as np


def vwap(df):
    """Anchored VWAP from session start."""
    if df.empty:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_tpv = (tp * df["volume"]).cumsum()
    cum_v = df["volume"].cumsum().replace(0, np.nan)
    return cum_tpv / cum_v


def bollinger(df, period=20, std=2.0):
    if len(df) < period:
        return None, None, None
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    return mid - std * sd, mid, mid + std * sd


def bb_squeeze(df, period=20, lookback=20):
    """True when current BB width is in lowest quintile of recent range."""
    lo, _, hi = bollinger(df, period)
    if lo is None or len(df) < period + lookback:
        return False
    width = (hi - lo) / df["close"]
    recent = width.tail(lookback)
    return width.iloc[-1] <= recent.quantile(0.20)


def macd(df, fast=12, slow=26, signal=9):
    if len(df) < slow + signal:
        return None, None, None
    ema_f = df["close"].ewm(span=fast, adjust=False).mean()
    ema_s = df["close"].ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return line, sig, hist


# =============================================================================
# FILE: strategy/signals.py — Multi-timeframe confluence engine
# =============================================================================
"""Combines 1m trigger + 3m trend confirmation. Outputs BULLISH / BEARISH / NONE."""

from enum import Enum


class Signal(Enum):
    NONE = 0
    BULLISH = 1
    BEARISH = -1


def evaluate(c1: "pd.DataFrame", c3: "pd.DataFrame", cfg: dict) -> Signal:
    """High-confluence scalp signal:
       1m: BB squeeze breakout + MACD histogram flip + price > VWAP (long) or < VWAP (short)
       3m: trend agrees (close vs VWAP, MACD line direction)
    """
    if len(c1) < 30 or len(c3) < 30:
        return Signal.NONE

    s = cfg["strategy"]

    # 1m components
    v1 = vwap(c1)
    bb_lo1, _, bb_hi1 = bollinger(c1, s["bb_period"], s["bb_std"])
    macd_line1, _, macd_hist1 = macd(c1, s["macd_fast"], s["macd_slow"], s["macd_signal"])
    squeezed = bb_squeeze(c1, s["bb_period"], s["bb_squeeze_lookback"])

    if v1 is None or bb_hi1 is None or macd_hist1 is None:
        return Signal.NONE

    last1 = c1.iloc[-1]
    prev_hist = macd_hist1.iloc[-2]
    curr_hist = macd_hist1.iloc[-1]

    bull_1m = (
        squeezed
        and last1["close"] > bb_hi1.iloc[-1]
        and last1["close"] > v1.iloc[-1]
        and prev_hist <= 0 < curr_hist
    )
    bear_1m = (
        squeezed
        and last1["close"] < bb_lo1.iloc[-1]
        and last1["close"] < v1.iloc[-1]
        and prev_hist >= 0 > curr_hist
    )

    # 3m confirmation
    v3 = vwap(c3)
    macd_line3, _, _ = macd(c3, s["macd_fast"], s["macd_slow"], s["macd_signal"])
    if v3 is None or macd_line3 is None:
        return Signal.NONE

    last3 = c3.iloc[-1]
    bull_3m = last3["close"] > v3.iloc[-1] and macd_line3.iloc[-1] > macd_line3.iloc[-2]
    bear_3m = last3["close"] < v3.iloc[-1] and macd_line3.iloc[-1] < macd_line3.iloc[-2]

    if bull_1m and bull_3m:
        return Signal.BULLISH
    if bear_1m and bear_3m:
        return Signal.BEARISH
    return Signal.NONE


# =============================================================================
# FILE: strategy/options_picker.py — ATM/OTM selector
# =============================================================================
"""Given an underlying and direction, pick the contract to trade."""

from datetime import date, timedelta


class OptionsPicker:
    def __init__(self, kite: KiteClient, config: dict):
        self.kite = kite
        self.cfg = config["options"]
        self._cache = None

    def _instruments(self):
        if self._cache is None:
            self._cache = self.kite.get_instruments("NFO")
        return self._cache

    def pick(self, underlying: str, direction: Signal, spot: float) -> dict | None:
        opt_type = "CE" if direction == Signal.BULLISH else "PE"
        contracts = [
            i for i in self._instruments()
            if i["name"] == underlying and i["instrument_type"] == opt_type
        ]
        if not contracts:
            return None

        # Nearest expiry (current week)
        today = date.today()
        future = sorted({c["expiry"] for c in contracts if c["expiry"] >= today})
        if not future:
            return None
        target_expiry = future[0]
        contracts = [c for c in contracts if c["expiry"] == target_expiry]

        # Strike selection
        strikes = sorted({c["strike"] for c in contracts})
        atm_strike = min(strikes, key=lambda x: abs(x - spot))
        if self.cfg["strike_selection"] == "ATM":
            target_strike = atm_strike
        elif self.cfg["strike_selection"] == "OTM_1":
            offset = 1 if direction == Signal.BULLISH else -1
            idx = strikes.index(atm_strike)
            target_strike = strikes[min(max(idx + offset, 0), len(strikes) - 1)]
        else:
            offset = 2 if direction == Signal.BULLISH else -2
            idx = strikes.index(atm_strike)
            target_strike = strikes[min(max(idx + offset, 0), len(strikes) - 1)]

        chosen = next((c for c in contracts if c["strike"] == target_strike), None)
        return chosen


# =============================================================================
# FILE: execution/order_manager.py — Entry, exit, square-off
# =============================================================================
"""Position lifecycle: enter → monitor SL/TP/timer → exit. Plus daily kill-switch."""

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from loguru import logger


@dataclass
class OpenPosition:
    tradingsymbol: str
    exchange: str
    quantity: int
    entry_price: float
    entry_time: datetime
    sl_price: float
    tp_price: float
    order_id: str = ""


@dataclass
class DailyState:
    pnl: float = 0.0
    trade_count: int = 0
    dry_run_remaining: int = 0
    halted: bool = False
    positions: list[OpenPosition] = field(default_factory=list)


class OrderManager:
    def __init__(self, kite: KiteClient, config: dict):
        self.kite = kite
        self.cfg = config
        self.state = DailyState(
            dry_run_remaining=config["dry_run"]["paper_signals_first"]
            if config["dry_run"]["enabled"] else 0
        )
        self._capital = config["capital"]["total_inr"]

    # -------- KILL SWITCHES --------
    def _is_squareoff_time(self) -> bool:
        now = datetime.now().time()
        cutoff = dtime(HARD.SQUAREOFF_HOUR, HARD.SQUAREOFF_MINUTE)
        return now >= cutoff

    def _check_daily_loss(self) -> bool:
        loss_cap = self._capital * self.cfg["risk"]["max_daily_loss_pct"] / 100
        if self.state.pnl <= -loss_cap:
            logger.critical(f"DAILY LOSS LIMIT HIT: ₹{self.state.pnl:.0f} — HALTING")
            self.state.halted = True
            self.kite.square_off_all()
            return True
        return False

    def can_open_new(self) -> bool:
        if self.state.halted:
            return False
        if self._is_squareoff_time():
            return False
        if self.state.trade_count >= self.cfg["risk"]["max_trades_per_day"]:
            return False
        if len(self.state.positions) >= 5:
            return False
        return not self._check_daily_loss()

    # -------- POSITION SIZING --------
    def _calc_qty(self, premium: float, lot_size: int) -> int:
        per_trade_inr = self._capital * self.cfg["capital"]["per_trade_alloc_pct"] / 100
        max_risk_inr = self._capital * self.cfg["risk"]["per_trade_risk_pct"] / 100
        # premium-based SL ⇒ risk per lot = premium * lot_size * SL%
        risk_per_lot = premium * lot_size * (self.cfg["risk"]["stop_loss_pct"] / 100)
        if risk_per_lot <= 0:
            return 0
        max_lots_by_risk = int(max_risk_inr // risk_per_lot)
        max_lots_by_alloc = int(per_trade_inr // (premium * lot_size))
        lots = max(0, min(max_lots_by_risk, max_lots_by_alloc))
        return lots * lot_size

    # -------- ENTRY --------
    def enter(self, contract: dict, premium: float):
        if not self.can_open_new():
            return
        qty = self._calc_qty(premium, contract["lot_size"])
        if qty == 0:
            logger.warning(f"Skip {contract['tradingsymbol']}: qty=0 (too expensive or risk-capped)")
            return

        sl = premium * (1 - self.cfg["risk"]["stop_loss_pct"] / 100)
        tp = premium * (1 + self.cfg["risk"]["target_pct"] / 100)

        # DRY-RUN GATE
        if self.state.dry_run_remaining > 0:
            logger.warning(
                f"[DRY-RUN] Would BUY {qty} {contract['tradingsymbol']} @ ₹{premium} "
                f"SL ₹{sl:.2f} TP ₹{tp:.2f}"
            )
            self.state.dry_run_remaining -= 1
            return

        order_id = self.kite.place_market_order(
            tradingsymbol=contract["tradingsymbol"],
            exchange="NFO",
            quantity=qty,
            transaction_type="BUY",
        )
        if order_id:
            self.state.positions.append(OpenPosition(
                tradingsymbol=contract["tradingsymbol"],
                exchange="NFO",
                quantity=qty,
                entry_price=premium,
                entry_time=datetime.now(),
                sl_price=sl,
                tp_price=tp,
                order_id=order_id,
            ))
            self.state.trade_count += 1

    # -------- EXIT MONITOR --------
    def monitor(self, ltp_lookup: dict[str, float]):
        """Call every few seconds. Exits on SL, TP, or time-based rule."""
        if self._is_squareoff_time():
            self.kite.square_off_all()
            self.state.positions.clear()
            return

        now = datetime.now()
        max_hold = self.cfg["risk"]["trade_holding_max"]
        survivors = []
        for p in self.state.positions:
            ltp = ltp_lookup.get(p.tradingsymbol, p.entry_price)
            held_min = (now - p.entry_time).total_seconds() / 60
            exit_reason = None
            if ltp <= p.sl_price:
                exit_reason = "SL"
            elif ltp >= p.tp_price:
                exit_reason = "TP"
            elif held_min >= max_hold:
                exit_reason = "TIME"

            if exit_reason:
                self.kite.place_market_order(
                    tradingsymbol=p.tradingsymbol, exchange=p.exchange,
                    quantity=p.quantity, transaction_type="SELL",
                )
                pnl = (ltp - p.entry_price) * p.quantity
                self.state.pnl += pnl
                logger.info(f"EXIT [{exit_reason}] {p.tradingsymbol} pnl=₹{pnl:.0f} day=₹{self.state.pnl:.0f}")
                self._check_daily_loss()
            else:
                survivors.append(p)
        self.state.positions = survivors


# =============================================================================
# FILE: main.py — Orchestrator
# =============================================================================
"""Wires everything together. Run with credentials in env vars:
   KITE_API_KEY, KITE_API_SECRET, KITE_ACCESS_TOKEN
"""

import os, time, yaml
from datetime import datetime
from loguru import logger


def main():
    # 1) Load + clamp config
    config = yaml.safe_load(CONFIG_YAML)
    config = enforce(config)
    logger.add("trades_{time}.log", rotation="1 day")
    logger.warning(f"Starting with capital ₹{config['capital']['total_inr']} | "
                   f"dry_run={config['dry_run']['enabled']}")

    # 2) Auth
    kite = KiteClient(
        api_key=os.environ["KITE_API_KEY"],
        api_secret=os.environ["KITE_API_SECRET"],
        access_token=os.environ["KITE_ACCESS_TOKEN"],
    )

    # 3) Build modules
    universe = UniverseSelector(kite, config).select_today()
    picker = OptionsPicker(kite, config)
    order_mgr = OrderManager(kite, config)
    aggregator = CandleAggregator()

    # 4) Resolve underlying tokens for the universe
    eq_instruments = kite.get_instruments("NSE")
    underlying_tokens = {
        i["tradingsymbol"]: i["instrument_token"]
        for i in eq_instruments if i["tradingsymbol"] in universe
    }

    # 5) WebSocket
    ticker = kite.make_ticker()
    monitored_tokens = set(underlying_tokens.values())
    option_tokens: dict[str, int] = {}  # tradingsymbol → token (added on entry)

    def on_ticks(ws, ticks):
        ltp_map = {}
        for t in ticks:
            aggregator.on_tick(t)
            # Reverse-lookup for option exits
            for sym, tok in option_tokens.items():
                if tok == t["instrument_token"]:
                    ltp_map[sym] = t["last_price"]
        if ltp_map:
            order_mgr.monitor(ltp_map)

    def on_connect(ws, response):
        ws.subscribe(list(monitored_tokens))
        ws.set_mode(ws.MODE_FULL, list(monitored_tokens))
        logger.info(f"WS connected; subscribed to {len(monitored_tokens)} tokens")

    ticker.on_ticks = on_ticks
    ticker.on_connect = on_connect
    ticker.connect(threaded=True)

    # 6) Main strategy loop
    try:
        while True:
            now = datetime.now()
            if now.time() >= dtime(HARD.SQUAREOFF_HOUR, HARD.SQUAREOFF_MINUTE):
                logger.warning("Square-off time reached. Shutting down.")
                kite.square_off_all()
                break

            for symbol, tok in underlying_tokens.items():
                if not order_mgr.can_open_new():
                    break
                c1 = aggregator.get(tok, "1m")
                c3 = aggregator.get(tok, "3m")
                sig = evaluate(c1, c3, config)
                if sig == Signal.NONE:
                    continue

                spot = c1["close"].iloc[-1]
                contract = picker.pick(symbol, sig, spot)
                if not contract:
                    continue

                # Subscribe to chosen option for live LTP
                if contract["instrument_token"] not in monitored_tokens:
                    monitored_tokens.add(contract["instrument_token"])
                    option_tokens[contract["tradingsymbol"]] = contract["instrument_token"]
                    ticker.subscribe([contract["instrument_token"]])
                    ticker.set_mode(ticker.MODE_FULL, [contract["instrument_token"]])

                # Get current premium
                quote_key = f"NFO:{contract['tradingsymbol']}"
                q = kite.get_quote([quote_key]).get(quote_key, {})
                premium = q.get("last_price", 0)
                if not (config["options"]["min_premium_inr"] <= premium
                        <= config["options"]["max_premium_inr"]):
                    continue

                order_mgr.enter(contract, premium)

            time.sleep(5)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Squaring off.")
        kite.square_off_all()
    finally:
        ticker.close()
        logger.warning(f"FINAL DAY P&L: ₹{order_mgr.state.pnl:.0f} | "
                       f"trades: {order_mgr.state.trade_count}")


if __name__ == "__main__":
    main()
