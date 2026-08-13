"""Redis-backed politeness controls for the ingestion layer.

Three distinct mechanisms, each solving a different problem:

* ``Cooldown``      - stops a connector re-hitting the same upstream within N
                      minutes, surviving container restarts (TTL key).
* ``Checkpoint``    - remembers the last successful high-water mark per
                      connector so runs are incremental, not full re-scrapes.
* ``CircuitBreaker``- after repeated failures a connector is short-circuited
                      for a cooldown window instead of hammering a dead host.

Every mechanism degrades to a permissive in-memory no-op when Redis is
unavailable, so unit tests and laptop runs need no infrastructure.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..logging_config import get_logger
from ..settings import get_settings

logger = get_logger(__name__)

NAMESPACE = "brilink:ingest"


class _MemoryBackend:
    """Fallback store used when Redis is disabled or unreachable."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = (value, time.time() + ex if ex else None)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def incr(self, key: str) -> int:
        current = int(self.get(key) or 0) + 1
        _, expires = self._data.get(key, ("", None))
        self._data[key] = (str(current), expires)
        return current

    def expire(self, key: str, ttl: int) -> None:
        value = self.get(key)
        if value is not None:
            self._data[key] = (value, time.time() + ttl)

    def ttl(self, key: str) -> int:
        item = self._data.get(key)
        if not item or item[1] is None:
            return -1
        return max(int(item[1] - time.time()), 0)


class StateStore:
    """Uniform key/value facade over Redis with graceful degradation."""

    def __init__(self, client: Any | None = None) -> None:
        self._fallback = _MemoryBackend()
        self._client = client
        if client is None:
            self._client = self._connect()

    @staticmethod
    def _connect():
        settings = get_settings()
        if not settings.redis.enabled:
            return None
        try:
            import redis

            client = redis.Redis(**settings.redis.kwargs)
            client.ping()
            return client
        except Exception as exc:
            logger.warning("Redis unavailable (%s) - using in-memory state store", exc)
            return None

    @property
    def backend(self):
        return self._client or self._fallback

    def get(self, key: str) -> str | None:
        try:
            return self.backend.get(key)
        except Exception:
            logger.warning("State store read failed for %s", key, exc_info=True)
            return self._fallback.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        try:
            self.backend.set(key, value, ex=ttl)
        except Exception:
            logger.warning("State store write failed for %s", key, exc_info=True)
            self._fallback.set(key, value, ex=ttl)

    def delete(self, key: str) -> None:
        try:
            self.backend.delete(key)
        except Exception:
            self._fallback.delete(key)

    def incr(self, key: str, ttl: int | None = None) -> int:
        try:
            value = int(self.backend.incr(key))
            if ttl:
                self.backend.expire(key, ttl)
            return value
        except Exception:
            return self._fallback.incr(key)

    def ttl(self, key: str) -> int:
        try:
            return int(self.backend.ttl(key))
        except Exception:
            return self._fallback.ttl(key)


class Cooldown:
    """Prevents a connector from re-running before its cooldown expires."""

    def __init__(self, store: StateStore, connector: str, minutes: int) -> None:
        self.store = store
        self.key = f"{NAMESPACE}:cooldown:{connector}"
        self.seconds = max(minutes, 0) * 60

    def active(self) -> bool:
        if self.seconds == 0:
            return False
        return self.store.get(self.key) is not None

    def remaining(self) -> int:
        return max(self.store.ttl(self.key), 0)

    def arm(self) -> None:
        if self.seconds:
            self.store.set(self.key, str(int(time.time())), ttl=self.seconds)


class Checkpoint:
    """High-water mark so each run only asks upstream for what is new."""

    def __init__(self, store: StateStore, connector: str) -> None:
        self.store = store
        self.key = f"{NAMESPACE}:checkpoint:{connector}"

    def read(self) -> datetime | None:
        raw = self.store.get(self.key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("Corrupt checkpoint %r for %s - ignoring", raw, self.key)
            return None

    def write(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self.store.set(self.key, moment.astimezone(timezone.utc).isoformat())


class CircuitBreaker:
    """Opens after `threshold` consecutive failures, closes after `reset_seconds`."""

    def __init__(
        self,
        store: StateStore,
        connector: str,
        threshold: int = 3,
        reset_seconds: int = 3600,
    ) -> None:
        self.store = store
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.fail_key = f"{NAMESPACE}:failures:{connector}"
        self.open_key = f"{NAMESPACE}:circuit_open:{connector}"

    def is_open(self) -> bool:
        return self.store.get(self.open_key) is not None

    def record_failure(self) -> None:
        failures = self.store.incr(self.fail_key, ttl=self.reset_seconds)
        if failures >= self.threshold:
            self.store.set(self.open_key, "1", ttl=self.reset_seconds)
            logger.error(
                "Circuit opened after %d consecutive failures (reset in %ds)",
                failures,
                self.reset_seconds,
            )

    def record_success(self) -> None:
        self.store.delete(self.fail_key)
        self.store.delete(self.open_key)


class PoliteSleeper:
    """Spaces outbound calls without sleeping longer than necessary."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay = max(delay_seconds, 0.0)
        self._last = 0.0

    def wait(self) -> None:
        if not self.delay:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last = time.monotonic()
