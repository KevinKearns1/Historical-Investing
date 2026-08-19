"""Intraday path reconstruction.

WHY THIS EXISTS
---------------
There is no free public source of 1-minute bars for the year 2000. yfinance's
intraday endpoints only reach back 30 days (1m) / 60 days (2m-90m). So a
minute-level 2000 simulation cannot be driven by recorded minute data.

What IS recorded for 2000 is the daily OHLCV print. This module builds a
minute path that is *consistent with* that print: it opens at the true open,
touches the true high and the true low, closes at the true close, and
distributes volume on the U-shaped curve that US equities actually traded in
2000. Between those anchors the path is a seeded Brownian bridge.

WHAT THIS MEANS FOR RESULTS
---------------------------
  * Exact  -- anything keyed off the open, close, high, low, or volume of a
              session, and therefore all daily-resolution signals.
  * Modeled -- the ORDER in which the high and low were hit, and the price at
              any particular minute. A stop and a target that were both inside
              the day's range will resolve according to the modeled path, not
              the real one.

So intraday-path-dependent strategies carry genuine model error. The engine
quantifies it: run with several `--path-seeds` and the report shows the spread
of outcomes across paths. A strategy whose edge collapses when the seed
changes was reading noise, not signal.

The path is seeded from (symbol, date), so a given run is fully reproducible.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")

# Intraday volume profile, 13 half-hour buckets 09:30-16:00. Classic US equity
# U-shape: heavy open, midday lull, heavy close.
_VOLUME_SHAPE = np.array(
    [0.148, 0.098, 0.078, 0.063, 0.055, 0.049, 0.046,
     0.046, 0.050, 0.058, 0.070, 0.098, 0.141]
)
# Volatility follows the same U but less extreme.
_VOL_SHAPE = np.sqrt(_VOLUME_SHAPE / _VOLUME_SHAPE.mean())

_GLOBAL_SEED_OFFSET = 0


def set_path_seed(offset: int) -> None:
    """Shift every synthesized path. Used to test path sensitivity."""
    global _GLOBAL_SEED_OFFSET
    _GLOBAL_SEED_OFFSET = int(offset)


def _seed(symbol: str, d: date) -> int:
    h = hash((symbol, d.toordinal(), _GLOBAL_SEED_OFFSET)) & 0x7FFFFFFF
    return h


def _minute_index(d: date, n: int) -> pd.DatetimeIndex:
    start = datetime.combine(d, time(9, 31), tzinfo=ET)
    return pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])


def synthesize_session(
    symbol: str,
    d: date,
    o: float,
    h: float,
    l: float,
    c: float,
    volume: float,
    prev_close: float,
    half_day: bool = False,
) -> pd.DataFrame:
    """Build a minute path consistent with the day's OHLCV."""
    n = 210 if half_day else 390
    rng = np.random.default_rng(_seed(symbol, d))

    if not np.isfinite([o, h, l, c]).all() or h <= 0 or l <= 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    h = max(h, o, c)
    l = min(l, o, c)

    # 1. Brownian bridge in log space from open to close.
    prof = np.interp(np.linspace(0, len(_VOL_SHAPE) - 1, n),
                     np.arange(len(_VOL_SHAPE)), _VOL_SHAPE)
    steps = rng.standard_normal(n) * prof
    walk = np.cumsum(steps)
    walk -= np.linspace(0, 1, n) * walk[-1]          # pin the endpoint
    span = max(np.log(h / l), 1e-6)
    if walk.std() > 1e-9:
        walk *= (span * 0.42) / walk.std()            # scale to the day's range
    path = np.log(o) + walk + np.linspace(0, np.log(c / o), n)

    # 2. Force the true high and low to be touched, at plausible minutes.
    #    Which comes first is decided by where the close sits in the range:
    #    a strong close more often means the low printed early.
    pos = (c - l) / max(h - l, 1e-9)
    low_first = rng.random() < (0.30 + 0.40 * pos)
    a, b = sorted(rng.choice(np.arange(2, n - 2), size=2, replace=False))
    i_low, i_high = (a, b) if low_first else (b, a)

    path[i_high] = np.log(h)
    path[i_low] = np.log(l)
    px = np.exp(path)
    px = np.clip(px, l, h)
    px[0], px[-1] = o, c
    px[i_high], px[i_low] = h, l

    # 3. Minute OHLC around each close, staying inside the day's range.
    prev = np.concatenate([[o], px[:-1]])
    wig = np.abs(rng.standard_normal(n)) * (px * span / n**0.5) * 0.55
    hi = np.clip(np.maximum(prev, px) + wig, None, h)
    lo = np.clip(np.minimum(prev, px) - wig, l, None)

    # 4. Volume on the U-shape, with noise.
    vprof = np.interp(np.linspace(0, len(_VOLUME_SHAPE) - 1, n),
                      np.arange(len(_VOLUME_SHAPE)), _VOLUME_SHAPE)
    vprof = vprof / vprof.sum()
    vol = vprof * max(volume, 0.0) * (1.0 + 0.25 * rng.standard_normal(n))
    vol = np.clip(vol, 0.0, None)

    return pd.DataFrame(
        {"open": prev, "high": hi, "low": lo, "close": px, "volume": vol},
        index=_minute_index(d, n),
    )
