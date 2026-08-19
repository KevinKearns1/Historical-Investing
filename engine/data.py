"""Point-in-time market data.

The contract: given a SimClock, this feed will never hand back a value that
would not have been printed on the tape by `clock.now`. That is enforced in
one place -- `_visible_slice` -- so there is no way for a strategy to route
around it.

Daily bars come from yfinance and are cached to disk (scripts/fetch_data.py),
so a backtest run needs no network access at all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from engine.clock import ET, SimClock, LookaheadError, session_close_time
from engine.intraday import synthesize_session
from engine.intraday_source import IntradaySource

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache")

# Columns we keep. `adj_close` is used ONLY for computing returns/indicators;
# every fill price uses the raw, unadjusted print, because in 2000 you traded
# the actual price, not a split-adjusted one.
COLS = ["open", "high", "low", "close", "adj_close", "volume"]


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


class PointInTimeFeed:
    """Serves daily and (synthesized) intraday bars, clamped to the sim clock."""

    def __init__(self, clock: SimClock, cache_dir: str = CACHE_DIR,
                 intraday_source: IntradaySource | None = None):
        self.clock = clock
        self.cache_dir = cache_dir
        self._daily: dict[str, pd.DataFrame] = {}
        self._intraday_cache: dict[tuple[str, date], pd.DataFrame] = {}
        self.intraday_source = intraday_source or IntradaySource()

    # -- loading ----------------------------------------------------------
    def load(self, symbols: Iterable[str]) -> list[str]:
        """Load whatever is cached. Returns the symbols that were found."""
        found = []
        for sym in symbols:
            path = os.path.join(self.cache_dir, f"{sym.replace('^','_idx_')}.csv")
            if not os.path.exists(path):
                continue
            df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
            df = df[[c for c in COLS if c in df.columns]].astype(float)
            self._daily[sym] = df
            found.append(sym)
        return found

    def symbols(self) -> list[str]:
        return sorted(self._daily)

    # -- the one and only visibility gate ---------------------------------
    def _visible_slice(self, sym: str) -> pd.DataFrame:
        """Daily bars that have fully printed as of clock.now.

        A daily bar for date D is complete at D's closing bell. During the
        session on D, that day's bar does NOT exist yet -- only D-1 and older.
        """
        df = self._daily.get(sym)
        if df is None:
            raise KeyError(f"{sym} not loaded (run scripts/fetch_data.py)")
        local = self.clock.now.astimezone(ET)
        today = local.date()
        cutoff = today
        if local.time() < session_close_time(today):
            # Intraday, or premarket: today's daily bar is not knowable yet.
            cutoff = today - timedelta(days=1)
        return df.loc[df.index.date <= cutoff]

    # -- public reads -----------------------------------------------------
    def history(self, sym: str, lookback: int = 250) -> pd.DataFrame:
        """The last `lookback` COMPLETED daily bars. Safe by construction."""
        return self._visible_slice(sym).tail(lookback)

    def last_close(self, sym: str) -> float | None:
        h = self._visible_slice(sym)
        return float(h["close"].iloc[-1]) if len(h) else None

    def prior_bar(self, sym: str) -> Bar | None:
        h = self._visible_slice(sym)
        if not len(h):
            return None
        r = h.iloc[-1]
        ts = datetime.combine(h.index[-1].date(), session_close_time(h.index[-1].date()))
        return Bar(ts.replace(tzinfo=ET), r.open, r.high, r.low, r.close, r.volume)

    def today_daily_raw(self, sym: str) -> pd.Series | None:
        """Today's OHLCV. PRIVATE to the engine -- used to build the intraday
        path and to fill orders. Strategies must never call this; it is the
        one place where the day's shape is known, and the intraday layer only
        ever reveals it minute by minute."""
        df = self._daily.get(sym)
        if df is None:
            return None
        d = self.clock.now.astimezone(ET).date()
        hit = df.loc[df.index.date == d]
        return hit.iloc[0] if len(hit) else None

    # -- intraday ---------------------------------------------------------
    def _session_path(self, sym: str, d: date) -> pd.DataFrame | None:
        key = (sym, d)
        if key in self._intraday_cache:
            return self._intraday_cache[key]
        df = self._daily.get(sym)
        if df is None:
            return None
        hit = df.loc[df.index.date == d]
        if not len(hit):
            return None
        row = hit.iloc[0]
        prev = df.loc[df.index.date < d]
        prev_close = float(prev["close"].iloc[-1]) if len(prev) else float(row.open)
        # Recorded bars if the symbol has validated intraday data on disk,
        # reconstruction from the real daily bar otherwise. The source tags
        # every bar so the report can state what the run actually rested on.
        path, _src = self.intraday_source.session(sym, d, row, prev_close)
        if path is None:
            path = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        self._intraday_cache[key] = path
        if len(self._intraday_cache) > 4000:
            self._intraday_cache.pop(next(iter(self._intraday_cache)))
        return path

    def intraday(self, sym: str, d: date | None = None) -> pd.DataFrame:
        """Minute bars for the session, truncated at clock.now.

        Bar stamped 09:31 covers 09:30:00-09:30:59 and is visible at 09:31.
        """
        local = self.clock.now.astimezone(ET)
        d = d or local.date()
        path = self._session_path(sym, d)
        if path is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if d < local.date():
            return path
        return path.loc[path.index <= local]

    def last_price(self, sym: str) -> float | None:
        """Best estimate of the current print."""
        bars = self.intraday(sym)
        if len(bars):
            return float(bars["close"].iloc[-1])
        return self.last_close(sym)

    def session_vwap(self, sym: str) -> float | None:
        bars = self.intraday(sym)
        if not len(bars):
            return None
        tp = (bars["high"] + bars["low"] + bars["close"]) / 3.0
        v = bars["volume"].values
        return float((tp.values * v).sum() / max(v.sum(), 1e-9))

    # -- derived, all point-in-time --------------------------------------
    def realized_vol(self, sym: str, window: int = 21) -> float | None:
        h = self.history(sym, window + 2)
        if len(h) < window + 1:
            return None
        r = np.log(h["close"] / h["close"].shift(1)).dropna()
        if len(r) < window:
            return None
        return float(r.tail(window).std(ddof=1) * np.sqrt(252))

    def sma(self, sym: str, window: int) -> float | None:
        h = self.history(sym, window + 1)
        if len(h) < window:
            return None
        return float(h["close"].tail(window).mean())

    def atr(self, sym: str, window: int = 14) -> float | None:
        h = self.history(sym, window + 2)
        if len(h) < window + 1:
            return None
        pc = h["close"].shift(1)
        tr = pd.concat([h["high"] - h["low"], (h["high"] - pc).abs(),
                        (h["low"] - pc).abs()], axis=1).max(axis=1).dropna()
        return float(tr.tail(window).mean())

    def adv(self, sym: str, window: int = 20) -> float | None:
        h = self.history(sym, window + 1)
        if len(h) < 5:
            return None
        return float(h["volume"].tail(window).mean())

    def opening_volume_ratio(self, sym: str, minutes: int = 30, lookback: int = 20) -> float | None:
        """Today's opening-N-minute volume over its recent median.

        CAVEAT, and it is a real one: because the intraday path is synthesized
        with a fixed U-shaped volume profile, the opening share of a session is
        a constant fraction of that session's total. This ratio therefore
        reduces to "today's volume against a typical day" rather than measuring
        genuine opening-bell urgency. It is a real signal, but a coarser one
        than the same filter computed on recorded minute data would be. This is
        one of the signals most degraded by path synthesis -- see
        engine/intraday.py.
        """
        bars = self.intraday(sym)
        if len(bars) < minutes:
            return None
        today_open_vol = float(bars["volume"].iloc[:minutes].sum())
        hist = self.history(sym, lookback + 1)
        if len(hist) < 10:
            return None
        share = 0.246 if minutes == 30 else 0.148 if minutes <= 15 else 0.30
        typical = float(hist["volume"].tail(lookback).median()) * share
        if typical <= 0:
            return None
        return today_open_vol / typical

    def ret(self, sym: str, days: int) -> float | None:
        h = self.history(sym, days + 2)
        if len(h) < days + 1:
            return None
        a, b = h["adj_close"].iloc[-days - 1], h["adj_close"].iloc[-1]
        return float(b / a - 1.0) if a > 0 else None
