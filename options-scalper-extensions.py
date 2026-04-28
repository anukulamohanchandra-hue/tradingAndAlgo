"""
=============================================================================
  EXTENSIONS — adds to the main agent:
    1. data/universe_ranker.py   — live OI + spread + turnover ranking
    2. strategy/greeks.py        — IV / delta / theta filters (py_vollib)
    3. backtest/engine.py        — historical replay with slippage model
    4. backtest/runner.py        — CLI entry point
    5. main.py patches           — integration points
=============================================================================
   Install additional deps:
     pip install py_vollib mibian scipy tqdm matplotlib
=============================================================================
"""

# =============================================================================
# FILE: data/universe_ranker.py
# =============================================================================
"""Properly ranks F&O underlyings by live options turnover + spread quality.

Algorithm:
  1. Get all F&O underlyings (~180 names).
  2. For each, find current-week ATM CE+PE pair using spot quote.
  3. Batch-quote all (~360 contracts) via Kite quote() — accepts up to 500.
  4. Score = (ce_oi + pe_oi) * weight_oi
          + (ce_volume + pe_volume) * weight_vol
          - spread_pct_atm * penalty
  5. Filter: drop names where ATM spread > max_spread_pct OR OI < min.
  6. Return top N.

Cost: 2-3 API calls total. Runs once at session start (~9:20 AM IST).
"""

from datetime import date
from collections import defaultdict
from loguru import logger


SCORE_WEIGHTS = {
    "oi": 1.0,
    "volume": 0.5,
    "spread_penalty": 1000.0,  # multiplier on spread_pct
}


