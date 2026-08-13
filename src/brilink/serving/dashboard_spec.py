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
"""

from __future__ import annotations

# Grid is 24 columns wide in Metabase.
CARDS: list[dict] = [
    {
        "key": "kpi_documents",
        "name": "Total Dokumen Dianalisis (90 hari)",
        "description": "Jumlah dokumen dari seluruh kanal yang lolos ambang keyakinan.",
        "display": "scalar",
        "position": {"row": 0, "col": 0, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT SUM(document_count) AS "Dokumen"
            FROM {schema}.mart_sentiment_daily
            WHERE event_date >= CURRENT_DATE - 90
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
    },
    {
        "key": "kpi_nss",
        "name": "Net Sentiment Score (30 hari)",
        "description": "(%positif - %negatif) x 100. Rentang -100 s/d +100.",
        "display": "scalar",
        "position": {"row": 0, "col": 6, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT ROUND(
                     (SUM(positive_count) - SUM(negative_count))::numeric
                     / NULLIF(SUM(document_count), 0) * 100, 1) AS "NSS 30 Hari"
            FROM {schema}.mart_sentiment_daily
            WHERE event_date >= CURRENT_DATE - 30
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
        """,
        "visualization_settings": {"scalar.suffix": " pts"},
    },
    {
        "key": "kpi_negative_share",
        "name": "Porsi Sentimen Negatif (30 hari)",
        "description": "Persentase dokumen berlabel negatif.",
        "display": "scalar",
        "position": {"row": 0, "col": 12, "size_x": 6, "size_y": 3},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT ROUND(SUM(negative_count)::numeric
                         / NULLIF(SUM(document_count), 0) * 100, 1) AS "Negatif %"
            FROM {schema}.mart_sentiment_daily
            WHERE event_date >= CURRENT_DATE - 30
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
        "name": "Tren Sentimen Harian (rolling 7 hari)",
        "description": "NSS harian dan rata-rata bergerak 7 hari untuk meredam noise akhir pekan.",
        "display": "line",
        "position": {"row": 3, "col": 0, "size_x": 16, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
        "sql": """
            SELECT
                event_date                                          AS "Tanggal",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(document_count), 0) * 100, 2)    AS "NSS Harian",
                ROUND(AVG(net_sentiment_score_rolling), 2)          AS "NSS Rolling 7 Hari",
                SUM(document_count)                                 AS "Volume Dokumen"
            FROM {schema}.mart_sentiment_daily
            WHERE event_date >= CURRENT_DATE - 120
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
            GROUP BY 1
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Tanggal"],
            "graph.metrics": ["NSS Harian", "NSS Rolling 7 Hari"],
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
            WHERE event_date >= CURRENT_DATE - 90
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
        },
    },
    {
        "key": "aspect_ranking",
        "name": "Peringkat Aspek Berdasarkan NSS (90 hari)",
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
            WHERE event_date >= CURRENT_DATE - 90
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
        },
    },
    {
        "key": "aspect_trend",
        "name": "Tren Mingguan per Aspek",
        "description": "Pergerakan NSS tiap aspek dari minggu ke minggu.",
        "display": "line",
        "position": {"row": 10, "col": 12, "size_x": 12, "size_y": 7},
        "filters": ["tgl_mulai", "tgl_akhir", "kanal", "aspek"],
        "sql": """
            SELECT
                DATE_TRUNC('week', event_date)::date                AS "Minggu",
                aspect_label                                        AS "Aspek",
                ROUND((SUM(positive_count) - SUM(negative_count))::numeric
                      / NULLIF(SUM(mention_documents), 0) * 100, 1) AS "NSS"
            FROM {schema}.mart_aspect_daily
            WHERE event_date >= CURRENT_DATE - 120
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            GROUP BY 1, 2
            HAVING SUM(mention_documents) >= 3
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Minggu", "Aspek"],
            "graph.metrics": ["NSS"],
        },
    },
    {
        "key": "aspect_matrix",
        "name": "Matriks Aspek x Kanal",
        "description": "Di kanal mana setiap aspek paling banyak dibicarakan, dan bagaimana nadanya.",
        "display": "table",
        "position": {"row": 17, "col": 0, "size_x": 12, "size_y": 7},
        "filters": ["kanal", "aspek"],
        "sql": """
            SELECT
                aspect_label       AS "Aspek",
                source_platform    AS "Kanal",
                mention_documents  AS "Dokumen",
                aspect_nss         AS "NSS",
                share_of_voice_pct AS "Share of Voice %"
            FROM {schema}.mart_aspect_source_matrix
            WHERE 1 = 1
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            ORDER BY mention_documents DESC
        """,
    },
    {
        "key": "source_scorecard",
        "name": "Scorecard Sumber (30 vs 90 hari)",
        "description": "Sumber mana yang nadanya berubah paling tajam belakangan ini.",
        "display": "table",
        "position": {"row": 17, "col": 12, "size_x": 12, "size_y": 7},
        "filters": ["kanal"],
        "sql": """
            SELECT
                source_platform       AS "Kanal",
                source_name           AS "Sumber",
                documents_90d         AS "Dokumen 90h",
                nss_90d               AS "NSS 90h",
                nss_30d               AS "NSS 30h",
                nss_delta_30d_vs_90d  AS "Delta",
                avg_confidence        AS "Rata-rata Keyakinan"
            FROM {schema}.mart_sentiment_by_source
            WHERE documents_90d >= 5
              [[AND source_platform = {{kanal}}]]
            ORDER BY nss_delta_30d_vs_90d ASC
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
            WHERE alert_status IN ('deterioration_significant', 'deterioration_watch')
              [[AND source_platform = {{kanal}}]]
              [[AND aspect_label = {{aspek}}]]
            ORDER BY z_score ASC
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
        "description": "Kecocokan dengan rating bintang & gold set, plus ketersediaan tiap sinyal per hari.",
        "display": "line",
        "position": {"row": 30, "col": 0, "size_x": 12, "size_y": 6},
        "filters": ["tgl_mulai", "tgl_akhir"],
        "sql": """
            SELECT
                event_date                          AS "Tanggal",
                ROUND(rating_agreement_rate * 100, 1) AS "Cocok dgn Rating %",
                ROUND(gold_agreement_rate   * 100, 1) AS "Cocok dgn Gold Set %",
                ROUND(avg_confidence        * 100, 1) AS "Rata-rata Keyakinan %"
            FROM {schema}.mart_engine_quality
            WHERE event_date >= CURRENT_DATE - 90
              [[AND event_date >= {{tgl_mulai}}]]
              [[AND event_date <= {{tgl_akhir}}]]
            ORDER BY 1
        """,
        "visualization_settings": {
            "graph.dimensions": ["Tanggal"],
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
        "filters": ["tgl_mulai", "tgl_akhir", "kanal"],
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
              [[AND connector = {{kanal}}]]
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
                event_at            AS "Waktu",
                source_platform     AS "Kanal",
                source_name         AS "Sumber",
                headline            AS "Judul / Cuplikan",
                sentiment_label     AS "Sentimen",
                sentiment_score     AS "Skor",
                confidence          AS "Keyakinan",
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
            WHERE check_date >= CURRENT_DATE - 30
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
        "filters": ["kanal"],
        "sql": """
            SELECT
                connector                                                       AS "Konektor",
                MAX(started_at)                                                 AS "Run Terakhir",
                MAX(started_at) FILTER (WHERE is_success)                       AS "Sukses Terakhir",
                ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) / 3600, 1)  AS "Umur (jam)"
            FROM {schema}.stg_ingestion_runs
            WHERE started_at >= NOW() - INTERVAL '30 days'
              [[AND connector = {{kanal}}]]
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
    "Analisis sentimen BRILink dari berita nasional, ulasan aplikasi, dan "
    "percakapan sosial. Label dihasilkan mesin ensemble multi-sinyal "
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
        "type": "category/string",
        "sectionId": "string",
        "default": None,
        "values_source_type": "static-list",
        "values_source_config": {
            "values": [
                ["news", "Berita (RSS)"],
                ["playstore", "Play Store"],
                ["appstore", "App Store"],
                ["reddit", "Reddit"],
                ["youtube", "YouTube"],
                ["twitter", "Twitter/X"],
                ["seed", "Korpus Sintetis"],
            ]
        },
    },
    {
        "name": "Aspek",
        "slug": "aspek",
        "id": "param_aspek",
        "type": "category/string",
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
    "kanal": {"type": "text", "widget-type": "category", "display-name": "Kanal"},
    "aspek": {"type": "text", "widget-type": "category", "display-name": "Aspek"},
}
