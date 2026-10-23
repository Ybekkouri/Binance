"""Broker layer: order execution and account state.

Two implementations behind one duck-typed interface:

  LiveBroker  — real orders via ccxt binanceusdm (testnet or live). Every
                entry is immediately protected by an exchange-side
                close-position stop, so a bot crash never leaves an
                unprotected position.
  PaperBroker — simulated fills against live market prices, with fees and
                slippage, persisted to disk. No API keys required.

Position dicts share the shape:
  {symbol, side ('long'/'short'), contracts, notional, entry_price,
   liquidation_price, margin_ratio}
"""

import json
import logging
import os
import time
from typing import Optional

import ccxt

log = logging.getLogger("bot.broker")


# ---------------------------------------------------------------- live
class LiveBroker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.binanceusdm({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True},
        })
        if cfg.mode == "testnet":
            self.client.set_sandbox_mode(True)
            log.info("LiveBroker on Binance Futures TESTNET.")
        else:
            log.warning("LiveBroker on LIVE Binance Futures — real money.")
        self.client.load_markets()

    def setup_symbol(self, symbol: str) -> None:
        try:
            self.client.set_margin_mode(self.cfg.margin_mode, symbol)
        except ccxt.BaseError as e:
            log.debug("set_margin_mode(%s): %s", symbol, e)  # already set is fine
        try:
            self.client.set_leverage(self.cfg.leverage, symbol)
        except ccxt.BaseError as e:
            log.debug("set_leverage(%s): %s", symbol, e)

    # ---- account ----
    def equity_usdt(self) -> float:
        return float(self.client.fetch_balance()["USDT"]["total"])

    def open_positions(self) -> list[dict]:
        out = []
        for p in self.client.fetch_positions(self.cfg.symbols):
            contracts = float(p.get("contracts") or 0)
            if contracts == 0:
                continue
            out.append({
                "symbol": p["symbol"].split(":")[0],
                "side": p["side"],
                "contracts": contracts,
                "notional": abs(float(p.get("notional") or 0)),
                "entry_price": float(p.get("entryPrice") or 0),
                "liquidation_price": float(p.get("liquidationPrice") or 0),
                "margin_ratio": float(p.get("marginRatio") or 0),
            })
        return out

    # ---- precision helpers ----
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.client.amount_to_precision(symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.client.price_to_precision(symbol, price))

    def min_notional(self, symbol: str) -> float:
        """Exchange minimum order value in USDT (0 when unknown)."""
        limits = self.client.market(symbol).get("limits", {})
        return float((limits.get("cost") or {}).get("min") or 0.0)

    # ---- orders ----
    # Conditional orders (stop-loss / take-profit) on Binance USDS-M route
    # through separate "algo" endpoints in ccxt. Two consequences shape this
    # code: order types must be expressed via the unified stopLossPrice /
    # takeProfitPrice params (a raw params["type"] is consumed as the MARKET
    # type and silently dropped, turning take-profits into instant-trigger
    # stops), and fetching/cancelling them requires params={"trigger": True}
    # (plain calls only see regular orders).

    def enter(self, symbol: str, side: str, amount: float, stop: float,
              tp1: float, tp1_amount: float, tp2: float) -> dict:
        """Market entry + close-position stop + partial TP1 + close-position TP2.

        Returns {"id": entry order id, "sl_order_id": stop order id}.
        """
        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"
        # Leftover trigger orders from a previous trade on this symbol could
        # close the new position at a stale level. We only enter when flat,
        # so clearing them here is always safe.
        self.cancel_all(symbol)
        try:
            entry = self.client.create_order(symbol, "market", order_side, amount)
        except ccxt.BaseError:
            # A client-side timeout can leave a filled order behind: flatten
            # defensively so no naked position survives an entry error.
            log.exception("Entry order failed on %s — defensive flatten.", symbol)
            try:
                self.close_position(symbol)
            except ccxt.BaseError:
                log.exception("Defensive flatten also failed — CHECK EXCHANGE.")
            raise
        try:
            sl = self.client.create_order(
                symbol, "market", close_side, None, None,
                {"stopLossPrice": self.price_to_precision(symbol, stop),
                 "closePosition": True},
            )
            if tp1_amount > 0:
                self.client.create_order(
                    symbol, "market", close_side, tp1_amount, None,
                    {"takeProfitPrice": self.price_to_precision(symbol, tp1),
                     "reduceOnly": True},
                )
            self.client.create_order(
                symbol, "market", close_side, None, None,
                {"takeProfitPrice": self.price_to_precision(symbol, tp2),
                 "closePosition": True},
            )
        except ccxt.BaseError:
            log.exception("Bracket placement failed for %s — flattening.", symbol)
            self.close_position(symbol)
            raise
        return {"id": entry.get("id"), "sl_order_id": sl.get("id")}

    def place_stop(self, symbol: str, side: str, stop: float,
                   amount: float) -> str:
        """Sized reduce-only stop. Used for replacements and re-protection:
        unlike closePosition stops, several can coexist (Binance allows only
        one closePosition stop per direction, error -4130), which lets a new
        stop go in BEFORE the old one is cancelled."""
        close_side = "sell" if side == "long" else "buy"
        order = self.client.create_order(
            symbol, "market", close_side,
            self.amount_to_precision(symbol, amount), None,
            {"stopLossPrice": self.price_to_precision(symbol, stop),
             "reduceOnly": True},
        )
        return order.get("id")

    def replace_stop(self, symbol: str, side: str, new_stop: float,
                     old_order_id: str = None, old_stop: float = None,
                     amount: float = None) -> str:
        """Place the new stop first, then cancel the old one, so the position
        is never without a stop. Raises if the new stop cannot be placed —
        the old stop is then still live and the caller retries later."""
        if amount is None:
            pos = next((p for p in self.open_positions()
                        if p["symbol"] == symbol), None)
            if pos is None:
                return None
            amount = pos["contracts"]
        new_id = self.place_stop(symbol, side, new_stop, amount)

        cancelled = False
        if old_order_id:
            try:
                self.client.cancel_order(old_order_id, symbol,
                                         {"trigger": True})
                cancelled = True
            except ccxt.BaseError as e:
                log.warning("cancel stop %s by id failed (%s); falling back "
                            "to trigger-price match", old_order_id, e)
        if not cancelled and old_stop:
            try:
                for o in self.client.fetch_open_orders(symbol,
                                                       params={"trigger": True}):
                    if o.get("id") == new_id:
                        continue
                    trig = float(o.get("triggerPrice") or
                                 o.get("stopPrice") or 0)
                    if trig and abs(trig - old_stop) / old_stop < 0.002:
                        self.client.cancel_order(o["id"], symbol,
                                                 {"trigger": True})
            except ccxt.BaseError:
                # a leftover looser stop is harmless (reduce-only, further away)
                log.exception("old-stop cleanup failed on %s", symbol)
        return new_id

    def cancel_all(self, symbol: str) -> bool:
        """Cancel BOTH regular and conditional (trigger) orders.
        Returns False when a cancel call failed for a reason other than
        there being nothing to cancel."""
        ok = True
        for params in ({}, {"trigger": True}):
            try:
                self.client.cancel_all_orders(symbol, params=params)
            except ccxt.OrderNotFound:
                pass
            except ccxt.BaseError as e:
                if "no" in str(e).lower() and "order" in str(e).lower():
                    continue   # "no orders to cancel" variants
                ok = False
                log.warning("cancel_all_orders(%s, %s) failed: %s",
                            symbol, params, e)
        return ok

    def close_position(self, symbol: str) -> None:
        for p in self.open_positions():
            if p["symbol"] != symbol:
                continue
            side = "sell" if p["side"] == "long" else "buy"
            self.client.create_order(
                symbol, "market", side, p["contracts"], None, {"reduceOnly": True}
            )
            log.info("Closed %s position on %s.", p["side"], symbol)
        self.cancel_all(symbol)

    def close_all(self) -> list:
        """Flatten every position, continuing past per-symbol errors, then
        verify. Returns the symbols still open afterwards (empty = flat).
        Orders are cancelled only on symbols confirmed flat, so a position
        that failed to close keeps its exchange-side stop."""
        for p in self.open_positions():
            try:
                side = "sell" if p["side"] == "long" else "buy"
                self.client.create_order(p["symbol"], "market", side,
                                         p["contracts"], None,
                                         {"reduceOnly": True})
                log.info("Closed %s position on %s.", p["side"], p["symbol"])
            except ccxt.BaseError:
                log.exception("Failed to close %s", p["symbol"])
        still_open = [p["symbol"] for p in self.open_positions()]
        for symbol in self.cfg.symbols:
            if symbol not in still_open:
                self.cancel_all(symbol)
        return still_open

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        """Realized PnL + commissions + funding for a symbol since a time.

        ccxt returns ledger `amount` as an ABSOLUTE value with the sign in
        `direction`, so the raw signed `income` is read instead. Paginates
        (the endpoint caps rows per call across all symbols), and raises if
        no REALIZED_PNL row exists yet: Binance income rows lag fills by a
        few seconds, and the caller retries instead of booking a partial."""
        market_id = self.client.market_id(symbol)
        total = 0.0
        saw_realized = False
        for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            since = since_ms
            for _ in range(10):  # hard cap: 10k rows per income type
                entries = self.client.fetch_ledger(
                    since=since, limit=1000, params={"incomeType": income_type}
                )
                for e in entries:
                    info = e.get("info", {})
                    if info.get("symbol") != market_id:
                        continue
                    total += _signed_income(e)
                    if income_type == "REALIZED_PNL":
                        saw_realized = True
                if len(entries) < 1000:
                    break
                since = int(entries[-1]["timestamp"]) + 1
        if not saw_realized:
            raise RuntimeError(f"no realized PnL row for {symbol} yet")
        return total


