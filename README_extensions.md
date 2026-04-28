Good. Doing all three properly means this becomes a real project, not a single file. I'll add them as a second artifact alongside the first one — keeping main.py and the existing modules intact, and adding three new modules plus integration hooks.

One important caveat before I build: **a backtest harness for options scalping with Kite historical data has fundamental limitations** I want you to know upfront, not after you've trusted backtest results:

1. **Kite historical API gives OHLC, not tick data.** A 1-minute candle hides the path within that minute. For 10-15 min scalps where SL/TP can hit mid-candle, this systematically *overstates* backtest profits.
2. **No bid-ask spread in historical data.** Real fills won't be at LTP. I'll inject a configurable slippage model (default 2% of premium per side) so backtests aren't fantasy numbers.
3. **Survivorship in option chains.** Expired contracts disappear from instrument dumps, so historical strike selection has to reconstruct what was ATM *then*, not now.
4. **Liquidity changes intraday.** A strike that was tight at 10 AM may be 10% wide at 2 PM. Backtest can't see this.

I'm building it anyway because *directionally* it's still useful — for filtering out obviously-broken signal logic and for parameter sanity. Just don't treat green backtest equity curves as predictive of live performance. I'll print a big warning at the end of every backtest run.## What Each Piece Does

**1. `LiveUniverseRanker`** — replaces the placeholder. Each morning it batch-quotes ATM CE+PE pairs across the entire F&O universe (~180 names → ~360 quotes in 2-3 API calls), scores each underlying by `OI + 0.5×volume − 1000×spread%`, and applies hard filters (min OI, max spread). Only ~4 API calls total at session start. Drops indices by default so you get pure stock universe; remove from the `EXCLUDE` set if you want NIFTY/BANKNIFTY back in.

**2. `GreeksFilter`** — uses `py_vollib` for IV and delta. The interesting design choice: it **refuses to gate signals on Greeks during the last 1.5 hours of expiry day** because Black-Scholes assumptions break down there (gamma → ∞, IV becomes meaningless). Instead it just blocks new entries during that window. For non-expiry-day or earlier in the session, it filters: delta in [0.30, 0.75] (avoids both deep OTM lottery tickets and deep ITM where you're paying for intrinsic value not movement), and IV-spike detection (skip if IV > 2.5× its rolling median, which usually means an event is priced in and you're buying the top).

**3. `BacktestEngine`** — uses Kite's `historical_data` API for both underlying and option 1m candles, replays minute-by-minute, and substitutes a `_BacktestBroker` into the same `OrderManager` your live code uses. **Same code path = no logic divergence between backtest and live**, which is the only way backtests are useful.

## Critical Limitations Printed Every Run

I made the engine print this at the end of every backtest because it's that important:

```
1. OHLC fills approximate; intra-candle path ignored.
2. Slippage applied at 2%/side; real may be 2-10x worse
   at expiry / illiquid strikes.
3. Survivorship bias: expired contracts missing from chain dump.
4. No order-rejection / margin-shortfall simulation.
5. Backtest universe is today's most-liquid set, not the day's actual.
```

The intra-candle issue is the biggest one. If your 1m candle goes 100→90→105→102, your SL at 92 *did* hit, but the close-only backtest sees 102 and counts the trade as a winner. **This systematically inflates backtest results.** The 2%/side slippage is meant to partially compensate but it's a blunt instrument.

## How to Use

**Backtest first** (please do this for at least 2 weeks of historical data before going live):
```bash
python -m backtest.runner --start 2026-04-01 --end 2026-04-25 \
  --universe RELIANCE,HDFCBANK,ICICIBANK,TCS,INFY
```

**Apply the integration patches** to `main.py` (shown in the `PATCHES` string at the bottom of artifact 2) — three small edits that wire the new universe ranker and Greeks filter into the live loop.

**Install the new deps:**
```bash
pip install py_vollib mibian scipy tqdm
```

## What I'd Suggest Doing Next, in Order

First, run the backtest on the last 30 days. If the strategy shows positive returns *after* the 2% slippage assumption, that's a baseline — but assume real performance is 30-50% worse than backtest. Second, with `dry_run.enabled: true`, run live for 2-3 sessions and compare the dry-run signals against what backtest would have produced on those exact days; this tells you if your live data pipeline matches your historical pipeline. Third, only then flip `dry_run.enabled: false` — and for the first week, manually halve `capital.total_inr` in config (so a bad day caps at ₹2,250 loss instead of ₹4,500).

If you want, I can build one more piece: a **simple Streamlit dashboard** that shows live positions, today's P&L, signals fired, and the dry-run log — so you're not staring at log files all day. Useful for the first few weeks of live operation when you want eyes on the agent. Want me to add that?
