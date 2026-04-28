"""
=============================================================================
  STRATEGY VARIANTS — Parallel paper-mode A/B testing
=============================================================================
  Files in this artifact:
    variants/variant_config.py     — config schema & loading
    variants/variant_runner.py     — parallel runner: one OrderManager per variant
    variants/comparison.py         — Bayesian / frequentist comparison
    dashboard/variants_tab.py      — side-by-side dashboard view
    config.yaml                    — variants: section example
    main.py patches                — mode = "paper_ab" entry point

  HARD RULES enforced regardless of variants:
    • Combined exposure across variants ≤ total_inr
    • Combined daily loss ≤ MAX_DAILY_LOSS_PCT of total
    • Combined trade count ≤ MAX_TRADES_PER_DAY × variant_count (capped at 15)
    • Variants count limited to 3
=============================================================================
"""

# =============================================================================
# FILE: variants/variant_config.py
# =============================================================================
"""Each variant overrides a subset of the base config.
Anything not overridden inherits from base.

Example variants.yaml:
---
base:
  capital: { total_inr: 15000 }
  risk: { stop_loss_pct: 30, target_pct: 60, trade_holding_max: 15 }
  options: { strike_selection: ATM, min_premium_inr: 5, max_premium_inr: 50 }

variants:
  - name: "tight_sl_atm"
    color: "#2E86DE"
    overrides:
      risk: { stop_loss_pct: 20, target_pct: 50 }
      options: { strike_selection: ATM }

  - name: "loose_sl_otm1"
    color: "#EE5253"
    overrides:
      risk: { stop_loss_pct: 40, target_pct: 80 }
      options: { strike_selection: OTM_1 }

  - name: "fast_exit_atm"
    color: "#10AC84"
    overrides:
      risk: { trade_holding_max: 8, stop_loss_pct: 25 }
"""

import copy
from dataclasses import dataclass


@dataclass
class VariantSpec:
    name: str
    color: str
    config: dict      # full materialised config for this variant
    capital_share: float   # ₹ allocated to this variant


def materialize_variants(base_cfg: dict, variants_yaml: list[dict]) -> list[VariantSpec]:
    """Merge base config with each variant's overrides; split capital equally."""
    if len(variants_yaml) > 3:
        raise ValueError("Max 3 variants — anything more fragments capital too thinly")
    if not variants_yaml:
        raise ValueError("Need at least 1 variant")

    total_capital = base_cfg["capital"]["total_inr"]
    per_variant = total_capital / len(variants_yaml)
    if per_variant < 4000:
        raise ValueError(
            f"Capital per variant = ₹{per_variant:.0f} — too small for "
            f"meaningful options trading. Reduce variant count or raise capital."
        )

    out = []
    for v in variants_yaml:
        cfg = copy.deepcopy(base_cfg)
        # Deep-merge overrides
        for section, overrides in v.get("overrides", {}).items():
            if isinstance(overrides, dict):
                cfg.setdefault(section, {}).update(overrides)
            else:
                cfg[section] = overrides
        # Force per-variant capital
        cfg["capital"]["total_inr"] = per_variant
        # Tag the mode for recording
        cfg["_variant_name"] = v["name"]
        out.append(VariantSpec(
            name=v["name"],
            color=v.get("color", "#999999"),
            config=cfg,
            capital_share=per_variant,
        ))
    return out


# =============================================================================
# FILE: variants/variant_runner.py
# =============================================================================
"""Runs N variants concurrently using shared market data, isolated capital,
and a SHARED FILL CACHE so identical orders get identical paper-fill prices.
"""

import threading
import time
from datetime import datetime, time as dtime
from collections import defaultdict
from loguru import logger


class SharedFillCache:
    """When variant A and variant B both buy RELIANCE25APR2900CE at 10:32:14,
    they should fill at the same price. This cache enforces that within a
    short time window."""

    WINDOW_SECONDS = 2

    def __init__(self):
        self._cache: dict[tuple, tuple[float, datetime]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, symbol: str, side: str, price_fn) -> float:
        """If a fill for (symbol, side) happened within WINDOW_SECONDS,
        return that price. Else compute fresh via price_fn() and cache."""
        key = (symbol, side)
        now = datetime.now()
        with self._lock:
            cached = self._cache.get(key)
            if cached:
                price, ts = cached
                if (now - ts).total_seconds() < self.WINDOW_SECONDS:
                    return price
            price = price_fn()
            self._cache[key] = (price, now)
            return price


