{{ config(materialized='table') }}

/*
    The human-in-the-loop backlog, prioritised.

    Priority is not just "lowest confidence": a low-confidence document nobody
    reads matters less than a low-confidence document on a high-engagement post
    about fraud. The score below blends uncertainty, reach and aspect severity.
*/

with candidates as (

    select
        d.*,
        a.worst_aspect_code,
        a.worst_aspect_score
    from {{ ref('int_documents_scored') }} d
    left join (
        select
            document_id,
            (array_agg(aspect_code order by aspect_score asc))[1] as worst_aspect_code,
            min(aspect_score)                                     as worst_aspect_score
        from {{ ref('stg_document_aspects') }}
        group by document_id
    ) a on a.document_id = d.document_id
    where d.requires_review
      and d.reviewed_label is null

)

select
    document_id,
    event_at,
    event_date,
    source_platform,
    source_name,
    url,
    coalesce(title, left(body, 180))              as headline,
    left(coalesce(body, ''), 800)                 as excerpt,
    sentiment_label,
    sentiment_score,
    confidence,
    agreement,
    review_reason,
    engagement_score,
    worst_aspect_code,
    worst_aspect_score,
    signal_contributions,

    round(
        (1 - confidence) * 0.5
        + (1 - agreement) * 0.2
        + least(engagement_score / 100.0, 1.0) * 0.15
        + case when worst_aspect_code in ('keamanan_fraud', 'dana_transaksi')
               then 0.15 else 0 end
    , 4)                                          as review_priority,

    case
        when review_reason = 'signal_conflict'      then 'Sinyal saling bertentangan'
        when review_reason = 'low_confidence'       then 'Keyakinan model rendah'
        when review_reason = 'borderline_label'     then 'Skor di ambang batas netral'
        when review_reason = 'insufficient_signals' then 'Bukti tidak cukup'
        else 'Perlu ditinjau'
    end                                           as review_reason_id

from candidates
order by review_priority desc
