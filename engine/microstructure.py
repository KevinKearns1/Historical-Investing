"""Year-2000 trading frictions.

The details below are not cosmetic -- each one materially changes what a day
trading strategy could actually do in 2000, and leaving them out is the most
common way a backtest of this era invents profit that never existed.

  * SIXTEENTHS. US equities traded in $1/16 = $0.0625 increments for all of
    2000. Decimalization was a small NYSE pilot from 2000-08-28 and did not
    finish until 2001 (NYSE Jan 2001, Nasdaq Apr 2001). A minimum tick 6x
    today's penny puts a hard floor under the spread, which is exactly the
    cost a scalping strategy lives or dies on.
  * UPTICK RULE. SEC Rule 10a-1 (in force until 2007) barred shorting on a
    downtick. A short entry had to print at a price above the last different
    price. In a fast tape that is a real, and frequently binding, constraint.
  * NO PATTERN DAY TRADER RULE. The $25,000 PDT minimum was approved in
    Feb 2001 and took effect 2001-09-28. In 2000 a $25,000 account faced no
    day trade count limit -- but also got no 4x day trading buying power,
    which arrived with the same rule set. Reg T 2x is the correct leverage.
  * COMMISSIONS. Online retail in 2000 ran roughly $8-$30 a stock trade and
    $15-$30 plus ~$1.75/contract on options. Vastly more than today.
  * SEC SECTION 31 FEE on sales, plus wide spreads on anything but the most
    liquid names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

TICK_SIXTEENTH = 1.0 / 16.0
DECIMALIZATION_PILOT = date(2000, 8, 28)


def round_to_tick(price: float, d: date | None = None, decimal: bool = False) -> float:
    """Snap a price to the tick that was legal on that date."""
    if price <= 0:
        return 0.0
    if decimal:
        return round(price, 2)
    if price < 1.0:                     # sub-dollar traded in 1/32nds
        return round(price * 32) / 32
    return round(price / TICK_SIXTEENTH) * TICK_SIXTEENTH


@dataclass
class CostModel:
    """Costs and frictions for a 2000-era online retail account."""

    equity_commission: float = 10.00        # per stock trade, flat
    option_base: float = 15.00              # per options ticket
    option_per_contract: float = 1.75
    sec_fee_rate: float = 1.0 / 300_000     # Section 31, on sale proceeds
    borrow_rate_annual: float = 0.035       # short borrow on general collateral
    hard_to_borrow_rate: float = 0.25       # hot names (many 2000 tech shorts)
    margin_rate_annual: float = 0.095       # Reg T debit rate, prime + ~2%
    enforce_uptick: bool = True
    decimal_after_pilot: bool = False       # keep sixteenths all year by default

    # -- spreads ----------------------------------------------------------
    def equity_half_spread(self, price: float, adv: float | None, vol: float | None) -> float:
        """Half the quoted bid/ask, floored at half a tick."""
        base = max(price * 0.0006, TICK_SIXTEENTH / 2)
        if adv and adv < 1_000_000:
            base *= 2.2
        elif adv and adv < 5_000_000:
            base *= 1.4
        if vol and vol > 0.60:              # high-vol names quoted wider
            base *= 1.0 + min((vol - 0.60) * 1.5, 1.5)
        return base

    def option_half_spread(self, premium: float, underlying_vol: float | None) -> float:
        """Options spreads in 2000 were brutal: a tick minimum, and typically
        5-15% of premium on anything but front-month at-the-money."""
        base = max(premium * 0.06, TICK_SIXTEENTH)
        if underlying_vol and underlying_vol > 0.70:
            base = max(base, premium * 0.10)
        return min(base, premium * 0.35)

    def impact(self, shares: float, adv: float | None, price: float) -> float:
        """Square-root market impact, in price units."""
        if not adv or adv <= 0:
            return 0.0
        part = min(abs(shares) / adv, 0.25)
        return price * 0.10 * (part ** 0.5)

    # -- fees -------------------------------------------------------------
    def equity_fees(self, shares: float, price: float, is_sale: bool) -> float:
        fee = self.equity_commission
        if is_sale:
            fee += abs(shares) * price * self.sec_fee_rate
        return fee

    def option_fees(self, contracts: int, premium: float, is_sale: bool) -> float:
        fee = self.option_base + abs(contracts) * self.option_per_contract
        if is_sale:
            fee += abs(contracts) * 100 * premium * self.sec_fee_rate
        return fee


def uptick_ok(last: float, prev_different: float | None) -> bool:
    """Rule 10a-1: a short may only be initiated on a plus tick, or a zero-plus
    tick (same price as the last, where that last was itself an uptick)."""
    if prev_different is None:
        return True
    return last > prev_different
