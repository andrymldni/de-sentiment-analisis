"""Declarative Metabase dashboard specification.

Keeping the dashboard as data (rather than clicks in a UI) means it is
reviewable in a pull request, reproducible on a fresh machine, and versioned
alongside the models it depends on.  Each card is a native SQL question against
a dbt mart - never against a raw table - so the semantics shown to a business
user are exactly the ones the tests cover.

Interactivity
-------------
Cards opt into dashboard-wide filters through ``filters``. Each entry is a
slug declared in :data:`DASHBOARD_PARAMETERS`; the provisioning script turns it
into a Metabase template tag (``{{slug}}``) and the SQL uses the optional
``[[...]]`` syntax so a card still works when the filter is left unset.

Time range
----------
A card with a date filter never hard-codes its own window (no
``CURRENT_DATE - 90``): with the filter unset it shows the full history
(backfilled to 2019), with it set it shows exactly that period. A hard window
used to make every date filter outside the last 90 days return nothing. Only
the deliberately "right now" cards - early-warning alerts, connector freshness
- keep a fixed window, and they have no date filter.
"""

from __future__ import annotations

# Fixed, intuitive colours so "negatif" is never rendered in blue.
SENTIMENT_COLORS: dict[str, str] = {
    "Positif": "#84BB4C",
    "Netral": "#C4C9D0",
    "Negatif": "#ED6E6E",
}

