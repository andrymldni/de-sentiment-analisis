{{ config(materialized='view') }}

select
    run_id,
    connector,
    status,
    status = 'success'                                            as is_success,
    status like 'skipped%'                                        as is_skipped,
    status in ('failed', 'unavailable')                           as is_failure,
    window_start,
    started_at,
    finished_at,
    extract(epoch from (finished_at - started_at))                as duration_seconds,
    fetched_count,
    inserted_count,
    duplicate_count,
    irrelevant_count,
    {{ safe_divide('duplicate_count', 'fetched_count') }}         as duplicate_rate,
    message,
    date_trunc('day', started_at)::date                           as run_date
from {{ source('raw', 'ingestion_runs') }}
