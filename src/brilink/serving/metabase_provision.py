"""Idempotent Metabase provisioning.

Runs after dbt and turns a blank Metabase into a finished dashboard: completes
first-run setup, registers the warehouse connection, syncs the schema, creates
every card from ``dashboard_spec`` and lays them out on a dashboard.

Idempotent throughout - re-running updates existing objects instead of
duplicating them, so it is safe to call on every deploy.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from typing import Any

from ..logging_config import configure_logging, get_logger
from ..settings import get_settings
from .dashboard_spec import CARDS, DASHBOARD_DESCRIPTION, DASHBOARD_PARAMETERS, FILTERS

logger = get_logger(__name__)

MART_SCHEMA = "analytics_marts"  # dbt: schema 'analytics' + custom schema 'marts'


class MetabaseClient:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._session_token: str | None = None

    # -- plumbing -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._session_token:
            headers["X-Metabase-Session"] = self._session_token
        return headers

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self._client.request(
            method, f"{self.base_url}{path}", headers=self._headers(), **kwargs
        )
        response.raise_for_status()
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: dict | None = None):
        return self.request("POST", path, json=payload or {})

    def put(self, path: str, payload: dict | None = None):
        return self.request("PUT", path, json=payload or {})

    # -- lifecycle ------------------------------------------------------
    def wait_until_ready(self, timeout_seconds: int) -> dict:
        deadline = time.time() + timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                return self.get("/api/session/properties")
            except Exception as exc:  # noqa: PERF203
                last_error = exc
                time.sleep(5)
        raise RuntimeError(f"Metabase never became ready: {last_error}")

    def ensure_session(self, email: str, password: str, site_name: str) -> None:
        properties = self.wait_until_ready(get_settings().metabase.setup_timeout_seconds)
        needs_setup = not properties.get("has-user-setup", False)

        if needs_setup:
            # Metabase keeps returning a setup-token in session properties even
            # after setup - only ``has-user-setup`` tells us whether it is fresh.
            setup_token = properties.get("setup-token")
            if not setup_token:
                raise RuntimeError("Metabase is fresh but issued no setup token")
            logger.info("Metabase is uninitialised - running first-run setup")
            result = self.post(
                "/api/setup",
                {
                    "token": setup_token,
                    "user": {
                        "email": email,
                        "password": password,
                        "first_name": "BRILink",
                        "last_name": "Analytics",
                        "site_name": site_name,
                    },
                    "prefs": {"site_name": site_name, "allow_tracking": False},
                },
            )
            self._session_token = result.get("id") if isinstance(result, dict) else None
            if self._session_token:
                return

        logger.info("Authenticating against existing Metabase instance")
        result = self.post("/api/session", {"username": email, "password": password})
        self._session_token = result["id"]

    def close(self) -> None:
        self._client.close()


def ensure_database(client: MetabaseClient, name: str) -> int:
    settings = get_settings()
    existing = client.get("/api/database")
    databases = existing.get("data", existing) if isinstance(existing, dict) else existing

    for database in databases or []:
        if database.get("name") == name:
            logger.info("Warehouse connection already present (id=%s)", database["id"])
            return database["id"]

    payload = {
        "name": name,
        "engine": "postgres",
        "details": {
            "host": settings.db.host,
            "port": settings.db.port,
            "dbname": settings.db.db,
            "user": settings.db.user,
            "password": settings.db.password,
            "ssl": False,
            "tunnel-enabled": False,
        },
        "is_full_sync": True,
    }
    created = client.post("/api/database", payload)
    database_id = created["id"]
    logger.info("Created warehouse connection (id=%s)", database_id)

    try:
        client.post(f"/api/database/{database_id}/sync_schema")
    except Exception:
        logger.warning("Schema sync request failed - Metabase will sync on its own")
    return database_id


def wait_for_tables(
    client: MetabaseClient, database_id: int, expected: set[str], timeout: int = 180
) -> None:
    """Metabase discovers tables asynchronously; cards fail if we race it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            metadata = client.get(f"/api/database/{database_id}/metadata")
            found = {t["name"] for t in metadata.get("tables", [])}
            if expected.issubset(found):
                logger.info("All %d expected marts visible to Metabase", len(expected))
                return
            missing = expected - found
            logger.info(
                "Waiting for Metabase to discover %d table(s): %s",
                len(missing),
                sorted(missing)[:4],
            )
        except Exception as exc:
            logger.debug("Metadata poll failed: %s", exc)
        with contextlib.suppress(Exception):
            client.post(f"/api/database/{database_id}/sync_schema")
        time.sleep(10)
    logger.warning("Proceeding without full table discovery - some cards may need a manual refresh")


