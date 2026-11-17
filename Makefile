PYTHON ?= python3
.DEFAULT_GOAL := build
export PYTHONPATH := $(CURDIR)/src:$(CURDIR)

.PHONY: bootstrap lean proof-audit check test build clean serve

bootstrap:
	$(PYTHON) scripts/bootstrap.py

lean:
	$(PYTHON) scripts/lean_tool.py lake build

proof-audit: lean
	$(PYTHON) scripts/audit_proofs.py

check: proof-audit
	$(PYTHON) -m lawbridge check examples/contracts --report .lawbridge/report.json
	$(PYTHON) -m splitexpense.laws --report .lawbridge/splitexpense-laws.json

test: lean
	$(PYTHON) -m unittest discover -s tests -v

build: check test

PORT ?= 8791
serve:
	$(PYTHON) -m splitexpense --host 127.0.0.1 --port $(PORT) --db .lawbridge/splitexpense.sqlite3

clean:
	$(PYTHON) -c "import shutil; shutil.rmtree('.lawbridge', ignore_errors=True)"
