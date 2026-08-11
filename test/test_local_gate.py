"""Tests for the change-scoped local gate (``scripts/local-gate.py``).

The gate narrows the LOCAL iteration suite the same way CI narrows its matrix:
three diff buckets, narrowing only on a provably single-surface diff, full run
on any doubt. These tests pin the two contracts that make it safe:

1. **Fail-open**: every unreadable/ambiguous input produces the FULL plan.
2. **CI parity**: the bucket prefixes here are the SAME ones ci.yml's
   ``changes`` job uses, asserted against the workflow text so the two cannot
   drift apart silently.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "local-gate.py"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_gate():
    spec = importlib.util.spec_from_file_location("local_gate", _SCRIPT)
    assert spec and spec.loader, "could not build an import spec for the gate"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def _args(**overrides):
    defaults = {"base": "origin/main", "dry_run": True, "full": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_script_exists(gate) -> None:
    assert _SCRIPT.is_file()


# ---------------------------------------------------------------------------
# classify(): the three-bucket rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("website/src/App.tsx", (True, False, False)),
        ("website/electron/main.js", (True, False, False)),
        (".github/workflows/ci.yml", (False, True, False)),
        ("scripts/local-gate.py", (False, True, False)),
        ("src/kiro_crew/gateway.py", (False, False, True)),
        ("test/test_gateway.py", (False, False, True)),
        ("docs/README.md", (False, False, True)),  # catch-all: unrecognised = backend
        ("newtoplevel.cfg", (False, False, True)),
        ("websites/evil.py", (False, False, True)),  # prefix, not substring
    ],
)
def test_classify_buckets(gate, path: str, expected) -> None:
    assert gate.classify([path]) == expected


def test_classify_mixed_diff_sets_both_flags(gate) -> None:
    frontend, meta, backend = gate.classify(
        ["website/src/App.tsx", "src/kiro_crew/gateway.py"]
    )
    assert frontend and backend and not meta


def test_classify_windows_separators(gate) -> None:
    assert gate.classify(["website\\src\\App.tsx"]) == (True, False, False)


def test_classify_ignores_blank_lines(gate) -> None:
    assert gate.classify(["", "  "]) == (False, False, False)


# ---------------------------------------------------------------------------
# CI parity: the buckets MUST be the ones ci.yml uses
# ---------------------------------------------------------------------------

def test_bucket_prefixes_match_ci_changes_job(gate) -> None:
    """The bucket rules must be exactly ci.yml's — in BOTH directions.

    A one-directional substring pin would catch a local prefix missing from
    ci.yml but not a bucket CI adds (a new frontend tree, a path moved into
    meta): the local gate would misclassify it into the backend catch-all and
    skip locally what CI runs. Parse the workflow's ``filters:`` block and
    assert set equality, so any divergence — either direction — fails here.
    """
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    filter_step = next(
        step
        for step in workflow["jobs"]["changes"]["steps"]
        if "paths-filter" in str(step.get("uses", ""))
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])

    def _prefixes(patterns: list[str]) -> set[str]:
        # ci.yml expresses buckets as '<prefix>**' globs; anything else in a
        # bucket the local gate mirrors would need new classify() logic, so
        # fail loudly rather than approximating.
        out = set()
        for pattern in patterns:
            assert pattern.endswith("**") and not pattern.startswith("!"), (
                f"ci.yml bucket pattern {pattern!r} is not a plain '<prefix>**' "
                "glob -- update scripts/local-gate.py classify() to match it, "
                "then update this parser"
            )
            out.add(pattern[:-2])
        return out

    assert set(gate._FRONTEND_PREFIXES) == _prefixes(filters["frontend"]), (
        "frontend bucket drifted between scripts/local-gate.py and ci.yml -- "
        "update _FRONTEND_PREFIXES and ci.yml together"
    )
    assert set(gate._META_PREFIXES) == _prefixes(filters["meta"]), (
        "meta bucket drifted between scripts/local-gate.py and ci.yml -- "
        "update _META_PREFIXES and ci.yml together"
    )
    # The backend bucket must stay the exact complement of the other two:
    # positive '**' plus a negation for every frontend/meta pattern. A bucket
    # added to frontend/meta without its matching backend negation would make
    # some paths land in TWO buckets, breaking "only_X means only X changed".
    positives = [p for p in filters["backend"] if not p.startswith("!")]
    negations = {p[1:] for p in filters["backend"] if p.startswith("!")}
    assert positives == ["**"], (
        "ci.yml's backend bucket is no longer a pure '**' catch-all -- "
        "scripts/local-gate.py classify() must be reworked to match"
    )
    assert negations == set(filters["frontend"]) | set(filters["meta"]), (
        "ci.yml's backend negations no longer mirror frontend+meta exactly -- "
        "re-derive the bucket rules in scripts/local-gate.py"
    )


# ---------------------------------------------------------------------------
# build_plan(): fail-open on every doubtful input
# ---------------------------------------------------------------------------

def test_electron_filter_is_still_mirrored_from_ci(gate) -> None:
    """build_plan() filters ``website/electron/`` guards out of the vitest
    hand-off the same way ci.yml's frontend-test scope step does. That step is
    bash, so full structural parity isn't parseable — pin the observable
    contract instead: the frontend-test job still strips electron specs with
    ``grep -v '^electron/'`` after re-rooting to cwd=website. If this
    disappears, CI stopped partitioning electron from vitest specs and the
    local filter needs a fresh look."""
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "frontend-test:" in workflow
    frontend_job = workflow.split("frontend-test:", 1)[1]
    # Bound the search to this job: cut at the next top-level job key.
    next_job = re.search(r"\n  [a-z][a-z0-9-]*:\n", frontend_job)
    if next_job:
        frontend_job = frontend_job[: next_job.start()]
    assert "grep -v '^electron/'" in frontend_job, (
        "ci.yml's frontend-test job no longer filters electron specs from the "
        "vitest hand-off -- re-examine the electron filtering in "
        "scripts/local-gate.py build_plan()"
    )


def _plan_labels(plan) -> list[str]:
    return [label for label, _cmd, _cwd in plan.commands]


def _is_full(plan) -> bool:
    return _plan_labels(plan) == ["backend (full)", "frontend (full)"]


def test_full_flag_forces_full_gate(gate) -> None:
    assert _is_full(gate.build_plan(_args(full=True)))


def test_unreadable_diff_falls_open(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: None)
    plan = gate.build_plan(_args())
    assert _is_full(plan)
    assert "fail-open" in plan.reason


def test_empty_diff_falls_open(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: [])
    assert _is_full(gate.build_plan(_args()))


def test_meta_diff_runs_full(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["scripts/clean.sh"])
    assert _is_full(gate.build_plan(_args()))


def test_both_surfaces_runs_full(gate, monkeypatch) -> None:
    monkeypatch.setattr(
        gate, "changed_files",
        lambda base: ["website/src/App.tsx", "src/kiro_crew/gateway.py"],
    )
    assert _is_full(gate.build_plan(_args()))


def test_selector_failure_falls_open(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: None)
    plan = gate.build_plan(_args())
    assert _is_full(plan)
    assert "fail-open" in plan.reason


# ---------------------------------------------------------------------------
# build_plan(): the two narrowed shapes
# ---------------------------------------------------------------------------

def test_frontend_only_diff_narrows_backend(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(
        gate, "selector_must_run",
        lambda surface: ["test/test_redaction_mirror_parity.py"],
    )
    plan = gate.build_plan(_args())
    labels = _plan_labels(plan)
    assert labels == ["frontend (full)", "backend (cross-surface guards)"]
    _label, cmd, _cwd = plan.commands[1]
    assert cmd[-1] == "test/test_redaction_mirror_parity.py"


def test_frontend_only_diff_with_no_guards_runs_frontend_alone(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: [])
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["frontend (full)"]


def test_backend_only_diff_narrows_frontend(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    monkeypatch.setattr(
        gate, "selector_must_run",
        lambda surface: [
            "website/src/utils/sanitize.test.ts",
            "website/electron/permissions.test.js",  # electron job's guard: filtered
        ],
    )
    plan = gate.build_plan(_args())
    labels = _plan_labels(plan)
    assert labels == ["backend (full)", "frontend (cross-surface guards)"]
    _label, cmd, cwd = plan.commands[1]
    # electron guard filtered out; path re-rooted for cwd=website
    assert cmd[-1] == "src/utils/sanitize.test.ts"
    assert not any("electron" in part for part in cmd)
    assert cwd.name == "website"


def test_backend_only_diff_with_no_guards_runs_backend_alone(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: [])
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["backend (full)"]


# ---------------------------------------------------------------------------
# End-to-end dry run against the real repo (no tests executed)
# ---------------------------------------------------------------------------

def test_dry_run_exits_zero_and_prints_plan(gate, capsys) -> None:
    rc = gate.main(["--dry-run", "--full"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "local-gate:" in err
    assert "backend (full)" in err
    assert "frontend (full)" in err
