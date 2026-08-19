"""OP-02  Rolling Bear Put Debit Spreads on the Nasdaq-100 proxy (QQQ).

Thesis. Long puts are right about direction and wrong about cost: in 2000 index
IV was persistently bid, so paying full freight for downside was expensive. The
debit spread sells the deeper strike back, cutting the cost roughly in half and
capping the payoff at a level the index genuinely reached repeatedly that year.

Instrument note. QQQ began trading 1999-03-10, so it is a legitimately tradable
2000 instrument. It is used here rather than NDX because the contract size fits
a $25,000 account -- NDX at ~3,500 index points was a $350,000 notional per
contract and simply unaffordable.

Rules.
  Regime     armed when QQQ closes below its 20-day average.
  Structure  buy the ~ATM put, sell the put ~10% lower, 45-60 DTE, monthly.
  Cadence    one spread at a time, rolled when closed or at 14 DTE.
  Exit       +75% of max profit, -60% of debit, or 14 DTE.
  Reality    both legs cross a modelled spread and pay a 2000-era ticket plus
             per-contract commission, twice -- four commissioned legs per
             round trip. That friction is why the exits are wide.
"""
from __future__ import annotations

from strategies.base import Strategy
from engine.options import OptionContract, expiry_near, nearest_strike


class BearPutSpread(Strategy):
    def __init__(self, symbol: str = "QQQ", **kw):
        super().__init__(name="OP-02 Bear Put Spread (QQQ)", sleeve="options", cadence=30, **kw)
        self.symbol = symbol

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        if not ctx.fire_once(self.name, 90) or not ctx.risk.can_open(self.name):
            return
        if self.my_option_positions(ctx):
            return
        px = ctx.feed.last_price(self.symbol)
        m20 = ctx.feed.sma(self.symbol, 20)
        rv = ctx.feed.realized_vol(self.symbol, 21)
        if None in (px, m20, rv) or px >= m20:
            return

        today = ctx.date
        exp = expiry_near(today, 52)
        long_k = nearest_strike(px, 1.00)
        short_k = nearest_strike(px, 0.90)
        if short_k >= long_k:
            return
        lc = OptionContract(self.symbol, "put", long_k, exp)
        sc = OptionContract(self.symbol, "put", short_k, exp)
        lq, sq = ctx.broker.option_quote(lc, ctx.regime["stress"]), ctx.broker.option_quote(sc, ctx.regime["stress"])
        if not lq or not sq:
            return
        debit = lq[1] - sq[0]
        if debit <= 0.10:
            return
        n = ctx.risk.contracts_for(ctx.net_liq(), debit, ctx.open_option_premium())
        if n < 1:
            return
        f1 = ctx.broker.option_order(lc, n, self.name, leg_id="bps", note="long leg",
                                     regime_stress=ctx.regime["stress"])
        if not f1.ok:
            return
        f2 = ctx.broker.option_order(sc, -n, self.name, leg_id="bps", note="short leg",
                                     regime_stress=ctx.regime["stress"])
        if not f2.ok:
            ctx.broker.close_option(lc.key, self.name, "unwind unpaired leg", ctx.regime["stress"])
            return
        ctx.risk.note_trade(self.name)
        self.state["debit"] = f1.price - f2.price
        self.state["width"] = long_k - short_k
        self.log(ctx, f"OPEN {n}x {long_k}/{short_k} put spread {exp:%b%y} "
                      f"debit {self.state['debit']:.4f} (spot {px:.2f})")

    def _manage(self, ctx) -> None:
        pos = self.my_option_positions(ctx)
        if not pos:
            return
        value, dte = 0.0, 999
        for p in pos.values():
            q = ctx.broker.option_quote(p.contract, ctx.regime["stress"])
            if not q:
                return
            bid, ask, _ = q
            value += p.contracts * (bid if p.contracts > 0 else ask)
            dte = min(dte, p.contract.dte(ctx.date))
        debit = self.state.get("debit", 0.0)
        width = self.state.get("width", 0.0)
        maxp = max(width - debit, 0.01)
        why = None
        if debit > 0 and value - debit >= 0.75 * maxp:
            why = "spread +75% of max"
        elif debit > 0 and value <= debit * 0.40:
            why = "spread -60% of debit"
        elif dte <= 14:
            why = "spread roll window"
        if why:
            for key in list(pos):
                ctx.broker.close_option(key, self.name, why, ctx.regime["stress"])
            self.state.pop("debit", None)
