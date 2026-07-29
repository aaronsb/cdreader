# cdripper development tasks.
#
# Quick start:  make dev && make test
# Everything runs inside .venv -- no global installs, no sudo (except `make deps`).
#
# PY and RUFF can be overridden to use an interpreter that is already on PATH,
# which is how CI runs these targets:  make test PY=python

VENV    := .venv
PY       = $(VENV)/bin/python
RUFF     = $(VENV)/bin/ruff
PYTEST   = $(PY) -m pytest
UV      := $(shell command -v uv 2>/dev/null)

# Version comes from the latest git tag via setuptools-scm.
CURRENT_VERSION := $(shell git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)

.DEFAULT_GOAL := help
.PHONY: help require-dev dev deps test test-cov lint fmt check run once clean distclean build tag

help: ## Show this help
	@echo "cdripper -- development tasks"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Current version: $(CURRENT_VERSION)"

# Fail with a useful message rather than a bare exit 127.
require-dev:
	@command -v $(PY) >/dev/null 2>&1 \
		|| { echo "No Python environment found. Run 'make dev' first."; exit 1; }
	@command -v $(RUFF) >/dev/null 2>&1 \
		|| { echo "Dev tools not installed. Run 'make dev' first."; exit 1; }

$(VENV):
ifdef UV
	$(UV) venv $(VENV)
else
	@python3 -m venv $(VENV) || { \
		echo ""; \
		echo "Could not create a virtualenv. Either install uv:"; \
		echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		echo "or install your distro's venv package, e.g.:"; \
		echo "    sudo apt install python3-venv"; \
		exit 1; }
endif

dev: $(VENV) ## Create venv and install the package + dev tools (editable)
ifdef UV
	$(UV) pip install --python $(PY) -e ".[dev]"
else
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
endif
	@echo "Ready. Run 'make test'."

deps: ## Install the system packages cdripper shells out to (needs sudo)
	@if   command -v pacman >/dev/null; then sudo pacman  -S --needed cdparanoia flac libdiscid eject; \
	 elif command -v apt-get >/dev/null; then sudo apt-get install -y cdparanoia flac libdiscid0 eject; \
	 elif command -v dnf    >/dev/null; then sudo dnf install -y cdparanoia flac libdiscid eject; \
	 else echo "Unknown package manager -- install cdparanoia, flac, libdiscid, eject by hand."; exit 1; fi

test: require-dev ## Run the unit tests
	$(PYTEST) tests/ -q

test-cov: require-dev ## Run tests with a coverage report
	$(PYTEST) tests/ --cov=cdripper --cov-report=term-missing

lint: require-dev ## Check style and common errors
	$(RUFF) check src/ tests/

fmt: require-dev ## Auto-fix what ruff can fix
	$(RUFF) check --fix src/ tests/

# Deliberately has no require-dev guard: this is the target you run *because*
# something is broken, so every probe degrades to a message instead of failing.
check: ## Verify the runtime environment (system binaries + python imports)
	@echo "System binaries:"
	@for b in cdparanoia flac eject; do \
		printf "  %-12s " "$$b"; \
		command -v $$b >/dev/null && echo "ok" || echo "MISSING -- run 'make deps'"; \
	done
	@printf "Python env:    "
	@command -v $(PY) >/dev/null 2>&1 && echo "ok ($(PY))" || echo "MISSING -- run 'make dev'"
	@echo "Python imports:"
	@command -v $(PY) >/dev/null 2>&1 \
		&& { $(PY) -c "import discid, musicbrainzngs, mutagen, rich; print('  all ok')" \
			|| echo "  FAILED -- run 'make dev' (and 'make deps' if libdiscid is missing)"; } \
		|| echo "  skipped (no python env)"
	@echo "Optical drives:"
	@command -v $(PY) >/dev/null 2>&1 \
		&& { $(PY) -c "import cdripper; d = cdripper.detect_drives(); \
			print('  ' + (', '.join(d) if d else 'none detected'))" \
			|| echo "  unknown -- cdripper is not importable"; } \
		|| echo "  skipped (no python env)"

run: require-dev ## Run cdripper against all detected drives
	$(VENV)/bin/cdripper

once: require-dev ## Rip a single disc and exit
	$(VENV)/bin/cdripper --once

build: clean ## Build the wheel and sdist
ifdef UV
	$(UV) build
else
	$(PY) -m build
endif

tag: ## Tag a release: make tag V=0.4.0
	@test -n "$(V)" || { echo "Usage: make tag V=0.4.0"; exit 1; }
	@echo "$(V)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([-.].+)?$$' \
		|| { echo "V must look like 0.4.0 -- no leading 'v' (got '$(V)')."; exit 1; }
	@git rev-parse -q --verify "refs/tags/v$(V)" >/dev/null \
		&& { echo "Tag v$(V) already exists."; exit 1; } || true
	@git diff --quiet && git diff --cached --quiet \
		|| { echo "Working tree is dirty -- commit or stash first."; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" \
		|| { echo "Tag from main, not $$(git rev-parse --abbrev-ref HEAD)."; exit 1; }
	@git fetch origin main --quiet && git diff --quiet HEAD origin/main \
		|| { echo "Local main differs from origin/main -- pull first."; exit 1; }
	$(MAKE) lint test
	git tag -a "v$(V)" -m "Release v$(V)"
	git push origin "v$(V)"
	@echo "Tagged v$(V) (was $(CURRENT_VERSION))."

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	@find . -path ./$(VENV) -prune -o -path ./.git -prune -o \
		-type d -name __pycache__ -print0 | xargs -0 -r rm -rf

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)
