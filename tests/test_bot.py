"""Offline test suite: no network, no API keys, synthetic data only.

Run with either:
    python3 -m pytest tests/        (if pytest is installed)
    python3 tests/test_bot.py       (plain Python, no extra packages)
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_SECRET_KEY", "test")

import ccxt  # noqa: E402

from bot import indicators as ta, strategy  # noqa: E402
from bot.analysis import factors_detailed, two_prop_z  # noqa: E402
from bot.broker import LiveBroker, PaperBroker, _signed_income  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.datastore import DataStore  # noqa: E402
from bot.decision import LONG, NO_TRADE, SHORT, TradeDecision  # noqa: E402
from bot.events import EventGuard  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.manager import TradeManager  # noqa: E402
from bot.market_data import BookSummary, MarketSnapshot  # noqa: E402
from bot.notify import Telegram  # noqa: E402
from bot.risk import RiskEngine, size_position  # noqa: E402
from bot.trader import Track, Trader, stopped_marker  # noqa: E402

TMP = tempfile.mkdtemp(prefix="bot-tests-")


# ------------------------------------------------------------ fixtures
def make_cfg(**overrides):
    cfg = load_config("config.yaml", require_keys=False)
    cfg.mode = "paper"
    cfg.symbols = ["BTC/USDT"]
    d = tempfile.mkdtemp(dir=TMP)
    cfg.state_file = f"{d}/risk.json"
    cfg.paper_state_file = f"{d}/paper.json"
    cfg.journal_file = f"{d}/journal.jsonl"
    cfg.datastore_file = f"{d}/data.db"
    cfg.kill_file = f"{d}/KILL"
    cfg.shadow.paper_state_file = f"{d}/sh_paper.json"
    cfg.shadow.risk_state_file = f"{d}/sh_risk.json"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def zigzag(n=300, base=100.0, up=0.35, down=0.25, noise=0.05, seed=1,
           breakout=1.2, start="2026-06-01", shock=False):
    rng = np.random.default_rng(seed)
    closes = [base]
    for i in range(n - 2):
        closes.append(closes[-1] + (up if i % 18 < 12 else -down)
                      + rng.normal(0, noise))
    closes.append(closes[-1] + breakout)
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    h, lo = np.maximum(o, c) + 0.15, np.minimum(o, c) - 0.15
    if shock:
        h[-1] = c[-1] + 8.0
        lo[-1] = c[-1] - 8.0
    v = np.full(n, 1000.0)
    v[-1] = 3000.0
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c,
                         "volume": v}, index=idx)


def htf(df):
    return df.resample("1h").agg({"open": "first", "high": "max",
                                  "low": "min", "close": "last",
                                  "volume": "sum"}).dropna()


def snap_of(df, symbol="BTC/USDT", funding=-0.0001, btc=1, price=None,
            imbalance=1.8):
    return MarketSnapshot(
        symbol=symbol, candles=df, trend_candles=htf(df),
        last_price=float(df.close.iloc[-1]) if price is None else price,
        quote_volume_24h=5e8, funding_rate=funding, oi_change_pct=2.5,
        book=BookSummary(bid=100, ask=100.01, spread_pct=0.01,
                         bid_depth=90000, ask_depth=50000,
                         imbalance=imbalance),
        btc_trend=btc)


def mirror(df):
    out = df.copy()
    for col in ("open", "close"):
        out[col] = 200 - df[col]
    out["high"] = 200 - df["low"]
    out["low"] = 200 - df["high"]
    return out


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def once(self, key, text):
        self.sent.append(text)

    def poll_commands(self):
        return []

    def send_document(self, path, caption=""):
        return True


class FakeClient:
    """Records calls; behaviour switches let tests inject failures."""

    def __init__(self):
        self.calls = []
        self.positions = []
        self.fail_create = False
        self.fail_close_for = set()
        self.ledger = []

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append(("create", symbol, side, amount, dict(params)))
        if self.fail_create and ("stopLossPrice" in params
                                 or "takeProfitPrice" in params):
            raise ccxt.NetworkError("boom")
        if params.get("reduceOnly") and "stopLossPrice" not in params \
                and "takeProfitPrice" not in params:
            if symbol in self.fail_close_for:
                raise ccxt.ExchangeError("cannot close")
            self.positions = [p for p in self.positions if p["symbol"] != symbol]
        return {"id": f"o{len(self.calls)}"}

    def cancel_order(self, oid, symbol, params=None):
        self.calls.append(("cancel", oid, symbol))

    def cancel_all_orders(self, symbol, params=None):
        self.calls.append(("cancel_all", symbol, dict(params or {})))

    def fetch_open_orders(self, symbol, params=None):
        return []

    def fetch_positions(self, symbols):
        return [{"symbol": p["symbol"] + ":USDT", "side": p["side"],
                 "contracts": p["contracts"], "notional": 100,
                 "entryPrice": 100} for p in self.positions]

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def market_id(self, symbol):
        return symbol.replace("/", "")

    def fetch_ledger(self, since=None, limit=None, params=None):
        t = params["incomeType"]
        return [e for e in self.ledger if e["info"]["incomeType"] == t]


def live_broker(cfg):
    b = LiveBroker.__new__(LiveBroker)
    b.cfg = cfg
    b.client = FakeClient()
    return b


def ledger_row(income_type, income):
    # shaped like ccxt's parse_ledger_entry: ABS amount + direction
    amt = abs(income)
    return {"amount": amt, "direction": "out" if income <= 0 else "in",
            "timestamp": 1, "info": {"symbol": "BTCUSDT",
                                     "incomeType": income_type,
                                     "income": str(income)}}


# ------------------------------------------------------------ config
def test_config_loads_and_bot_mode_overrides():
    cfg = load_config("config.yaml", require_keys=False)
    assert cfg.risk.max_consecutive_losses == 3
    os.environ["BOT_MODE"] = "paper"
    try:
        assert load_config("config.yaml", require_keys=False).mode == "paper"
    finally:
        del os.environ["BOT_MODE"]


# ------------------------------------------------------------ strategy
def test_strategy_long_short_and_no_trade():
    cfg = make_cfg()
    up = zigzag()
    d = strategy.decide(snap_of(up), cfg)
    assert d.direction == LONG, d.reasons
    assert d.stop_loss < d.entry_price < d.take_profit_1 < d.take_profit_2
    assert d.risk_reward >= cfg.risk.min_risk_reward

    dn = mirror(up)
    d2 = strategy.decide(snap_of(dn, funding=0.0001, btc=-1, imbalance=0.55), cfg)
    assert d2.direction == SHORT, d2.reasons

    rng = np.random.default_rng(9)
    c = 100 + np.cumsum(rng.normal(0, 0.2, 300))
    chop = pd.DataFrame({"open": c, "high": c + .2, "low": c - .2, "close": c,
                         "volume": 1000.0},
                        index=pd.date_range("2026-06-01", periods=300,
                                            freq="15min", tz="UTC"))
    s = snap_of(chop, funding=0.0)
    s.book = None
    assert strategy.decide(s, cfg).direction == NO_TRADE


def test_hard_gates():
    cfg = make_cfg()
    up = zigzag()
    d = strategy.decide(snap_of(up, symbol="ETH/USDT", btc=-1), cfg)
    assert d.direction == NO_TRADE and any("BTC filter" in r for r in d.reasons)
    d = strategy.decide(snap_of(up, funding=0.005), cfg)
    assert d.direction == NO_TRADE and any("funding" in r for r in d.reasons)


def test_rsi_flat_is_neutral():
    assert abs(ta.rsi(pd.Series([100.0] * 30), 14).iloc[-1] - 50) < 1e-9
    assert ta.rsi(pd.Series(np.arange(100.0, 130.0)), 14).iloc[-1] > 99


# ------------------------------------------------------------ risk
def test_risk_limits_and_sizing():
    cfg = make_cfg()
    r = RiskEngine(cfg)
    r.roll(1000)
    assert r.account_checks(1000, [], 500, "BTC/USDT") == []
    r.record_close("X", -16, 984)
    assert any("daily loss" in f for f in r.account_checks(984, [], 100, "BTC/USDT"))

    cfg = make_cfg()
    r = RiskEngine(cfg)
    r.roll(1000)
    r.record_close("X", -0.3, 999.7)          # fee noise is not a loss
    assert r.state.consecutive_losses == 0
    for _ in range(3):
        r.record_close("X", -2, 1000)
    assert any("consecutive" in f for f in r.account_checks(1000, [], 100, "BTC/USDT"))

    cfg = make_cfg()
    r = RiskEngine(cfg)
    r.roll(1000)
    assert any("weekly drawdown" in f for f in r.account_checks(950, [], 100, "BTC/USDT"))
    pos = [{"symbol": "ETH/USDT", "notional": 900.0}]
    assert any("total exposure" in f for f in r.account_checks(1000, pos, 1500, "BTC/USDT"))
    assert any("already open" in f for f in r.account_checks(1000, pos, 100, "ETH/USDT"))

    assert abs(size_position(cfg, 1000, 100, 98, []) - 5 / 2.02) < 1e-9
    assert abs(size_position(cfg, 1000, 100, 99.99, []) - 10.0) < 1e-9


# ------------------------------------------------------------ paper broker
def test_paper_broker_fills():
    cfg = make_cfg()
    b = PaperBroker(cfg)
    b.state["balance"] = 1000.0
    b.mark("BTC/USDT", 100.0)
    b.enter("BTC/USDT", "long", 2.0, 98.0, 102.0, 1.0, 105.0)
    b.mark("BTC/USDT", 94.0)                  # gap far through the stop
    loss = b.state["closed"][-1]["pnl"]
    assert loss < 2 * (94.0 * 1.0002 - 100.0)  # filled at market, not stop

    b.mark("ETH/USDT", 50.0)
    b.enter("ETH/USDT", "long", 2.0, 49.0, 51.0, 1.0, 52.5)
    b.mark("ETH/USDT", 53.0)                  # TP1 and TP2 in one tick
    assert "ETH/USDT" not in b.state["positions"]


# ------------------------------------------------------------ live broker
def test_ledger_sign_is_respected():
    # ccxt returns ABS amounts: a -10 loss must stay -10, not become +10
    assert _signed_income(ledger_row("REALIZED_PNL", -10)) == -10
    no_raw = {"amount": 4.0, "direction": "out", "info": {}}
    assert _signed_income(no_raw) == -4.0

    b = live_broker(make_cfg())
    b.client.ledger = [ledger_row("REALIZED_PNL", -10.0),
                       ledger_row("COMMISSION", -0.5),
                       ledger_row("FUNDING_FEE", 0.2)]
    assert abs(b.realized_pnl_since("BTC/USDT", 0) - (-10.3)) < 1e-9


def test_pnl_waits_for_realized_row():
    b = live_broker(make_cfg())
    b.client.ledger = [ledger_row("COMMISSION", -0.5)]
    try:
        b.realized_pnl_since("BTC/USDT", 0)
        raise AssertionError("should wait for the realized row")
    except RuntimeError:
        pass


def test_replace_stop_places_new_before_cancelling_old():
    b = live_broker(make_cfg())
    b.replace_stop("BTC/USDT", "long", 101.0, old_order_id="old1", amount=2.0)
    kinds = [c[0] for c in b.client.calls]
    assert kinds.index("create") < kinds.index("cancel")
    create = next(c for c in b.client.calls if c[0] == "create")
    assert create[4].get("reduceOnly") and "closePosition" not in create[4]


def test_replace_stop_failure_keeps_old_stop():
    b = live_broker(make_cfg())
    b.client.fail_create = True
    try:
        b.replace_stop("BTC/USDT", "long", 101.0, old_order_id="old1", amount=2.0)
        raise AssertionError("expected failure")
    except ccxt.NetworkError:
        pass
    assert not any(c[0] == "cancel" for c in b.client.calls)


def test_close_all_continues_and_keeps_stops_on_failures():
    cfg = make_cfg()
    cfg.symbols = ["BTC/USDT", "ETH/USDT"]
    b = live_broker(cfg)
    b.client.positions = [{"symbol": "BTC/USDT", "side": "long", "contracts": 1},
                          {"symbol": "ETH/USDT", "side": "short", "contracts": 1}]
    b.client.fail_close_for = {"BTC/USDT"}
    still = b.close_all()
    assert still == ["BTC/USDT"]
    cancelled = {c[1] for c in b.client.calls if c[0] == "cancel_all"}
    assert "ETH/USDT" in cancelled and "BTC/USDT" not in cancelled


def test_entry_clears_leftover_orders_first():
    b = live_broker(make_cfg())
    b.enter("BTC/USDT", "long", 2.0, 98.0, 102.0, 1.0, 105.0)
    assert b.client.calls[0][0] == "cancel_all"


# ------------------------------------------------------------ manager
def _managed_position(cfg, broker):
    risk = RiskEngine(cfg)
    risk.roll(1000)
    mgr = TradeManager(cfg, broker, risk, Journal(cfg.journal_file),
                       notifier=FakeNotifier())
    d = TradeDecision(symbol="BTC/USDT", direction=LONG, entry_price=100.0,
                      stop_loss=98.0, take_profit_1=102.0, take_profit_2=105.0)
    mgr.on_entry(d, 2.0, 1000.0, sl_order_id="sl1")
    return risk, mgr


def test_breakeven_not_marked_when_stop_move_fails():
    cfg = make_cfg()
    b = live_broker(cfg)
    b.client.positions = [{"symbol": "BTC/USDT", "side": "long", "contracts": 2}]
    risk, mgr = _managed_position(cfg, b)
    b.client.fail_create = True
    pos = b.open_positions()[0]
    mgr.manage(snap_of(zigzag(), price=102.5), position=pos)
    info = risk.state.managed["BTC/USDT"]
    assert info["breakeven_done"] is False and info["stop"] == 98.0
    b.client.fail_create = False
    mgr.manage(snap_of(zigzag(), price=102.5), position=pos)
    assert risk.state.managed["BTC/USDT"]["breakeven_done"] is True


def test_needs_protection_is_retried():
    cfg = make_cfg()
    b = live_broker(cfg)
    b.client.positions = [{"symbol": "BTC/USDT", "side": "long", "contracts": 2}]
    risk, mgr = _managed_position(cfg, b)
    risk.state.managed["BTC/USDT"]["needs_protection"] = True
    pos = b.open_positions()[0]
    mgr.manage(snap_of(zigzag(), price=100.5), position=pos)
    assert risk.state.managed["BTC/USDT"]["needs_protection"] is False
    assert any(c[0] == "create" and "stopLossPrice" in c[4]
               for c in b.client.calls)


# ------------------------------------------------------------ trader
def _paper_track(cfg, notifier=None, events=None):
    broker = PaperBroker(cfg)
    broker.state["balance"] = 1000.0
    risk = RiskEngine(cfg)
    journal = Journal(cfg.journal_file)
    mgr = TradeManager(cfg, broker, risk, journal, notifier=notifier)
    return Track("real", cfg, broker, risk, mgr, journal, notifier=notifier,
                 events=events), broker, risk


def test_paper_track_full_lifecycle():
    cfg = make_cfg()
    track, broker, risk = _paper_track(cfg)
    s = snap_of(zigzag())
    track.process(s)
    assert len(broker.open_positions()) == 1
    info = risk.state.managed["BTC/USDT"]
    track.process(snap_of(zigzag(), price=info["entry"] + info["risk_dist"] * 1.3))
    assert risk.state.managed["BTC/USDT"]["breakeven_done"]
    stop = risk.state.managed["BTC/USDT"]["stop"]
    track.process(snap_of(zigzag(), price=stop - 0.05))
    assert not broker.open_positions() and risk.state.daily_pnl > 0


def test_min_notional_guard_uses_rounded_tp1():
    cfg = make_cfg()
    track, broker, _ = _paper_track(cfg)
    broker.state["balance"] = 100.0
    broker.min_notional = lambda s: 100.0
    track.process(snap_of(zigzag()))
    assert not broker.open_positions()
    lines = [json.loads(x) for x in open(cfg.journal_file)]
    assert any("exchange minimums" in r for e in lines
               if e["event"] == "risk_block" for r in e["reasons"])


def test_failed_entry_retries_once_per_candle_and_adopts_leftover():
    cfg = make_cfg()
    track, broker, risk = _paper_track(cfg, notifier=FakeNotifier())
    attempts = []

    def failing_enter(*a, **k):
        attempts.append(1)
        # leave a position behind, like a fill whose bracket failed
        broker.state["positions"]["BTC/USDT"] = {
            "side": "long", "contracts": 1.0, "entry_price": 100.0,
            "stop": 0, "tp1": 1e9, "tp1_amount": 0, "tp2": 1e9,
            "tp1_filled": True, "mark": 100.0, "fees": 0, "realized": 0,
            "opened_ms": 0}
        raise ccxt.NetworkError("bracket failed")

    broker.enter = failing_enter
    track.is_shadow = True                     # shadow swallows the error
    track.process(snap_of(zigzag()))
    assert attempts and "BTC/USDT" in risk.state.managed
    assert risk.state.managed["BTC/USDT"]["needs_protection"] is True

    # deterministic rejection: at most 2 attempts on the same candle
    del broker.state["positions"]["BTC/USDT"]
    risk.state.managed.clear()
    attempts.clear()
    track.entry_failures.clear()
    track.last_eval_candle.clear()

    def always_fail(*a, **k):
        attempts.append(1)
        raise ccxt.ExchangeError("rejected")

    broker.enter = always_fail
    for _ in range(5):
        track.process(snap_of(zigzag()))
    assert len(attempts) == 2, attempts


# ------------------------------------------------------------ events
def test_shock_observed_without_a_signal_and_blocks_next_entry():
    cfg = make_cfg()
    n = FakeNotifier()
    g = EventGuard(cfg, notifier=n)
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 0.05, 100))
    df = pd.DataFrame({"open": c, "high": c + .1, "low": c - .1, "close": c,
                       "volume": 1.0},
                      index=pd.date_range("2026-06-01", periods=100,
                                          freq="15min", tz="UTC"))
    df.iloc[-1, df.columns.get_loc("high")] += 5
    s = snap_of(df)
    g.observe(s)                              # no trade signal involved
    assert any("⚡" in m for m in n.sent)
    assert any("cooldown" in r for r in g.check(snap_of(zigzag())))
    g.shock_until["BTC/USDT"] = time.time() - 1
    assert g.check(snap_of(zigzag())) == []


def test_blackouts_and_malformed_warning():
    cfg = make_cfg()
    now = datetime.now(timezone.utc)
    cfg.events.blackouts = [
        {"start": (now - timedelta(minutes=5)).isoformat(),
         "end": (now + timedelta(hours=1)).isoformat(), "label": "FOMC"},
        {"start": "garbage"},
        {"start": now.isoformat(), "end": (now - timedelta(hours=1)).isoformat()},
    ]
    n = FakeNotifier()
    g = EventGuard(cfg, notifier=n)
    assert any("FOMC" in r for r in g.check(snap_of(zigzag())))
    assert any("Ignored invalid blackout" in m for m in n.sent)


# ------------------------------------------------------------ shutdown
def _trader(cfg, broker, notifier):
    risk = RiskEngine(cfg)
    real = Track("real", cfg, broker, risk,
                 TradeManager(cfg, broker, risk, Journal(cfg.journal_file)),
                 Journal(cfg.journal_file))
    return Trader(cfg, None, real, Journal(cfg.journal_file), notifier=notifier)


def test_kill_success_writes_stopped_marker():
    cfg = make_cfg()
    n = FakeNotifier()
    t = _trader(cfg, PaperBroker(cfg), n)
    open(cfg.kill_file, "w").close()
    assert t._shutdown("kill switch", persistent=True) is True
    assert os.path.isfile(stopped_marker(cfg))
    assert not os.path.isfile(cfg.kill_file)
    assert "stay off" in n.sent[-1]


def test_kill_failure_is_reported_and_retried():
    cfg = make_cfg()
    n = FakeNotifier()
    b = live_broker(cfg)
    b.client.positions = [{"symbol": "BTC/USDT", "side": "long", "contracts": 1}]
    b.client.fail_close_for = {"BTC/USDT"}
    t = _trader(cfg, b, n)
    open(cfg.kill_file, "w").close()
    assert t._shutdown("kill switch", persistent=True) is False
    assert os.path.isfile(cfg.kill_file)          # kept, so it is retried
    assert not os.path.isfile(stopped_marker(cfg))
    assert any("could NOT close" in m for m in n.sent)
    assert not any("closed." in m and "Engine stopped" in m for m in n.sent)


def test_daily_summary_reports_quiet_days():
    cfg = make_cfg()
    n = FakeNotifier()
    t = _trader(cfg, PaperBroker(cfg), n)
    t._summary_date = "2000-01-01"
    t.real.risk.state.date = "2000-01-01"         # last activity long ago
    t._maybe_daily_summary()
    assert "no trades" in n.sent[-1]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    t._summary_date = "2000-01-01"
    t.real.risk.state.date = yesterday
    t.real.risk.state.daily_pnl = 7.5
    t.real.risk.state.trades_today = 2
    t._maybe_daily_summary()
    assert "+7.50" in n.sent[-1]


# ------------------------------------------------------------ telegram
def test_telegram_token_redacted_and_ack():
    class TG(Telegram):
        def __init__(self):
            super().__init__("123:SECRET", "1")
            self.calls = []

        def _api(self, method, params):
            self.calls.append((method, params.get("offset")))
            if method == "sendMessage":
                raise ConnectionError(
                    "https://api.telegram.org/bot123:SECRET/sendMessage failed")
            return {"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": 1}, "text": "/kill"}},
                {"update_id": 8, "message": {"chat": {"id": 99}, "text": "/kill"}}]}

    tg = TG()
    assert "SECRET" not in tg.redact(ConnectionError("x bot123:SECRET y"))
    assert tg.poll_commands() == ["/kill"]       # stranger ignored
    tg.ack()
    assert ("getUpdates", 9) in tg.calls


# ------------------------------------------------------------ statistics
def _store_with_trades(path, n, win_fn, shadow=False, seed=7, symbol="B",
                       candle_prefix="2026-06-01"):
    store = DataStore(path)
    rng = np.random.default_rng(seed)
    df1 = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1],
                        "volume": [1]},
                       index=pd.DatetimeIndex([pd.Timestamp(
                           f"{candle_prefix}T00:00", tz="UTC")]))
    for i in range(n):
        aligned = rng.random() < 0.6
        win = rng.random() < win_fn(aligned)
        side = "long" if i % 2 == 0 else "short"
        dirn = 1 if side == "long" else -1
        snap = MarketSnapshot(symbol=symbol, candles=df1, trend_candles=df1,
                              last_price=1)
        sid = store.record_snapshot(snap)
        store.conn.execute("UPDATE snapshots SET candle_time=? WHERE id=?",
                           (f"{candle_prefix} {i:05d}", sid))
        did = store.record_decision(TradeDecision(
            symbol=symbol, direction="LONG" if dirn > 0 else "SHORT",
            confidence=0.5 + i * 1e-5,
            votes=[{"name": "trend_htf", "vote": dirn if aligned else -dirn,
                    "weight": 2.0, "detail": ""}]), sid, shadow=shadow)
        store.mark_executed(did)
        store.record_trade(symbol, side, f"2026-06-01T{i % 24:02d}:00:00+00:00",
                           5.0 if win else -4.0, "take_profit_2",
                           decision_id=did, shadow=shadow)
    store.conn.commit()
    return store


def test_weight_suggestions_ignore_noise_and_follow_real_edges():
    import research
    cfg = make_cfg()
    noise = _store_with_trades(f"{TMP}/noise.db", 120, lambda a: 0.5)
    sugg, _ = research.suggest_weights(research.factor_table(noise, "all"),
                                       cfg.strategy.weights, 30)
    assert sugg["trend_htf"] == cfg.strategy.weights["trend_htf"]

    edge = _store_with_trades(f"{TMP}/edge.db", 300,
                              lambda a: 0.7 if a else 0.3)
    sugg, _ = research.suggest_weights(research.factor_table(edge, "all"),
                                       cfg.strategy.weights, 30)
    assert sugg["trend_htf"] > cfg.strategy.weights["trend_htf"]
    line = [x for x in factors_detailed(edge.trades_full("all")).split("\n")
            if x.startswith("trend_htf")][0]
    assert "*" in line
    assert abs(two_prop_z(60, 100, 40, 100) - 2.828) < 0.01


# ------------------------------------------------------------ merge
def test_merge_dedup_transaction_and_old_schema():
    mine = _store_with_trades(f"{TMP}/m.db", 10, lambda a: 0.5, seed=1)
    _store_with_trades(f"{TMP}/clone.db", 10, lambda a: 0.5, seed=1).conn.close()
    _store_with_trades(f"{TMP}/other.db", 10, lambda a: 0.5, seed=2,
                       symbol="E", candle_prefix="2026-07-01").conn.close()

    added = mine.merge_from(f"{TMP}/clone.db")    # same moments = same evidence
    assert added["trades"] == 0, added
    added = mine.merge_from(f"{TMP}/other.db")    # a different pair = new
    assert added["trades"] == 10, added
    assert mine.counts()["trades"] == 20

    try:
        mine.merge_from(f"{TMP}/m.db")
        raise AssertionError("self-merge must be refused")
    except ValueError:
        pass

    broken = f"{TMP}/broken.db"
    con = sqlite3.connect(broken)
    con.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    before = mine.counts()
    try:
        mine.merge_from(broken)
        raise AssertionError("broken file must fail")
    except sqlite3.DatabaseError:
        pass
    assert mine.counts() == before                # nothing half-imported

    old = f"{TMP}/old.db"
    con = sqlite3.connect(old)
    con.executescript("""
    CREATE TABLE snapshots (id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT,
      candle_time TEXT, last_price REAL, quote_volume_24h REAL,
      funding_rate REAL, oi_change_pct REAL, spread_pct REAL,
      book_imbalance REAL, long_short_ratio REAL, taker_buy_sell_ratio REAL,
      btc_trend INTEGER);
    CREATE TABLE decisions (id INTEGER PRIMARY KEY, snapshot_id INTEGER,
      ts TEXT, symbol TEXT, direction TEXT, confidence REAL, risk_reward REAL,
      market_condition TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL,
      votes TEXT, reasons TEXT, executed INTEGER DEFAULT 0);
    CREATE TABLE trades (id INTEGER PRIMARY KEY, decision_id INTEGER,
      symbol TEXT, side TEXT, opened_ts TEXT, closed_ts TEXT, pnl REAL,
      exit_reason TEXT);
    INSERT INTO snapshots (ts, symbol, candle_time) VALUES ('t','S','c1');
    INSERT INTO decisions (snapshot_id, ts, symbol, direction, confidence,
      votes, reasons) VALUES (1,'t','S','LONG',0.7,'[]','[]');
    INSERT INTO trades (decision_id, symbol, side, opened_ts, closed_ts, pnl,
      exit_reason) VALUES (1,'S','long','a','b',3.0,'take_profit_2');
    """)
    con.commit()
    con.close()
    assert mine.merge_from(old)["trades"] == 1


# ------------------------------------------------------------ backtest
def test_backtest_parity_features():
    import backtest
    cfg = make_cfg()
    cfg.risk.min_quote_volume_24h = 0
    cfg.strategy.min_confidence = 0.45
    cfg.strategy.min_aligned_factors = 3
    rng = np.random.default_rng(11)
    n = 2880
    regime = np.concatenate([np.full(720, v) for v in [12, -10, 8, -6]])
    zig = np.where((np.arange(n) % 24) < 16, 1.0, -0.8)
    steps = rng.normal(0, 35, n) + regime * zig
    close = 60000 + np.cumsum(steps)
    o = np.concatenate([[close[0]], close[:-1]])
    df = pd.DataFrame({"open": o, "high": np.maximum(o, close) + 20,
                       "low": np.minimum(o, close) - 20, "close": close,
                       "volume": np.abs(rng.normal(1000, 300, n)) + np.abs(steps)},
                      index=pd.date_range("2026-04-01", periods=n,
                                          freq="15min", tz="UTC"))
    trades, _ = backtest.run_backtest(cfg, df, backtest.resample(df, "1h"),
                                      lambda t: 0, pd.Series(dtype=float), 1000.0)
    assert len(trades) > 3 and (trades.equity > 0).all()

    blocked, _ = backtest.run_backtest(cfg, df, backtest.resample(df, "1h"),
                                       lambda t: 0, pd.Series(dtype=float),
                                       1000.0, min_notional=1e9)
    assert len(blocked) == 0                       # exchange minimum enforced

    # BTC filter lookup must never see an unfinished higher-timeframe bar
    seen = []

    def spy(t):
        seen.append(t)
        return 0
    backtest.run_backtest(cfg, df.iloc[:400], backtest.resample(df.iloc[:400], "1h"),
                          spy, pd.Series(dtype=float), 1000.0)
    bar = df.index[1] - df.index[0]
    assert seen and all((t - bar) in df.index for t in seen)   # bar CLOSE times


def test_installer_syntax():
    assert os.system("bash -n deploy/install.sh") == 0


# ------------------------------------------------------------ runner
if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:      # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
