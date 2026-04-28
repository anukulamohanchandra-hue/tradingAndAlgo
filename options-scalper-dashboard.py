"""
=============================================================================
  DASHBOARD — Streamlit live monitor + toggles
=============================================================================
  Architecture: trader process and dashboard process are SEPARATE.
  They communicate through a JSON state file with file locking.

  Files added in this artifact:
    shared/state_store.py    — atomic read/write of state file
    shared/commands.py       — dashboard → trader command queue
    main.py patches          — integrate state writes + command polling
    dashboard/app.py         — Streamlit UI

  Run:
    Terminal 1:  python main.py            # the trader
    Terminal 2:  streamlit run dashboard/app.py

  Install:
    pip install streamlit plotly filelock
=============================================================================
"""

# =============================================================================
# FILE: shared/state_store.py
# =============================================================================
"""Thread-safe + cross-process JSON state file.

The trader writes its current state every loop iteration.
The dashboard reads it on every refresh.
Both use filelock for safe concurrent access.
"""

import json
import os
import time
from pathlib import Path
from datetime import datetime
from filelock import FileLock
from dataclasses import asdict, is_dataclass


STATE_DIR = Path.home() / ".kite_scalper"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
COMMANDS_FILE = STATE_DIR / "commands.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
LOG_FILE = STATE_DIR / "events.log"
LOCK_FILE = STATE_DIR / ".lock"


def _serialize(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


class StateStore:
    """Trader-side: writes state. Dashboard-side: reads state."""

    def __init__(self):
        self.lock = FileLock(str(LOCK_FILE), timeout=2)

    def write_state(self, snapshot: dict):
        snapshot["_updated"] = datetime.now().isoformat()
        try:
            with self.lock:
                STATE_FILE.write_text(json.dumps(_serialize(snapshot), indent=2, default=str))
        except Exception:
            pass  # never let state-write crash the trader

    def read_state(self) -> dict:
        if not STATE_FILE.exists():
            return {}
        try:
            with self.lock:
                return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}

    def append_event(self, level: str, message: str):
        line = f"{datetime.now().isoformat()} [{level}] {message}\n"
        try:
            with self.lock:
                with LOG_FILE.open("a") as f:
                    f.write(line)
        except Exception:
            pass

    def read_events(self, last_n: int = 200) -> list[str]:
        if not LOG_FILE.exists():
            return []
        try:
            with self.lock:
                lines = LOG_FILE.read_text().splitlines()
                return lines[-last_n:]
        except Exception:
            return []


# =============================================================================
# FILE: shared/commands.py
# =============================================================================
"""Dashboard writes commands; trader reads + clears them each loop iteration."""

from datetime import datetime
import json
from filelock import FileLock


VALID_COMMANDS = {
    "HALT",                 # stop opening new trades
    "RESUME",               # un-halt
    "SQUARE_OFF_ALL",       # close every open position now
    "PAUSE_SYMBOL",         # arg: symbol — exclude from universe
    "RESUME_SYMBOL",        # arg: symbol — re-include
    "RELOAD_SETTINGS",      # re-read settings.json
}


class CommandQueue:
    def __init__(self):
        self.lock = FileLock(str(LOCK_FILE), timeout=2)

    def enqueue(self, cmd: str, arg: str = ""):
        if cmd not in VALID_COMMANDS:
            raise ValueError(f"Invalid command: {cmd}")
        item = {
            "cmd": cmd,
            "arg": arg,
            "ts": datetime.now().isoformat(),
        }
        with self.lock:
            existing = []
            if COMMANDS_FILE.exists():
                try:
                    existing = json.loads(COMMANDS_FILE.read_text())
                except Exception:
                    existing = []
            existing.append(item)
            COMMANDS_FILE.write_text(json.dumps(existing, indent=2))

    def drain(self) -> list[dict]:
        """Trader calls this once per loop iteration."""
        with self.lock:
            if not COMMANDS_FILE.exists():
                return []
            try:
                items = json.loads(COMMANDS_FILE.read_text())
                COMMANDS_FILE.write_text("[]")
                return items
            except Exception:
                return []


