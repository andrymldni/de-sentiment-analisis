"""Connector contract and the shared document envelope.

Every source - news, app stores, forums, video comments, microblogs - is
reduced to the same ``Document`` shape at the edge of the system.  Downstream
(quality gate, NLP, dbt, Metabase) therefore has exactly one schema to reason
about, and adding a new source is a single class implementing ``fetch``.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..logging_config import get_logger
from ..settings import Settings, get_settings
from ..utils.text import (
    clean_text,
    content_hash,
    detect_language,
    relevance_score,
    simhash,
    truncate,
)

logger = get_logger(__name__)

MAX_TITLE = 600
MAX_BODY = 12000


@dataclass
class Document:
    """Canonical, source-agnostic record."""

    source_platform: str
    source_name: str
    external_id: str
    title: str | None = None
    body: str | None = None
    url: str | None = None
    author: str | None = None
    rating: int | None = None
    published_at: datetime | None = None
    engagement: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    # Derived - populated by ``finalize``.
    doc_uid: str = ""
    content_hash_value: str = ""
    simhash_value: int = 0
    language: str = "unknown"
    relevance: float = 0.0
    source_type: str = "ugc"

    def finalize(self, keywords: list[str]) -> Document:
        self.title = truncate(clean_text(self.title, keep_case=True), MAX_TITLE) or None
        self.body = truncate(clean_text(self.body, keep_case=True), MAX_BODY) or None

        combined = " ".join(p for p in (self.title, self.body) if p)
        self.doc_uid = content_hash(self.source_platform, self.source_name, self.external_id)
        self.content_hash_value = content_hash(self.title, self.body)
        self.simhash_value = simhash(combined)
        self.language = detect_language(combined)
        self.relevance = relevance_score(combined, keywords)
        self.source_type = "editorial" if self.source_platform == "news" else "ugc"

        if self.published_at is not None and self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)
        return self

    @property
    def text(self) -> str:
        return " ".join(p for p in (self.title, self.body) if p).strip()

    def as_row(self, run_id: str) -> tuple:
        from ..db import as_jsonb

        return (
            self.doc_uid,
            self.source_platform,
            self.source_name,
            self.source_type,
            self.external_id,
            self.url,
            self.title,
            self.body,
            self.author,
            self.language,
            self.rating,
            as_jsonb(self.engagement or {}),
            self.published_at,
            run_id,
            self.content_hash_value,
            # simhash is stored as signed bigint; Postgres has no uint64.
            (
                self.simhash_value - (1 << 64)
                if self.simhash_value >= (1 << 63)
                else self.simhash_value
            ),
            round(self.relevance, 4),
            as_jsonb(self.raw_payload or {}),
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["published_at"] = self.published_at.isoformat() if self.published_at else None
        return payload


DOCUMENT_COLUMNS = (
    "doc_uid",
    "source_platform",
    "source_name",
    "source_type",
    "external_id",
    "url",
    "title",
    "body",
    "author",
    "language",
    "rating",
    "engagement",
    "published_at",
    "ingestion_run_id",
    "content_hash",
    "simhash",
    "relevance_score",
    "raw_payload",
)


class ConnectorUnavailable(RuntimeError):
    """Raised when a connector cannot run (missing credential, dead dependency)."""


class BaseConnector(abc.ABC):
    """Contract for every source connector.

    Implementations only worry about *talking to the upstream*.  Cooldowns,
    checkpoints, circuit breaking, deduplication, relevance filtering, run
    bookkeeping and persistence are handled once, by the orchestrator.
    """

    name: str = "base"
    platform: str = "unknown"
    description: str = ""
    requires_credentials: tuple[str, ...] = ()
    default_enabled: bool = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.keywords = [
            k.strip() for k in self.settings.ingestion.keywords.split(",") if k.strip()
        ]

    @property
    def max_items(self) -> int:
        """Per-run volume budget. Overridden where a source has its own bound."""
        return self.settings.ingestion.max_items_per_connector

    # -- capability -----------------------------------------------------
    def missing_credentials(self) -> list[str]:
        creds = self.settings.credentials
        return [name for name in self.requires_credentials if not getattr(creds, name, None)]

    def is_available(self) -> bool:
        return not self.missing_credentials()

    # -- work -----------------------------------------------------------
    @abc.abstractmethod
    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        """Yield raw documents published at or after ``since``."""

    # -- helpers --------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def collect(self, since: datetime, limit: int) -> Iterator[Document]:
        for doc in self.fetch(since, limit):
            if doc is None:
                continue
            yield doc.finalize(self.keywords)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name} platform={self.platform}>"
