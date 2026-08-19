"""Simulation clock.

Every read of market data in this system is gated on a single mutable
timestamp: `SimClock.now`. Nothing in the engine is allowed to look at a
timestamp greater than `now`. The clock only ever moves forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# NYSE/Nasdaq full-day closures in 1999-2000. Used to build the session list
# without needing a network call to an exchange calendar.
HOLIDAYS_1999_2001 = {
    # 1999
    "1999-01-01", "1999-01-18", "1999-02-15", "1999-04-02", "1999-05-31",
    "1999-07-05", "1999-09-06", "1999-11-25", "1999-12-24",
    # 2000
    "2000-01-17", "2000-02-21", "2000-04-21", "2000-05-29", "2000-07-04",
    "2000-09-04", "2000-11-23", "2000-12-25",
    # 2001 (so a run can roll past year end without a calendar gap)
    "2001-01-01", "2001-01-15", "2001-02-19", "2001-04-13", "2001-05-28",
}

# Sessions that closed early (13:00 ET). Half days change the close auction and
# the "flat by the bell" logic, so the engine needs to know about them.
HALF_DAYS_1999_2001 = {
    "1999-11-26", "2000-07-03", "2000-11-24", "2000-12-26",
}


class LookaheadError(RuntimeError):
    """Raised when any component asks for data at or after the simulated now."""


@dataclass
class SimClock:
    """The single source of truth for 'what time is it in the simulation'."""

    now: datetime
    _high_water: datetime = field(init=False)

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=ET)
        self._high_water = self.now

    def advance_to(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        if ts < self._high_water:
            raise LookaheadError(
                f"clock cannot move backwards: {ts} < {self._high_water}"
            )
        self.now = ts
        self._high_water = ts

    # -- guards -----------------------------------------------------------
    def assert_visible(self, ts: datetime, what: str = "data") -> None:
        """A bar stamped `ts` is only visible once that bar has *completed*.

        Callers pass the bar's closing timestamp. A 09:31 minute bar covers
        09:30:00-09:30:59 and is knowable at 09:31:00, not before.
        """
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        if ts > self.now:
            raise LookaheadError(
                f"{what} stamped {ts} is in the future (sim now = {self.now})"
            )

    @property
    def session_date(self):
        return self.now.astimezone(ET).date()

    def is_open(self) -> bool:
        local = self.now.astimezone(ET)
        d = local.date().isoformat()
        if d in HOLIDAYS_1999_2001 or local.weekday() >= 5:
            return False
        close = time(13, 0) if d in HALF_DAYS_1999_2001 else MARKET_CLOSE
        return MARKET_OPEN <= local.time() < close

    def __repr__(self) -> str:
        return f"<SimClock {self.now.astimezone(ET):%Y-%m-%d %H:%M:%S %Z}>"


def session_close_time(d) -> time:
    return time(13, 0) if d.isoformat() in HALF_DAYS_1999_2001 else MARKET_CLOSE


def is_session(d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS_1999_2001


def sessions_between(start, end) -> list:
    """Inclusive list of trading dates between two `date` objects."""
    out, d = [], start
    while d <= end:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out