def ensure_collection(client: MetabaseClient, name: str) -> int:
    for collection in client.get("/api/collection") or []:
        if collection.get("name") == name and not collection.get("archived"):
            return collection["id"]
    created = client.post(
        "/api/collection",
        {
            "name": name,
            "description": "Aset analitik sentimen BRILink (dikelola sebagai kode).",
            "color": "#0F4C81",
            "parent_id": None,
        },
    )
    logger.info("Created collection '%s' (id=%s)", name, created["id"])
    return created["id"]


def existing_cards(client: MetabaseClient, collection_id: int) -> dict[str, dict]:
    items = client.get(f"/api/collection/{collection_id}/items", params={"models": "card"})
    data = items.get("data", []) if isinstance(items, dict) else (items or [])
    return {item["name"]: item for item in data}


def native_query(spec: dict, schema: str) -> dict:
    """Build the ``native`` block for a card, including template tags.

    Each slug in ``spec["filters"]`` becomes a Metabase template tag that the
    dashboard parameters are mapped onto. The SQL keeps the optional ``[[...]]``
    syntax, so unset filters simply drop the clause.
    """
    card_schema = spec.get("schema", schema)
    # Use replace, not str.format: the SQL legitimately contains ``{{tag}}``
    # template syntax which str.format would collapse into ``{tag}``.
    native: dict = {"query": spec["sql"].replace("{schema}", card_schema).strip()}
    tags = {}
    for slug in spec.get("filters", []):
        tag = dict(FILTERS[slug])
        tag["name"] = slug
        tag["id"] = slug
        tags[slug] = tag
    if tags:
        native["template-tags"] = tags
    return native


def upsert_card(
    client: MetabaseClient,
    spec: dict,
    database_id: int,
    collection_id: int,
    schema: str,
    known: dict[str, dict],
) -> int:
    payload = {
        "name": spec["name"],
        "description": spec.get("description"),
        "display": spec.get("display", "table"),
        "collection_id": collection_id,
        "dataset_query": {
            "type": "native",
            "database": database_id,
            "native": native_query(spec, schema),
        },
        "visualization_settings": spec.get("visualization_settings", {}),
    }

    if spec["name"] in known:
        card_id = known[spec["name"]]["id"]
        client.put(f"/api/card/{card_id}", payload)
        logger.info("Updated card '%s' (id=%s)", spec["name"], card_id)
        return card_id

    created = client.post("/api/card", payload)
    logger.info("Created card '%s' (id=%s)", spec["name"], created["id"])
    return created["id"]


def archive_stale_cards(client: MetabaseClient, known: dict[str, dict], wanted: set[str]) -> None:
    """Archive cards that were renamed or removed from the spec.

    Cards are matched by name, so renaming a card in ``dashboard_spec`` would
    otherwise leave the old version orphaned in the collection.
    """
    for name, item in known.items():
        if name in wanted:
            continue
        try:
            client.put(f"/api/card/{item['id']}", {"archived": True})
            logger.info("Archived stale card '%s' (id=%s)", name, item["id"])
        except Exception as exc:
            logger.warning("Could not archive stale card '%s': %s", name, exc)


def ensure_dashboard(client: MetabaseClient, name: str, collection_id: int) -> int:
    items = client.get(f"/api/collection/{collection_id}/items", params={"models": "dashboard"})
    data = items.get("data", []) if isinstance(items, dict) else (items or [])
    for item in data:
        if item.get("name") == name:
            dashboard_id = item["id"]
            client.put(f"/api/dashboard/{dashboard_id}", {"parameters": DASHBOARD_PARAMETERS})
            logger.info("Updated dashboard '%s' parameters (id=%s)", name, dashboard_id)
            return dashboard_id
    created = client.post(
        "/api/dashboard",
        {
            "name": name,
            "description": DASHBOARD_DESCRIPTION,
            "collection_id": collection_id,
            "parameters": DASHBOARD_PARAMETERS,
        },
    )
    logger.info("Created dashboard '%s' (id=%s)", name, created["id"])
    return created["id"]


