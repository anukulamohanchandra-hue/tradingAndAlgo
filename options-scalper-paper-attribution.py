"""
=============================================================================
  PAPER TRADING + ATTRIBUTION ANALYSIS
=============================================================================
  Files in this artifact:
    broker/paper_broker.py        — drop-in for KiteClient, fills simulated
    analytics/trade_recorder.py   — per-trade structured log to SQLite
    analytics/attribution.py      — slice P&L by signal/symbol/time/etc
    dashboard/attribution_tab.py  — new dashboard tab using the above
    main.py patches               — mode switch (live | paper | backtest)
=============================================================================
   Install:
     pip install sqlalchemy
=============================================================================
"""

# =============================================================================
# FILE: broker/paper_broker.py
# =============================================================================
"""Simulates fills using live Kite quotes, but never sends real orders.

Why this is different from the backtest broker:
  • Backtest replays historical OHLC, no live data.
  • Paper broker uses LIVE WebSocket ticks for realism — same data path
    the live agent would see — but routes orders to a simulated book.

Implementation: it WRAPS a real KiteClient (for quote/historical/ticker)
but overrides place_market_order, square_off_all, get_positions to be
in-memory simulations with slippage.
"""

import time
from datetime import datetime
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class _SimPosition:
    tradingsymbol: str
    exchange: str
    quantity: int
    avg_price: float
    entry_time: datetime


class PaperBroker:
    """Quacks like KiteClient. Use anywhere KiteClient is used."""

    SLIPPAGE_PCT_PER_SIDE = 1.5  # Slightly less harsh than backtest (live data)
    LATENCY_MS = 250             # Simulated order-to-fill latency

    def __init__(self, real_kite, capital_inr: float):
        self._real = real_kite              # delegate market-data calls
        self._capital_start = capital_inr
        self._cash = capital_inr
        self._positions: dict[str, _SimPosition] = {}
        self._fills: list[dict] = []
        self._next_order_id = 1

    # ---- Pass-through (read-only) ----
    def get_instruments(self, exchange="NFO"):
        return self._real.get_instruments(exchange)

    def get_quote(self, symbols):
        return self._real.get_quote(symbols)

    def get_historical(self, *args, **kwargs):
        return self._real.get_historical(*args, **kwargs)

    def make_ticker(self):
        return self._real.make_ticker()

    @staticmethod
    def generate_access_token(*a, **k):
        return KiteClient.generate_access_token(*a, **k)  # noqa

    # ---- Simulated execution ----
    def get_funds(self):
        return {"available": {"cash": self._cash}}

    def get_positions(self):
        return {"net": [
            {
                "tradingsymbol": p.tradingsymbol,
                "exchange": p.exchange,
                "quantity": p.quantity,
                "average_price": p.avg_price,
            }
            for p in self._positions.values()
        ]}

    def _fetch_ltp(self, exchange: str, symbol: str) -> float:
        try:
            q = self._real.get_quote([f"{exchange}:{symbol}"])
            return q.get(f"{exchange}:{symbol}", {}).get("last_price", 0)
        except Exception as e:
            logger.warning(f"[PAPER] LTP fetch failed for {symbol}: {e}")
            return 0

    def place_market_order(self, tradingsymbol, exchange, quantity,
                           transaction_type, product="MIS"):
        time.sleep(self.LATENCY_MS / 1000)  # realism
        ltp = self._fetch_ltp(exchange, tradingsymbol)
        if ltp <= 0:
            logger.error(f"[PAPER] Cannot fill {tradingsymbol}: no LTP")
            return None

        slip = ltp * self.SLIPPAGE_PCT_PER_SIDE / 100
        fill_price = ltp + slip if transaction_type == "BUY" else ltp - slip

        pnl = 0.0
        if transaction_type == "BUY":
            cost = fill_price * quantity
            if cost > self._cash:
                logger.warning(f"[PAPER] Insufficient cash: need ₹{cost:.0f} have ₹{self._cash:.0f}")
                return None
            self._cash -= cost
            self._positions[tradingsymbol] = _SimPosition(
                tradingsymbol=tradingsymbol, exchange=exchange,
                quantity=quantity, avg_price=fill_price,
                entry_time=datetime.now(),
            )
        else:
            pos = self._positions.pop(tradingsymbol, None)
            if not pos:
                logger.warning(f"[PAPER] SELL with no position: {tradingsymbol}")
                return None
            proceeds = fill_price * quantity
            self._cash += proceeds
            pnl = (fill_price - pos.avg_price) * quantity

        order_id = f"PAPER-{self._next_order_id}"
        self._next_order_id += 1
        self._fills.append({
            "ts": datetime.now().isoformat(),
            "order_id": order_id,
            "symbol": tradingsymbol,
            "side": transaction_type,
            "qty": quantity,
            "price": fill_price,
            "ltp_at_fill": ltp,
            "slippage_inr": slip * quantity,
            "pnl": pnl,
        })
        logger.info(f"[PAPER] {transaction_type} {quantity} {tradingsymbol} "
                    f"@₹{fill_price:.2f} (ltp ₹{ltp:.2f}, slip ₹{slip*quantity:.0f})"
                    + (f" pnl=₹{pnl:.0f}" if pnl else ""))
        return order_id

    def square_off_all(self):
        for sym in list(self._positions.keys()):
            pos = self._positions[sym]
            self.place_market_order(sym, pos.exchange, pos.quantity, "SELL")

    # ---- Reporting ----
    def session_summary(self) -> dict:
        sells = [f for f in self._fills if f["side"] == "SELL"]
        total_pnl = sum(f["pnl"] for f in sells)
        wins = [f for f in sells if f["pnl"] > 0]
        return {
            "starting_capital": self._capital_start,
            "current_cash": self._cash,
            "open_positions": len(self._positions),
            "total_trades": len(sells),
            "wins": len(wins),
            "losses": len(sells) - len(wins),
            "win_rate_pct": (len(wins) / len(sells) * 100) if sells else 0,
            "total_pnl": total_pnl,
            "total_slippage_paid": sum(f["slippage_inr"] for f in self._fills),
            "all_fills": self._fills,
        }


