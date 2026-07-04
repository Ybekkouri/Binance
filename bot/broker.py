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

    # ---- orders ----
    def enter(self, symbol: str, side: str, amount: float, stop: float,
              tp1: float, tp1_amount: float, tp2: float) -> dict:
        """Market entry + close-position stop + partial TP1 + close-position TP2."""
        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"
        entry = self.client.create_order(symbol, "market", order_side, amount)
        try:
            self.client.create_order(
                symbol, "market", close_side, None, None,
                {"stopPrice": self.price_to_precision(symbol, stop),
                 "type": "STOP_MARKET", "closePosition": True},
            )
            if tp1_amount > 0:
                self.client.create_order(
                    symbol, "market", close_side, tp1_amount, None,
                    {"stopPrice": self.price_to_precision(symbol, tp1),
                     "type": "TAKE_PROFIT_MARKET", "reduceOnly": True},
                )
            self.client.create_order(
                symbol, "market", close_side, None, None,
                {"stopPrice": self.price_to_precision(symbol, tp2),
                 "type": "TAKE_PROFIT_MARKET", "closePosition": True},
            )
        except ccxt.BaseError:
            log.exception("Bracket placement failed for %s — flattening.", symbol)
            self.close_position(symbol)
            raise
        return entry

    def replace_stop(self, symbol: str, side: str, new_stop: float) -> None:
        """Cancel the existing stop and place a new close-position stop.
        New stop goes in first only when it wouldn't instantly conflict; we
        cancel-then-place and re-flatten on failure to stay protected."""
        close_side = "sell" if side == "long" else "buy"
        for o in self.client.fetch_open_orders(symbol):
            if o.get("type", "").upper().startswith("STOP"):
                self.client.cancel_order(o["id"], symbol)
        try:
            self.client.create_order(
                symbol, "market", close_side, None, None,
                {"stopPrice": self.price_to_precision(symbol, new_stop),
                 "type": "STOP_MARKET", "closePosition": True},
            )
        except ccxt.BaseError:
            log.exception("Failed to replace stop on %s — closing position.", symbol)
            self.close_position(symbol)
            raise

    def cancel_all(self, symbol: str) -> None:
        try:
            self.client.cancel_all_orders(symbol)
        except ccxt.BaseError:
            log.exception("cancel_all_orders(%s) failed", symbol)

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

    def close_all(self) -> None:
        for p in self.open_positions():
            self.close_position(p["symbol"])

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        """Realized PnL + commissions + funding for a symbol since a time."""
        total = 0.0
        for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            entries = self.client.fetch_ledger(
                since=since_ms, params={"incomeType": income_type}
            )
            for e in entries:
                info = e.get("info", {})
                if info.get("symbol") == self.client.market_id(symbol):
                    total += float(e["amount"])
        return total


# ---------------------------------------------------------------- paper
class PaperBroker:
    """Simulated broker: fills at the current price with slippage and taker
    fees, triggers SL/TP against each poll's price, persists state to disk.

    The trader calls mark(symbol, price) every tick before reading state.
    """

    START_BALANCE = 1000.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = self._load()
        log.info("PaperBroker: balance %.2f USDT (simulated).", self.state["balance"])

    def _load(self) -> dict:
        if os.path.isfile(self.cfg.paper_state_file):
            try:
                with open(self.cfg.paper_state_file) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"balance": self.START_BALANCE, "positions": {}, "closed": []}

    def _save(self) -> None:
        with open(self.cfg.paper_state_file, "w") as f:
            json.dump(self.state, f, indent=1)

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
        return {"id": f"paper-{symbol}-{int(time.time())}", "price": fill}

    def replace_stop(self, symbol: str, side: str, new_stop: float) -> None:
        pos = self.state["positions"].get(symbol)
        if pos:
            pos["stop"] = new_stop
            self._save()

    def cancel_all(self, symbol: str) -> None:
        pass

    def mark(self, symbol: str, price: float) -> None:
        """Update the mark price and trigger any SL/TP fills."""
        self.state.setdefault("marks", {})[symbol] = price
        pos = self.state["positions"].get(symbol)
        if pos is None:
            self._save()
            return
        pos["mark"] = price
        sign = 1 if pos["side"] == "long" else -1

        def crossed(level: float, above: bool) -> bool:
            return price >= level if above else price <= level

        # Stop-loss (checked first: conservative)
        if crossed(pos["stop"], above=pos["side"] == "short"):
            self._close_amount(symbol, pos["contracts"], pos["stop"], "stop_loss")
        elif not pos["tp1_filled"] and crossed(pos["tp1"], above=pos["side"] == "long"):
            self._close_amount(symbol, pos["tp1_amount"], pos["tp1"], "take_profit_1")
            pos = self.state["positions"].get(symbol)
            if pos:
                pos["tp1_filled"] = True
        elif crossed(pos["tp2"], above=pos["side"] == "long"):
            self._close_amount(symbol, pos["contracts"], pos["tp2"], "take_profit_2")
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
            del self.state["positions"][symbol]

    def close_position(self, symbol: str) -> None:
        pos = self.state["positions"].get(symbol)
        if pos:
            self._close_amount(symbol, pos["contracts"], pos["mark"], "manual_close")
            self._save()

    def close_all(self) -> None:
        for symbol in list(self.state["positions"]):
            self.close_position(symbol)

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
