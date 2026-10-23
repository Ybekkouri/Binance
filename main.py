"""Entry point.

  python main.py                     # run with config.yaml
  python main.py --config other.yaml
  python main.py --close-all         # manual override: flatten everything and exit

Kill switch while running: create a file named KILL (see bot.kill_file) in
the working directory — the bot cancels orders, optionally flattens the real
account, and stops.
"""

import argparse
import logging
import os

from bot.broker import PaperBroker, make_broker
from bot.config import load_config
from bot.datastore import DataStore
from bot.events import EventGuard
from bot.journal import Journal
from bot.manager import TradeManager
from bot.market_data import MarketData
from bot.notify import make_notifier
from bot.risk import RiskEngine
from bot.trader import Track, Trader, stopped_marker


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

    marker = stopped_marker(cfg)
    if os.path.isfile(marker) and not args.close_all:
        # The kill switch was used: stay off, even across reboots, until the
        # owner deliberately restarts (the installer removes this marker).
        logging.warning("Bot was stopped with the kill switch (%s exists). "
                        "Re-run the installer to start it again.", marker)
        raise SystemExit(0)

    if cfg.mode == "live" and not args.close_all:
        # Headless servers (systemd) can't type: they must set
        # BOT_CONFIRM_LIVE=yes in .env to run live without a prompt.
        if os.environ.get("BOT_CONFIRM_LIVE") != "yes":
            try:
                answer = input(
                    "mode: live — this trades REAL money on Binance Futures.\n"
                    "Type 'live' to confirm: "
                )
            except EOFError:
                # no keyboard (running as a service): refuse clearly, and
                # with an exit code systemd will not restart-loop on
                logging.error("Live mode needs BOT_CONFIRM_LIVE=yes in .env "
                              "when running as a service. Not starting.")
                raise SystemExit(78)
            if answer.strip().lower() != "live":
                raise SystemExit("Aborted.")

    broker = make_broker(cfg)
    if args.close_all:
        # close first; close_all only cancels orders on symbols it confirmed
        # flat, so a position that fails to close keeps its stop-loss
        still_open = broker.close_all()
        if still_open:
            print("Could NOT close: " + ", ".join(still_open) +
                  " — their stop-losses were left in place. Check Binance.")
            raise SystemExit(1)
        print("All positions closed and their orders cancelled.")
        return

    journal = Journal(cfg.journal_file)
    datastore = DataStore(cfg.datastore_file)
    notifier = make_notifier(cfg)
    events = EventGuard(cfg, notifier=notifier)   # shared: market-level guard

    # Real track: the strict engine on the configured broker.
    risk = RiskEngine(cfg)
    manager = TradeManager(cfg, broker, risk, journal, datastore=datastore,
                           notifier=notifier)
    real = Track("real", cfg, broker, risk, manager, journal,
                 datastore=datastore, notifier=notifier, events=events)

    # Shadow track: the identical pipeline, relaxed thresholds, virtual money.
    shadow = None
    if cfg.shadow.enabled:
        sh_broker = PaperBroker(cfg, state_file=cfg.shadow.paper_state_file,
                                start_balance=cfg.shadow.start_balance)
        sh_risk = RiskEngine(cfg, state_file=cfg.shadow.risk_state_file)
        sh_manager = TradeManager(cfg, sh_broker, sh_risk, journal,
                                  datastore=datastore, track="shadow",
                                  notifier=notifier)
        shadow = Track("shadow", cfg, sh_broker, sh_risk, sh_manager, journal,
                       datastore=datastore,
                       min_confidence=cfg.shadow.min_confidence,
                       min_aligned_factors=cfg.shadow.min_aligned_factors,
                       notifier=notifier, events=events)

    trader = Trader(cfg, MarketData(cfg), real, journal,
                    datastore=datastore, shadow=shadow, notifier=notifier)
    trader.run()


if __name__ == "__main__":
    main()
