-- =====================================================================
-- Convenience views.
--
-- These exist so the warehouse is usable the moment ingestion finishes,
-- even before dbt has run. The analytical models the dashboard actually
-- consumes live in dbt (marts schema); these are the operational cuts.
-- =====================================================================

-- Flat document + sentiment join, the workhorse for ad-hoc exploration.
CREATE OR REPLACE VIEW core.v_document_sentiment AS
SELECT
    d.document_id,
    d.doc_uid,
    d.source_platform,
    d.source_name,
    d.source_type,
    d.title,
    d.body,
    d.url,
    d.author,
    d.language,
    d.rating,
    d.published_at,
    d.ingested_at,
    d.relevance_score,
    COALESCE((d.raw_payload ->> 'is_synthetic')::boolean, FALSE) AS is_synthetic,
    s.sentiment_label,
    s.sentiment_score,
    s.confidence,
    s.agreement,
    s.requires_review,
    s.review_reason,
    s.reviewed_label,
    s.model_version,
    s.engine_mode,
    s.scored_at,
    s.signals,
    s.explanation
FROM raw.documents d
JOIN core.document_sentiment s ON s.document_id = d.document_id;

COMMENT ON VIEW core.v_document_sentiment IS 'Document-level sentiment with full source context and explainability payloads.';

-- Human review queue: what an analyst should look at first.
CREATE OR REPLACE VIEW core.v_review_queue AS
SELECT
    d.document_id,
    d.source_platform,
    d.source_name,
    d.url,
    d.published_at,
    COALESCE(d.title, LEFT(d.body, 160)) AS preview,
    s.sentiment_label,
    s.sentiment_score,
    s.confidence,
    s.agreement,
    s.review_reason,
    s.explanation -> 'contributions'  AS signal_contributions,
    s.explanation -> 'drivers'        AS lexical_drivers
FROM raw.documents d
JOIN core.document_sentiment s ON s.document_id = d.document_id
WHERE s.requires_review
  AND s.reviewed_label IS NULL
ORDER BY s.confidence ASC, d.published_at DESC;

COMMENT ON VIEW core.v_review_queue IS 'Low-confidence or conflicting documents awaiting human adjudication.';

-- Engine observability: are the signals healthy, and how often do they fire?
CREATE OR REPLACE VIEW ops.v_engine_health AS
SELECT
    DATE_TRUNC('day', s.scored_at)::date          AS score_date,
    s.model_version,
    s.engine_mode,
    COUNT(*)                                      AS documents_scored,
    ROUND(AVG(s.confidence), 4)                   AS avg_confidence,
    ROUND(AVG(s.agreement), 4)                    AS avg_agreement,
    COUNT(*) FILTER (WHERE s.requires_review)     AS review_count,
    ROUND(
        COUNT(*) FILTER (WHERE s.requires_review)::numeric
        / NULLIF(COUNT(*), 0) * 100, 2)           AS review_rate_pct,
    COUNT(*) FILTER (WHERE (s.signals -> 'transformer' ->> 'available')::boolean) AS transformer_available,
    COUNT(*) FILTER (WHERE (s.signals -> 'emotion'     ->> 'available')::boolean) AS emotion_available,
    COUNT(*) FILTER (WHERE (s.signals -> 'rating'      ->> 'available')::boolean) AS rating_available,
    COUNT(*) FILTER (WHERE (s.signals -> 'aspect'      ->> 'available')::boolean) AS aspect_available
FROM core.document_sentiment s
GROUP BY 1, 2, 3;

COMMENT ON VIEW ops.v_engine_health IS 'Per-day signal coverage and confidence. Detects silent model degradation.';

-- Ingestion freshness per connector - the first thing to check when numbers look wrong.
CREATE OR REPLACE VIEW ops.v_ingestion_freshness AS
SELECT
    connector,
    MAX(finished_at)                                        AS last_run_at,
    ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(finished_at))) / 3600.0, 2) AS hours_since_last_run,
    SUM(inserted_count)  FILTER (WHERE started_at >= NOW() - INTERVAL '7 days') AS inserted_7d,
    SUM(duplicate_count) FILTER (WHERE started_at >= NOW() - INTERVAL '7 days') AS duplicates_7d,
    COUNT(*) FILTER (WHERE status = 'success'  AND started_at >= NOW() - INTERVAL '7 days') AS successes_7d,
    COUNT(*) FILTER (WHERE status IN ('failed','unavailable') AND started_at >= NOW() - INTERVAL '7 days') AS failures_7d
FROM raw.ingestion_runs
GROUP BY connector;

COMMENT ON VIEW ops.v_ingestion_freshness IS 'Connector-level SLA view: recency, throughput and failure counts.';
