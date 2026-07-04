"""Research datastore: every snapshot, decision and outcome, queryable.

The journal (JSONL) is the audit trail; this SQLite store is the *dataset* —
structured so the research tooling can join decisions to their outcomes and
measure which factors actually predict wins. The bot keeps feeding it while
it runs (in every mode, including paper), so the dataset grows even on days
with zero trades.
"""

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT, symbol TEXT, candle_time TEXT,
    last_price REAL, quote_volume_24h REAL,
    funding_rate REAL, oi_change_pct REAL,
    spread_pct REAL, book_imbalance REAL,
    long_short_ratio REAL, taker_buy_sell_ratio REAL,
    btc_trend INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER REFERENCES snapshots(id),
    ts TEXT, symbol TEXT, direction TEXT,
    confidence REAL, risk_reward REAL, market_condition TEXT,
    entry REAL, stop REAL, tp1 REAL, tp2 REAL,
    votes TEXT,          -- JSON: [{name, vote, weight, detail}, ...]
    reasons TEXT,        -- JSON list
    executed INTEGER DEFAULT 0,
    shadow INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions(id),
    symbol TEXT, side TEXT,
    opened_ts TEXT, closed_ts TEXT,
    pnl REAL, exit_reason TEXT,
    shadow INTEGER DEFAULT 0   -- 1: virtual learning trade, pnl is in R units
);
CREATE INDEX IF NOT EXISTS idx_dec_symbol_ts ON decisions(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_snap_symbol_ts ON snapshots(symbol, ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DataStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a table already existed on disk."""
        for table in ("decisions", "trades"):
            cols = [r[1] for r in
                    self.conn.execute(f"PRAGMA table_info({table})")]
            if "shadow" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN shadow INTEGER DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    # ---- writers ----
    def record_snapshot(self, snap) -> int:
        s = snap.summary()
        cur = self.conn.execute(
            """INSERT INTO snapshots
               (ts, symbol, candle_time, last_price, quote_volume_24h,
                funding_rate, oi_change_pct, spread_pct, book_imbalance,
                long_short_ratio, taker_buy_sell_ratio, btc_trend)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), s["symbol"], s["candle_time"], s["last_price"],
             s["quote_volume_24h"], s["funding_rate"], s["oi_change_pct"],
             s["spread_pct"], s["book_imbalance"],
             s.get("long_short_ratio"), s.get("taker_buy_sell_ratio"),
             s["btc_trend"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_decision(self, decision, snapshot_id: int,
                        shadow: bool = False) -> int:
        d = decision.to_dict()
        cur = self.conn.execute(
            """INSERT INTO decisions
               (snapshot_id, ts, symbol, direction, confidence, risk_reward,
                market_condition, entry, stop, tp1, tp2, votes, reasons, shadow)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot_id, d["timestamp"] or _now(), d["symbol"], d["direction"],
             d["confidence"], d["risk_reward"], d["market_condition"],
             d["entry_price"], d["stop_loss"], d["take_profit_1"],
             d["take_profit_2"], json.dumps(d["votes"]),
             json.dumps(d["reasons"]), int(shadow)),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_executed(self, decision_id: int) -> None:
        self.conn.execute("UPDATE decisions SET executed=1 WHERE id=?",
                          (decision_id,))
        self.conn.commit()

    def record_trade(self, symbol: str, side: str, opened_ts: str,
                     pnl: float, exit_reason: str,
                     decision_id: int = None, shadow: bool = False) -> None:
        self.conn.execute(
            """INSERT INTO trades
               (decision_id, symbol, side, opened_ts, closed_ts, pnl,
                exit_reason, shadow)
               VALUES (?,?,?,?,?,?,?,?)""",
            (decision_id, symbol, side, opened_ts, _now(), pnl, exit_reason,
             int(shadow)),
        )
        self.conn.commit()

    # ---- readers (research) ----
    def executed_trades_with_votes(self, source: str = "all") -> list[dict]:
        """Closed trades joined to the decision that opened them.
        source: 'all', 'real', or 'shadow'."""
        where = {"all": "", "real": "AND t.shadow=0", "shadow": "AND t.shadow=1"}[source]
        rows = self.conn.execute(
            f"""SELECT t.pnl, t.side, t.symbol, t.closed_ts, d.votes,
                       d.confidence, t.shadow
                FROM trades t JOIN decisions d ON d.id = t.decision_id
                WHERE t.decision_id IS NOT NULL {where}"""
        ).fetchall()
        return [
            {"pnl": r[0], "side": r[1], "symbol": r[2], "closed_ts": r[3],
             "votes": json.loads(r[4]), "confidence": r[5], "shadow": bool(r[6])}
            for r in rows
        ]

    def counts(self) -> dict:
        out = {}
        for table in ("snapshots", "decisions", "trades"):
            out[table] = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out["executed_decisions"] = self.conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE executed=1").fetchone()[0]
        out["shadow_trades"] = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE shadow=1").fetchone()[0]
        out["real_trades"] = out["trades"] - out["shadow_trades"]
        return out
