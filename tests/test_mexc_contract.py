"""MEXC contract tests: the REAL ccxt MEXC client, offline.

Same idea as test_exchange_contract.py: only ccxt's HTTP transport is
replaced, so request building, endpoint routing, signing and response
parsing are the genuine library code. These tests pin down exactly what
the bot would put on the wire to MEXC futures.

Run with:  python3 tests/test_mexc_contract.py   (or pytest)
"""

import json
import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import ccxt  # noqa: E402

from mexc_fixture import CONTRACTS, SPOT  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.manager import TradeManager  # noqa: E402
from bot.market_data import MarketData  # noqa: E402
from bot.mexc_broker import MexcBroker  # noqa: E402

T = 1700000000000
OK = {"success": True, "code": 0}


class Wire(ccxt.mexc):
    """Real ccxt MEXC client with an in-memory exchange behind it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.log = []
        self.hold_vol = 0            # open BTC_USDT position, in contracts
        self.position_type = 1       # 1 long, 2 short
        self.history = []
        self.reject_plan = False
        self.hedged = True
        self.refuse_mode_switch = False
        self.ignore_closes = False   # close orders accepted but not executed
        self.plan_orders = []

    def fetch(self, url, method="GET", headers=None, body=None):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        q = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        if path.endswith("/capital/config/getall"):
            raise ccxt.PermissionDenied("futures-only key: no wallet access")
        if path.endswith("/exchangeInfo"):
            return SPOT
        if path.endswith("/api/v3/time"):
            return {"serverTime": self.milliseconds()}
        if path.endswith("/contract/detail"):
            return CONTRACTS
        self.log.append((method, path, payload if payload is not None else q))
        if path.endswith("/order/create") or path.endswith("/order/submit"):
            vol = float(payload["vol"])
            if payload["side"] in (1, 3):        # open long / open short
                self.hold_vol += vol
                self.position_type = 1 if payload["side"] == 1 else 2
            elif not self.ignore_closes:
                self.hold_vol = max(0.0, self.hold_vol - vol)
            return {**OK, "data": {"orderId": str(100 + len(self.log)),
                                   "ts": T}}
        if path.endswith("/planorder/place"):
            if self.reject_plan:
                raise ccxt.ExchangeError(
                    'mexc {"success":false,"code":2015,"message":"bad"}')
            oid = 9000 + len(self.log)
            self.plan_orders.append({**payload, "id": oid})
            return {**OK, "data": oid}
        if path.endswith("/planorder/cancel"):
            ids = {str(r["orderId"]) for r in payload}
            self.plan_orders = [o for o in self.plan_orders
                                if str(o["id"]) not in ids]
            return dict(OK)
        if path.endswith("/planorder/cancel_all"):
            self.plan_orders = []
            return dict(OK)
        if path.endswith("/order/cancel_all"):
            return dict(OK)
        if path.endswith("/planorder/list/orders"):
            return {**OK, "data": [
                {"id": o["id"], "symbol": o["symbol"], "leverage": 3,
                 "side": o["side"], "triggerPrice": o["triggerPrice"],
                 "price": 0, "vol": o["vol"], "openType": o["openType"],
                 "triggerType": o["triggerType"], "state": 1,
                 "executeCycle": o["executeCycle"], "trend": 1,
                 "orderType": 5, "createTime": T, "updateTime": T}
                for o in self.plan_orders]}
        if path.endswith("/position/open_positions"):
            if self.hold_vol == 0:
                return {**OK, "data": []}
            return {**OK, "data": [{
                "positionId": 1, "symbol": "BTC_USDT",
                "positionType": self.position_type, "openType": 1,
                "state": 1, "holdVol": self.hold_vol, "frozenVol": 0,
                "closeVol": 0, "holdAvgPrice": 50000, "openAvgPrice": 50000,
                "closeAvgPrice": 0, "liquidatePrice": 34000, "oim": 10,
                "im": 10, "holdFee": 0, "realised": -0.03, "leverage": 3,
                "createTime": T, "updateTime": T, "autoAddIm": False}]}
        if path.endswith("/position/list/history_positions"):
            return {**OK, "data": self.history}
        if path.endswith("/position/position_mode"):
            return {**OK, "data": 1 if self.hedged else 2}
        if path.endswith("/position/change_position_mode"):
            if self.refuse_mode_switch:
                raise ccxt.ExchangeError(
                    'mexc {"success":false,"code":2029,"message":"has open"}')
            self.hedged = payload["positionMode"] == 1
            return dict(OK)
        if path.endswith("/position/change_leverage"):
            return dict(OK)
        if path.endswith("/account/assets"):
            return {**OK, "data": [
                {"currency": "USDT", "positionMargin": 10,
                 "availableBalance": 980, "cashBalance": 990,
                 "frozenBalance": 0, "equity": 1003.5, "unrealized": 13.5,
                 "bonus": 0}]}
        if "/contract/depth/" in path:
            return {**OK, "data": {
                "asks": [[50010, 2000, 1], [50020, 3000, 2]],
                "bids": [[50000, 1000, 1], [49990, 4000, 2]],
                "version": 1, "timestamp": T}}
        raise ccxt.NetworkError(f"unhandled {method} {path}")


def broker(**wire):
    cfg = load_config("config.yaml", require_keys=False)
    cfg.exchange = "mexc"
    cfg.symbols = ["BTC/USDT"]
    b = MexcBroker.__new__(MexcBroker)
    b.cfg = cfg
    b.FILL_LAG_S = 0                 # no real waiting in tests
    b.client = Wire({"apiKey": "k", "secret": "s",
                     "options": {"defaultType": "swap"}})
    MexcBroker._prepare(b.client)
    for k, v in wire.items():
        setattr(b.client, k, v)
    b._hedged = False
    b.client.load_markets()
    b.client.log.clear()
    return b


def calls(b, suffix):
    return [q for _, p, q in b.client.log if p.endswith(suffix)]


def test_markets_load_with_a_futures_only_key():
    # the wallet endpoint refuses futures-only keys; startup must not need it
    b = broker()
    assert b._market("BTC/USDT")["swap"] is True
    assert b._market("BTC/USDT")["id"] == "BTC_USDT"     # never spot BTCUSDT


def test_sizes_convert_to_whole_contracts_rounded_down():
    b = broker()
    assert b.amount_to_precision("BTC/USDT", 0.00127) == 0.0012
    assert b.amount_to_precision("BTC/USDT", 0.0012) == 0.0012   # float noise
    assert b.amount_to_precision("BTC/USDT", 0.00009) == 0.0     # < 1 contract
    assert b.amount_to_precision("ETH/USDT", 0.057) == 0.05
    assert b.price_to_precision("BTC/USDT", 50000.04) == 50000.0
    assert b.min_notional("BTC/USDT") == 0.0


def test_long_entry_bracket_on_the_wire():
    b = broker()
    out = b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
    # entry: open long (side 1), market (type 6), isolated with leverage
    (entry,) = calls(b, "/order/create")
    assert entry["symbol"] == "BTC_USDT" and entry["side"] == 1
    assert entry["vol"] == 12 and entry["type"] == 6
    assert entry["openType"] == 1 and entry["leverage"] == 3
    assert entry["positionMode"] == 1                    # account is hedged
    sl, tp1, tp2 = calls(b, "/planorder/place")
    for o in (sl, tp1, tp2):
        assert o["side"] == 4                            # close long
        assert o["orderType"] == 5                       # market on trigger
        assert o["executeCycle"] == 2                    # 7 days
        assert o["symbol"] == "BTC_USDT"
    # stop fires when price FALLS to it (2); take-profits when it RISES (1)
    assert (sl["triggerPrice"], sl["triggerType"], sl["vol"]) == ("49000", 2, 12)
    assert (tp1["triggerPrice"], tp1["triggerType"], tp1["vol"]) == ("51000", 1, 6)
    assert (tp2["triggerPrice"], tp2["triggerType"], tp2["vol"]) == ("52500", 1, 6)
    assert tp1["vol"] + tp2["vol"] == entry["vol"]       # never oversized
    assert out["sl_order_id"] == str(b.client.plan_orders[0]["id"])
    # leftovers from an earlier trade are cleared before entering
    # hedge mode is confirmed first, then leftovers from an earlier trade
    # are cleared, and only then is the entry sent
    paths = [p.rsplit("/", 2)[-2:] for _, p, _ in b.client.log]
    assert paths[:4] == [["position", "position_mode"], ["order", "cancel_all"],
                         ["planorder", "cancel_all"], ["order", "create"]]


def test_short_entry_mirrors_sides_and_trigger_directions():
    b = broker()
    b.enter("BTC/USDT", "short", 0.0010, 51000, 49000, 0.0005, 47500)
    (entry,) = calls(b, "/order/create")
    assert entry["side"] == 3                            # open short
    sl, tp1, tp2 = calls(b, "/planorder/place")
    assert all(o["side"] == 2 for o in (sl, tp1, tp2))   # close short
    assert sl["triggerType"] == 1                        # stop: price RISES
    assert tp1["triggerType"] == 2 and tp2["triggerType"] == 2


def test_bracket_failure_flattens_the_position():
    b = broker(reject_plan=True)
    try:
        b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
        raise AssertionError("bracket failure must raise")
    except ccxt.BaseError:
        pass
    opened, closed = calls(b, "/order/create")
    assert opened["side"] == 1
    assert closed["side"] == 4 and closed["vol"] == 12   # market close long
    assert b.open_positions() == []


def test_positions_and_equity_are_in_bot_units():
    b = broker(hold_vol=12)
    (p,) = b.open_positions()
    assert p["symbol"] == "BTC/USDT" and p["side"] == "long"
    assert p["contracts"] == 0.0012 and p["vol"] == 12   # base units + raw
    assert abs(p["notional"] - 60.0) < 1e-9
    assert p["liquidation_price"] == 34000
    assert b.equity_usdt() == 1003.5                     # includes unrealized


def test_replace_stop_places_new_before_cancelling_old():
    b = broker(hold_vol=6)
    old = b.place_stop("BTC/USDT", "long", 49000, 0.0012)
    b.client.log.clear()
    new = b.replace_stop("BTC/USDT", "long", 50000, old_order_id=old,
                         old_stop=49000, amount=0.0006)
    order = [p.rsplit("/", 2)[-2:] for _, p, _ in b.client.log]
    assert order == [["planorder", "place"], ["planorder", "cancel"]]
    (placed,) = calls(b, "/planorder/place")
    assert placed["vol"] == 6 and placed["triggerPrice"] == "50000"
    (cancel,) = calls(b, "/planorder/cancel")
    assert cancel == [{"symbol": "BTC_USDT", "orderId": old}]
    assert [str(o["id"]) for o in b.client.plan_orders] == [new]


def test_replace_stop_falls_back_to_price_match_without_an_id():
    b = broker(hold_vol=12)
    b.place_stop("BTC/USDT", "long", 49000, 0.0012)
    new = b.replace_stop("BTC/USDT", "long", 49000, old_order_id=None,
                         old_stop=49000, amount=0.0006)
    # the old same-price stop is gone; the NEW one is never cancelled
    assert [str(o["id"]) for o in b.client.plan_orders] == [new]


def test_close_all_closes_then_cancels_both_order_kinds():
    b = broker(hold_vol=12)
    assert b.close_all() == []
    (close,) = calls(b, "/order/create")
    assert close["side"] == 4 and close["vol"] == 12
    assert calls(b, "/order/cancel_all") == [{"symbol": "BTC_USDT"}]
    assert calls(b, "/planorder/cancel_all") == [{"symbol": "BTC_USDT"}]


def test_unexecuted_close_keeps_the_stop_and_raises():
    # MEXC only ACCEPTS a close order; if it does not execute, the stop and
    # take-profits must survive and the caller must be told
    b = broker()
    b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
    b.client.ignore_closes = True
    try:
        b.close_position("BTC/USDT")
        raise AssertionError("an unconfirmed close must raise")
    except RuntimeError as e:
        assert "still open" in str(e)
    assert len(b.client.plan_orders) == 3
    assert calls(b, "/planorder/cancel_all") == [{"symbol": "BTC_USDT"}]  # entry only
    b.client.ignore_closes = False
    b.close_position("BTC/USDT")
    assert b.open_positions() == [] and b.client.plan_orders == []


def test_one_way_account_is_switched_to_hedge_mode_or_refused():
    b = broker(hedged=False)
    b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
    assert calls(b, "/position/change_position_mode") == [{"positionMode": 1}]
    assert calls(b, "/order/create")[0]["positionMode"] == 1
    # if the switch is refused (open orders/positions), nothing is traded
    b = broker(hedged=False, refuse_mode_switch=True)
    try:
        b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
        raise AssertionError("must refuse to trade in one-way mode")
    except ccxt.BaseError as e:
        assert "Hedge" in str(e)
    assert calls(b, "/order/create") == []


def test_realized_pnl_reads_signed_closed_position_records():
    b = broker()
    row = {"positionId": 1, "symbol": "BTC_USDT", "positionType": 1,
           "openType": 1, "state": 3, "holdVol": 0, "closeVol": 12,
           "openAvgPrice": 50000, "closeAvgPrice": 49000,
           "liquidatePrice": 0, "leverage": 3, "createTime": T}
    b.client.history = [
        {**row, "realised": "-1.2600", "updateTime": T + 5000},   # ours: a loss
        {**row, "realised": "9.99", "updateTime": T - 5000},      # older trade
    ]
    assert abs(b.realized_pnl_since("BTC/USDT", T) - (-1.26)) < 1e-9
    b.client.history = [{**row, "realised": "9.99", "updateTime": T - 5000}]
    try:
        b.realized_pnl_since("BTC/USDT", T)
        raise AssertionError("must wait for the closed-position record")
    except RuntimeError:
        pass


def test_setup_symbol_sets_leverage_for_both_sides():
    b = broker()
    b.setup_symbol("BTC/USDT")
    sent = calls(b, "/position/change_leverage")
    assert [(c["positionType"], c["openType"], c["leverage"]) for c in sent] \
        == [(1, 1, 3), (2, 1, 3)]


def test_market_data_uses_swap_market_and_base_unit_depth():
    cfg = load_config("config.yaml", require_keys=False)
    cfg.exchange = "mexc"
    md = MarketData.__new__(MarketData)
    md.cfg, md.is_mexc, md._cache = cfg, True, {}
    md.client = Wire({"options": {"defaultType": "swap"}})
    md.client.load_markets()
    assert md._s("BTC/USDT") == "BTC/USDT:USDT"
    book = md.book_summary("BTC/USDT", 10)
    # 5000 contracts x 0.0001 BTC = 0.5 BTC on each side
    assert abs(book.bid_depth - 0.5) < 1e-9 and abs(book.ask_depth - 0.5) < 1e-9
    assert book.bid == 50000 and book.ask == 50010
    assert md.oi_change_pct("BTC/USDT") is None          # not published


# ---- trade manager behaviour that only matters on sized-stop exchanges ----
class _Risk:
    def __init__(self):
        from types import SimpleNamespace
        self.state = SimpleNamespace(managed={}, daily_pnl=0.0, trades_today=0,
                                     equity_day_start=1000.0,
                                     consecutive_losses=0, date="d")
        self.closed = []

    def record_open(self, symbol, info, equity):
        self.state.managed[symbol] = info

    def record_close(self, symbol, pnl, equity):
        self.closed.append((symbol, pnl))
        self.state.managed.pop(symbol, None)

    def save(self):
        pass


class _Journal:
    def __init__(self):
        self.events = []

    def position(self, event, symbol, **kw):
        self.events.append(event)

    def error(self, *a, **k):
        self.events.append("error")


def _managed(b, price):
    import pandas as pd
    from types import SimpleNamespace
    idx = pd.date_range("2024-01-01", periods=60, freq="15min", tz="UTC")
    candles = pd.DataFrame({"open": 50000.0, "high": 50050.0, "low": 49950.0,
                            "close": 50000.0, "volume": 1.0}, index=idx)
    cfg = b.cfg
    cfg.exits.exit_on_opposing_signal = False
    risk, journal = _Risk(), _Journal()
    mgr = TradeManager(cfg, b, risk, journal)
    decision = SimpleNamespace(direction="LONG", symbol="BTC/USDT",
                               entry_price=50000.0, stop_loss=49000.0,
                               take_profit_1=51000.0, take_profit_2=52500.0)
    out = b.enter("BTC/USDT", "long", 0.0012, 49000, 51000, 0.0006, 52500)
    mgr.on_entry(decision, 0.0012, 1000.0, sl_order_id=out["sl_order_id"])
    snap = SimpleNamespace(symbol="BTC/USDT", last_price=price,
                           candles=candles)
    return mgr, risk, journal, snap


def test_manager_resizes_stop_after_a_partial_close():
    b = broker()
    mgr, risk, journal, snap = _managed(b, 50400.0)   # below +1R: no BE move
    mgr.manage(snap)
    assert "stop_resized" not in journal.events        # size unchanged: no-op
    b.client.hold_vol = 6                              # TP1 partial filled
    b.client.plan_orders = [o for o in b.client.plan_orders
                            if o["triggerPrice"] != "51000"]
    mgr.manage(snap)
    assert "stop_resized" in journal.events
    stops = [o for o in b.client.plan_orders if o["triggerType"] == 2]
    assert [(o["triggerPrice"], o["vol"]) for o in stops] == [("49000", 6)]
    assert risk.state.managed["BTC/USDT"]["stop_amount"] == 0.0006


def test_manager_refreshes_stop_before_it_expires():
    b = broker()
    mgr, risk, journal, snap = _managed(b, 50400.0)
    info = risk.state.managed["BTC/USDT"]
    old_id = info["sl_order_id"]
    info["stop_ts"] -= MexcBroker.STOP_REFRESH_S + 60
    mgr.manage(snap)
    assert "stop_resized" in journal.events
    assert info["sl_order_id"] != old_id
    stops = [o for o in b.client.plan_orders if o["triggerType"] == 2]
    assert len(stops) == 1 and stops[0]["vol"] == 12


def test_manager_failsafe_closes_when_price_is_past_the_stop():
    b = broker()
    mgr, risk, journal, snap = _managed(b, 48700.0)   # 0.3R beyond the stop
    row = {"positionId": 1, "symbol": "BTC_USDT", "positionType": 1,
           "state": 3, "holdVol": 0, "realised": "-1.60",
           "updateTime": b.client.milliseconds() + 1000}
    b.client.history = [row]
    mgr.manage(snap)
    assert "stop_failsafe_close" in journal.events
    assert b.open_positions() == []
    assert risk.closed == [("BTC/USDT", -1.60)]
    # just past the stop is left to the exchange (no racing its own stop)
    b2 = broker()
    mgr2, _, journal2, snap2 = _managed(b2, 48900.0)
    mgr2.manage(snap2)
    assert "stop_failsafe_close" not in journal2.events


def test_manager_failsafe_runs_while_the_stop_is_known_missing():
    b = broker()
    mgr, risk, journal, snap = _managed(b, 48000.0)   # a full 1R past the stop
    risk.state.managed["BTC/USDT"]["needs_protection"] = True
    b.client.reject_plan = True                       # stop cannot be restored
    b.client.history = [{"positionId": 1, "symbol": "BTC_USDT",
                         "positionType": 1, "state": 3, "holdVol": 0,
                         "realised": "-2.40",
                         "updateTime": b.client.milliseconds() + 1000}]
    mgr.manage(snap)
    assert "stop_failsafe_close" in journal.events
    assert b.open_positions() == [] and risk.closed == [("BTC/USDT", -2.40)]


def test_manager_keeps_position_managed_when_failsafe_close_is_unconfirmed():
    b = broker()
    mgr, risk, journal, snap = _managed(b, 48700.0)
    b.client.ignore_closes = True
    mgr.manage(snap)
    assert "BTC/USDT" in risk.state.managed and risk.closed == []
    assert len(b.client.plan_orders) == 3             # brackets untouched
    b.client.ignore_closes = False
    b.client.history = [{"positionId": 1, "symbol": "BTC_USDT",
                         "positionType": 1, "state": 3, "holdVol": 0,
                         "realised": "-1.60",
                         "updateTime": b.client.milliseconds() + 1000}]
    mgr.manage(snap)                                  # retried next tick
    assert risk.closed == [("BTC/USDT", -1.60)]


def test_config_rejects_mexc_testnet_and_reads_mexc_keys():
    keep = {k: os.environ.get(k) for k in
            ("BOT_EXCHANGE", "BOT_MODE", "MEXC_API_KEY", "MEXC_SECRET_KEY")}
    try:
        os.environ.update(BOT_EXCHANGE="mexc", BOT_MODE="testnet")
        try:
            load_config("config.yaml", require_keys=False)
            raise AssertionError("mexc + testnet must be refused")
        except SystemExit as e:
            assert "no futures testnet" in str(e)
        os.environ.update(BOT_MODE="live", MEXC_API_KEY="mk",
                          MEXC_SECRET_KEY="ms")
        cfg = load_config("config.yaml")
        assert (cfg.exchange, cfg.exchange_name) == ("mexc", "MEXC")
        assert (cfg.api_key, cfg.api_secret) == ("mk", "ms")
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
