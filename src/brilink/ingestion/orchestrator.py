"""Ingestion orchestrator: the cross-cutting concerns, implemented once.

Responsibilities kept out of the connectors on purpose:

* cooldown windows and circuit breaking (Redis-backed, restart-safe)
* incremental checkpoints so each run only asks for what is new
* exact and near-duplicate suppression (SimHash over a rolling window)
* relevance filtering for broad sources
* per-run bookkeeping into ``raw.ingestion_runs`` for observability
* seed-corpus fallback so a demo is never blank
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..db import as_jsonb, bulk_upsert, get_connection
from ..logging_config import get_logger, safe_extra
from ..settings import Settings, get_settings
from ..utils.ratelimit import Checkpoint, CircuitBreaker, Cooldown, StateStore
from ..utils.text import hamming_distance
from .base import DOCUMENT_COLUMNS, BaseConnector, ConnectorUnavailable, Document
from .registry import resolve
from .seed_connector import SeedConnector

logger = get_logger(__name__)

NEAR_DUPLICATE_THRESHOLD = 4
RELEVANCE_FLOOR = 1e-9  # news must mention at least one keyword


@dataclass
class RunReport:
    connector: str
    run_id: str
    status: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    irrelevant: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    message: str | None = None

    def as_dict(self) -> dict:
        return {
            "connector": self.connector,
            "run_id": self.run_id,
            "status": self.status,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "irrelevant": self.irrelevant,
            "duration_s": round(
                (
                    (self.finished_at or datetime.now(timezone.utc)) - self.started_at
                ).total_seconds(),
                2,
            ),
            "message": self.message,
        }


class IngestionOrchestrator:
    def __init__(self, settings: Settings | None = None, store: StateStore | None = None):
        self.settings = settings or get_settings()
        self.store = store or StateStore()

    # ------------------------------------------------------------------
    def run(
        self,
        connector_names: str | None = None,
        force: bool = False,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[RunReport]:
        """Run the selected connectors.

        ``since`` switches to *backfill* mode: the window starts at that moment
        instead of the incremental checkpoint, and neither the checkpoint nor
        the cooldown is touched, so scheduled runs carry on exactly as before.
        ``limit`` overrides the per-connector item budget.
        """
        connectors = resolve(connector_names, self.settings)
        reports = [
            self._run_connector(c, force=force, since=since, limit=limit) for c in connectors
        ]

        if self._should_seed(connectors, reports):
            logger.warning(
                "Warehouse is empty and no live source delivered - falling back to "
                "the synthetic corpus so downstream models and the dashboard "
                "remain demonstrable"
            )
            reports.append(self._run_connector(SeedConnector(self.settings), force=True))

        for report in reports:
            logger.info("Ingestion summary", extra=safe_extra(report.as_dict()))
        return reports

    def _should_seed(
        self, connectors: Sequence[BaseConnector], reports: Sequence[RunReport]
    ) -> bool:
        """Fall back only when the warehouse would otherwise be unusable.

        "Nothing new was inserted" is the normal steady state for an idempotent
        pipeline, so it is deliberately *not* the trigger - otherwise every
        quiet run would append synthetic rows to a perfectly healthy warehouse.
        """
        if not self.settings.ingestion.seed_fallback:
            return False
        if any(c.name == SeedConnector.name for c in connectors):
            return False
        if sum(r.inserted for r in reports) > 0:
            return False

        from ..db import fetch_one

        try:
            row = fetch_one("SELECT COUNT(*) AS n FROM raw.documents")
        except Exception:
            logger.warning("Could not check warehouse occupancy", exc_info=True)
            return False
        return int((row or {}).get("n", 0)) == 0

    # ------------------------------------------------------------------
    def _run_connector(
        self,
        connector: BaseConnector,
        force: bool,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> RunReport:
        run_id = str(uuid.uuid4())
        report = RunReport(connector=connector.name, run_id=run_id, status="running")

        cooldown = Cooldown(self.store, connector.name, self.settings.ingestion.cooldown_minutes)
        breaker = CircuitBreaker(self.store, connector.name)
        checkpoint = Checkpoint(self.store, connector.name)

        # A backfill is a one-off manual pull of history. It must not move the
        # incremental checkpoint, arm the cooldown or trip the circuit breaker,
        # otherwise it would change what the next scheduled run does.
        backfill = since is not None

        missing = connector.missing_credentials()
        if missing:
            return self._finish(report, "skipped_no_credentials", f"missing: {', '.join(missing)}")
        if not force and cooldown.active():
            return self._finish(
                report, "skipped_cooldown", f"cooldown active for {cooldown.remaining()}s"
            )
        if not force and breaker.is_open():
            return self._finish(report, "skipped_circuit_open", "circuit breaker open")

        window_start = since if backfill else self._resolve_since(checkpoint)
        budget = limit or connector.max_items
        self._record_run(report, window_start)

        try:
            documents = list(connector.collect(window_start, budget))
        except ConnectorUnavailable as exc:
            if not backfill:
                breaker.record_failure()
            return self._finish(report, "unavailable", str(exc))
        except Exception as exc:
            logger.exception("Connector %s failed", connector.name)
            if not backfill:
                breaker.record_failure()
            return self._finish(report, "failed", str(exc))

        report.fetched = len(documents)
        kept = self._filter(connector, documents, report)

        if kept:
            report.inserted = self._persist(kept, run_id)
            newest = max((d.published_at for d in kept if d.published_at), default=None)
            if newest and not backfill:
                # Rewind slightly: publishers backfill timestamps, and a hard
                # high-water mark would silently drop late arrivals.
                checkpoint.write(newest - timedelta(hours=6))

        if backfill:
            return self._finish(
                report, "success", f"backfill since {window_start.date().isoformat()}"
            )

        breaker.record_success()
        cooldown.arm()
        return self._finish(report, "success")

    # ------------------------------------------------------------------
    def _resolve_since(self, checkpoint: Checkpoint) -> datetime:
        stored = checkpoint.read()
        now = datetime.now(timezone.utc)
        if stored:
            floor = now - timedelta(days=self.settings.ingestion.lookback_days)
            return max(stored, floor)
        return now - timedelta(days=self.settings.ingestion.initial_lookback_days)

    def _filter(
        self, connector: BaseConnector, documents: Sequence[Document], report: RunReport
    ) -> list[Document]:
        seen_uids: set[str] = set()
        seen_hashes: set[str] = set()
        simhashes: list[int] = []
        kept: list[Document] = []

        for doc in documents:
            if not doc.text:
                continue
            if connector.platform == "news" and doc.relevance <= RELEVANCE_FLOOR:
                report.irrelevant += 1
                continue
            if doc.doc_uid in seen_uids or doc.content_hash_value in seen_hashes:
                report.duplicates += 1
                continue
            if any(
                hamming_distance(doc.simhash_value, other) <= NEAR_DUPLICATE_THRESHOLD
                for other in simhashes
            ):
                report.duplicates += 1
                continue

            seen_uids.add(doc.doc_uid)
            seen_hashes.add(doc.content_hash_value)
            simhashes.append(doc.simhash_value)
            kept.append(doc)

        return kept

    @staticmethod
    def _persist(documents: Sequence[Document], run_id: str) -> int:
        rows = [doc.as_row(run_id) for doc in documents]
        with get_connection() as conn:
            return bulk_upsert(
                conn,
                table="raw.documents",
                columns=DOCUMENT_COLUMNS,
                rows=rows,
                conflict_target="doc_uid",
                update_columns=("engagement", "rating", "relevance_score"),
                count_new=True,
            )

    def _record_run(self, report: RunReport, since: datetime) -> None:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO raw.ingestion_runs
                        (run_id, connector, status, window_start, started_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (report.run_id, report.connector, "running", since, report.started_at),
                )
                conn.commit()
        except Exception:
            logger.warning("Could not open run record for %s", report.connector, exc_info=True)

    def _finish(self, report: RunReport, status: str, message: str | None = None) -> RunReport:
        report.status = status
        report.message = message
        report.finished_at = datetime.now(timezone.utc)

        if status.startswith("skipped"):
            logger.info("%s skipped: %s", report.connector, message)
        elif status in {"failed", "unavailable"}:
            logger.warning("%s %s: %s", report.connector, status, message)

        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO raw.ingestion_runs
                        (run_id, connector, status, started_at, finished_at,
                         fetched_count, inserted_count, duplicate_count,
                         irrelevant_count, message, metrics)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        finished_at = EXCLUDED.finished_at,
                        fetched_count = EXCLUDED.fetched_count,
                        inserted_count = EXCLUDED.inserted_count,
                        duplicate_count = EXCLUDED.duplicate_count,
                        irrelevant_count = EXCLUDED.irrelevant_count,
                        message = EXCLUDED.message,
                        metrics = EXCLUDED.metrics
                    """,
                    (
                        report.run_id,
                        report.connector,
                        report.status,
                        report.started_at,
                        report.finished_at,
                        report.fetched,
                        report.inserted,
                        report.duplicates,
                        report.irrelevant,
                        report.message,
                        as_jsonb(report.as_dict()),
                    ),
                )
                conn.commit()
        except Exception:
            logger.warning("Could not persist run record for %s", report.connector, exc_info=True)

        return report
