"""Options pricing for a year-2000 simulation.

WHY MODELLED AND NOT RECORDED
-----------------------------
yfinance exposes only the *current* option chain. No historical option quotes
exist in it at any date, let alone 2000. So every option price here is a model
price: Black-Scholes-Merton on the real, point-in-time underlying, with an
implied-vol surface built from point-in-time realized vol.

That is a genuine limitation and it cuts one way: model prices are smoother
and fairer than the real 2000 tape, where a wide market and a slow fill were
often the whole trade. The cost model deliberately overstates spreads to lean
against that.

PERIOD DETAILS THAT MATTER
--------------------------
  * NO WEEKLY OPTIONS. Weeklys launched in 2005. In 2000 the only listed
    expiries were the monthly third-Friday cycle (plus LEAPS). A strategy that
    quietly assumes a 7-day option is not a 2000 strategy.
  * Strike ladders: $2.50 under $25, $5.00 from $25-$200, $10 above $200.
  * Equity options are American -> early assignment is modelled, notably on
    short calls into an ex-dividend date.
  * Expiry: last trading day is the third Friday; settlement was Saturday.
  * Rates were HIGH. Fed funds went 5.50% -> 6.50% across 2000. Carry is not
    negligible at those levels, unlike in a zero-rate era.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from scipy.stats import norm

# Actual 3-month T-bill levels through 2000, used as the risk-free curve.
# Fed funds target: 5.50% -> 5.75% (Feb 2) -> 6.00% (Mar 21) -> 6.50% (May 16),
# held at 6.50% for the rest of the year.
_RF_CURVE = [
    (date(1999, 1, 1), 0.0450), (date(1999, 7, 1), 0.0475),
    (date(1999, 11, 1), 0.0520), (date(2000, 1, 1), 0.0532),
    (date(2000, 2, 1), 0.0560), (date(2000, 3, 1), 0.0578),
    (date(2000, 4, 1), 0.0588), (date(2000, 5, 1), 0.0605),
    (date(2000, 6, 1), 0.0640), (date(2000, 7, 1), 0.0602),
    (date(2000, 8, 1), 0.0615), (date(2000, 9, 1), 0.0620),
    (date(2000, 10, 1), 0.0618), (date(2000, 11, 1), 0.0620),
    (date(2000, 12, 1), 0.0592), (date(2001, 1, 1), 0.0529),
]


def risk_free_rate(d: date) -> float:
    prev = _RF_CURVE[0][1]
    for dt, r in _RF_CURVE:
        if d < dt:
            return prev
        prev = r
    return prev


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    fridays = [d + timedelta(days=i) for i in range(31)
               if (d + timedelta(days=i)).month == month
               and (d + timedelta(days=i)).weekday() == 4]
    return fridays[2]


def next_expiries(today: date, count: int = 4) -> list[date]:
    """Listed monthly expiries on or after `today`. No weeklys in 2000."""
    out, y, m = [], today.year, today.month
    while len(out) < count:
        e = third_friday(y, m)
        if e >= today:
            out.append(e)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def expiry_near(today: date, target_dte: int) -> date:
    """The listed expiry closest to a wanted days-to-expiry."""
    cands = next_expiries(today, 6)
    return min(cands, key=lambda e: abs((e - today).days - target_dte))


def strike_increment(price: float) -> float:
    if price < 25:
        return 2.5
    if price <= 200:
        return 5.0
    return 10.0


def strike_ladder(price: float, n: int = 12) -> list[float]:
    inc = strike_increment(price)
    atm = round(price / inc) * inc
    return [round(atm + i * inc, 2) for i in range(-n, n + 1) if atm + i * inc > 0]


def nearest_strike(price: float, moneyness: float = 1.0) -> float:
    target = price * moneyness
    inc = strike_increment(price)
    return round(round(target / inc) * inc, 2)


# -- Black-Scholes-Merton -------------------------------------------------
def bsm(S: float, K: float, T: float, r: float, sigma: float,
        kind: str = "call", q: float = 0.0) -> float:
    """Price with continuous dividend yield q. T in years."""
    if T <= 1e-9 or sigma <= 1e-9 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if kind == "call" else (K - S))
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, sigma: float,
           kind: str = "call", q: float = 0.0) -> dict:
    if T <= 1e-9 or sigma <= 1e-9:
        intrinsic_delta = (1.0 if S > K else 0.0) if kind == "call" else (-1.0 if S < K else 0.0)
        return {"delta": intrinsic_delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    pdf = norm.pdf(d1)
    delta = math.exp(-q * T) * (norm.cdf(d1) if kind == "call" else norm.cdf(d1) - 1)
    gamma = math.exp(-q * T) * pdf / (S * sigma * sq)
    vega = S * math.exp(-q * T) * pdf * sq / 100.0
    if kind == "call":
        theta = (-S * pdf * sigma * math.exp(-q * T) / (2 * sq)
                 - r * K * math.exp(-r * T) * norm.cdf(d2)
                 + q * S * math.exp(-q * T) * norm.cdf(d1)) / 365.0
    else:
        theta = (-S * pdf * sigma * math.exp(-q * T) / (2 * sq)
                 + r * K * math.exp(-r * T) * norm.cdf(-d2)
                 - q * S * math.exp(-q * T) * norm.cdf(-d1)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def delta_strike(S: float, T: float, r: float, sigma: float, target_delta: float,
                 kind: str = "put", q: float = 0.0) -> float:
    """Listed strike whose delta is closest to `target_delta` (unsigned)."""
    best, best_err = None, 1e9
    for K in strike_ladder(S, 14):
        g = greeks(S, K, T, r, sigma, kind, q)
        err = abs(abs(g["delta"]) - abs(target_delta))
        if err < best_err:
            best, best_err = K, err
    return best if best is not None else nearest_strike(S)


@dataclass
class VolSurface:
    """Implied vol from point-in-time realized vol, plus the shape features the
    2000 market actually showed: a rich put skew, a term structure that
    inverted when spot fell, and an event bump into earnings."""

    vol_risk_premium: float = 1.08   # IV over realized on average
    skew_per_10pct: float = 0.055    # IV added per 10% below spot
    call_skew_relief: float = 0.020  # IV subtracted per 10% above spot
    min_iv: float = 0.15
    max_iv: float = 2.50
    # How large a one-day earnings move the market prices, as a multiple of the
    # stock's ordinary daily move. Empirically ~2x for a liquid large cap.
    #
    # THIS IS THE SINGLE MOST IMPORTANT FREE PARAMETER IN THE OPTIONS SLEEVE.
    # It alone decides whether OP-04 ever sees "cheap" earnings vol. Raise it
    # and OP-04 never trades; lower it and OP-04 trades constantly and wins on
    # an assumption rather than on a signal. It is set to a defensible value,
    # not a tuned one, and any OP-04 result should be read with that in mind.
    event_move_multiple: float = 2.2

    def iv(self, realized_vol: float, S: float, K: float, T: float,
           kind: str = "call", earnings_in: int | None = None,
           regime_stress: float = 0.0) -> float:
        base = max(realized_vol, 0.05) * self.vol_risk_premium

        # Term structure: short-dated richer, and much richer under stress.
        if T < 0.12:
            base *= 1.0 + (0.12 - T) * (0.9 + 2.5 * regime_stress)
        elif T > 0.5:
            base *= 0.95

        # Skew, in moneyness terms.
        m = (K / S) - 1.0
        if m < 0:
            base += self.skew_per_10pct * (abs(m) / 0.10)
        else:
            base -= self.call_skew_relief * (m / 0.10)

        # Panic bid for vol -- 2000 saw repeated IV spikes on the way down.
        base *= 1.0 + 0.55 * regime_stress

        # Earnings premium, added as VARIANCE for a single day rather than as a
        # blanket multiplier on vol. This matters: a multiplicative bump inflates
        # a 6-month option as much as a 2-week one, which is not how an event
        # prices. Adding a fixed lump of variance makes the bump dilute with
        # tenor, exactly as a real term structure does -- and it is what makes
        # the front-vs-back extraction in OP-04 recoverable at all.
        if earnings_in is not None and 0 <= earnings_in <= 400 and T > 1e-6:
            ev = self.event_variance(base)
            ordinary = base ** 2 / 252.0
            base = math.sqrt(max(base ** 2 + (ev - ordinary) / T, 1e-6))

        return float(min(max(base, self.min_iv), self.max_iv))

    def event_variance(self, base_vol: float) -> float:
        """Variance the market attaches to the single earnings session."""
        ordinary_daily = base_vol / math.sqrt(252.0)
        return (self.event_move_multiple * ordinary_daily) ** 2


@dataclass
class OptionContract:
    symbol: str
    kind: str          # "call" | "put"
    strike: float
    expiry: date

    @property
    def key(self) -> str:
        return f"{self.symbol}{self.expiry:%y%m%d}{self.kind[0].upper()}{int(self.strike*1000):08d}"

    def dte(self, today: date) -> int:
        return max((self.expiry - today).days, 0)

    def T(self, today: date) -> float:
        return max((self.expiry - today).days, 0) / 365.0

    def intrinsic(self, S: float) -> float:
        return max(0.0, S - self.strike) if self.kind == "call" else max(0.0, self.strike - S)

    def __repr__(self) -> str:
        return f"{self.symbol} {self.expiry:%b%d'%y} {self.strike:g} {self.kind[0].upper()}"


class OptionPricer:
    def __init__(self, surface: VolSurface | None = None):
        self.surface = surface or VolSurface()

    def price(self, c: OptionContract, S: float, today: date, realized_vol: float,
              q: float = 0.0, earnings_in: int | None = None,
              regime_stress: float = 0.0) -> tuple[float, float]:
        """Returns (theoretical mid, implied vol used)."""
        T = c.T(today)
        r = risk_free_rate(today)
        sigma = self.surface.iv(realized_vol, S, c.strike, T, c.kind,
                                earnings_in, regime_stress)
        px = bsm(S, c.strike, T, r, sigma, c.kind, q)
        # American early-exercise floor for puts; both floored at intrinsic.
        return max(px, c.intrinsic(S)), sigma

    def greeks(self, c: OptionContract, S: float, today: date, sigma: float,
               q: float = 0.0) -> dict:
        return greeks(S, c.strike, c.T(today), risk_free_rate(today), sigma, c.kind, q)
