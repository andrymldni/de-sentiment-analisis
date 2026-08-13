-- =====================================================================
-- BRILink Sentiment Platform - warehouse schema
--
-- Layering:
--   raw   : immutable landing zone, one row per source document
--   core  : engine output (document + aspect level) and dimensions
--   ops   : operational metadata surfaced on the dashboard
-- dbt builds staging / intermediate / marts schemas on top of these.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------------
-- raw.documents : canonical landing table for every source
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.documents (
    document_id      BIGSERIAL PRIMARY KEY,
    doc_uid          TEXT        NOT NULL UNIQUE,
    source_platform  TEXT        NOT NULL,
    source_name      TEXT        NOT NULL,
    source_type      TEXT        NOT NULL DEFAULT 'ugc',
    external_id      TEXT        NOT NULL,
    url              TEXT,
    title            TEXT,
    body             TEXT,
    author           TEXT,
    language         TEXT        NOT NULL DEFAULT 'unknown',
    rating           SMALLINT,
    engagement       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    published_at     TIMESTAMPTZ,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_run_id UUID,
    content_hash     TEXT        NOT NULL,
    simhash          BIGINT      NOT NULL DEFAULT 0,
    relevance_score  NUMERIC(5,4) NOT NULL DEFAULT 0,
    raw_payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT chk_rating_range CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    CONSTRAINT chk_has_text     CHECK (COALESCE(title, '') <> '' OR COALESCE(body, '') <> '')
);

CREATE INDEX IF NOT EXISTS idx_documents_published   ON raw.documents (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_platform    ON raw.documents (source_platform, source_name);
CREATE INDEX IF NOT EXISTS idx_documents_ingested    ON raw.documents (ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON raw.documents (content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_payload_gin ON raw.documents USING GIN (raw_payload);

COMMENT ON TABLE  raw.documents IS 'Immutable landing zone. One row per source document across news, app stores and social platforms.';
COMMENT ON COLUMN raw.documents.doc_uid IS 'Deterministic hash of platform+source+external id. Idempotency key for re-runs.';
COMMENT ON COLUMN raw.documents.simhash IS '64-bit SimHash used for near-duplicate detection of syndicated copy.';
COMMENT ON COLUMN raw.documents.relevance_score IS 'Share of target keywords present. Used to drop off-topic captures.';

-- ---------------------------------------------------------------------
-- raw.ingestion_runs : one row per connector execution
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.ingestion_runs (
    run_id           UUID PRIMARY KEY,
    connector        TEXT        NOT NULL,
    status           TEXT        NOT NULL,
    window_start     TIMESTAMPTZ,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    fetched_count    INTEGER     NOT NULL DEFAULT 0,
    inserted_count   INTEGER     NOT NULL DEFAULT 0,
    duplicate_count  INTEGER     NOT NULL DEFAULT 0,
    irrelevant_count INTEGER     NOT NULL DEFAULT 0,
    message          TEXT,
    metrics          JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_runs_connector ON raw.ingestion_runs (connector, started_at DESC);

-- ---------------------------------------------------------------------
-- core.dim_aspect : business aspect catalogue (loaded by the app)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.dim_aspect (
    aspect_code   TEXT PRIMARY KEY,
    aspect_label  TEXT NOT NULL,
    aspect_group  TEXT NOT NULL,
    description   TEXT,
    keyword_count INTEGER NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- core.document_sentiment : ensemble output, one row per document
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.document_sentiment (
    document_id      BIGINT       PRIMARY KEY
                     REFERENCES raw.documents(document_id) ON DELETE CASCADE,
    sentiment_label  TEXT         NOT NULL,
    sentiment_score  NUMERIC(6,4) NOT NULL,
    confidence       NUMERIC(6,4) NOT NULL,
    agreement        NUMERIC(6,4) NOT NULL,
    requires_review  BOOLEAN      NOT NULL DEFAULT FALSE,
    review_reason    TEXT,
    reviewed_label   TEXT,
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    model_version    TEXT         NOT NULL,
    engine_mode      TEXT         NOT NULL,
    signals          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    explanation      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    scored_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_label      CHECK (sentiment_label IN ('positive','neutral','negative')),
    CONSTRAINT chk_score_rng  CHECK (sentiment_score BETWEEN -1 AND 1),
    CONSTRAINT chk_conf_rng   CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT chk_review_lbl CHECK (reviewed_label IS NULL OR reviewed_label IN ('positive','neutral','negative'))
);

CREATE INDEX IF NOT EXISTS idx_sentiment_label   ON core.document_sentiment (sentiment_label);
CREATE INDEX IF NOT EXISTS idx_sentiment_review  ON core.document_sentiment (requires_review) WHERE requires_review;
CREATE INDEX IF NOT EXISTS idx_sentiment_scored  ON core.document_sentiment (scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_sentiment_signals ON core.document_sentiment USING GIN (signals);

COMMENT ON COLUMN core.document_sentiment.signals IS 'Per-signal score, weight and evidence. Enables "why was this negative?" drill-down.';
COMMENT ON COLUMN core.document_sentiment.agreement IS '1.0 = all signals agree; low values route the document to human review.';

-- ---------------------------------------------------------------------
-- core.document_aspect_sentiment : ABSA output, one row per (doc, aspect)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.document_aspect_sentiment (
    document_id     BIGINT       NOT NULL
                    REFERENCES raw.documents(document_id) ON DELETE CASCADE,
    aspect_code     TEXT         NOT NULL REFERENCES core.dim_aspect(aspect_code),
    mentions        INTEGER      NOT NULL DEFAULT 1,
    sentiment_label TEXT         NOT NULL,
    sentiment_score NUMERIC(6,4) NOT NULL,
    matched_keywords TEXT[]      NOT NULL DEFAULT '{}',
    evidence        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    scored_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (document_id, aspect_code),
    CONSTRAINT chk_aspect_label CHECK (sentiment_label IN ('positive','neutral','negative')),
    CONSTRAINT chk_aspect_score CHECK (sentiment_score BETWEEN -1 AND 1)
);

CREATE INDEX IF NOT EXISTS idx_aspect_code  ON core.document_aspect_sentiment (aspect_code);
CREATE INDEX IF NOT EXISTS idx_aspect_label ON core.document_aspect_sentiment (aspect_code, sentiment_label);

-- ---------------------------------------------------------------------
-- ops.data_quality_results : Great Expectations gate outcomes
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.data_quality_results (
    result_id     BIGSERIAL PRIMARY KEY,
    suite_name    TEXT        NOT NULL,
    expectation   TEXT        NOT NULL,
    column_name   TEXT,
    success       BOOLEAN     NOT NULL,
    observed      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    evaluated_rows INTEGER    NOT NULL DEFAULT 0,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dag_run_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_dq_checked ON ops.data_quality_results (checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_dq_success ON ops.data_quality_results (success, checked_at DESC);
