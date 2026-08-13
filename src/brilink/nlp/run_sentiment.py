"""CLI entry point for the sentiment stage.

Idempotent by construction: it selects documents that have no score for the
*current model version*, so bumping ``SENTIMENT_MODEL_VERSION`` triggers a
clean re-score without truncating anything, and a crashed run simply resumes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from ..db import as_jsonb, bulk_upsert, get_connection
from ..logging_config import configure_logging, get_logger
from ..settings import get_settings
from .aspects import aspect_catalog, label_for
from .ensemble import DocumentScore, EnsembleSentimentEngine, ScoreInput

logger = get_logger(__name__)

SELECT_UNSCORED = """
    SELECT d.document_id, d.title, d.body, d.rating, d.source_platform
    FROM raw.documents d
    LEFT JOIN core.document_sentiment s
           ON s.document_id = d.document_id
          AND s.model_version = %(model_version)s
    WHERE s.document_id IS NULL
      AND (COALESCE(d.title, '') <> '' OR COALESCE(d.body, '') <> '')
    ORDER BY d.published_at DESC NULLS LAST
    LIMIT %(limit)s
"""

SENTIMENT_COLUMNS = (
    "document_id",
    "sentiment_label",
    "sentiment_score",
    "confidence",
    "agreement",
    "requires_review",
    "review_reason",
    "model_version",
    "engine_mode",
    "signals",
    "explanation",
)
SENTIMENT_UPDATE = SENTIMENT_COLUMNS[1:] + ("scored_at",)

ASPECT_COLUMNS = (
    "document_id",
    "aspect_code",
    "mentions",
    "sentiment_label",
    "sentiment_score",
    "matched_keywords",
    "evidence",
)


def sync_aspect_dimension() -> int:
    rows = [
        (
            a["aspect_code"],
            a["aspect_label"],
            a["aspect_group"],
            a["description"],
            a["keyword_count"],
        )
        for a in aspect_catalog()
    ]
    with get_connection() as conn:
        return bulk_upsert(
            conn,
            table="core.dim_aspect",
            columns=("aspect_code", "aspect_label", "aspect_group", "description", "keyword_count"),
            rows=rows,
            conflict_target="aspect_code",
            update_columns=("aspect_label", "aspect_group", "description", "keyword_count"),
        )


def fetch_unscored(limit: int, model_version: str) -> list[dict]:
    from ..db import fetch_all

    return fetch_all(SELECT_UNSCORED, {"limit": limit, "model_version": model_version})


def persist(scores: Sequence[DocumentScore], neutral_band: float) -> tuple[int, int]:
    if not scores:
        return 0, 0

    sentiment_rows = [
        (
            s.document_id,
            s.label,
            s.score,
            s.confidence,
            s.agreement,
            s.requires_review,
            s.review_reason,
            s.model_version,
            s.engine_mode,
            as_jsonb(s.signals_payload()),
            as_jsonb(s.explanation),
        )
        for s in scores
    ]

    aspect_rows = []
    for score in scores:
        for hit in score.aspects:
            aspect_rows.append(
                (
                    score.document_id,
                    hit.aspect.code,
                    hit.mentions,
                    label_for(hit.score, neutral_band),
                    round(hit.score, 4),
                    hit.matched_keywords[:12],
                    as_jsonb(
                        {
                            "score_available": hit.score_available,
                            "window_sample": hit.windows[0][:300] if hit.windows else "",
                        }
                    ),
                )
            )

    with get_connection() as conn:
        sentiment_written = bulk_upsert(
            conn,
            table="core.document_sentiment",
            columns=SENTIMENT_COLUMNS,
            rows=sentiment_rows,
            conflict_target="document_id",
            update_columns=SENTIMENT_COLUMNS[1:],
        )
        aspect_written = bulk_upsert(
            conn,
            table="core.document_aspect_sentiment",
            columns=ASPECT_COLUMNS,
            rows=aspect_rows,
            conflict_target="document_id, aspect_code",
            update_columns=(
                "mentions",
                "sentiment_label",
                "sentiment_score",
                "matched_keywords",
                "evidence",
            ),
        )
    return sentiment_written, aspect_written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score unscored documents")
    parser.add_argument("--limit", type=int, default=None, help="Max documents this run")
    parser.add_argument(
        "--engine-mode",
        choices=("ensemble", "transformer_only"),
        default=None,
        help="Override the configured engine mode",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.engine_mode:
        settings.sentiment.engine_mode = args.engine_mode

    limit = args.limit or settings.sentiment.scoring_limit

    synced = sync_aspect_dimension()
    logger.info("Aspect dimension synchronised (%d rows)", synced)

    pending = fetch_unscored(limit, settings.sentiment.model_version)
    logger.info("Found %d documents to score", len(pending))
    if not pending:
        print(json.dumps({"scored": 0, "aspects": 0}))
        return 0

    engine = EnsembleSentimentEngine()
    batch_size = max(settings.sentiment.batch_size * 4, 32)

    total_docs = total_aspects = 0
    review_count = 0
    label_counts: dict[str, int] = {}

    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        inputs = [
            ScoreInput(
                document_id=row["document_id"],
                title=row["title"],
                body=row["body"],
                rating=row["rating"],
                source_platform=row["source_platform"],
            )
            for row in chunk
        ]
        scores = engine.score_batch(inputs)
        docs_written, aspects_written = persist(scores, settings.sentiment.neutral_band)

        total_docs += docs_written
        total_aspects += aspects_written
        review_count += sum(1 for s in scores if s.requires_review)
        for s in scores:
            label_counts[s.label] = label_counts.get(s.label, 0) + 1

        logger.info(
            "Scored batch %d-%d",
            start + 1,
            start + len(chunk),
            extra={"documents": docs_written, "aspects": aspects_written},
        )

    summary = {
        "scored": total_docs,
        "aspects": total_aspects,
        "requires_review": review_count,
        "label_distribution": label_counts,
        "model_version": settings.sentiment.model_version,
        "engine_mode": settings.sentiment.engine_mode,
    }
    logger.info("Sentiment stage finished", extra=summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