# =============================================================================
# FILE: analytics/trade_recorder.py
# =============================================================================
"""Records every signal + entry + exit to SQLite for later attribution."""

import sqlite3
from pathlib import Path
from datetime import datetime
from threading import Lock


DB_PATH = Path.home() / ".kite_scalper" / "trades.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    underlying    TEXT NOT NULL,
    direction     TEXT NOT NULL,           -- BULLISH / BEARISH
    spot          REAL,
    -- signal feature snapshot:
    bb_squeezed   INTEGER,                 -- 0/1
    macd_hist_1m  REAL,
    macd_hist_3m  REAL,
    vwap_dev_1m   REAL,
    vwap_dev_3m   REAL,
    -- Greeks at entry:
    iv            REAL,
    delta_val     REAL,
    days_to_exp   INTEGER,
    -- Routing:
    chosen_symbol TEXT,
    chosen_strike REAL,
    chosen_type   TEXT,                    -- CE/PE
    blocked_by    TEXT,                    -- e.g. 'greeks-filter', 'rate-limit'
    mode          TEXT NOT NULL            -- live / paper / dry_run
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER REFERENCES signals(signal_id),
    entry_ts      TEXT NOT NULL,
    exit_ts       TEXT,
    symbol        TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    sl_price      REAL,
    tp_price      REAL,
    exit_reason   TEXT,                    -- SL / TP / TIME / SQUARE_OFF / MANUAL
    holding_min   REAL,
    pnl           REAL,
    slippage_inr  REAL,
    mode          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_ts);
