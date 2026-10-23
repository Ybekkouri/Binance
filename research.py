"""Research & learning loop for the trading engine.

The engine feeds market_data.db while it runs (every snapshot, every decision
with its factor votes, every outcome). This tool turns that dataset into
knowledge — WITHOUT ever touching the live configuration by itself:

  python research.py stats
      Dataset size: snapshots, decisions, executed trades.

  python research.py collect [--interval 300]
      Standalone data collector: keeps snapshotting all configured symbols
      into the datastore even when the bot isn't trading. No keys needed.

  python research.py report [--source all|real|shadow] [--out report.txt]
      THE complete analysis: overview with confidence intervals, real-vs-
      shadow comparison, confidence calibration, market-condition/exit/
      symbol/direction/timing breakdowns, context-metric significance tests
      (the evidence for promoting recorded metrics to voting factors), and
      per-factor statistics.

  python research.py factors [--min-trades 20] [--write config_suggested.yaml]
      Which factors actually predicted wins? Per-factor aligned/against win
      rates and PnL, plus bounded weight suggestions once there's enough
      sample. --write saves a candidate config; it is NEVER auto-applied.

  python research.py history --symbol BTC/USDT [--source spot|futures] [--out f.txt]
      Deep history for a pair you intend to trade regularly: fetches the
      MAXIMUM daily history Binance has (spot reaches furthest back),
      caches it, and dissects it — bull/bear phases, yearly breakdown,
      regime persistence, seasonality, volatility anatomy, top drawdowns.

  python research.py compare --config-b config_suggested.yaml [--days 60] [--offset-days 60]
      Out-of-sample referee: backtests the current config vs the candidate
      on the same data. Only promote the candidate if it wins here.

The learning discipline, in order: collect -> measure -> suggest ->
validate out-of-sample -> promote by hand. No step is skipped; that is what
separates learning from curve-fitting.
"""

import argparse
import json
import logging
import math
import sqlite3
import time

import yaml

from bot import analysis, metrics
from bot.config import load_config
from bot.datastore import DataStore

log = logging.getLogger("research")

MIN_SAMPLE_DEFAULT = 30
MAX_WEIGHT_SHIFT = 0.25   # a suggestion may move a weight at most +/-25%
WEIGHT_CAP = 3.0


# ------------------------------------------------------------ factors
def factor_table(store: DataStore, source: str = "all") -> dict:
    """Per-factor performance over closed trades (real, shadow, or all).

    'aligned' = the factor voted in the trade's direction; 'against' = it
    voted the other way but was overruled by the confluence.
    """
    trades = store.trades_full(source)   # 'all' drops real/shadow twins
    stats: dict = {}
    overall_wins = sum(1 for t in trades if t["pnl"] > 0)
    for t in trades:
        direction = 1 if t["side"] == "long" else -1
        win = t["pnl"] > 0
        for v in t["votes"]:
            s = stats.setdefault(v["name"], {
                "aligned_n": 0, "aligned_wins": 0, "aligned_pnl": 0.0,
                "against_n": 0, "against_wins": 0, "against_pnl": 0.0,
            })
            if v["vote"] == 0:
                continue
            if v["vote"] * direction > 0:
                s["aligned_n"] += 1
                s["aligned_wins"] += win
                s["aligned_pnl"] += t["pnl"]
            else:
                s["against_n"] += 1
                s["against_wins"] += win
                s["against_pnl"] += t["pnl"]
    return {
        "n_trades": len(trades),
        "overall_win_rate": overall_wins / len(trades) if trades else 0.0,
        "factors": stats,
    }


def suggest_weights(table: dict, current_weights: dict,
                    min_trades: int) -> tuple[dict, list]:
    """Bounded, significance-gated weight suggestions.

    A weight moves only when BOTH the aligned trades and the rest have at
    least `min_trades` each AND the win-rate difference clears a two-sided
    z-test at 95% (|z| >= 1.96). Even then the move is capped at +/-25%.
    Factors are correlated and trades are selected on the total score, so
    a significant edge is still a hint to validate with `compare`, never
    proof on its own."""
    suggestions = dict(current_weights)
    notes = []
    total_wins = round(table["overall_win_rate"] * table["n_trades"])
    for name, s in table["factors"].items():
        if name not in current_weights:
            continue
        comp_n = table["n_trades"] - s["aligned_n"]
        comp_w = total_wins - s["aligned_wins"]
        if s["aligned_n"] < min_trades or comp_n < min_trades:
            notes.append(f"{name}: {s['aligned_n']} aligned / {comp_n} other "
                         f"trades (need {min_trades} each) — keeping weight")
            continue
        a_rate = s["aligned_wins"] / s["aligned_n"]
        c_rate = comp_w / comp_n
        z = analysis.two_prop_z(s["aligned_wins"], s["aligned_n"], comp_w, comp_n)
        if abs(z) < analysis.Z95:
            notes.append(f"{name}: {a_rate * 100:.0f}% vs {c_rate * 100:.0f}% "
                         f"(z={z:+.2f}) — not significant, keeping weight")
            continue
        edge = a_rate - c_rate
        shift = max(-MAX_WEIGHT_SHIFT, min(MAX_WEIGHT_SHIFT, edge))
        new = round(min(WEIGHT_CAP, max(0.0, current_weights[name] * (1 + shift))), 2)
        if new != current_weights[name]:
            notes.append(f"{name}: aligned win rate {a_rate * 100:.0f}% vs "
                         f"{c_rate * 100:.0f}% when not aligned (z={z:+.2f}) "
                         f"-> weight {current_weights[name]} -> {new}")
            suggestions[name] = new
    return suggestions, notes


