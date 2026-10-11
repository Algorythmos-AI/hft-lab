# HFT Lab tasks. `make` (or `make help`) lists them.

SHELL := /bin/bash
PY ?= .venv/bin/python
export PYTHONPATH := src
export PYTHONDONTWRITEBYTECODE := 1

LOCK := uv pip compile --universal --python-version 3.12 --generate-hashes

.DEFAULT_GOAL := help
.PHONY: help bootstrap ml lock lint typecheck test leakcheck attest ci hooks

help: ## List the tasks
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

bootstrap: ## Create .venv from the hash-locked files (Python 3.12)
	uv venv --python 3.12 .venv
	uv pip install --require-hashes --python $(PY) -r requirements.lock.txt
	uv pip install --require-hashes --python $(PY) -r requirements-dev.lock.txt

ml: ## Add the optional ML set to .venv, hash-checked: gradient boosting, needed to train and by nothing else
	uv pip install --require-hashes --python $(PY) -r requirements-ml.lock.txt

lock: ## Recompile the lock files from the .in files, with hashes
	$(LOCK) requirements.in -o requirements.lock.txt
	$(LOCK) requirements-dev.in -o requirements-dev.lock.txt
	$(LOCK) requirements-ml.in -o requirements-ml.lock.txt

lint: ## Ruff (rules in pyproject.toml)
	$(PY) -m ruff check .

typecheck: ## mypy --strict on the whole hftlab package and tools/
	$(PY) -m mypy

test: ## Unit and property tests, then the research-registry lint
	$(PY) -m pytest
	$(PY) -m hftlab.registry lint

leakcheck: ## Scan the tracked tree and all history, then check HEAD's attestation (ADR 0006)
	$(PY) tools/leakcheck.py --all-history
	$(PY) tools/leakcheck.py --verify-attest

attest: ## Rewrite and stage the attestation by hand (the pre-commit hook normally does this)
	$(PY) tools/leakcheck.py --write-attest
	git add -- .leakcheck-attest.json

ci: lint typecheck test leakcheck ## Everything CI runs

hooks: ## Use the tracked hooks in .githooks (pre-commit, commit-msg, pre-push)
	git config core.hooksPath .githooks
