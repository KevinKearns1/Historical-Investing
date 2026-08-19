"""Point-in-time fundamentals.

THE WHOLE PROBLEM IN ONE SENTENCE
---------------------------------
A company's Q4 1999 results are a fact about 1999, but they became KNOWABLE on
the day the filing hit the wire -- and using them one day earlier is the single
most productive way to build a backtest that cannot be traded.

So every record here carries two dates:

    period_end   the fiscal period the numbers describe   (1999-12-31)
    available_on the date they became public              (2000-03-30)

and lookups are keyed on `available_on`. Ask for MSFT's ROE on 2000-02-01 and
you get the figures from the LAST filing published on or before 2000-02-01 --
never the one that landed in March, however much it describes December.

The gap is not small. A 10-K could land 90 days after the fiscal year in 2000
(the deadline tightened to 60-75 days only in 2003-2006), and a 10-Q 45 days
after the quarter. A backtest keyed on period_end rather than available_on
gets up to three months of free foresight on every name, every quarter.

WHY THIS IS NOT yfinance
------------------------
yfinance's `.info` returns TODAY's ratios and its statements reach back roughly
four or five years. Neither is usable for 2000. Fundamentals must come from a
genuine point-in-time source; see docs/DATA_SOURCES.md for which ones actually
carry a filing-date field and which merely look like they do.

RESTATEMENTS
------------
A point-in-time source keeps what was ORIGINALLY reported, including figures the
company later restated. That is the correct behaviour -- you traded on the
original -- and it is the main thing that distinguishes a real PIT database from
a current-view database with a date column bolted on. `is_restatement` marks
records that supersede an earlier one, so the loader can keep the original.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

FUND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "fundamentals")

# Columns the engine understands. A source adapter maps its own schema onto
# these; anything missing simply reads back as None rather than being guessed.
FIELDS = [
    "revenue", "net_income", "eps_diluted", "ebitda", "operating_income",
    "gross_profit", "total_assets", "total_equity", "total_debt", "cash",
    "operating_cash_flow", "capex", "shares_diluted", "dividends_per_share",
    "book_value_per_share",
]


@dataclass
class FundamentalRecord:
    symbol: str
    period_end: date
    available_on: date          # the filing date -- the only date that matters
    fiscal_period: str = ""     # "Q1".."Q4", "FY"
    is_restatement: bool = False
    values: dict = field(default_factory=dict)

    def get(self, name: str):
        v = self.values.get(name)
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


class FundamentalsStore:
    """Serves the latest filing published on or before a given date."""

    def __init__(self, clock=None, root: str = FUND_DIR):
        self.clock = clock
        self.root = root
        self._by_symbol: dict[str, list[FundamentalRecord]] = {}
        self.loaded: list[str] = []
        self.missing: list[str] = []

    # -- loading ----------------------------------------------------------
    def load_csv(self, path: str) -> int:
        """Load a normalized CSV: symbol, period_end, available_on, ... fields.

        Written by scripts/fetch_fundamentals.py, which does the mapping from
        whichever vendor schema you have.
        """
        if not os.path.exists(path):
            return 0
        df = pd.read_csv(path)
        need = {"symbol", "period_end", "available_on"}
        if not need.issubset({c.lower() for c in df.columns}):
            raise ValueError(f"{path} must contain {sorted(need)}")
        df = df.rename(columns=str.lower)
        df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
        df["available_on"] = pd.to_datetime(df["available_on"]).dt.date
        n = 0
        for sym, grp in df.groupby("symbol"):
            recs = []
            for _, r in grp.sort_values("available_on").iterrows():
                recs.append(FundamentalRecord(
                    symbol=str(sym), period_end=r["period_end"],
                    available_on=r["available_on"],
                    fiscal_period=str(r.get("fiscal_period", "")),
                    is_restatement=bool(r.get("is_restatement", False)),
                    values={f: r.get(f) for f in FIELDS if f in df.columns},
                ))
                n += 1
            self._by_symbol[str(sym)] = recs
            self.loaded.append(str(sym))
        return n

    # -- the point-in-time lookup ----------------------------------------
    def latest(self, symbol: str, as_of: date | None = None,
               keep_original: bool = True) -> FundamentalRecord | None:
        """The most recent filing available on or before `as_of`."""
        as_of = as_of or (self.clock.session_date if self.clock else None)
        if as_of is None:
            raise ValueError("as_of required when the store has no clock")
        recs = self._by_symbol.get(symbol)
        if not recs:
            if symbol not in self.missing:
                self.missing.append(symbol)
            return None
        visible = [r for r in recs if r.available_on <= as_of]
        if keep_original:
            visible = [r for r in visible if not r.is_restatement] or visible
        return visible[-1] if visible else None

    def trailing(self, symbol: str, n: int = 4, as_of: date | None = None) -> list:
        as_of = as_of or (self.clock.session_date if self.clock else None)
        recs = self._by_symbol.get(symbol) or []
        visible = [r for r in recs if r.available_on <= as_of and not r.is_restatement]
        return visible[-n:]

    # -- derived KPIs, all computed only from visible filings -------------
    def kpis(self, symbol: str, price: float | None = None,
             as_of: date | None = None) -> dict:
        """The ratio set, computed from the latest VISIBLE filing.

        Every value is None when its inputs are not available. Nothing is
        estimated or carried forward from a later filing.
        """
        as_of = as_of or (self.clock.session_date if self.clock else None)
        r = self.latest(symbol, as_of)
        if r is None:
            return {}
        ttm = self.trailing(symbol, 4, as_of)

        def s(name):
            vals = [x.get(name) for x in ttm]
            vals = [v for v in vals if v is not None]
            return sum(vals) if len(vals) == 4 else None

        rev_ttm = s("revenue")
        ni_ttm = s("net_income")
        eps_ttm = s("eps_diluted")
        ocf_ttm = s("operating_cash_flow")
        capex_ttm = s("capex")

        equity = r.get("total_equity")
        debt = r.get("total_debt")
        shares = r.get("shares_diluted")
        bvps = r.get("book_value_per_share")
        if bvps is None and equity and shares:
            bvps = equity / shares

        out = {
            "as_of": as_of,
            "period_end": r.period_end,
            "available_on": r.available_on,
            "reporting_lag_days": (r.available_on - r.period_end).days,
            "eps_ttm": eps_ttm,
            "revenue_ttm": rev_ttm,
            "ebitda": r.get("ebitda"),
            "net_margin": (ni_ttm / rev_ttm) if ni_ttm is not None and rev_ttm else None,
            "gross_margin": (r.get("gross_profit") / r.get("revenue"))
                            if r.get("gross_profit") and r.get("revenue") else None,
            "operating_margin": (r.get("operating_income") / r.get("revenue"))
                                if r.get("operating_income") and r.get("revenue") else None,
            "roe": (ni_ttm / equity) if ni_ttm is not None and equity else None,
            "debt_to_equity": (debt / equity) if debt is not None and equity else None,
            "free_cash_flow": (ocf_ttm - abs(capex_ttm))
                              if ocf_ttm is not None and capex_ttm is not None else None,
            "book_value_per_share": bvps,
            "dividends_per_share": r.get("dividends_per_share"),
            "shares_diluted": shares,
        }

        # Revenue growth needs the same quarter a year earlier, and only if
        # that filing was itself already public.
        prior = [x for x in (self._by_symbol.get(symbol) or [])
                 if x.available_on <= as_of and x.fiscal_period == r.fiscal_period
                 and x.period_end < r.period_end]
        if prior and prior[-1].get("revenue") and r.get("revenue"):
            out["revenue_growth_yoy"] = r.get("revenue") / prior[-1].get("revenue") - 1.0

        if price:
            out["price"] = price
            out["market_cap"] = price * shares if shares else None
            out["pe"] = (price / eps_ttm) if eps_ttm and eps_ttm > 0 else None
            out["price_to_book"] = (price / bvps) if bvps and bvps > 0 else None
            dps = r.get("dividends_per_share")
            out["dividend_yield"] = (dps / price) if dps else None
            if dps and eps_ttm and eps_ttm > 0:
                out["payout_ratio"] = dps / eps_ttm
            if out.get("pe") and out.get("revenue_growth_yoy"):
                g = out["revenue_growth_yoy"] * 100
                out["peg"] = out["pe"] / g if g > 0 else None
        return out

    def coverage(self) -> dict:
        return {"symbols_loaded": len(self._by_symbol),
                "records": sum(len(v) for v in self._by_symbol.values()),
                "symbols_missing": sorted(set(self.missing))}
