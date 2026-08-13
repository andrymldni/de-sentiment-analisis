{{ config(materialized='view') }}

select
    result_id,
    suite_name,
    expectation,
    column_name,
    success,
    evaluated_rows,
    (observed ->> 'unexpected_count')::int      as unexpected_count,
    (observed ->> 'unexpected_percent')::numeric as unexpected_percent,
    observed -> 'partial_unexpected_list'       as unexpected_sample,
    dag_run_id,
    checked_at,
    date_trunc('day', checked_at)::date         as check_date
from {{ source('ops', 'data_quality_results') }}
