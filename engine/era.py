"""Date-aware market rules, so a strategy can be tested across decades.

The first version of this engine hard-coded year-2000 rules. That is correct
for 2000 and wrong for every other year, which makes it useless for the thing
that actually matters: finding out whether a strategy survives across regimes.

Everything here is a function of the simulated date. Run the same strategy over
1999 and 2019 and it faces different tick sizes, different shorting law,
different leverage, different commissions and a different options market --
because it did.

Sources for each transition are named inline. Where a rule phased in over
months (decimalization) the engine uses the completion date for the relevant
venue, which is the conservative choice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# -- tick size ------------------------------------------------------------
# US equities quoted in eighths until 1997-06, sixteenths until decimalization.
# NYSE pilot 2000-08-28, NYSE complete 2001-01-29, Nasdaq complete 2001-04-09.
TEENIES_START = date(1997, 6, 24)
DECIMAL_NYSE_COMPLETE = date(2001, 1, 29)
DECIMAL_NASDAQ_COMPLETE = date(2001, 4, 9)


def min_tick(d: date, price: float, venue: str = "nasdaq") -> float:
    """Minimum price increment that was legal on `d`."""
    complete = DECIMAL_NASDAQ_COMPLETE if venue == "nasdaq" else DECIMAL_NYSE_COMPLETE
    if d >= complete:
        return 0.01
    if d >= TEENIES_START:
        return 1.0 / 16.0 if price >= 1.0 else 1.0 / 32.0
    return 1.0 / 8.0 if price >= 1.0 else 1.0 / 16.0


# -- short selling --------------------------------------------------------
# SEC Rule 10a-1 uptick rule, in force 1938 -> repealed effective 2007-07-06.
# Replaced 2010-02-24 by Rule 201, the "alternative uptick" circuit breaker,
# which only binds after a 10% intraday decline in a given name.
UPTICK_REPEALED = date(2007, 7, 6)
ALT_UPTICK_START = date(2010, 2, 24)


def uptick_rule_applies(d: date, intraday_decline: float = 0.0) -> bool:
    if d < UPTICK_REPEALED:
        return True
    if d >= ALT_UPTICK_START and intraday_decline <= -0.10:
        return True          # Rule 201 circuit breaker tripped
    return False


# -- leverage and the pattern day trader rule -----------------------------
# NASD 2520 / NYSE 431 amendments: approved Feb 2001, effective 2001-09-28.
# They introduced BOTH the $25,000 minimum equity for pattern day traders AND
# the 4x day-trading buying power. Before that date: neither existed, and plain
# Reg T 2x is the correct constraint.
PDT_EFFECTIVE = date(2001, 9, 28)
PDT_MIN_EQUITY = 25_000.0


@dataclass
class LeverageRules:
    initial_margin: float
    maintenance_margin: float
    day_trade_buying_power: float
    pdt_applies: bool
    pdt_min_equity: float


def leverage_rules(d: date, account_equity: float) -> LeverageRules:
    if d < PDT_EFFECTIVE:
        # No day-trade counting, but also no 4x. Reg T only.
        return LeverageRules(0.50, 0.25, 2.0, False, 0.0)
    if account_equity >= PDT_MIN_EQUITY:
        return LeverageRules(0.50, 0.25, 4.0, True, PDT_MIN_EQUITY)
    # Under the minimum: day trading is restricted, not merely deleveraged.
    return LeverageRules(0.50, 0.25, 2.0, True, PDT_MIN_EQUITY)


# -- options --------------------------------------------------------------
# Weeklys launched 2005-10-28 (CBOE), broadened from 2010.
# Penny Pilot for option quoting began 2007-01-26.
WEEKLYS_START = date(2005, 10, 28)
WEEKLYS_BROAD = date(2010, 6, 1)
OPTION_PENNY_PILOT = date(2007, 1, 26)
# Expiration moved from the Saturday after the third Friday to the Friday
# itself for most classes in Feb 2015.
EXPIRY_SATURDAY_UNTIL = date(2015, 2, 1)


def weeklys_available(d: date) -> bool:
    return d >= WEEKLYS_START


def option_tick(d: date, premium: float) -> float:
    if d >= OPTION_PENNY_PILOT:
        return 0.01 if premium < 3.0 else 0.05
    if d >= TEENIES_START:
        return 1.0 / 16.0
    return 1.0 / 8.0


# -- commissions ----------------------------------------------------------
# Retail online commission history. These are representative of the mainstream
# discount brokers of each era, not the cheapest outlier available.
_COMMISSION_ERAS = [
    (date(1990, 1, 1), 45.00, 35.00, 3.00),
    (date(1996, 1, 1), 25.00, 25.00, 2.50),
    (date(1999, 1, 1), 12.00, 18.00, 1.75),
    (date(2003, 1, 1), 10.00, 15.00, 1.50),
    (date(2006, 1, 1), 9.99, 12.99, 0.90),
    (date(2010, 1, 1), 8.95, 9.99, 0.75),
    (date(2017, 1, 1), 6.95, 6.95, 0.75),
    (date(2019, 10, 1), 0.00, 0.00, 0.65),   # the zero-commission cutover
]


def commissions(d: date) -> tuple[float, float, float]:
    """(equity per trade, option ticket, option per contract)."""
    eq, ob, opc = _COMMISSION_ERAS[0][1:]
    for start, a, b, c in _COMMISSION_ERAS:
        if d >= start:
            eq, ob, opc = a, b, c
    return eq, ob, opc


# -- regulatory fees ------------------------------------------------------
# SEC Section 31 fee on sales. The rate has been reset many times; these are
# the order-of-magnitude levels per dollar of principal.
_SEC_FEE = [
    (date(1997, 1, 1), 1 / 300_000),
    (date(2003, 1, 1), 1 / 30_000),
    (date(2007, 1, 1), 1 / 60_000),
    (date(2012, 1, 1), 1 / 45_000),
    (date(2019, 1, 1), 1 / 47_000),
    (date(2023, 1, 1), 8.0e-6),
]


def sec_fee_rate(d: date) -> float:
    r = _SEC_FEE[0][1]
    for start, v in _SEC_FEE:
        if d >= start:
            r = v
    return r


@dataclass
class Era:
    """Everything the engine needs to know about the rules on a given date."""

    d: date
    tick: float
    uptick_rule: bool
    initial_margin: float
    day_trade_bp: float
    pdt_applies: bool
    equity_commission: float
    option_base: float
    option_per_contract: float
    sec_fee_rate: float
    weeklys: bool

    @classmethod
    def on(cls, d: date, price: float = 50.0, account_equity: float = 25_000.0) -> "Era":
        lev = leverage_rules(d, account_equity)
        eq, ob, opc = commissions(d)
        return cls(
            d=d, tick=min_tick(d, price), uptick_rule=uptick_rule_applies(d),
            initial_margin=lev.initial_margin, day_trade_bp=lev.day_trade_buying_power,
            pdt_applies=lev.pdt_applies, equity_commission=eq, option_base=ob,
            option_per_contract=opc, sec_fee_rate=sec_fee_rate(d),
            weeklys=weeklys_available(d),
        )

    def describe(self) -> str:
        return (f"{self.d}: tick {self.tick:.4f}, uptick rule "
                f"{'ON' if self.uptick_rule else 'off'}, day-trade BP {self.day_trade_bp:.0f}x, "
                f"PDT {'yes' if self.pdt_applies else 'no'}, "
                f"commissions ${self.equity_commission:.2f}/trade, "
                f"weeklys {'yes' if self.weeklys else 'no'}")
