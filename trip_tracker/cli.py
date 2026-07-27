import argparse
import logging

from trip_tracker.logging_config import configure_logging
from trip_tracker.services.gas_prices import GasPriceUnavailable, run_gas_snapshot_once

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trip-tracker")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("gas-snapshot", help="Fetch and store the current Michigan gas price")

    args = parser.parse_args()
    configure_logging("gas-snapshot" if args.command == "gas-snapshot" else "cli")

    if args.command == "gas-snapshot":
        try:
            monthly = run_gas_snapshot_once()
        except GasPriceUnavailable as exc:
            logger.warning("Gas snapshot unavailable: %s", exc)
            raise SystemExit(f"Gas snapshot unavailable: {exc}") from exc
        except Exception:
            logger.exception("Gas snapshot failed")
            raise
        print(
            f"Saved {monthly.state} monthly average "
            f"{monthly.average_price_per_gallon} for {monthly.year}-{monthly.month:02d}"
        )


if __name__ == "__main__":
    main()
