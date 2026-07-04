# Binance Futures Trading Engine

A risk-first trading engine for Binance USDⓈ-M perpetual futures. Its primary
goal is **capital preservation**: it trades only when multiple independent
factors align and every risk control is satisfied. The default action is
**NO TRADE**.

> ⚠️ **Read this first.** Leveraged futures can lose money faster than you can
> react. No strategy guarantees profits, and every limit in this engine is a
> stopping rule, not a promise of returns. Start in paper mode, then testnet,
> and only go live with money you can afford to lose. Use API keys with
> futures permission only — **never enable withdrawals**.

## Architecture

```
main.py                entry point (+ --close-all manual override)
backtest.py            historical simulation of the exact same engine
research.py            learning loop: factor analysis, weight candidates, A/B
config.yaml            every parameter and risk limit
bot/
  config.py            strict config loading (fails fast on bad values)
  market_data.py       public data: klines, funding, OI, order book, positioning
  indicators.py        EMA, RSI, ATR, swing points, structure, S/R levels
  strategy.py          multi-factor confluence engine -> TradeDecision
  decision.py          the full auditable decision record
  risk.py              portfolio-level risk engine + position sizing
  broker.py            LiveBroker (ccxt, testnet/live) + PaperBroker (simulated)
  manager.py           breakeven, ATR trailing, invalidation & liquidation guard
  trader.py            orchestrator loop, kill switch, data-outage protection
  journal.py           JSONL audit log of every decision and order
  datastore.py         SQLite research dataset: snapshots, decisions, outcomes
  analysis.py          the complete statistical analysis over the dataset
  metrics.py           win rate, profit factor, Sharpe, Sortino, expectancy...
```

The trader runs one or two **tracks** — complete, independent pipelines over
the same market snapshots. The *real* track is the strict engine on your
configured broker. The optional *shadow* track is the identical pipeline at
relaxed thresholds on a virtual account (see below).

## How a trade happens (or doesn't)

1. **Snapshot** — closed candles on two timeframes, funding rate, open
   interest change, order book depth/spread, 24h volume, BTC trend.
2. **Strategy** — eleven weighted factors vote long/short/neutral:
   higher-timeframe trend, execution trend, market structure, momentum,
   breakout, volume expansion, room to S/R, overextension guard, funding,
   open interest, book imbalance. A trade needs `min_confidence` (0.60)
   **and** `min_aligned_factors` (4) agreeing, then must survive hard gates
   (volatility band, BTC direction filter for alts, funding extremes) and the
   minimum risk/reward (1.5 blended across targets).
3. **Risk engine** — daily loss cap, daily profit target, weekly drawdown
   halt, consecutive-loss cooldown, trades-per-day cap, max open positions,
   per-symbol and total exposure caps, spread/liquidity/book-depth gates,
   margin sufficiency. *Any* failure = NO TRADE, with the reason journaled.
4. **Execution** — market entry sized so the stop costs `risk_per_trade_pct`
   (0.5%) of equity, then exchange-side brackets immediately: close-position
   stop-loss, partial take-profit at 1R (50%), final target at 2.5R. If
   bracket placement fails, the position is flattened on the spot.
5. **Management** — stop to breakeven at +1R, ATR trailing after TP1,
   early exit on a confident opposing signal, liquidation-distance guard,
   PnL settled into the risk counters (fees and funding included).

