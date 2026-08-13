{{ config(materialized='table', indexes=[{'columns':['event_date'],'type':'btree'}]) }}

/*
    Headline daily trend. One row per (date, platform, source, synthetic flag).
    Net Sentiment Score (NSS) = (%positive - %negative), the single number the
    dashboard leads with, plus a 7-day rolling version to damp weekday noise.
*/

with base as (

    select * from {{ ref('int_documents_scored') }}
    where is_kpi_eligible

),

daily as (

    select
        event_date,
        source_platform,
        source_name,
        is_synthetic,
        count(*)                                   as document_count,
        sum(is_positive)                           as positive_count,
        sum(is_negative)                           as negative_count,
        sum(is_neutral)                            as neutral_count,
        round(avg(sentiment_score), 4)             as avg_sentiment_score,
        round(avg(confidence), 4)                  as avg_confidence,
        round(avg(agreement), 4)                   as avg_agreement,
        sum(case when requires_review then 1 else 0 end) as review_count,
        sum(engagement_score)                      as total_engagement
    from base
    group by 1, 2, 3, 4

)

select
    *,
    {{ net_sentiment_score('positive_count', 'negative_count', 'document_count') }} as net_sentiment_score,
    {{ safe_divide('positive_count', 'document_count') }} as positive_share,
    {{ safe_divide('negative_count', 'document_count') }} as negative_share,
    {{ safe_divide('review_count',   'document_count') }} as review_share,
    round(
        avg(
            {{ net_sentiment_score('positive_count', 'negative_count', 'document_count') }}
        ) over (
            partition by source_platform, source_name, is_synthetic
            order by event_date
            rows between {{ var('rolling_window_days') - 1 }} preceding and current row
        )
    , 2) as net_sentiment_score_rolling,
    sum(document_count) over (
        partition by source_platform, source_name, is_synthetic
        order by event_date
        rows between {{ var('rolling_window_days') - 1 }} preceding and current row
    ) as document_count_rolling
from daily