"""


class TradeRecorder:
    _lock = Lock()

    def __init__(self):
        DB_PATH.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- Signals ----
    def record_signal(self, **fields) -> int:
        """Returns signal_id."""
        keys = list(fields.keys())
        placeholders = ",".join("?" * len(keys))
        cols = ",".join(keys)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO signals (ts,{cols}) VALUES (?,{placeholders})",
                (datetime.now().isoformat(), *fields.values())
            )
            self.conn.commit()
            return cur.lastrowid

    # ---- Trade entry ----
    def record_entry(self, signal_id: int, symbol: str, qty: int,
                     entry_price: float, sl: float, tp: float, mode: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO trades
                   (signal_id, entry_ts, symbol, quantity, entry_price,
                    sl_price, tp_price, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal_id, datetime.now().isoformat(), symbol, qty,
                 entry_price, sl, tp, mode)
            )
            self.conn.commit()
            return cur.lastrowid

    # ---- Trade exit ----
    def record_exit(self, trade_id: int, exit_price: float, exit_reason: str,
                    pnl: float, slippage_inr: float = 0.0):
        with self._lock:
            row = self.conn.execute(
                "SELECT entry_ts FROM trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
            if not row:
                return
            entry_ts = datetime.fromisoformat(row[0])
            holding_min = (datetime.now() - entry_ts).total_seconds() / 60
            self.conn.execute(
                """UPDATE trades SET exit_ts=?, exit_price=?, exit_reason=?,
                   pnl=?, slippage_inr=?, holding_min=?
                   WHERE trade_id=?""",
                (datetime.now().isoformat(), exit_price, exit_reason,
                 pnl, slippage_inr, holding_min, trade_id)
            )
            self.conn.commit()

    def record_blocked(self, **fields):
        """Signal that didn't make it to entry (e.g. blocked by greeks)."""
        return self.record_signal(**fields)


# =============================================================================
# FILE: analytics/attribution.py
# =============================================================================
"""Slice the trade history to find what's actually working.

Questions answered:
  1. Win rate / avg P&L by underlying — which symbols are profitable?
  2. Win rate by exit reason — are we hitting targets or just SL+timer?
  3. P&L by hour-of-day — when does the strategy work?
  4. P&L by direction (bull vs bear) — are we one-sided?
  5. P&L by holding-time bucket — does longer = better?
  6. Greeks filter contribution — would skipped signals have made money?
  7. Slippage drag — what % of gross P&L is eaten by slippage?
  8. Signal-to-trade conversion — how many signals get blocked, by what?
"""

import sqlite3
from pathlib import Path
import pandas as pd


DB_PATH = Path.home() / ".kite_scalper" / "trades.db"