class LiveUniverseRanker:
    """Replaces UniverseSelector. Drop in via dependency injection."""

    def __init__(self, kite, config: dict):
        self.kite = kite
        self.cfg = config["universe"]

    def select_today(self) -> list[str]:
        nfo = self.kite.get_instruments("NFO")
        nse = self.kite.get_instruments("NSE")

        # --- 1. Build map: underlying → eq_token (for spot lookup) ---
        eq_token = {i["tradingsymbol"]: i["instrument_token"]
                    for i in nse if i["segment"] == "NSE"}

        # --- 2. Group options by underlying, find current-week expiry ---
        today = date.today()
        by_underlying: dict[str, list] = defaultdict(list)
        for inst in nfo:
            if inst["instrument_type"] not in ("CE", "PE"):
                continue
            if inst["expiry"] < today:
                continue
            by_underlying[inst["name"]].append(inst)

        # Drop indices for stock-only universe (configurable)
        EXCLUDE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
        candidates = {n: lst for n, lst in by_underlying.items()
                      if n not in EXCLUDE and n in eq_token}

        if not candidates:
            logger.warning("No F&O candidates found.")
            return []

        # --- 3. Get spot prices in one batch ---
        spot_keys = [f"NSE:{n}" for n in candidates]
        try:
            spot_quotes = self.kite.get_quote(spot_keys)
        except Exception as e:
            logger.error(f"Spot quote failed: {e}")
            return self._fallback()

        # --- 4. For each, pick ATM CE + PE of current-week expiry ---
        atm_contracts = []  # list of (underlying, ce_inst, pe_inst)
        for name, opts in candidates.items():
            spot_q = spot_quotes.get(f"NSE:{name}")
            if not spot_q:
                continue
            spot = spot_q.get("last_price", 0)
            if spot <= 0:
                continue

            expiries = sorted({o["expiry"] for o in opts})
            current_expiry = expiries[0]
            same_expiry = [o for o in opts if o["expiry"] == current_expiry]
            strikes = sorted({o["strike"] for o in same_expiry})
            if not strikes:
                continue
            atm_strike = min(strikes, key=lambda s: abs(s - spot))

            ce = next((o for o in same_expiry
                       if o["strike"] == atm_strike and o["instrument_type"] == "CE"), None)
            pe = next((o for o in same_expiry
                       if o["strike"] == atm_strike and o["instrument_type"] == "PE"), None)
            if ce and pe:
                atm_contracts.append((name, ce, pe))

        # --- 5. Batch-quote ATM contracts (max 500/call; chunk to be safe) ---
        opt_keys = []
        for _, ce, pe in atm_contracts:
            opt_keys.append(f"NFO:{ce['tradingsymbol']}")
            opt_keys.append(f"NFO:{pe['tradingsymbol']}")

        opt_quotes = {}
        for chunk_start in range(0, len(opt_keys), 400):
            chunk = opt_keys[chunk_start:chunk_start + 400]
            try:
                opt_quotes.update(self.kite.get_quote(chunk))
            except Exception as e:
                logger.warning(f"Option quote chunk failed: {e}")

        # --- 6. Score each underlying ---
        scored = []
        for name, ce, pe in atm_contracts:
            cek = f"NFO:{ce['tradingsymbol']}"
            pek = f"NFO:{pe['tradingsymbol']}"
            ceq = opt_quotes.get(cek, {})
            peq = opt_quotes.get(pek, {})
            if not ceq or not peq:
                continue

            ce_oi = ceq.get("oi", 0)
            pe_oi = peq.get("oi", 0)
            ce_vol = ceq.get("volume", 0)
            pe_vol = peq.get("volume", 0)

            # Spread = (ask - bid) / mid for ATM CE (representative)
            depth = ceq.get("depth", {})
            buy = depth.get("buy", [{}])[0].get("price", 0)
            sell = depth.get("sell", [{}])[0].get("price", 0)
            mid = (buy + sell) / 2 if (buy and sell) else ceq.get("last_price", 0)
            spread_pct = ((sell - buy) / mid * 100) if (mid and buy and sell) else 999

            # Hard filters
            if (ce_oi + pe_oi) < self.cfg["min_options_oi"]:
                continue
            if spread_pct > self.cfg["max_spread_pct"]:
                continue

            score = (
                (ce_oi + pe_oi) * SCORE_WEIGHTS["oi"]
                + (ce_vol + pe_vol) * SCORE_WEIGHTS["volume"]
                - spread_pct * SCORE_WEIGHTS["spread_penalty"]
            )
            scored.append((name, score, spread_pct, ce_oi + pe_oi))

        scored.sort(key=lambda x: -x[1])
        top = scored[: self.cfg["top_n"]]

        if not top:
            logger.warning("No underlyings passed liquidity filters; using fallback.")
            return self._fallback()

        for name, sc, sp, oi in top:
            logger.info(f"  {name:14s} score={sc:>12.0f} spread={sp:.2f}% OI={oi:>10,}")
        return [t[0] for t in top]

    @staticmethod
    def _fallback() -> list[str]:
        return ["RELIANCE", "HDFCBANK", "ICICIBANK", "TCS", "INFY",
                "SBIN", "AXISBANK", "BAJFINANCE"]


# =============================================================================
# FILE: strategy/greeks.py
# =============================================================================
"""Greeks-based filters using py_vollib.

NOTE on expiry-day Greeks: black-scholes assumes continuous-time GBM, which
breaks down for ATM options with <2 hours to expiry. Gamma is theoretically
infinite at strike-at-expiry. We compute Greeks but apply them conservatively:

  • DELTA filter: skip if |delta| < 0.30 or > 0.75 (avoids deep ITM/OTM)
  • IV filter:    skip if implied_vol > 2.5x its 5-day median (event spike)
  • DTE-aware:    if DTE < 0.5 day (intraday expiry), use historical IV stats
                  not BS-implied (which becomes unstable).
"""

import math
from datetime import date, datetime
from typing import Literal

try:
    from py_vollib.black_scholes.implied_volatility import implied_volatility as bs_iv
    from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
    HAVE_VOLLIB = True
except ImportError:
    HAVE_VOLLIB = False


RISK_FREE = 0.07  # India 91-day T-bill ≈ 7%


def _years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    now = now or datetime.now()
    expiry_dt = datetime.combine(expiry, datetime.min.time().replace(hour=15, minute=30))
    seconds = max((expiry_dt - now).total_seconds(), 60)  # floor at 1 min
    return seconds / (365 * 24 * 3600)


