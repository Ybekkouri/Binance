"""Exchange contract tests: the REAL ccxt Binance client, offline.

Only ccxt's lowest-level HTTP transport is replaced. Everything above it —
request building, order-type inference, endpoint routing, signing,
response parsing — is the genuine library code the live bot runs. These
tests pin down exactly what the bot puts on the wire, so a ccxt upgrade
that changes behaviour (as the ledger sign and order-type handling once
did) fails here instead of on the exchange.

Run with:  python3 tests/test_exchange_contract.py   (or pytest)
"""

import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import ccxt  # noqa: E402

from binance_fixture import EXCHANGE_INFO  # noqa: E402
from bot.broker import LiveBroker  # noqa: E402
from bot.config import load_config  # noqa: E402

T = 1700000000000
IDS = ("newClientOrderId", "clientAlgoId")


class Wire(ccxt.binanceusdm):
    """Real ccxt client with an in-memory exchange behind it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.log = []
        self.position_amt = "0"
        self.income = []
        self.reject_algo = False

    def fetch(self, url, method="GET", headers=None, body=None):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if body:
            q.update(dict(urllib.parse.parse_qsl(body)))
        for k in ("signature", "timestamp", "recvWindow"):
            q.pop(k, None)
        path = parsed.path
        if "/sapi/" in path:
            return []
        if path.endswith("/exchangeInfo"):
            return EXCHANGE_INFO
        self.log.append((method, path, {k: v for k, v in q.items()
                                        if k not in IDS}))
        if path.endswith("/algoOrder") and method == "POST":
            if self.reject_algo:
                raise ccxt.ExchangeError('binanceusdm {"code":-2021,"msg":'
                                         '"Order would immediately trigger."}')
            return {"algoId": 9000 + len(self.log), "algoType": "CONDITIONAL",
                    "orderType": q.get("type"), "symbol": q["symbol"],
                    "side": q["side"], "positionSide": "BOTH",
                    "quantity": q.get("quantity", "0"), "algoStatus": "NEW",
                    "triggerPrice": q.get("triggerPrice"), "price": "0",
                    "closePosition": q.get("closePosition") == "true",
                    "reduceOnly": q.get("reduceOnly") == "true",
                    "workingType": "CONTRACT_PRICE", "createTime": T,
                    "updateTime": T}
        if path.endswith("/algoOrder") and method == "DELETE":
            return {"algoId": int(q.get("algoId", 0)), "code": "200",
                    "msg": "success"}
        if path.endswith("/order") and method == "POST":
            if q.get("reduceOnly") == "true":
                self.position_amt = "0"
            elif q.get("type") == "MARKET":
                sign = "" if q["side"] == "BUY" else "-"
                self.position_amt = sign + q["quantity"]
            return {"orderId": len(self.log), "symbol": q["symbol"],
                    "status": "FILLED", "price": "0", "avgPrice": "100",
                    "origQty": q.get("quantity"),
                    "executedQty": q.get("quantity"), "cumQuote": "0",
                    "timeInForce": "GTC", "type": q["type"],
                    "reduceOnly": q.get("reduceOnly") == "true",
                    "closePosition": False, "side": q["side"],
                    "positionSide": "BOTH", "stopPrice": "0",
                    "origType": q["type"], "updateTime": T}
        if path.endswith("allOpenOrders") or path.endswith("algoOpenOrders"):
            return {"code": 200, "msg": "The operation of cancel all open "
                    "order is done."}
        if "openAlgoOrders" in path or path.endswith("/openOrders"):
            return []
        if path.endswith("/leverageBracket"):
            return [{"symbol": "BTCUSDT", "brackets": [
                {"bracket": 1, "initialLeverage": 125, "notionalCap": 50000,
                 "notionalFloor": 0, "maintMarginRatio": 0.004, "cum": 0}]}]
        if path.endswith("/marginType"):
            return {"code": 200, "msg": "success"}
        if path.endswith("/leverage"):
            return {"leverage": int(q["leverage"]),
                    "maxNotionalValue": "1000000", "symbol": q["symbol"]}
        if "/balance" in path or path.endswith("/account"):
            return [{"accountAlias": "x", "asset": "USDT", "balance": "1000.00",
                     "crossWalletBalance": "1000.00", "crossUnPnl": "0",
                     "availableBalance": "1000.00",
                     "maxWithdrawAmount": "1000.00", "marginAvailable": True,
                     "updateTime": T}]
        if "positionRisk" in path:
            if float(self.position_amt) == 0:
                return []
            return [{"symbol": "BTCUSDT", "positionSide": "BOTH",
                     "positionAmt": self.position_amt, "entryPrice": "100",
                     "breakEvenPrice": "100", "markPrice": "101",
                     "unRealizedProfit": "2", "liquidationPrice": "50",
                     "isolatedMargin": "70", "notional": "202",
                     "marginAsset": "USDT", "isolatedWallet": "68",
                     "initialMargin": "67", "maintMargin": "1",
                     "positionInitialMargin": "67",
                     "openOrderInitialMargin": "0", "adl": 0,
                     "bidNotional": "0", "askNotional": "0",
                     "updateTime": T}]
        if path.endswith("/income"):
            return [r for r in self.income
                    if r["incomeType"] == q.get("incomeType")]
        raise ccxt.NetworkError(f"unhandled {method} {path}")


def broker():
    cfg = load_config("config.yaml", require_keys=False)
    cfg.symbols = ["BTC/USDT"]
    b = LiveBroker.__new__(LiveBroker)
    b.cfg = cfg
    b.client = Wire({"apiKey": "k", "secret": "s"})
    b.client.load_markets()
    b.client.log.clear()
    return b


def calls(b, path_suffix, method=None):
    return [q for m, p, q in b.client.log
            if p.endswith(path_suffix) and (method is None or m == method)]


# ------------------------------------------------------------ tests
def test_entry_bracket_on_the_wire():
    b = broker()
    r = b.enter("BTC/USDT", "long", 2.0, stop=98.0, tp1=102.0,
                tp1_amount=1.0, tp2=105.0)
    paths = [(m, p) for m, p, _ in b.client.log]
    # leftovers cleared first (regular AND conditional), then the entry
    assert paths[0] == ("DELETE", "/fapi/v1/allOpenOrders")
    assert paths[1] == ("DELETE", "/fapi/v1/algoOpenOrders")
    entry = calls(b, "/fapi/v1/order", "POST")
    assert entry == [{"symbol": "BTCUSDT", "side": "BUY",
                      "newOrderRespType": "RESULT", "type": "MARKET",
                      "quantity": "2"}]
    sl, tp1, tp2 = calls(b, "/fapi/v1/algoOrder", "POST")
    # stops and take-profits go to the CONDITIONAL algo endpoint, with the
    # right types (a raw params type used to turn TPs into instant stops)
    assert sl["type"] == "STOP_MARKET" and sl["triggerPrice"] == "98"
    assert sl["closePosition"] == "true" and "quantity" not in sl
    assert tp1["type"] == "TAKE_PROFIT_MARKET" and tp1["triggerPrice"] == "102"
    assert tp1["quantity"] == "1" and tp1["reduceOnly"] == "true"
    assert "closePosition" not in tp1
    assert tp2["type"] == "TAKE_PROFIT_MARKET" and tp2["triggerPrice"] == "105"
    assert tp2["closePosition"] == "true" and "quantity" not in tp2
    for o in (sl, tp1, tp2):
        assert o["side"] == "SELL" and o["algoType"] == "CONDITIONAL"
    assert r["sl_order_id"]


def test_short_entry_bracket_sides():
    b = broker()
    b.enter("BTC/USDT", "short", 2.0, stop=102.0, tp1=98.0,
            tp1_amount=1.0, tp2=95.0)
    assert calls(b, "/fapi/v1/order", "POST")[0]["side"] == "SELL"
    algo = calls(b, "/fapi/v1/algoOrder", "POST")
    assert [o["side"] for o in algo] == ["BUY", "BUY", "BUY"]
    assert algo[0]["type"] == "STOP_MARKET" and algo[0]["triggerPrice"] == "102"


def test_bracket_rejection_flattens_the_position():
    b = broker()
    b.client.reject_algo = True
    try:
        b.enter("BTC/USDT", "long", 2.0, 98.0, 102.0, 1.0, 105.0)
        raise AssertionError("expected the bracket failure to propagate")
    except ccxt.ExchangeError:
        pass
    closes = [q for q in calls(b, "/fapi/v1/order", "POST")
              if q.get("reduceOnly") == "true"]
    assert closes and closes[0]["side"] == "SELL"
    assert float(b.client.position_amt) == 0


def test_stop_replacement_new_before_old():
    b = broker()
    b.client.position_amt = "2.000"
    b.replace_stop("BTC/USDT", "long", 100.0, old_order_id="9004", amount=2.0)
    (m1, p1, new), (m2, p2, old) = b.client.log
    assert (m1, p1) == ("POST", "/fapi/v1/algoOrder")
    assert new["type"] == "STOP_MARKET" and new["triggerPrice"] == "100"
    assert new["reduceOnly"] == "true" and new["quantity"] == "2"
    # a second closePosition stop would be rejected by Binance (-4130)
    assert "closePosition" not in new
    assert (m2, p2) == ("DELETE", "/fapi/v1/algoOrder")
    assert old["algoId"] == "9004"


def test_reprotection_stop():
    b = broker()
    b.place_stop("BTC/USDT", "short", 103.5, 1.5)
    (q,) = calls(b, "/fapi/v1/algoOrder", "POST")
    assert q["side"] == "BUY" and q["type"] == "STOP_MARKET"
    assert q["triggerPrice"] == "103.5" and q["quantity"] == "1.5"
    assert q["reduceOnly"] == "true"


def test_setup_positions_balance_and_close():
    b = broker()
    b.setup_symbol("BTC/USDT")
    assert calls(b, "/marginType") == [{"symbol": "BTCUSDT",
                                        "marginType": "ISOLATED"}]
    assert calls(b, "/leverage") == [{"symbol": "BTCUSDT", "leverage": "3"}]
    b.client.position_amt = "2.000"
    (pos,) = b.open_positions()
    assert pos["symbol"] == "BTC/USDT" and pos["side"] == "long"
    assert pos["contracts"] == 2.0 and pos["liquidation_price"] == 50.0
    assert b.equity_usdt() == 1000.0
    b.client.log.clear()
    assert b.close_all() == []
    close = [q for q in calls(b, "/fapi/v1/order", "POST")]
    assert close == [{"symbol": "BTCUSDT", "side": "SELL",
                      "newOrderRespType": "RESULT", "type": "MARKET",
                      "quantity": "2", "reduceOnly": "true"}]
    # verified flat, so leftovers on the symbol are cleaned up
    assert calls(b, "algoOpenOrders", "DELETE")


def test_ledger_sign_through_real_parser():
    b = broker()
    b.client.income = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL",
         "income": "-10.00000000", "asset": "USDT", "info": "", "time": T,
         "tranId": 1, "tradeId": "1"},
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
         "income": "-0.50000000", "asset": "USDT", "info": "", "time": T,
         "tranId": 2, "tradeId": "1"},
        {"symbol": "ETHUSDT", "incomeType": "COMMISSION",
         "income": "-9.00000000", "asset": "USDT", "info": "", "time": T,
         "tranId": 3, "tradeId": "2"},
        {"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE",
         "income": "0.20000000", "asset": "USDT", "info": "", "time": T,
         "tranId": 4, "tradeId": ""},
    ]
    row = b.client.fetch_ledger(since=0, limit=1000,
                                params={"incomeType": "REALIZED_PNL"})[0]
    # the trap: ccxt reports the loss as a POSITIVE amount
    assert row["amount"] == 10.0 and row["direction"] == "out"
    assert abs(b.realized_pnl_since("BTC/USDT", 0) - (-10.3)) < 1e-9


def test_pnl_not_ready_raises_for_retry():
    b = broker()
    b.client.income = [
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.5",
         "asset": "USDT", "info": "", "time": T, "tranId": 2, "tradeId": "1"}]
    try:
        b.realized_pnl_since("BTC/USDT", 0)
        raise AssertionError("must wait for the realized row")
    except RuntimeError:
        pass


def test_min_notional_and_precision_from_market():
    b = broker()
    assert b.min_notional("BTC/USDT") == 100.0
    assert b.amount_to_precision("BTC/USDT", 1.23456) == 1.234
    assert b.price_to_precision("BTC/USDT", 98.37) == 98.4   # nearest tick
    assert b.price_to_precision("BTC/USDT", 98.34) == 98.3


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
