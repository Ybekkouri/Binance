"""Research datastore: every snapshot, decision and outcome, queryable.

The journal (JSONL) is the audit trail; this SQLite store is the *dataset* —
structured so the research tooling can join decisions to their outcomes and
measure which factors actually predict wins. The bot keeps feeding it while
it runs (in every mode, including paper), so the dataset grows even on days
with zero trades.
"""

import json
import os
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
    shadow INTEGER DEFAULT 0   -- 1: virtual learning trade on the shadow account
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

    def trades_full(self, source: str = "all") -> list[dict]:
        """Closed trades with their decision AND the market snapshot the
        decision was made from — the complete context for analysis."""
        where = {"all": "", "real": "AND t.shadow=0", "shadow": "AND t.shadow=1"}[source]
        rows = self.conn.execute(
            f"""SELECT t.pnl, t.side, t.symbol, t.opened_ts, t.closed_ts,
                       t.exit_reason, t.shadow,
                       d.confidence, d.risk_reward, d.market_condition, d.votes,
                       s.funding_rate, s.oi_change_pct, s.spread_pct,
                       s.book_imbalance, s.long_short_ratio,
                       s.taker_buy_sell_ratio, s.btc_trend
                FROM trades t
                JOIN decisions d ON d.id = t.decision_id
                LEFT JOIN snapshots s ON s.id = d.snapshot_id
                WHERE t.decision_id IS NOT NULL {where}"""
        ).fetchall()
        keys = ["pnl", "side", "symbol", "opened_ts", "closed_ts",
                "exit_reason", "shadow", "confidence", "risk_reward",
                "market_condition", "votes", "funding_rate", "oi_change_pct",
                "spread_pct", "book_imbalance", "long_short_ratio",
                "taker_buy_sell_ratio", "btc_trend"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["votes"] = json.loads(d["votes"]) if d["votes"] else []
            d["shadow"] = bool(d["shadow"])
            out.append(d)
        return out

    def merge_from(self, other_path: str) -> dict:
        """Import another instance's dataset (snapshots, decisions, trades)
        into this one, preserving decision->trade links via ID remapping.

        All-or-nothing: any error rolls the whole file back. Duplicates are
        detected by MARKET MOMENT, not by wall-clock: two people running the
        same config see the same candle and make the same decision, and that
        is one piece of evidence, not two — so it is imported once. Users on
        different pairs, timeframes or thresholds add genuinely new rows.

        This is how several people running the bot pool their learning data:
        each sends their market_data.db (/senddata), one person merges them
        and runs the research tools on the combined history."""
        if not os.path.isfile(other_path):
            raise FileNotFoundError(other_path)
        mine = self.conn.execute("PRAGMA database_list").fetchone()[2]
        if mine and os.path.realpath(mine) == os.path.realpath(other_path):
            raise ValueError("refusing to merge a dataset into itself")

        src = sqlite3.connect(other_path)

        def cols(table):
            return {r[1] for r in src.execute(f"PRAGMA table_info({table})")}

        def col(name, table, default="0"):
            # files from older versions may lack columns added later
            return name if name in cols(table) else f"{default} AS {name}"

        added = {"snapshots": 0, "decisions": 0, "trades": 0, "skipped": 0,
                 "unlinked": 0}
        try:
            self.conn.execute("BEGIN")

            snap_map, snap_candle = {}, {}
            for row in src.execute(
                    "SELECT id, ts, symbol, candle_time, last_price, "
                    "quote_volume_24h, funding_rate, oi_change_pct, spread_pct, "
                    "book_imbalance, long_short_ratio, taker_buy_sell_ratio, "
                    "btc_trend FROM snapshots"):
                old_id, rest = row[0], row[1:]
                ts, symbol, candle = rest[0], rest[1], rest[2]
                snap_candle[old_id] = candle
                if candle:
                    dup = self.conn.execute(
                        "SELECT id FROM snapshots WHERE symbol=? AND candle_time=?",
                        (symbol, candle)).fetchone()
                else:
                    dup = self.conn.execute(
                        "SELECT id FROM snapshots WHERE ts=? AND symbol=?",
                        (ts, symbol)).fetchone()
                if dup:
                    snap_map[old_id] = dup[0]
                    added["skipped"] += 1
                    continue
                cur = self.conn.execute(
                    "INSERT INTO snapshots (ts, symbol, candle_time, last_price,"
                    " quote_volume_24h, funding_rate, oi_change_pct, spread_pct,"
                    " book_imbalance, long_short_ratio, taker_buy_sell_ratio,"
                    " btc_trend) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rest)
                snap_map[old_id] = cur.lastrowid
                added["snapshots"] += 1

            dec_map, dec_was_dup = {}, {}
            for row in src.execute(
                    "SELECT id, snapshot_id, ts, symbol, direction, confidence, "
                    "risk_reward, market_condition, entry, stop, tp1, tp2, votes, "
                    f"reasons, {col('executed', 'decisions')}, "
                    f"{col('shadow', 'decisions')} FROM decisions"):
                old_id, old_snap, rest = row[0], row[1], row[2:]
                ts, symbol, direction, conf = rest[0], rest[1], rest[2], rest[3]
                shadow = rest[13]
                candle = snap_candle.get(old_snap)
                if candle:
                    dup = self.conn.execute(
                        "SELECT d.id FROM decisions d JOIN snapshots s "
                        "ON s.id = d.snapshot_id WHERE d.symbol=? AND "
                        "s.candle_time=? AND d.direction=? AND d.shadow=? AND "
                        "ROUND(d.confidence, 4)=ROUND(?, 4)",
                        (symbol, candle, direction, shadow, conf)).fetchone()
                else:
                    dup = self.conn.execute(
                        "SELECT id FROM decisions WHERE ts=? AND symbol=? AND "
                        "direction=? AND shadow=? AND "
                        "ROUND(confidence, 4)=ROUND(?, 4)",
                        (ts, symbol, direction, shadow, conf)).fetchone()
                if dup:
                    dec_map[old_id] = dup[0]
                    dec_was_dup[old_id] = True
                    added["skipped"] += 1
                    continue
                new_snap = snap_map.get(old_snap)
                if old_snap is not None and new_snap is None:
                    added["unlinked"] += 1
                cur = self.conn.execute(
                    "INSERT INTO decisions (snapshot_id, ts, symbol, direction,"
                    " confidence, risk_reward, market_condition, entry, stop,"
                    " tp1, tp2, votes, reasons, executed, shadow)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_snap,) + rest)
                dec_map[old_id] = cur.lastrowid
                added["decisions"] += 1

            for row in src.execute(
                    "SELECT decision_id, symbol, side, opened_ts, closed_ts, pnl,"
                    f" exit_reason, {col('shadow', 'trades')} FROM trades"):
                old_dec, rest = row[0], row[1:]
                symbol, side, opened, closed, pnl, shadow = (
                    rest[0], rest[1], rest[2], rest[3], rest[4], rest[6])
                new_dec = dec_map.get(old_dec)
                if dec_was_dup.get(old_dec) and self.conn.execute(
                        "SELECT 1 FROM trades WHERE decision_id=?",
                        (new_dec,)).fetchone():
                    added["skipped"] += 1   # same decision, already has its outcome
                    continue
                if self.conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND side=? AND "
                        "opened_ts=? AND closed_ts=? AND pnl=? AND shadow=?",
                        (symbol, side, opened, closed, pnl, shadow)).fetchone():
                    added["skipped"] += 1
                    continue
                if old_dec is not None and new_dec is None:
                    added["unlinked"] += 1
                self.conn.execute(
                    "INSERT INTO trades (decision_id, symbol, side, opened_ts,"
                    " closed_ts, pnl, exit_reason, shadow) VALUES (?,?,?,?,?,?,?,?)",
                    (new_dec,) + rest)
                added["trades"] += 1

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            src.close()
        return added

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
