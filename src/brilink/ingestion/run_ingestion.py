"""CLI entry point for the ingestion stage."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from ..logging_config import configure_logging, get_logger
from ..settings import get_settings
from .orchestrator import IngestionOrchestrator
from .registry import catalog

logger = get_logger(__name__)


def _parse_since(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment > datetime.now(timezone.utc):
        raise argparse.ArgumentTypeError(f"--since {value} is in the future")
    return moment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run BRILink source ingestion")
    parser.add_argument(
        "--connectors",
        default=None,
        help="Comma separated connector names, or 'all' (default: from settings)",
    )
    parser.add_argument("--force", action="store_true", help="Ignore cooldown and circuit breaker")
    parser.add_argument("--list", action="store_true", help="Print the connector catalog and exit")
    parser.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="Exit non-zero when no documents were ingested at all",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help=(
            "Backfill from this date (YYYY-MM-DD, UTC) instead of the incremental "
            "checkpoint. Leaves checkpoint, cooldown and circuit breaker untouched."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents per connector this run (default: INGEST_MAX_ITEMS_PER_CONNECTOR)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.list:
        print(json.dumps(catalog(), indent=2, ensure_ascii=False))
        return 0

    reports = IngestionOrchestrator(settings).run(
        args.connectors, force=args.force, since=args.since, limit=args.limit
    )
    summary = [r.as_dict() for r in reports]
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    total = sum(r.inserted for r in reports)
    logger.info("Ingestion finished: %d new documents", total)

    if args.fail_on_empty and total == 0:
        logger.error("No documents ingested and --fail-on-empty was set")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
