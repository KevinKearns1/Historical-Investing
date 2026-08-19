"""EQ-01  Opening Range Breakout.

Thesis. In 2000 the first thirty minutes carried an enormous share of the day's
information: overnight news, European close, and a retail order flood all
cleared into the 09:30-10:00 window. A clean break of that range on expanding
volume was the single most reliable intraday continuation pattern of the era,
especially in Nasdaq large-cap tech where daily ranges of 5-8% were routine.

Rules.
  Universe   liquid Nasdaq large caps, 20-day ADV > 3m shares, price > $10.
  Range      high/low of 09:30-10:00.
  Entry      first minute closing outside the range, requiring the range to be
             at least 0.35 x ATR(14) (a too-narrow range is noise) and no more
             than 1.5 x (a gap day is EQ-02's trade, not this one), plus
             opening-30m volume above 1.15 x this name's own recent median
             opening-range volume. The
             floor is calibrated to what an opening 30-minute range actually
             is -- roughly a third to a half of a daily ATR, not most of it.
  Stop       the opposite side of the range, capped at 2.0 x ATR.
  Target     2R, with a move of the stop to breakeven at 1R.
  Exit       target, stop, or 15:50 -- never held overnight.
  Regime     longs only when the index is above its 10-day average; shorts only
             when below. Direction-agnostic breakout trading was a fast way to
             lose money in the second half of 2000.
"""
from __future__ import annotations

from strategies.base import Strategy

OPENING_MINUTES = 30


class OpeningRangeBreakout(Strategy):
    def __init__(self, **kw):
        super().__init__(name="EQ-01 Opening Range Breakout", sleeve="equity", cadence=1, **kw)

    def on_session_start(self, ctx) -> None:
        self.state = {"ranges": {}, "taken": set(), "be_moved": set()}

    def on_bar(self, ctx) -> None:
        m = ctx.minute_index
        if m < OPENING_MINUTES:
            return
        self._manage(ctx)
        if ctx.fire_once(self.name + ':range', OPENING_MINUTES):
            self._build_ranges(ctx)
        if not (OPENING_MINUTES <= m < 330):        # no new entries after ~15:00
            return
        if not ctx.risk.can_open(self.name):
            return
        for sym, rng in self.state["ranges"].items():
            if sym in self.state["taken"]:
                continue
            if len(self.my_equity_positions(ctx)) >= 2:
                return
            self._try_entry(ctx, sym, rng)

    def _build_ranges(self, ctx) -> None:
        for sym in ctx.universe:
            bars = ctx.feed.intraday(sym)
            if len(bars) < OPENING_MINUTES:
                continue
            op = bars.iloc[:OPENING_MINUTES]
            atr = ctx.feed.atr(sym, 14)
            adv = ctx.feed.adv(sym, 20)
            px = ctx.feed.last_price(sym)
            if not atr or not adv or not px or px < 10:
                continue
            hi, lo = float(op["high"].max()), float(op["low"].min())
            width = hi - lo
            if width < 0.35 * atr or width > 1.5 * atr:
                continue
            # The opening 30 minutes are normally ~22% of the day's volume.
            ovr = ctx.feed.opening_volume_ratio(sym, OPENING_MINUTES)
            if ovr is None or ovr < 1.15:
                continue
            self.state["ranges"][sym] = {"hi": hi, "lo": lo, "atr": atr}

    def _try_entry(self, ctx, sym: str, rng: dict) -> None:
        px = ctx.feed.last_price(sym)
        if not px:
            return
        trend = ctx.regime["trend"]
        long_ok, short_ok = trend >= 0, trend <= 0
        side = 0
        if px > rng["hi"] and long_ok:
            side = 1
        elif px < rng["lo"] and short_ok:
            side = -1
        if side == 0:
            return

        stop = rng["lo"] if side > 0 else rng["hi"]
        if abs(px - stop) > 2.0 * rng["atr"]:
            stop = px - side * 2.0 * rng["atr"]
        qty = ctx.risk.shares_for(ctx.net_liq(), px, stop, self.capital) * side
        if abs(qty) < 1:
            return
        fill = ctx.broker.market_order(sym, qty, self.name, note="ORB entry")
        if fill.ok:
            self.state["taken"].add(sym)
            ctx.risk.note_trade(self.name)
            pos = ctx.pf.equities[sym]
            pos.stop = stop
            pos.target = fill.price + side * 2.0 * abs(fill.price - stop)
            self.log(ctx, f"{'LONG' if side>0 else 'SHORT'} {abs(qty)} {sym} @ {fill.price:.4f} "
                          f"stop {stop:.4f} target {pos.target:.4f}")

    def _manage(self, ctx) -> None:
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            px = ctx.feed.last_price(sym)
            if not px or pos.stop is None:
                continue
            side = 1 if pos.shares > 0 else -1
            r = abs(pos.avg_price - pos.stop)
            if side * (px - pos.stop) <= 0:
                ctx.broker.close_equity(sym, self.name, "ORB stop", is_stop=True)
            elif pos.target and side * (px - pos.target) >= 0:
                ctx.broker.close_equity(sym, self.name, "ORB target")
            elif sym not in self.state["be_moved"] and r > 0 and side * (px - pos.avg_price) >= r:
                pos.stop = pos.avg_price
                self.state["be_moved"].add(sym)

    def on_session_end(self, ctx) -> None:
        for sym in list(self.my_equity_positions(ctx)):
            ctx.broker.close_equity(sym, self.name, "ORB flat on close")
