{#
    Generic tests implemented locally rather than pulled from dbt_utils.

    Rationale: these are the only two dbt_utils tests this project needs, and
    vendoring them removes a network fetch (`dbt deps` against hub.getdbt.com)
    from the critical path of every build, every CI run and every air-gapped
    deployment. The semantics match dbt_utils so the yml syntax is unchanged.
#}

{% test accepted_range(model, column_name, min_value=none, max_value=none,
                       inclusive=true, where=none) %}

    with validation as (
        select {{ column_name }} as value_field
        from {{ model }}
        where {{ column_name }} is not null
        {% if where %} and ({{ where }}) {% endif %}
    )

    select value_field
    from validation
    where
        1 = 0
        {% if min_value is not none %}
            {% if inclusive %} or value_field < {{ min_value }}
            {% else %}        or value_field <= {{ min_value }}
            {% endif %}
        {% endif %}
        {% if max_value is not none %}
            {% if inclusive %} or value_field > {{ max_value }}
            {% else %}        or value_field >= {{ max_value }}
            {% endif %}
        {% endif %}

{% endtest %}


{% test unique_combination_of_columns(model, combination_of_columns, where=none) %}

    {%- set column_list = combination_of_columns | join(', ') -%}

    select
        {{ column_list }},
        count(*) as duplicate_rows
    from {{ model }}
    {% if where %} where {{ where }} {% endif %}
    group by {{ column_list }}
    having count(*) > 1

{% endtest %}


{% test not_constant(model, column_name) %}

    {#- A column that never varies is usually a broken join or a stuck default. -#}
    select count(distinct {{ column_name }}) as distinct_values
    from {{ model }}
    having count(distinct {{ column_name }}) = 1

{% endtest %}
