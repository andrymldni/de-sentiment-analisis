"""Thin database access layer.

Deliberately psycopg2 + explicit SQL rather than an ORM: the workload is bulk
upsert and analytical read, where hand-written SQL is clearer and faster, and
it keeps the dbt models as the single place where business logic lives.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection

from .logging_config import get_logger
from .settings import get_settings

logger = get_logger(__name__)

psycopg2.extras.register_uuid()


@contextmanager
def get_connection(autocommit: bool = False) -> Iterator[PGConnection]:
    settings = get_settings()
    conn = psycopg2.connect(**settings.db.psycopg_kwargs)
    conn.autocommit = autocommit
    try:
        yield conn
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with (
        get_connection() as conn,
        conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur,
    ):
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        affected = cur.rowcount
        conn.commit()
        return affected


def bulk_upsert(
    conn: PGConnection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    conflict_target: str,
    update_columns: Sequence[str] | None = None,
    page_size: int = 500,
    count_new: bool = False,
) -> int:
    """Insert many rows with an ON CONFLICT clause.

    ``count_new=True`` returns the number of rows genuinely inserted rather
    than the number touched - the difference matters for an idempotent
    pipeline, where a re-run should honestly report zero new documents.
    """
    rows = list(rows)
    if not rows:
        return 0

    col_sql = ", ".join(columns)
    if update_columns:
        setters = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
        conflict_sql = f"ON CONFLICT ({conflict_target}) DO UPDATE SET {setters}"
    else:
        conflict_sql = f"ON CONFLICT ({conflict_target}) DO NOTHING"

    sql = f"INSERT INTO {table} ({col_sql}) VALUES %s {conflict_sql}"
    if count_new:
        # xmax = 0 is true only for tuples this statement actually inserted,
        # which is how we report "new documents" rather than "rows touched".
        sql += " RETURNING (xmax = 0) AS was_inserted"

    affected = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), page_size):
            chunk = rows[start : start + page_size]
            if count_new:
                returned = psycopg2.extras.execute_values(
                    cur, sql, chunk, page_size=page_size, fetch=True
                )
                affected += sum(1 for row in returned if row[0])
            else:
                psycopg2.extras.execute_values(cur, sql, chunk, page_size=page_size)
                affected += max(cur.rowcount, 0)
    conn.commit()
    return affected


def as_jsonb(value: Any) -> psycopg2.extras.Json:
    """Wrap a Python object for a jsonb column (orjson-free, stdlib safe)."""
    return psycopg2.extras.Json(value, dumps=lambda v: json.dumps(v, ensure_ascii=False))


def healthcheck() -> bool:
    try:
        with get_connection(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        logger.exception("Database healthcheck failed")
        return False
