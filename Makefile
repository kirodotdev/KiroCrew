# KiroCrew — public build targets (pip + npm/vite + pytest).
# Common flow: `make` runs build (frontend + backend) then tests.
#
# Standalone distribution targets:
#   make wheel     — self-contained pip wheel (dashboard bundled)
#   make backend-bin — frozen standalone backend binary (PyInstaller)
#   make desktop   — double-clickable desktop app (universal DMG on macOS / AppImage on Linux)
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
	# `cat ... || true`, not `cat ... &&`: on a first run where ensure-node.sh
	# could not record a bin dir (no network, unsupported platform, or node
	# already fine on PATH but the write failed), the marker file is absent and
	# `cat` exits 1. With `&&` chaining that non-zero exit aborts the whole
	# recipe line, so the target fails before npm is ever reached. An absent
	# marker must degrade to "use whatever node is on PATH", not stop the build.
	cd website && \
	  NBD="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"; \
	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; }; \
	  if ! command -v npm >/dev/null 2>&1; then \
	    echo "ERROR: npm not found. Install Node >= 18 (see ensure-node.sh) and re-run." >&2; \
	    exit 1; \
	  fi; \
	  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && \
	  npm run build
	rm -rf src/kiro_crew/static/dist
	mkdir -p src/kiro_crew/static
	cp -R website/dist src/kiro_crew/static/dist

backend:
	bash ensure-python.sh || true
	# Same `|| true` reasoning as the frontend target: an absent marker file must
	# fall back to $(PY), not abort the recipe.
	PY="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/python-bin" 2>/dev/null || true)"; [ -n "$$PY" ] || PY="$(PY)"; \
	  if [ -x $(VENV)/bin/python ] && ! $(VENV)/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then \
	    echo "  → recreating $(VENV) (existing interpreter < 3.10)"; rm -rf $(VENV); fi; \
	  if ! "$$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then \
	    echo "ERROR: '$$PY' is not Python >= 3.10 (package requires-python is >=3.10)." >&2; \
	    echo "       Without this gate the venv is built from a too-old interpreter, the" >&2; \
	    echo "       version guard above deletes it on every run, and the install either" >&2; \
	    echo "       backtracks forever or crashes at import. Provision 3.10+ first:" >&2; \
	    echo "         bash ensure-python.sh   # or: make backend PY=python3.12" >&2; \
	    exit 1; \
	  fi; \
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
#
# Runs through the venv the `backend` target provisions rather than a bare
# `$(PY) -m pip install --upgrade build`: on hosts whose system python3 is older
# than 3.10 (Amazon Linux 2023 ships 3.9) that bare form installs `build` into
# the *system* interpreter — mutating it without a venv, and tripping PEP 668
# "externally-managed-environment" where the marker exists. Depending on
# `backend` guarantees a >= 3.10 venv exists first.
wheel: frontend backend
	$(PIP) install --upgrade build
	$(VENV)/bin/python -m build --wheel

# Frozen standalone backend binary (no system Python needed). Stages the
# dashboard first so it's embedded in the bundle. Host-arch only (UNIVERSAL=0):
# the standalone backend is a local-machine artifact, not a distributable app.
backend-bin: frontend
	UNIVERSAL=0 SKIP_FRONTEND=1 SKIP_ELECTRON=1 bash packaging/build-desktop.sh

# Full double-clickable desktop app. macOS: ONE universal DMG (arm64 + x86_64,
# needs an Apple-Silicon host with Rosetta 2 — see docs/DESKTOP_APP.md;
# UNIVERSAL=0 for a faster host-arch-only build). Linux: AppImage (host arch).
#
# build-desktop.sh runs `npm ci` / `npm run build` itself, so it needs node on
# PATH. It provisions its own uv + PBS interpreter but NOT node, so bootstrap
# node here — otherwise a first `make desktop` on a node-less host dies at the
# script's npm step instead of installing it like every other target does.
desktop:
	bash ensure-node.sh || true
	NBD="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"; \
	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; }; \
	  bash packaging/build-desktop.sh

clean:
	rm -rf build dist *.egg-info src/*.egg-info \
	       src/kiro_crew/static/dist website/dist \
	       website/electron/backend-dist website/electron/dist \
	       .pytest_cache .mypy_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
