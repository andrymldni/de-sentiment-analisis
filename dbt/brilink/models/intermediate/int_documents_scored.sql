{{ config(materialized='view') }}

/*
    The single "one row per scored document" grain that every mart builds on.

    Two rules are enforced here, once, so no mart can disagree:
      * the *effective* label prefers a human adjudication over the model;
      * a document only counts toward headline KPIs when its confidence clears
        the configured floor. Low-confidence rows are kept - they are the
        review queue - but they are not allowed to move a headline number.
*/

with documents as (

    select * from {{ ref('stg_documents') }}

),

sentiment as (

    select * from {{ ref('stg_document_sentiment') }}

)

select
    d.document_id,
    d.doc_uid,
    d.source_platform,
    d.source_name,
    d.source_type,
    d.url,
    d.title,
    d.body,
    d.author,
    d.language,
    d.rating,
    d.engagement_score,
    d.text_length,
    d.relevance_score,
    d.is_synthetic,
    d.is_adversarial,
    d.intended_polarity,
    d.collector,
    d.event_at,
    d.event_date,
    d.event_week,
    d.event_month,
    d.published_at,
    d.ingested_at,
    d.used_ingested_at_fallback,

    s.sentiment_label,
    s.reviewed_label,
    s.effective_label,
    s.human_reviewed,
    s.sentiment_score,
    s.confidence,
    s.agreement,
    s.requires_review,
    s.review_reason,
    s.model_version,
    s.engine_mode,
    s.scored_at,
    s.transformer_score,
    s.emotion_score,
    s.rating_score,
    s.aspect_score,
    s.llm_judge_score,
    s.has_transformer_signal,
    s.has_emotion_signal,
    s.has_rating_signal,
    s.escalated_to_llm,
    s.transformer_entropy,
    s.dominant_emotion,
    s.signal_contributions,

    s.confidence >= {{ var('min_confidence_for_kpi') }}          as is_kpi_eligible,

    case when s.effective_label = 'positive' then 1 else 0 end   as is_positive,
    case when s.effective_label = 'negative' then 1 else 0 end   as is_negative,
    case when s.effective_label = 'neutral'  then 1 else 0 end   as is_neutral,

    -- Star rating vs model label: a free, continuous accuracy proxy on the
    -- subset of documents where the platform gives us a weak ground truth.
    case
        when d.rating is null then null
        when d.rating >= 4 then 'positive'
        when d.rating <= 2 then 'negative'
        else 'neutral'
    end                                                          as rating_implied_label,

    -- Same idea for the synthetic corpus, where the intended polarity is known.
    case
        when d.intended_polarity is null then null
        when d.intended_polarity = s.effective_label then true
        else false
    end                                                          as matches_intended_polarity

from documents d
inner join sentiment s on s.document_id = d.document_id
{% if not var('include_synthetic') %}
where not d.is_synthetic
{% endif %}
