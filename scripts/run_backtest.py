#!/usr/bin/env python3
"""Run the 2000 simulation.

    python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31

Runs entirely off data/cache/. If the cache is empty it says so and stops
rather than quietly producing a zero-trade run that looks like a result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import yaml

from engine.backtest import Backtest
from engine.intraday import set_path_seed
from engine.metrics import monthly_table, per_strategy, summarize
from engine.microstructure import CostModel
from engine.risk import RiskLimits

from strategies.eq01_opening_range_breakout import OpeningRangeBreakout
from strategies.eq02_gap_trade import GapTrade
from strategies.eq03_vwap_reversion import VWAPReversion
from strategies.eq04_momentum_rotation import MomentumRotation
from strategies.eq05_downtrend_bounce_short import DowntrendBounceShort
from strategies.op01_protective_puts import ProtectiveLongPuts
from strategies.op02_bear_put_spreads import BearPutSpread
from strategies.op03_cash_secured_puts import CashSecuredPuts
from strategies.op04_earnings_strangle import EarningsStrangle
from strategies.op05_collar_overlay import CollarOverlay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_yaml(p):
    with open(os.path.join(ROOT, p)) as f:
        return yaml.safe_load(f)


def build_strategies(cfg, uni):
    eq = cfg["sleeves"]["equity"] / 5.0
    op = cfg["sleeves"]["options"] / 5.0
    earn_cfg = load_yaml("config/earnings_2000.yml")
    earnings = {k: [datetime.strptime(d, "%Y-%m-%d").date() for d in v]
                for k, v in earn_cfg.items()
                if isinstance(v, list) and k not in ("verified", "note")}
    return [
        OpeningRangeBreakout(capital=eq),
        GapTrade(capital=eq),
        VWAPReversion(capital=eq),
        MomentumRotation(capital=eq, benchmark=uni["benchmark"]),
        DowntrendBounceShort(capital=eq, benchmark=uni["benchmark"]),
        ProtectiveLongPuts(capital=op, benchmark=uni["benchmark"]),
        BearPutSpread(capital=op, symbol="QQQ"),
        CashSecuredPuts(capital=op, universe=uni.get("value", [])),
        EarningsStrangle(capital=op, earnings=earnings),
        CollarOverlay(capital=op, symbol="SPY"),
    ], earn_cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--end", default="2000-12-31")
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--minute-step", type=int, default=1,
                    help="1 = every minute (slow, faithful). 5 = every 5 minutes.")
    ap.add_argument("--path-seeds", type=int, default=1,
                    help="Re-run with N different synthetic intraday paths to "
                         "measure how much of the result is path-luck.")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    cache = a.cache_dir or os.path.join(ROOT, "data", "cache")
    csvs = [f for f in os.listdir(cache) if f.endswith(".csv")] if os.path.isdir(cache) else []
    if not csvs:
        print(f"No cached data in {cache}.\n"
              f"Run:  python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31\n"
              f"(or:  python3 scripts/make_demo_data.py   for a synthetic smoke test)")
        return 2

    cfg = load_yaml("config/strategies.yml")
    uni = load_yaml("config/universe_2000.yml")
    cash = a.cash or cfg["starting_cash"]
    universe = (uni.get("tech") or []) + (uni.get("etfs") or [])
    start = datetime.strptime(a.start, "%Y-%m-%d").date()
    end = datetime.strptime(a.end, "%Y-%m-%d").date()

    runs = []
    for seed in range(a.path_seeds):
        set_path_seed(seed)
        strategies, earn_cfg = build_strategies(cfg, uni)
        bt = Backtest(
            start=start, end=end, universe=universe, strategies=strategies,
            starting_cash=cash, benchmark=uni["benchmark"],
            costs=CostModel(**{k: v for k, v in cfg["costs"].items()}),
            limits=RiskLimits(**cfg["risk"]),
            hard_to_borrow=set(uni.get("hard_to_borrow") or []),
            minute_step=a.minute_step, verbose=a.verbose, cache_dir=cache,
        )
        res = bt.run()
        runs.append(res)
        stats = summarize(res["equity_curve"], cash)
        print(f"seed {seed}: end {stats['ending_equity']:>12,.2f}  "
              f"return {stats['total_return']:>7.2%}  "
              f"maxDD {stats['max_drawdown']:>7.2%}  "
              f"trades {len(res['portfolio'].trades)}")

    base = runs[0]
    stats = summarize(base["equity_curve"], cash)
    strat = per_strategy(base["portfolio"].trades)
    months = monthly_table(base["equity_curve"])

    outdir = os.path.join(ROOT, a.out)
    os.makedirs(outdir, exist_ok=True)
    pd.DataFrame(base["daily"]).to_csv(os.path.join(outdir, "daily.csv"), index=False)
    if len(strat):
        strat.to_csv(os.path.join(outdir, "by_strategy.csv"), index=False)
    if len(months):
        months.to_csv(os.path.join(outdir, "monthly.csv"), index=False)
    pd.DataFrame([{
        "ts": t.ts, "strategy": t.strategy, "symbol": t.symbol, "side": t.side,
        "qty": t.qty, "price": round(t.price, 4), "fees": round(t.fees, 2),
        "kind": t.kind, "pnl": round(t.pnl, 2), "note": t.note,
    } for t in base["portfolio"].trades]).to_csv(
        os.path.join(outdir, "trades.csv"), index=False)

    path_spread = None
    if len(runs) > 1:
        ends = [r["equity_curve"][-1][1] for r in runs]
        path_spread = {"n_paths": len(ends), "min": min(ends), "max": max(ends),
                       "mean": sum(ends) / len(ends),
                       "spread_pct": (max(ends) - min(ends)) / cash}

    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump({"stats": stats, "path_sensitivity": path_spread,
                   "rejects": base["rejects"],
                   "missing_symbols": base["missing_symbols"],
                   "halted": base["risk"].halted,
                   "halt_reason": base["risk"].halt_reason,
                   "fees_paid": round(base["portfolio"].fees_paid, 2),
                   "interest_paid": round(base["portfolio"].interest_paid, 2),
                   "borrow_paid": round(base["portfolio"].borrow_paid, 2)},
                  f, indent=2, default=str)

    # -- console report ---------------------------------------------------
    print("\n" + "=" * 72)
    print(f"  2000 SIMULATION   {start} -> {end}")
    print("=" * 72)
    for k in ("starting_equity", "ending_equity", "total_return", "cagr",
              "annual_vol", "sharpe_vs_tbill", "sortino", "max_drawdown",
              "max_dd_date", "calmar", "pct_up_days", "excess_over_cash"):
        v = stats.get(k)
        if isinstance(v, float):
            v = f"{v:,.4f}" if abs(v) > 100 else f"{v:.4f}"
        print(f"  {k:<22} {v}")
    print(f"  {'fees_paid':<22} {base['portfolio'].fees_paid:,.2f}")
    print(f"  {'margin_interest':<22} {base['portfolio'].interest_paid:,.2f}")
    print(f"  {'short_borrow':<22} {base['portfolio'].borrow_paid:,.2f}")
    if base["risk"].halted:
        print(f"\n  HALTED: {base['risk'].halt_reason}")
    if base["missing_symbols"]:
        print(f"\n  survivorship gap ({len(base['missing_symbols'])} names not in cache):")
        print("    " + ", ".join(base["missing_symbols"]))
    if path_spread:
        print(f"\n  path sensitivity across {path_spread['n_paths']} synthetic intraday paths:")
        print(f"    ending equity {path_spread['min']:,.0f} .. {path_spread['max']:,.0f} "
              f"(spread {path_spread['spread_pct']:.1%} of starting capital)")
    if len(strat):
        print("\n  BY STRATEGY")
        print(strat.to_string(index=False))
    if len(months):
        print("\n  MONTHLY")
        print(months.to_string(index=False))
    if base["rejects"]:
        print("\n  order rejects:", dict(sorted(base["rejects"].items(),
                                                key=lambda x: -x[1])))
    print(f"\n  written to {outdir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
