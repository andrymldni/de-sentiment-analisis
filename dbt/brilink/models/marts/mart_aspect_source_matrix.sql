{{ config(materialized='table') }}

/*
    Aspect x platform heatmap for the trailing 90 days, with a share-of-voice
    column so a loud-but-tiny aspect is not mistaken for a systemic problem.
*/

with joined as (

    select
        s.source_platform,
        a.aspect_code,
        a.aspect_label,
        a.aspect_group,
        a.aspect_score,
        a.aspect_label_sentiment
    from {{ ref('stg_document_aspects') }} a
    inner join {{ ref('int_documents_scored') }} s on s.document_id = a.document_id
    where s.is_kpi_eligible
      and s.event_date >= current_date - 90

),

agg as (

    select
        source_platform,
        aspect_code,
        aspect_label,
        aspect_group,
        count(*)                                                            as mention_documents,
        sum(case when aspect_label_sentiment = 'positive' then 1 else 0 end) as positive_count,
        sum(case when aspect_label_sentiment = 'negative' then 1 else 0 end) as negative_count,
        round(avg(aspect_score), 4)                                         as avg_aspect_score
    from joined
    group by 1, 2, 3, 4

)

select
    *,
    {{ net_sentiment_score('positive_count', 'negative_count', 'mention_documents') }} as aspect_nss,
    round(
        mention_documents::numeric
        / nullif(sum(mention_documents) over (partition by source_platform), 0) * 100
    , 2) as share_of_voice_pct
from agg
