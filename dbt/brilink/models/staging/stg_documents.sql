{{ config(materialized='view') }}

/*
    Cleaned, typed copy of the landing table. No joins, no aggregation - the
    only job here is to make every downstream model agree on names, types and
    the handful of derived flags that are genuinely source-level.
*/

with source as (

    select * from {{ source('raw', 'documents') }}

),

cleaned as (

    select
        document_id,
        doc_uid,
        lower(trim(source_platform))                        as source_platform,
        lower(trim(source_name))                            as source_name,
        source_type,
        external_id,
        url,
        nullif(trim(title), '')                             as title,
        nullif(trim(body), '')                              as body,
        nullif(trim(author), '')                            as author,
        language,
        rating,
        engagement,
        published_at,
        ingested_at,
        ingestion_run_id,
        content_hash,
        relevance_score,
        raw_payload,

        coalesce((raw_payload ->> 'is_synthetic')::boolean, false)   as is_synthetic,
        coalesce((raw_payload ->> 'is_adversarial')::boolean, false) as is_adversarial,
        raw_payload ->> 'intended_polarity'                          as intended_polarity,
        raw_payload ->> 'collector'                                  as collector,

        length(coalesce(title, '') || ' ' || coalesce(body, ''))     as text_length,
        coalesce((engagement ->> 'score')::int,
                 (engagement ->> 'likes')::int,
                 (engagement ->> 'thumbs_up')::int, 0)               as engagement_score,

        -- Publication time is authoritative when present; ingestion time is the
        -- documented fallback so time-series never silently drop rows.
        coalesce(published_at, ingested_at)                          as event_at

    from source
    where coalesce(title, '') <> '' or coalesce(body, '') <> ''

)

select
    *,
    date_trunc('day',  event_at)::date as event_date,
    date_trunc('week', event_at)::date as event_week,
    date_trunc('month', event_at)::date as event_month,
    published_at is null                as used_ingested_at_fallback
from cleaned
