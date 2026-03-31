.DEFAULT_GOAL := default

.PHONY: default install lint test upgrade build clean docs docs-serve docs-deploy serve

default: install lint test

install:
	uv sync --all-extras

lint:
	uv run python devtools/lint.py

test:
	uv run pytest

upgrade:
	uv sync --upgrade --all-extras --dev

build:
	uv build

serve:
	uv run cointoss server start

clean:
	-rm -rf dist/
	-rm -rf *.egg-info/
	-rm -rf .pytest_cache/
	-rm -rf .mypy_cache/
	-rm -rf .venv/
	-rm -rf site/
	-find . -type d -name "__pycache__" -exec rm -rf {} +

docs:
	uv run --group docs mkdocs build

docs-serve:
	uv run --group docs mkdocs serve

docs-deploy:
	uv run --group docs mkdocs gh-deploy
