"""Risk management: position sizing and daily profit/loss limits.

All limits are expressed as percentages of account equity, so they scale
automatically with the capital in the account. Daily limits are computed
against an equity snapshot taken at the first check of each UTC day (not
against live equity, so the goalposts don't move intraday): once the day's
realized PnL reaches +daily_profit_target_pct of that snapshot the bot stops
opening positions until the next UTC day, and it does the same after losing
daily_max_loss_pct so one bad day can't spiral.
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
    equity_start: float = 0.0  # equity snapshot at the start of the day


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
    def _roll_day(self, equity: float) -> None:
        if self.state.date != _today():
            self.state = DailyState()
        if self.state.equity_start <= 0 and equity > 0:
            self.state.equity_start = equity
            self._save()

    def daily_limits(self) -> tuple[float, float]:
        """(profit_target, max_loss) in USDT for today, from the day-start
        equity snapshot."""
        base = self.state.equity_start
        return (
            base * self.cfg.daily_profit_target_pct / 100.0,
            base * self.cfg.daily_max_loss_pct / 100.0,
        )

    def record_trade(self, pnl: float, equity: float) -> None:
        self._roll_day(equity)
        self.state.realized_pnl += pnl
        self.state.trades += 1
        self._save()

    def can_trade(self, equity: float) -> tuple[bool, str]:
        self._roll_day(equity)
        target, max_loss = self.daily_limits()
        if target <= 0:
            return False, "No equity snapshot yet — cannot compute daily limits."
        if self.state.realized_pnl >= target:
            return False, (
                f"Daily profit target reached ({self.state.realized_pnl:+.2f} / "
                f"target {target:.2f} USDT). Done for the day."
            )
        if self.state.realized_pnl <= -max_loss:
            return False, (
                f"Daily max loss hit ({self.state.realized_pnl:+.2f} / "
                f"limit -{max_loss:.2f} USDT). Stopping until tomorrow."
            )
        return True, ""

    # ---- position sizing ----
    def position_size(self, equity: float, entry: float, stop: float) -> float:
        """Contracts to buy/sell so that hitting the stop loses risk_per_trade_pct
        of equity. Capped by max notional (a multiple of equity) and by
        available margin."""
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or equity <= 0:
            return 0.0
        risk_usdt = equity * self.cfg.risk_per_trade_pct / 100.0
        amount = risk_usdt / stop_dist

        max_by_notional = equity * self.cfg.max_position_notional_mult / entry
        max_by_margin = equity * self.cfg.leverage * 0.95 / entry  # 5% buffer for fees
        return max(0.0, min(amount, max_by_notional, max_by_margin))
