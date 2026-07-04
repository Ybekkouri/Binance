"""Complete performance analysis over the research dataset.

Every section is built to answer one question an accurate bot needs answered,
with statistical honesty baked in:

  overview       — is the engine profitable at all? (win rate with a Wilson
                   confidence interval, expectancy, profit factor)
  tracks         — strict real track vs relaxed shadow track: what do the
                   stricter thresholds actually buy?
  calibration    — does higher decision confidence really mean a higher win
                   rate? (validates or refutes min_confidence directly)
  conditions     — which market regimes the strategy survives in
  exits          — where profits actually come from (TP2 vs breakeven churn)
  timing         — hour-of-day and weekday effects
  context        — recorded-but-not-voting metrics (funding, book imbalance,
                   long/short ratio, taker flow, OI change) split into
                   low/high halves and significance-tested: the evidence for
                   promoting a metric to a voting factor
  factors        — per-factor aligned/against performance with confidence
                   intervals and significance markers

Small samples are flagged, not hidden: a `*` marks differences that clear a
two-sided z-test at 95%; everything else should be treated as noise until
more data arrives.
"""

import math
from datetime import datetime

MIN_BUCKET = 5          # buckets smaller than this are shown but flagged
Z95 = 1.96


# ---------------------------------------------------------------- stats
def wilson_interval(wins: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for a win rate — honest about small samples."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def two_prop_z(w1: int, n1: int, w2: int, n2: int) -> float:
    """z statistic for the difference between two win rates."""
    if n1 == 0 or n2 == 0:
        return 0.0
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (w1 / n1 - w2 / n2) / se


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------- buckets
def _row(label: str, trades: list) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl = sum(t["pnl"] for t in trades)
    lo, hi = wilson_interval(wins, n)
    return {
        "label": label, "n": n, "wins": wins,
        "win_rate": wins / n if n else 0.0, "ci": (lo, hi),
        "avg_pnl": pnl / n if n else 0.0, "total_pnl": pnl,
    }


def bucket_by(trades: list, key_fn) -> list[dict]:
    groups: dict = {}
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        groups.setdefault(k, []).append(t)
    return [_row(str(k), g) for k, g in sorted(groups.items())]


def format_rows(title: str, rows: list[dict], unit: str = "USDT") -> str:
    lines = [title,
             f"{'bucket':<22}{'n':>5}{'win%':>7}{'95% CI':>15}"
             f"{'avg pnl':>10}{'total':>10}"]
    lines.append("-" * len(lines[1]))
    for r in rows:
        flag = "  (small sample)" if r["n"] < MIN_BUCKET else ""
        ci = f"[{r['ci'][0]*100:.0f}-{r['ci'][1]*100:.0f}%]"
        lines.append(
            f"{r['label']:<22}{r['n']:>5}{r['win_rate']*100:>6.0f}%{ci:>15}"
            f"{r['avg_pnl']:>+10.2f}{r['total_pnl']:>+10.2f}{flag}")
    return "\n".join(lines)


# ---------------------------------------------------------------- sections
def overview(trades: list) -> str:
    if not trades:
        return "No closed trades yet."
    r = _row("all trades", trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    return "\n".join([
        f"Trades:        {r['n']}",
        f"Win rate:      {r['win_rate']*100:.1f}%  "
        f"(95% CI {r['ci'][0]*100:.0f}-{r['ci'][1]*100:.0f}%)",
        f"Profit factor: {pf:.2f}",
        f"Avg win:       {sum(wins)/len(wins):+.2f}" if wins else "Avg win:       n/a",
        f"Avg loss:      {sum(losses)/len(losses):+.2f}" if losses else "Avg loss:      n/a",
        f"Expectancy:    {r['avg_pnl']:+.2f} per trade",
        f"Total PnL:     {r['total_pnl']:+.2f}",
    ])


def tracks(trades: list) -> str:
    rows = bucket_by(trades, lambda t: "shadow" if t["shadow"] else "real")
    out = format_rows("Real vs shadow track "
                      "(PnL in each track's own account currency):", rows)
    by = {r["label"]: r for r in rows}
    if "real" in by and "shadow" in by:
        z = two_prop_z(by["real"]["wins"], by["real"]["n"],
                       by["shadow"]["wins"], by["shadow"]["n"])
        sig = "statistically significant" if abs(z) >= Z95 else \
            "NOT yet statistically significant — keep collecting"
        out += (f"\nStrict-threshold win-rate edge: "
                f"{(by['real']['win_rate']-by['shadow']['win_rate'])*100:+.1f} "
                f"points (z={z:.2f}, {sig})")
    return out


def calibration(trades: list) -> str:
    edges = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01)]

    def key(t):
        c = t.get("confidence")
        if c is None:
            return None
        for lo, hi in edges:
            if lo <= c < hi:
                return f"conf {lo:.2f}-{hi:.2f}"
        return None

    rows = bucket_by(trades, key)
    out = format_rows("Confidence calibration — win rate should RISE with "
                      "confidence if the score means anything:", rows)
    usable = [r for r in rows if r["n"] >= MIN_BUCKET]
    if len(usable) >= 2:
        # Bucket-to-bucket monotonicity is too noisy to test directly; what
        # matters is whether high confidence significantly beats low.
        bot_r, top_r = usable[0], usable[-1]
        z = two_prop_z(top_r["wins"], top_r["n"], bot_r["wins"], bot_r["n"])
        if z >= Z95:
            out += (f"\nConfidence is informative: top bucket beats bottom by "
                    f"{(top_r['win_rate']-bot_r['win_rate'])*100:.0f} points "
                    f"(z={z:.2f}, significant).")
        elif z <= -Z95:
            out += (f"\nWARNING — calibration is INVERTED (z={z:.2f}): higher "
                    "confidence is losing more. Investigate before trusting "
                    "the score.")
        else:
            out += (f"\nNo significant difference yet between low and high "
                    f"confidence (z={z:.2f}) — keep collecting before tuning "
                    "min_confidence.")
    return out


