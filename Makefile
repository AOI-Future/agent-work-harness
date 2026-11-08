PY ?= .venv/bin/python
RUFF ?= /Users/shugo/.local/bin/ruff
TYPE_PY ?= /Users/shugo/project/aoi-system-role-persistence-rebuild/.venv/bin/python

.PHONY: verify test lint typecheck demo replay ci help
verify: lint typecheck test

lint:
	$(RUFF) check harness scripts tests
	$(RUFF) format --check harness scripts tests
	$(PY) scripts/validate_structure.py

typecheck:
	PYTHONPATH=.tools $(TYPE_PY) -m mypy harness scripts --exclude harness/vendor

test:
	$(PY) -m unittest discover -s tests -v

demo:
	$(PY) -m scripts.demo

replay:
	$(PY) -m scripts.replay_suite

ci: verify replay

help:
	$(PY) -m harness.cli --help

.venv/bin/python:
	python3 -m venv .venv
	./.venv/bin/python -m pip install -q pydantic pytest
