{{ config(materialized='table') }}

/*
    Operations pane: connector freshness, throughput, dedup effectiveness and
    the data-quality gate's record. If a chart looks wrong, this is the first
    place to look - which is why it ships with the dashboard rather than being
    an afterthought in the logs.
*/

with runs as (

    select
        run_date,
        connector,
        count(*)                                          as runs,
        sum(case when is_success then 1 else 0 end)       as successes,
        sum(case when is_failure then 1 else 0 end)       as failures,
        sum(case when is_skipped then 1 else 0 end)       as skips,
        sum(fetched_count)                                as fetched,
        sum(inserted_count)                               as inserted,
        sum(duplicate_count)                              as duplicates,
        sum(irrelevant_count)                             as irrelevant,
        round(avg(duration_seconds), 2)                   as avg_duration_seconds
    from {{ ref('stg_ingestion_runs') }}
    group by 1, 2

),

quality as (

    select
        check_date,
        count(*)                                          as expectations_run,
        sum(case when success then 1 else 0 end)          as expectations_passed,
        sum(case when not success then 1 else 0 end)      as expectations_failed,
        max(evaluated_rows)                               as rows_validated
    from {{ ref('stg_data_quality') }}
    group by 1

)

select
    r.run_date                                            as report_date,
    r.connector,
    r.runs,
    r.successes,
    r.failures,
    r.skips,
    r.fetched,
    r.inserted,
    r.duplicates,
    r.irrelevant,
    r.avg_duration_seconds,
    {{ safe_divide('r.duplicates', 'r.fetched') }}        as duplicate_rate,
    {{ safe_divide('r.inserted', 'r.fetched') }}          as yield_rate,
    q.expectations_run,
    q.expectations_passed,
    q.expectations_failed,
    q.rows_validated,
    {{ safe_divide('q.expectations_passed', 'q.expectations_run') }} as quality_pass_rate
from runs r
left join quality q on q.check_date = r.run_date
