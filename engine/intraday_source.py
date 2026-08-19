"""Intraday bars: recorded if you have them, reconstructed if you do not.

Resolution order for a given (symbol, date):
    1. A validated REAL bar file in data/intraday/<symbol>/<YYYY-MM-DD>.csv
       (or a per-symbol parquet covering a range). Used as-is.
    2. Reconstruction from the real daily OHLCV bar.
    3. Nothing -- the symbol is skipped for that session.

Real bars are validated before use (engine/provenance.py) and REJECTED if they
show the vendor gap-fill signature. A rejected series falls through to
reconstruction rather than poisoning the run.

Bars are resampled to whatever `interval_minutes` the run asks for, so a
2-minute simulation works off 1-minute source data without a separate download.
"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd

from engine.provenance import (ProvenanceLedger, Source, drop_interpolated,
                               validate_bars)
from engine.intraday import synthesize_session

INTRADAY_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "intraday")
_STD = ["open", "high", "low", "close", "volume"]


def _resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes <= 1:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(f"{minutes}min", label="right", closed="left").agg(agg).dropna(how="any")
    return out


class IntradaySource:
    """Serves one session's minute path, from disk or from reconstruction."""

    def __init__(self, root: str = INTRADAY_DIR, interval_minutes: int = 1,
                 ledger: ProvenanceLedger | None = None, require_real: bool = False):
        self.root = root
        self.interval = max(int(interval_minutes), 1)
        self.ledger = ledger or ProvenanceLedger()
        self.require_real = require_real
        self._files: dict[str, pd.DataFrame] = {}
        self._checked: set[str] = set()

    # -- real bars --------------------------------------------------------
    def _load_symbol_file(self, symbol: str) -> pd.DataFrame | None:
        """A single parquet/csv per symbol covering many sessions."""
        if symbol in self._files:
            return self._files[symbol]
        if symbol in self._checked:
            return None
        self._checked.add(symbol)
        safe = symbol.replace("^", "_idx_")
        for ext, reader in (("parquet", pd.read_parquet), ("csv", pd.read_csv)):
            path = os.path.join(self.root, f"{safe}.{ext}")
            if not os.path.exists(path):
                continue
            df = reader(path)
            tcol = next((c for c in df.columns if c.lower() in
                         ("timestamp", "datetime", "date", "begins_at", "time")), None)
            if tcol is None:
                return None
            df[tcol] = pd.to_datetime(df[tcol], utc=True, errors="coerce")
            df = df.dropna(subset=[tcol]).set_index(tcol).sort_index()
            df.index = df.index.tz_convert("America/New_York")
            df = df.rename(columns=str.lower)

            rep = validate_bars(df, symbol, "intraday")
            if not rep.usable:
                self.ledger.note_rejected(rep)
                return None
            df = drop_interpolated(df)
            keep = [c for c in _STD if c in df.columns]
            if len(keep) < 4:
                return None
            if "volume" not in df.columns:
                df["volume"] = 0.0
            self._files[symbol] = df[_STD]
            return self._files[symbol]
        return None

    def real_session(self, symbol: str, d: date) -> pd.DataFrame | None:
        df = self._load_symbol_file(symbol)
        if df is None:
            return None
        day = df.loc[df.index.date == d]
        if len(day) < 20:            # too sparse to drive an intraday strategy
            return None
        return _resample(day, self.interval)

    # -- resolution -------------------------------------------------------
    def session(self, symbol: str, d: date, daily_row, prev_close: float,
                half_day: bool = False) -> tuple[pd.DataFrame | None, Source]:
        real = self.real_session(symbol, d)
        if real is not None and len(real):
            self.ledger.note(symbol, Source.REAL, len(real))
            return real, Source.REAL

        if self.require_real:
            return None, Source.UNKNOWN

        if daily_row is None:
            return None, Source.UNKNOWN
        path = synthesize_session(
            symbol, d, float(daily_row["open"]), float(daily_row["high"]),
            float(daily_row["low"]), float(daily_row["close"]),
            float(daily_row.get("volume", 0.0)), prev_close, half_day)
        if not len(path):
            return None, Source.UNKNOWN
        path = _resample(path, self.interval)
        self.ledger.note(symbol, Source.SYNTHESIZED, len(path))
        return path, Source.SYNTHESIZED
