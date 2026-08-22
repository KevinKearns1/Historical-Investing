"""Strategy interface.

A strategy sees the world only through `ctx` (the point-in-time feed) and acts
only through `ctx.broker`. It has no access to the raw daily frame and no way
to reach a future timestamp -- the feed refuses.

Hooks, in the order the engine calls them each session:
    on_session_start(ctx)          once, 09:30
    on_bar(ctx)                    every minute the strategy asked to be woken
    on_session_end(ctx)            once, at 15:55 (flatten window)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Strategy:
    name: str = "unnamed"
    sleeve: str = "equity"          # "equity" | "options"
    capital: float = 0.0            # allocated sleeve capital
    enabled: bool = True
    state: dict = field(default_factory=dict)
    # Minute cadence: 1 = every minute, 5 = every 5th, etc.
    cadence: int = 1

    # -- lifecycle --------------------------------------------------------
    def on_session_start(self, ctx) -> None:
        pass

    def on_bar(self, ctx) -> None:
        pass

    def on_session_end(self, ctx) -> None:
        pass

    # -- helpers ----------------------------------------------------------
    def log(self, ctx, msg: str) -> None:
        ctx.log(f"[{self.name}] {msg}")

    def my_equity_positions(self, ctx) -> dict:
        return {s: p for s, p in ctx.pf.equities.items() if p.strategy == self.name}

    def my_option_positions(self, ctx) -> dict:
        return {k: p for k, p in ctx.pf.options.items() if p.strategy == self.name}

    def symbol_is_free(self, ctx, sym: str) -> bool:
        """True unless another strategy already holds this symbol.

        A real account nets per symbol -- it cannot be long and short the same
        stock at one broker. Without this guard, strategy B entering opposite
        to strategy A's position nets the position to flat: the fill reports
        ok, `ctx.pf.equities[sym]` no longer exists, and A's stop silently
        vanishes. Real 2000 data hit exactly this on SPY.

        This deliberately refuses a symbol the CALLING strategy already holds,
        not just one another strategy holds. A strategy resets its per-session
        "already traded this" set each morning, but a position whose closing
        order was REJECTED at the bell survives overnight -- so the next day it
        can signal the opposite way on a name it still owns and net itself to
        flat. That is the same corruption, self-inflicted, and it is why this
        is not `pos.strategy == self.name`. It showed up only on some runs:
        set iteration order varies between processes, so the trade sequence
        does too.
        """
        return ctx.pf.equities.get(sym) is None
