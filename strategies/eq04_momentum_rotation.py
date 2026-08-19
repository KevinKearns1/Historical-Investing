"""EQ-04  Relative-Strength Momentum Rotation (regime gated).

Thesis. Buy what is already winning -- but only while the market lets momentum
work. Unfiltered momentum was the best trade on earth through March 10 2000 and
one of the worst for the nine months after. The whole strategy is therefore the
regime filter; the ranking is almost incidental.

Rules.
  Rank       every session, score the universe on 5-day return relative to the
             Nasdaq Composite, requiring positive 20-day relative strength too.
  Regime     take positions ONLY when the index closed above its 10-day average
             AND the 10-day is above the 30-day. Otherwise sit in cash.
  Entry      top 2 names, bought in the 09:45-10:15 window to avoid the opening
             auction's spread.
  Stop       1.5 x ATR(14) from entry.
  Exit       the close, or the stop. Single-session holds only.
  Note       this is the strategy that should be flat for most of 2000. That is
             the correct behaviour, not a bug -- a momentum book that stayed
             invested through that year was destroyed.
"""
from __future__ import annotations

from strategies.base import Strategy


class MomentumRotation(Strategy):
    def __init__(self, benchmark: str = "^IXIC", **kw):
        super().__init__(name="EQ-04 Momentum Rotation", sleeve="equity", cadence=5, **kw)
        self.benchmark = benchmark

    def on_session_start(self, ctx) -> None:
        self.state = {"picks": [], "done": False}
        if not self._regime_ok(ctx):
            self.state["done"] = True
            return
        scored = []
        for sym in ctx.universe:
            r5, r20 = ctx.feed.ret(sym, 5), ctx.feed.ret(sym, 20)
            adv, px = ctx.feed.adv(sym, 20), ctx.feed.last_close(sym)
            if None in (r5, r20, adv, px) or adv < 2_000_000 or px < 10:
                continue
            b5, b20 = ctx.feed.ret(self.benchmark, 5), ctx.feed.ret(self.benchmark, 20)
            if b5 is None or b20 is None:
                continue
            if (r20 - b20) <= 0:
                continue
            scored.append((r5 - b5, sym))
        scored.sort(reverse=True)
        self.state["picks"] = [s for _, s in scored[:2]]
        if self.state["picks"]:
            self.log(ctx, f"regime on, picks {self.state['picks']}")

    def _regime_ok(self, ctx) -> bool:
        c = ctx.feed.last_close(self.benchmark)
        m10, m30 = ctx.feed.sma(self.benchmark, 10), ctx.feed.sma(self.benchmark, 30)
        if None in (c, m10, m30):
            return False
        return c > m10 and m10 > m30

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        m = ctx.minute_index
        if self.state["done"] or not ctx.fire_once(self.name, 15) or not ctx.risk.can_open(self.name):
            return
        for sym in self.state["picks"]:
            if sym in ctx.pf.equities:
                continue
            px, atr = ctx.feed.last_price(sym), ctx.feed.atr(sym, 14)
            if not px or not atr:
                continue
            stop = px - 1.5 * atr
            qty = ctx.risk.shares_for(ctx.net_liq(), px, stop, self.capital)
            if qty < 1:
                continue
            fill = ctx.broker.market_order(sym, qty, self.name, note="momentum long")
            if fill.ok:
                ctx.risk.note_trade(self.name)
                ctx.pf.equities[sym].stop = stop
                self.log(ctx, f"LONG {qty} {sym} @ {fill.price:.4f} stop {stop:.4f}")
        self.state["done"] = True

    def _manage(self, ctx) -> None:
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            px = ctx.feed.last_price(sym)
            if px and pos.stop and px <= pos.stop:
                ctx.broker.close_equity(sym, self.name, "momentum stop", is_stop=True)

    def on_session_end(self, ctx) -> None:
        for sym in list(self.my_equity_positions(ctx)):
            ctx.broker.close_equity(sym, self.name, "momentum flat on close")
