"""Export the analysed sentiment data to CSV files.

The second serving surface next to the Metabase dashboard: flat files a
business user can open in Excel / Google Sheets, or hand to another team.
Reads only the dbt marts (plus the article body from ``raw.documents``), so the
numbers match the dashboard exactly.

Files written to ``--output-dir``:

* ``sentimen_dokumen.csv``  one row per document - full text, label, score,
  confidence, aspects, review flag.
* ``ringkasan_aspek.csv``   per business aspect - volume, positive/negative, NSS.
* ``ringkasan_sumber.csv``  per channel & source - volume, label mix, NSS.

Files are UTF-8 with BOM so Excel renders Indonesian text correctly.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..logging_config import configure_logging, get_logger
from ..settings import get_settings

logger = get_logger(__name__)

MART_SCHEMA = "analytics_marts"
# Excel silently truncates a cell above 32,767 characters.
MAX_CELL_CHARS = 32_000

LABEL_ID = {"positive": "positif", "negative": "negatif", "neutral": "netral"}

DOCUMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("document_id", "id_dokumen"),
    ("event_date", "tanggal"),
    ("source_platform", "kanal"),
    ("source_name", "sumber"),
    ("title", "judul"),
    ("body", "isi_teks"),
    ("url", "tautan"),
    ("sentiment_label", "sentimen"),
    ("sentiment_score", "skor_sentimen"),
    ("confidence", "keyakinan"),
    ("dominant_emotion", "emosi_dominan"),
    ("rating", "rating_bintang"),
    ("negative_aspects", "aspek_negatif"),
    ("positive_aspects", "aspek_positif"),
    ("requires_review", "perlu_ditinjau"),
    ("review_reason", "alasan_tinjauan"),
    ("model_version", "versi_model"),
)

ASPECT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("aspect_label", "aspek"),
    ("aspect_group", "kelompok_aspek"),
    ("documents", "jumlah_dokumen"),
    ("positive_count", "positif"),
    ("neutral_count", "netral"),
    ("negative_count", "negatif"),
    ("nss", "net_sentiment_score"),
)

SOURCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_platform", "kanal"),
    ("source_name", "sumber"),
    ("documents", "jumlah_dokumen"),
    ("positive_count", "positif"),
    ("neutral_count", "netral"),
    ("negative_count", "negatif"),
    ("nss", "net_sentiment_score"),
    ("avg_confidence", "rata_rata_keyakinan"),
    ("first_date", "tanggal_pertama"),
    ("last_date", "tanggal_terakhir"),
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no database)
# ---------------------------------------------------------------------------
def format_value(value: Any) -> Any:
    """Render one DB value as a spreadsheet-friendly cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ya" if value else "tidak"
    if isinstance(value, list | tuple):
        return "; ".join(str(v) for v in value if v is not None)
    if isinstance(value, str):
        value = value.strip()
        if value in LABEL_ID:
            return LABEL_ID[value]
        if len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS] + " …[dipotong]"
    return value


