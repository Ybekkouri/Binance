"""Configuration loading for the bot: YAML for parameters, env for secrets."""

import os
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv


@dataclass
class Config:
    # exchange
    testnet: bool
    api_key: str
    api_secret: str
    # trading
    symbol: str
    timeframe: str
    leverage: int
    margin_mode: str
    # strategy
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    atr_period: int
    stop_atr_mult: float
    take_profit_atr_mult: float
    # risk
    risk_per_trade_pct: float
    daily_profit_target: float
    daily_max_loss: float
    max_position_notional: float
    # bot
    poll_seconds: int
    state_file: str


def load_config(path: str = "config.yaml") -> Config:
    load_dotenv()
    with open(path) as f:
        raw = yaml.safe_load(f)

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_SECRET_KEY", "")
    if not api_key or not api_secret:
        raise SystemExit(
            "BINANCE_API_KEY / BINANCE_SECRET_KEY not set. "
            "Copy .env.example to .env and fill in your keys."
        )

    ex, tr, st, rk, bt = (
        raw["exchange"], raw["trading"], raw["strategy"], raw["risk"], raw["bot"]
    )
    return Config(
        testnet=bool(ex["testnet"]),
        api_key=api_key,
        api_secret=api_secret,
        symbol=tr["symbol"],
        timeframe=tr["timeframe"],
        leverage=int(tr["leverage"]),
        margin_mode=tr["margin_mode"],
        ema_fast=int(st["ema_fast"]),
        ema_slow=int(st["ema_slow"]),
        rsi_period=int(st["rsi_period"]),
        rsi_overbought=float(st["rsi_overbought"]),
        rsi_oversold=float(st["rsi_oversold"]),
        atr_period=int(st["atr_period"]),
        stop_atr_mult=float(st["stop_atr_mult"]),
        take_profit_atr_mult=float(st["take_profit_atr_mult"]),
        risk_per_trade_pct=float(rk["risk_per_trade_pct"]),
        daily_profit_target=float(rk["daily_profit_target"]),
        daily_max_loss=float(rk["daily_max_loss"]),
        max_position_notional=float(rk["max_position_notional"]),
        poll_seconds=int(bt["poll_seconds"]),
        state_file=bt["state_file"],
    )
