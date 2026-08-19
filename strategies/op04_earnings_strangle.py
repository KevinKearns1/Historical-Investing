"""OP-04  Earnings Long Strangle, gated on the implied move vs the realized one.

Thesis. 2000 produced some of the largest post-earnings gaps in market
history -- 30-50% single-day repricings were not rare among the high-multiple
names. Owning a strangle into that is a bet that the actual move exceeds the
one the option market has priced.

THE GATE, AND WHY THE OBVIOUS VERSION DOES NOT WORK
---------------------------------------------------
The natural filter is "buy when implied vol is below realized vol". It is
useless here, and the engine demonstrates why: once you model a realistic
earnings vol bump, implied is essentially ALWAYS above trailing realized into
an event. That is not a mispricing, it is the market correctly pricing a known
catalyst. A filter comparing the two never passes, and a strategy that never
trades has not been tested.

The correct comparison is the one an options desk actually makes:

    implied EVENT move  vs  the stock's own median absolute move on its PAST
                            earnings dates

Getting that comparison right matters more than it looks. The naive version --
straddle premium over spot -- is wrong, and wrong by an order of magnitude: with
no weeklys in 2000 the nearest monthly expiry is often 30-45 days out, so that
ratio measures the expected move over six weeks (30-40%) and compares it against
a one-day earnings reaction (2-15%). It rejects everything, always.

The extraction has to isolate the EVENT premium, and the tempting shortcut --
comparing front-month IV against trailing realized vol -- does not do that. It
dumps the entire volatility risk premium, the skew and the term structure into
the "event" term and reports a 35% implied move for a stock priced for 8%.

The method that works uses two expiries, both of which contain the event. The
event contributes the same lump of variance to each, while ordinary diffusion
scales with tenor:

    IV1^2 * T1  =  base^2 * (T1 - dt)  +  event
    IV2^2 * T2  =  base^2 * (T2 - dt)  +  event

Two equations, two unknowns. Solving for `event` and taking its square root
gives the market's implied one-day earnings move -- the number a desk quotes as
"priced for a 12% move", and the only one comparable to the realized history.
It needs nothing but two observable quotes, so it works identically against
real option chains.

Both sides are computable point-in-time -- past earnings dates are in the
config, and the reaction to each is in the price history the feed already
limits to completed bars.

Rules.
  Trigger    2-4 sessions before a scheduled earnings date.
  Filter     implied EVENT move < 0.85 x the median of the stock's last 4
             realized earnings-day absolute moves. At least 3 past events
             required -- with fewer the estimate is noise and the trade is
             skipped.
  Structure  long ~20-delta call and ~20-delta put, on the first monthly expiry
             at least ~50 days out. That is further than a pure event trade
             wants, and it is a direct cost of there being NO WEEKLYS in 2000:
             the shortest listed contract containing the event is a monthly, and
             the front monthly is too contaminated by term structure to price
             the event off. The trade pays extra time value for that.
  Size       hard-capped at 1% of net liq per event. This strategy is expected
             to lose small and often, and win rarely and large.
  Exit       the session after the announcement. Holding through the post-event
             vol crush turns a winner into a loser.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from strategies.base import Strategy
from engine.options import OptionContract, delta_strike, expiry_near, nearest_strike, risk_free_rate


class EarningsStrangle(Strategy):
    def __init__(self, earnings: dict | None = None, **kw):
        super().__init__(name="OP-04 Earnings Strangle", sleeve="options", cadence=30, **kw)
        self.earnings = earnings or {}

    def _days_to_earnings(self, sym: str, today):
        fut = [(d - today).days for d in self.earnings.get(sym, []) if (d - today).days >= 0]
        return min(fut) if fut else None

    def _historical_earnings_move(self, ctx, sym: str) -> float | None:
        """Median absolute move on this stock's PAST earnings dates.

        Point-in-time by construction: only events strictly before today are
        considered, and each move is read out of the daily history the feed
        already limits to completed bars.
        """
        today = ctx.date
        past = sorted(d for d in self.earnings.get(sym, []) if d < today)
        if len(past) < 3:
            return None
        h = ctx.feed.history(sym, 400)
        if len(h) < 30:
            return None
        dates = [d.date() for d in h.index]
        moves = []
        for ev in past[-4:]:
            # The reaction prints on the session AFTER the announcement.
            after = [i for i, d in enumerate(dates) if d > ev]
            if not after or after[0] == 0:
                continue
            i = after[0]
            prev, react = float(h["close"].iloc[i - 1]), float(h["close"].iloc[i])
            if prev > 0:
                moves.append(abs(react / prev - 1.0))
        return float(np.median(moves)) if len(moves) >= 3 else None

    @staticmethod
    def _implied_event_move(iv1: float, T1: float, iv2: float, T2: float) -> float | None:
        """Market-implied one-day earnings move, from two expiries.

        Both expiries must contain the event. Returns None if the two tenors are
        too close together for the solve to be numerically meaningful, or if the
        implied event variance comes out non-positive -- the latter means the
        term structure carries no event premium at all, which on real quotes is
        a sign of a stale or crossed market rather than a gift, so it is
        rejected rather than treated as free optionality.
        """
        # The front expiry must sit past the steep end of the term structure.
        # If it does not, the natural front-month richness -- which has nothing
        # to do with earnings -- gets attributed to the event and the extraction
        # overstates the implied move by roughly a factor of two. This is a real
        # property of the two-expiry method on any non-flat curve, not an
        # artifact of the model, and requiring T1 >= 0.12 years (~45 days) is
        # the standard way to keep the solve honest.
        if T1 < 0.12 or T2 - T1 < 0.02 or T1 <= 0:
            return None
        dt = 1.0 / 252.0
        num = iv1 ** 2 * T1 * (T2 - dt) - iv2 ** 2 * T2 * (T1 - dt)
        den = (T2 - dt) - (T1 - dt)
        if abs(den) < 1e-9:
            return None
        event_var = num / den
        if event_var <= 0:
            return None
        return float(event_var ** 0.5)

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        if not ctx.fire_once(self.name, 45) or not ctx.risk.can_open(self.name):
            return
        if len({p.leg_id for p in self.my_option_positions(ctx).values()}) >= 2:
            return

        for sym in ctx.universe:
            d_earn = self._days_to_earnings(sym, ctx.date)
            if d_earn is None or not (2 <= d_earn <= 4):
                continue
            if any(p.contract.symbol == sym for p in self.my_option_positions(ctx).values()):
                continue
            hist_move = self._historical_earnings_move(ctx, sym)
            if hist_move is None:
                continue
            px, rv = ctx.feed.last_price(sym), ctx.feed.realized_vol(sym, 21)
            if None in (px, rv):
                continue

            today = ctx.date
            # At least ~50 DTE so the front leg clears the term-structure kink
            # that would otherwise contaminate the event extraction below.
            exp = expiry_near(today, max(d_earn + 45, 50))
            T = max((exp - today).days, 1) / 365.0
            r = risk_free_rate(today)
            Kc = delta_strike(px, T, r, rv, 0.20, "call")
            Kp = delta_strike(px, T, r, rv, 0.20, "put")
            cc = OptionContract(sym, "call", Kc, exp)
            cp = OptionContract(sym, "put", Kp, exp)
            qc = ctx.broker.option_quote(cc, ctx.regime["stress"], earnings_in=d_earn)
            qp = ctx.broker.option_quote(cp, ctx.regime["stress"], earnings_in=d_earn)
            if not qc or not qp:
                continue

            # The implied ONE-DAY event move, extracted from front-month ATM IV.
            Ka = nearest_strike(px, 1.0)
            qac = ctx.broker.option_quote(OptionContract(sym, "call", Ka, exp),
                                          ctx.regime["stress"], earnings_in=d_earn)
            qap = ctx.broker.option_quote(OptionContract(sym, "put", Ka, exp),
                                          ctx.regime["stress"], earnings_in=d_earn)
            if not qac or not qap:
                continue
            iv1 = (qac[2] + qap[2]) / 2.0

            # Second expiry, further out, also containing the event.
            exp2 = expiry_near(exp + timedelta(days=25), 30)
            if exp2 <= exp:
                continue
            T2 = max((exp2 - today).days, 1) / 365.0
            Ka2 = nearest_strike(px, 1.0)
            q2c = ctx.broker.option_quote(OptionContract(sym, "call", Ka2, exp2),
                                          ctx.regime["stress"], earnings_in=d_earn)
            q2p = ctx.broker.option_quote(OptionContract(sym, "put", Ka2, exp2),
                                          ctx.regime["stress"], earnings_in=d_earn)
            if not q2c or not q2p:
                continue
            iv2 = (q2c[2] + q2p[2]) / 2.0

            implied_move = self._implied_event_move(iv1, T, iv2, T2)
            if implied_move is None or implied_move >= 0.85 * hist_move:
                continue          # the market has already priced the event

            cost = qc[1] + qp[1]
            n = int((ctx.net_liq() * 0.01) // (cost * 100))
            if n < 1:
                continue
            f1 = ctx.broker.option_order(cc, n, self.name, leg_id=f"str-{sym}",
                                         note="strangle call",
                                         regime_stress=ctx.regime["stress"], earnings_in=d_earn)
            if not f1.ok:
                continue
            f2 = ctx.broker.option_order(cp, n, self.name, leg_id=f"str-{sym}",
                                         note="strangle put",
                                         regime_stress=ctx.regime["stress"], earnings_in=d_earn)
            if not f2.ok:
                ctx.broker.close_option(cc.key, self.name, "unwind unpaired leg",
                                        ctx.regime["stress"])
                continue
            ctx.risk.note_trade(self.name)
            self.state.setdefault("event", {})[f"str-{sym}"] = d_earn + ctx.day_number
            self.log(ctx, f"STRANGLE {n}x {sym} {Kp}P/{Kc}C {exp:%b%y} cost {cost:.4f} "
                          f"implied event move {implied_move:.1%} vs realized "
                          f"history {hist_move:.1%} (earnings in {d_earn}d)")
            return

    def _manage(self, ctx) -> None:
        for key, pos in list(self.my_option_positions(ctx).items()):
            due = self.state.get("event", {}).get(pos.leg_id)
            if due is not None and ctx.day_number > due and ctx.minute_index >= 30:
                ctx.broker.close_option(key, self.name, "post-earnings exit", ctx.regime["stress"])
            elif pos.contract.dte(ctx.date) <= 7:
                ctx.broker.close_option(key, self.name, "strangle theta exit", ctx.regime["stress"])
