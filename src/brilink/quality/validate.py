"""Great Expectations gate between ingestion and scoring.

Two design choices worth calling out:

1. **It is a gate, not a report.** Failures raise, the Airflow task exits
   non-zero, and everything downstream is skipped. A warning nobody reads is
   not data quality.
2. **Results are persisted.** Every expectation outcome is written to
   ``ops.data_quality_results`` so the dashboard can show quality over time
   rather than only the latest run's console output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from ..db import as_jsonb, get_connection
from ..logging_config import configure_logging, get_logger
from ..settings import get_settings

logger = get_logger(__name__)

SUITE_NAME = "raw_documents_suite"

KNOWN_PLATFORMS = ["news", "playstore", "appstore", "reddit", "youtube", "twitter", "seed"]
KNOWN_LANGUAGES = ["id", "en", "unknown"]

# Publishers backfill timestamps and timezones drift; a day of slack keeps the
# expectation meaningful without flagging normal behaviour.
FUTURE_TOLERANCE_HOURS = 24


def load_window(hours: int):
    import pandas as pd
    from sqlalchemy import create_engine

    settings = get_settings()
    engine = create_engine(settings.db.dsn)
    try:
        return pd.read_sql(
            """
            SELECT document_id, doc_uid, source_platform, source_name, source_type,
                   title, body, language, rating, published_at, ingested_at,
                   relevance_score, content_hash
            FROM raw.documents
            WHERE ingested_at >= NOW() - make_interval(hours => %(hours)s)
            """,
            engine,
            params={"hours": hours},
        )
    finally:
        engine.dispose()


def persist_results(result, evaluated_rows: int, dag_run_id: str | None) -> None:
    rows = []
    for item in result.results:
        config = item.expectation_config
        kwargs = getattr(config, "kwargs", {}) or {}
        observed = getattr(item, "result", {}) or {}
        rows.append(
            (
                SUITE_NAME,
                getattr(config, "type", str(config)),
                kwargs.get("column"),
                bool(item.success),
                as_jsonb(
                    {
                        "element_count": observed.get("element_count"),
                        "unexpected_count": observed.get("unexpected_count"),
                        "unexpected_percent": observed.get("unexpected_percent"),
                        "partial_unexpected_list": observed.get("partial_unexpected_list", [])[:5],
                    }
                ),
                evaluated_rows,
                dag_run_id,
            )
        )

    if not rows:
        return

    with get_connection() as conn, conn.cursor() as cur:
        import psycopg2.extras

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO ops.data_quality_results
                (suite_name, expectation, column_name, success, observed, evaluated_rows, dag_run_id)
            VALUES %s
            """,
            rows,
        )
        conn.commit()


def run(hours: int, min_rows: int, dag_run_id: str | None) -> dict:
    import great_expectations as gx
    import pandas as pd

    frame = load_window(hours)
    logger.info("Validating %d documents ingested in the last %dh", len(frame), hours)

    if frame.empty:
        logger.warning("Nothing ingested in the window - gate passes vacuously")
        return {"status": "skipped", "rows": 0}

    if len(frame) < min_rows:
        logger.warning(
            "Only %d rows in window (minimum expected %d) - continuing but flagging",
            len(frame),
            min_rows,
        )

    frame["published_at"] = pd.to_datetime(frame["published_at"], errors="coerce", utc=True)
    frame["published_at"] = frame["published_at"].dt.tz_localize(None)
    frame["text_length"] = frame["title"].fillna("").str.len() + frame["body"].fillna("").str.len()

    max_published = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        hours=FUTURE_TOLERANCE_HOURS
    )

    context = gx.get_context(mode="ephemeral")
    source = context.data_sources.add_pandas("brilink_pandas")
    asset = source.add_dataframe_asset(name="raw_documents")
    batch_definition = asset.add_batch_definition_whole_dataframe("current_window")
    batch = batch_definition.get_batch(batch_parameters={"dataframe": frame})

    suite = context.suites.add(gx.ExpectationSuite(name=SUITE_NAME))
    suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(column="doc_uid"))
    suite.add_expectation(gx.expectations.ExpectColumnValuesToBeUnique(column="doc_uid"))
    suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(column="source_platform"))
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeInSet(
            column="source_platform", value_set=KNOWN_PLATFORMS
        )
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeInSet(column="language", value_set=KNOWN_LANGUAGES)
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeBetween(column="rating", min_value=1, max_value=5)
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeBetween(
            column="text_length", min_value=3, max_value=20000, mostly=0.99
        )
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeBetween(
            column="published_at",
            min_value=None,
            max_value=max_published.isoformat(),
            mostly=0.98,
        )
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeBetween(
            column="relevance_score", min_value=0, max_value=1
        )
    )
    # Duplicate content across outlets is expected (syndication) but a flood of
    # identical bodies means the dedup layer regressed.
    suite.add_expectation(
        gx.expectations.ExpectColumnProportionOfUniqueValuesToBeBetween(
            column="content_hash", min_value=0.7, max_value=1.0
        )
    )

    result = batch.validate(suite)
    persist_results(result, len(frame), dag_run_id)

    failures = [r for r in result.results if not r.success]
    summary = {
        "status": "passed" if result.success else "failed",
        "rows": int(len(frame)),
        "expectations": len(result.results),
        "failed": len(failures),
    }

    if failures:
        for item in failures:
            config = item.expectation_config
            observed = getattr(item, "result", {}) or {}
            logger.error(
                "FAILED %s on column=%s: unexpected=%s sample=%s",
                getattr(config, "type", config),
                (getattr(config, "kwargs", {}) or {}).get("column"),
                observed.get("unexpected_count"),
                (observed.get("partial_unexpected_list") or [])[:5],
            )
        raise SystemExit(
            "Data quality gate FAILED - downstream tasks intentionally blocked. "
            f"{len(failures)} expectation(s) did not pass; details above and in "
            "ops.data_quality_results."
        )

    logger.info("Data quality gate passed", extra=summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the data quality gate")
    parser.add_argument("--hours", type=int, default=24, help="Ingestion window to validate")
    parser.add_argument("--min-rows", type=int, default=1, help="Soft minimum row count")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    summary = run(args.hours, args.min_rows, os.getenv("AIRFLOW_CTX_DAG_RUN_ID"))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
