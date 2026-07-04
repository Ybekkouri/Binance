"""Configuration loading: YAML for parameters, environment for secrets.

The config is deliberately strict — unknown modes or missing keys fail fast
at startup rather than surprising us mid-trade.
"""

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

MODES = ("testnet", "paper", "live")


@dataclass
class StrategyConfig:
    min_confidence: float
    min_aligned_factors: int
    weights: dict
    ema_fast: int
    ema_slow: int
    trend_ema_fast: int
    trend_ema_slow: int
    rsi_period: int
    atr_period: int
    breakout_lookback: int
    swing_lookback: int
    swing_window: int
    volume_ma: int
    volume_expansion: float
    extension_atr_max: float
    book_depth_levels: int
    book_imbalance_ratio: float
    min_atr_pct: float
    max_atr_pct: float
    max_funding_against: float
    btc_filter: bool


@dataclass
class ExitConfig:
    stop_atr_mult: float
    tp1_r: float
    tp1_fraction: float
    tp2_r: float
    breakeven_at_r: float
    trail_atr_mult: float
    trail_min_step_pct: float
    exit_on_opposing_signal: bool


@dataclass
class RiskConfig:
    risk_per_trade_pct: float
    max_leverage: int
    max_open_positions: int
    max_symbol_exposure_mult: float
    max_total_exposure_mult: float
    daily_max_loss_pct: float
    daily_profit_target_pct: float
    weekly_max_drawdown_pct: float
    max_consecutive_losses: int
    max_trades_per_day: int
    min_risk_reward: float
    max_spread_pct: float
    min_quote_volume_24h: float
    min_book_depth_mult: float


@dataclass
class Config:
    mode: str
    api_key: str
    api_secret: str
    # trading
    symbols: list
    timeframe: str
    trend_timeframe: str
    leverage: int
    margin_mode: str
    # engines
    strategy: StrategyConfig = field(default=None)
    exits: ExitConfig = field(default=None)
    risk: RiskConfig = field(default=None)
    # execution assumptions
    slippage_pct: float = 0.02
    taker_fee_pct: float = 0.05
    # bot
    poll_seconds: int = 30
    state_file: str = "bot_state.json"
    paper_state_file: str = "paper_state.json"
    journal_file: str = "journal.jsonl"
    kill_file: str = "KILL"
    close_positions_on_kill: bool = True
    max_stale_data_seconds: int = 180


def load_config(path: str = "config.yaml", require_keys: bool = True) -> Config:
    load_dotenv()
    with open(path) as f:
        raw = yaml.safe_load(f)

    mode = str(raw["mode"]).lower()
    if mode not in MODES:
        raise SystemExit(f"mode must be one of {MODES}, got {mode!r}")

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_SECRET_KEY", "")
    if require_keys and mode != "paper" and (not api_key or not api_secret):
        raise SystemExit(
            "BINANCE_API_KEY / BINANCE_SECRET_KEY not set. "
            "Copy .env.example to .env and fill in your keys "
            "(or use mode: paper, which needs no keys)."
        )

    tr, st, ex, rk, exe, bt = (
        raw["trading"], raw["strategy"], raw["exits"], raw["risk"],
        raw["execution"], raw["bot"],
    )
    cfg = Config(
        mode=mode,
        api_key=api_key,
        api_secret=api_secret,
        symbols=list(tr["symbols"]),
        timeframe=tr["timeframe"],
        trend_timeframe=tr["trend_timeframe"],
        leverage=int(tr["leverage"]),
        margin_mode=tr["margin_mode"],
        strategy=StrategyConfig(
            min_confidence=float(st["min_confidence"]),
            min_aligned_factors=int(st["min_aligned_factors"]),
            weights=dict(st["weights"]),
            ema_fast=int(st["ema_fast"]),
            ema_slow=int(st["ema_slow"]),
            trend_ema_fast=int(st["trend_ema_fast"]),
            trend_ema_slow=int(st["trend_ema_slow"]),
            rsi_period=int(st["rsi_period"]),
            atr_period=int(st["atr_period"]),
            breakout_lookback=int(st["breakout_lookback"]),
            swing_lookback=int(st["swing_lookback"]),
            swing_window=int(st["swing_window"]),
            volume_ma=int(st["volume_ma"]),
            volume_expansion=float(st["volume_expansion"]),
            extension_atr_max=float(st["extension_atr_max"]),
            book_depth_levels=int(st["book_depth_levels"]),
            book_imbalance_ratio=float(st["book_imbalance_ratio"]),
            min_atr_pct=float(st["min_atr_pct"]),
            max_atr_pct=float(st["max_atr_pct"]),
            max_funding_against=float(st["max_funding_against"]),
            btc_filter=bool(st["btc_filter"]),
        ),
        exits=ExitConfig(
            stop_atr_mult=float(ex["stop_atr_mult"]),
            tp1_r=float(ex["tp1_r"]),
            tp1_fraction=float(ex["tp1_fraction"]),
            tp2_r=float(ex["tp2_r"]),
            breakeven_at_r=float(ex["breakeven_at_r"]),
            trail_atr_mult=float(ex["trail_atr_mult"]),
            trail_min_step_pct=float(ex["trail_min_step_pct"]),
            exit_on_opposing_signal=bool(ex["exit_on_opposing_signal"]),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=float(rk["risk_per_trade_pct"]),
            max_leverage=int(rk["max_leverage"]),
            max_open_positions=int(rk["max_open_positions"]),
            max_symbol_exposure_mult=float(rk["max_symbol_exposure_mult"]),
            max_total_exposure_mult=float(rk["max_total_exposure_mult"]),
            daily_max_loss_pct=float(rk["daily_max_loss_pct"]),
            daily_profit_target_pct=float(rk["daily_profit_target_pct"]),
            weekly_max_drawdown_pct=float(rk["weekly_max_drawdown_pct"]),
            max_consecutive_losses=int(rk["max_consecutive_losses"]),
            max_trades_per_day=int(rk["max_trades_per_day"]),
            min_risk_reward=float(rk["min_risk_reward"]),
            max_spread_pct=float(rk["max_spread_pct"]),
            min_quote_volume_24h=float(rk["min_quote_volume_24h"]),
            min_book_depth_mult=float(rk["min_book_depth_mult"]),
        ),
        slippage_pct=float(exe["slippage_pct"]),
        taker_fee_pct=float(exe["taker_fee_pct"]),
        poll_seconds=int(bt["poll_seconds"]),
        state_file=bt["state_file"],
        paper_state_file=bt["paper_state_file"],
        journal_file=bt["journal_file"],
        kill_file=bt["kill_file"],
        close_positions_on_kill=bool(bt["close_positions_on_kill"]),
        max_stale_data_seconds=int(bt["max_stale_data_seconds"]),
    )
    if cfg.leverage > cfg.risk.max_leverage:
        raise SystemExit(
            f"trading.leverage ({cfg.leverage}) exceeds risk.max_leverage "
            f"({cfg.risk.max_leverage}) — refusing to start."
        )
    return cfg
