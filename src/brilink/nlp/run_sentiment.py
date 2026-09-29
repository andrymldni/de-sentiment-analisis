"""CLI entry point for the sentiment stage.

Idempotent by construction: it selects documents that have no score for the
*current model version*, so bumping ``SENTIMENT_MODEL_VERSION`` triggers a
clean re-score without truncating anything, and a crashed run simply resumes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence

from ..db import as_jsonb, bulk_upsert, fetch_all, get_connection
from ..logging_config import configure_logging, get_logger
from ..settings import Settings, get_settings
from .aspects import AspectHit, aspect_catalog, label_for
from .ensemble import DocumentScore, EnsembleSentimentEngine, ScoreInput

logger = get_logger(__name__)

# Rows are persisted in batches larger than the model batch: the engine
# micro-batches internally, the database prefers fewer round-trips.
MIN_PERSIST_BATCH = 32
MAX_MATCHED_KEYWORDS = 12
EVIDENCE_SAMPLE_CHARS = 300
EXIT_OK = 0
EXIT_USAGE = 2

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

# Documents already scored (before the LLM judge was switched on) that are still
# waiting for a human and have never been seen by the judge. A human label
# always wins, so those are left alone.
SELECT_REVIEW_WITHOUT_LLM = """
    SELECT d.document_id, d.title, d.body, d.rating, d.source_platform
    FROM core.document_sentiment s
    JOIN raw.documents d ON d.document_id = s.document_id
    WHERE s.requires_review
      AND s.reviewed_label IS NULL
      AND COALESCE((s.signals -> 'llm_judge' ->> 'available')::boolean, false) = false
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
    return fetch_all(SELECT_UNSCORED, {"limit": limit, "model_version": model_version})


def fetch_review_without_llm(limit: int) -> list[dict]:
    return fetch_all(SELECT_REVIEW_WITHOUT_LLM, {"limit": limit})


def _sentiment_row(score: DocumentScore) -> tuple:
    return (
        score.document_id,
        score.label,
        score.score,
        score.confidence,
        score.agreement,
        score.requires_review,
        score.review_reason,
        score.model_version,
        score.engine_mode,
        as_jsonb(score.signals_payload()),
        as_jsonb(score.explanation),
    )


def _aspect_row(document_id: int | str, hit: AspectHit, neutral_band: float) -> tuple:
    evidence = {
        "score_available": hit.score_available,
        "window_sample": hit.windows[0][:EVIDENCE_SAMPLE_CHARS] if hit.windows else "",
    }
    return (
        document_id,
        hit.aspect.code,
        hit.mentions,
        label_for(hit.score, neutral_band),
        round(hit.score, 4),
        hit.matched_keywords[:MAX_MATCHED_KEYWORDS],
        as_jsonb(evidence),
    )


def persist(scores: Sequence[DocumentScore], neutral_band: float) -> tuple[int, int]:
    if not scores:
        return 0, 0

    sentiment_rows = [_sentiment_row(score) for score in scores]
    aspect_rows = [
        _aspect_row(score.document_id, hit, neutral_band)
        for score in scores
        for hit in score.aspects
    ]

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


def _to_input(row: dict) -> ScoreInput:
    return ScoreInput(
        document_id=row["document_id"],
        title=row["title"],
        body=row["body"],
        rating=row["rating"],
        source_platform=row["source_platform"],
    )


def _load_pending(settings: Settings, limit: int, *, rejudge_review: bool) -> list[dict]:
    if rejudge_review:
        pending = fetch_review_without_llm(limit)
        logger.info("Found %d review-queue documents for the LLM judge", len(pending))
    else:
        pending = fetch_unscored(limit, settings.sentiment.model_version)
        logger.info("Found %d documents to score", len(pending))
    return pending


def _score_and_persist(
    engine: EnsembleSentimentEngine, pending: list[dict], settings: Settings
) -> dict:
    """Score ``pending`` in persist-sized batches and return the run totals."""
    batch_size = max(settings.sentiment.batch_size * 4, MIN_PERSIST_BATCH)
    total_docs = total_aspects = review_count = 0
    label_counts: Counter[str] = Counter()

    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        scores = engine.score_batch([_to_input(row) for row in chunk])
        docs_written, aspects_written = persist(scores, settings.sentiment.neutral_band)

        total_docs += docs_written
        total_aspects += aspects_written
        review_count += sum(1 for s in scores if s.requires_review)
        label_counts.update(s.label for s in scores)

        logger.info(
            "Scored batch %d-%d",
            start + 1,
            start + len(chunk),
            extra={"documents": docs_written, "aspects": aspects_written},
        )

    return {
        "scored": total_docs,
        "aspects": total_aspects,
        "requires_review": review_count,
        "label_distribution": dict(label_counts),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score unscored documents")
    parser.add_argument("--limit", type=int, default=None, help="Max documents this run")
    parser.add_argument(
        "--engine-mode",
        choices=("ensemble", "transformer_only"),
        default=None,
        help="Override the configured engine mode",
    )
    parser.add_argument(
        "--rejudge-review",
        action="store_true",
        help=(
            "Re-score documents still in the review queue that the LLM judge has "
            "never seen (use once after enabling SENTIMENT_ENABLE_LLM_JUDGE)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if args.engine_mode:
        settings.sentiment.engine_mode = args.engine_mode

    synced = sync_aspect_dimension()
    logger.info("Aspect dimension synchronised (%d rows)", synced)

    if args.rejudge_review and not settings.sentiment.enable_llm_judge:
        logger.error("--rejudge-review needs SENTIMENT_ENABLE_LLM_JUDGE=true")
        return EXIT_USAGE

    limit = args.limit or settings.sentiment.scoring_limit
    pending = _load_pending(settings, limit, rejudge_review=args.rejudge_review)
    if not pending:
        print(json.dumps({"scored": 0, "aspects": 0}))
        return EXIT_OK

    engine = EnsembleSentimentEngine()
    summary = _score_and_persist(engine, pending, settings)
    summary["model_version"] = settings.sentiment.model_version
    summary["engine_mode"] = settings.sentiment.engine_mode
    if engine.judge.enabled:
        summary["llm_judge"] = engine.judge.stats()
    engine.judge.close()

    logger.info("Sentiment stage finished", extra=summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