class Attribution:
    def __init__(self, mode_filter: str | None = None,
                 since_date: str | None = None):
        """mode_filter: 'live' / 'paper' / None for all
           since_date:  'YYYY-MM-DD' or None for all-time"""
        self.conn = sqlite3.connect(str(DB_PATH))
        self.mode_filter = mode_filter
        self.since_date = since_date

    def _trades_df(self) -> pd.DataFrame:
        q = "SELECT * FROM trades WHERE pnl IS NOT NULL"
        params = []
        if self.mode_filter:
            q += " AND mode = ?"; params.append(self.mode_filter)
        if self.since_date:
            q += " AND entry_ts >= ?"; params.append(self.since_date)
        df = pd.read_sql(q, self.conn, params=params)
        if df.empty:
            return df
        df["entry_ts"] = pd.to_datetime(df["entry_ts"])
        df["hour"] = df["entry_ts"].dt.hour
        df["underlying"] = df["symbol"].str.extract(r"^([A-Z&]+?)\d")
        return df

    def _signals_df(self) -> pd.DataFrame:
        q = "SELECT * FROM signals"
        params = []
        if self.mode_filter:
            q += " WHERE mode = ?"; params.append(self.mode_filter)
        return pd.read_sql(q, self.conn, params=params)

    # ---- 1. By underlying ----
    def by_underlying(self) -> pd.DataFrame:
        df = self._trades_df()
        if df.empty:
            return df
        return (df.groupby("underlying")
                  .agg(trades=("trade_id", "count"),
                       total_pnl=("pnl", "sum"),
                       avg_pnl=("pnl", "mean"),
                       win_rate=("pnl", lambda s: (s > 0).mean() * 100),
                       avg_holding_min=("holding_min", "mean"))
                  .round(2)
                  .sort_values("total_pnl", ascending=False))

    # ---- 2. By exit reason ----
    def by_exit_reason(self) -> pd.DataFrame:
        df = self._trades_df()
        if df.empty:
            return df
        return (df.groupby("exit_reason")
                  .agg(trades=("trade_id", "count"),
                       total_pnl=("pnl", "sum"),
                       avg_pnl=("pnl", "mean"))
                  .round(2))

    # ---- 3. By hour ----
    def by_hour(self) -> pd.DataFrame:
        df = self._trades_df()
        if df.empty:
            return df
        return (df.groupby("hour")
                  .agg(trades=("trade_id", "count"),
                       total_pnl=("pnl", "sum"),
                       win_rate=("pnl", lambda s: (s > 0).mean() * 100))
                  .round(2))

    # ---- 4. By direction ----
    def by_direction(self) -> pd.DataFrame:
        df = self._trades_df()
        if df.empty:
            return df
        df["dir"] = df["symbol"].str[-2:].map({"CE": "BULL", "PE": "BEAR"})
        return (df.groupby("dir")
                  .agg(trades=("trade_id", "count"),
                       total_pnl=("pnl", "sum"),
                       win_rate=("pnl", lambda s: (s > 0).mean() * 100))
                  .round(2))

    # ---- 5. By holding time ----
    def by_holding_bucket(self) -> pd.DataFrame:
        df = self._trades_df()
        if df.empty:
            return df
        bins = [0, 3, 7, 12, 15, 999]
        labels = ["0-3m", "3-7m", "7-12m", "12-15m", "15m+"]
        df["bucket"] = pd.cut(df["holding_min"], bins=bins, labels=labels)
        return (df.groupby("bucket", observed=True)
                  .agg(trades=("trade_id", "count"),
                       total_pnl=("pnl", "sum"),
                       win_rate=("pnl", lambda s: (s > 0).mean() * 100))
                  .round(2))

    # ---- 6. Greeks filter contribution ----
    def greeks_filter_impact(self) -> dict:
        """Compare: signals that PASSED greeks vs were BLOCKED by greeks.
        For blocked ones, we obviously can't measure realized P&L (didn't trade),
        but we can show how many were blocked and the rough $ saved/missed if
        we had a counterfactual price history (we don't, so just counts)."""
        sigs = self._signals_df()
        if sigs.empty:
            return {"blocked_signals": 0, "passed_signals": 0, "block_rate_pct": 0}
        blocked = sigs[sigs["blocked_by"].str.contains("greeks", na=False)]
        passed = sigs[sigs["blocked_by"].isna()]
        return {
            "blocked_signals": len(blocked),
            "passed_signals": len(passed),
            "block_rate_pct": round(len(blocked) / len(sigs) * 100, 1) if len(sigs) else 0,
        }

    # ---- 7. Slippage drag ----
    def slippage_summary(self) -> dict:
        df = self._trades_df()
        if df.empty:
            return {}
        gross = df["pnl"].sum() + df["slippage_inr"].sum()
        slip = df["slippage_inr"].sum()
        return {
            "gross_pnl_before_slippage": round(gross, 0),
            "total_slippage_paid": round(slip, 0),
            "net_pnl": round(df["pnl"].sum(), 0),
            "slippage_drag_pct": round(slip / gross * 100, 1) if gross else 0,
        }

    # ---- 8. Conversion funnel ----
    def conversion_funnel(self) -> dict:
        sigs = self._signals_df()
        trades = self._trades_df()
        if sigs.empty:
            return {}
        return {
            "signals_generated": len(sigs),
            "signals_blocked": int(sigs["blocked_by"].notna().sum()),
            "trades_entered": len(trades),
            "entry_rate_pct": round(len(trades) / len(sigs) * 100, 1) if len(sigs) else 0,
        }


