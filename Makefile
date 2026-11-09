PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: dev lint format typecheck test build check clean

dev:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e '.[dev]'

lint:
	$(BIN)/ruff check src tests

format:
	$(BIN)/ruff format src tests

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest -v

build:
	rm -rf dist
	$(BIN)/python -m build

check: lint
	$(BIN)/ruff format --check src tests
	$(BIN)/mypy
	$(BIN)/pytest -v

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
