# Mizan core — developer entry points.
# Every recipe is a plain `python -m ...` command so it runs identically from
# PowerShell, cmd, Git Bash and CI. If you have no `make`, run the commands directly.
#
#   make install            editable install with dev + advisory extras, plus the git hook
#   make lint               ruff
#   make typecheck          mypy over mizan/
#   make test               everything except invariants / security / integration
#   make test-contracts     contract conformance (L0)
#   make test-invariants    Hard Rule invariants (red-until-implemented is VISIBLE, never hidden)
#   make test-security      security suite (L5)
#   make test-integration   integration suite (L6)
#   make test-all           every suite
#   make secret-scan        tracked + untracked files
#   make secret-scan-history  every commit on every branch
#   make verify-chain ARGS=<ledger.sqlite|records.jsonl>
#   make compose-up / compose-down   local Postgres (requires POSTGRES_PASSWORD in .env)
#   make ci                 what CI runs, in CI order

PYTHON ?= python
ARGS   ?=
PYTEST  = $(PYTHON) -m pytest -p no:cacheprovider -q
# Run a suite only if its directory exists. tests/integration (L6) and tests/security (L5)
# arrive in later sprints; until then `pytest <missing dir>` exits 4 and would break `make ci`
# for a suite nobody has written yet. CI prints the same warning and carries on. No shell
# built-ins, so this behaves identically under sh, cmd and PowerShell.
PYTEST_IF = $(PYTHON) -c "import pathlib,subprocess,sys; d=sys.argv[1]; sys.exit(subprocess.call([sys.executable,'-m','pytest','-p','no:cacheprovider','-q',d]) if pathlib.Path(d).is_dir() else print('skipped: '+d+' does not exist yet'))"

.PHONY: install hooks lint typecheck test test-contracts test-invariants test-security test-integration \
	    test-all secret-scan secret-scan-history verify-chain compose-up compose-down ci

install:
	$(PYTHON) -m pip install -e ".[dev,advisory]"
	$(PYTHON) scripts/install_hooks.py

hooks:
	$(PYTHON) scripts/install_hooks.py

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTEST) tests --ignore=tests/invariants --ignore=tests/security --ignore=tests/integration

test-contracts:
	$(PYTEST) tests/contracts

test-invariants:
	$(PYTEST) tests/invariants

test-security:
	$(PYTEST_IF) tests/security

test-integration:
	$(PYTEST_IF) tests/integration

test-all:
	$(PYTEST) tests

secret-scan:
	$(PYTHON) scripts/secret_scan.py --self-test
	$(PYTHON) scripts/secret_scan.py --all

secret-scan-history:
	$(PYTHON) scripts/secret_scan.py --history

verify-chain:
	$(PYTHON) -m mizan.audit.verify_chain $(ARGS)

compose-up:
	docker compose up -d --wait

compose-down:
	docker compose down

ci: lint secret-scan secret-scan-history test-contracts test test-security test-integration test-invariants
