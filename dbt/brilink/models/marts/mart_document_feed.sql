{{ config(materialized='table', indexes=[{'columns':['event_at'],'type':'btree'}]) }}

/*
    Drill-down table behind every chart. Deliberately wide: a Metabase user who
    clicks a spike lands here and can read the actual text, the label, the
    confidence, the driving phrases and the aspects - without writing SQL.
*/

select
    d.document_id,
    d.event_at,
    d.event_date,
    d.source_platform,
    d.source_name,
    d.source_type,
    d.is_synthetic,
    d.url,
    coalesce(d.title, left(d.body, 180))                       as headline,
    left(coalesce(d.body, ''), 1200)                           as excerpt,
    d.author,
    d.rating,
    d.engagement_score,
    d.effective_label                                          as sentiment_label,
    d.sentiment_score,
    d.confidence,
    d.agreement,
    d.requires_review,
    d.review_reason,
    d.human_reviewed,
    d.model_version,
    d.transformer_score,
    d.emotion_score,
    d.rating_score,
    d.dominant_emotion,
    d.rating_implied_label,
    d.signal_contributions,

    -- Aspect summary rolled up to the document, ready for a table column.
    a.aspect_codes,
    a.negative_aspects,
    a.positive_aspects,
    a.worst_aspect_code,
    a.worst_aspect_score

from {{ ref('int_documents_scored') }} d
left join (

    select
        document_id,
        array_agg(aspect_code order by aspect_code)                                as aspect_codes,
        array_agg(aspect_label order by aspect_score)
            filter (where aspect_label_sentiment = 'negative')                     as negative_aspects,
        array_agg(aspect_label order by aspect_score desc)
            filter (where aspect_label_sentiment = 'positive')                     as positive_aspects,
        (array_agg(aspect_code order by aspect_score asc))[1]                      as worst_aspect_code,
        min(aspect_score)                                                          as worst_aspect_score
    from {{ ref('stg_document_aspects') }}
    group by document_id

) a on a.document_id = d.document_id
