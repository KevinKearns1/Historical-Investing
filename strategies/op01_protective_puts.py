"""OP-01  Directional Long Puts on Extended High-Beta Names.

Thesis. The convex trade of 2000. A high-beta Nasdaq name stretched far above
its own trend, in a market whose trend has turned down, has an asymmetric
distribution: bounded upside on the short thesis, unbounded downside in the
stock. Buying the put rather than shorting the stock sidesteps the uptick rule,
caps the loss at the premium, and pays for the gap risk that destroyed outright
shorts on every squeeze.

Rules.
  Regime     armed only when the index is below its 20-day average.
  Signal     name trades more than 1.5 standard deviations above its 50-day
             average, on 21-day realized vol above 55%.
  Structure  long ~25-delta put, 30-45 DTE. Monthly expiries only -- weeklys
             did not exist until 2005.
  Size       at most 2% of net liq of premium per trade, 10% open at a time.
  Exit       +100% premium, -50% premium, or 10 days to expiry -- gamma is
             seductive and theta into expiry is what actually kills long
             option books.
  Cost note  every fill crosses a modelled 2000-era options spread, which on a
             60-vol Nasdaq name was frequently 10% of the premium.
"""
from __future__ import annotations

from strategies.base import Strategy
from engine.options import OptionContract, delta_strike, expiry_near, risk_free_rate


class ProtectiveLongPuts(Strategy):
    def __init__(self, benchmark: str = "^IXIC", **kw):
        super().__init__(name="OP-01 Long Puts (extended high-beta)", sleeve="options",
                         cadence=30, **kw)
        self.benchmark = benchmark

    def on_bar(self, ctx) -> None:
        self._manage(ctx)
        if not ctx.fire_once(self.name, 60) or not ctx.risk.can_open(self.name):
            return
        c = ctx.feed.last_close(self.benchmark)
        m20 = ctx.feed.sma(self.benchmark, 20)
        if c is None or m20 is None or c >= m20:
            return
        if len(self.my_option_positions(ctx)) >= 3:
            return

        for sym in ctx.universe:
            h = ctx.feed.history(sym, 60)
            rv = ctx.feed.realized_vol(sym, 21)
            px = ctx.feed.last_price(sym)
            if len(h) < 50 or rv is None or px is None or rv < 0.55:
                continue
            ma50 = float(h["close"].tail(50).mean())
            sd = float(h["close"].tail(50).std(ddof=1))
            if sd <= 0 or (px - ma50) / sd < 1.5:
                continue

            today = ctx.date
            exp = expiry_near(today, 38)
            T = max((exp - today).days, 1) / 365.0
            K = delta_strike(px, T, risk_free_rate(today), rv, 0.25, "put")
            c_ = OptionContract(sym, "put", K, exp)
            q = ctx.broker.option_quote(c_, ctx.regime["stress"])
            if not q:
                continue
            _, ask, _ = q
            n = ctx.risk.contracts_for(ctx.net_liq(), ask, ctx.open_option_premium())
            if n < 1:
                continue
            fill = ctx.broker.option_order(c_, n, self.name, note="long put",
                                           regime_stress=ctx.regime["stress"])
            if fill.ok:
                ctx.risk.note_trade(self.name)
                self.log(ctx, f"BUY {n}x {c_} @ {fill.price:.4f} (spot {px:.2f}, rv {rv:.0%})")
                return

    def _manage(self, ctx) -> None:
        for key, pos in list(self.my_option_positions(ctx).items()):
            q = ctx.broker.option_quote(pos.contract, ctx.regime["stress"])
            if not q:
                continue
            bid, _, _ = q
            dte = pos.contract.dte(ctx.date)
            if bid >= pos.avg_premium * 2.0:
                ctx.broker.close_option(key, self.name, "put +100%", ctx.regime["stress"])
            elif bid <= pos.avg_premium * 0.5:
                ctx.broker.close_option(key, self.name, "put -50%", ctx.regime["stress"])
            elif dte <= 10:
                ctx.broker.close_option(key, self.name, "put theta exit", ctx.regime["stress"])
