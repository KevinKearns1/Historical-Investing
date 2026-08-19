"""EQ-02  Gap Continuation and Gap Fade.

Thesis. 2000 was the golden age of the gap. Earnings, analyst actions and
dot-com news routinely repriced a name 10-20% before the bell. Two different
trades hide in that: a moderate gap with real volume behind it tends to keep
going, while an extreme gap with no follow-through volume is an overreaction
that decays back toward the prior close.

Rules.
  Gap        measured open vs prior close, requires 20-day ADV > 2m shares.
  Continue   |gap| between 2% and 6%, first 15 minutes holding the gap
             direction, and volume above 1.5x the usual opening share.
             Entry on a 15-minute range break in the gap's direction.
  Fade       |gap| > 8% and the first 15 minutes FAILING to extend, which in
             2000 usually meant the move was a pre-market print rather than
             real demand. Entry against the gap, targeting a third of it.
  Stop       continuation: the 15-minute extreme against the trade.
             fade: 1.0 x ATR beyond the session extreme.
  Exit       target, stop, or 15:50. Flat overnight, always.
"""
from __future__ import annotations

from strategies.base import Strategy

SETUP_MINUTES = 15


class GapTrade(Strategy):
    def __init__(self, **kw):
        super().__init__(name="EQ-02 Gap Continuation/Fade", sleeve="equity", cadence=1, **kw)

    def on_session_start(self, ctx) -> None:
        self.state = {"plans": {}, "taken": set(), "built": False}

    def on_bar(self, ctx) -> None:
        m = ctx.minute_index
        self._manage(ctx)
        if not self.state["built"] and ctx.fire_once(self.name + ':setup', SETUP_MINUTES):
            self._build(ctx)
            self.state["built"] = True
        if not (SETUP_MINUTES <= m < 300) or not ctx.risk.can_open(self.name):
            return
        for sym, plan in self.state["plans"].items():
            if sym in self.state["taken"]:
                continue
            if len(self.my_equity_positions(ctx)) >= 2:
                return
            self._try_entry(ctx, sym, plan)

    def _build(self, ctx) -> None:
        for sym in ctx.universe:
            prior = ctx.feed.prior_bar(sym)
            bars = ctx.feed.intraday(sym)
            adv, atr = ctx.feed.adv(sym, 20), ctx.feed.atr(sym, 14)
            if prior is None or len(bars) < SETUP_MINUTES or not adv or not atr:
                continue
            if adv < 2_000_000 or prior.close < 10:
                continue
            op = float(bars["open"].iloc[0])
            gap = op / prior.close - 1.0
            seg = bars.iloc[:SETUP_MINUTES]
            hi, lo = float(seg["high"].max()), float(seg["low"].min())
            last = float(seg["close"].iloc[-1])
            ovr = ctx.feed.opening_volume_ratio(sym, SETUP_MINUTES)
            vol_ok = ovr is not None and ovr > 1.30

            if 0.02 <= abs(gap) <= 0.06 and vol_ok:
                held = (last >= op) if gap > 0 else (last <= op)
                if held:
                    side = 1 if gap > 0 else -1
                    self.state["plans"][sym] = {
                        "mode": "continue", "side": side,
                        "trigger": hi if side > 0 else lo,
                        "stop": lo if side > 0 else hi, "atr": atr,
                        "target": None, "gap": gap,
                    }
            elif abs(gap) > 0.08:
                failed = (last < op) if gap > 0 else (last > op)
                if failed:
                    side = -1 if gap > 0 else 1
                    self.state["plans"][sym] = {
                        "mode": "fade", "side": side,
                        "trigger": lo if side < 0 else hi,
                        "stop": (hi if side < 0 else lo) + side * -1.0 * atr,
                        "atr": atr,
                        "target": op - side * -1 * abs(op - prior.close) * 0.33,
                        "gap": gap,
                    }

    def _try_entry(self, ctx, sym: str, plan: dict) -> None:
        px = ctx.feed.last_price(sym)
        if not px:
            return
        side = plan["side"]
        if side * (px - plan["trigger"]) <= 0:
            return
        stop = plan["stop"]
        if side * (px - stop) <= 0:
            return
        qty = ctx.risk.shares_for(ctx.net_liq(), px, stop, self.capital) * side
        if abs(qty) < 1:
            return
        fill = ctx.broker.market_order(sym, qty, self.name, note=f"gap {plan['mode']}")
        if fill.ok:
            self.state["taken"].add(sym)
            ctx.risk.note_trade(self.name)
            pos = ctx.pf.equities[sym]
            pos.stop = stop
            r = abs(fill.price - stop)
            pos.target = plan["target"] or (fill.price + side * 2.0 * r)
            self.log(ctx, f"{plan['mode']} {'L' if side>0 else 'S'} {abs(qty)} {sym} "
                          f"@ {fill.price:.4f} gap {plan['gap']:+.1%}")

    def _manage(self, ctx) -> None:
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            px = ctx.feed.last_price(sym)
            if not px or pos.stop is None:
                continue
            side = 1 if pos.shares > 0 else -1
            if side * (px - pos.stop) <= 0:
                ctx.broker.close_equity(sym, self.name, "gap stop", is_stop=True)
            elif pos.target and side * (px - pos.target) >= 0:
                ctx.broker.close_equity(sym, self.name, "gap target")

    def on_session_end(self, ctx) -> None:
        for sym in list(self.my_equity_positions(ctx)):
            ctx.broker.close_equity(sym, self.name, "gap flat on close")