def conditions(trades: list) -> str:
    return format_rows("By market condition at entry:",
                       bucket_by(trades, lambda t: t.get("market_condition") or None))


def exits(trades: list) -> str:
    return format_rows("By exit reason — many 'breakeven' exits mean TP1 is "
                       "doing the work; many stop_loss means entries are bad:",
                       bucket_by(trades, lambda t: t.get("exit_reason") or None))


def by_symbol(trades: list) -> str:
    return format_rows("By symbol:", bucket_by(trades, lambda t: t["symbol"]))


def by_side(trades: list) -> str:
    return format_rows("By direction:", bucket_by(trades, lambda t: t["side"]))


def timing(trades: list) -> str:
    def hour_block(t):
        try:
            h = _parse_ts(t["opened_ts"]).hour
        except (ValueError, TypeError):
            return None
        lo = (h // 4) * 4
        return f"{lo:02d}:00-{lo+4:02d}:00 UTC"

    def weekday(t):
        try:
            d = _parse_ts(t["opened_ts"])
        except (ValueError, TypeError):
            return None
        return f"{d.isoweekday()}-{d.strftime('%a')}"

    return (format_rows("By 4-hour entry block:", bucket_by(trades, hour_block))
            + "\n\n" + format_rows("By weekday:", bucket_by(trades, weekday)))


CONTEXT_METRICS = [
    ("funding_rate", "funding rate"),
    ("oi_change_pct", "open interest change %"),
    ("book_imbalance", "order book imbalance"),
    ("long_short_ratio", "long/short account ratio"),
    ("taker_buy_sell_ratio", "taker buy/sell ratio"),
    ("spread_pct", "spread %"),
]


def context(trades: list) -> str:
    """Low-half vs high-half split per recorded metric, significance-tested.
    This is the evidence base for promoting a metric to a voting factor."""
    blocks = ["Context metrics (low half vs high half of each recorded "
              "metric; '*' = significant at 95%):"]
    for key, label in CONTEXT_METRICS:
        have = [t for t in trades if t.get(key) is not None]
        if len(have) < MIN_BUCKET * 2:
            blocks.append(f"\n{label}: only {len(have)} trades with data — skipped")
            continue
        values = sorted(t[key] for t in have)
        median = values[len(values) // 2]
        low = [t for t in have if t[key] <= median]
        high = [t for t in have if t[key] > median]
        if not low or not high:
            blocks.append(f"\n{label}: no variation in values — skipped")
            continue
        rl, rh = _row(f"low  (<= {median:.5g})", low), _row(f"high (> {median:.5g})", high)
        z = two_prop_z(rh["wins"], rh["n"], rl["wins"], rl["n"])
        star = " *" if abs(z) >= Z95 else ""
        blocks.append(
            f"\n{label}{star}\n"
            f"  {rl['label']:<20} n={rl['n']:<4} win {rl['win_rate']*100:>3.0f}% "
            f"avg {rl['avg_pnl']:+.2f}\n"
            f"  {rh['label']:<20} n={rh['n']:<4} win {rh['win_rate']*100:>3.0f}% "
            f"avg {rh['avg_pnl']:+.2f}   (z={z:+.2f})")
    blocks.append("\nWith several metrics tested at 95%, expect an occasional "
                  "false '*' by pure chance — trust only splits that STAY "
                  "significant as the dataset grows. A significant, repeatable "
                  "split is the evidence needed to promote a metric to a "
                  "voting factor — validate with `research.py compare` after "
                  "any change.")
    return "\n".join(blocks)


def factors_detailed(trades: list) -> str:
    """Per-factor performance with CIs and significance vs the baseline."""
    if not trades:
        return "No trades."
    base_wins = sum(1 for t in trades if t["pnl"] > 0)
    base_n = len(trades)
    stats: dict = {}
    for t in trades:
        direction = 1 if t["side"] == "long" else -1
        win = t["pnl"] > 0
        for v in t["votes"]:
            s = stats.setdefault(v["name"], {"a_n": 0, "a_w": 0, "g_n": 0, "g_w": 0})
            if v["vote"] == 0:
                continue
            if v["vote"] * direction > 0:
                s["a_n"] += 1
                s["a_w"] += win
            else:
                s["g_n"] += 1
                s["g_w"] += win

    lines = [f"Factor performance vs baseline win rate "
             f"{base_wins/base_n*100:.0f}% ('*' = significant at 95%):",
             f"{'factor':<16}{'aligned n':>10}{'win%':>7}{'95% CI':>15}"
             f"{'z':>7}{'  against n':>12}{'win%':>7}"]
    lines.append("-" * len(lines[1]))
    for name, s in sorted(stats.items()):
        if s["a_n"]:
            wr = s["a_w"] / s["a_n"]
            lo, hi = wilson_interval(s["a_w"], s["a_n"])
            z = two_prop_z(s["a_w"], s["a_n"], base_wins, base_n)
            star = "*" if abs(z) >= Z95 else " "
            ci = f"[{lo*100:.0f}-{hi*100:.0f}%]"
        else:
            wr, ci, z, star = 0.0, "-", 0.0, " "
        g = f"{s['g_w']/s['g_n']*100:>6.0f}%" if s["g_n"] else "     -"
        lines.append(f"{name:<16}{s['a_n']:>10}{wr*100:>6.0f}%{ci:>15}"
                     f"{z:>+6.1f}{star}{s['g_n']:>12}{g:>7}")
    return "\n".join(lines)


# ---------------------------------------------------------------- report
SECTIONS = [
    ("OVERVIEW", overview),
    ("TRACKS", tracks),
    ("CONFIDENCE CALIBRATION", calibration),
    ("MARKET CONDITIONS", conditions),
    ("EXIT REASONS", exits),
    ("SYMBOLS", by_symbol),
    ("DIRECTION", by_side),
    ("TIMING", timing),
    ("CONTEXT METRICS", context),
    ("FACTORS", factors_detailed),
]


def full_report(trades: list, source: str) -> str:
    header = (f"{'='*66}\nCOMPLETE ANALYSIS  (source: {source}, "
              f"{len(trades)} closed trades)\n{'='*66}")
    if not trades:
        return header + "\nNo closed trades yet — run the bot and come back."
    parts = [header]
    for title, fn in SECTIONS:
        if title == "TRACKS" and source != "all":
            continue
        parts.append(f"\n--- {title} " + "-" * (60 - len(title)))
        parts.append(fn(trades))
    if len(trades) < 30:
        parts.append(
            f"\nNOTE: only {len(trades)} trades — most splits above are noise "
            "at this sample size. Collect more (the shadow track is the "
            "fastest source) before acting on anything unstarred.")
    return "\n".join(parts)