# Grid is 24 columns wide in Metabase.
CARDS: list[dict] = [
    {
        "key": "kpi_documents",
        "name": "Total Dokumen Dianalisis",
        "description": (
            "Jumlah dokumen dari seluruh kanal yang lolos ambang keyakinan. "
            "Tanpa filter tanggal = seluruh riwayat (2019 - sekarang)."
        ),
        "display": "scalar",
        "position": {"row": 0, "col": 0, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT SUM(document_count) AS "Dokumen"
            FROM {schema}.mart_sentiment_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
    },
    {
        "key": "kpi_nss",
        "name": "Net Sentiment Score",
        "description": (
            "(%positif - %negatif) x 100, rentang -100 s/d +100, untuk periode filter "
            "tanggal (tanpa filter = seluruh riwayat)."
        ),
        "display": "scalar",
        "position": {"row": 0, "col": 6, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT ROUND(
                     (SUM(positive_count) - SUM(negative_count))::numeric
                     / NULLIF(SUM(document_count), 0) * 100, 1) AS "NSS"
            FROM {schema}.mart_sentiment_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
        "visualization_settings": {"scalar.suffix": " pts"},
    },
    {
        "key": "kpi_negative_share",
        "name": "Porsi Sentimen Negatif",
        "description": "Persentase dokumen berlabel negatif pada periode filter tanggal.",
        "display": "scalar",
        "position": {"row": 0, "col": 12, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT ROUND(SUM(negative_count)::numeric
                         / NULLIF(SUM(document_count), 0) * 100, 1) AS "Negatif %"
            FROM {schema}.mart_sentiment_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
        "visualization_settings": {"scalar.suffix": " %"},
    },
    {
        "key": "kpi_review",
        "name": "Antrean Tinjauan Manual",
        "description": "Dokumen berkeyakinan rendah atau bersinyal konflik yang menunggu adjudikasi manusia.",
        "display": "scalar",
        "position": {"row": 0, "col": 18, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "click_target": "review_queue",
        "sql": """
            SELECT COUNT(*) AS "Perlu Ditinjau"
            FROM {schema}.mart_review_queue
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
    },
    {
        "key": "trend_daily",
        "name": "Tren Sentimen Bulanan (NSS & Volume)",
        "description": (
            "Batang = jumlah dokumen per bulan, garis = Net Sentiment Score bulan itu. "
            "Agregasi bulanan agar riwayat 2019 - sekarang tetap terbaca; persempit "
            "dengan filter tanggal untuk melihat periode tertentu."
        ),
        "display": "combo",
        "position": {"row": 3, "col": 0, "size_x": 16, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT
                DATE_TRUNC('month', event_date)::date               AS "Bulan",
                SUM(document_count)                                 AS "Jumlah Dokumen",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(document_count), 0) * 100, 1)    AS "NSS"
            FROM {schema}.mart_sentiment_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
            GROUP BY 1
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Bulan"],
            "graph.metrics": ["Jumlah Dokumen", "NSS"],
            "graph.x_axis.title_text": "Bulan",
            "series_settings": {
                "Jumlah Dokumen": {"display": "bar", "color": "#A7ADB5"},
                "NSS": {"display": "line", "color": "#509EE3", "axis": "right"},
            },
        },
    },
    {
        "key": "mix_platform",
        "name": "Komposisi Label per Kanal",
        "description": "Distribusi positif/netral/negatif per platform sumber.",
        "display": "bar",
        "position": {"row": 3, "col": 16, "size_x": 8, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT
                source_platform     AS "Kanal",
                SUM(positive_count) AS "Positif",
                SUM(neutral_count)  AS "Netral",
                SUM(negative_count) AS "Negatif"
            FROM {schema}.mart_sentiment_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
            GROUP BY 1
            ORDER BY SUM(document_count) DESC
        """,
        "visualization_settings": {
            "graph.dimensions": ["Kanal"],
            "graph.metrics": ["Positif", "Netral", "Negatif"],
            "stackable.stack_type": "normalized",
            "graph.show_values": True,
            "series_settings": {
                label: {"color": color} for label, color in SENTIMENT_COLORS.items()
            },
        },
    },
    {
        "key": "aspect_ranking",
        "name": "Peringkat Aspek Berdasarkan NSS",
        "description": "Aspek mana yang menjadi sumber keluhan, dan mana yang jadi kekuatan.",
        "display": "row",
        "position": {"row": 10, "col": 0, "size_x": 12, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal", "aspek"],
        "sql": """
            SELECT
                aspect_label                                        AS "Aspek",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(mention_documents), 0) * 100, 1) AS "NSS Aspek",
                SUM(mention_documents)                              AS "Jumlah Dokumen"
            FROM {schema}.mart_aspect_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            GROUP BY 1
            ORDER BY 2 ASC
        """,
        "visualization_settings": {
            "graph.dimensions": ["Aspek"],
            "graph.metrics": ["NSS Aspek"],
            "graph.show_values": True,
            "series_settings": {"NSS Aspek": {"color": "#509EE3"}},
        },
    },
    {
        "key": "aspect_trend",
        "name": "Tren Bulanan per Aspek",
        "description": "Pergerakan NSS tiap aspek dari bulan ke bulan (min. 3 dokumen per titik).",
        "display": "line",
        "position": {"row": 10, "col": 12, "size_x": 12, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal", "aspek"],
        "sql": """
            SELECT
                DATE_TRUNC('month', event_date)::date               AS "Bulan",
                aspect_label                                        AS "Aspek",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(mention_documents), 0) * 100, 1) AS "NSS"
            FROM {schema}.mart_aspect_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            GROUP BY 1, 2
            HAVING SUM(mention_documents) >= 3
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Bulan", "Aspek"],
            "graph.metrics": ["NSS"],
        },
    },
    {
        "key": "aspect_matrix",
        "name": "Matriks Aspek x Kanal",
        "description": (
            "Di kanal mana setiap aspek paling banyak dibicarakan, dan bagaimana nadanya, "
            "pada periode filter tanggal."
        ),
        "display": "table",
        "position": {"row": 17, "col": 0, "size_x": 12, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal", "aspek"],
        # Aggregated from the daily mart (not mart_aspect_source_matrix, which is
        # fixed to the trailing 90 days) so the date filter reaches back to 2019.
        "sql": """
            SELECT
                aspect_label                                        AS "Aspek",
                source_platform                                     AS "Kanal",
                SUM(mention_documents)                              AS "Dokumen",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(mention_documents), 0) * 100, 2) AS "NSS",
                ROUND(SUM(mention_documents)::numeric
                      / NULLIF(SUM(SUM(mention_documents))
                               OVER (PARTITION BY source_platform), 0) * 100, 2)
                                                                    AS "Share of Voice %"
            FROM {schema}.mart_aspect_daily
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            GROUP BY 1, 2
            ORDER BY 3 DESC
        """,
    },
    {
        "key": "source_scorecard",
        "name": "Scorecard Sumber",
        "description": (
            "NSS tiap sumber pada periode filter tanggal (tanpa filter = seluruh riwayat), "
            "dibandingkan 90 hari terakhir periode itu. Delta negatif = nada memburuk."
        ),
        "display": "table",
        "position": {"row": 17, "col": 12, "size_x": 12, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        # Aggregated from the daily mart (not mart_sentiment_by_source, which is
        # fixed to the trailing 90 days). "Recent" = the last 90 days of the
        # selected period, so the comparison still works for, say, 2021 alone.
        "sql": """
            WITH scoped AS (
                SELECT *
                FROM {schema}.mart_sentiment_daily
                WHERE 1 = 1
                  [[AND event_date >= {{tgl_mulai}}]]
                  [[AND event_date <= {{tgl_akhir}}]]
                  [[AND source_platform = {{kanal}}]]
            ),
            flagged AS (
                SELECT *,
                       event_date > MAX(event_date) OVER () - 90 AS is_recent
                FROM scoped
            ),
            per_source AS (
                SELECT
                    source_platform,
                    source_name,
                    SUM(document_count)                                   AS docs,
                    MIN(event_date)                                       AS first_date,
                    MAX(event_date)                                       AS last_date,
                    ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                          / NULLIF(SUM(document_count), 0) * 100, 1)      AS nss_all,
                    ROUND((SUM(positive_count) FILTER (WHERE is_recent)
                           - SUM(negative_count) FILTER (WHERE is_recent))::numeric
                          / NULLIF(SUM(document_count) FILTER (WHERE is_recent), 0)
                          * 100, 1)                                       AS nss_recent,
                    ROUND(SUM(avg_confidence * document_count)
                          / NULLIF(SUM(document_count), 0), 4)            AS avg_conf
                FROM flagged
                GROUP BY 1, 2
            )
            SELECT
                source_platform         AS "Kanal",
                source_name             AS "Sumber",
                docs                    AS "Dokumen",
                first_date              AS "Pertama",
                last_date               AS "Terakhir",
                nss_all                 AS "NSS Periode",
                nss_recent              AS "NSS 90h Terakhir",
                nss_recent - nss_all    AS "Delta",
                avg_conf                AS "Rata-rata Keyakinan"
            FROM per_source
            WHERE docs >= 2
            ORDER BY "Delta" ASC NULLS LAST, docs DESC
        """,
    },
    {
        "key": "alerts",
        "name": "Peringatan Dini Penurunan Sentimen",
        "description": "Aspek dengan penurunan NSS signifikan secara statistik (z-score vs baseline 28 hari).",
        "display": "table",
        "position": {"row": 24, "col": 0, "size_x": 12, "size_y": 6},
        "filters": ["kanal", "aspek"],
        "sql": """
            SELECT
                aspect_label    AS "Aspek",
                source_platform AS "Kanal",
                nss_7d          AS "NSS 7 Hari",
                nss_baseline    AS "Baseline",
                nss_delta       AS "Perubahan",
                z_score         AS "Z-Score",
                severity        AS "Tingkat",
                alert_status    AS "Status"
            FROM {schema}.mart_sentiment_alerts
            WHERE 1 = 1
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            -- Show every aspect (incl. 'stable' / 'insufficient_data') so an
            -- empty card never looks like a broken one; deteriorations first.
            ORDER BY CASE alert_status
                         WHEN 'deterioration_significant' THEN 0
                         WHEN 'deterioration_watch' THEN 1
                         ELSE 2
                     END,
                     z_score ASC NULLS LAST
        """,
    },
    {
        "key": "review_queue",
        "name": "Antrean Tinjauan Manual (prioritas tertinggi)",
        "description": "Model sengaja tidak memutuskan sendiri di kasus ambigu - ini daftar kerjanya.",
        "display": "table",
        "position": {"row": 24, "col": 12, "size_x": 12, "size_y": 6},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT
                event_date         AS "Tanggal",
                source_platform    AS "Kanal",
                headline           AS "Cuplikan",
                sentiment_label    AS "Label Model",
                confidence         AS "Keyakinan",
                review_reason_id   AS "Alasan",
                review_priority    AS "Prioritas"
            FROM {schema}.mart_review_queue
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
            ORDER BY review_priority DESC
            LIMIT 100
        """,
    },
    {
        "key": "engine_quality",
        "name": "Kualitas Mesin Sentimen",
        "description": "Kecocokan dengan rating bintang & gold set, plus rata-rata keyakinan, per bulan.",
        "display": "line",
        "position": {"row": 30, "col": 0, "size_x": 12, "size_y": 6},
        "filters": ["tgl_mulai", "tgl_akhir"],
        # Monthly and volume-weighted (sum of matches / sum of documents), not an
        # average of daily rates, so a 1-document day cannot swing the line.
        "sql": """
            SELECT
                DATE_TRUNC('month', event_date)::date               AS "Bulan",
                ROUND(SUM(rating_matches)::numeric
                      / NULLIF(SUM(rated_documents), 0) * 100, 1)   AS "Cocok dgn Rating %",
                ROUND(SUM(gold_matches)::numeric
                      / NULLIF(SUM(gold_documents), 0) * 100, 1)    AS "Cocok dgn Gold Set %",
                ROUND(SUM(avg_confidence * documents_scored)
                      / NULLIF(SUM(documents_scored), 0) * 100, 1)  AS "Rata-rata Keyakinan %"
            FROM {schema}.mart_engine_quality
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
            GROUP BY 1
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Bulan"],
            "graph.metrics": [
                "Cocok dgn Rating %",
                "Cocok dgn Gold Set %",
                "Rata-rata Keyakinan %",
            ],
        },
    },
    {
        "key": "pipeline_health",
        "name": "Kesehatan Pipeline & Data Quality",
        "description": "Throughput konektor, tingkat duplikat, dan hasil gerbang Great Expectations.",
        "display": "table",
        "position": {"row": 30, "col": 12, "size_x": 12, "size_y": 6},
        # No "kanal" filter: rows are per *connector* (rss, web_scraper, ...),
        # not per platform, so filtering by kanal=news always returned nothing.
        "filters": ["tgl_mulai", "tgl_akhir"],
        "sql": """
            SELECT
                report_date         AS "Tanggal",
                connector           AS "Konektor",
                fetched             AS "Diambil",
                inserted            AS "Baru",
                duplicates          AS "Duplikat",
                ROUND(duplicate_rate * 100, 1) AS "Duplikat %",
                failures            AS "Gagal",
                expectations_failed AS "DQ Gagal"
            FROM {schema}.mart_pipeline_health
            WHERE 1 = 1
              [[AND report_date >= {{tgl_mulai}}]]
              [[AND report_date <= {{tgl_akhir}}]]
            ORDER BY report_date DESC, connector
            LIMIT 200
        """,
    },
    {
        "key": "document_feed",
        "name": "Feed Dokumen (drill-down)",
        "description": "Teks asli, label, keyakinan, frasa pemicu, dan aspek negatif per dokumen.",
        "display": "table",
        "position": {"row": 36, "col": 0, "size_x": 24, "size_y": 8},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT
                event_date          AS "Tanggal",
                source_platform     AS "Kanal",
                source_name         AS "Sumber",
                -- Verdict columns first so they are visible without scrolling.
                sentiment_label     AS "Sentimen",
                sentiment_score     AS "Skor",
                confidence          AS "Keyakinan",
                headline            AS "Judul / Cuplikan",
                rating              AS "Rating",
                negative_aspects    AS "Aspek Negatif",
                positive_aspects    AS "Aspek Positif",
                url                 AS "Tautan"
            FROM {schema}.mart_document_feed
            WHERE 1 = 1
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
            ORDER BY event_at DESC
            LIMIT 500
        """,
    },
    # --- Data quality observability (DQ engineer view) -----------------
    {
        "key": "dq_trend",
        "name": "Tren Hasil Gerbang Kualitas (Great Expectations)",
        "description": "Jumlah expectation gagal vs total per hari. Ini tren kualitas data, bukan sekadar status run terakhir.",
        "display": "line",
        "position": {"row": 44, "col": 0, "size_x": 12, "size_y": 6},
        "schema": "analytics_staging",
        "filters": ["tgl_mulai", "tgl_akhir"],
        "sql": """
            SELECT
                check_date                                      AS "Tanggal",
                COUNT(*)                                        AS "Total",
                COUNT(*) FILTER (WHERE NOT success)             AS "Gagal",
                ROUND(100.0 * COUNT(*) FILTER (WHERE success)
                      / NULLIF(COUNT(*), 0), 1)                 AS "Lolos %"
            FROM {schema}.stg_data_quality
            WHERE 1 = 1
              [[AND check_date >= {{tgl_mulai}}]]
              [[AND check_date <= {{tgl_akhir}}]]
            GROUP BY 1
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Tanggal"],
            "graph.metrics": ["Total", "Gagal", "Lolos %"],
        },
    },
    {
        "key": "dq_expectation_rank",
        "name": "Expectation Paling Sering Gagal",
        "description": "Expectation & kolom mana yang paling sering melanggar - target perbaikan sumber data.",
        "display": "bar",
        "position": {"row": 44, "col": 12, "size_x": 12, "size_y": 6},
        "schema": "analytics_staging",
        "filters": ["tgl_mulai", "tgl_akhir"],
        "sql": """
            SELECT
                expectation                                     AS "Expectation",
                column_name                                     AS "Kolom",
                COUNT(*) FILTER (WHERE NOT success)             AS "Gagal",
                COUNT(*)                                        AS "Total"
            FROM {schema}.stg_data_quality
            WHERE 1 = 1
              [[AND check_date >= {{tgl_mulai}}]]
              [[AND check_date <= {{tgl_akhir}}]]
            GROUP BY 1, 2
            HAVING COUNT(*) FILTER (WHERE NOT success) > 0
            ORDER BY 3 DESC
            LIMIT 20
        """,
        "visualization_settings": {
            "graph.dimensions": ["Expectation"],
            "graph.metrics": ["Gagal"],
        },
    },
    {
        "key": "dq_freshness",
        "name": "Freshness Ingestion per Konektor",
        "description": "Kapan terakhir tiap konektor berhasil menarik data, dan berapa umurnya. Mengungkap sumber yang diam-diam mati.",
        "display": "table",
        "position": {"row": 50, "col": 0, "size_x": 12, "size_y": 6},
        "schema": "analytics_staging",
        "sql": """
            SELECT
                connector                                                       AS "Konektor",
                MAX(started_at)                                                 AS "Run Terakhir",
                MAX(started_at) FILTER (WHERE is_success)                       AS "Sukses Terakhir",
                ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) / 3600, 1)  AS "Umur (jam)"
            FROM {schema}.stg_ingestion_runs
            WHERE started_at >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            ORDER BY 3 DESC NULLS LAST
        """,
    },
    {
        "key": "dq_gate_latest",
        "name": "Hasil Gate Terakhir per Expectation",
        "description": "Snapshot run gate terbaru: mana yang lolos, mana yang melanggar beserta jumlahnya.",
        "display": "table",
        "position": {"row": 50, "col": 12, "size_x": 12, "size_y": 6},
        "schema": "analytics_staging",
        "sql": """
            SELECT
                expectation                                     AS "Expectation",
                column_name                                     AS "Kolom",
                success                                         AS "Lolos",
                unexpected_count                                AS "Jumlah Anomali",
                unexpected_percent                              AS "Anomali %",
                checked_at                                      AS "Diperiksa"
            FROM {schema}.stg_data_quality
            WHERE checked_at = (SELECT MAX(checked_at) FROM {schema}.stg_data_quality)
            ORDER BY 3 ASC, 4 DESC NULLS LAST
        """,
    },
]

DASHBOARD_DESCRIPTION = (
    "Analisis sentimen BRILink dari berita nasional (RSS + web scraping), "
    "ulasan aplikasi (Play Store, App Store), forum (Reddit, Kaskus), "
    "media sosial (YouTube, Twitter/X), Google Trends, dan ulasan Google Maps. "
    "Label dihasilkan mesin ensemble multi-sinyal "
    "(IndoBERT + emosi + rating + konsensus aspek) - bukan pencocokan satu "
    "kata kunci. Setiap angka bisa ditelusuri sampai ke dokumen aslinya di "
    "kartu 'Feed Dokumen'. Gunakan filter Tanggal / Kanal / Aspek di atas "
    "untuk menggali - bagian bawah adalah tampilan kesehatan data pipeline."
)

# --- dashboard-wide filters -------------------------------------------
# ``slug`` appears in the SQL as an optional ``{{slug}}`` template tag; the
# provisioning script wires each of these to the matching cards via
# ``parameter_mappings``.

DASHBOARD_PARAMETERS: list[dict] = [
    {
        "name": "Tanggal mulai",
        "slug": "tgl_mulai",
        "id": "param_tgl_mulai",
        "type": "date/single",
        "sectionId": "date",
        "default": None,
    },
    {
        "name": "Tanggal akhir",
        "slug": "tgl_akhir",
        "id": "param_tgl_akhir",
        "type": "date/single",
        "sectionId": "date",
        "default": None,
    },
    {
        "name": "Kanal",
        "slug": "kanal",
        "id": "param_kanal",
        # Metabase v0.50 rejects "category/string" at query time (HTTP 500,
        # "Invalid parameter type") - which broke every filtered card.
        "type": "string/=",
        "sectionId": "string",
        "default": None,
        "values_source_type": "static-list",
        "values_source_config": {
            "values": [
                ["news", "Berita (RSS + Web)"],
                ["playstore", "Play Store"],
                ["appstore", "App Store"],
                ["reddit", "Reddit"],
                ["youtube", "YouTube"],
                ["twitter", "Twitter/X"],
                ["kaskus", "Kaskus Forum"],
                ["google_trends", "Google Trends"],
                ["google_maps", "Google Maps"],
                ["seed", "Korpus Sintetis"],
            ]
        },
    },
    {
        "name": "Aspek",
        "slug": "aspek",
        "id": "param_aspek",
        "type": "string/=",
        "sectionId": "string",
        "default": None,
        "values_source_type": "static-list",
        "values_source_config": {
            "values": [
                ["Akses & Inklusi Keuangan", "Akses & Inklusi Keuangan"],
                ["Aplikasi Digital", "Aplikasi Digital"],
                ["Biaya & Tarif", "Biaya & Tarif"],
                ["Dana & Penyelesaian Transaksi", "Dana & Penyelesaian Transaksi"],
                ["Jaringan & Sistem", "Jaringan & Sistem"],
                ["Keamanan & Fraud", "Keamanan & Fraud"],
                ["Kemitraan & Ekonomi Agen", "Kemitraan & Ekonomi Agen"],
                ["Layanan Agen", "Layanan Agen"],
            ]
        },
    },
]

# Template-tag shape for each filter slug (Metabase native SQL variables).
FILTERS: dict[str, dict] = {
    "tgl_mulai": {"type": "date", "widget-type": "date/single", "display-name": "Tanggal mulai"},
    "tgl_akhir": {"type": "date", "widget-type": "date/single", "display-name": "Tanggal akhir"},
    "kanal": {"type": "text", "widget-type": "string/=", "display-name": "Kanal"},
    "aspek": {"type": "text", "widget-type": "string/=", "display-name": "Aspek"},
}
