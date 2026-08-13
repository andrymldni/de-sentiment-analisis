-- The stored aspect label must match the stored aspect score under the same
-- neutral band the engine used. Catches a Python/SQL threshold mismatch.
select
    document_id,
    aspect_code,
    aspect_score,
    aspect_label_sentiment
from {{ ref('stg_document_aspects') }}
where aspect_label_sentiment <> (
    case
        when aspect_score >  0.12 then 'positive'
        when aspect_score < -0.12 then 'negative'
        else 'neutral'
    end
)