def write_csv(
    path: Path,
    columns: Sequence[tuple[str, str]],
    rows: Iterable[dict],
    delimiter: str = ",",
) -> int:
    """Write ``rows`` to ``path`` with Indonesian headers. Returns the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    # utf-8-sig: the BOM makes Excel detect UTF-8 instead of mangling "é"/"–".
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([header for _, header in columns])
        for row in rows:
            writer.writerow([format_value(row.get(key)) for key, _ in columns])
            written += 1
    return written


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def build_where(
    alias: str, date_column: str, days: int | None, platform: str | None, synthetic: bool
) -> tuple[str, list]:
    """Shared WHERE clause. Values are always bound parameters, never interpolated."""
    prefix = f"{alias}." if alias else ""
    clauses, params = ["1 = 1"], []
    if days:
        clauses.append(f"{prefix}{date_column} >= CURRENT_DATE - %s")
        params.append(days)
    if platform:
        clauses.append(f"{prefix}source_platform = %s")
        params.append(platform)
    if not synthetic:
        clauses.append(f"NOT COALESCE({prefix}is_synthetic, FALSE)")
    return " AND ".join(clauses), params


def document_query(
    schema: str, days: int | None, platform: str | None, synthetic: bool
) -> tuple[str, list]:
    where, params = build_where("f", "event_date", days, platform, synthetic)
    sql = f"""
        SELECT
            f.document_id, f.event_date, f.source_platform, f.source_name,
            COALESCE(NULLIF(d.title, ''), f.headline) AS title,
            COALESCE(NULLIF(d.body, ''), f.excerpt)   AS body,
            f.url, f.sentiment_label, f.sentiment_score, f.confidence,
            f.dominant_emotion, f.rating, f.negative_aspects, f.positive_aspects,
            f.requires_review, f.review_reason, f.model_version
        FROM {schema}.mart_document_feed f
        JOIN raw.documents d USING (document_id)
        WHERE {where}
        ORDER BY f.event_at DESC NULLS LAST, f.document_id
    """
    return sql, params


def aspect_query(
    schema: str, days: int | None, platform: str | None, synthetic: bool
) -> tuple[str, list]:
    where, params = build_where("", "event_date", days, platform, synthetic)
    sql = f"""
        SELECT
            aspect_label, aspect_group,
            SUM(mention_documents) AS documents,
            SUM(positive_count)    AS positive_count,
            SUM(neutral_count)     AS neutral_count,
            SUM(negative_count)    AS negative_count,
            ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                  / NULLIF(SUM(mention_documents), 0) * 100, 1) AS nss
        FROM {schema}.mart_aspect_daily
        WHERE {where}
        GROUP BY 1, 2
        ORDER BY documents DESC
    """
    return sql, params


def source_query(
    schema: str, days: int | None, platform: str | None, synthetic: bool
) -> tuple[str, list]:
    where, params = build_where("", "event_date", days, platform, synthetic)
    sql = f"""
        SELECT
            source_platform, source_name,
            SUM(document_count) AS documents,
            SUM(positive_count) AS positive_count,
            SUM(neutral_count)  AS neutral_count,
            SUM(negative_count) AS negative_count,
            ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                  / NULLIF(SUM(document_count), 0) * 100, 1) AS nss,
            ROUND(SUM(avg_confidence * document_count)
                  / NULLIF(SUM(document_count), 0), 3)       AS avg_confidence,
            MIN(event_date) AS first_date,
            MAX(event_date) AS last_date
        FROM {schema}.mart_sentiment_daily
        WHERE {where}
        GROUP BY 1, 2
        ORDER BY documents DESC
    """
    return sql, params


EXPORTS = (
    ("sentimen_dokumen.csv", DOCUMENT_COLUMNS, document_query),
    ("ringkasan_aspek.csv", ASPECT_COLUMNS, aspect_query),
    ("ringkasan_sumber.csv", SOURCE_COLUMNS, source_query),
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def export(
    output_dir: Path,
    schema: str = MART_SCHEMA,
    days: int | None = None,
    platform: str | None = None,
    synthetic: bool = False,
    delimiter: str = ",",
) -> dict[str, int]:
    from ..db import fetch_all  # lazy: keeps the pure helpers importable without psycopg2

    summary: dict[str, int] = {}
    for filename, columns, build in EXPORTS:
        sql, params = build(schema, days, platform, synthetic)
        rows = fetch_all(sql, params)
        path = output_dir / filename
        summary[filename] = write_csv(path, columns, rows, delimiter)
        logger.info("Wrote %d rows to %s", summary[filename], path)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export analysed sentiment data to CSV")
    parser.add_argument("--output-dir", default="output", help="Target folder (default: output)")
    parser.add_argument("--schema", default=MART_SCHEMA, help="Schema holding the dbt marts")
    parser.add_argument("--days", type=int, default=None, help="Only the last N days")
    parser.add_argument("--platform", default=None, help="Only one channel, e.g. news")
    parser.add_argument(
        "--include-synthetic", action="store_true", help="Include the synthetic demo corpus"
    )
    parser.add_argument(
        "--delimiter",
        default=",",
        help="Column separator. Use ';' for Excel with Indonesian regional settings.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    output_dir = Path(args.output_dir)
    try:
        summary = export(
            output_dir,
            schema=args.schema,
            days=args.days,
            platform=args.platform,
            synthetic=args.include_synthetic,
            delimiter=args.delimiter,
        )
    except Exception:
        logger.exception("CSV export failed")
        return 1

    print(f"\nCSV siap di folder '{output_dir}':")
    for filename, count in summary.items():
        print(f"  {filename:<24} {count:>6} baris")
    return 0


if __name__ == "__main__":
    sys.exit(main())
