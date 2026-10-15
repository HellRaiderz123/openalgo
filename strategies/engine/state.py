"""
state.py
--------
State manager — tracks position across the trading session.
Prevents double entry, tracks MTM, enforces stop/target/daily limits.
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import date
from .builder import TradeSpec, Leg


@dataclass
class PositionState:
    in_trade: bool = False
    squared_off: bool = False
    strategy: str = ""
    legs: List[Leg] = field(default_factory=list)
    entry_premium: float = 0.0   # total premium collected at entry
    current_premium: float = 0.0
    mtm_pnl: float = 0.0
    daily_pnl: float = 0.0
    session_date: Optional[date] = None

    def reset_for_new_day(self, today: date):
        self.in_trade     = False
        self.squared_off  = False
        self.strategy     = ""
        self.legs         = []
        self.entry_premium  = 0.0
        self.current_premium = 0.0
        self.mtm_pnl      = 0.0
        self.daily_pnl    = 0.0
        self.session_date = today

    def enter(self, spec: TradeSpec, entry_premium: float):
        self.in_trade      = True
        self.strategy      = spec.strategy
        self.legs          = spec.legs
        self.entry_premium = entry_premium

    def update_mtm(self, current_premium: float, qty: int):
        self.current_premium = current_premium
        # for short strategies: profit = entry - current (premium decays)
        self.mtm_pnl  = round((self.entry_premium - current_premium) * qty, 2)
        self.daily_pnl = self.mtm_pnl

    def exit(self):
        self.in_trade    = False
        self.squared_off = True

    def should_stop(self, stop_loss_inr: float) -> bool:
        return self.mtm_pnl <= -abs(stop_loss_inr)

    def should_target(self, target_inr: float) -> bool:
        return self.mtm_pnl >= target_inr

    def daily_limit_hit(self, daily_limit_inr: float) -> bool:
        return self.daily_pnl <= -abs(daily_limit_inr)
