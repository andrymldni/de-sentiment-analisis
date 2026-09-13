"""Central, typed configuration for the BRILink sentiment platform.

Every knob is driven by environment variables so the same image runs
unchanged in local Docker Compose, CI and GCP.  Defaults are chosen so the
project boots with zero configuration (demo-friendly) while still allowing a
fully production-shaped setup.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

EngineMode = Literal["ensemble", "transformer_only"]


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host: str = "postgres"
    port: int = 5432
    db: str = "brilinkdb"
    user: str = "brilink"
    password: str = "brilink"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dsn(self) -> str:
        """SQLAlchemy URL.

        A host beginning with "/" is a Unix socket directory, not a hostname -
        the form Cloud SQL uses when a service connects over its socket mount.
        It has to move into the query string, because a bare path in the
        authority section is not a valid URL.
        """
        user = quote_plus(self.user)
        password = quote_plus(self.password) if self.password else ""
        credentials = f"{user}:{password}" if password else user

        if self.host.startswith("/"):
            return (
                f"postgresql+psycopg2://{credentials}@/{self.db}" f"?host={quote_plus(self.host)}"
            )
        return f"postgresql+psycopg2://{credentials}@{self.host}:{self.port}/{self.db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def psycopg_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.db,
            "user": self.user,
            "password": self.password,
        }


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    host: str = "redis"
    port: int = 6379
    db: int = 0
    enabled: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "db": self.db,
            "decode_responses": True,
            "socket_connect_timeout": 3,
        }


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGEST_", extra="ignore")

    # Comma separated list of connectors to run. "all" expands to the registry.
    connectors: str = "all"
    # Query terms shared across every text connector.
    keywords: str = (
        "brilink,agen brilink,agen bri,laku pandai bri,bri link,"
        "branchless banking bri,agen laku pandai"
    )
    lookback_days: int = 14
    initial_lookback_days: int = 90
    max_items_per_connector: int = 400
    # Minimum gap (minutes) before the same connector may run again.
    cooldown_minutes: int = 90
    # Politeness delay between outbound HTTP calls, seconds.
    request_delay_seconds: float = 1.5
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    # If a live connector yields nothing, backfill with the synthetic corpus so
    # the dashboard is never empty during a demo.
    seed_fallback: bool = True
    seed_documents: int = 2400
    seed_days: int = 180
    seed_random_state: int = 20260806
    user_agent: str = (
        "brilink-sentiment-platform/2.0 (portfolio data-engineering project; "
        "contact: data@example.com)"
    )


class CredentialSettings(BaseSettings):
    """Optional third-party credentials. Missing values disable the connector
    gracefully instead of raising - the pipeline degrades, it does not break."""

    model_config = SettingsConfigDict(extra="ignore")

    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "brilink-sentiment/2.0"
    youtube_api_key: str | None = None
    twitter_bearer_token: str | None = None
    google_maps_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None


class SentimentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTIMENT_", extra="ignore")

    engine_mode: EngineMode = "ensemble"
    transformer_model: str = "mdhugol/indonesia-bert-sentiment-classification"
    emotion_model: str = "StevenLimcorn/indonesian-roberta-base-emotion-classifier"
    enable_transformer: bool = True
    enable_emotion: bool = True
    enable_llm_judge: bool = False
    batch_size: int = 16
    max_tokens: int = 384
    # Sliding-window chunking for long articles.
    chunk_words: int = 180
    chunk_overlap_words: int = 40
    max_chunks: int = 6
    # Neutral dead-band: |score| below this is reported as neutral.
    neutral_band: float = 0.12
    # Documents scoring below this confidence go to the human review queue.
    review_confidence_threshold: float = 0.55
    scoring_limit: int = 5000
    model_version: str = "brilink-ensemble-v2.0.0"


class MetabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="METABASE_", extra="ignore")

    url: str = "http://metabase:3000"
    admin_email: str = "admin@brilink.local"
    admin_password: str = "BrilinkDemo123!"
    site_name: str = "BRILink Sentiment Intelligence"
    database_display_name: str = "BRILink Warehouse"
    collection_name: str = "BRILink Sentiment"
    dashboard_name: str = "BRILink Sentiment Intelligence"
    setup_timeout_seconds: int = 300


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    environment: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_json: bool = Field(default=False, alias="LOG_JSON")

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)
    sentiment: SentimentSettings = Field(default_factory=SentimentSettings)
    metabase: MetabaseSettings = Field(default_factory=MetabaseSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
