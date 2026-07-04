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
                 track: str = "real"):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.datastore = datastore
        self.track = track
        self.is_shadow = track != "real"

    def on_entry(self, decision, amount: float, equity: float,
                 decision_id=None) -> None:
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
        }
        self.risk.record_open(decision.symbol, info, equity)
        self.journal.position("opened", decision.symbol, track=self.track, **info)

    def manage(self, snap, opposing_decision=None) -> None:
        """Run one management pass for a symbol with an open position."""
        symbol = snap.symbol
        info = self.risk.state.managed.get(symbol)
        position = next(
            (p for p in self.broker.open_positions() if p["symbol"] == symbol), None
        )

        if position is None:
            if info is not None:
                self._settle(symbol, info, reason=self._exit_reason(symbol))
            return
        if info is None:
            # Position exists that we have no record of (e.g. manual trade).
            log.warning("Unmanaged position on %s — leaving it alone.", symbol)
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
            self.broker.replace_stop(symbol, side, new_stop)
            self.journal.position("stop_moved", symbol,
                                  old_stop=info["stop"], new_stop=new_stop,
                                  r_gain=round(r_gain, 2))
            info["stop"] = new_stop
            self.risk.save()

    def _exit_reason(self, symbol: str) -> str:
        """Best-effort exit reason: paper broker knows exactly; live infers
        that an exchange-side bracket did the closing."""
        if hasattr(self.broker, "last_exit_reason"):
            return self.broker.last_exit_reason(symbol) or "bracket"
        return "bracket"

    def _settle(self, symbol: str, info: dict, reason: str = "bracket") -> None:
        """Position is gone: cancel leftovers, book the PnL, update counters."""
        self.broker.cancel_all(symbol)
        try:
            pnl = self.broker.realized_pnl_since(symbol, info["opened_ms"])
        except Exception:
            log.exception("Could not fetch realized PnL for %s; recording 0.", symbol)
            self.journal.error("settle", f"pnl fetch failed for {symbol}")
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