All limits are **ratios of equity**, so they scale with the account.
No martingale, no averaging down, no revenge trading — the engine refuses
duplicate positions and cools off after consecutive losses.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # only needed for testnet/live modes
```

## The safe path to live

**1. Backtest** (no keys needed):

```bash
python backtest.py --days 90 --equity 1000
python backtest.py --symbol ETH/USDT --days 60
```

Reports win rate, profit factor, expectancy, Sharpe, Sortino, max drawdown,
fees, funding and per-day PnL. Order book and open interest factors vote
neutral in backtests (no historical data), which only makes backtests more
conservative than live. To compare strategies, copy `config.yaml`, change
parameters, and run with `--config variant.yaml`.

**2. Paper trade** (`mode: paper`) — live market data, simulated fills with
fees and slippage, no keys, no risk. State persists in `paper_state.json`.

**3. Testnet** (`mode: testnet`, the default) — real order flow against
Binance's futures testnet (free keys at <https://testnet.binancefuture.com>).

**4. Live** (`mode: live`) — requires typing `live` at startup. Restrict API
keys to futures trading + your IP, withdrawals disabled.

Run any mode with:

```bash
python main.py
```

## Safety controls

- **Kill switch**: create a file named `KILL` in the working directory — the
  bot cancels all orders, flattens all positions (configurable), and stops.
- **Manual override**: `python main.py --close-all` flattens everything.
- **Crash safety**: stops and targets live on the exchange, not in the bot.
- **Data outage**: repeated data failures block new entries; open positions
  stay protected by their exchange-side brackets.
- **Restart safety**: risk counters and managed positions persist to
  `bot_state.json`; a restart resumes management, it doesn't reset limits.
- **Audit trail**: every decision (including every NO_TRADE), risk block,
  order, stop move, exit and error is one JSON line in `journal.jsonl`.

## Data & the learning loop

The engine is data-hungry by design: every snapshot it takes (price, funding,
open interest, order book depth and imbalance, long/short account ratio,
taker buy/sell flow, BTC trend), every decision with all eleven factor votes,
and every trade outcome is stored in `market_data.db`. The dataset grows in
every mode — paper trading feeds it just as well as live.

```bash
python research.py stats                     # dataset size
python research.py collect --interval 300    # keep collecting even when not trading
python research.py factors                   # which factors actually predict wins?
python research.py factors --write config_suggested.yaml   # bounded weight candidate
python research.py compare --config-b config_suggested.yaml --days 60   # out-of-sample referee
```

### The shadow track: trade rarely, learn constantly

The strict engine trades maybe once a day — good for capital, terrible for
sample size. The **shadow track** solves it: a complete twin of the trading
pipeline runs alongside the real one, on the same live market data, at
relaxed thresholds (confidence ≥ 0.35, 2 aligned factors vs the strict
0.60/4). *Same process, no money*: the shadow track goes through the full
risk engine (its own daily/weekly limits and counters), full position sizing,
simulated fills with fees and slippage on a virtual account (default 1,000
USDT), and the full trade manager — partial TP1, breakeven move, ATR
trailing, invalidation exits, liquidation guard — then settles its PnL and
records everything to the dataset flagged `shadow`. Restart-safe via its own
state files. The only thing it cannot do is touch money or influence the
real track.

So the split you'd want is built in: **limited real trades with money,
unlimited virtual trades for data**, running simultaneously in the same
process. Because the shadow account compounds its own equity, its results
also answer a strategic question directly: *what would a relaxed version of
this engine have earned?* Analyze the tracks together or apart:

```bash
python research.py factors --source shadow   # learning-track only
python research.py factors --source real     # money trades only
python research.py factors                   # both (win rates are unit-free)
```

Shadow data never influences execution — it only informs the research loop.

### The complete analysis report

```bash
python research.py report [--source all|real|shadow] [--out report.txt]
```

One command, every question that matters, with statistical honesty built in
(Wilson confidence intervals on every win rate; `*` marks differences that
clear a 95% two-sided z-test — everything unstarred is noise until more data
arrives):

- **Overview** — win rate with confidence interval, profit factor, expectancy
- **Tracks** — the strict real track vs the relaxed shadow track, with a
  significance test on the win-rate edge: what do stricter thresholds buy?
- **Confidence calibration** — do higher-confidence decisions actually win
  more? Tests top vs bottom bucket; warns loudly if calibration is inverted
- **Market conditions / exits / symbols / direction / timing** — where the
  edge lives and where it doesn't (regime, exit reason, hour block, weekday)
- **Context metrics** — funding rate, open interest change, book imbalance,
  long/short ratio, taker flow, spread: each split at its median and
  significance-tested. This is the evidence base for promoting a recorded
  metric to a voting factor
- **Factors** — per-factor aligned/against performance with confidence
  intervals and z-scores vs the baseline

The report warns about small samples and about multiple-comparison false
positives (test enough metrics at 95% and one will star by chance) — trust
only effects that stay significant as the dataset grows.

The learning discipline, in order: **collect → measure → suggest → validate
out-of-sample → promote by hand.** Weight suggestions are bounded (±25%),
require a minimum sample (20 aligned trades per factor), and are written to a
*candidate* file — never applied automatically. `compare` backtests the
candidate against the current config on the same data; promote it only if it
wins on profit factor and drawdown, not just total PnL, and re-check on a
second period. This is what separates learning from curve-fitting — the
original prototype in this repo "learned" from its own trades in a circle,
which is precisely the failure mode this pipeline is built to avoid.

## Expectations, honestly

The engine's discipline controls *losses*; profits depend on market
conditions, and there will be losing days and quiet weeks where it simply
refuses to trade. The daily profit target (default +2% of equity) and loss
cap (−1.5%) are when it *stops for the day*, not what it earns. Judge it on
the backtest and a long paper/testnet run — not on hope.