# =============================================================================
# FILE: shared/settings_store.py
# =============================================================================
"""User-toggleable settings. Always clamped against HARD limits.

Dashboard writes here; trader reads after each RELOAD_SETTINGS command.
"""

import json
from filelock import FileLock


# Default operational settings (subset of config — only the safe-to-toggle ones)
DEFAULT_SETTINGS = {
    "halted": False,
    "dry_run": True,
    "max_daily_loss_pct": 30.0,
    "per_trade_risk_pct": 5.0,
    "max_trades_per_day": 5,
    "stop_loss_pct": 30.0,
    "target_pct": 60.0,
    "trade_holding_max": 15,
    "paused_symbols": [],
}


class SettingsStore:
    def __init__(self):
        self.lock = FileLock(str(LOCK_FILE), timeout=2)

    def read(self) -> dict:
        if not SETTINGS_FILE.exists():
            self.write(DEFAULT_SETTINGS)
            return dict(DEFAULT_SETTINGS)
        with self.lock:
            try:
                s = json.loads(SETTINGS_FILE.read_text())
                # Backfill missing keys (forward compat)
                for k, v in DEFAULT_SETTINGS.items():
                    s.setdefault(k, v)
                return s
            except Exception:
                return dict(DEFAULT_SETTINGS)

    def write(self, s: dict):
        # ENFORCE HARD LIMITS HERE. Dashboard cannot bypass this.
        from risk_limits import HARD  # noqa
        clamped = dict(s)
        clamped["max_daily_loss_pct"] = min(
            float(s.get("max_daily_loss_pct", 30)), HARD.MAX_DAILY_LOSS_PCT)
        clamped["per_trade_risk_pct"] = min(
            float(s.get("per_trade_risk_pct", 5)), HARD.MAX_PER_TRADE_RISK_PCT)
        clamped["max_trades_per_day"] = min(
            int(s.get("max_trades_per_day", 5)), HARD.MAX_TRADES_PER_DAY)
        # SL/TP/holding can only be tightened, not loosened (subjective —
        # we let the user set these freely within sane bounds)
        clamped["stop_loss_pct"] = max(5.0, min(float(s.get("stop_loss_pct", 30)), 50.0))
        clamped["target_pct"] = max(10.0, min(float(s.get("target_pct", 60)), 200.0))
        clamped["trade_holding_max"] = max(2, min(int(s.get("trade_holding_max", 15)), 30))

        with self.lock:
            SETTINGS_FILE.write_text(json.dumps(clamped, indent=2))


