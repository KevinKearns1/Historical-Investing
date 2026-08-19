#!/usr/bin/env python3
"""Generate SYNTHETIC data so the engine can be exercised without network.

=======================================================================
 THIS IS NOT MARKET DATA. Every price in the files it writes is invented.
 A backtest run against it produces NUMBERS, NOT RESULTS. They say whether
 the plumbing works. They say nothing whatsoever about whether a strategy
 would have made money in 2000.
=======================================================================

Every file it writes is stamped with a `synthetic` column so no run can
mistake it for the real cache, and run_backtest.py prints a loud banner when
it detects that stamp.

The generator does imitate the SHAPE of 2000 -- a January-to-March melt-up in
the high-beta names, a March top, a long decline, high realized vol, and a
value cohort that rises while tech falls -- because plumbing bugs (a regime
filter that never arms, a short that never fills) only surface against data
with those features. Imitating the shape is not the same as reproducing the
history, and nothing here should be read as the latter.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yaml

from engine.clock import sessions_between
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "demo_cache")


def make_series(sym: str, days, seed: int, start_px: float, kind: str,
                earnings_dates=None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(days)
    top = int(n * 0.42)          # a March-like top

    if kind == "tech":
        drift = np.concatenate([np.full(top, 0.0035), np.full(n - top, -0.0022)])
        vol = np.concatenate([np.full(top, 0.040), np.full(n - top, 0.055)])
    elif kind == "value":
        drift = np.concatenate([np.full(top, -0.0008), np.full(n - top, 0.0012)])
        vol = np.full(n, 0.018)
    else:                         # index / etf
        drift = np.concatenate([np.full(top, 0.0020), np.full(n - top, -0.0014)])
        vol = np.concatenate([np.full(top, 0.022), np.full(n - top, 0.030)])

    shocks = rng.standard_normal(n) * vol + drift
    # Overnight gaps. These have to be carried into the OPEN, not just the
    # close -- an earlier version added them to the close only, which meant the
    # synthetic opens never gapped and both gap strategies were untestable.
    gap_day = rng.random(n) < 0.08
    gap_shock = np.zeros(n)
    gap_shock[gap_day] = rng.standard_normal(gap_day.sum()) * 0.075
    shocks = shocks + gap_shock

    # Earnings jumps. Without these the synthetic tape has no events in it at
    # all, so OP-04's whole premise -- that realized earnings moves are larger
    # than the market prices -- has nothing to measure and the strategy sits
    # out the entire run looking broken when it is merely correct.
    if earnings_dates:
        idx = {d: i for i, d in enumerate(days)}
        for ev in earnings_dates:
            # The reaction prints on the session AFTER the announcement.
            after = [i for d, i in idx.items() if d > ev]
            if not after:
                continue
            i = min(after)
            jump = rng.standard_normal() * 0.16      # fat, 2000-style reactions
            shocks[i] += jump
            gap_shock[i] += jump * 0.85              # mostly an overnight gap

    close = start_px * np.exp(np.cumsum(shocks))

    prev = np.concatenate([[start_px], close[:-1]])
    # The open carries the whole overnight gap plus a little noise.
    op = prev * np.exp(gap_shock) * (1 + rng.standard_normal(n) * vol * 0.20)
    rng_day = np.abs(rng.standard_normal(n)) * vol * close * 1.3
    hi = np.maximum(op, close) + rng_day * 0.6
    lo = np.minimum(op, close) - rng_day * 0.6
    lo = np.maximum(lo, 0.5)
    base_v = {"tech": 2.5e7, "value": 8e6, "etf": 3e7}[kind]
    vol_sh = base_v * (1 + 0.5 * np.abs(rng.standard_normal(n)))
    vol_sh[gap_day] *= 1.9      # news days trade heavy

    return pd.DataFrame({
        "date": pd.to_datetime(days), "open": op, "high": hi, "low": lo,
        "close": close, "adj_close": close, "volume": vol_sh.round(),
        "synthetic": 1,
    }).set_index("date")


def main() -> int:
    with open(os.path.join(ROOT, "config/universe_2000.yml")) as f:
        uni = yaml.safe_load(f)
    os.makedirs(OUT, exist_ok=True)
    days = sessions_between(date(1999, 1, 4), date(2001, 1, 31))

    with open(os.path.join(ROOT, "config/earnings_2000.yml")) as f:
        ecfg = yaml.safe_load(f)
    from datetime import datetime as _dt
    earnings = {k: [_dt.strptime(x, "%Y-%m-%d").date() for x in v]
                for k, v in ecfg.items() if isinstance(v, list)}

    groups = [("tech", uni["tech"], 60.0), ("value", uni["value"], 45.0),
              ("etf", uni["etfs"], 130.0)]
    groups.append(("etf", [uni["benchmark"], uni["secondary_benchmark"]], 2500.0))

    written = 0
    for i, (kind, syms, px0) in enumerate(groups):
        for j, s in enumerate(syms):
            df = make_series(s, days, seed=1000 * i + j, start_px=px0 * (0.6 + 0.9 * ((j % 7) / 7)),
                             kind=kind, earnings_dates=earnings.get(s))
            df.to_csv(os.path.join(OUT, f"{s.replace('^','_idx_')}.csv"))
            written += 1

    print(f"Wrote {written} SYNTHETIC files to {OUT}")
    print("\n*** These are invented prices. Any backtest run against them")
    print("*** measures the engine, not any strategy's real 2000 performance.")
    print(f"\nSmoke test:\n  python3 scripts/run_backtest.py --cache-dir {OUT} "
          f"--start 2000-01-03 --end 2000-12-29 --minute-step 5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