def _signed_income(entry: dict) -> float:
    """Signed amount of a ccxt ledger entry (raw `income` when present,
    otherwise `amount` signed by `direction`)."""
    raw = entry.get("info", {}).get("income")
    if raw is not None:
        return float(raw)
    amount = float(entry.get("amount") or 0)
    return -amount if entry.get("direction") == "out" else amount


# ---------------------------------------------------------------- paper
class PaperBroker:
    """Simulated broker: fills at the current price with slippage and taker
    fees, triggers SL/TP against each poll's price, persists state to disk.

    The trader calls mark(symbol, price) every tick before reading state.
    """

    START_BALANCE = 1000.0

    def __init__(self, cfg, state_file: str = None, start_balance: float = None):
        self.cfg = cfg
        self.state_file = state_file or cfg.paper_state_file
        self.start_balance = start_balance or self.START_BALANCE
        self.state = self._load()
        log.info("PaperBroker[%s]: balance %.2f USDT (simulated).",
                 self.state_file, self.state["balance"])

    def _load(self) -> dict:
        if os.path.isfile(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"balance": self.start_balance, "positions": {}, "closed": []}

    def _save(self) -> None:
        # atomic write: a crash mid-write must never truncate the state file
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_file)

    def setup_symbol(self, symbol: str) -> None:
        pass

    # ---- account ----
    def equity_usdt(self) -> float:
        eq = self.state["balance"]
        for pos in self.state["positions"].values():
            sign = 1 if pos["side"] == "long" else -1
            eq += sign * (pos["mark"] - pos["entry_price"]) * pos["contracts"]
        return eq

    def open_positions(self) -> list[dict]:
        out = []
        for symbol, pos in self.state["positions"].items():
            lev = max(self.cfg.leverage, 1)
            sign = 1 if pos["side"] == "long" else -1
            liq = pos["entry_price"] * (1 - sign * 0.99 / lev)
            out.append({
                "symbol": symbol,
                "side": pos["side"],
                "contracts": pos["contracts"],
                "notional": pos["contracts"] * pos["mark"],
                "entry_price": pos["entry_price"],
                "liquidation_price": liq,
                "margin_ratio": 0.0,
            })
        return out

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return round(amount, 6)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return round(price, 6)

    def min_notional(self, symbol: str) -> float:
        return 0.0  # simulated fills have no exchange minimum

    def _fee(self, notional: float) -> float:
        return notional * self.cfg.taker_fee_pct / 100

    def _slip(self, price: float, side: str) -> float:
        s = self.cfg.slippage_pct / 100
        return price * (1 + s) if side == "buy" else price * (1 - s)

    # ---- orders ----
    def enter(self, symbol: str, side: str, amount: float, stop: float,
              tp1: float, tp1_amount: float, tp2: float) -> dict:
        if symbol in self.state["positions"]:
            raise RuntimeError(f"paper position already open in {symbol}")
        mark = self.state.get("marks", {}).get(symbol)
        if mark is None:
            raise RuntimeError(f"no mark price for {symbol}; call mark() first")
        fill = self._slip(mark, "buy" if side == "long" else "sell")
        fee = self._fee(fill * amount)
        self.state["balance"] -= fee
        self.state["positions"][symbol] = {
            "side": side, "contracts": amount, "entry_price": fill,
            "stop": stop, "tp1": tp1, "tp1_amount": tp1_amount, "tp2": tp2,
            "tp1_filled": tp1_amount <= 0, "mark": fill, "fees": fee,
            "realized": -fee, "opened_ms": int(time.time() * 1000),
        }
        self._save()
        log.info("[paper] entry %s %s %.6f @ %.2f (fee %.4f)", side, symbol, amount, fill, fee)
        return {"id": f"paper-{symbol}-{int(time.time())}", "price": fill,
                "sl_order_id": None}

    def replace_stop(self, symbol: str, side: str, new_stop: float,
                     old_order_id: str = None, old_stop: float = None,
                     amount: float = None):
        pos = self.state["positions"].get(symbol)
        if pos:
            pos["stop"] = new_stop
            self._save()
        return None

    def place_stop(self, symbol: str, side: str, stop: float,
                   amount: float):
        return self.replace_stop(symbol, side, stop)

    def cancel_all(self, symbol: str) -> bool:
        return True

    def mark(self, symbol: str, price: float) -> None:
        """Update the mark price and trigger any SL/TP fills.

        Realism rules: a stop that has been gapped through fills at the
        (worse) current price, not the trigger; take-profits fill at their
        trigger (never credited gap upside); after a TP1 partial the
        remaining brackets are re-checked in the same tick."""
        self.state.setdefault("marks", {})[symbol] = price
        pos = self.state["positions"].get(symbol)
        if pos is None:
            self._save()
            return
        pos["mark"] = price
        sign = 1 if pos["side"] == "long" else -1

        def crossed(level: float, above: bool) -> bool:
            return price >= level if above else price <= level

        for _ in range(2):   # a TP1 fill may be followed by TP2/stop same tick
            pos = self.state["positions"].get(symbol)
            if pos is None:
                break
            if crossed(pos["stop"], above=pos["side"] == "short"):
                # gap-through fills at the worse of trigger vs market
                fill = (min(pos["stop"], price) if sign > 0
                        else max(pos["stop"], price))
                self._close_amount(symbol, pos["contracts"], fill, "stop_loss")
                break
            if not pos["tp1_filled"] and crossed(pos["tp1"],
                                                 above=pos["side"] == "long"):
                self._close_amount(symbol, pos["tp1_amount"], pos["tp1"],
                                   "take_profit_1")
                pos = self.state["positions"].get(symbol)
                if pos:
                    pos["tp1_filled"] = True
                continue
            if crossed(pos["tp2"], above=pos["side"] == "long"):
                self._close_amount(symbol, pos["contracts"], pos["tp2"],
                                   "take_profit_2")
            break
        self._save()

    def _close_amount(self, symbol: str, amount: float, price: float, reason: str) -> None:
        pos = self.state["positions"][symbol]
        amount = min(amount, pos["contracts"])
        sign = 1 if pos["side"] == "long" else -1
        fill = self._slip(price, "sell" if pos["side"] == "long" else "buy")
        gross = sign * (fill - pos["entry_price"]) * amount
        fee = self._fee(fill * amount)
        self.state["balance"] += gross - fee
        pos["realized"] += gross - fee
        pos["fees"] += fee
        pos["contracts"] -= amount
        log.info("[paper] %s %s %.6f @ %.2f pnl %+.4f", reason, symbol, amount, fill, gross - fee)
        if pos["contracts"] <= 1e-12:
            self.state["closed"].append({
                "symbol": symbol, "pnl": pos["realized"] - 0.0,
                "fees": pos["fees"], "reason": reason,
                "closed_ms": int(time.time() * 1000),
                "opened_ms": pos["opened_ms"],
            })
            # bound the history so the per-tick state rewrite stays small
            self.state["closed"] = self.state["closed"][-500:]
            del self.state["positions"][symbol]

    def close_position(self, symbol: str) -> None:
        pos = self.state["positions"].get(symbol)
        if pos:
            self._close_amount(symbol, pos["contracts"], pos["mark"], "manual_close")
            self._save()

    def close_all(self) -> list:
        for symbol in list(self.state["positions"]):
            self.close_position(symbol)
        return list(self.state["positions"])

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        return sum(
            t["pnl"] for t in self.state["closed"]
            if t["symbol"] == symbol and t["closed_ms"] >= since_ms
        )

    def last_exit_reason(self, symbol: str) -> Optional[str]:
        for t in reversed(self.state["closed"]):
            if t["symbol"] == symbol:
                return t["reason"]
        return None


def make_broker(cfg):
    return PaperBroker(cfg) if cfg.mode == "paper" else LiveBroker(cfg)
