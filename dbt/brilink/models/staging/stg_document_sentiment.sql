{{ config(materialized='view') }}

/*
    Engine output, flattened. The JSONB signal payload is unpacked into typed
    columns here so BI users never have to write JSON path expressions.
*/

with source as (

    select * from {{ source('core', 'document_sentiment') }}

)

select
    document_id,
    sentiment_label,
    reviewed_label,
    {{ effective_label() }}                                    as effective_label,
    reviewed_label is not null                                 as human_reviewed,
    sentiment_score,
    confidence,
    agreement,
    requires_review,
    review_reason,
    model_version,
    engine_mode,
    scored_at,

    -- Per-signal scores, typed for direct use in BI.
    (signals -> 'transformer' ->> 'score')::numeric            as transformer_score,
    (signals -> 'emotion'     ->> 'score')::numeric            as emotion_score,
    (signals -> 'rating'      ->> 'score')::numeric            as rating_score,
    (signals -> 'aspect'      ->> 'score')::numeric            as aspect_score,
    (signals -> 'llm_judge'   ->> 'score')::numeric            as llm_judge_score,

    coalesce((signals -> 'transformer' ->> 'available')::boolean, false) as has_transformer_signal,
    coalesce((signals -> 'emotion'     ->> 'available')::boolean, false) as has_emotion_signal,
    coalesce((signals -> 'rating'      ->> 'available')::boolean, false) as has_rating_signal,
    coalesce((signals -> 'llm_judge'   ->> 'available')::boolean, false) as escalated_to_llm,

    (signals -> 'transformer' ->> 'normalized_entropy')::numeric as transformer_entropy,
    (signals -> 'emotion'     ->> 'dominant_emotion')            as dominant_emotion,

    explanation -> 'contributions'                             as signal_contributions,
    signals                                                    as signals_raw

from source
