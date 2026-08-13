{{ config(materialized='view') }}

/*
    Pivots the ensemble's per-signal scores from wide to long.

    This is what makes the "why?" answerable in Metabase: a user can chart the
    contribution of each signal over time, spot the day IndoBERT stopped
    loading, or see that the emotion model and the transformer routinely
    disagree on a particular source.
*/

with scored as (

    select * from {{ ref('int_documents_scored') }}

),

unpivoted as (

    select document_id, event_date, source_platform, effective_label, confidence,
           'transformer' as signal_name, transformer_score as signal_score from scored
    union all
    select document_id, event_date, source_platform, effective_label, confidence,
           'emotion'     as signal_name, emotion_score     as signal_score from scored
    union all
    select document_id, event_date, source_platform, effective_label, confidence,
           'rating'      as signal_name, rating_score      as signal_score from scored
    union all
    select document_id, event_date, source_platform, effective_label, confidence,
           'aspect'      as signal_name, aspect_score      as signal_score from scored
    union all
    select document_id, event_date, source_platform, effective_label, confidence,
           'llm_judge'   as signal_name, llm_judge_score   as signal_score from scored

)

select
    document_id,
    event_date,
    source_platform,
    effective_label,
    confidence,
    signal_name,
    signal_score,
    {{ sentiment_bucket('signal_score') }} as signal_label
from unpivoted
where signal_score is not null
