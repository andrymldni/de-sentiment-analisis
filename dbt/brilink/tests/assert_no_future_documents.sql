-- Documents dated in the future indicate a timezone or parsing bug upstream.
select document_id, event_at
from {{ ref('stg_documents') }}
where event_at > now() + interval '24 hours'
