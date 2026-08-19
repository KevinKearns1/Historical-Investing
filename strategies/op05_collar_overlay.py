"""OP-05  Index Tail Hedge -- defined-risk put spread overlay.

WHY THIS IS NOT A COLLAR
------------------------
The obvious design here is a collar: long an out-of-the-money index put,
financed by selling an out-of-the-money index call. That is a fine structure
when you OWN the underlying -- the short call is covered by your shares, and
you are simply selling away upside you already have.

This account does not own the index. A short index call with no underlying is
a naked short call: undefined loss, and on a $25,000 account one gap higher
would be an account-ending event. Backtested naively it also looks *good* for
most of 2000, because the market fell -- which is exactly the trap. It
collects premium every month and then hands back several years of it in a
single squeeze. (Nasdaq rallied over 14% in the four sessions from 2000-05-24,
and again violently in late May and early June.)

So the overlay here buys a put SPREAD instead. It gives up the call premium,
costs a little more, and can never lose more than the debit. For a small
account whose entire purpose is to survive the year, that trade is correct.

Thesis. The other nine strategies express views. This one exists so that a
single overnight gap cannot end the program. It is expected to LOSE money in
most months; it is judged on the drawdown it prevents, not on its own P&L,
and the report breaks out both.

Rules.
  Instrument SPY. Contract size is the reason -- SPX at ~1,400 index points
             was $140,000 of notional per contract in 2000, uninvestable here.
  Structure  long ~5% OTM put, short ~15% OTM put, 60-90 DTE, monthly cycle.
  Sizing     scaled to the book's actual OVERNIGHT exposure (the wheel stock
             and any assigned shares), not to net liq -- an intraday-flat book
             does not need an overnight hedge, and paying for one anyway is
             just a slow leak.
  Scale-up   doubled when the stress reading is elevated (index below its
             50-day and realized vol above 30%).
  Roll       at 21 DTE, or once the long leg is 2% in the money.
"""
from __future__ import annotations

from strategies.base import Strategy
from engine.options import OptionContract, expiry_near, nearest_strike


class CollarOverlay(Strategy):
    def __init__(self, symbol: str = "SPY", **kw):
        super().__init__(name="OP-05 Index Tail Hedge", sleeve="options", cadence=60, **kw)
        self.symbol = symbol

    def _overnight_exposure(self, ctx) -> float:
        """What the book actually carries through the night."""
        total = 0.0
        for sym, pos in ctx.pf.equities.items():
            px = ctx.feed.last_price(sym)
            if px:
                total += abs(pos.shares * px)
        return total

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        if not ctx.fire_once(self.name, 180):
            return
        if self.my_option_positions(ctx):
            return

        px = ctx.feed.last_price(self.symbol)
        rv = ctx.feed.realized_vol(self.symbol, 21)
        if None in (px, rv):
            return

        exposure = self._overnight_exposure(ctx)
        stress = ctx.regime["stress"]
        # Always carry a minimum hedge in a stressed tape, even flat, because
        # the equity sleeve can be assigned into stock overnight by OP-03.
        target = max(exposure, ctx.net_liq() * (0.5 if stress > 0.5 else 0.25))
        # Granularity problem, stated plainly: one SPY contract was ~$16,600 of
        # notional in 2000, about two thirds of this account. The hedge cannot
        # be sized smoothly, so it rounds to the nearest contract and takes one
        # whenever stress alone justifies it.
        n = int(round(target / (px * 100)))
        if stress > 0.5:
            n *= 2
        if n < 1:
            n = 1 if stress > 0.4 else 0
        if n < 1:
            return

        today = ctx.date
        exp = expiry_near(today, 75)
        Kl = nearest_strike(px, 0.95)
        Ks = nearest_strike(px, 0.85)
        if Ks >= Kl:
            return
        cl = OptionContract(self.symbol, "put", Kl, exp)
        cs = OptionContract(self.symbol, "put", Ks, exp)
        ql = ctx.broker.option_quote(cl, stress)
        qs = ctx.broker.option_quote(cs, stress)
        if not ql or not qs:
            return
        debit = ql[1] - qs[0]
        if debit <= 0.05:
            return
        n = min(n, ctx.risk.contracts_for(ctx.net_liq(), debit, ctx.open_option_premium()))
        if n < 1:
            return

        f1 = ctx.broker.option_order(cl, n, self.name, leg_id="hedge",
                                     note="tail hedge long leg", regime_stress=stress)
        if not f1.ok:
            return
        f2 = ctx.broker.option_order(cs, -n, self.name, leg_id="hedge",
                                     note="tail hedge short leg", regime_stress=stress)
        if not f2.ok:
            ctx.broker.close_option(cl.key, self.name, "unwind unpaired leg", stress)
            return
        self.state["debit"] = f1.price - f2.price
        self.log(ctx, f"TAIL HEDGE {n}x {Kl}/{Ks} put spread {exp:%b%y} "
                      f"debit {self.state['debit']:.4f} (spot {px:.2f}, stress {stress:.2f})")

    def _manage(self, ctx) -> None:
        pos = self.my_option_positions(ctx)
        if not pos:
            return
        px = ctx.feed.last_price(self.symbol)
        if px is None:
            return
        why = None
        for p in pos.values():
            if p.contract.dte(ctx.date) <= 21:
                why = "hedge roll window"
            if p.contracts > 0 and px < p.contract.strike * 0.98:
                why = "hedge in the money, monetise"
        if why:
            for key in list(pos):
                ctx.broker.close_option(key, self.name, why, ctx.regime["stress"])
            self.state.pop("debit", None)
