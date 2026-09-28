# =====================================================================
# BRILink Sentiment Platform
# =====================================================================
SHELL := /bin/bash
COMPOSE := docker compose
DBT := cd dbt/brilink && dbt

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- environment -----------------------------------------------------
.PHONY: env
env: ## Create .env from the template
	@test -f .env || (cp .env.example .env && echo "Created .env")

.PHONY: build
build: env ## Build the application image
	$(COMPOSE) build

.PHONY: build-light
build-light: env ## Build without torch/transformers (smoke test only, sentiment degraded)
	INSTALL_TORCH=false $(COMPOSE) build

.PHONY: up
up: env ## Start the full stack
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  Airflow  -> http://localhost:8080  (admin / admin)"
	@echo "  Metabase -> http://localhost:3000"
	@echo ""

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs from every service
	$(COMPOSE) logs -f --tail=100

# --- pipeline --------------------------------------------------------
.PHONY: demo
demo: up ## Run the whole pipeline once, end to end
	$(COMPOSE) --profile tools run --rm --build pipeline-runner

.PHONY: ingest
ingest: ## Run ingestion only (make ingest CONNECTORS=rss,playstore)
	$(COMPOSE) --profile tools run --rm --build pipeline-runner \
	  python -m brilink.ingestion.run_ingestion --force $(if $(CONNECTORS),--connectors $(CONNECTORS),)

.PHONY: seed
seed: ## Load the synthetic demo corpus
	$(COMPOSE) --profile tools run --rm --build pipeline-runner \
	  python -m brilink.ingestion.run_ingestion --connectors seed --force

.PHONY: score
score: ## Score unscored documents
	$(COMPOSE) --profile tools run --rm --build pipeline-runner python -m brilink.nlp.run_sentiment

.PHONY: rejudge
rejudge: ## Send the existing review queue to the LLM judge (needs SENTIMENT_ENABLE_LLM_JUDGE=true)
	$(COMPOSE) --profile tools run --rm --build pipeline-runner python -m brilink.nlp.run_sentiment --rejudge-review

.PHONY: validate
validate: ## Run the data quality gate
	$(COMPOSE) --profile tools run --rm --build pipeline-runner python -m brilink.quality.validate --hours 168

.PHONY: dbt
dbt: ## dbt deps + run + test
	$(COMPOSE) --profile tools run --rm --build pipeline-runner bash -lc \
	  "cd /opt/airflow/dbt/brilink && dbt run --profiles-dir . && dbt test --profiles-dir ."

.PHONY: dashboard
dashboard: ## Provision the Metabase dashboard
	$(COMPOSE) --profile tools run --rm --build pipeline-runner python -m brilink.serving.metabase_provision

.PHONY: export
export: ## Export analysed data to CSV in ./output (make export DAYS=30 PLATFORM=news)
	$(COMPOSE) --profile tools run --rm --build pipeline-runner \
	  python -m brilink.serving.export_csv --output-dir /opt/airflow/output \
	  $(if $(DAYS),--days $(DAYS),) $(if $(PLATFORM),--platform $(PLATFORM),)

# --- development -----------------------------------------------------
.PHONY: install
install: ## Install the package and dev dependencies locally
	python -m pip install -U pip
	python -m pip install -r requirements/dev.txt -r requirements/nlp.txt
	python -m pip install -e .

.PHONY: test
test: ## Run the unit test suite
	python -m pytest -v

.PHONY: cov
cov: ## Run tests with a coverage report
	python -m pytest --cov=brilink --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## Lint and type-check
	python -m ruff check src tests dags
	python -m black --check src tests
	python -m mypy src --ignore-missing-imports || true

.PHONY: format
format: ## Auto-format the codebase
	python -m ruff check --fix src tests dags
	python -m black src tests dags

.PHONY: bench
bench: ## Score the built-in gold cases and print an accuracy report
	python -m tests.gold_report
