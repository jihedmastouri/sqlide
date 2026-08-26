# Development entry points. `make` on its own lists them.
#
# One interpreter, built two ways:
#
#   * GTK, libadwaita and PyGObject are system packages, so $(VENV) is
#     created on the system python ($(PYTHON)) with --system-site-packages
#     — that is the only way `import gi` works from a venv;
#   * the database drivers and pytest are installed into that venv.
#
# So everything — the app, the tests, the seeding script — runs on
# $(BIN)/python. Running the app on $(PYTHON) directly would find GTK
# but none of the drivers.

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
UV := $(shell command -v uv 2>/dev/null)

# The servers `make servers` starts: one recent version of each engine.
SERVERS ?= postgres16 mysql8
DEMO_DB ?= demo.db

.DEFAULT_GOAL := help
.PHONY: help venv install run run-fresh demo test test-sqlite lint fmt check \
        servers servers-all servers-stop servers-clean init-db \
        flatpak web clean

help:  ## List the targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	@echo "→ creating $(VENV) (with system site packages, for PyGObject)"
ifeq ($(UV),)
	$(PYTHON) -m venv --system-site-packages $(VENV)
else
	uv venv --system-site-packages --python $(PYTHON) $(VENV)
endif

venv: $(BIN)/python  ## Create the virtualenv

install: venv  ## Install sqlide with its drivers and test extras
ifeq ($(UV),)
	$(BIN)/python -m pip install -e ".[test,all]"
else
	uv pip install --python $(BIN)/python -e ".[test,all]"
endif

run: venv  ## Launch the app (venv python: GTK via system site packages)
	$(BIN)/python -m sqlide

# Startup goes straight into the last-used workspace, so the only way
# back to a first run is a config directory that has never seen one.
# SCRATCH_CONFIG defaults to a fresh mktemp dir per run; point it at a
# fixed path to keep a throwaway profile between runs.
run-fresh: venv  ## Launch with a throwaway config (a real first run)
	@dir="$${SCRATCH_CONFIG:-$$(mktemp -d -t sqlide-config-XXXXXX)}"; \
	echo "→ XDG_CONFIG_HOME=$$dir"; \
	XDG_CONFIG_HOME="$$dir" $(BIN)/python -m sqlide

demo:  ## Build the SQLite demo database (demo.db)
	$(PYTHON) scripts/make_demo_db.py $(DEMO_DB)

check: venv  ## Compile every module and import the GTK entry point
	$(BIN)/python -m compileall -q sqlide
	$(BIN)/python -c "import sqlide.frontend.application"

test: venv  ## Run the tests (server tests skip when nothing is up)
	$(BIN)/python -m pytest

test-sqlite: venv  ## Run only the tests that need no database server
	$(BIN)/python -m pytest -k "not postgres and not mysql"

lint:  ## Ruff, read-only
	ruff check sqlide tests scripts

fmt:  ## Ruff's fixable complaints, applied
	ruff check --fix sqlide tests scripts

servers:  ## Start one server per engine (postgres16, mysql8)
	docker compose up -d --wait $(SERVERS)

servers-all:  ## Start every server version in docker-compose.yml
	docker compose up -d --wait

servers-stop:  ## Stop the servers, keeping their data
	docker compose stop

servers-clean:  ## Stop the servers and delete their data
	docker compose down -v

init-db: venv  ## Rebuild the demo database on every running server
	$(BIN)/python scripts/init_databases.py --drop

flatpak:  ## Build the Flatpak from build-aux/
	flatpak-builder --force-clean build-dir \
		build-aux/flatpak/dev.jihed.sqlide.yml

web:  ## Serve the docs site from web/
	cd web && npm install && npm run dev

clean:  ## Remove build, cache and demo artefacts
	rm -rf build-dir .pytest_cache .ruff_cache $(DEMO_DB)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
