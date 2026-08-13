{{ config(materialized='table') }}

/*
    Does the engine deserve to be believed?

    Three independent checks, none of which requires a hand-labelled test set:
      * agreement with the platform star rating (weak supervision);
      * agreement with the synthetic corpus's intended polarity (gold cases);
      * per-signal availability, which catches a silently-missing model.
*/

with scored as (

    select * from {{ ref('int_documents_scored') }}

),

rating_agreement as (

    select
        event_date,
        model_version,
        count(*)                                                                    as rated_documents,
        sum(case when rating_implied_label = effective_label then 1 else 0 end)      as rating_matches,
        sum(case when rating_implied_label = 'negative'
                  and effective_label = 'positive' then 1 else 0 end)               as severe_disagreements
    from scored
    where rating_implied_label is not null
    group by 1, 2

),

gold_agreement as (

    select
        event_date,
        model_version,
        count(*)                                                       as gold_documents,
        sum(case when matches_intended_polarity then 1 else 0 end)     as gold_matches
    from scored
    where intended_polarity is not null
    group by 1, 2

),

coverage as (

    select
        event_date,
        model_version,
        count(*)                                                        as documents_scored,
        round(avg(confidence), 4)                                       as avg_confidence,
        round(avg(agreement), 4)                                        as avg_agreement,
        sum(case when requires_review then 1 else 0 end)                as review_count,
        sum(case when has_transformer_signal then 1 else 0 end)         as transformer_signals,
        sum(case when has_emotion_signal     then 1 else 0 end)         as emotion_signals,
        sum(case when has_rating_signal      then 1 else 0 end)         as rating_signals,
        sum(case when escalated_to_llm       then 1 else 0 end)         as llm_escalations
    from scored
    group by 1, 2

)

select
    c.event_date,
    c.model_version,
    c.documents_scored,
    c.avg_confidence,
    c.avg_agreement,
    c.review_count,
    {{ safe_divide('c.review_count', 'c.documents_scored') }}              as review_rate,
    c.transformer_signals,
    c.emotion_signals,
    c.rating_signals,
    c.llm_escalations,
    {{ safe_divide('c.transformer_signals', 'c.documents_scored') }}       as transformer_coverage,
    r.rated_documents,
    r.rating_matches,
    {{ safe_divide('r.rating_matches', 'r.rated_documents') }}             as rating_agreement_rate,
    r.severe_disagreements,
    g.gold_documents,
    g.gold_matches,
    {{ safe_divide('g.gold_matches', 'g.gold_documents') }}                as gold_agreement_rate
from coverage c
left join rating_agreement r on r.event_date = c.event_date and r.model_version = c.model_version
left join gold_agreement   g on g.event_date = c.event_date and g.model_version = c.model_version
