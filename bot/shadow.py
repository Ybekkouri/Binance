"""Shadow trading track: hypothetical trades that feed the learning loop.

The strict engine trades rarely — by design. That starves the dataset. The
shadow book fixes it: on candles where the strict engine says NO_TRADE but
the same factor logic at RELAXED thresholds would have fired, it opens a
virtual trade (no money anywhere, not even paper balance), follows it against
real prices with the same bracket rules (stop, partial TP1 + breakeven,
TP2, plus an age limit), and records the outcome into the datastore flagged
`shadow`. Outcomes are measured in R multiples (risk units), so they are
comparable across symbols and account sizes.

Shadow data accelerates factor research; it never influences execution.
"""

import json
import logging
import os
from datetime import datetime, timezone

from .decision import LONG, SHORT

log = logging.getLogger("bot.shadow")


def _now():
    return datetime.now(timezone.utc)


class ShadowBook:
    def __init__(self, cfg, datastore=None):
        self.cfg = cfg
        self.datastore = datastore
        self.book: dict = self._load()   # symbol -> open shadow trade

    # ---------- persistence ----------
    def _load(self) -> dict:
        path = self.cfg.shadow.state_file
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {}

    def _save(self) -> None:
        with open(self.cfg.shadow.state_file, "w") as f:
            json.dump(self.book, f, indent=1)

    # ---------- lifecycle ----------
    def consider(self, decision, decision_id=None) -> bool:
        """Open a shadow trade from a relaxed-threshold decision."""
        if not self.cfg.shadow.enabled:
            return False
        if decision.direction not in (LONG, SHORT):
            return False
        if decision.symbol in self.book:
            return False
        risk = abs(decision.entry_price - decision.stop_loss)
        if risk <= 0:
            return False
        self.book[decision.symbol] = {
            "side": "long" if decision.direction == LONG else "short",
            "entry": decision.entry_price,
            "stop": decision.stop_loss,
            "tp1": decision.take_profit_1,
            "tp2": decision.take_profit_2,
            "risk": risk,
            "tp1_filled": False,
            "realized_r": 0.0,
            "opened_ts": _now().isoformat(),
            "decision_id": decision_id,
            "confidence": decision.confidence,
        }
        self._save()
        log.info("[shadow] open %s %s @ %.2f (confidence %.2f)",
                 self.book[decision.symbol]["side"], decision.symbol,
                 decision.entry_price, decision.confidence)
        return True

    def update(self, symbol: str, price: float) -> None:
        """Check an open shadow trade against the current price."""
        t = self.book.get(symbol)
        if t is None:
            return
        sign = 1 if t["side"] == "long" else -1

        def hit_stop() -> bool:
            return price <= t["stop"] if sign > 0 else price >= t["stop"]

        def hit(level) -> bool:
            return price >= level if sign > 0 else price <= level

        frac = 0.5 if t["tp1_filled"] else 1.0
        if hit_stop():
            t["realized_r"] += frac * sign * (t["stop"] - t["entry"]) / t["risk"]
            self._close(symbol, "stop_loss" if not t["tp1_filled"] else "breakeven_stop")
        elif not t["tp1_filled"] and hit(t["tp1"]):
            t["realized_r"] += 0.5 * sign * (t["tp1"] - t["entry"]) / t["risk"]
            t["tp1_filled"] = True
            t["stop"] = t["entry"]           # breakeven, same as the real engine
            self._save()
        elif hit(t["tp2"]):
            t["realized_r"] += frac * sign * (t["tp2"] - t["entry"]) / t["risk"]
            self._close(symbol, "take_profit_2")
        else:
            age_h = (_now() - datetime.fromisoformat(t["opened_ts"])
                     ).total_seconds() / 3600
            if age_h >= self.cfg.shadow.max_age_hours:
                t["realized_r"] += frac * sign * (price - t["entry"]) / t["risk"]
                self._close(symbol, "max_age")

    def _close(self, symbol: str, reason: str) -> None:
        t = self.book.pop(symbol)
        self._save()
        log.info("[shadow] close %s %s: %+.2fR (%s)",
                 t["side"], symbol, t["realized_r"], reason)
        if self.datastore is not None:
            # pnl column carries R multiples for shadow rows
            self.datastore.record_trade(
                symbol, t["side"], t["opened_ts"], t["realized_r"], reason,
                decision_id=t.get("decision_id"), shadow=True,
            )
