# PerfBench developer entry points.
#
# `make test` uses the bundled zero-dependency coverage runner (scripts/run_tests.py)
# so the suite is verifiable on machines without pytest. `make pytest` runs the
# same suite under pytest/pytest-cov when the dev extras are installed.

PY ?= python3

.PHONY: test pytest coverage validate clean

test:
	$(PY) scripts/run_tests.py --fail-under 90

coverage: test

pytest:
	$(PY) -m pytest --cov=perfbench --cov-fail-under=90

validate:
	PYTHONPATH=src $(PY) -m perfbench.cli validate scenarios/

clean:
	rm -rf .coverage htmlcov __pycache__ src/perfbench/__pycache__ build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
