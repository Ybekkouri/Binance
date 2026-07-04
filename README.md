# Binance Futures Trading Bot

A trend-following bot for Binance USDⓈ-M futures with strict risk management,
built around a **daily profit target** (default: +2% of equity) and a **daily
loss cap** (default: −1.5% of equity). All limits are ratios of account
equity, so they adapt automatically as the account grows or shrinks — with
1,000 USDT the bot stops for the day at +20 or −15; with 2,500 USDT at +50 or
−37.5. When either limit is hit, the bot stops opening positions until the
next UTC day.

> ⚠️ **Read this first.** Futures trading with leverage can lose money faster
> than you can react, up to your entire margin. No strategy — this one
> included — can *guarantee* 50 USDT/day. The daily target is a stopping rule,
> not a promise. Always start on the testnet, backtest first, and never trade
> money you can't afford to lose.

## How it works

- **Strategy** (`bot/strategy.py`): EMA(20/50) crossover with an RSI filter,
  evaluated on closed 15-minute candles. Long on a bullish crossover, short on
  a bearish one.
- **Brackets**: every entry immediately gets a reduce-only stop-loss
  (1.5 × ATR) and take-profit (2.25 × ATR) on the exchange, so the position is
  protected even if the bot crashes or loses connectivity. If bracket
  placement fails, the position is closed on the spot.
- **Position sizing** (`bot/risk.py`): each trade risks 1% of account equity
  (distance to stop), capped by leverage and a max notional.
- **Daily discipline**: realized PnL (fees included) is tracked per UTC day
  and persisted to `bot_state.json`, so restarts don't reset your limits.
  The day's target and loss cap are computed from an equity snapshot taken at
  the start of the day, so the goalposts don't move intraday.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your API keys
```

For the testnet, create keys at <https://testnet.binancefuture.com> — it's
free play money. For live trading, create API keys in your Binance account
with futures permission only (never enable withdrawals) and restrict them to
your IP.

## Usage

**1. Backtest the strategy first** (no keys needed):

```bash
python backtest.py --days 90 --equity 5000
```

This replays the exact live-bot logic over historical candles and prints
daily PnL stats — including how many days would actually have reached +50.

**2. Run on the testnet** (`testnet: true` in `config.yaml`, the default):

```bash
python main.py
```

Let it run for at least a couple of weeks and check that behavior and PnL
match your backtest expectations.

**3. Go live** only after that: set `testnet: false` in `config.yaml`. The bot
will ask for confirmation on startup.

## Configuration

Everything lives in `config.yaml`: symbol, timeframe, leverage, strategy
periods, risk per trade, and the daily target/loss limits. Secrets stay in
`.env` and are never committed.

## The math behind the daily target

Risking 1% per trade with a 1.5R take-profit means each winner makes about
1.5% of equity before fees, and the strategy fires roughly 1–3 signals a day
on 15-minute candles. That's why the default target is **+2% of equity per
day** — one or two net winners reach it, and the bot banks the day instead of
giving profits back. In absolute terms the target scales with the account:
about +20 USDT/day at 1,000 equity, and +50 USDT/day once equity reaches
~2,500 (through compounding or deposits). Forcing bigger absolute numbers out
of a small account by raising leverage or risk-per-trade is how accounts blow
up. Run the backtest with your actual equity to see realistic numbers:
even +2%/day is an aggressive goal that no strategy sustains every day.

## Project layout

```
main.py            # entry point
backtest.py        # historical simulation of the same strategy
config.yaml        # all tunable parameters
bot/
  config.py        # config + secrets loading
  exchange.py      # ccxt Binance futures wrapper (testnet-aware)
  strategy.py      # EMA/RSI/ATR signal logic
  risk.py          # position sizing + daily limits
  trader.py        # main loop / state machine
my_bot.py.txt      # old uploaded prototype (kept for reference; not used)
```
