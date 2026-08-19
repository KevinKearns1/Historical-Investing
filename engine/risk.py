"""Position sizing and the limits that keep a $25,000 account alive.

The single most important number in a 2000 backtest is not the entry signal,
it is the loss limit. A high-beta long book in that year could give back a
year of gains in a fortnight, so risk here is enforced by the engine, not
left to the strategies' good intentions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RiskLimits:
    risk_per_trade: float = 0.010        # 1% of net liq at the stop
    max_equity_position: float = 0.25    # 25% of net liq in one name
    max_gross_leverage: float = 2.0      # Reg T, 2000 rules
    max_option_premium_per_trade: float = 0.02
    max_option_premium_open: float = 0.10
    daily_loss_limit: float = 0.03       # stop trading for the day at -3%
    weekly_loss_limit: float = 0.06
    max_drawdown_stop: float = 0.20      # halt the whole program at -20%
    max_concurrent_equity: int = 5
    max_new_trades_per_day: int = 12
    max_new_trades_per_strategy_per_day: int = 3


@dataclass
class RiskManager:
    limits: RiskLimits = field(default_factory=RiskLimits)
    high_water: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    trades_today: int = 0
    trades_by_strategy: dict = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""
    _day: date | None = None
    blocked_today: bool = False

    def start_day(self, d: date, net_liq: float) -> None:
        self._day = d
        self.day_start_equity = net_liq
        self.trades_today = 0
        self.trades_by_strategy = {}
        self.blocked_today = False
        if self.high_water == 0.0:
            self.high_water = net_liq
            self.week_start_equity = net_liq
        if d.weekday() == 0:
            self.week_start_equity = net_liq

    def update(self, net_liq: float) -> None:
        self.high_water = max(self.high_water, net_liq)
        if self.high_water > 0 and net_liq <= self.high_water * (1 - self.limits.max_drawdown_stop):
            self.halted = True
            self.halt_reason = (
                f"program drawdown stop: {net_liq:,.0f} is "
                f"{(1 - net_liq / self.high_water):.1%} off the {self.high_water:,.0f} high"
            )
        if self.day_start_equity > 0 and net_liq <= self.day_start_equity * (1 - self.limits.daily_loss_limit):
            self.blocked_today = True
        if self.week_start_equity > 0 and net_liq <= self.week_start_equity * (1 - self.limits.weekly_loss_limit):
            self.blocked_today = True

    def can_open(self, strategy: str | None = None) -> bool:
        if self.halted or self.blocked_today:
            return False
        if self.trades_today >= self.limits.max_new_trades_per_day:
            return False
        if strategy is not None:
            used = self.trades_by_strategy.get(strategy, 0)
            if used >= self.limits.max_new_trades_per_strategy_per_day:
                return False
        return True

    def note_trade(self, strategy: str | None = None) -> None:
        self.trades_today += 1
        if strategy is not None:
            self.trades_by_strategy[strategy] = self.trades_by_strategy.get(strategy, 0) + 1

    # -- sizing -----------------------------------------------------------
    def shares_for(self, net_liq: float, entry: float, stop: float,
                   sleeve_capital: float | None = None) -> int:
        """Size so that a stop-out costs exactly `risk_per_trade` of net liq."""
        risk_ps = abs(entry - stop)
        if risk_ps <= 1e-6 or entry <= 0:
            return 0
        by_risk = (net_liq * self.limits.risk_per_trade) / risk_ps
        by_conc = (net_liq * self.limits.max_equity_position) / entry
        cap = by_conc if sleeve_capital is None else min(by_conc, sleeve_capital / entry)
        return int(max(min(by_risk, cap), 0))

    def contracts_for(self, net_liq: float, premium: float, open_premium: float) -> int:
        if premium <= 0:
            return 0
        per_trade = net_liq * self.limits.max_option_premium_per_trade
        headroom = max(net_liq * self.limits.max_option_premium_open - open_premium, 0.0)
        budget = min(per_trade, headroom)
        return int(budget // (premium * 100))
