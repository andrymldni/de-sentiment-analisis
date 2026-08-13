{{ config(materialized='table') }}

/*
    Source scorecard for the last 30 / 90 days. Answers "which outlet or channel
    is driving the narrative, and in which direction?" - and, importantly, how
    much we should trust each row (volume + average confidence).
*/

with base as (

    select * from {{ ref('int_documents_scored') }}
    where is_kpi_eligible

),

windowed as (

    select
        source_platform,
        source_name,
        source_type,
        is_synthetic,
        count(*)                                                                   as documents_90d,
        count(*) filter (where event_date >= current_date - 30)                    as documents_30d,
        sum(is_positive)                                                           as positive_90d,
        sum(is_negative)                                                           as negative_90d,
        sum(is_positive) filter (where event_date >= current_date - 30)            as positive_30d,
        sum(is_negative) filter (where event_date >= current_date - 30)            as negative_30d,
        count(*) filter (where event_date >= current_date - 30)                    as denom_30d,
        round(avg(sentiment_score), 4)                                             as avg_sentiment_score,
        round(avg(sentiment_score) filter (where event_date >= current_date - 30), 4) as avg_sentiment_score_30d,
        round(avg(confidence), 4)                                                  as avg_confidence,
        sum(engagement_score)                                                      as total_engagement,
        max(event_date)                                                            as last_document_date
    from base
    where event_date >= current_date - 90
    group by 1, 2, 3, 4

)

select
    *,
    {{ net_sentiment_score('positive_90d', 'negative_90d', 'documents_90d') }} as nss_90d,
    {{ net_sentiment_score('positive_30d', 'negative_30d', 'denom_30d') }}     as nss_30d,
    {{ net_sentiment_score('positive_30d', 'negative_30d', 'denom_30d') }}
        - {{ net_sentiment_score('positive_90d', 'negative_90d', 'documents_90d') }} as nss_delta_30d_vs_90d,
    current_date - last_document_date                                          as days_since_last_document
from windowed
