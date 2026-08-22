"""Engine mechanics: pricing, fills, accounting, period rules."""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.microstructure import CostModel, round_to_tick, uptick_ok
from engine.options import (OptionContract, VolSurface, bsm, expiry_near, greeks,
                            nearest_strike, next_expiries, risk_free_rate,
                            strike_increment, third_friday)
from engine.portfolio import Portfolio
from engine.intraday import synthesize_session


# -- period-correct market rules -----------------------------------------
def test_prices_snap_to_sixteenths_not_pennies():
    """US equities traded in 1/16 increments for all of 2000."""
    assert round_to_tick(50.03) == pytest.approx(50.0)       # nearer 50 than 50 1/16
    assert round_to_tick(50.05) == pytest.approx(50.0625)    # nearer 50 1/16
    assert round_to_tick(50.00) == pytest.approx(50.0)
    assert round_to_tick(99.97) == pytest.approx(100.0)
    # Every rounded price must be an exact multiple of a sixteenth.
    for p in (12.34, 87.91, 145.02, 3.19):
        assert (round_to_tick(p) * 16) == pytest.approx(round(round_to_tick(p) * 16))


def test_third_friday_expiries_are_correct():
    assert third_friday(2000, 1) == date(2000, 1, 21)
    assert third_friday(2000, 3) == date(2000, 3, 17)
    assert third_friday(2000, 12) == date(2000, 12, 15)


def test_only_monthly_expiries_exist_no_weeklys():
    """Weeklys launched in 2005. Every listed expiry in 2000 is a third Friday."""
    for e in next_expiries(date(2000, 4, 3), 6):
        assert e.weekday() == 4
        assert e == third_friday(e.year, e.month)
    # Nothing inside two weeks of a mid-month date should be available.
    e = expiry_near(date(2000, 4, 3), 7)
    assert (e - date(2000, 4, 3)).days >= 14


def test_strike_ladders_match_2000_conventions():
    assert strike_increment(20) == 2.5
    assert strike_increment(50) == 5.0
    assert strike_increment(250) == 10.0
    assert nearest_strike(107.0, 1.0) == 105.0


def test_rates_reflect_2000_levels():
    """Fed funds went 5.50% -> 6.50% across 2000; the curve must show it."""
    assert 0.050 < risk_free_rate(date(2000, 1, 15)) < 0.060
    assert risk_free_rate(date(2000, 7, 1)) > risk_free_rate(date(2000, 1, 15))
    assert risk_free_rate(date(2000, 6, 15)) > 0.060


def test_uptick_rule():
    assert uptick_ok(50.0625, 50.0)          # plus tick, short allowed
    assert not uptick_ok(50.0, 50.0625)      # minus tick, short blocked
    assert uptick_ok(50.0, None)             # no prior print


# -- options pricing ------------------------------------------------------
def test_put_call_parity():
    S, K, T, r, sig = 100.0, 100.0, 0.5, 0.06, 0.40
    c = bsm(S, K, T, r, sig, "call")
    p = bsm(S, K, T, r, sig, "put")
    import math
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)


def test_option_price_is_never_below_intrinsic():
    c = OptionContract("X", "put", 120.0, date(2000, 6, 16))
    assert bsm(80.0, 120.0, 0.01, 0.06, 0.3, "put") >= 0
    assert c.intrinsic(80.0) == 40.0


def test_greeks_have_the_right_signs():
    g = greeks(100, 100, 0.25, 0.06, 0.5, "call")
    assert 0 < g["delta"] < 1 and g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0
    g = greeks(100, 100, 0.25, 0.06, 0.5, "put")
    assert -1 < g["delta"] < 0 and g["gamma"] > 0


def test_put_skew_is_present():
    """2000 index vol carried a pronounced put skew."""
    vs = VolSurface()
    otm_put = vs.iv(0.40, 100, 85, 0.25, "put")
    atm = vs.iv(0.40, 100, 100, 0.25, "put")
    assert otm_put > atm


def test_earnings_premium_dilutes_with_tenor():
    """A single event adds a fixed lump of VARIANCE, so its effect on quoted
    vol must shrink as the tenor grows. A multiplicative bump would not."""
    vs = VolSurface()
    near = vs.iv(0.60, 100, 100, 30 / 365, "call", earnings_in=3)
    far = vs.iv(0.60, 100, 100, 365 / 365, "call", earnings_in=3)
    base_near = vs.iv(0.60, 100, 100, 30 / 365, "call", None)
    base_far = vs.iv(0.60, 100, 100, 365 / 365, "call", None)
    assert (near - base_near) > (far - base_far) > -1e-9


# -- portfolio accounting -------------------------------------------------
def test_round_trip_pnl_and_cash():
    from datetime import datetime
    pf = Portfolio(starting_cash=25_000)
    ts = datetime(2000, 3, 1, 10, 0)
    pf.apply_equity_fill(ts, "ABC", 100, 50.0, 10.0, "S")
    assert pf.cash == pytest.approx(25_000 - 5_000 - 10)
    r = pf.apply_equity_fill(ts, "ABC", -100, 55.0, 10.0, "S")
    assert r == pytest.approx(500.0)
    assert "ABC" not in pf.equities
    assert pf.cash == pytest.approx(25_000 + 500 - 20)


