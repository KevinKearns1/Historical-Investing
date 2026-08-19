#!/usr/bin/env python3
"""Load real intraday bars from a vendor export into data/intraday/.

NO FREE SOURCE HAS YEAR-2000 INTRADAY DATA. That is the finding, and it is not
for want of looking -- see docs/DATA_SOURCES.md for what was tested and what
each provider actually carries. This script therefore does not scrape anything.
It converts an export you have obtained into the engine's format, validates it,
and refuses anything that shows the vendor gap-fill signature.

Supported layouts (--format):
    taq         NYSE TAQ / WRDS extract (trades or 1-min bars)
    kibot       Kibot 1-minute:  date,time,open,high,low,close,volume
    quantquote  QuantQuote 1-minute:  date,time,open,high,low,close,volume,splits
    tickdata    Tick Data LLC 1-minute
    polygon     Polygon.io aggregates JSON/CSV (2003+, so NOT useful for 2000)
    generic     any CSV with a parseable timestamp + OHLCV columns

Everything is normalised to one file per symbol, timestamps in UTC, and then
resampled at RUN time to whatever bar size the simulation asks for -- so pull
1-minute once and run 1, 2, 5 or 15-minute simulations off it.

    python3 scripts/fetch_intraday.py --format kibot --src ~/kibot/ --symbols MSFT INTC
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from engine.provenance import drop_interpolated, validate_bars

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "intraday")

STD = ["open", "high", "low", "close", "volume"]


def _read_kibot(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, header=None,
                     names=["date", "time", "open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(df["date"] + " " + df["time"], format="%m/%d/%Y %H:%M")
    return pd.DataFrame({**{c: df[c] for c in STD}}, index=ts)


def _read_quantquote(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, header=None,
                     names=["date", "time", "open", "high", "low", "close", "volume",
                            "splits", "earnings", "dividends"][:len(pd.read_csv(path, nrows=1).columns)])
    ts = pd.to_datetime(df["date"].astype(str), format="%Y%m%d") + \
        pd.to_timedelta(df["time"].astype(int) // 100 * 60 + df["time"].astype(int) % 100, unit="m")
    return pd.DataFrame({c: df[c] for c in STD if c in df.columns}, index=ts)


def _read_generic(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next((c for c in df.columns if c in
                 ("timestamp", "datetime", "date_time", "time", "date", "begins_at")), None)
    if tcol is None:
        raise ValueError(f"{path}: no timestamp column found")
    ts = pd.to_datetime(df[tcol], errors="coerce", utc=False)
    ren = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
           "open_price": "open", "high_price": "high", "low_price": "low",
           "close_price": "close", "vol": "volume"}
    df = df.rename(columns=ren)
    keep = {c: df[c] for c in STD if c in df.columns}
    if "interpolated" in df.columns:
        keep["interpolated"] = df["interpolated"]
    return pd.DataFrame(keep, index=ts).dropna(how="all")


READERS = {"kibot": _read_kibot, "quantquote": _read_quantquote,
           "taq": _read_generic, "tickdata": _read_generic,
           "polygon": _read_generic, "generic": _read_generic}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="directory of vendor files")
    ap.add_argument("--format", default="generic", choices=sorted(READERS))
    ap.add_argument("--symbols", nargs="*", help="limit to these symbols")
    ap.add_argument("--tz", default="America/New_York",
                    help="timezone the vendor stamps its bars in")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    reader = READERS[a.format]
    files = sorted(glob.glob(os.path.join(a.src, "**", "*.*"), recursive=True))
    files = [f for f in files if f.lower().endswith((".csv", ".txt", ".json"))]
    if not files:
        print(f"no files under {a.src}")
        return 2

    by_symbol: dict[str, list] = {}
    for f in files:
        sym = os.path.splitext(os.path.basename(f))[0].split("_")[0].upper()
        if a.symbols and sym not in {s.upper() for s in a.symbols}:
            continue
        try:
            df = reader(f)
        except Exception as e:                                   # noqa: BLE001
            print(f"  {os.path.basename(f):40s} SKIP ({e})")
            continue
        by_symbol.setdefault(sym, []).append(df)

    accepted, rejected = [], []
    for sym, parts in sorted(by_symbol.items()):
        df = pd.concat(parts).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        if df.index.tz is None:
            df.index = df.index.tz_localize(a.tz, ambiguous="NaT", nonexistent="NaT")
        df = df[df.index.notna()]
        df.index = df.index.tz_convert("UTC")

        rep = validate_bars(df, sym, "intraday")
        if not rep.usable:
            print(f"  {sym:8s} {rep.summary()}")
            rejected.append(sym)
            continue
        df = drop_interpolated(df)
        df.index.name = "timestamp"
        path = os.path.join(a.out, f"{sym.replace('^','_idx_')}.csv")
        df[[c for c in STD if c in df.columns]].to_csv(path)
        span = f"{df.index[0].date()} -> {df.index[-1].date()}"
        print(f"  {sym:8s} {len(df):8,d} bars  {span}  -> {os.path.basename(path)}")
        accepted.append(sym)

    print(f"\naccepted {len(accepted)} symbols, rejected {len(rejected)}")
    if rejected:
        print("rejected:", ", ".join(rejected))
        print("A rejection means the series showed a gap-fill signature -- flat "
              "prices, zero volume, or long unchanged runs. The engine will fall "
              "back to reconstruction for those names rather than trade on filler.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
