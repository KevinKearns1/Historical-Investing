"""OP-03  Cash-Secured Puts / The Wheel on Old-Economy Large Caps.

Thesis. The forgotten half of 2000. While the Nasdaq halved, money rotated
hard into the value and defensive names that had been left for dead in 1999 --
consumer staples, pharma, financials, energy. Those names were unusually
stable, yet their options carried an index-wide volatility bid that had little
to do with their own realized vol. Selling that mispriced premium is the one
short-vol trade that belonged in a 2000 book.

Rules.
  Universe   low-beta large caps only, explicitly NOT the tech names the other
             strategies trade -- the point is diversification away from them.
  Filter     21-day realized vol below 45%, price above $20, and the name above
             its own 50-day average. Never sell puts under a broken chart.
  Structure  sell a ~30-delta put, 30-45 DTE, fully cash-secured. No naked
             leverage: the account must hold strike x 100 x contracts in cash.
  Exit       buy back at 50% of premium collected, or roll at 10 DTE.
  Assignment if assigned, take the stock and sell ~30-delta covered calls
             against it -- the wheel. Assignment is modelled, including early
             assignment when the put goes deep in the money.
  Screen     a point-in-time solvency check -- positive trailing EPS, positive
             free cash flow, non-negative net margin, debt/equity under 2 --
             read from the last filing that was PUBLIC on the trade date. A name
             with no filing yet is not rejected; see strategies/filters.py for
             why treating unknown as failure would smuggle survivorship bias
             back in.
  Honesty    this is the strategy most likely to look brilliant and then hand
             it all back in one week. The engine's per-trade cap exists
             precisely because short premium lies about its risk.
"""
from __future__ import annotations

from strategies.base import Strategy
from strategies.filters import SOLVENCY, ScreenStats
from engine.options import OptionContract, delta_strike, expiry_near, risk_free_rate


class CashSecuredPuts(Strategy):
    def __init__(self, universe: list[str] | None = None, **kw):
        super().__init__(name="OP-03 Cash-Secured Puts / Wheel", sleeve="options",
                         cadence=30, **kw)
        self.value_universe = universe or []
        self.screen = SOLVENCY
        self.screen_stats = ScreenStats()

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        self._wheel_calls(ctx)
        if not ctx.fire_once(self.name + ':csp', 120) or not ctx.risk.can_open(self.name):
            return
        shorts = [p for p in self.my_option_positions(ctx).values() if p.contracts < 0]
        if len(shorts) >= 2:
            return

        for sym in (self.value_universe or ctx.universe):
            if any(p.contract.symbol == sym for p in self.my_option_positions(ctx).values()):
                continue
            px = ctx.feed.last_price(sym)
            rv = ctx.feed.realized_vol(sym, 21)
            ma50 = ctx.feed.sma(sym, 50)
            if None in (px, rv, ma50) or px < 20 or rv > 0.45 or px < ma50:
                continue

            # Selling a put is agreeing to own the stock, so the balance sheet
            # gets a vote. Point-in-time: this reads the last filing that was
            # actually public today, never a later one.
            res = self.screen.check(ctx.kpis(sym))
            self.screen_stats.record(res)
            if not res:
                continue

            today = ctx.date
            exp = expiry_near(today, 38)
            T = max((exp - today).days, 1) / 365.0
            K = delta_strike(px, T, risk_free_rate(today), rv, 0.30, "put")
            c = OptionContract(sym, "put", K, exp)
            q = ctx.broker.option_quote(c, ctx.regime["stress"])
            if not q:
                continue
            bid = q[0]
            # Fully cash-secured: how many can the account actually cover?
            n = int(ctx.pf.cash * 0.35 // (K * 100))
            n = min(n, ctx.risk.contracts_for(ctx.net_liq(), max(bid, 0.05), 0.0))
            if n < 1 or bid < 0.25:
                continue
            fill = ctx.broker.option_order(c, -n, self.name, note="CSP",
                                           regime_stress=ctx.regime["stress"])
            if fill.ok:
                ctx.risk.note_trade(self.name)
                self.state.setdefault("collected", {})[c.key] = fill.price
                self.log(ctx, f"SELL {n}x {c} @ {fill.price:.4f} (secured {K*100*n:,.0f})")
                return

    def _manage(self, ctx) -> None:
        for key, pos in list(self.my_option_positions(ctx).items()):
            if pos.contracts >= 0:
                continue
            q = ctx.broker.option_quote(pos.contract, ctx.regime["stress"])
            if not q:
                continue
            ask = q[1]
            collected = self.state.get("collected", {}).get(key, pos.avg_premium)
            if ask <= collected * 0.50:
                ctx.broker.close_option(key, self.name, "CSP 50% profit", ctx.regime["stress"])
            elif pos.contract.dte(ctx.date) <= 10:
                ctx.broker.close_option(key, self.name, "CSP roll window", ctx.regime["stress"])

    def _wheel_calls(self, ctx) -> None:
        """Assigned shares get covered calls written against them."""
        if not ctx.fire_once(self.name + ':cc', 150):
            return
        for sym, pos in list(self.my_equity_positions(ctx).items()):
            if pos.shares < 100:
                continue
            if any(p.contract.symbol == sym and p.contract.kind == "call"
                   for p in self.my_option_positions(ctx).values()):
                continue
            px, rv = ctx.feed.last_price(sym), ctx.feed.realized_vol(sym, 21)
            if None in (px, rv):
                continue
            today = ctx.date
            exp = expiry_near(today, 38)
            T = max((exp - today).days, 1) / 365.0
            K = delta_strike(px, T, risk_free_rate(today), rv, 0.30, "call")
            c = OptionContract(sym, "call", K, exp)
            n = int(pos.shares // 100)
            if n < 1:
                continue
            fill = ctx.broker.option_order(c, -n, self.name, note="covered call",
                                           regime_stress=ctx.regime["stress"])
            if fill.ok:
                self.log(ctx, f"COVERED CALL {n}x {c} @ {fill.price:.4f}")

    def on_session_end(self, ctx) -> None:
        # Wheel stock is an intentional overnight hold -- do not flatten it.
        pass
