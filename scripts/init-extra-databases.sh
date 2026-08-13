#!/usr/bin/env bash
# Creates the side databases Airflow and Metabase keep their own state in.
# Running them in the same Postgres instance keeps the demo footprint small
# while still isolating application metadata from the analytical warehouse.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE airflow_meta OWNER $POSTGRES_USER;
    CREATE DATABASE metabase_app OWNER $POSTGRES_USER;
EOSQL

echo "Created airflow_meta and metabase_app databases"