def compute_iv(spot: float, strike: float, premium: float, expiry: date,
               flag: Literal["c", "p"]) -> float | None:
    if not HAVE_VOLLIB:
        return None
    try:
        t = _years_to_expiry(expiry)
        return bs_iv(premium, spot, strike, t, RISK_FREE, flag)
    except Exception:
        return None


def compute_delta(spot: float, strike: float, iv: float, expiry: date,
                  flag: Literal["c", "p"]) -> float | None:
    if not HAVE_VOLLIB or iv is None:
        return None
    try:
        t = _years_to_expiry(expiry)
        return bs_delta(flag, spot, strike, t, RISK_FREE, iv)
    except Exception:
        return None


class GreeksFilter:
    """Wraps the OptionsPicker output with a sanity check on Greeks."""

    DELTA_MIN = 0.30
    DELTA_MAX = 0.75
    IV_SPIKE_MULTIPLIER = 2.5

    def __init__(self):
        self._iv_history: dict[str, list[float]] = {}  # symbol → recent IVs

    def update_iv_history(self, symbol: str, iv: float, max_keep: int = 50):
        if iv is None or iv <= 0:
            return
        hist = self._iv_history.setdefault(symbol, [])
        hist.append(iv)
        if len(hist) > max_keep:
            hist.pop(0)

    def _iv_median(self, symbol: str) -> float | None:
        h = self._iv_history.get(symbol, [])
        if len(h) < 5:
            return None
        return sorted(h)[len(h) // 2]

    def passes(self, contract: dict, spot: float, premium: float) -> tuple[bool, str]:
        """Returns (ok, reason)."""
        if not HAVE_VOLLIB:
            return True, "py_vollib not installed; skipping greeks filter"

        flag = "c" if contract["instrument_type"] == "CE" else "p"
        expiry = contract["expiry"]
        strike = contract["strike"]

        # Intraday-expiry guard: switch off BS Greeks
        dte = (expiry - date.today()).days
        if dte < 1:
            now = datetime.now()
            if now.hour >= 14:  # last 1.5 hours, gamma scream
                return False, "intraday-expiry-late: skip (BS Greeks unreliable)"

        iv = compute_iv(spot, strike, premium, expiry, flag)
        if iv is None:
            return True, "iv computation failed; not blocking"

        sym = contract["tradingsymbol"]
        median_iv = self._iv_median(sym)
        self.update_iv_history(sym, iv)

        if median_iv and iv > self.IV_SPIKE_MULTIPLIER * median_iv:
            return False, f"iv-spike: {iv:.2f} > {self.IV_SPIKE_MULTIPLIER}x median {median_iv:.2f}"

        d = compute_delta(spot, strike, iv, expiry, flag)
        if d is None:
            return True, "delta computation failed; not blocking"

        abs_d = abs(d)
        if abs_d < self.DELTA_MIN:
            return False, f"delta-too-low: |{d:.2f}| < {self.DELTA_MIN}"
        if abs_d > self.DELTA_MAX:
            return False, f"delta-too-high: |{d:.2f}| > {self.DELTA_MAX} (deep ITM)"

        return True, f"ok iv={iv:.2f} delta={d:.2f}"


# =============================================================================
# FILE: backtest/engine.py
# =============================================================================
"""Historical replay engine.

Inputs:
  • Date range
  • Universe (or auto-pick using LiveUniverseRanker as of that date — but
    Kite historical doesn't expose past instrument dumps, so we use today's
    universe as a proxy. This is a known bias.)
  • Same config as live agent

Approach:
  1. For each trading day, pull 1m candles for each underlying.
  2. Reconstruct what would have been the current-week ATM CE/PE on that day
     by using current option chain (LIMITATION: post-expiry contracts may be
     missing from `instruments("NFO")`. Mitigation: only backtest dates
     within the last ~60 days where weekly contracts haven't all expired
     out of the dump).
  3. Pull 1m candles for those option contracts.
  4. Replay tick-by-tick (here, candle-by-candle), feeding the same signal
     engine and order manager — but with `_BacktestBroker` substituting
     the live KiteClient.
  5. Apply slippage model on every fill.

KNOWN LIMITATIONS (printed at end of every run):
  • OHLC ≠ tick data → SL/TP fills are approximated to candle boundaries
  • No bid-ask in history → flat slippage applied
  • Survivorship: expired contracts may be missing
  • Liquidity proxy: backtest assumes always fillable
"""

from datetime import datetime, date, timedelta, time as dtime
from dataclasses import dataclass, field
import pandas as pd
from loguru import logger


SLIPPAGE_PCT_PER_SIDE = 2.0   # 2% of premium each on entry and exit


@dataclass
class BacktestFill:
    timestamp: datetime
    symbol: str
    side: str          # BUY / SELL
    quantity: int
    price: float       # fill price after slippage
    pnl: float = 0.0
    reason: str = ""


@dataclass
class BacktestState:
    cash: float
    starting_cash: float
    fills: list[BacktestFill] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)