def cmd_factors(args) -> None:
    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    table = factor_table(store, args.source)
    if table["n_trades"] == 0:
        print("No closed trades in the dataset yet for source "
              f"'{args.source}'. Run the bot (paper mode counts — and the "
              "shadow track fills the dataset fastest), then come back.")
        return

    counts = store.counts()
    print(f"Trades analyzed: {table['n_trades']} (source={args.source}; "
          f"dataset has {counts['real_trades']} real, "
          f"{counts['shadow_trades']} shadow) — overall win rate "
          f"{table['overall_win_rate'] * 100:.1f}%")
    if args.source == "all" and counts["shadow_trades"] and counts["real_trades"]:
        print("note: pooled view — shadow trades that duplicate a real trade "
              "(same candle and direction) are counted once. PnL columns still "
              "mix two accounts of different sizes; win rates are unit-free.")
    print()
    header = (f"{'factor':<16}{'aligned n':>10}{'win%':>7}{'pnl':>10}"
              f"{'against n':>11}{'win%':>7}{'pnl':>10}")
    print(header)
    print("-" * len(header))
    for name, s in sorted(table["factors"].items()):
        aw = s["aligned_wins"] / s["aligned_n"] * 100 if s["aligned_n"] else 0
        gw = s["against_wins"] / s["against_n"] * 100 if s["against_n"] else 0
        print(f"{name:<16}{s['aligned_n']:>10}{aw:>6.0f}%{s['aligned_pnl']:>+10.2f}"
              f"{s['against_n']:>11}{gw:>6.0f}%{s['against_pnl']:>+10.2f}")

    suggestions, notes = suggest_weights(
        table, cfg.strategy.weights, args.min_trades)
    print()
    for n in notes:
        print("  " + n)
    if table["n_trades"] < args.min_trades:
        print(f"\nSample too small ({table['n_trades']} trades) for reliable "
              "conclusions — keep collecting.")

    if args.write:
        with open(args.config) as f:
            raw = yaml.safe_load(f)
        raw["strategy"]["weights"] = suggestions
        with open(args.write, "w") as f:
            f.write("# Candidate weights suggested by research.py from "
                    f"{table['n_trades']} trades.\n"
                    "# VALIDATE before use:  python research.py compare "
                    f"--config-b {args.write}\n")
            yaml.safe_dump(raw, f, sort_keys=False)
        print(f"\nCandidate config written to {args.write} — validate it "
              "out-of-sample before promoting.")


# ------------------------------------------------------------ compare
# Factors that always vote 0 in backtests (no historical order book / OI).
# Their weights only dilute normalized confidence there, so a candidate that
# changes them would look better or worse for no real reason.
BACKTEST_BLIND_FACTORS = ("book_imbalance", "open_interest")


def cmd_compare(args) -> None:
    import backtest  # imported here: pulls in ccxt/network only when needed

    cfg_a = load_config(args.config_a, require_keys=False)
    cfg_b = load_config(args.config_b, require_keys=False)
    symbol = args.symbol or cfg_a.symbols[0]
    for cfg in (cfg_a, cfg_b):
        for f in BACKTEST_BLIND_FACTORS:
            cfg.strategy.weights[f] = cfg_a.strategy.weights.get(f, 0)

    ago = (f", ending {args.offset_days} days ago (out-of-sample)"
           if args.offset_days else "")
    print(f"Fetching {args.days} days of {symbol}{ago} once, used for both configs...")
    data = backtest.load_data(cfg_a, symbol, args.days,
                              offset_days=args.offset_days)
    results = {}
    for label, cfg, path in (("A (current)", cfg_a, args.config_a),
                             ("B (candidate)", cfg_b, args.config_b)):
        print(f"\n=== {label}: {path} on {symbol}, {args.days}d{ago} ===")
        trades, daily, m = backtest.run(cfg, symbol, args.days, args.equity,
                                        data=data)
        print(metrics.format_report(m, daily))
        results[label] = m

    a, b = results["A (current)"], results["B (candidate)"]
    if a.get("trades") and b.get("trades"):
        print("\n=== VERDICT (B minus A) ===")
        for key, better in (("total_pnl", "higher"), ("profit_factor", "higher"),
                            ("sharpe", "higher"), ("sortino", "higher"),
                            ("max_drawdown_pct", "LOWER"),
                            ("win_rate_pct", "higher")):
            va, vb = a[key], b[key]
            if not (math.isfinite(va) and math.isfinite(vb)):
                # e.g. profit factor is inf when a run had zero losers
                print(f"{key:<18}n/a (A={va:.2f}, B={vb:.2f} — "
                      "too few trades for a meaningful ratio)")
                continue
            print(f"{key:<18}{vb - va:+.2f}   ({better} is better)")
        if args.offset_days:
            print(f"\nThis window ended {args.offset_days} days ago. If the "
                  "bot has been collecting data for less time than that, the "
                  "candidate never saw it: a genuine out-of-sample test.")
        else:
            print("\nThis window overlaps the recent period the candidate's "
                  "weights were learned from, so it flatters B. Re-run with "
                  "--offset-days 60 (an older window) for the real test.")
        print("Promote B only if it wins on profit factor AND drawdown in an "
              "out-of-sample window, and again on another symbol (--symbol).")
        print(f"(Weights of {', '.join(BACKTEST_BLIND_FACTORS)} were held equal: "
              "backtests have no historical data for them.)")


