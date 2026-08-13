{{ config(materialized='table') }}

/*
    Anomaly surface, computed in SQL rather than eyeballed on a chart.

    For each aspect and platform we compare the last 7 days against the
    preceding 28-day baseline and flag statistically meaningful negative
    swings. The z-score uses the baseline's own daily volatility, so a noisy
    low-volume channel does not fire an alert every week.
*/

with daily as (

    select * from {{ ref('mart_aspect_daily') }}
    where not is_synthetic or {{ var('include_synthetic') }}

),

recent as (

    select
        source_platform,
        aspect_code,
        aspect_label,
        aspect_group,
        sum(mention_documents)                as documents_7d,
        avg(aspect_nss)                       as nss_7d
    from daily
    where event_date >= current_date - 7
    group by 1, 2, 3, 4

),

baseline as (

    select
        source_platform,
        aspect_code,
        sum(mention_documents)                as documents_baseline,
        avg(aspect_nss)                       as nss_baseline,
        coalesce(stddev_samp(aspect_nss), 0)  as nss_volatility,
        count(*)                              as baseline_days
    from daily
    where event_date between current_date - 35 and current_date - 8
    group by 1, 2

)

select
    r.source_platform,
    r.aspect_code,
    r.aspect_label,
    r.aspect_group,
    r.documents_7d,
    b.documents_baseline,
    round(r.nss_7d, 2)                                              as nss_7d,
    round(b.nss_baseline, 2)                                        as nss_baseline,
    round(r.nss_7d - b.nss_baseline, 2)                             as nss_delta,
    round(b.nss_volatility, 2)                                      as nss_volatility,
    b.baseline_days,
    round(
        (r.nss_7d - b.nss_baseline) / nullif(b.nss_volatility, 0)
    , 2)                                                            as z_score,

    case
        when b.baseline_days < 7 or r.documents_7d < 5 then 'insufficient_data'
        when b.nss_volatility = 0 then 'no_variance'
        when (r.nss_7d - b.nss_baseline) / nullif(b.nss_volatility, 0) <= -2
             then 'deterioration_significant'
        when (r.nss_7d - b.nss_baseline) / nullif(b.nss_volatility, 0) <= -1
             then 'deterioration_watch'
        when (r.nss_7d - b.nss_baseline) / nullif(b.nss_volatility, 0) >= 2
             then 'improvement_significant'
        else 'stable'
    end                                                             as alert_status,

    case
        when r.documents_7d >= 20 and (r.nss_7d - b.nss_baseline) <= -15 then 'high'
        when r.documents_7d >= 10 and (r.nss_7d - b.nss_baseline) <= -8  then 'medium'
        else 'low'
    end                                                             as severity

from recent r
inner join baseline b
    on b.source_platform = r.source_platform
   and b.aspect_code     = r.aspect_code
