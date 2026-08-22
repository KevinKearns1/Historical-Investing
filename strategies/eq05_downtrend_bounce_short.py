"""EQ-05  Downtrend Bounce Short (uptick-rule compliant).

Thesis. This is the strategy built for what 2000 actually was. From March the
Nasdaq fell roughly 50% off its high, and every leg down was punctuated by
violent one- and two-day bounces that retraced into a falling intraday average
and then failed. Selling those bounces -- rather than shorting new lows, which
the uptick rule made nearly impossible to execute -- was the year's cleanest
repeatable edge.

Rules.
  Regime     armed only when the index is below its 20-day average AND the
             20-day is falling. Otherwise the strategy does not trade at all.
  Universe   weak names: 20-day return below the index, price > $10,
             ADV > 2m shares.
  Entry      intraday rally back INTO the declining session VWAP from below,
             within 0.5%, that then rolls over -- i.e. a lower high against
             VWAP. Entry is a market short, which the broker will reject
             unless the tape prints an uptick (Rule 10a-1), so the strategy
             re-tries on subsequent bars rather than assuming a fill.
  Stop       0.75 x ATR above the session high made after entry.
  Target     the session low, or 2R, whichever comes first.
  Exit       target, stop, or 15:50.
  Borrow     shorts pay the hard-to-borrow rate on names flagged in config --
             in 2000 the hottest tech shorts were expensive or simply
             unavailable, and pretending otherwise flatters the results.
"""
from __future__ import annotations

from strategies.base import Strategy


class DowntrendBounceShort(Strategy):
    def __init__(self, benchmark: str = "^IXIC", **kw):
        super().__init__(name="EQ-05 Downtrend Bounce Short", sleeve="equity", cadence=1, **kw)
        self.benchmark = benchmark

    def on_session_start(self, ctx) -> None:
        self.state = {"armed": self._regime_ok(ctx), "tried": {}, "watch": []}
        if not self.state["armed"]:
            return
        b20 = ctx.feed.ret(self.benchmark, 20)
        watch = []
        for sym in ctx.universe:
            r20, adv, px = ctx.feed.ret(sym, 20), ctx.feed.adv(sym, 20), ctx.feed.last_close(sym)
            if None in (r20, adv, px, b20) or adv < 2_000_000 or px < 10:
                continue
            if r20 < b20:
                watch.append((r20, sym))
        watch.sort()
        self.state["watch"] = [s for _, s in watch[:8]]

    def _regime_ok(self, ctx) -> bool:
        c = ctx.feed.last_close(self.benchmark)
        m20 = ctx.feed.sma(self.benchmark, 20)
        h = ctx.feed.history(self.benchmark, 30)
        if c is None or m20 is None or len(h) < 26:
            return False
        m20_prev = float(h["close"].iloc[-25:-5].mean())
        return c < m20 and m20 < m20_prev

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        m = ctx.minute_index
        if not self.state["armed"] or not (60 <= m < 330) or not ctx.risk.can_open(self.name):
            return
        if len(self.my_equity_positions(ctx)) >= 2:
            return
        for sym in self.state["watch"]:
            if sym in ctx.pf.equities or self.state["tried"].get(sym, 0) >= 6:
                continue
            self._try(ctx, sym)

    def _try(self, ctx, sym: str) -> None:
        bars = ctx.feed.intraday(sym)
        if len(bars) < 45:
            return
        vwap, px, atr = ctx.feed.session_vwap(sym), ctx.feed.last_price(sym), ctx.feed.atr(sym, 14)
        if not all([vwap, px, atr]):
            return
        recent = bars.tail(20)
        # A bounce into VWAP from below that is now rolling over.
        touched = float(recent["high"].max()) >= vwap * 0.995
        below_now = px < vwap
        rolling = px < float(recent["close"].iloc[-4]) if len(recent) >= 4 else False
        session_low = float(bars["low"].min())
        if not (touched and below_now and rolling and px > session_low):
            return

        stop = max(float(recent["high"].max()), vwap) + 0.75 * atr
        qty = -ctx.risk.shares_for(ctx.net_liq(), px, stop, self.capital)
        if abs(qty) < 1:
            return
        if not self.symbol_is_free(ctx, sym):
            return              # another strategy owns this symbol
        self.state["tried"][sym] = self.state["tried"].get(sym, 0) + 1
        fill = ctx.broker.market_order(sym, qty, self.name, note="bounce short")
        if fill.ok:
            ctx.risk.note_trade(self.name)
            pos = ctx.pf.equities[sym]
            pos.stop = stop
            r = abs(fill.price - stop)
            pos.target = max(session_low, fill.price - 2.0 * r)
            self.log(ctx, f"SHORT {abs(qty)} {sym} @ {fill.price:.4f} stop {stop:.4f} "
                          f"target {pos.target:.4f}")

    def _manage(self, ctx) -> None:
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            px = ctx.feed.last_price(sym)
            if not px or pos.stop is None:
                continue
            if px >= pos.stop:
                ctx.broker.close_equity(sym, self.name, "short stop", is_stop=True)
            elif pos.target and px <= pos.target:
                ctx.broker.close_equity(sym, self.name, "short target")

    def on_session_end(self, ctx) -> None:
        for sym in list(self.my_equity_positions(ctx)):
            ctx.broker.close_equity(sym, self.name, "short flat on close")