# ------------------------------------------------------------ collect
def cmd_collect(args) -> None:
    from bot.market_data import MarketData

    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    market = MarketData(cfg)
    print(f"Collecting {', '.join(cfg.symbols)} every {args.interval}s "
          f"into {cfg.datastore_file} (Ctrl-C to stop)...")
    try:
        while True:
            try:
                bt = market.btc_trend()
                for symbol in cfg.symbols:
                    snap = market.snapshot(symbol, bt)
                    store.record_snapshot(snap)
                log.info("collected %s | totals: %s",
                         ", ".join(cfg.symbols), store.counts())
            except Exception as e:
                log.warning("collection round failed: %s", e)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.", store.counts())


def cmd_stats(args) -> None:
    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    print(json.dumps(store.counts(), indent=2))


def cmd_history(args) -> None:
    from bot import history

    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    print(f"Fetching maximum available {args.source} history for "
          f"{args.symbol} (cached incrementally; first run takes a minute)...")
    df = history.fetch_daily_history(store.conn, args.symbol, args.source)
    text = history.full_history_report(df, args.symbol, args.source)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"\nReport written to {args.out}")


def cmd_merge(args) -> None:
    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    for path in args.files:
        try:
            added = store.merge_from(path)
        except (OSError, ValueError, sqlite3.DatabaseError) as e:
            print(f"{path}: NOT merged ({e}) — nothing from it was imported.")
            continue
        print(f"{path}: +{added['snapshots']} snapshots, "
              f"+{added['decisions']} decisions, +{added['trades']} trades "
              f"({added['skipped']} already known, skipped)")
        if added["unlinked"]:
            print(f"   note: {added['unlinked']} rows lost their link to a "
                  "decision or snapshot and won't appear in factor analysis")
    print("\nCombined dataset:", json.dumps(store.counts()))
    print("Run `python research.py report --source shadow` to analyze it.")


def cmd_report(args) -> None:
    cfg = load_config(args.config, require_keys=False)
    store = DataStore(cfg.datastore_file)
    trades = store.trades_full(args.source)
    text = analysis.full_report(trades, args.source)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"\nReport written to {args.out}")


# ------------------------------------------------------------ cli
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="dataset size")

    p = sub.add_parser("report", help="the complete analysis report")
    p.add_argument("--source", choices=["all", "real", "shadow"], default="all")
    p.add_argument("--out", default=None, help="also write the report to a file")

    p = sub.add_parser("merge", help="pool datasets from other bot instances")
    p.add_argument("files", nargs="+",
                   help="market_data.db files from other users/instances")

    p = sub.add_parser("history", help="deep history analysis for one pair")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--source", choices=["spot", "futures"], default="spot",
                   help="spot has the longest history; futures since ~2019")
    p.add_argument("--out", default=None, help="also write the report to a file")

    p = sub.add_parser("collect", help="standalone snapshot collector")
    p.add_argument("--interval", type=int, default=300, help="seconds between rounds")

    p = sub.add_parser("factors", help="factor performance & weight suggestions")
    p.add_argument("--min-trades", type=int, default=MIN_SAMPLE_DEFAULT)
    p.add_argument("--source", choices=["all", "real", "shadow"], default="shadow",
                   help="which trades to learn from (default: shadow — real and shadow overlap, so pooling double-counts)")
    p.add_argument("--write", default=None,
                   help="write candidate config to this path")

    p = sub.add_parser("compare", help="backtest current vs candidate config")
    p.add_argument("--config-a", default="config.yaml")
    p.add_argument("--config-b", required=True)
    p.add_argument("--symbol", default=None)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--offset-days", type=int, default=0,
                   help="end the test window this many days ago, so it does "
                        "not overlap the data the weights were learned from")
    p.add_argument("--equity", type=float, default=1000)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    {"stats": cmd_stats, "collect": cmd_collect, "report": cmd_report,
     "history": cmd_history, "factors": cmd_factors,
     "compare": cmd_compare, "merge": cmd_merge}[args.command](args)


if __name__ == "__main__":
    main()