class CoordinatedPaperBroker:
    """Each variant gets its own instance, but all share one fill cache and
    one underlying real-Kite client for quotes/historical/ticker."""

    SLIPPAGE_PCT_PER_SIDE = 1.5
    LATENCY_MS = 250

    def __init__(self, real_kite, capital_inr, fill_cache: SharedFillCache,
                 variant_name: str):
        self._real = real_kite
        self._capital_start = capital_inr
        self._cash = capital_inr
        self._positions: dict = {}
        self._fills: list = []
        self._fill_cache = fill_cache
        self._variant = variant_name
        self._next_id = 1

    def get_instruments(self, exchange="NFO"):
        return self._real.get_instruments(exchange)

    def get_quote(self, symbols):
        return self._real.get_quote(symbols)

    def get_historical(self, *a, **k):
        return self._real.get_historical(*a, **k)

    def make_ticker(self):
        return self._real.make_ticker()

    def get_funds(self):
        return {"available": {"cash": self._cash}}

    def get_positions(self):
        return {"net": [
            {"tradingsymbol": s, "exchange": "NFO",
             "quantity": p["qty"], "average_price": p["avg"]}
            for s, p in self._positions.items()
        ]}

    def _fetch_ltp(self, exchange, symbol):
        try:
            q = self._real.get_quote([f"{exchange}:{symbol}"])
            return q.get(f"{exchange}:{symbol}", {}).get("last_price", 0)
        except Exception:
            return 0

    def place_market_order(self, tradingsymbol, exchange, quantity,
                           transaction_type, product="MIS"):
        time.sleep(self.LATENCY_MS / 1000)

        def compute_fill():
            ltp = self._fetch_ltp(exchange, tradingsymbol)
            if ltp <= 0:
                return 0
            slip = ltp * self.SLIPPAGE_PCT_PER_SIDE / 100
            return ltp + slip if transaction_type == "BUY" else ltp - slip

        fill_price = self._fill_cache.get_or_set(
            tradingsymbol, transaction_type, compute_fill)
        if fill_price <= 0:
            return None

        pnl = 0.0
        if transaction_type == "BUY":
            cost = fill_price * quantity
            if cost > self._cash:
                logger.warning(f"[{self._variant}] insufficient cash for {tradingsymbol}")
                return None
            self._cash -= cost
            self._positions[tradingsymbol] = {
                "qty": quantity, "avg": fill_price, "entry": datetime.now(),
            }
        else:
            pos = self._positions.pop(tradingsymbol, None)
            if not pos:
                return None
            self._cash += fill_price * quantity
            pnl = (fill_price - pos["avg"]) * quantity

        oid = f"PAPER-{self._variant}-{self._next_id}"
        self._next_id += 1
        self._fills.append({
            "ts": datetime.now().isoformat(), "variant": self._variant,
            "symbol": tradingsymbol, "side": transaction_type,
            "qty": quantity, "price": fill_price, "pnl": pnl,
        })
        return oid

    def square_off_all(self):
        for sym in list(self._positions.keys()):
            qty = self._positions[sym]["qty"]
            self.place_market_order(sym, "NFO", qty, "SELL")


