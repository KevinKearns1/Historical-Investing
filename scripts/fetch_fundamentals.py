#!/usr/bin/env python3
"""Normalize a point-in-time fundamentals export into data/fundamentals/.

THE ONE THING THAT MATTERS: your source must carry a FILING DATE.

A dataset with only period_end is not point-in-time, no matter what it is
called. Q4 1999 numbers stamped 1999-12-31 and used on 2000-01-02 give the
strategy three months of foresight it could not have had. If your export has no
filing-date column, this script will refuse it rather than let it through with
an assumed lag.

Supported (--format):
    sharadar   Nasdaq Data Link Sharadar SF1. `datekey` IS the filing date;
               `dimension` selects the view -- use ARQ (as-reported quarterly)
               for point-in-time work, NOT MRQ.
    compustat  WRDS Compustat Point-in-Time. Use the PITDATE / DATADATE pair.
    generic    any CSV, with --period-col and --filed-col named explicitly.

    python3 scripts/fetch_fundamentals.py --format sharadar --src sf1.csv
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "fundamentals")

# Vendor column -> engine field.
SHARADAR = {
    "ticker": "symbol", "calendardate": "period_end", "datekey": "available_on",
    "revenue": "revenue", "netinc": "net_income", "epsdil": "eps_diluted",
    "ebitda": "ebitda", "opinc": "operating_income", "gp": "gross_profit",
    "assets": "total_assets", "equity": "total_equity", "debt": "total_debt",
    "cashneq": "cash", "ncfo": "operating_cash_flow", "capex": "capex",
    "shareswadil": "shares_diluted", "dps": "dividends_per_share", "bvps": "book_value_per_share",
}
COMPUSTAT = {
    "tic": "symbol", "datadate": "period_end", "pitdate": "available_on",
    "revtq": "revenue", "niq": "net_income", "epsfxq": "eps_diluted",
    "oiadpq": "operating_income", "atq": "total_assets", "seqq": "total_equity",
    "dlttq": "total_debt", "cheq": "cash", "oancfy": "operating_cash_flow",
    "capxy": "capex", "cshfdq": "shares_diluted",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--format", default="sharadar", choices=["sharadar", "compustat", "generic"])
    ap.add_argument("--period-col", help="generic: fiscal period end column")
    ap.add_argument("--filed-col", help="generic: FILING DATE column (required)")
    ap.add_argument("--dimension", default="ARQ",
                    help="sharadar: ARQ = as-reported quarterly (the PIT view)")
    ap.add_argument("--out", default=os.path.join(OUT, "fundamentals.csv"))
    a = ap.parse_args()

    df = pd.read_csv(a.src)
    df.columns = [c.strip().lower() for c in df.columns]

    if a.format == "sharadar":
        if "dimension" in df.columns:
            before = len(df)
            df = df[df["dimension"].str.upper() == a.dimension.upper()]
            print(f"dimension={a.dimension}: kept {len(df):,} of {before:,} rows")
            if a.dimension.upper().startswith("MR"):
                print("\nWARNING: MR* dimensions are the CURRENT view -- they carry\n"
                      "restated figures back-propagated to old periods. For\n"
                      "point-in-time work use ARQ or ARY.\n")
        mapping = SHARADAR
    elif a.format == "compustat":
        mapping = COMPUSTAT
    else:
        if not (a.period_col and a.filed_col):
            print("generic format requires --period-col and --filed-col")
            return 2
        mapping = {a.period_col.lower(): "period_end", a.filed_col.lower(): "available_on"}
        for c in df.columns:
            if c not in mapping:
                mapping[c] = c

    out = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})

    if "available_on" not in out.columns:
        print("\nREFUSING TO WRITE: no filing-date column found.\n"
              "This export is not point-in-time. Using period_end as the\n"
              "availability date would hand every strategy up to three months of\n"
              "foresight on every name, every quarter -- which is the exact bias\n"
              "this whole engine exists to prevent.\n"
              f"Columns present: {sorted(df.columns)[:25]}")
        return 3

    keep = ["symbol", "period_end", "available_on"] + [
        v for v in set(mapping.values())
        if v not in ("symbol", "period_end", "available_on") and v in out.columns]
    out = out[[c for c in keep if c in out.columns]].copy()
    out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
    out["available_on"] = pd.to_datetime(out["available_on"]).dt.date
    out = out.dropna(subset=["symbol", "period_end", "available_on"])

    lag = (pd.to_datetime(out["available_on"]) - pd.to_datetime(out["period_end"])).dt.days
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    out.sort_values(["symbol", "available_on"]).to_csv(a.out, index=False)

    print(f"\nwrote {len(out):,} records for {out['symbol'].nunique()} symbols -> {a.out}")
    print(f"reporting lag (filing date minus period end):")
    print(f"  median {lag.median():.0f} days, p90 {lag.quantile(0.9):.0f}, max {lag.max():.0f}")
    if lag.min() < 0:
        print(f"\n  WARNING: {(lag < 0).sum()} records are filed BEFORE their period\n"
              f"  ends. That is impossible; check the column mapping.")
    if lag.median() < 5:
        print("\n  WARNING: a median lag of a few days is not plausible for real\n"
              "  filings. This export is probably NOT point-in-time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