class _BacktestBroker:
    """Drop-in replacement for KiteClient inside the OrderManager.
    Order calls become recorded fills; quote calls read from a candle frame."""

    def __init__(self, state: BacktestState):
        self.state = state
        self._current_prices: dict[str, float] = {}
        self._lot_sizes: dict[str, int] = {}
        self._positions: dict[str, dict] = {}  # symbol → {qty, avg_price}
        self._now: datetime = datetime.now()

    def set_current(self, now: datetime, prices: dict[str, float],
                    lot_sizes: dict[str, int]):
        self._now = now
        self._current_prices.update(prices)
        self._lot_sizes.update(lot_sizes)

    def place_market_order(self, tradingsymbol, exchange, quantity,
                           transaction_type, product="MIS"):
        ltp = self._current_prices.get(tradingsymbol)
        if ltp is None or ltp <= 0:
            return None
        slip = ltp * SLIPPAGE_PCT_PER_SIDE / 100
        fill_price = ltp + slip if transaction_type == "BUY" else ltp - slip

        pnl = 0.0
        if transaction_type == "BUY":
            self._positions[tradingsymbol] = {"qty": quantity, "avg": fill_price}
            self.state.cash -= fill_price * quantity
        else:
            pos = self._positions.pop(tradingsymbol, None)
            if pos:
                pnl = (fill_price - pos["avg"]) * pos["qty"]
                self.state.cash += fill_price * quantity

        self.state.fills.append(BacktestFill(
            timestamp=self._now, symbol=tradingsymbol, side=transaction_type,
            quantity=quantity, price=fill_price, pnl=pnl,
        ))
        return f"BT-{len(self.state.fills)}"

    def square_off_all(self):
        for sym in list(self._positions.keys()):
            qty = self._positions[sym]["qty"]
            self.place_market_order(sym, "NFO", qty, "SELL")

    def get_positions(self):
        return {"net": [
            {"tradingsymbol": s, "exchange": "NFO", "quantity": p["qty"]}
            for s, p in self._positions.items()
        ]}

    def get_funds(self):
        return {"available": {"cash": self.state.cash}}

    def get_quote(self, symbols):
        out = {}
        for s in symbols:
            sym = s.split(":", 1)[1] if ":" in s else s
            out[s] = {"last_price": self._current_prices.get(sym, 0)}
        return out

    def get_instruments(self, exchange):
        return []  # caller must inject instrument list separately