# =============================================================================
# FILE: dashboard/attribution_tab.py
# =============================================================================
"""New tab to drop into dashboard/app.py."""

ATTRIBUTION_TAB_CODE = '''
import streamlit as st
from analytics.attribution import Attribution

with tab_attribution:   # add this to your st.tabs(...) call
    st.subheader("📊 P&L Attribution")

    cA, cB = st.columns(2)
    mode = cA.selectbox("Mode", ["all", "live", "paper"], index=0)
    days_back = cB.number_input("Days back", min_value=1, max_value=365, value=30)
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=int(days_back))).isoformat()

    attr = Attribution(
        mode_filter=None if mode == "all" else mode,
        since_date=since,
    )

    # Funnel
    st.markdown("### Signal → Trade Funnel")
    funnel = attr.conversion_funnel()
    if funnel:
        cols = st.columns(len(funnel))
        for i, (k, v) in enumerate(funnel.items()):
            cols[i].metric(k.replace("_", " ").title(), v)
    else:
        st.info("No signals recorded yet.")

    # Slippage
    st.markdown("### Slippage Drag")
    slip = attr.slippage_summary()
    if slip:
        cols = st.columns(len(slip))
        for i, (k, v) in enumerate(slip.items()):
            cols[i].metric(k.replace("_", " ").title(),
                           f"₹{v:,.0f}" if "pnl" in k or "slippage" in k
                           else f"{v}%")

    # By underlying
    st.markdown("### By Underlying")
    df = attr.by_underlying()
    st.dataframe(df, use_container_width=True) if not df.empty else st.info("No trades.")

    # By exit reason
    cL, cR = st.columns(2)
    with cL:
        st.markdown("### By Exit Reason")
        df = attr.by_exit_reason()
        st.dataframe(df, use_container_width=True) if not df.empty else st.info("—")
    with cR:
        st.markdown("### By Direction (CE vs PE)")
        df = attr.by_direction()
        st.dataframe(df, use_container_width=True) if not df.empty else st.info("—")

    # Time-of-day
    st.markdown("### By Hour of Day")
    df = attr.by_hour()
    if not df.empty:
        st.bar_chart(df["total_pnl"])
        st.dataframe(df, use_container_width=True)

    # Holding time
    st.markdown("### By Holding Duration")
    df = attr.by_holding_bucket()
    st.dataframe(df, use_container_width=True) if not df.empty else st.info("—")

    # Greeks filter
    st.markdown("### Greeks Filter Impact")
    g = attr.greeks_filter_impact()
    if g:
        cols = st.columns(len(g))
        for i, (k, v) in enumerate(g.items()):
            cols[i].metric(k.replace("_", " ").title(), v)
'''


