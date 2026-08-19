"""The tests that matter most: proving the simulation cannot see the future.

Everything else in this repository is a modelling opinion. This file is the
part that is either right or wrong, and if it is wrong every number the engine
produces is worthless.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.clock import ET, LookaheadError, SimClock, sessions_between
from engine.data import PointInTimeFeed


@pytest.fixture
def feed(tmp_path):
    """A tiny, known series: close on day N is exactly 100 + N."""
    days = sessions_between(date(1999, 6, 1), date(2000, 3, 31))
    rows = []
    for i, d in enumerate(days):
        c = 100.0 + i
        rows.append({"date": d, "open": c - 0.5, "high": c + 1.0, "low": c - 1.0,
                     "close": c, "adj_close": c, "volume": 1_000_000 + i})
    df = pd.DataFrame(rows).set_index("date")
    df.to_csv(tmp_path / "TEST.csv")
    clock = SimClock(datetime(2000, 1, 3, 9, 30, tzinfo=ET))
    f = PointInTimeFeed(clock, str(tmp_path))
    assert f.load(["TEST"]) == ["TEST"]
    return f


def test_clock_refuses_to_move_backwards(feed):
    feed.clock.advance_to(datetime(2000, 1, 10, 12, 0, tzinfo=ET))
    with pytest.raises(LookaheadError):
        feed.clock.advance_to(datetime(2000, 1, 10, 11, 59, tzinfo=ET))


def test_todays_daily_bar_is_invisible_during_the_session(feed):
    """The single most common backtest bug: using today's close intraday."""
    feed.clock.advance_to(datetime(2000, 2, 15, 11, 0, tzinfo=ET))
    hist = feed.history("TEST", 500)
    assert len(hist) > 0
    latest = hist.index[-1].date()
    assert latest < date(2000, 2, 15), (
        f"feed exposed a bar for {latest} while the session was still running")


def test_daily_bar_becomes_visible_after_the_close(feed):
    feed.clock.advance_to(datetime(2000, 2, 15, 16, 0, tzinfo=ET))
    hist = feed.history("TEST", 500)
    assert hist.index[-1].date() == date(2000, 2, 15)


def test_no_future_bar_ever_appears_at_any_minute(feed):
    """Walk a session minute by minute and assert the invariant every step."""
    d = date(2000, 3, 1)
    for m in range(0, 391, 7):
        ts = datetime.combine(d, time(9, 30), tzinfo=ET) + timedelta(minutes=m)
        feed.clock.advance_to(ts)
        daily = feed.history("TEST", 500)
        if len(daily):
            assert daily.index[-1].date() < d or m >= 390
        bars = feed.intraday("TEST")
        if len(bars):
            assert bars.index[-1] <= feed.clock.now, "intraday bar from the future"


def test_intraday_is_truncated_at_now(feed):
    d = date(2000, 3, 1)
    feed.clock.advance_to(datetime.combine(d, time(10, 30), tzinfo=ET))
    early = feed.intraday("TEST")
    feed.clock.advance_to(datetime.combine(d, time(14, 30), tzinfo=ET))
    late = feed.intraday("TEST")
    assert len(early) < len(late)
    # What was already printed must not be revised by the passage of time.
    pd.testing.assert_frame_equal(early, late.iloc[: len(early)])


def test_indicators_never_include_the_current_session(feed):
    """A 20-day SMA computed at noon must match one computed from bars that
    ended yesterday -- if it does not, today's close leaked into the signal."""
    feed.clock.advance_to(datetime(2000, 3, 1, 12, 0, tzinfo=ET))
    sma_live = feed.sma("TEST", 20)
    hist = feed.history("TEST", 20)
    assert hist.index[-1].date() < date(2000, 3, 1)
    assert sma_live == pytest.approx(float(hist["close"].tail(20).mean()))


def test_assert_visible_rejects_future_timestamps(feed):
    feed.clock.advance_to(datetime(2000, 3, 1, 12, 0, tzinfo=ET))
    feed.clock.assert_visible(datetime(2000, 3, 1, 11, 59, tzinfo=ET))
    with pytest.raises(LookaheadError):
        feed.clock.assert_visible(datetime(2000, 3, 1, 12, 1, tzinfo=ET))


def test_returns_are_computed_from_completed_bars_only(feed):
    feed.clock.advance_to(datetime(2000, 3, 1, 10, 0, tzinfo=ET))
    r = feed.ret("TEST", 5)
    hist = feed.history("TEST", 10)
    expected = float(hist["adj_close"].iloc[-1] / hist["adj_close"].iloc[-6] - 1)
    assert r == pytest.approx(expected)
    assert hist.index[-1].date() < date(2000, 3, 1)
