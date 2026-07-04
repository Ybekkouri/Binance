"""Research & learning loop for the trading engine.

The engine feeds market_data.db while it runs (every snapshot, every decision
with its factor votes, every outcome). This tool turns that dataset into
knowledge — WITHOUT ever touching the live configuration by itself:

  python research.py stats
      Dataset size: snapshots, decisions, executed trades.

  python research.py collect [--interval 300]
      Standalone data collector: keeps snapshotting all configured symbols
      into the datastore even when the bot isn't trading. No keys needed.

  python research.py factors [--min-trades 20] [--write config_suggested.yaml]
      Which factors actually predicted wins? Per-factor aligned/against win
      rates and PnL, plus bounded weight suggestions once there's enough
      sample. --write saves a candidate config; it is NEVER auto-applied.

  python research.py compare --config-b config_suggested.yaml [--days 60]
      Out-of-sample referee: backtests the current config vs the candidate
      on the same data. Only promote the candidate if it wins here.

The learning discipline, in order: collect -> measure -> suggest ->
validate out-of-sample -> promote by hand. No step is skipped; that is what
separates learning from curve-fitting.
"""

import argparse
import copy
import json
import logging
import time

import yaml

from bot import metrics
from bot.config import load_config
from bot.datastore import DataStore

log = logging.getLogger("research")

MIN_SAMPLE_DEFAULT = 20
MAX_WEIGHT_SHIFT = 0.25   # a suggestion may move a weight at most +/-25%
WEIGHT_CAP = 3.0


# ------------------------------------------------------------ factors
def factor_table(store: DataStore, source: str = "all") -> dict:
    """Per-factor performance over closed trades (real, shadow, or all).

    'aligned' = the factor voted in the trade's direction; 'against' = it
    voted the other way but was overruled by the confluence.
    """
    trades = store.executed_trades_with_votes(source)
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
    """Bounded weight suggestions: nudge each factor's weight by its edge
    over the overall win rate, capped at +/-25%, only with enough sample."""
    suggestions = dict(current_weights)
    notes = []
    base = table["overall_win_rate"]
    for name, s in table["factors"].items():
        if name not in current_weights:
            continue
        if s["aligned_n"] < min_trades:
            notes.append(f"{name}: only {s['aligned_n']} aligned trades "
                         f"(need {min_trades}) — keeping weight")
            continue
        edge = s["aligned_wins"] / s["aligned_n"] - base
        shift = max(-MAX_WEIGHT_SHIFT, min(MAX_WEIGHT_SHIFT, edge))
        new = round(min(WEIGHT_CAP, max(0.0, current_weights[name] * (1 + shift))), 2)
        if new != current_weights[name]:
            notes.append(f"{name}: aligned win rate "
                         f"{s['aligned_wins'] / s['aligned_n'] * 100:.0f}% vs "
                         f"{base * 100:.0f}% overall -> weight "
                         f"{current_weights[name]} -> {new}")
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
        print("note: PnL columns mix USDT (real) and R units (shadow); "
              "win rates are unit-free and drive the suggestions.")
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
def cmd_compare(args) -> None:
    import backtest  # imported here: pulls in ccxt/network only when needed

    results = {}
    for label, path in (("A (current)", args.config_a),
                        ("B (candidate)", args.config_b)):
        cfg = load_config(path, require_keys=False)
        symbol = args.symbol or cfg.symbols[0]
        print(f"\n=== {label}: {path} on {symbol}, {args.days}d ===")
        trades, daily, m = backtest.run(cfg, symbol, args.days, args.equity)
        print(metrics.format_report(m, daily))
        results[label] = m

    a, b = results["A (current)"], results["B (candidate)"]
    if a.get("trades") and b.get("trades"):
        print("\n=== VERDICT (B minus A) ===")
        for key in ("total_pnl", "profit_factor", "sharpe", "sortino",
                    "max_drawdown_pct", "win_rate_pct"):
            print(f"{key:<18}{b[key] - a[key]:+.2f}")
        print("\nPromote B only if it wins on profit factor and drawdown, "
              "not just total PnL — and re-check on a second period.")


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


# ------------------------------------------------------------ cli
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="dataset size")

    p = sub.add_parser("collect", help="standalone snapshot collector")
    p.add_argument("--interval", type=int, default=300, help="seconds between rounds")

    p = sub.add_parser("factors", help="factor performance & weight suggestions")
    p.add_argument("--min-trades", type=int, default=MIN_SAMPLE_DEFAULT)
    p.add_argument("--source", choices=["all", "real", "shadow"], default="all",
                   help="which trades to learn from (default: all)")
    p.add_argument("--write", default=None,
                   help="write candidate config to this path")

    p = sub.add_parser("compare", help="backtest current vs candidate config")
    p.add_argument("--config-a", default="config.yaml")
    p.add_argument("--config-b", required=True)
    p.add_argument("--symbol", default=None)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--equity", type=float, default=1000)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    {"stats": cmd_stats, "collect": cmd_collect,
     "factors": cmd_factors, "compare": cmd_compare}[args.command](args)


if __name__ == "__main__":
    main()