# =============================================================================
# FILE: main.py — INTEGRATION PATCHES
# =============================================================================
PATCHES_FOR_MAIN = """
# ============= CHANGE 1: mode flag in config.yaml =============
# Add under 'capital:' section:
#
# mode: "paper"         # one of: live | paper | dry_run | backtest
#

# ============= CHANGE 2: broker selection in main() =============
# Replace the KiteClient construction with:

    real_kite = KiteClient(
        api_key=os.environ['KITE_API_KEY'],
        api_secret=os.environ['KITE_API_SECRET'],
        access_token=os.environ['KITE_ACCESS_TOKEN'],
    )

    mode = config['capital'].get('mode', 'paper')
    if mode == 'paper':
        from broker.paper_broker import PaperBroker
        kite = PaperBroker(real_kite, config['capital']['total_inr'])
        logger.warning('=== PAPER TRADING MODE — no real orders ===')
    elif mode == 'live':
        kite = real_kite
        logger.critical('=== LIVE TRADING MODE — REAL MONEY AT RISK ===')
    else:
        kite = real_kite  # dry_run still uses real client; OrderManager gates orders

# ============= CHANGE 3: trade recorder hook =============
# After order_mgr is created:

    from analytics.trade_recorder import TradeRecorder
    recorder = TradeRecorder()

# ============= CHANGE 4: record signals before entry =============
# Wherever the strategy decides to act on a signal, do:

    sig_id = recorder.record_signal(
        underlying=symbol,
        direction=sig.name,
        spot=spot,
        bb_squeezed=int(squeezed) if 'squeezed' in dir() else None,
        macd_hist_1m=float(curr_hist) if 'curr_hist' in dir() else None,
        chosen_symbol=contract['tradingsymbol'],
        chosen_strike=contract['strike'],
        chosen_type=contract['instrument_type'],
        blocked_by=None,
        mode=mode,
    )

# When greeks filter blocks:
    if not ok:
        recorder.record_blocked(
            underlying=symbol, direction=sig.name, spot=spot,
            chosen_symbol=contract['tradingsymbol'],
            blocked_by=f'greeks: {reason}', mode=mode,
        )
        continue

# ============= CHANGE 5: hook entry/exit into OrderManager =============
# Easiest: subclass OrderManager to call recorder. In main:

    class RecordingOrderManager(OrderManager):
        def __init__(self, broker, config, recorder, sig_id_provider, mode):
            super().__init__(broker, config)
            self._rec = recorder
            self._mode = mode
            self._sig_id = sig_id_provider   # callable returning latest signal_id
            self._trade_id_by_sym: dict[str, int] = {}

        def enter(self, contract, premium):
            # capture pre-state to detect new position
            before = {p.tradingsymbol for p in self.state.positions}
            super().enter(contract, premium)
            after = {p.tradingsymbol for p in self.state.positions}
            new_syms = after - before
            for sym in new_syms:
                p = next(x for x in self.state.positions if x.tradingsymbol == sym)
                tid = self._rec.record_entry(
                    signal_id=self._sig_id(),
                    symbol=sym, qty=p.quantity, entry_price=p.entry_price,
                    sl=p.sl_price, tp=p.tp_price, mode=self._mode,
                )
                self._trade_id_by_sym[sym] = tid

        def monitor(self, ltp_lookup):
            before = {p.tradingsymbol: p for p in self.state.positions}
            super().monitor(ltp_lookup)
            after = {p.tradingsymbol for p in self.state.positions}
            closed = set(before.keys()) - after
            for sym in closed:
                p = before[sym]
                ltp = ltp_lookup.get(sym, p.entry_price)
                pnl = (ltp - p.entry_price) * p.quantity
                # Reason inference: re-derive (simple heuristic)
                reason = ('SL' if ltp <= p.sl_price else
                          'TP' if ltp >= p.tp_price else 'TIME')
                tid = self._trade_id_by_sym.pop(sym, None)
                if tid:
                    self._rec.record_exit(tid, ltp, reason, pnl)

# Instantiate this instead of OrderManager:
    latest_sig_id = [0]
    order_mgr = RecordingOrderManager(
        kite, config, recorder, lambda: latest_sig_id[0], mode)
    # When recording a signal, also store sig_id:
    sig_id = recorder.record_signal(...); latest_sig_id[0] = sig_id

# ============= CHANGE 6: dashboard tab =============
# In dashboard/app.py, change the tabs line to:
#    tab1, tab2, tab3, tab4, tab_attribution = st.tabs([
#        "📊 Positions", "🎯 Universe", "📜 Event Log", "📈 P&L", "🔍 Attribution"
#    ])
# Then paste the body from ATTRIBUTION_TAB_CODE.
"""
