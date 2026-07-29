# cdripper development tasks.
#
# Quick start:  make dev && make test
# Everything runs inside .venv -- no global installs, no sudo (except `make deps`).

VENV    := .venv
PY      := $(VENV)/bin/python
PYTEST  := $(PY) -m pytest
RUFF    := $(VENV)/bin/ruff
UV      := $(shell command -v uv 2>/dev/null)

# Version comes from the latest git tag via setuptools-scm.
CURRENT_VERSION := $(shell git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)

.DEFAULT_GOAL := help
.PHONY: help dev deps test test-cov lint fmt check run once clean distclean build tag

help: ## Show this help
	@echo "cdripper -- development tasks"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Current version: $(CURRENT_VERSION)"

$(VENV):
ifdef UV
	$(UV) venv $(VENV)
else
	python3 -m venv $(VENV)
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

test: ## Run the unit tests
	$(PYTEST) tests/ -q

test-cov: ## Run tests with a coverage report
	$(PYTEST) tests/ --cov=cdripper --cov-report=term-missing

lint: ## Check style and common errors
	$(RUFF) check src/ tests/

fmt: ## Auto-fix what ruff can fix
	$(RUFF) check --fix src/ tests/

check: ## Verify the runtime environment (system binaries + python imports)
	@echo "System binaries:"
	@for b in cdparanoia flac eject; do \
		printf "  %-12s " "$$b"; \
		command -v $$b >/dev/null && echo "ok" || echo "MISSING"; \
	done
	@echo "Python imports:"
	@$(PY) -c "import discid, musicbrainzngs, mutagen, rich; print('  all ok')" \
		|| echo "  MISSING -- run 'make dev' (and 'make deps' for libdiscid)"
	@echo "Optical drives detected:"
	@$(PY) -c "import cdripper; d=cdripper.detect_drives(); print('  ' + (', '.join(d) if d else 'none'))"

run: ## Run cdripper against all detected drives
	$(VENV)/bin/cdripper

once: ## Rip a single disc and exit
	$(VENV)/bin/cdripper --once

build: clean ## Build the wheel and sdist
ifdef UV
	$(UV) build
else
	$(PY) -m build
endif

tag: ## Tag a release: make tag V=0.4.0
	@test -n "$(V)" || { echo "Usage: make tag V=0.4.0"; exit 1; }
	@git diff --quiet && git diff --cached --quiet \
		|| { echo "Working tree is dirty -- commit or stash first."; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" \
		|| { echo "Tag from main, not $$(git rev-parse --abbrev-ref HEAD)."; exit 1; }
	@git fetch origin main --quiet && git diff --quiet HEAD origin/main \
		|| { echo "Local main differs from origin/main -- pull first."; exit 1; }
	$(MAKE) test
	git tag -a "v$(V)" -m "Release v$(V)"
	git push origin "v$(V)"
	@echo "Tagged v$(V) (was $(CURRENT_VERSION))."

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)
