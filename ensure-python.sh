#!/bin/bash
# Ensure a Python >= 3.10 interpreter is available for the backend venv.
# Called by: Makefile (backend), setup.sh
# Platforms: macOS, Amazon Linux 2 (system python3 is 3.7), Amazon Linux 2023
#            (system python3 is 3.9), Linux
#
# The package requires Python >= 3.10 (pyproject `requires-python`; the code
# uses stdlib features such as contextlib.aclosing that need 3.10). Amazon
# Linux 2 ships Python 3.7 and Amazon Linux 2023 ships 3.9 as `python3`, so
# `python3 -m venv` produces a too-old venv and either `pip install -e .` fails
# to resolve modern deps (AL2/3.7) or the install "succeeds" and then crashes at
# import (AL2023/3.9). Prefer any already-usable system Python >= 3.10;
# otherwise install one via mise, whose python-build-standalone binaries run on
# AL2's glibc 2.26. On success the chosen interpreter path is recorded in
# "<data-home>/python-bin" so non-interactive callers (make) can use it without
# re-running a version manager. The data home is "$KIROCREW_HOME" when set, else
# "$HOME/.kiro/crew" (the current default) — NOT the pre-move "$HOME/.kirocrew",
# which the one-time data-home migration deletes, so writing there would
# resurrect the legacy dir on every build.

MIN_MAJOR=3
MIN_MINOR=10
TARGET_PY="${KIROCREW_PYTHON_VERSION:-3.12}"

# True if $1 is a python that runs AND is >= MIN_MAJOR.MIN_MINOR.
_py_ok() {
    [ -n "$1" ] || return 1
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" >/dev/null 2>&1
}

_pyver() {
    "$1" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null
}

# First usable system python, preferring the newest SUPPORTED minor (3.12 is
# the CI/build target, matching TARGET_PY above); untested 3.13 and bare
# python3/python are last resorts so a bleeding-edge install still succeeds.
_find_system_python() {
    local c resolved
    for c in python3.12 python3.11 python3.10 python3.13 python3 python; do
        resolved="$(command -v "$c" 2>/dev/null)" || continue
        if _py_ok "$resolved"; then
            echo "$resolved"
            return 0
        fi
    done
    return 1
}

_mise_bin() {
    if [ -x "$HOME/.local/bin/mise" ]; then
        echo "$HOME/.local/bin/mise"
    elif command -v mise >/dev/null 2>&1; then
        echo "mise"
    fi
}

_record() {
    home="${KIROCREW_HOME:-$HOME/.kiro/crew}"
    mkdir -p "$home"
    printf '%s\n' "$1" > "$home/python-bin" 2>/dev/null || true
}

# 1. Existing usable system python.
PY="$(_find_system_python)"
if [ -n "$PY" ]; then
    echo "  ✅ python $(_pyver "$PY") ($PY)"
    _record "$PY"
    exit 0
fi

MISE="$(_mise_bin)"

# 2. Already-installed mise python. `mise which` resolves the path directly —
# `mise activate` does not reliably inject PATH in non-interactive scripts.
if [ -n "$MISE" ]; then
    CAND="$("$MISE" which python 2>/dev/null)"
    if _py_ok "$CAND"; then
        echo "  ✅ python $(_pyver "$CAND") (mise: $CAND)"
        _record "$CAND"
        exit 0
    fi
fi

# 3. Install via mise.
echo "  → No Python >= $MIN_MAJOR.$MIN_MINOR found; installing python@$TARGET_PY via mise…"
if [ -z "$MISE" ]; then
    echo "  → Installing mise…"
    curl -fsSL https://mise.run | sh
    MISE="$(_mise_bin)"
fi
if [ -z "$MISE" ]; then
    echo "  ⚠️  mise unavailable — install Python >= $MIN_MAJOR.$MIN_MINOR manually and re-run."
    exit 1
fi

"$MISE" use -g "python@$TARGET_PY" 2>/dev/null
CAND="$("$MISE" which python 2>/dev/null)"
if _py_ok "$CAND"; then
    echo "  ✅ python $(_pyver "$CAND") installed ($CAND)"
    _record "$CAND"
else
    echo "  ⚠️  Python install failed — install manually: curl https://mise.run | sh && mise use -g python@$TARGET_PY"
    exit 1
fi
