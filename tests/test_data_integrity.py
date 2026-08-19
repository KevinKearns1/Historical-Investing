"""Tests for the parts that decide whether the inputs can be trusted at all.

The centrepiece is test_rejects_the_real_broker_gapfill: a regression test built
from data an actual connected API returned when asked for January 2000.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.era import (Era, commissions, leverage_rules, min_tick, option_tick,
                        uptick_rule_applies, weeklys_available)
from engine.fundamentals import FundamentalsStore
from engine.provenance import (ProvenanceLedger, Source, drop_interpolated,
                               validate_bars)
from strategies.filters import QualityScreen, SOLVENCY


# ---------------------------------------------------------------- provenance
def _flat_gapfill(n=780, px=484.52):
    """Exactly what the broker API returned for MSFT, January 2000."""
    return pd.DataFrame({
        "open": [px] * n, "high": [px] * n, "low": [px] * n, "close": [px] * n,
        "volume": [0] * n, "interpolated": [True] * n,
    })


def _realistic(n=780, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.002))
    return pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998,
                         "close": c, "volume": rng.integers(1e4, 1e5, n)})


def test_rejects_the_real_broker_gapfill():
    """REGRESSION. A connected API returned 780 fully-populated 5-minute bars
    for Jan 2000, all interpolated, zero volume, one single price. Accepting it
    would have produced a backtest on a constant price."""
    rep = validate_bars(_flat_gapfill(), "MSFT", "5minute")
    assert not rep.usable
    assert rep.n_flagged_interpolated == 780
    assert rep.distinct_closes == 1
    # Caught independently, so removing any one check still fails it.
    assert len(rep.problems) >= 3


def test_accepts_realistic_bars():
    assert validate_bars(_realistic(), "MSFT", "5minute").usable


def test_rejects_flat_line_even_without_the_vendor_flag():
    """The vendor flag is a courtesy, not a guarantee. Strip it and the shape
    of the data must still give it away."""
    df = _flat_gapfill().drop(columns=["interpolated"])
    assert not validate_bars(df, "MSFT", "5minute").usable


def test_rejects_zero_volume_series():
    df = _realistic()
    df["volume"] = 0
    assert not validate_bars(df, "X", "1min").usable


def test_rejects_incoherent_ohlc():
    df = _realistic()
    df.loc[df.index[:100], "high"] = df["low"] - 1.0
    assert not validate_bars(df, "X", "1min").usable


def test_rejects_empty_series():
    assert not validate_bars(pd.DataFrame(), "X", "1min").usable


def test_drop_interpolated_removes_gapfill():
    assert len(drop_interpolated(_flat_gapfill())) == 0


def test_ledger_reports_real_fraction():
    led = ProvenanceLedger()
    led.note("A", Source.REAL, 300)
    led.note("B", Source.SYNTHESIZED, 100)
    assert led.real_fraction == pytest.approx(0.75)
    assert led.summary()["symbols_with_real_intraday"] == ["A"]


# ---------------------------------------------------------------------- era
def test_tick_size_follows_decimalization():
    assert min_tick(date(2000, 6, 1), 50.0) == pytest.approx(1 / 16)
    assert min_tick(date(2002, 6, 1), 50.0) == pytest.approx(0.01)
    assert min_tick(date(1995, 6, 1), 50.0) == pytest.approx(1 / 8)


def test_uptick_rule_lifetime():
    assert uptick_rule_applies(date(2000, 6, 1))
    assert uptick_rule_applies(date(2007, 7, 5))
    assert not uptick_rule_applies(date(2007, 7, 6))       # repealed
    # Rule 201 circuit breaker, post-2010, only after a 10% decline.
    assert not uptick_rule_applies(date(2015, 6, 1), -0.05)
    assert uptick_rule_applies(date(2015, 6, 1), -0.12)


def test_pdt_and_leverage_arrive_together_in_2001():
    """The $25k minimum and the 4x buying power came from the same rule set,
    effective 2001-09-28. A 2000 backtest must have neither."""
    before = leverage_rules(date(2000, 6, 1), 25_000)
    assert not before.pdt_applies and before.day_trade_buying_power == 2.0
    after = leverage_rules(date(2002, 6, 1), 25_000)
    assert after.pdt_applies and after.day_trade_buying_power == 4.0
    small = leverage_rules(date(2002, 6, 1), 10_000)
    assert small.pdt_applies and small.day_trade_buying_power == 2.0


def test_weeklys_did_not_exist_in_2000():
    assert not weeklys_available(date(2000, 6, 1))
    assert not weeklys_available(date(2004, 6, 1))
    assert weeklys_available(date(2011, 6, 1))


def test_option_tick_follows_penny_pilot():
    assert option_tick(date(2000, 6, 1), 2.0) == pytest.approx(1 / 16)
    assert option_tick(date(2010, 6, 1), 2.0) == pytest.approx(0.01)


def test_commissions_fall_over_time_and_reach_zero():
    assert commissions(date(2000, 6, 1))[0] > commissions(date(2010, 6, 1))[0]
    assert commissions(date(2021, 6, 1))[0] == 0.0


def test_era_snapshot_is_coherent():
    e = Era.on(date(2000, 6, 1), 50.0, 25_000)
    assert e.uptick_rule and not e.weeklys and e.day_trade_bp == 2.0
    assert e.tick == pytest.approx(1 / 16)


# -------------------------------------------------------------- fundamentals
@pytest.fixture
def store(tmp_path):
    """Q4 1999 results, filed 2000-03-30. Knowable in April, not in January."""
    rows = [
        {"symbol": "ABC", "period_end": "1999-09-30", "available_on": "1999-11-12",
         "fiscal_period": "Q3", "revenue": 900, "net_income": 90, "eps_diluted": 0.90,
         "total_equity": 1000, "total_debt": 500, "shares_diluted": 100,
         "operating_cash_flow": 120, "capex": 30},
        {"symbol": "ABC", "period_end": "1999-12-31", "available_on": "2000-03-30",
         "fiscal_period": "Q4", "revenue": 1000, "net_income": 100, "eps_diluted": 1.00,
         "total_equity": 1100, "total_debt": 500, "shares_diluted": 100,
         "operating_cash_flow": 140, "capex": 40},
    ]
    p = tmp_path / "f.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    s = FundamentalsStore()
    s.load_csv(str(p))
    return s


def test_filing_is_invisible_before_it_was_filed(store):
    """The core point-in-time guarantee for fundamentals."""
    r = store.latest("ABC", date(2000, 1, 15))
    assert r is not None
    assert r.period_end == date(1999, 9, 30), (
        "Q4 1999 results were used on 2000-01-15 but were not filed until March")


def test_filing_becomes_visible_on_its_filing_date(store):
    assert store.latest("ABC", date(2000, 3, 29)).period_end == date(1999, 9, 30)
    assert store.latest("ABC", date(2000, 3, 30)).period_end == date(1999, 12, 31)


def test_reporting_lag_is_exposed(store):
    k = store.kpis("ABC", price=20.0, as_of=date(2000, 4, 1))
    assert k["reporting_lag_days"] == 90


def test_kpis_compute_from_visible_filings_only(store):
    k = store.kpis("ABC", price=20.0, as_of=date(2000, 4, 1))
    assert k["debt_to_equity"] == pytest.approx(500 / 1100)
    assert k["book_value_per_share"] == pytest.approx(11.0)
    assert k["price_to_book"] == pytest.approx(20.0 / 11.0)


def test_unknown_symbol_returns_empty_not_an_error(store):
    assert store.kpis("NOPE", price=10.0, as_of=date(2000, 4, 1)) == {}


# ------------------------------------------------------------------ screens
def test_screen_passes_when_there_is_no_data():
    """Unknown must not mean fail, or the screen becomes a filter on data
    coverage and reintroduces survivorship bias."""
    r = SOLVENCY.check({})
    assert r.passed and not r.screened


def test_screen_rejects_on_real_evidence():
    r = SOLVENCY.check({"eps_ttm": -1.5, "debt_to_equity": 0.4, "net_margin": -0.2,
                        "free_cash_flow": -100})
    assert not r.passed and r.screened and len(r.reasons) >= 2


def test_screen_only_judges_fields_it_was_given():
    r = QualityScreen(max_debt_to_equity=1.0, require_positive_eps=False,
                      min_net_margin=None).check({"debt_to_equity": 0.5})
    assert r.passed and r.screened
