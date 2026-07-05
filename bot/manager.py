"""Trade management for open positions.

Once a position exists, this module owns it:
  - moves the stop to breakeven at +breakeven_at_r,
  - trails the stop by ATR after TP1 is reached,
  - exits fully on a confident opposing signal (invalidation),
  - watches the liquidation price and flattens if it gets too close,
  - settles closed trades into the risk engine's counters.

The managed-position record lives in the risk state file so restarts
resume management seamlessly. Exchange-side brackets remain the safety
net at all times — this layer only ever tightens them.
"""

import logging
import math
import time
from datetime import datetime, timezone

from . import indicators as ta
from .decision import LONG, SHORT

log = logging.getLogger("bot.manager")

LIQ_ATR_BUFFER = 3.0  # flatten if liquidation is within this many ATRs


class TradeManager:
    def __init__(self, cfg, broker, risk, journal, datastore=None,
                 track: str = "real", notifier=None):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.datastore = datastore
        self.track = track
        self.is_shadow = track != "real"
        self.notifier = notifier

    def on_entry(self, decision, amount: float, equity: float,
                 decision_id=None, sl_order_id=None) -> None:
        info = {
            "side": "long" if decision.direction == LONG else "short",
            "entry": decision.entry_price,
            "stop": decision.stop_loss,
            "initial_stop": decision.stop_loss,
            "tp1": decision.take_profit_1,
            "tp2": decision.take_profit_2,
            "amount": amount,
            "risk_dist": abs(decision.entry_price - decision.stop_loss),
            "breakeven_done": False,
            "opened_ms": int(time.time() * 1000),
            "decision_id": decision_id,
            "sl_order_id": sl_order_id,
            "settle_attempts": 0,
        }
        self.risk.record_open(decision.symbol, info, equity)
        self.journal.position("opened", decision.symbol, track=self.track, **info)

    def manage(self, snap, opposing_decision=None, position="fetch") -> None:
        """Run one management pass for a symbol with an open position.
        The caller may pass the already-fetched position dict (or None if
        flat) to avoid a redundant API call."""
        symbol = snap.symbol
        info = self.risk.state.managed.get(symbol)
        if position == "fetch":
            position = next(
                (p for p in self.broker.open_positions()
                 if p["symbol"] == symbol), None
            )

        if position is None:
            if info is not None:
                self._settle(symbol, info, reason=self._exit_reason(symbol))
            return
        if info is None:
            # Position exists that we have no record of (manual trade, or a
            # crash between entry and record). We won't touch it, but the
            # operator must know it has no bot-side management.
            log.warning("Unmanaged position on %s — leaving it alone.", symbol)
            if self.notifier is not None:
                self.notifier.once(
                    f"unmanaged-{symbol}",
                    f"⚠️ Unmanaged {position['side']} position detected on "
                    f"{symbol} ({position['contracts']} contracts). The bot "
                    "will NOT manage it — verify its stop-loss on Binance, "
                    "or close it manually.")
            return

        price = snap.last_price
        side = info["side"]
        sign = 1 if side == "long" else -1
        atr = float(ta.atr(snap.candles, self.cfg.strategy.atr_period).iloc[-1])
        r_gain = sign * (price - info["entry"]) / info["risk_dist"]

        # 1. Liquidation proximity: flatten rather than flirt with liquidation.
        liq = position.get("liquidation_price") or 0
        if liq > 0 and abs(price - liq) < LIQ_ATR_BUFFER * atr:
            log.warning("%s liquidation price %.2f within %.0f ATRs — closing.",
                        symbol, liq, LIQ_ATR_BUFFER)
            self.journal.position("liquidation_guard_close", symbol,
                                  price=price, liquidation=liq)
            self.broker.close_position(symbol)
            self._settle(symbol, info, reason="liquidation_guard")
            return

        # 2. Invalidation: confident opposing signal closes the trade early.
        if (self.cfg.exits.exit_on_opposing_signal and opposing_decision is not None
                and opposing_decision.direction in (LONG, SHORT)):
            opposite = (opposing_decision.direction == SHORT) == (side == "long")
            if opposite:
                log.info("%s opposing %s signal (confidence %.2f) — invalidation exit.",
                         symbol, opposing_decision.direction,
                         opposing_decision.confidence)
                self.journal.position("invalidation_exit", symbol,
                                      confidence=opposing_decision.confidence)
                self.broker.close_position(symbol)
                self._settle(symbol, info, reason="invalidation")
                return

        # 3. Breakeven: once +breakeven_at_r, stop goes to entry.
        new_stop = None
        if not info["breakeven_done"] and r_gain >= self.cfg.exits.breakeven_at_r:
            new_stop = info["entry"]
            info["breakeven_done"] = True

        # 4. Trailing: after TP1 territory, ratchet the stop by ATR.
        if r_gain >= self.cfg.exits.tp1_r:
            trail = price - sign * atr * self.cfg.exits.trail_atr_mult
            better = trail > info["stop"] if side == "long" else trail < info["stop"]
            min_step = info["stop"] * self.cfg.exits.trail_min_step_pct / 100
            if better and abs(trail - info["stop"]) >= min_step:
                new_stop = trail if new_stop is None else \
                    (max(new_stop, trail) if side == "long" else min(new_stop, trail))

        if new_stop is not None and not math.isclose(new_stop, info["stop"]):
            new_id = self.broker.replace_stop(
                symbol, side, new_stop,
                old_order_id=info.get("sl_order_id"),
                old_stop=info["stop"])
            self.journal.position("stop_moved", symbol,
                                  old_stop=info["stop"], new_stop=new_stop,
                                  r_gain=round(r_gain, 2))
            info["stop"] = new_stop
            info["sl_order_id"] = new_id
            self.risk.save()

    def _exit_reason(self, symbol: str) -> str:
        """Best-effort exit reason: paper broker knows exactly; live infers
        that an exchange-side bracket did the closing."""
        if hasattr(self.broker, "last_exit_reason"):
            return self.broker.last_exit_reason(symbol) or "bracket"
        return "bracket"

    def _settle(self, symbol: str, info: dict, reason: str = "bracket") -> None:
        """Position is gone: cancel leftovers, book the PnL, update counters.

        A failed PnL fetch is retried on later ticks rather than silently
        booked as zero — booking 0 would blind the daily loss limit."""
        self.broker.cancel_all(symbol)
        try:
            pnl = self.broker.realized_pnl_since(symbol, info["opened_ms"])
        except Exception:
            info["settle_attempts"] = info.get("settle_attempts", 0) + 1
            if info["settle_attempts"] < 5:
                log.warning("PnL fetch failed for %s (attempt %d) — will retry.",
                            symbol, info["settle_attempts"])
                self.risk.save()
                return
            log.exception("PnL fetch failed 5x for %s; recording 0.", symbol)
            self.journal.error("settle", f"pnl fetch failed 5x for {symbol}")
            if self.notifier is not None:
                self.notifier.send(
                    f"⚠️ Could not fetch realized PnL for {symbol} after 5 "
                    "attempts — recorded as 0. Daily loss tracking may be "
                    "off; check the account manually.")
            pnl = 0.0
        equity = self.broker.equity_usdt()
        self.risk.record_close(symbol, pnl, equity)
        self.journal.position(
            "closed", symbol, pnl=pnl, equity=equity, exit_reason=reason,
            track=self.track,
            daily_pnl=self.risk.state.daily_pnl,
            consecutive_losses=self.risk.state.consecutive_losses,
        )
        if self.datastore is not None:
            opened_ts = datetime.fromtimestamp(
                info["opened_ms"] / 1000, tz=timezone.utc).isoformat()
            self.datastore.record_trade(
                symbol, info["side"], opened_ts, pnl, reason,
                decision_id=info.get("decision_id"), shadow=self.is_shadow,
            )
        log.info("[%s] %s closed (%s): %+.2f USDT | today %+.2f | equity %.2f",
                 self.track, symbol, reason, pnl,
                 self.risk.state.daily_pnl, equity)
        self._notify_close(symbol, pnl, reason, equity)

    def _notify_close(self, symbol: str, pnl: float, reason: str,
                      equity: float) -> None:
        if self.notifier is None or (
                self.is_shadow and not self.cfg.telegram_notify_shadow):
            return
        tag = " [shadow]" if self.is_shadow else ""
        emoji = "✅" if pnl > 0 else "🔻"
        state = self.risk.state
        self.notifier.send(
            f"{emoji} Closed {symbol}{tag}: {pnl:+.2f} USDT ({reason})\n"
            f"today {state.daily_pnl:+.2f} over {state.trades_today} trade(s) "
            f"| equity {equity:.2f}")
        # one-time daily halt alerts
        base = state.equity_day_start
        rk = self.cfg.risk
        if base > 0 and rk.daily_profit_target_pct > 0 and \
                state.daily_pnl >= base * rk.daily_profit_target_pct / 100:
            self.notifier.once(
                f"{self.track}-target-{state.date}",
                f"🎯 Daily profit target reached{tag} "
                f"({state.daily_pnl:+.2f} USDT). Done for the day.")
        if base > 0 and state.daily_pnl <= -base * rk.daily_max_loss_pct / 100:
            self.notifier.once(
                f"{self.track}-maxloss-{state.date}",
                f"🛑 Daily loss limit hit{tag} ({state.daily_pnl:+.2f} USDT). "
                "No more trades until tomorrow (UTC).")
        if state.consecutive_losses >= rk.max_consecutive_losses:
            self.notifier.once(
                f"{self.track}-streak-{state.date}",
                f"⏸️ {state.consecutive_losses} consecutive losses{tag} — "
                "cooling off for the rest of the day.")
