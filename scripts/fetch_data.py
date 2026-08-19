#!/usr/bin/env python3
"""Download the daily bars the simulation needs, once, to data/cache/.

This is the ONLY component that touches the network. Everything else runs off
the cache, which means a backtest is fully reproducible and cannot accidentally
reach for live data mid-run.

It deliberately pulls from 1999-01-01 so that on 2000-01-03 -- the first
session of the simulation -- a 250-day lookback already exists. Without a
warm-up year the first quarter of 2000 would trade on half-formed indicators.

    python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31

Missing symbols are reported, not silently skipped. That report is the
survivorship-bias measurement for the run: every name that fails to download is
a company whose 2000 history the data source no longer carries.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "cache")


def load_universe(path: str) -> tuple[list[str], dict]:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    syms = []
    for k in ("tech", "value", "etfs"):
        syms += cfg.get(k, []) or []
    syms += [cfg.get("benchmark"), cfg.get("secondary_benchmark")]
    return sorted({s for s in syms if s}), cfg


def fetch(symbols: list[str], start: str, end: str, pause: float = 0.4) -> dict:
    import pandas as pd
    import yfinance as yf

    os.makedirs(CACHE, exist_ok=True)
    ok, missing, short = [], [], []
    for sym in symbols:
        try:
            df = yf.download(sym, start=start, end=end, interval="1d",
                             auto_adjust=False, progress=False, threads=False)
        except Exception as e:                       # noqa: BLE001
            print(f"  {sym:8s} ERROR {e}")
            missing.append(sym)
            continue
        if df is None or df.empty:
            print(f"  {sym:8s} no data returned")
            missing.append(sym)
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Adj Close": "adj_close",
                                "Volume": "volume"})
        keep = [c for c in ["open", "high", "low", "close", "adj_close", "volume"]
                if c in df.columns]
        df = df[keep].dropna(how="all")
        if "adj_close" not in df.columns:
            df["adj_close"] = df["close"]
        df.index.name = "date"
        # A name that only starts partway through is a listing-date problem and
        # the run needs to know about it (IWM, for example, listed 2000-05-22).
        first = df.index[0].date().isoformat()
        if first > "1999-03-01":
            short.append((sym, first))
            print(f"  {sym:8s} {len(df):5d} rows  ** starts {first} **")
        else:
            print(f"  {sym:8s} {len(df):5d} rows  {first} -> {df.index[-1].date()}")
        df.to_csv(os.path.join(CACHE, f"{sym.replace('^','_idx_')}.csv"))
        ok.append(sym)
        time.sleep(pause)
    return {"ok": ok, "missing": missing, "short_history": short}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="config/universe_2000.yml")
    ap.add_argument("--start", default="1999-01-01")
    ap.add_argument("--end", default="2001-01-31")
    a = ap.parse_args()

    symbols, _ = load_universe(a.universe)
    print(f"Fetching {len(symbols)} symbols, {a.start} -> {a.end}\n")
    res = fetch(symbols, a.start, a.end)

    print(f"\n{'='*64}\nfetched : {len(res['ok'])}")
    if res["short_history"]:
        print("late listings (unavailable at the start of the sim):")
        for s, d in res["short_history"]:
            print(f"   {s} first bar {d}")
    if res["missing"]:
        print(f"\nSURVIVORSHIP GAP -- {len(res['missing'])} names unavailable:")
        print("  " + ", ".join(res["missing"]))
        print("\n  These are companies whose 2000 history the data source no longer")
        print("  serves, mostly because they were acquired or went bankrupt. Their")
        print("  absence biases long strategies UP and short strategies DOWN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