# =============================================================================
# FILE: main.py — INTEGRATION PATCHES (apply to main.py from artifact 1)
# =============================================================================
PATCHES_FOR_MAIN = """
# Add these imports at the top of main.py:
from shared.state_store import StateStore
from shared.commands import CommandQueue
from shared.settings_store import SettingsStore

# Initialize once in main() after config load:
    state_store = StateStore()
    cmd_queue = CommandQueue()
    settings_store = SettingsStore()
    settings_store.write({       # seed with config values on first run
        'halted': False,
        'dry_run': config['dry_run']['enabled'],
        'max_daily_loss_pct': config['risk']['max_daily_loss_pct'],
        'per_trade_risk_pct': config['risk']['per_trade_risk_pct'],
        'max_trades_per_day': config['risk']['max_trades_per_day'],
        'stop_loss_pct': config['risk']['stop_loss_pct'],
        'target_pct': config['risk']['target_pct'],
        'trade_holding_max': config['risk']['trade_holding_max'],
        'paused_symbols': [],
    })

# Hook into a loguru sink so events also stream to the dashboard log:
    logger.add(lambda msg: state_store.append_event(
        msg.record['level'].name, msg.record['message']))

# Inside the main while-loop, AT THE TOP of each iteration, do:
        # 1. Drain commands from dashboard
        for c in cmd_queue.drain():
            cmd, arg = c['cmd'], c['arg']
            if cmd == 'HALT':
                order_mgr.state.halted = True
                logger.warning('Dashboard: HALT received')
            elif cmd == 'RESUME':
                order_mgr.state.halted = False
                logger.warning('Dashboard: RESUME received')
            elif cmd == 'SQUARE_OFF_ALL':
                kite.square_off_all()
                order_mgr.state.positions.clear()
                logger.warning('Dashboard: SQUARE_OFF_ALL received')
            elif cmd == 'PAUSE_SYMBOL':
                # add to in-memory paused set
                paused_symbols.add(arg)
                logger.warning(f'Dashboard: paused {arg}')
            elif cmd == 'RESUME_SYMBOL':
                paused_symbols.discard(arg)
            elif cmd == 'RELOAD_SETTINGS':
                s = settings_store.read()
                config['risk']['max_daily_loss_pct'] = s['max_daily_loss_pct']
                config['risk']['per_trade_risk_pct'] = s['per_trade_risk_pct']
                config['risk']['max_trades_per_day'] = s['max_trades_per_day']
                config['risk']['stop_loss_pct'] = s['stop_loss_pct']
                config['risk']['target_pct'] = s['target_pct']
                config['risk']['trade_holding_max'] = s['trade_holding_max']
                config['dry_run']['enabled'] = s['dry_run']
                order_mgr.cfg = config  # re-bind
                if s['dry_run'] and order_mgr.state.dry_run_remaining == 0:
                    order_mgr.state.dry_run_remaining = 3
                if not s['dry_run']:
                    order_mgr.state.dry_run_remaining = 0
                logger.warning(f'Dashboard: settings reloaded — dry_run={s[\"dry_run\"]}')

        # 2. Skip paused symbols when iterating universe
        # Replace 'for symbol, tok in underlying_tokens.items():' with:
        #   for symbol, tok in underlying_tokens.items():
        #       if symbol in paused_symbols: continue

        # 3. At the END of each loop iteration, write state snapshot:
        state_store.write_state({
            'capital': config['capital']['total_inr'],
            'pnl_today': order_mgr.state.pnl,
            'trade_count': order_mgr.state.trade_count,
            'halted': order_mgr.state.halted,
            'dry_run_remaining': order_mgr.state.dry_run_remaining,
            'open_positions': [
                {
                    'symbol': p.tradingsymbol,
                    'qty': p.quantity,
                    'entry_price': p.entry_price,
                    'entry_time': p.entry_time.isoformat(),
                    'sl': p.sl_price,
                    'tp': p.tp_price,
                } for p in order_mgr.state.positions
            ],
            'universe': list(underlying_tokens.keys()),
            'paused_symbols': list(paused_symbols),
        })

# Initialize before the loop:
    paused_symbols: set[str] = set()
"""


