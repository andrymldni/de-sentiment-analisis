{{ config(materialized='view') }}

with source as (

    select * from {{ source('core', 'document_aspect_sentiment') }}

),

dim as (

    select * from {{ source('core', 'dim_aspect') }}

)

select
    s.document_id,
    s.aspect_code,
    d.aspect_label,
    d.aspect_group,
    d.description        as aspect_description,
    s.mentions,
    s.sentiment_label    as aspect_label_sentiment,
    s.sentiment_score    as aspect_score,
    s.matched_keywords,
    s.evidence -> 'evidence'       as aspect_evidence,
    s.evidence ->> 'window_sample' as aspect_window_sample,
    s.scored_at
from source s
left join dim d on d.aspect_code = s.aspect_code
