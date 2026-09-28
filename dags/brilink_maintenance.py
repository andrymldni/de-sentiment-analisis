"""Weekly maintenance: rescore drift, prune operational logs, refresh docs.

Kept separate from the main pipeline because its failure modes and its cadence
are different - maintenance must never be able to break the hourly product.
"""

from __future__ import annotations

import pendulum
from airflow.decorators import task
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

LOCAL_TZ = pendulum.timezone("Asia/Jakarta")

with DAG(
    dag_id="brilink_maintenance",
    description="Weekly warehouse hygiene and model-drift rescoring",
    default_args={"owner": "data-engineering", "retries": 1},
    schedule="0 2 * * 0",
    start_date=pendulum.datetime(2026, 1, 1, tz=LOCAL_TZ),
    catchup=False,
    tags=["brilink", "maintenance"],
) as dag:

    @task(task_id="prune_operational_logs")
    def prune_operational_logs() -> dict:
        """Ingestion runs and DQ results are observability data, not history."""
        from brilink.db import execute
        from brilink.logging_config import configure_logging

        configure_logging()
        runs = execute(
            "DELETE FROM raw.ingestion_runs WHERE started_at < NOW() - INTERVAL '90 days'"
        )
        quality = execute(
            "DELETE FROM ops.data_quality_results WHERE checked_at < NOW() - INTERVAL '90 days'"
        )
        return {"ingestion_runs_deleted": runs, "quality_results_deleted": quality}

    @task(task_id="report_unscored_backlog")
    def report_unscored_backlog() -> dict:
        from brilink.db import fetch_one
        from brilink.settings import get_settings

        version = get_settings().sentiment.model_version
        row = fetch_one(
            """
            SELECT COUNT(*) AS n
            FROM raw.documents d
            LEFT JOIN core.document_sentiment s
                   ON s.document_id = d.document_id AND s.model_version = %s
            WHERE s.document_id IS NULL
            """,
            (version,),
        )
        return {"model_version": version, "unscored": int((row or {}).get("n", 0))}

    # One -c per statement: psql sends a multi-statement -c string as a single
    # query, which the server runs as an implicit transaction block, and VACUUM
    # refuses to run inside one ("VACUUM cannot run inside a transaction block").
    vacuum = BashOperator(
        task_id="vacuum_analyze",
        bash_command=(
            'psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@$POSTGRES_HOST:'
            '$POSTGRES_PORT/$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            '-c "VACUUM ANALYZE raw.documents" '
            '-c "VACUUM ANALYZE core.document_sentiment"'
        ),
    )

    rebuild_marts = BashOperator(
        task_id="full_refresh_marts",
        bash_command=(
            "cd /opt/airflow/dbt/brilink && "
            "dbt run --profiles-dir . --full-refresh --select marts"
        ),
    )

    prune_operational_logs() >> report_unscored_backlog() >> vacuum >> rebuild_marts