def lay_out_dashboard(client: MetabaseClient, dashboard_id: int, card_ids: dict[str, int]) -> None:
    dashcards = []
    for index, spec in enumerate(CARDS):
        card_id = card_ids.get(spec["key"])
        if card_id is None:
            continue
        position = spec["position"]
        mappings = [
            {
                "parameter_id": f"param_{slug}",
                "card_id": card_id,
                "target": ["variable", ["template-tag", slug]],
            }
            for slug in spec.get("filters", [])
        ]
        visualization: dict = {}
        click_target = spec.get("click_target")
        if click_target and card_ids.get(click_target):
            visualization["click_behavior"] = {
                "type": "link",
                "linkType": "question",
                "targetId": card_ids[click_target],
                "parameterMapping": {},
            }
        dashcards.append(
            {
                # Negative ids tell Metabase these are new cards to create.
                "id": -(index + 1),
                "card_id": card_id,
                "row": position["row"],
                "col": position["col"],
                "size_x": position["size_x"],
                "size_y": position["size_y"],
                "parameter_mappings": mappings,
                "visualization_settings": visualization,
            }
        )

    try:
        client.put(f"/api/dashboard/{dashboard_id}", {"dashcards": dashcards})
        logger.info("Placed %d cards on the dashboard", len(dashcards))
    except Exception as exc:
        logger.warning("Bulk layout failed (%s) - falling back to per-card placement", exc)
        for dashcard in dashcards:
            try:
                client.post(
                    f"/api/dashboard/{dashboard_id}/cards",
                    {
                        "cardId": dashcard["card_id"],
                        "row": dashcard["row"],
                        "col": dashcard["col"],
                        "size_x": dashcard["size_x"],
                        "size_y": dashcard["size_y"],
                    },
                )
            except Exception:
                logger.warning("Could not place card %s", dashcard["card_id"])


def provision(schema: str = MART_SCHEMA) -> dict:
    settings = get_settings().metabase
    client = MetabaseClient(settings.url)
    try:
        client.ensure_session(settings.admin_email, settings.admin_password, settings.site_name)
        database_id = ensure_database(client, settings.database_display_name)

        expected = {
            "mart_sentiment_daily",
            "mart_aspect_daily",
            "mart_review_queue",
            "mart_document_feed",
            "mart_engine_quality",
            "mart_sentiment_alerts",
            "mart_sentiment_by_source",
            "mart_aspect_source_matrix",
            "mart_pipeline_health",
            "stg_data_quality",
            "stg_ingestion_runs",
        }
        wait_for_tables(client, database_id, expected)

        collection_id = ensure_collection(client, settings.collection_name)
        known = existing_cards(client, collection_id)

        card_ids: dict[str, int] = {}
        for spec in CARDS:
            try:
                card_ids[spec["key"]] = upsert_card(
                    client, spec, database_id, collection_id, schema, known
                )
            except Exception as exc:
                logger.error("Card '%s' failed: %s", spec["name"], exc)

        archive_stale_cards(client, known, {spec["name"] for spec in CARDS})

        dashboard_id = ensure_dashboard(client, settings.dashboard_name, collection_id)
        lay_out_dashboard(client, dashboard_id, card_ids)

        summary = {
            "database_id": database_id,
            "collection_id": collection_id,
            "dashboard_id": dashboard_id,
            "cards": len(card_ids),
            "dashboard_url": f"{settings.url}/dashboard/{dashboard_id}",
        }
        logger.info("Metabase provisioning complete", extra=summary)
        return summary
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the Metabase dashboard")
    parser.add_argument("--schema", default=MART_SCHEMA, help="Schema holding the dbt marts")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    try:
        summary = provision(args.schema)
    except Exception:
        logger.exception("Metabase provisioning failed")
        return 1

    print(
        "\nDashboard siap: "
        f"{summary['dashboard_url']}\n"
        f"Login: {settings.metabase.admin_email} / {settings.metabase.admin_password}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
