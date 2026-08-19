"""Where every number came from, and whether it can be trusted.

MOTIVATION -- A REAL INCIDENT, NOT A HYPOTHETICAL
--------------------------------------------------
While building this, a connected broker data API was asked for 5-minute MSFT
bars covering January 2000. It returned 780 bars. Every field was populated.
Nothing errored. It looked exactly like data.

All 780 were flagged `interpolated: true`, every volume was 0, and all 780 bars
carried the SAME price -- 484.52 -- because the provider has no history before
2013 and silently gap-fills a flat line across anything earlier.

Ingested without checking, that would have produced a backtest on a constant
price: no volatility, no ranges, every mean-reversion signal dead, every
breakout signal dead, and a tidy equity curve that meant nothing whatsoever.
The failure is silent and total, and it is exactly the failure a "we were
there" simulation is most vulnerable to.

So no bar enters this engine without a provenance tag, nothing that fails
validation is used, and every report states what fraction of the decisions
rested on recorded data versus reconstruction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd


class Source(str, Enum):
    REAL = "real"                  # recorded from the tape
    SYNTHESIZED = "synthesized"    # reconstructed from a real daily bar
    INTERPOLATED = "interpolated"  # vendor gap-fill: carries NO information
    UNKNOWN = "unknown"


class DataQualityError(ValueError):
    """Raised when a series fails validation badly enough to be unusable."""


@dataclass
class QualityReport:
    symbol: str
    interval: str
    n_bars: int = 0
    n_zero_volume: int = 0
    n_flagged_interpolated: int = 0
    distinct_closes: int = 0
    flat_runs: int = 0
    zero_range_bars: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        head = f"{self.symbol} [{self.interval}] {self.n_bars} bars"
        if self.usable:
            return f"{head}: OK"
        return f"{head}: REJECTED -- " + "; ".join(self.problems)


def validate_bars(df: pd.DataFrame, symbol: str, interval: str,
                  max_zero_volume_frac: float = 0.35,
                  max_flat_frac: float = 0.60,
                  min_distinct_closes: int = 10) -> QualityReport:
    """Check a bar series for the failure modes that silently ruin a backtest.

    Deliberately strict. A false rejection costs a download; a false acceptance
    costs every conclusion drawn from the run.
    """
    rep = QualityReport(symbol=symbol, interval=interval, n_bars=len(df))
    if len(df) == 0:
        rep.problems.append("empty series")
        return rep

    cols = {c.lower() for c in df.columns}
    for need in ("open", "high", "low", "close"):
        if need not in cols:
            rep.problems.append(f"missing column '{need}'")
    if rep.problems:
        return rep

    d = df.rename(columns=str.lower)

    # 1. Vendor's own interpolation flag, if the adapter preserved it.
    if "interpolated" in cols:
        rep.n_flagged_interpolated = int(d["interpolated"].astype(bool).sum())
        frac = rep.n_flagged_interpolated / len(d)
        if frac > 0.02:
            rep.problems.append(
                f"{frac:.0%} of bars flagged interpolated by the vendor")

    # 2. Zero volume. Real bars in liquid names essentially always trade.
    if "volume" in cols:
        rep.n_zero_volume = int((d["volume"].fillna(0) <= 0).sum())
        frac = rep.n_zero_volume / len(d)
        if frac > max_zero_volume_frac:
            rep.problems.append(f"{frac:.0%} of bars have zero volume")

    # 3. Degenerate price variation -- the flat-line signature.
    rep.distinct_closes = int(d["close"].nunique())
    if rep.distinct_closes < min(min_distinct_closes, len(d)):
        rep.problems.append(
            f"only {rep.distinct_closes} distinct closes across {len(d)} bars")

    # 4. Bars where open == high == low == close: no range at all.
    zero_range = ((d["high"] - d["low"]).abs() < 1e-12)
    rep.zero_range_bars = int(zero_range.sum())
    if zero_range.mean() > max_flat_frac:
        rep.problems.append(f"{zero_range.mean():.0%} of bars have zero range")

    # 5. Long unchanged runs, which gap-fill produces and a real tape does not.
    same = (d["close"].diff().abs() < 1e-12).astype(int)
    if len(same):
        runs, cur = [], 0
        for v in same.values:
            cur = cur + 1 if v else 0
            runs.append(cur)
        rep.flat_runs = int(max(runs)) if runs else 0
        if rep.flat_runs > max(20, 0.25 * len(d)):
            rep.problems.append(
                f"longest unchanged run is {rep.flat_runs} bars")

    # 6. Basic OHLC coherence.
    bad = ((d["high"] < d["low"]) | (d["high"] < d["open"]) |
           (d["high"] < d["close"]) | (d["low"] > d["open"]) |
           (d["low"] > d["close"])).sum()
    if bad:
        rep.problems.append(f"{int(bad)} bars violate OHLC ordering")

    # 7. Non-positive prices.
    if (d[["open", "high", "low", "close"]] <= 0).any().any():
        rep.problems.append("non-positive prices present")

    return rep


def drop_interpolated(df: pd.DataFrame) -> pd.DataFrame:
    """Remove vendor gap-fill rows. They carry no information by definition."""
    if "interpolated" not in {c.lower() for c in df.columns}:
        return df
    d = df.rename(columns=str.lower)
    return df.loc[~d["interpolated"].astype(bool)]


@dataclass
class ProvenanceLedger:
    """Counts how the engine's decisions were actually informed."""

    bars_by_source: dict = field(default_factory=dict)
    symbols_real: set = field(default_factory=set)
    symbols_synth: set = field(default_factory=set)
    rejected: list = field(default_factory=list)

    def note(self, symbol: str, source: Source, n: int = 1) -> None:
        self.bars_by_source[source.value] = self.bars_by_source.get(source.value, 0) + n
        (self.symbols_real if source is Source.REAL else self.symbols_synth).add(symbol)

    def note_rejected(self, rep: QualityReport) -> None:
        self.rejected.append(rep.summary())

    @property
    def real_fraction(self) -> float:
        total = sum(self.bars_by_source.values())
        return self.bars_by_source.get(Source.REAL.value, 0) / total if total else 0.0

    def summary(self) -> dict:
        return {
            "bars_by_source": dict(self.bars_by_source),
            "real_bar_fraction": round(self.real_fraction, 4),
            "symbols_with_real_intraday": sorted(self.symbols_real),
            "symbols_on_synthesized_intraday": sorted(self.symbols_synth),
            "rejected_series": self.rejected,
        }
