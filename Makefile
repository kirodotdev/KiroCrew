# KiroCrew — public build targets (pip + npm/vite + pytest).
# Common flow: `make` runs build (frontend + backend) then tests.
#
# Standalone distribution targets:
#   make wheel     — self-contained pip wheel (dashboard bundled)
#   make backend-bin — frozen standalone backend binary (PyInstaller)
#   make desktop   — double-clickable desktop app (DMG / AppImage)
.PHONY: all build frontend backend test clean wheel backend-bin desktop

PY ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

all: test

# Build the frontend (npm/vite) and stage it into the package, then install
# the backend into a local venv.
build: frontend backend

frontend:
	bash ensure-node.sh || true
	cd website && \
	  NBD="$$(cat $$HOME/.kirocrew/node-bin-dir 2>/dev/null)" && \
	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; } && \
	  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && \
	  npm run build
	rm -rf src/kiro_crew/static/dist
	mkdir -p src/kiro_crew/static
	cp -R website/dist src/kiro_crew/static/dist

backend:
	bash ensure-python.sh || true
	PY="$$(cat $$HOME/.kirocrew/python-bin 2>/dev/null)"; [ -n "$$PY" ] || PY="$(PY)"; \
	  if [ -x $(VENV)/bin/python ] && ! $(VENV)/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then \
	    echo "  → recreating $(VENV) (existing interpreter < 3.10)"; rm -rf $(VENV); fi; \
	  test -x $(VENV)/bin/python || "$$PY" -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	# --prefer-binary: on hosts below the modern manylinux baseline (e.g. Amazon
	# Linux 2, glibc 2.26) the newest release of a compiled dep may ship only a
	# manylinux_2_28 wheel + an sdist. Without this flag pip picks the newest
	# version and builds the sdist from source, which fails (no toolchain / old
	# GCC / missing -dev headers). --prefer-binary makes pip take an older
	# prebuilt wheel instead. No-op where the newest deps already have a usable
	# wheel (macOS, AL2023).
	KIROCREW_SKIP_FRONTEND=1 $(PIP) install --prefer-binary -e ".[dev]"
	bash packaging/resign-macos-libs.sh $(VENV)/bin/python

test: build
	$(PYTEST) -q

# --- Standalone distribution -------------------------------------------------

# Self-contained pip wheel: builds + stages the dashboard, then produces a
# wheel that bundles the SPA (see setup.py BuildWithFrontend + MANIFEST.in).
wheel: frontend
	$(PY) -m pip install --upgrade build
	$(PY) -m build --wheel

# Frozen standalone backend binary (no system Python needed). Stages the
# dashboard first so it's embedded in the bundle.
backend-bin: frontend
	SKIP_FRONTEND=1 SKIP_ELECTRON=1 bash packaging/build-desktop.sh

# Full double-clickable desktop app (DMG on macOS, AppImage on Linux).
desktop:
	bash packaging/build-desktop.sh

clean:
	rm -rf build dist *.egg-info src/*.egg-info \
	       src/kiro_crew/static/dist website/dist \
	       website/electron/backend-dist website/electron/dist \
	       .pytest_cache .mypy_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
