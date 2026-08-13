-- The daily mart must reconcile with its own source grain. Any drift means a
-- filter was added in one place and not the other.
with mart_total as (
    select coalesce(sum(document_count), 0) as n from {{ ref('mart_sentiment_daily') }}
),
source_total as (
    select count(*) as n from {{ ref('int_documents_scored') }} where is_kpi_eligible
)
select m.n as mart_count, s.n as source_count
from mart_total m
cross join source_total s
where m.n <> s.n
