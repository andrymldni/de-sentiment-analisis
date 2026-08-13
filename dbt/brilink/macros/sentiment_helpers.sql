{#
    Shared sentiment SQL fragments.
    Centralising these guarantees the neutral band and the "effective label"
    rule are identical in every mart - the classic place where dashboards
    silently disagree with each other.
#}

{% macro sentiment_bucket(score_column, band=0.12) %}
    case
        when {{ score_column }} >  {{ band }} then 'positive'
        when {{ score_column }} < -{{ band }} then 'negative'
        else 'neutral'
    end
{% endmacro %}


{% macro effective_label(model_label='sentiment_label', human_label='reviewed_label') %}
    coalesce({{ human_label }}, {{ model_label }})
{% endmacro %}


{% macro net_sentiment_score(positive_col, negative_col, total_col) %}
    round(
        ({{ positive_col }} - {{ negative_col }})::numeric
        / nullif({{ total_col }}, 0) * 100
    , 2)
{% endmacro %}


{% macro safe_divide(numerator, denominator, precision=4) %}
    round(({{ numerator }})::numeric / nullif({{ denominator }}, 0), {{ precision }})
{% endmacro %}
