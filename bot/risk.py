"""Risk management: position sizing and daily profit/loss limits.

The daily limits are the heart of the "50 USD a day" goal: once the day's
realized PnL reaches the profit target the bot stops opening new positions
until the next UTC day, and it does the same after hitting the max daily
loss so one bad day can't spiral.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class DailyState:
    date: str = field(default_factory=_today)
    realized_pnl: float = 0.0
    trades: int = 0


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = self._load()

    # ---- persistence (survives restarts) ----
    def _load(self) -> DailyState:
        if os.path.isfile(self.cfg.state_file):
            try:
                with open(self.cfg.state_file) as f:
                    raw = json.load(f)
                state = DailyState(**raw)
                if state.date == _today():
                    return state
            except (json.JSONDecodeError, TypeError):
                pass
        return DailyState()

    def _save(self) -> None:
        with open(self.cfg.state_file, "w") as f:
            json.dump(self.state.__dict__, f)

    # ---- daily limits ----
    def _roll_day(self) -> None:
        if self.state.date != _today():
            self.state = DailyState()
            self._save()

    def record_trade(self, pnl: float) -> None:
        self._roll_day()
        self.state.realized_pnl += pnl
        self.state.trades += 1
        self._save()

    def can_trade(self) -> tuple[bool, str]:
        self._roll_day()
        if self.state.realized_pnl >= self.cfg.daily_profit_target:
            return False, (
                f"Daily profit target reached (+{self.state.realized_pnl:.2f} USDT). "
                "Done for the day."
            )
        if self.state.realized_pnl <= -self.cfg.daily_max_loss:
            return False, (
                f"Daily max loss hit ({self.state.realized_pnl:.2f} USDT). "
                "Stopping until tomorrow."
            )
        return True, ""

    # ---- position sizing ----
    def position_size(self, equity: float, entry: float, stop: float) -> float:
        """Contracts to buy/sell so that hitting the stop loses risk_per_trade_pct
        of equity. Capped by max_position_notional and available margin."""
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or equity <= 0:
            return 0.0
        risk_usdt = equity * self.cfg.risk_per_trade_pct / 100.0
        amount = risk_usdt / stop_dist

        max_by_notional = self.cfg.max_position_notional / entry
        max_by_margin = equity * self.cfg.leverage * 0.95 / entry  # 5% buffer for fees
        return max(0.0, min(amount, max_by_notional, max_by_margin))
