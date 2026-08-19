"""Order execution against the simulated 2000 tape.

Fill rules, deliberately pessimistic:
  * Market orders cross the spread and pay square-root impact.
  * Limit orders fill only if the minute bar actually traded through the
    limit, and fill AT the limit (never better) -- no free price improvement.
  * Stops become market orders on trigger and are charged an extra half tick
    of slippage, because in 2000 a stop in a fast tape got what it got.
  * Every price is snapped to the legal tick (sixteenths).
  * Short sales are refused when the uptick rule would have blocked them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.clock import ET
from engine.microstructure import CostModel, round_to_tick, uptick_ok
from engine.options import OptionContract, OptionPricer


@dataclass
class Fill:
    ok: bool
    price: float = 0.0
    qty: float = 0.0
    fees: float = 0.0
    reason: str = ""


class Broker:
    def __init__(self, feed, portfolio, costs: CostModel, pricer: OptionPricer):
        self.feed = feed
        self.pf = portfolio
        self.costs = costs
        self.pricer = pricer
        self.rejects: dict[str, int] = {}

    def _reject(self, why: str) -> Fill:
        self.rejects[why] = self.rejects.get(why, 0) + 1
        return Fill(False, reason=why)

    # -- equities ---------------------------------------------------------
    def market_order(self, symbol: str, qty: float, strategy: str,
                     note: str = "", is_stop: bool = False) -> Fill:
        if abs(qty) < 1:
            return self._reject("zero_qty")
        px = self.feed.last_price(symbol)
        if not px or px <= 0:
            return self._reject("no_price")

        adv = self.feed.adv(symbol)
        vol = self.feed.realized_vol(symbol)
        half = self.costs.equity_half_spread(px, adv, vol)
        imp = self.costs.impact(qty, adv, px)

        # Uptick rule: a new short must print on an uptick.
        pos = self.pf.equities.get(symbol)
        held = pos.shares if pos else 0.0
        opening_short = qty < 0 and held <= 0
        if opening_short and self.costs.enforce_uptick:
            bars = self.feed.intraday(symbol)
            if len(bars) >= 2:
                closes = bars["close"].values
                prev_diff = None
                for c in closes[-6:-1][::-1]:
                    if abs(c - closes[-1]) > 1e-9:
                        prev_diff = c
                        break
                if not uptick_ok(float(closes[-1]), prev_diff):
                    return self._reject("uptick_rule")

        direction = 1 if qty > 0 else -1
        fill = px + direction * (half + imp)
        if is_stop:
            fill += direction * (1.0 / 32.0)
        fill = round_to_tick(fill, self.feed.clock.session_date)
        if fill <= 0:
            return self._reject("bad_fill_price")

        # Buying power check on opening trades.
        if (qty > 0 and held >= 0) or opening_short:
            prices = self._price_map()
            if abs(qty) * fill > self.pf.buying_power(prices) + 1e-6:
                return self._reject("insufficient_buying_power")

        fees = self.costs.equity_fees(qty, fill, is_sale=qty < 0)
        self.pf.apply_equity_fill(self.feed.clock.now, symbol, qty, fill, fees, strategy, note)
        return Fill(True, fill, qty, fees)

    def limit_order(self, symbol: str, qty: float, limit: float, strategy: str,
                    note: str = "") -> Fill:
        """Fills only if the current minute traded through the limit."""
        bars = self.feed.intraday(symbol)
        if not len(bars):
            return self._reject("no_intraday")
        bar = bars.iloc[-1]
        limit = round_to_tick(limit, self.feed.clock.session_date)
        touched = bar["low"] <= limit if qty > 0 else bar["high"] >= limit
        if not touched:
            return self._reject("limit_not_touched")
        fees = self.costs.equity_fees(qty, limit, is_sale=qty < 0)
        prices = self._price_map()
        if qty > 0 and abs(qty) * limit > self.pf.buying_power(prices) + 1e-6:
            return self._reject("insufficient_buying_power")
        self.pf.apply_equity_fill(self.feed.clock.now, symbol, qty, limit, fees, strategy, note)
        return Fill(True, limit, qty, fees)

    def close_equity(self, symbol: str, strategy: str, note: str = "",
                     is_stop: bool = False) -> Fill:
        pos = self.pf.equities.get(symbol)
        if not pos or abs(pos.shares) < 1e-9:
            return self._reject("no_position")
        return self.market_order(symbol, -pos.shares, strategy, note, is_stop)

    # -- options ----------------------------------------------------------
    def option_quote(self, c: OptionContract, regime_stress: float = 0.0,
                     earnings_in: int | None = None) -> tuple[float, float, float] | None:
        """Returns (bid, ask, iv) or None if unpriceable."""
        S = self.feed.last_price(c.symbol)
        rv = self.feed.realized_vol(c.symbol, 21)
        if not S or not rv:
            return None
        today = self.feed.clock.session_date
        mid, iv = self.pricer.price(c, S, today, rv, earnings_in=earnings_in,
                                    regime_stress=regime_stress)
        half = self.costs.option_half_spread(mid, rv)
        bid = max(round_to_tick(mid - half, today), 0.0625)
        ask = max(round_to_tick(mid + half, today), bid + 0.0625)
        return bid, ask, iv

    def option_order(self, c: OptionContract, qty: int, strategy: str,
                     leg_id: str = "", note: str = "", regime_stress: float = 0.0,
                     earnings_in: int | None = None) -> Fill:
        if qty == 0:
            return self._reject("zero_qty")
        q = self.option_quote(c, regime_stress, earnings_in)
        if q is None:
            return self._reject("no_option_quote")
        bid, ask, _ = q
        px = ask if qty > 0 else bid
        if px <= 0:
            return self._reject("worthless_quote")

        if qty > 0:
            cost = qty * px * 100
            if cost > self.pf.cash:
                return self._reject("insufficient_cash_for_premium")
        else:
            # Short options need collateral. Cash-secured puts and covered
            # calls are checked by the strategy; naked shorts get the standard
            # 20%-of-underlying formula.
            key = c.key
            existing = self.pf.options.get(key)
            if not existing or existing.contracts <= 0:
                S = self.feed.last_price(c.symbol) or 0.0
                req = abs(qty) * 100 * max(0.20 * S - max(0.0, (S - c.strike) if c.kind == "call" else (c.strike - S)), 0.10 * S)
                if req > self.pf.cash:
                    return self._reject("insufficient_collateral")

        fees = self.costs.option_fees(qty, px, is_sale=qty < 0)
        self.pf.apply_option_fill(self.feed.clock.now, c, qty, px, fees, strategy, leg_id, note)
        return Fill(True, px, qty, fees)

    def close_option(self, key: str, strategy: str, note: str = "",
                     regime_stress: float = 0.0) -> Fill:
        pos = self.pf.options.get(key)
        if not pos:
            return self._reject("no_option_position")
        return self.option_order(pos.contract, -pos.contracts, strategy,
                                 pos.leg_id, note, regime_stress)

    # -- helpers ----------------------------------------------------------
    def _price_map(self) -> dict[str, float]:
        out = {}
        for s in self.pf.equities:
            p = self.feed.last_price(s)
            if p:
                out[s] = p
        return out
