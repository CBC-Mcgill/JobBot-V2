.PHONY: install lint test check-config

install:
	python -m pip install -e '.[dev]'

lint:
	python -m ruff check jobbot tests

test:
	python -m pytest

check-config:
	python -m jobbot check-config
