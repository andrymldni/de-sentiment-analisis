"""CLI entry point for the ingestion stage."""

from __future__ import annotations

import argparse
import json
import sys

from ..logging_config import configure_logging, get_logger
from ..settings import get_settings
from .orchestrator import IngestionOrchestrator
from .registry import catalog

logger = get_logger(__name__)


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
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.list:
        print(json.dumps(catalog(), indent=2, ensure_ascii=False))
        return 0

    reports = IngestionOrchestrator(settings).run(args.connectors, force=args.force)
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
