"""Independent Telegram dispatcher and explicit local delivery recovery CLI."""

import argparse
import json
import logging
import time
from uuid import UUID

from app.services.delivery import DeliveryService
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import ConflictError, NotFoundError, Repository


def create_delivery_service(settings=None, repository=None, sender=None):
    settings = settings or Settings.from_env()
    if repository is None:
        database = Database(settings.database_path)
        database.initialize()
        repository = Repository(database)
    return (DeliveryService(repository, settings, sender=sender) if sender is not None
            else DeliveryService(repository, settings))


def _alert_id(value):
    try:
        parsed = UUID(value)
        if str(parsed) != value.lower():
            raise ValueError("noncanonical identifier")
        return str(parsed)
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("ALERT_ID must be a canonical UUID") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one bounded dispatch turn and exit")
    parser.add_argument("--recover", metavar="ALERT_ID", type=_alert_id,
                        help="reevaluate and requeue an action-required/exhausted delivery")
    parser.add_argument("--acknowledge-duplicate-risk", action="store_true",
                        help="explicitly allow recovery of uncertain delivery; it may duplicate a message")
    args = parser.parse_args(argv)
    if args.recover is not None and args.once:
        parser.error("--recover and --once are mutually exclusive")
    if args.acknowledge_duplicate_risk and args.recover is None:
        parser.error("--acknowledge-duplicate-risk requires --recover")
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    service = create_delivery_service(settings)
    if args.recover is not None:
        try:
            recovered = service.repository.recover_alert(
                args.recover, acknowledge_duplicate_risk=args.acknowledge_duplicate_risk)
            print(json.dumps({"alert_id": args.recover,
                              "status": "requeued" if recovered else "ineligible"}))
            return 0 if recovered else 1
        except (ConflictError, NotFoundError):
            print(json.dumps({"alert_id": args.recover, "status": "recovery_refused"}))
            return 1
    if args.once:
        service.run_once()
        return 0
    logging.info("Independent notification dispatcher started")
    try:
        while True:
            service.run_once()
            time.sleep(settings.check_interval_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
