"""Entry point.

  python main.py                     # run with config.yaml
  python main.py --config other.yaml
  python main.py --close-all         # manual override: flatten everything and exit

Kill switch while running: create a file named KILL (see bot.kill_file) in
the working directory — the bot cancels orders, optionally flattens, and stops.
"""

import argparse
import logging

from bot.broker import make_broker
from bot.config import load_config
from bot.journal import Journal
from bot.manager import TradeManager
from bot.market_data import MarketData
from bot.risk import RiskEngine
from bot.trader import Trader


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance USDS-M futures trading engine")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--close-all", action="store_true",
                        help="cancel all orders, close all positions, exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    if cfg.mode == "live" and not args.close_all:
        answer = input(
            "mode: live — this trades REAL money on Binance Futures.\n"
            "Type 'live' to confirm: "
        )
        if answer.strip().lower() != "live":
            raise SystemExit("Aborted.")

    broker = make_broker(cfg)
    if args.close_all:
        for symbol in cfg.symbols:
            broker.cancel_all(symbol)
        broker.close_all()
        print("All orders cancelled, all positions closed.")
        return

    journal = Journal(cfg.journal_file)
    risk = RiskEngine(cfg)
    manager = TradeManager(cfg, broker, risk, journal)
    trader = Trader(cfg, MarketData(cfg), broker, risk, manager, journal)
    trader.run()


if __name__ == "__main__":
    main()
