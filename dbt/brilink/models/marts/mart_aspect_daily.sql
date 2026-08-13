{{ config(materialized='table', indexes=[{'columns':['event_date'],'type':'btree'}]) }}

/*
    Aspect-level trend: the model's real value. "Sentiment is down" is not
    actionable; "sentiment on Biaya & Tarif is down 18 points in Java-facing
    outlets while Akses & Inklusi is flat" is.
*/

with aspects as (

    select * from {{ ref('stg_document_aspects') }}

),

scored as (

    select * from {{ ref('int_documents_scored') }}
    where is_kpi_eligible

),

joined as (

    select
        s.event_date,
        s.source_platform,
        s.is_synthetic,
        a.aspect_code,
        a.aspect_label,
        a.aspect_group,
        a.aspect_score,
        a.aspect_label_sentiment,
        a.mentions,
        s.engagement_score
    from aspects a
    inner join scored s on s.document_id = a.document_id

)

select
    event_date,
    source_platform,
    is_synthetic,
    aspect_code,
    aspect_label,
    aspect_group,
    count(*)                                                            as mention_documents,
    sum(mentions)                                                       as mention_count,
    sum(case when aspect_label_sentiment = 'positive' then 1 else 0 end) as positive_count,
    sum(case when aspect_label_sentiment = 'negative' then 1 else 0 end) as negative_count,
    sum(case when aspect_label_sentiment = 'neutral'  then 1 else 0 end) as neutral_count,
    round(avg(aspect_score), 4)                                         as avg_aspect_score,
    sum(engagement_score)                                               as total_engagement,
    {{ net_sentiment_score(
        "sum(case when aspect_label_sentiment = 'positive' then 1 else 0 end)",
        "sum(case when aspect_label_sentiment = 'negative' then 1 else 0 end)",
        'count(*)') }}                                                  as aspect_nss
from joined
group by 1, 2, 3, 4, 5, 6