# =============================================================================
# FILE: dashboard/app.py
# =============================================================================
"""Streamlit dashboard. Run with:  streamlit run dashboard/app.py

Auto-refreshes every 3 seconds. Reads state file, writes commands + settings.
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import time

# These imports assume dashboard/ is sibling to shared/
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.state_store import StateStore
from shared.commands import CommandQueue
from shared.settings_store import SettingsStore


st.set_page_config(page_title="Kite Options Scalper", layout="wide",
                   initial_sidebar_state="expanded")

state_store = StateStore()
cmd_queue = CommandQueue()
settings_store = SettingsStore()


# ----------- AUTO-REFRESH -----------
REFRESH_SEC = 3
st.markdown(
    f'<meta http-equiv="refresh" content="{REFRESH_SEC}">',
    unsafe_allow_html=True,
)


# ----------- READ CURRENT STATE -----------
state = state_store.read_state()
settings = settings_store.read()


# ----------- HEADER / STATUS BAR -----------
st.title("⚡ Kite Options Scalper — Live Monitor")

last_update = state.get("_updated", "—")
trader_alive = False
if last_update != "—":
    try:
        delta = (datetime.now() - datetime.fromisoformat(last_update)).total_seconds()
        trader_alive = delta < 30
    except Exception:
        pass

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trader", "🟢 LIVE" if trader_alive else "🔴 OFFLINE",
          help=f"Last update: {last_update}")
c2.metric("Mode", "🟡 DRY-RUN" if settings["dry_run"] else "🔴 LIVE TRADING")
c3.metric("Status", "⏸️ HALTED" if settings["halted"] else "▶️ RUNNING")

pnl = state.get("pnl_today", 0)
capital = state.get("capital", 0)
pnl_pct = (pnl / capital * 100) if capital else 0
c4.metric("Day P&L", f"₹{pnl:,.0f}", f"{pnl_pct:+.2f}%")
c5.metric("Trades Today", f"{state.get('trade_count', 0)} / {settings['max_trades_per_day']}")


# ----------- SIDEBAR — KILL SWITCHES + SETTINGS -----------
with st.sidebar:
    st.header("🛑 Emergency Controls")

    if st.button("🚨 HALT TRADING", type="primary", use_container_width=True):
        cmd_queue.enqueue("HALT")
        s = settings_store.read(); s["halted"] = True; settings_store.write(s)
        st.success("HALT command sent")

    if st.button("▶️ RESUME", use_container_width=True):
        cmd_queue.enqueue("RESUME")
        s = settings_store.read(); s["halted"] = False; settings_store.write(s)
        st.success("RESUME command sent")

    if st.button("💥 SQUARE OFF ALL", use_container_width=True):
        cmd_queue.enqueue("SQUARE_OFF_ALL")
        st.success("Square-off command sent")

    st.divider()
    st.header("⚙️ Settings")
    st.caption("Changes only become effective after 'Apply'.")

    new_dry = st.toggle("Dry-run mode", value=settings["dry_run"],
                        help="When ON, signals are logged but no orders placed")
    if not settings["dry_run"] and not new_dry:
        st.info("Currently LIVE. Toggle ON to switch to dry-run.")
    if settings["dry_run"] and not new_dry:
        st.warning("⚠️ Switching OFF dry-run = REAL ORDERS will be placed")

    new_loss_cap = st.slider(
        "Max daily loss %", 5.0, 30.0, float(settings["max_daily_loss_pct"]),
        step=1.0, help="Hard ceiling 30% — cannot exceed")
    new_per_risk = st.slider(
        "Per-trade risk %", 1.0, 5.0, float(settings["per_trade_risk_pct"]),
        step=0.5, help="Hard ceiling 5%")
    new_max_trades = st.slider(
        "Max trades/day", 1, 5, int(settings["max_trades_per_day"]),
        help="Hard ceiling 5")
    new_sl = st.slider(
        "Stop loss %", 5.0, 50.0, float(settings["stop_loss_pct"]), step=2.5)
    new_tp = st.slider(
        "Target %", 10.0, 200.0, float(settings["target_pct"]), step=5.0)
    new_hold = st.slider(
        "Max holding (min)", 2, 30, int(settings["trade_holding_max"]))

    if st.button("Apply Settings", type="primary", use_container_width=True):
        new_settings = dict(settings)
        new_settings.update({
            "dry_run": new_dry,
            "max_daily_loss_pct": new_loss_cap,
            "per_trade_risk_pct": new_per_risk,
            "max_trades_per_day": new_max_trades,
            "stop_loss_pct": new_sl,
            "target_pct": new_tp,
            "trade_holding_max": new_hold,
        })
        settings_store.write(new_settings)
        cmd_queue.enqueue("RELOAD_SETTINGS")
        st.success("Settings applied — trader will pick up next cycle")
        time.sleep(0.5)
        st.rerun()


# ----------- MAIN PANEL -----------
tab1, tab2, tab3, tab4 = st.tabs(["📊 Positions", "🎯 Universe", "📜 Event Log", "📈 P&L"])


# ------ TAB 1: OPEN POSITIONS ------
with tab1:
    st.subheader("Open Positions")
    open_pos = state.get("open_positions", [])
    if open_pos:
        df = pd.DataFrame(open_pos)
        df["entry_time"] = pd.to_datetime(df["entry_time"]).dt.strftime("%H:%M:%S")
        df["age_min"] = df.apply(
            lambda r: (datetime.now() - pd.to_datetime(open_pos[r.name]["entry_time"]))
                       .total_seconds() / 60,
            axis=1).round(1)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions.")

    st.subheader("Today's Closed Trades")
    # Pulled from event log scan — quick filter
    events = state_store.read_events(500)
    closed = [e for e in events if "EXIT" in e]
    if closed:
        st.code("\n".join(closed[-20:]), language="text")
    else:
        st.info("No closed trades yet.")


# ------ TAB 2: UNIVERSE + PAUSE TOGGLES ------
with tab2:
    st.subheader("Today's Universe")
    universe = state.get("universe", [])
    paused = set(settings.get("paused_symbols", []))

    if not universe:
        st.info("Universe not yet loaded by trader.")
    else:
        cols = st.columns(min(4, len(universe)))
        for i, sym in enumerate(universe):
            with cols[i % 4]:
                is_paused = sym in paused
                label = f"{'⏸️' if is_paused else '✅'} {sym}"
                if st.button(label, key=f"univ_{sym}", use_container_width=True):
                    if is_paused:
                        cmd_queue.enqueue("RESUME_SYMBOL", sym)
                        s = settings_store.read()
                        s["paused_symbols"] = [x for x in s["paused_symbols"] if x != sym]
                        settings_store.write(s)
                    else:
                        cmd_queue.enqueue("PAUSE_SYMBOL", sym)
                        s = settings_store.read()
                        if sym not in s["paused_symbols"]:
                            s["paused_symbols"].append(sym)
                        settings_store.write(s)
                    st.rerun()


# ------ TAB 3: EVENT LOG ------
with tab3:
    st.subheader("Event Log (last 200)")
    events = state_store.read_events(200)
    level_filter = st.multiselect(
        "Filter by level",
        ["INFO", "WARNING", "ERROR", "CRITICAL"],
        default=["WARNING", "ERROR", "CRITICAL"],
    )
    filtered = [e for e in events if any(f"[{lvl}]" in e for lvl in level_filter)]
    st.code("\n".join(reversed(filtered[-100:])) or "(no events)", language="text")


# ------ TAB 4: P&L CHART ------
with tab4:
    st.subheader("Cumulative P&L Today")
    events = state_store.read_events(2000)

    # Parse exit events for a P&L curve
    points = []
    cum = 0.0
    for line in events:
        if "EXIT" in line and "pnl=" in line:
            try:
                ts = line.split(" [")[0]
                pnl_str = line.split("pnl=₹")[1].split(" ")[0]
                cum += float(pnl_str)
                points.append({"time": ts, "cumulative_pnl": cum})
            except Exception:
                continue

    if points:
        df = pd.DataFrame(points)
        df["time"] = pd.to_datetime(df["time"])
        st.line_chart(df.set_index("time")["cumulative_pnl"])
    else:
        st.info("No closed trades yet to chart.")


# ----------- FOOTER -----------
st.caption(f"Dashboard auto-refresh every {REFRESH_SEC}s. "
           f"Hard limits enforced in code: max daily loss ≤30%, max risk/trade ≤5%, "
           f"max 5 trades/day, square-off at 15:15 IST. "
           f"These cannot be raised from the dashboard.")
