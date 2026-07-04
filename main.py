"""Entry point: python main.py [--config config.yaml]"""

import argparse
import logging

from bot.config import load_config
from bot.trader import Trader


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance USDS-M futures bot")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    if not cfg.testnet:
        answer = input(
            "config.yaml has testnet: false — this trades REAL money. "
            "Type 'live' to continue: "
        )
        if answer.strip().lower() != "live":
            raise SystemExit("Aborted.")

    Trader(cfg).run()


if __name__ == "__main__":
    main()
