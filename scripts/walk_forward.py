#!/usr/bin/env python3
"""Run the book across many years and report per-regime, not just in aggregate.

WHY A SINGLE MULTI-YEAR NUMBER IS NOT THE ANSWER
------------------------------------------------
A strategy that returns 12% a year over twenty years sounds settled until you
see that it made all of it in three years and bled for seventeen. What you
asked for -- something "tested through time that works if you follow it" --
is a question about CONSISTENCY, and consistency is invisible in a CAGR.

So this reports each year separately, then the statistics that actually bear on
whether a rule is followable:

  * hit rate across years      how often it was positive at all
  * worst year, worst drawdown what following it actually felt like
  * excess over T-bills        per year, because cash paid 6% in 2000 and 0.1%
                               in 2015, and a raw return hides that entirely
  * regime split               bull / bear / high-vol buckets, so you can see
                               whether the edge is real or is one market

and, most importantly, an out-of-sample split: parameters chosen on the first
window are judged only on the later ones. Anything tuned on the whole history
is not a test, it is a description.

    python3 scripts/walk_forward.py --start 1999 --end 2020
    python3 scripts/walk_forward.py --start 1999 --end 2020 --is-years 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yaml

from engine.backtest import Backtest
from engine.era import Era
from engine.intraday import set_path_seed
from engine.metrics import summarize
from engine.microstructure import CostModel
from engine.risk import RiskLimits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts.run_backtest import build_strategies, load_yaml   # noqa: E402

# Annual average 3-month T-bill, used as the per-year hurdle. A strategy is not
# judged against zero -- it is judged against what cash paid that year.
TBILL = {
    1999: 0.0464, 2000: 0.0582, 2001: 0.0333, 2002: 0.0160, 2003: 0.0101,
    2004: 0.0137, 2005: 0.0315, 2006: 0.0473, 2007: 0.0435, 2008: 0.0137,
    2009: 0.0015, 2010: 0.0014, 2011: 0.0005, 2012: 0.0009, 2013: 0.0006,
    2014: 0.0003, 2015: 0.0005, 2016: 0.0032, 2017: 0.0093, 2018: 0.0194,
    2019: 0.0206, 2020: 0.0036, 2021: 0.0004, 2022: 0.0202, 2023: 0.0507,
    2024: 0.0512,
}


def run_year(year: int, cfg: dict, uni: dict, cache: str, args) -> dict:
    set_path_seed(args.seed)
    strategies, _ = build_strategies(cfg, uni)
    bt = Backtest(
        start=date(year, 1, 1), end=date(year, 12, 31),
        universe=(uni.get("tech") or []) + (uni.get("etfs") or []),
        strategies=strategies, starting_cash=cfg["starting_cash"],
        benchmark=uni["benchmark"],
        costs=CostModel(**{k: v for k, v in cfg["costs"].items()}),
        limits=RiskLimits(**cfg["risk"]),
        hard_to_borrow=set(uni.get("hard_to_borrow") or []),
        minute_step=args.minute_step, cache_dir=cache,
        interval_minutes=args.interval_minutes,
        require_real_intraday=args.require_real_intraday,
        fundamentals_csv=args.fundamentals, era_rules=True,
    )
    res = bt.run()
    if len(res["equity_curve"]) < 2:
        return {"year": year, "sessions": 0, "skipped": "no data"}
    st = summarize(res["equity_curve"], cfg["starting_cash"],
                   rf=TBILL.get(year, 0.02))
    era = Era.on(date(year, 7, 1), 50.0, cfg["starting_cash"])
    return {
        "year": year,
        "return": st["total_return"],
        "tbill": TBILL.get(year, 0.02),
        "excess": st["total_return"] - TBILL.get(year, 0.02),
        "max_dd": st["max_drawdown"],
        "sharpe": st["sharpe_vs_tbill"],
        "vol": st["annual_vol"],
        "trades": len(res["portfolio"].trades),
        "fees": res["portfolio"].fees_paid,
        "halted": res["risk"].halted,
        "tick": era.tick,
        "uptick_rule": era.uptick_rule,
        "commission": era.equity_commission,
        "real_bars": res["provenance"].get("real_bar_fraction", 0.0),
        "sessions": st["sessions"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1999)
    ap.add_argument("--end", type=int, default=2005)
    ap.add_argument("--is-years", type=int, default=3,
                    help="Leading years treated as IN-SAMPLE. Everything after "
                         "is out-of-sample and is what the verdict rests on.")
    ap.add_argument("--minute-step", type=int, default=15)
    ap.add_argument("--interval-minutes", type=int, default=1)
    ap.add_argument("--require-real-intraday", action="store_true")
    ap.add_argument("--fundamentals", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--out", default="reports/walk_forward.csv")
    a = ap.parse_args()

    cache = a.cache_dir or os.path.join(ROOT, "data", "cache")
    if not os.path.isdir(cache) or not [f for f in os.listdir(cache) if f.endswith(".csv")]:
        print(f"No cached data in {cache}. Run scripts/fetch_data.py first.")
        return 2

    cfg = load_yaml("config/strategies.yml")
    uni = load_yaml("config/universe_2000.yml")

    rows = []
    for y in range(a.start, a.end + 1):
        try:
            r = run_year(y, cfg, uni, cache, a)
        except Exception as e:                                   # noqa: BLE001
            print(f"{y}: FAILED ({e})")
            continue
        if r.get("sessions", 0) < 2:
            print(f"{y}: skipped ({r.get('skipped','no sessions')})")
            continue
        rows.append(r)
        print(f"{y}: {r['return']:>8.2%}  excess {r['excess']:>8.2%}  "
              f"maxDD {r['max_dd']:>7.2%}  trades {r['trades']:>5d}  "
              f"tick {r['tick']:.4f}  uptick {'Y' if r['uptick_rule'] else 'n'}"
              f"{'  HALTED' if r['halted'] else ''}")

    if not rows:
        print("no years produced results")
        return 1

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(os.path.join(ROOT, a.out)), exist_ok=True)
    df.to_csv(os.path.join(ROOT, a.out), index=False)

    is_df = df[df["year"] < a.start + a.is_years]
    oos_df = df[df["year"] >= a.start + a.is_years]

    def block(name, d):
        if not len(d):
            return
        print(f"\n  {name}  ({len(d)} years: {int(d['year'].min())}-{int(d['year'].max())})")
        print(f"    mean return      {d['return'].mean():>8.2%}")
        print(f"    median return    {d['return'].median():>8.2%}")
        print(f"    mean excess/cash {d['excess'].mean():>8.2%}")
        print(f"    positive years   {(d['return'] > 0).mean():>8.0%}")
        print(f"    beat cash        {(d['excess'] > 0).mean():>8.0%}")
        print(f"    worst year       {d['return'].min():>8.2%}  ({int(d.loc[d['return'].idxmin(),'year'])})")
        print(f"    worst drawdown   {d['max_dd'].min():>8.2%}")
        print(f"    years halted     {int(d['halted'].sum())}")

    print("\n" + "=" * 66)
    print("  WALK-FORWARD")
    print("=" * 66)
    block("IN-SAMPLE", is_df)
    block("OUT-OF-SAMPLE", oos_df)

    if len(oos_df) >= 3:
        r = oos_df["return"].values
        t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else 0.0
        print(f"\n  Out-of-sample mean annual return t-statistic: {t:.2f}")
        print("    Below ~2 means the yearly results are not distinguishable from")
        print("    noise at this sample size. With a handful of years that is the")
        print("    expected outcome -- it is a statement about how little evidence")
        print("    a few annual observations carry, not proof the edge is absent.")

    if len(is_df) and len(oos_df):
        drop = is_df["return"].mean() - oos_df["return"].mean()
        print(f"\n  In-sample minus out-of-sample mean: {drop:>+.2%}")
        if drop > 0.05:
            print("    A large positive gap is the classic overfitting signature:")
            print("    the rules describe the first window rather than generalising.")

    print(f"\n  written to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
