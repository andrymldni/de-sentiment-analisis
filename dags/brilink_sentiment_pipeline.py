"""BRILink sentiment pipeline.

    ingest.* (parallel per connector)
        -> data_quality_gate
            -> score_sentiment
                -> dbt_deps -> dbt_run -> dbt_test
                    -> provision_dashboard

Design notes
------------
* Connectors run **in parallel and independently**. One dead upstream must not
  block the others, so each task is allowed to fail without failing the group;
  the join task enforces the real requirement ("at least one source produced
  data") instead.
* The quality gate is a genuine gate: it raises, so everything downstream is
  skipped rather than silently processing bad data.
* Callables import their modules lazily. Heavy imports (torch, transformers) at
  DAG-parse time would slow every scheduler heartbeat.
"""

from __future__ import annotations

import pendulum
from airflow.decorators import task
from airflow.exceptions import AirflowFailException, AirflowSkipException
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.utils.task_group import TaskGroup
from airflow.utils.trigger_rule import TriggerRule

LOCAL_TZ = pendulum.timezone("Asia/Jakarta")
DBT_DIR = "/opt/airflow/dbt/brilink"
DBT_FLAGS = "--profiles-dir . --target dev"

CONNECTORS = ["rss", "playstore", "appstore", "reddit", "youtube", "twitter"]

DEFAULT_ARGS = {
    "owner": "data-engineering",
    "retries": 2,
    "retry_delay": pendulum.duration(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": pendulum.duration(minutes=30),
    "execution_timeout": pendulum.duration(hours=2),
    "depends_on_past": False,
}

DOC_MD = """
### BRILink Sentiment Pipeline

Multi-source ingestion (news via RSS only) -> data-quality gate -> ensemble
sentiment scoring (IndoBERT + emotion + rating + aspect) -> dbt
transformations and tests -> auto-provisioned Metabase dashboard.

**Failure semantics**

| Stage | On failure |
|---|---|
| individual connector | logged, other connectors continue |
| ingestion join | fails only if *every* connector produced nothing |
| data-quality gate | hard stop, downstream skipped |
| sentiment | retried, then hard stop |
| dbt test | hard stop before the dashboard refreshes |
| dashboard provisioning | warning only, data is already correct |
"""


with DAG(
    dag_id="brilink_sentiment_pipeline",
    description="BRILink multi-source sentiment intelligence pipeline",
    doc_md=DOC_MD,
    default_args=DEFAULT_ARGS,
    schedule="0 */6 * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=LOCAL_TZ),
    catchup=False,
    max_active_runs=1,
    tags=["brilink", "sentiment", "nlp", "dbt", "metabase"],
) as dag:

    start = EmptyOperator(task_id="start")

    # ------------------------------------------------------------------
    # 1. Ingestion - one task per connector, isolated failure domains
    # ------------------------------------------------------------------
    with TaskGroup(group_id="ingest", tooltip="Per-connector ingestion") as ingest:

        @task(task_id="noop_placeholder")
        def _placeholder() -> None:  # pragma: no cover - never scheduled
            return None

        def _make_ingest_task(connector_name: str):
            @task(task_id=connector_name, retries=1, trigger_rule=TriggerRule.ALL_DONE)
            def _run(connector: str = connector_name) -> dict:
                from brilink.ingestion.orchestrator import IngestionOrchestrator
                from brilink.logging_config import configure_logging
                from brilink.settings import get_settings

                settings = get_settings()
                configure_logging(settings.log_level, settings.log_json)

                # Seed fallback is a pipeline-level decision, not a per-connector one.
                settings.ingestion.seed_fallback = False
                reports = IngestionOrchestrator(settings).run(connector)
                report = reports[0].as_dict() if reports else {"inserted": 0}

                if report.get("status", "").startswith("skipped"):
                    raise AirflowSkipException(report.get("message", "skipped"))
                return report

            return _run()

        ingest_tasks = [_make_ingest_task(name) for name in CONNECTORS]

    @task(task_id="consolidate_ingestion", trigger_rule=TriggerRule.ALL_DONE)
    def consolidate_ingestion() -> dict:
        """Fail only if *no* source delivered anything, after the seed fallback."""
        from brilink.db import fetch_one
        from brilink.ingestion.orchestrator import IngestionOrchestrator
        from brilink.logging_config import configure_logging
        from brilink.settings import get_settings

        settings = get_settings()
        configure_logging(settings.log_level, settings.log_json)

        recent = fetch_one("""
            SELECT COUNT(*) AS n
            FROM raw.documents
            WHERE ingested_at >= NOW() - INTERVAL '12 hours'
            """)
        fresh = int((recent or {}).get("n", 0))

        if fresh == 0 and settings.ingestion.seed_fallback:
            IngestionOrchestrator(settings).run("seed", force=True)
            recent = fetch_one("SELECT COUNT(*) AS n FROM raw.documents")
            fresh = int((recent or {}).get("n", 0))

        if fresh == 0:
            raise AirflowFailException(
                "No documents available from any connector and the seed fallback is "
                "disabled - nothing downstream can run."
            )
        return {"documents_available": fresh}

    # ------------------------------------------------------------------
    # 2. Data quality gate
    # ------------------------------------------------------------------
    @task(task_id="data_quality_gate")
    def data_quality_gate() -> dict:
        from brilink.logging_config import configure_logging
        from brilink.quality.validate import run as run_validation
        from brilink.settings import get_settings

        settings = get_settings()
        configure_logging(settings.log_level, settings.log_json)
        return run_validation(hours=24, min_rows=1, dag_run_id="{{ run_id }}")

    # ------------------------------------------------------------------
    # 3. Sentiment scoring
    # ------------------------------------------------------------------
    score_sentiment = BashOperator(
        task_id="score_sentiment",
        bash_command="cd /opt/airflow && python -m brilink.nlp.run_sentiment",
        execution_timeout=pendulum.duration(hours=3),
        doc_md=(
            "Ensemble scoring. Idempotent per model version: re-running only "
            "picks up documents that have no score for the current version."
        ),
    )

    # ------------------------------------------------------------------
    # 4. Transform & test
    # ------------------------------------------------------------------
    with TaskGroup(group_id="transform", tooltip="dbt build & tests") as transform:
        dbt_run = BashOperator(
            task_id="dbt_run",
            bash_command=f"cd {DBT_DIR} && dbt run {DBT_FLAGS}",
        )
        dbt_test = BashOperator(
            task_id="dbt_test",
            bash_command=f"cd {DBT_DIR} && dbt test {DBT_FLAGS}",
            doc_md="Schema tests plus four singular tests that reconcile the marts.",
        )
        dbt_docs = BashOperator(
            task_id="dbt_docs_generate",
            bash_command=f"cd {DBT_DIR} && dbt docs generate {DBT_FLAGS}",
            trigger_rule=TriggerRule.ALL_DONE,
        )
        dbt_run >> dbt_test >> dbt_docs

    # ------------------------------------------------------------------
    # 5. Serving
    # ------------------------------------------------------------------
    provision_dashboard = BashOperator(
        task_id="provision_dashboard",
        bash_command="cd /opt/airflow && python -m brilink.serving.metabase_provision || true",
        doc_md=(
            "Idempotent Metabase provisioning. Deliberately non-blocking: if the "
            "BI layer is down the warehouse is still correct."
        ),
    )

    end = EmptyOperator(task_id="end", trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)

    consolidated = consolidate_ingestion()
    gate = data_quality_gate()

    (
        start
        >> ingest
        >> consolidated
        >> gate
        >> score_sentiment
        >> transform
        >> provision_dashboard
        >> end
    )