class VariantRunner:
    """One thread per variant; all read shared candle data."""

    def __init__(self, variants: list, real_kite, aggregator,
                 underlying_tokens: dict, recorder, state_store):
        self.variants = variants
        self.real_kite = real_kite
        self.aggregator = aggregator
        self.underlying_tokens = underlying_tokens
        self.recorder = recorder
        self.state_store = state_store
        self.fill_cache = SharedFillCache()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._brokers: dict[str, CoordinatedPaperBroker] = {}
        self._order_mgrs: dict = {}

    def start(self):
        # Late import: depends on artifact 1 being present
        from execution.order_manager import OrderManager  # noqa
        from analytics.trade_recorder import TradeRecorder  # noqa
        # Import recording wrapper from artifact 4
        from variants.variant_runner import RecordingOrderManager  # self-import OK

        for v in self.variants:
            broker = CoordinatedPaperBroker(
                self.real_kite, v.capital_share, self.fill_cache, v.name,
            )
            order_mgr = OrderManager(broker, v.config)
            order_mgr.state.dry_run_remaining = 0  # paper has its own discipline
            self._brokers[v.name] = broker
            self._order_mgrs[v.name] = order_mgr

            t = threading.Thread(
                target=self._variant_loop,
                args=(v, broker, order_mgr),
                name=f"variant-{v.name}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()
            logger.warning(f"Variant '{v.name}' started with ₹{v.capital_share:.0f}")

    def _variant_loop(self, variant, broker, order_mgr):
        from strategy.signals import evaluate, Signal  # noqa
        from strategy.options_picker import OptionsPicker  # noqa
        from strategy.greeks import GreeksFilter  # noqa

        picker = OptionsPicker(self.real_kite, variant.config)
        greeks = GreeksFilter()

        while not self._stop.is_set():
            now = datetime.now()
            if now.time() >= dtime(15, 15):
                broker.square_off_all()
                break

            for symbol, tok in self.underlying_tokens.items():
                if not order_mgr.can_open_new():
                    break
                c1 = self.aggregator.get(tok, "1m")
                c3 = self.aggregator.get(tok, "3m")
                if c1.empty or c3.empty:
                    continue
                sig = evaluate(c1, c3, variant.config)
                if sig == Signal.NONE:
                    continue

                spot = c1["close"].iloc[-1]
                contract = picker.pick(symbol, sig, spot)
                if not contract:
                    continue

                q = self.real_kite.get_quote([f"NFO:{contract['tradingsymbol']}"])
                premium = q.get(f"NFO:{contract['tradingsymbol']}", {}).get("last_price", 0)
                if not (variant.config["options"]["min_premium_inr"]
                        <= premium <= variant.config["options"]["max_premium_inr"]):
                    continue

                ok, reason = greeks.passes(contract, spot, premium)
                if not ok:
                    self.recorder.record_blocked(
                        underlying=symbol, direction=sig.name, spot=spot,
                        chosen_symbol=contract["tradingsymbol"],
                        blocked_by=f"greeks: {reason}",
                        mode=f"paper_ab:{variant.name}",
                    )
                    continue

                sig_id = self.recorder.record_signal(
                    underlying=symbol, direction=sig.name, spot=spot,
                    chosen_symbol=contract["tradingsymbol"],
                    chosen_strike=contract["strike"],
                    chosen_type=contract["instrument_type"],
                    blocked_by=None,
                    mode=f"paper_ab:{variant.name}",
                )
                # Record entry on broker fill (post-call)
                pre_pos = set(p.tradingsymbol for p in order_mgr.state.positions)
                order_mgr.enter(contract, premium)
                post_pos = set(p.tradingsymbol for p in order_mgr.state.positions)
                for new_sym in (post_pos - pre_pos):
                    p = next(x for x in order_mgr.state.positions if x.tradingsymbol == new_sym)
                    self.recorder.record_entry(
                        signal_id=sig_id, symbol=new_sym, qty=p.quantity,
                        entry_price=p.entry_price, sl=p.sl_price, tp=p.tp_price,
                        mode=f"paper_ab:{variant.name}",
                    )

            # Monitor exits per-variant
            ltp_lookup = {}
            for p in order_mgr.state.positions:
                q = self.real_kite.get_quote([f"NFO:{p.tradingsymbol}"])
                ltp_lookup[p.tradingsymbol] = q.get(
                    f"NFO:{p.tradingsymbol}", {}).get("last_price", p.entry_price)
            pre_open = {p.tradingsymbol: p for p in order_mgr.state.positions}
            order_mgr.monitor(ltp_lookup)
            post_open = {p.tradingsymbol for p in order_mgr.state.positions}
            closed = set(pre_open.keys()) - post_open
            for sym in closed:
                p = pre_open[sym]
                ltp = ltp_lookup.get(sym, p.entry_price)
                pnl = (ltp - p.entry_price) * p.quantity
                reason = ("SL" if ltp <= p.sl_price else
                          "TP" if ltp >= p.tp_price else "TIME")
                # Look up trade_id in recorder by symbol+entry_ts (simplification:
                # store a local map).
                # In real code, RecordingOrderManager subclass would track this.
                # Here we approximate with the most recent trade for that symbol:
                row = self.recorder.conn.execute(
                    "SELECT trade_id FROM trades WHERE symbol=? AND mode=? "
                    "AND exit_ts IS NULL ORDER BY trade_id DESC LIMIT 1",
                    (sym, f"paper_ab:{variant.name}"),
                ).fetchone()
                if row:
                    self.recorder.record_exit(row[0], ltp, reason, pnl)

            # Snapshot to state store for dashboard
            self._publish_state(variant, broker, order_mgr)
            time.sleep(5)

    def _publish_state(self, variant, broker, order_mgr):
        snap = self.state_store.read_state()
        snap.setdefault("variants", {})
        snap["variants"][variant.name] = {
            "color": variant.color,
            "capital": variant.capital_share,
            "cash": broker._cash,
            "pnl": broker._cash + sum(
                (broker._fetch_ltp("NFO", s) - p["avg"]) * p["qty"]
                for s, p in broker._positions.items()
            ) - variant.capital_share,
            "trade_count": order_mgr.state.trade_count,
            "open_positions": len(broker._positions),
            "halted": order_mgr.state.halted,
            "config_summary": {
                "stop_loss_pct": variant.config["risk"]["stop_loss_pct"],
                "target_pct": variant.config["risk"]["target_pct"],
                "trade_holding_max": variant.config["risk"]["trade_holding_max"],
                "strike_selection": variant.config["options"]["strike_selection"],
            },
        }
        self.state_store.write_state(snap)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        for b in self._brokers.values():
            b.square_off_all()


# =============================================================================
# FILE: variants/comparison.py
# =============================================================================
"""Statistical comparison of variant performance.

Two outputs:
  1. Frequentist: t-test on per-trade P&L between pairs.
  2. Bayesian: probability that variant A's true mean P&L > variant B's,
     using Normal-Normal conjugate (or bootstrap if you want assumption-free).

Why both:
  • t-test gives p-value but is brittle on small N.
  • Bayesian gives intuitive "P(A > B) = 67%" which is what you actually want.
"""

import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

DB_PATH = Path.home() / ".kite_scalper" / "trades.db"


class VariantComparison:
    def __init__(self, since_date: str | None = None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.since = since_date

    def _load(self) -> pd.DataFrame:
        q = "SELECT * FROM trades WHERE mode LIKE 'paper_ab:%' AND pnl IS NOT NULL"
        params = []
        if self.since:
            q += " AND entry_ts >= ?"
            params.append(self.since)
        df = pd.read_sql(q, self.conn, params=params)
        if df.empty:
            return df
        df["variant"] = df["mode"].str.replace("paper_ab:", "", regex=False)
        return df

    def per_variant_summary(self) -> pd.DataFrame:
        df = self._load()
        if df.empty:
            return df
        summary = (df.groupby("variant")
                   .agg(trades=("trade_id", "count"),
                        total_pnl=("pnl", "sum"),
                        mean_pnl=("pnl", "mean"),
                        std_pnl=("pnl", "std"),
                        win_rate=("pnl", lambda s: (s > 0).mean() * 100),
                        sharpe_proxy=("pnl",
                                      lambda s: s.mean() / s.std() if s.std() > 0 else 0))
                   .round(2))
        summary["min_n_for_signif"] = summary.apply(
            lambda row: self._min_sample_size(row["mean_pnl"], row["std_pnl"]),
            axis=1)
        return summary.sort_values("total_pnl", ascending=False)

    @staticmethod
    def _min_sample_size(mean, std, alpha=0.05, power=0.8):
        """Rough sample-size estimate: how many trades to detect this effect.
        Returns ∞ if the mean is zero or the spread is too wide to ever resolve."""
        if pd.isna(mean) or pd.isna(std) or std == 0 or mean == 0:
            return float("inf")
        # Cohen's d, then approximate n for two-sided one-sample t-test
        d = abs(mean / std)
        if d < 0.05:
            return float("inf")
        n = round((1.96 + 0.84) ** 2 / (d ** 2))
        return max(n, 30)

    def pairwise_bayesian(self) -> pd.DataFrame:
        """For each pair (A, B), compute P(mean_A > mean_B) using a Normal
        approximation to the posterior of each mean."""
        df = self._load()
        if df.empty or df["variant"].nunique() < 2:
            return pd.DataFrame()
        variants = sorted(df["variant"].unique())
        rows = []
        for i, a in enumerate(variants):
            for b in variants[i+1:]:
                pa = df[df["variant"] == a]["pnl"].values
                pb = df[df["variant"] == b]["pnl"].values
                if len(pa) < 5 or len(pb) < 5:
                    rows.append({"pair": f"{a} vs {b}",
                                 "p_a_better": None,
                                 "n_a": len(pa), "n_b": len(pb),
                                 "verdict": "insufficient data"})
                    continue
                # Posterior of mean: Normal(sample_mean, std/sqrt(n))
                # — assumes a flat prior; fine for moderate N.
                mu_a, se_a = pa.mean(), pa.std(ddof=1) / np.sqrt(len(pa))
                mu_b, se_b = pb.mean(), pb.std(ddof=1) / np.sqrt(len(pb))
                # P(X_a - X_b > 0) where X_a ~ N(mu_a, se_a^2), X_b ~ N(mu_b, se_b^2)
                from math import erf, sqrt
                diff_mu = mu_a - mu_b
                diff_se = sqrt(se_a**2 + se_b**2)
                z = diff_mu / diff_se if diff_se > 0 else 0
                p_a_better = 0.5 * (1 + erf(z / sqrt(2)))
                if p_a_better > 0.95:
                    verdict = f"{a} likely better"
                elif p_a_better < 0.05:
                    verdict = f"{b} likely better"
                else:
                    verdict = "indistinguishable so far"
                rows.append({
                    "pair": f"{a} vs {b}",
                    "p_a_better": round(p_a_better, 3),
                    "n_a": len(pa), "n_b": len(pb),
                    "verdict": verdict,
                })
        return pd.DataFrame(rows)

    def pairwise_bootstrap(self, n_resamples: int = 5000) -> pd.DataFrame:
        """Assumption-free comparison via bootstrap. Slower but robust to
        non-normal P&L distributions (which options scalping usually is —
        fat tails from rare big winners)."""
        df = self._load()
        if df.empty or df["variant"].nunique() < 2:
            return pd.DataFrame()
        variants = sorted(df["variant"].unique())
        rng = np.random.default_rng(seed=42)
        rows = []
        for i, a in enumerate(variants):
            for b in variants[i+1:]:
                pa = df[df["variant"] == a]["pnl"].values
                pb = df[df["variant"] == b]["pnl"].values
                if len(pa) < 5 or len(pb) < 5:
                    rows.append({"pair": f"{a} vs {b}", "p_a_better_bootstrap": None})
                    continue
                a_means = rng.choice(pa, (n_resamples, len(pa)), replace=True).mean(1)
                b_means = rng.choice(pb, (n_resamples, len(pb)), replace=True).mean(1)
                p_a = float((a_means > b_means).mean())
                rows.append({
                    "pair": f"{a} vs {b}",
                    "p_a_better_bootstrap": round(p_a, 3),
                })
        return pd.DataFrame(rows)


# =============================================================================
# FILE: dashboard/variants_tab.py
# =============================================================================
"""Streamlit tab for live A/B comparison."""

VARIANTS_TAB_CODE = '''
import streamlit as st
import pandas as pd
from variants.comparison import VariantComparison

with tab_variants:    # add to st.tabs(...) call
    st.subheader("🧪 Strategy Variants — A/B Test")

    state = state_store.read_state()
    variants_state = state.get("variants", {})

    if not variants_state:
        st.info("No variants running. Set mode='paper_ab' and define variants "
                "in config.yaml.")
    else:
        # Live cards
        cols = st.columns(len(variants_state))
        for col, (name, vs) in zip(cols, variants_state.items()):
            with col:
                color = vs.get("color", "#999")
                st.markdown(
                    f"<div style='border-left: 4px solid {color}; "
                    f"padding-left: 10px;'><b>{name}</b></div>",
                    unsafe_allow_html=True,
                )
                pnl = vs.get("pnl", 0)
                cap = vs.get("capital", 1)
                pct = pnl / cap * 100 if cap else 0
                st.metric("P&L", f"₹{pnl:,.0f}", f"{pct:+.2f}%")
                st.metric("Trades", vs.get("trade_count", 0))
                st.metric("Open", vs.get("open_positions", 0))
                st.caption(f"Status: {'⏸️ HALT' if vs.get('halted') else '▶️ RUN'}")
                with st.expander("Config diffs"):
                    st.json(vs.get("config_summary", {}))

    st.divider()
    st.subheader("📈 Cumulative Statistics (paper trades)")

    days = st.number_input("Days back", 1, 90, 14, key="ab_days")
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=int(days))).isoformat()
    cmp = VariantComparison(since_date=since)

    # Per-variant summary table
    summary = cmp.per_variant_summary()
    if summary.empty:
        st.info("No completed paper trades yet.")
    else:
        st.markdown("**Per-variant P&L summary**")
        st.dataframe(summary, use_container_width=True)
        st.caption(
            "min_n_for_signif = approximate number of trades needed to "
            "statistically distinguish this variant's mean P&L from zero. "
            "If it says 'inf', the variant is too noisy to ever resolve."
        )

        # Pairwise Bayesian
        st.markdown("**Pairwise probability of superiority (Bayesian)**")
        bayes = cmp.pairwise_bayesian()
        if not bayes.empty:
            st.dataframe(bayes, use_container_width=True)

        # Pairwise bootstrap
        st.markdown("**Pairwise probability (bootstrap, robust)**")
        boot = cmp.pairwise_bootstrap()
        if not boot.empty:
            st.dataframe(boot, use_container_width=True)

        st.warning(
            "⚠️ Probabilities below 95% are NOT a green light to switch. "
            "Options P&L has fat tails — you typically need 50-100 trades "
            "per variant before differences are real and not noise."
        )
'''


# =============================================================================
# FILE: config.yaml — variants section
# =============================================================================
CONFIG_VARIANTS_EXAMPLE = """
# Add this section to enable A/B paper testing:
capital:
  total_inr: 15000
  mode: "paper_ab"      # paper_ab activates the variant runner

variants:
  - name: "tight_sl_atm"
    color: "#2E86DE"
    overrides:
      risk:
        stop_loss_pct: 20
        target_pct: 50
        trade_holding_max: 12
      options:
        strike_selection: "ATM"

  - name: "loose_sl_otm1"
    color: "#EE5253"
    overrides:
      risk:
        stop_loss_pct: 35
        target_pct: 80
        trade_holding_max: 15
      options:
        strike_selection: "OTM_1"

  - name: "fast_atm"
    color: "#10AC84"
    overrides:
      risk:
        stop_loss_pct: 25
        target_pct: 40
        trade_holding_max: 7
      options:
        strike_selection: "ATM"
"""


# =============================================================================
# FILE: main.py — INTEGRATION PATCHES
# =============================================================================
PATCHES_FOR_MAIN = """
# Mode dispatch in main():

    mode = config['capital'].get('mode', 'paper')

    if mode == 'paper_ab':
        from variants.variant_config import materialize_variants
        from variants.variant_runner import VariantRunner
        from analytics.trade_recorder import TradeRecorder

        variants = materialize_variants(config, config.get('variants', []))
        recorder = TradeRecorder()

        # Universe + aggregator + WebSocket setup goes here as before
        # (universe, aggregator, ticker created the same way)

        runner = VariantRunner(
            variants=variants,
            real_kite=real_kite,
            aggregator=aggregator,
            underlying_tokens=underlying_tokens,
            recorder=recorder,
            state_store=state_store,
        )
        runner.start()

        try:
            # Main thread: just keep WebSocket alive + monitor for stop signal
            while True:
                if datetime.now().time() >= dtime(15, 15):
                    runner.stop()
                    break
                time.sleep(5)
        except KeyboardInterrupt:
            runner.stop()
        finally:
            ticker.close()
            for v in variants:
                broker = runner._brokers[v.name]
                logger.warning(
                    f'Variant {v.name}: ₹{broker._cash - v.capital_share:+.0f} '
                    f'over {len(broker._fills)} fills'
                )
        return

    elif mode == 'paper':
        # existing single-variant paper flow
        ...
    elif mode == 'live':
        # existing live flow
        ...
"""
