"""EQ-03  VWAP Mean Reversion.

Thesis. Between the open and the close, a liquid stock oscillates around its
volume-weighted average price. Institutions in 2000 were benchmarked to VWAP
and mechanically leaned against extensions, which made the snap-back real and
repeatable. This is the sleeve's counterweight: it makes money on the chop
that stops out the breakout strategies.

Rules.
  Universe   ADV > 3m shares, price > $15.
  Signal     price at least 2.0 intraday sigma from session VWAP, measured
             after 11:00 so VWAP has meaningful volume behind it.
  Entry      fade the extension -- buy below, sell short above (short entries
             still have to satisfy the uptick rule, which the broker enforces).
  Target     the VWAP touch.
  Stop       3.0 sigma, i.e. the extension getting worse by half again.
  Time stop  60 minutes. A reversion that has not happened is not reversion.
  Filter     skipped entirely on days when the name has gapped more than 4% --
             a repricing is not an extension, and fading news was the fastest
             way to be run over in 2000.
"""
from __future__ import annotations

import numpy as np

from strategies.base import Strategy


class VWAPReversion(Strategy):
    def __init__(self, **kw):
        super().__init__(name="EQ-03 VWAP Mean Reversion", sleeve="equity", cadence=1, **kw)

    def on_session_start(self, ctx) -> None:
        self.state = {"entries": {}, "count": {}}

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        m = ctx.minute_index
        if not (90 <= m < 340) or not ctx.risk.can_open(self.name):
            return
        if len(self.my_equity_positions(ctx)) >= 2:
            return
        for sym in ctx.universe:
            if not ctx.risk.can_open(self.name):
                return
            if self.state["count"].get(sym, 0) >= 1:
                continue
            if sym in ctx.pf.equities:
                continue
            self._try(ctx, sym)

    def _try(self, ctx, sym: str) -> None:
        bars = ctx.feed.intraday(sym)
        if len(bars) < 60:
            return
        adv = ctx.feed.adv(sym, 20)
        px = ctx.feed.last_price(sym)
        vwap = ctx.feed.session_vwap(sym)
        prior = ctx.feed.prior_bar(sym)
        if not all([adv, px, vwap, prior]) or adv < 3_000_000 or px < 15:
            return
        if abs(float(bars["open"].iloc[0]) / prior.close - 1.0) > 0.04:
            return              # news repricing, not an extension

        dev = bars["close"].values - np.asarray(
            (bars["high"] + bars["low"] + bars["close"]).values / 3.0
        )
        sigma = float(np.std(bars["close"].values[-60:], ddof=1))
        if sigma <= 1e-6:
            return
        z = (px - vwap) / sigma
        if abs(z) < 2.0 or abs(z) > 4.0:      # beyond 4 sigma it is a trend
            return

        side = -1 if z > 0 else 1
        stop = vwap + np.sign(z) * 3.0 * sigma
        qty = ctx.risk.shares_for(ctx.net_liq(), px, stop, self.capital) * side
        if abs(qty) < 1:
            return
        fill = ctx.broker.market_order(sym, qty, self.name, note=f"vwap z={z:.2f}")
        if fill.ok:
            ctx.risk.note_trade(self.name)
            self.state["count"][sym] = self.state["count"].get(sym, 0) + 1
            self.state["entries"][sym] = ctx.minute_index
            pos = ctx.pf.equities[sym]
            pos.stop = float(stop)
            pos.target = float(vwap)
            self.log(ctx, f"fade {'S' if side<0 else 'L'} {abs(qty)} {sym} z={z:+.2f} "
                          f"@ {fill.price:.4f} -> vwap {vwap:.4f}")

    def _manage(self, ctx) -> None:
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            px = ctx.feed.last_price(sym)
            if not px or pos.stop is None:
                continue
            side = 1 if pos.shares > 0 else -1
            entered = self.state["entries"].get(sym, ctx.minute_index)
            if side * (px - pos.stop) <= 0:
                ctx.broker.close_equity(sym, self.name, "vwap stop", is_stop=True)
            elif pos.target and side * (px - pos.target) >= 0:
                ctx.broker.close_equity(sym, self.name, "vwap target")
            elif ctx.minute_index - entered > 60:
                ctx.broker.close_equity(sym, self.name, "vwap time stop")

    def on_session_end(self, ctx) -> None:
        for sym in list(self.my_equity_positions(ctx)):
            ctx.broker.close_equity(sym, self.name, "vwap flat on close")
