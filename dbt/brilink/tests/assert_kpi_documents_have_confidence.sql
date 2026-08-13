-- A KPI-eligible document must clear the configured confidence floor.
-- If this ever returns rows, the confidence rule has drifted between models.
select document_id, confidence
from {{ ref('int_documents_scored') }}
where is_kpi_eligible
  and confidence < {{ var('min_confidence_for_kpi') }}
