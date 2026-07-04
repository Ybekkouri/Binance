"""Risk engine: portfolio-level limits that override everything else.

All limits are ratios of account equity so they scale with capital. Counters
(daily PnL, weekly drawdown, consecutive losses, trades per day) persist to
the state file so a restart cannot reset them.

Two layers of checks:
  - account_checks(): daily loss / profit target, weekly drawdown,
    consecutive losses, trades per day, open position count, exposure caps.
  - market_checks(): spread, 24h liquidity, order book depth vs order size.

Any failed check is a NO TRADE, and every failure reason is returned so the
journal can record exactly why.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _week() -> str:
    iso = _now().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class RiskState:
    date: str = field(default_factory=_today)
    week: str = field(default_factory=_week)
    daily_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    equity_day_start: float = 0.0
    equity_week_start: float = 0.0
    managed: dict = field(default_factory=dict)  # symbol -> managed position info


class RiskEngine:
    def __init__(self, cfg, state_file: str = None):
        self.cfg = cfg
        self.state_file = state_file or cfg.state_file
        self.state = self._load()

    # ---------- persistence ----------
    def _load(self) -> RiskState:
        if os.path.isfile(self.state_file):
            try:
                with open(self.state_file) as f:
                    return RiskState(**json.load(f))
            except (json.JSONDecodeError, TypeError):
                pass
        return RiskState()

    def save(self) -> None:
        with open(self.state_file, "w") as f:
            json.dump(asdict(self.state), f, indent=1)

    # ---------- day / week rolling ----------
    def roll(self, equity: float) -> None:
        if self.state.date != _today():
            self.state.date = _today()
            self.state.daily_pnl = 0.0
            self.state.trades_today = 0
            self.state.equity_day_start = 0.0
            # loss-streak cooldown lasts the rest of the day, not forever —
            # otherwise a maxed streak could never be broken (no trades, no win)
            self.state.consecutive_losses = 0
        if self.state.week != _week():
            self.state.week = _week()
            self.state.equity_week_start = 0.0
        if self.state.equity_day_start <= 0 and equity > 0:
            self.state.equity_day_start = equity
        if self.state.equity_week_start <= 0 and equity > 0:
            self.state.equity_week_start = equity
        self.save()

    def record_close(self, symbol: str, pnl: float, equity: float) -> None:
        self.roll(equity)
        self.state.daily_pnl += pnl
        self.state.consecutive_losses = 0 if pnl > 0 else self.state.consecutive_losses + 1
        self.state.managed.pop(symbol, None)
        self.save()

    def record_open(self, symbol: str, managed_info: dict, equity: float) -> None:
        self.roll(equity)
        self.state.trades_today += 1
        self.state.managed[symbol] = managed_info
        self.save()

    # ---------- account-level checks ----------
    def account_checks(self, equity: float, open_positions: list,
                       new_notional: float, symbol: str) -> list[str]:
        """Return failure reasons; empty list means clear to trade."""
        rk = self.cfg.risk
        self.roll(equity)
        day_base = self.state.equity_day_start
        week_base = self.state.equity_week_start
        fails = []

        if equity <= 0:
            fails.append("no equity")
            return fails

        if self.state.daily_pnl <= -day_base * rk.daily_max_loss_pct / 100:
            fails.append(
                f"daily loss limit hit ({self.state.daily_pnl:+.2f} USDT, "
                f"limit -{day_base * rk.daily_max_loss_pct / 100:.2f})"
            )
        if rk.daily_profit_target_pct > 0 and \
                self.state.daily_pnl >= day_base * rk.daily_profit_target_pct / 100:
            fails.append(
                f"daily profit target reached ({self.state.daily_pnl:+.2f} USDT) — "
                "banking the day"
            )
        dd = (week_base - equity) / week_base * 100 if week_base > 0 else 0
        if dd >= rk.weekly_max_drawdown_pct:
            fails.append(f"weekly drawdown {dd:.2f}% >= {rk.weekly_max_drawdown_pct}%")
        if self.state.consecutive_losses >= rk.max_consecutive_losses:
            fails.append(
                f"{self.state.consecutive_losses} consecutive losses — cooling off"
            )
        if self.state.trades_today >= rk.max_trades_per_day:
            fails.append(f"max trades per day ({rk.max_trades_per_day}) reached")
        if len(open_positions) >= rk.max_open_positions:
            fails.append(f"max open positions ({rk.max_open_positions}) reached")
        if any(p["symbol"] == symbol for p in open_positions):
            fails.append(f"position already open in {symbol} — no scaling/averaging")

        total_notional = sum(abs(p["notional"]) for p in open_positions) + new_notional
        if total_notional > equity * rk.max_total_exposure_mult:
            fails.append(
                f"total exposure {total_notional:.0f} would exceed "
                f"{rk.max_total_exposure_mult}x equity"
            )
        if new_notional > equity * rk.max_symbol_exposure_mult:
            fails.append(
                f"symbol exposure {new_notional:.0f} would exceed "
                f"{rk.max_symbol_exposure_mult}x equity"
            )
        return fails

    # ---------- market-quality checks ----------
    def market_checks(self, snap, order_amount: float) -> list[str]:
        rk = self.cfg.risk
        fails = []
        if snap.quote_volume_24h < rk.min_quote_volume_24h:
            fails.append(
                f"24h volume {snap.quote_volume_24h:,.0f} below "
                f"{rk.min_quote_volume_24h:,.0f} USDT"
            )
        if snap.book is not None:
            if snap.book.spread_pct > rk.max_spread_pct:
                fails.append(
                    f"spread {snap.book.spread_pct:.3f}% above {rk.max_spread_pct}%"
                )
            depth = min(snap.book.bid_depth, snap.book.ask_depth)
            if depth < order_amount * rk.min_book_depth_mult:
                fails.append(
                    f"book depth {depth:.4f} below "
                    f"{rk.min_book_depth_mult}x order size ({order_amount:.4f})"
                )
        return fails

    # ---------- sizing ----------
    def position_size(self, equity: float, entry: float, stop: float,
                      open_positions: list) -> float:
        return size_position(self.cfg, equity, entry, stop, open_positions)


def size_position(cfg, equity: float, entry: float, stop: float,
                  open_positions: list) -> float:
    """Contracts such that hitting the stop (plus slippage) loses
    risk_per_trade_pct of equity, capped by exposure and margin limits.
    Shared by the live engine and the backtester."""
    rk = cfg.risk
    stop_dist = abs(entry - stop) + entry * cfg.slippage_pct / 100
    if stop_dist <= 0 or equity <= 0:
        return 0.0
    risk_usdt = equity * rk.risk_per_trade_pct / 100
    amount = risk_usdt / stop_dist

    used = sum(abs(p["notional"]) for p in open_positions)
    room_total = max(0.0, equity * rk.max_total_exposure_mult - used)
    caps = [
        equity * rk.max_symbol_exposure_mult / entry,
        room_total / entry,
        equity * cfg.leverage * 0.95 / entry,  # margin with 5% buffer
    ]
    return max(0.0, min([amount] + caps))