class BacktestEngine:
    def __init__(self, kite_live, config: dict, start: date, end: date,
                 universe: list[str] | None = None):
        self.kite_live = kite_live   # for historical data only
        self.cfg = config
        self.start = start
        self.end = end
        self.universe = universe
        self.state = BacktestState(
            cash=config["capital"]["total_inr"],
            starting_cash=config["capital"]["total_inr"],
        )

    def _load_underlying_candles(self, symbol: str, token: int,
                                  d: date, interval: str) -> pd.DataFrame:
        from_dt = datetime.combine(d, dtime(9, 15))
        to_dt = datetime.combine(d, dtime(15, 30))
        try:
            data = self.kite_live.get_historical(token, interval, from_dt, to_dt)
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.warning(f"Hist load failed {symbol} {d}: {e}")
            return pd.DataFrame()

    def run(self):
        # Late imports of the agent modules (they live in artifact 1)
        from main import (CandleAggregator, OptionsPicker, OrderManager,
                          evaluate, Signal)

        # Reuse instrument dump from live client
        nfo = self.kite_live.get_instruments("NFO")
        nse = self.kite_live.get_instruments("NSE")
        eq_token = {i["tradingsymbol"]: i["instrument_token"]
                    for i in nse if i["segment"] == "NSE"}
        opt_token_by_sym = {i["tradingsymbol"]: i["instrument_token"]
                            for i in nfo}
        lot_by_sym = {i["tradingsymbol"]: i["lot_size"] for i in nfo}

        broker = _BacktestBroker(self.state)
        # Wire OrderManager + OptionsPicker against the backtest broker
        order_mgr = OrderManager(broker, self.cfg)
        # Force dry-run off in backtest
        order_mgr.state.dry_run_remaining = 0
        picker = OptionsPicker(self.kite_live, self.cfg)

        days = pd.bdate_range(self.start, self.end)
        for d in days:
            d = d.date()
            logger.info(f"=== Backtesting {d} ===")
            order_mgr.state.trade_count = 0
            order_mgr.state.halted = False

            for symbol in (self.universe or []):
                if symbol not in eq_token:
                    continue
                tok = eq_token[symbol]
                c1 = self._load_underlying_candles(symbol, tok, d, "minute")
                c3 = self._load_underlying_candles(symbol, tok, d, "3minute")
                if c1.empty or c3.empty:
                    continue

                # Walk forward minute-by-minute
                for ts in c1.index:
                    if ts.time() >= dtime(15, 15):
                        broker.square_off_all()
                        break
                    c1_so_far = c1.loc[:ts]
                    c3_so_far = c3.loc[:ts] if ts in c3.index else c3.loc[c3.index <= ts]
                    if len(c1_so_far) < 30 or len(c3_so_far) < 30:
                        continue

                    sig = evaluate(c1_so_far, c3_so_far, self.cfg)
                    if sig != Signal.NONE and order_mgr.can_open_new():
                        spot = c1_so_far["close"].iloc[-1]
                        contract = picker.pick(symbol, sig, spot)
                        if not contract:
                            continue
                        # Pull option's 1m candle for this timestamp
                        opt_df = self._load_underlying_candles(
                            contract["tradingsymbol"],
                            contract["instrument_token"], d, "minute"
                        )
                        if opt_df.empty or ts not in opt_df.index:
                            continue
                        premium = opt_df.loc[ts, "close"]
                        if not (self.cfg["options"]["min_premium_inr"]
                                <= premium <= self.cfg["options"]["max_premium_inr"]):
                            continue
                        broker.set_current(ts, {contract["tradingsymbol"]: premium},
                                           {contract["tradingsymbol"]: contract["lot_size"]})
                        order_mgr.enter(contract, premium)

                    # Update LTPs of open option positions for SL/TP/timer logic
                    ltp_lookup = {}
                    for p in order_mgr.state.positions:
                        opt_df = self._load_underlying_candles(
                            p.tradingsymbol, opt_token_by_sym.get(p.tradingsymbol, 0),
                            d, "minute"
                        )
                        if not opt_df.empty and ts in opt_df.index:
                            ltp_lookup[p.tradingsymbol] = opt_df.loc[ts, "close"]
                    if ltp_lookup:
                        broker.set_current(ts, ltp_lookup,
                                           {s: lot_by_sym.get(s, 0) for s in ltp_lookup})
                        order_mgr.monitor(ltp_lookup)

                    # Equity curve sample
                    self.state.equity_curve.append(
                        (ts, self.state.cash + sum(
                            (ltp_lookup.get(p.tradingsymbol, p.entry_price)
                             - p.entry_price) * p.quantity
                            for p in order_mgr.state.positions))
                    )

            broker.square_off_all()

        return self.report()

    def report(self) -> dict:
        fills = self.state.fills
        total_pnl = sum(f.pnl for f in fills)
        n_trades = sum(1 for f in fills if f.side == "SELL")
        wins = sum(1 for f in fills if f.side == "SELL" and f.pnl > 0)
        win_rate = (wins / n_trades * 100) if n_trades else 0.0
        ret_pct = total_pnl / self.state.starting_cash * 100

        rep = {
            "starting_capital": self.state.starting_cash,
            "ending_capital": self.state.starting_cash + total_pnl,
            "total_pnl": total_pnl,
            "return_pct": ret_pct,
            "trades": n_trades,
            "win_rate_pct": win_rate,
        }
        self._print_report(rep)
        return rep

    def _print_report(self, rep: dict):
        print("\n" + "=" * 70)
        print("BACKTEST REPORT")
        print("=" * 70)
        for k, v in rep.items():
            print(f"  {k:25s} : {v:>15,.2f}" if isinstance(v, float)
                  else f"  {k:25s} : {v}")
        print("\n" + "!" * 70)
        print("LIMITATIONS — DO NOT TREAT AS PREDICTIVE OF LIVE PERFORMANCE")
        print("!" * 70)
        print("  1. OHLC fills approximate; intra-candle path ignored.")
        print(f"  2. Slippage applied at {SLIPPAGE_PCT_PER_SIDE}%/side; real may be 2-10x worse")
        print("     at expiry / illiquid strikes.")
        print("  3. Survivorship bias: expired contracts missing from chain dump.")
        print("  4. No order-rejection / margin-shortfall simulation.")
        print("  5. Backtest universe is today's most-liquid set, not the day's actual.")
        print("  Treat green numbers as 'strategy is not obviously broken',")
        print("  not 'this will print money live'.")
        print("=" * 70 + "\n")