def test_short_position_pnl():
    from datetime import datetime
    pf = Portfolio(starting_cash=25_000)
    ts = datetime(2000, 3, 1, 10, 0)
    pf.apply_equity_fill(ts, "ABC", -100, 50.0, 10.0, "S")
    r = pf.apply_equity_fill(ts, "ABC", 100, 45.0, 10.0, "S")
    assert r == pytest.approx(500.0)


def test_reg_t_leverage_is_2x_not_4x():
    """The 4x day trading buying power arrived with the 2001 rule set. A 2000
    simulation that grants it is trading on rules that did not exist."""
    pf = Portfolio(starting_cash=25_000)
    assert pf.initial_margin == 0.50
    assert pf.buying_power({}) == pytest.approx(50_000)


def test_margin_interest_and_borrow_accrue():
    from datetime import datetime
    pf = Portfolio(starting_cash=10_000)
    pf.apply_equity_fill(datetime(2000, 3, 1, 10, 0), "ABC", 300, 50.0, 10.0, "S")
    before = pf.cash
    pf.accrue_overnight({"ABC": 50.0}, 0.095, 0.035, set(), 0.25, days=1)
    assert pf.cash < before and pf.interest_paid > 0


# -- intraday synthesis ---------------------------------------------------
def test_synthesized_path_honours_the_real_daily_bar():
    p = synthesize_session("T", date(2000, 3, 10), 105.0, 108.5, 103.25, 107.0,
                           32_000_000, 104.0)
    assert len(p) == 390
    assert p["open"].iloc[0] == pytest.approx(105.0)
    assert p["close"].iloc[-1] == pytest.approx(107.0)
    assert p["high"].max() == pytest.approx(108.5)
    assert p["low"].min() == pytest.approx(103.25)
    assert p["volume"].sum() == pytest.approx(32_000_000, rel=0.05)


def test_synthesized_path_is_deterministic():
    a = synthesize_session("T", date(2000, 3, 10), 105, 108.5, 103.25, 107, 3e7, 104)
    b = synthesize_session("T", date(2000, 3, 10), 105, 108.5, 103.25, 107, 3e7, 104)
    assert a["close"].equals(b["close"])


def test_costs_are_2000_sized():
    c = CostModel()
    assert c.equity_commission >= 8.0
    assert c.option_base + c.option_per_contract >= 15.0
    # A 2-contract options round trip must cost meaningfully more than today.
    assert c.option_fees(2, 3.0, False) + c.option_fees(2, 4.0, True) > 35.0


# -- one owner per symbol ------------------------------------------------
class _Ctx:
    """Minimal stand-in: symbol_is_free only ever reads ctx.pf.equities."""
    def __init__(self, pf):
        self.pf = pf


def test_strategy_refuses_a_symbol_another_strategy_holds():
    """The portfolio nets per symbol, as a real account does. Entering
    opposite to another strategy's position would net it to flat, deleting
    that strategy's stop with no error anywhere. Real 2000 data hit this."""
    from strategies.base import Strategy

    pf = Portfolio(starting_cash=25_000.0)
    pf.apply_equity_fill(None, "SPY", 100, 145.0, 10.0, "EQ-05 Downtrend Bounce Short")
    s = Strategy(name="EQ-01 Opening Range Breakout")
    assert s.symbol_is_free(_Ctx(pf), "SPY") is False
    assert s.symbol_is_free(_Ctx(pf), "QQQ") is True


def test_strategy_refuses_a_symbol_it_still_holds_itself():
    """The subtler half, and the one that only appeared on some runs.

    A strategy clears its per-session "already traded" set every morning, but
    a position whose closing order was REJECTED at the bell survives
    overnight. The next day the strategy can signal the opposite way on a name
    it still owns and net ITSELF to flat -- so the guard cannot key on
    ownership. Set iteration order varies between processes, which is why this
    reproduced only intermittently.
    """
    from strategies.base import Strategy

    name = "EQ-01 Opening Range Breakout"
    pf = Portfolio(starting_cash=25_000.0)
    pf.apply_equity_fill(None, "DIA", 50, 110.0, 10.0, name)   # stuck overnight
    s = Strategy(name=name)
    assert s.symbol_is_free(_Ctx(pf), "DIA") is False


def test_opposing_fill_nets_to_flat_and_deletes_the_position():
    """Documents the underlying mechanic the guard exists to prevent."""
    pf = Portfolio(starting_cash=25_000.0)
    pf.apply_equity_fill(None, "DIA", 50, 110.0, 10.0, "A")
    assert "DIA" in pf.equities
    pf.apply_equity_fill(None, "DIA", -50, 111.0, 10.0, "B")
    assert "DIA" not in pf.equities        # gone, with no error raised
