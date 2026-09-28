import csv
from datetime import date
from decimal import Decimal

from brilink.serving.dashboard_spec import CARDS, DASHBOARD_PARAMETERS, FILTERS
from brilink.serving.export_csv import (
    DOCUMENT_COLUMNS,
    EXPORTS,
    MAX_CELL_CHARS,
    build_where,
    format_value,
    write_csv,
)


def test_format_value_is_spreadsheet_friendly():
    assert format_value(None) == ""
    assert format_value(True) == "ya"
    assert format_value(False) == "tidak"
    assert format_value(["Biaya & Tarif", "Layanan Agen"]) == "Biaya & Tarif; Layanan Agen"
    assert format_value("negative") == "negatif"
    assert format_value("  teks  ") == "teks"
    assert format_value(Decimal("0.75")) == Decimal("0.75")
    long_text = "a" * (MAX_CELL_CHARS + 50)
    assert len(format_value(long_text)) < len(long_text)


def test_write_csv_roundtrip_keeps_commas_newlines_and_utf8(tmp_path):
    rows = [
        {
            "document_id": 1,
            "event_date": date(2026, 9, 26),
            "title": 'Agen BRILink, pria "nekat" bawa kabur Rp40 juta',
            "body": "Baris satu.\nBaris dua – dengan en-dash.",
            "sentiment_label": "negative",
            "negative_aspects": ["Keamanan & Fraud"],
            "requires_review": False,
        }
    ]
    path = tmp_path / "out" / "sentimen_dokumen.csv"
    assert write_csv(path, DOCUMENT_COLUMNS, rows) == 1

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM needed for Excel"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        parsed = list(csv.DictReader(handle))
    assert len(parsed) == 1
    row = parsed[0]
    assert row["judul"] == rows[0]["title"]
    assert row["isi_teks"] == rows[0]["body"]
    assert row["sentimen"] == "negatif"
    assert row["aspek_negatif"] == "Keamanan & Fraud"
    assert row["perlu_ditinjau"] == "tidak"
    assert row["tanggal"] == "2026-09-26"


def test_write_csv_semicolon_delimiter(tmp_path):
    path = tmp_path / "x.csv"
    write_csv(path, (("a", "kolom_a"), ("b", "kolom_b")), [{"a": 1, "b": "x;y"}], delimiter=";")
    text = path.read_text(encoding="utf-8-sig").splitlines()
    assert text[0] == "kolom_a;kolom_b"
    assert text[1] == '1;"x;y"'


def test_filters_are_bound_parameters_not_interpolated():
    where, params = build_where("f", "event_date", 30, "news'; DROP TABLE x;--", False)
    assert "DROP" not in where
    assert params == [30, "news'; DROP TABLE x;--"]
    assert "f.is_synthetic" in where


def test_every_export_query_builds():
    for filename, columns, build in EXPORTS:
        sql, params = build("analytics_marts", 90, "news", True)
        assert filename.endswith(".csv")
        assert "analytics_marts." in sql
        assert sql.count("%s") == len(params)
        assert columns


def test_dashboard_parameters_use_types_metabase_accepts():
    # "category/string" made Metabase v0.50 answer HTTP 500 for every filtered card.
    allowed = {"date/single", "string/=", "category"}
    for param in DASHBOARD_PARAMETERS:
        assert param["type"] in allowed, param
    slugs = {p["slug"] for p in DASHBOARD_PARAMETERS}
    for card in CARDS:
        for slug in card.get("filters", []):
            assert slug in slugs and slug in FILTERS
            assert "{{" + slug + "}}" in card["sql"], (card["key"], slug)


def test_date_filtered_cards_have_no_hard_coded_window():
    # A fixed "CURRENT_DATE - 90" silently emptied every card whenever the date
    # filter pointed at backfilled history (2019+). The filter owns the range.
    for card in CARDS:
        if "tgl_mulai" not in card.get("filters", []):
            continue
        sql = card["sql"].upper()
        assert "CURRENT_DATE -" not in sql, card["key"]
        assert "NOW() -" not in sql, card["key"]
        assert "_90D" not in sql, card["key"]