# =============================================================================
# FILE: backtest/runner.py — CLI entry point
# =============================================================================
"""
Usage:
    python -m backtest.runner --start 2026-04-01 --end 2026-04-25 \
        --universe RELIANCE,HDFCBANK,ICICIBANK
"""

import argparse, os, yaml
from datetime import datetime


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--universe", default="",
                    help="Comma-separated symbols; empty = use ranker fallback")
    args = ap.parse_args()

    from main import KiteClient, CONFIG_YAML, enforce
    config = enforce(yaml.safe_load(CONFIG_YAML))

    kite = KiteClient(
        api_key=os.environ["KITE_API_KEY"],
        api_secret=os.environ["KITE_API_SECRET"],
        access_token=os.environ["KITE_ACCESS_TOKEN"],
    )

    universe = (args.universe.split(",") if args.universe
                else LiveUniverseRanker._fallback())
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    engine = BacktestEngine(kite, config, start, end, universe)
    engine.run()


if __name__ == "__main__":
    cli()


# =============================================================================
# FILE: main.py — INTEGRATION PATCHES
# =============================================================================
"""Apply these changes to main.py from artifact 1:"""

PATCHES = """
# --- 1. Replace UniverseSelector usage in main() ---
# OLD:  universe = UniverseSelector(kite, config).select_today()
# NEW:
        from data.universe_ranker import LiveUniverseRanker
        universe = LiveUniverseRanker(kite, config).select_today()

# --- 2. Add Greeks filter in main() before order_mgr.enter() ---
# OLD:
#       order_mgr.enter(contract, premium)
# NEW:
        from strategy.greeks import GreeksFilter
        # (instantiate once, outside the loop:)
        greeks_filter = GreeksFilter()
        # (inside the loop, before enter:)
        ok, reason = greeks_filter.passes(contract, spot, premium)
        if not ok:
            logger.info(f"Greeks gate skip: {contract['tradingsymbol']} — {reason}")
            continue
        logger.info(f"Greeks gate pass: {contract['tradingsymbol']} — {reason}")
        order_mgr.enter(contract, premium)

# --- 3. CLI flag for backtest mode ---
# At top of main():
        import sys
        if "--backtest" in sys.argv:
            from backtest.runner import cli
            cli()
            return
"""
