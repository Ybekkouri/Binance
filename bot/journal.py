"""Structured audit journal: one JSON line per event.

Everything the bot decides or does is appended here — market snapshots,
decisions (including NO_TRADE), risk-check failures, orders, position
updates, exits, PnL, and errors — so any trade can be reconstructed later.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("bot.journal")


class Journal:
    def __init__(self, path: str):
        self.path = path

    def write(self, event: str, **data) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError:
            log.exception("Failed to write journal event %s", event)

    # Convenience wrappers keep call sites terse and event names consistent.
    def decision(self, decision, snapshot_summary: dict) -> None:
        self.write("decision", decision=decision.to_dict(), market=snapshot_summary)

    def risk_block(self, symbol: str, reasons: list) -> None:
        self.write("risk_block", symbol=symbol, reasons=reasons)

    def order(self, kind: str, symbol: str, request: dict, response=None) -> None:
        self.write("order", kind=kind, symbol=symbol, request=request,
                   response=response)

    def position(self, action: str, symbol: str, **data) -> None:
        self.write("position", action=action, symbol=symbol, **data)

    def error(self, where: str, message: str) -> None:
        self.write("error", where=where, message=message)
