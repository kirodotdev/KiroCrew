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
import subprocess
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
        # Evidence media matches NO bucket (#8027) -- mirrors ci.yml's
        # '!temp-screenshots/**' backend negation.
        ("temp-screenshots/feature/shot.png", (False, False, False)),
        ("temp-screenshotsx/evil.py", (False, False, True)),  # prefix, not substring
    ],
)
def test_classify_buckets(gate, path: str, expected) -> None:
    assert gate.classify([path]) == expected


def test_classify_evidence_does_not_flip_frontend_only(gate) -> None:
    """A screenshots+frontend diff stays frontend-only -- the #8027 fix."""
    frontend, meta, backend = gate.classify(
        ["website/src/App.tsx", "temp-screenshots/feature/shot.png"]
    )
    assert frontend and not meta and not backend


def test_changed_files_keeps_both_rename_endpoints(gate, monkeypatch) -> None:
    """Renaming a real file INTO temp-screenshots/ must not hide the old
    path's bucket: ``--no-renames`` splits a rename into delete + add (the
    same contract run_scoped_tests.py and CI's dorny/paths-filter apply), and
    the porcelain parser keeps BOTH sides of a defensive ``old -> new`` arrow.
    """
    class _Proc:
        def __init__(self, stdout: str = "") -> None:
            self.returncode = 0
            self.stdout = stdout

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["git", "merge-base"]:
            return _Proc("abc123\n")
        if argv[:2] == ["git", "diff"]:
            assert "--no-renames" in argv, "committed diff must not fold renames"
            return _Proc("src/kiro_crew/gateway.py\n")
        assert "--no-renames" in argv, "porcelain status must not fold renames"
        return _Proc('R  src/kiro_crew/moved.py -> temp-screenshots/f/moved.png\n')

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    paths = gate.changed_files("main")
    assert paths is not None
    assert "src/kiro_crew/moved.py" in paths
    assert "temp-screenshots/f/moved.png" in paths
    # And the classification consequence: the old backend path still counts.
    assert gate.classify(paths) == (False, False, True)


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
    ignored = {f"{prefix}**" for prefix in gate._IGNORED_PREFIXES}
    assert negations == set(filters["frontend"]) | set(filters["meta"]) | ignored, (
        "ci.yml's backend negations no longer mirror frontend+meta plus the "
        "ignored evidence prefixes -- re-derive the bucket rules in "
        "scripts/local-gate.py (_FRONTEND_PREFIXES / _META_PREFIXES / "
        "_IGNORED_PREFIXES)"
    )
    # classify() tests the ignored prefixes FIRST, so an entry overlapping a
    # real bucket would silently shadow it while the set-union above still
    # passed. Keep the carve-out disjoint from the buckets it is carved from.
    assert not (
        set(gate._IGNORED_PREFIXES)
        & (set(gate._FRONTEND_PREFIXES) | set(gate._META_PREFIXES))
    ), "_IGNORED_PREFIXES must not overlap the frontend/meta bucket prefixes"


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


def test_evidence_only_diff_falls_open(gate, monkeypatch) -> None:
    """A screenshots-only diff matches no bucket in CI (full matrix runs);
    the local gate mirrors that with the full plan (fail-open)."""
    monkeypatch.setattr(
        gate, "changed_files",
        lambda base: ["temp-screenshots/feature/shot.png"],
    )
    plan = gate.build_plan(_args())
    assert _is_full(plan)
    assert "fail-open" in plan.reason


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
        # REAL paths on purpose: the gate refuses a target the tree does not
        # carry, so a placeholder would exercise the fail-open branch instead
        # of the narrowed shape this test is about.
        lambda surface: [
            "website/src/test/AcpAdapter.defaults.test.ts",
            "website/electron/test/app-menu.test.js",  # electron job's guard: filtered
        ],
    )
    plan = gate.build_plan(_args())
    labels = _plan_labels(plan)
    assert labels == ["backend (full)", "frontend (cross-surface guards)"]
    _label, cmd, cwd = plan.commands[1]
    # electron guard filtered out; path re-rooted for cwd=website
    assert cmd[-1] == "src/test/AcpAdapter.defaults.test.ts"
    assert not any("electron" in part for part in cmd)
    assert cwd.name == "website"


# ---------------------------------------------------------------------------
# build_plan(): the selector's stdout is argv, so it is admitted, not trusted
# ---------------------------------------------------------------------------

HOSTILE_TARGETS = [
    "--config=evil.ini",       # reaches pytest as an OPTION, not a path
    "-p=no:randomly",
    "../outside.py",           # escapes the tree
    "test/does_not_exist.py",  # named but absent: the selection is stale
]


@pytest.mark.parametrize("hostile", HOSTILE_TARGETS)
def test_hostile_backend_target_falls_open(gate, monkeypatch, hostile: str) -> None:
    """A target that could act as an option or escape the tree runs everything.

    The selector's stdout is spliced straight into a pytest argv, so a file
    named ``--config=evil.ini`` would be read as a FLAG rather than a path.
    There is no shell (argv is always a list), so the exposure is argument
    injection -- and a test runner's own flags are quite enough to do damage.

    Falling open is this file's existing contract for anything doubtful, and
    it is also what ``SelectionUntrustworthy`` documents: the caller runs
    everything.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: [hostile])
    plan = gate.build_plan(_args())
    assert _is_full(plan)
    assert "fail-open" in plan.reason
    assert not any(hostile in part for _l, cmd, _c in plan.commands for part in cmd)


@pytest.mark.parametrize("hostile", ["--reporter=evil", "../outside.test.ts"])
def test_hostile_frontend_target_falls_open(gate, monkeypatch, hostile: str) -> None:
    """Same admission on the vitest hand-off.

    Checked separately because the frontend path re-roots each target to
    ``cwd=website`` first, so it validates against a different root and a
    single shared assertion would not prove both.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: ["website/" + hostile])
    plan = gate.build_plan(_args())
    assert _is_full(plan)
    assert "fail-open" in plan.reason


def test_one_hostile_target_condemns_the_whole_selection(gate, monkeypatch) -> None:
    """A good target beside a bad one must not be run as a narrowed plan.

    Dropping the bad entry and keeping the rest would be the tempting
    behaviour and the wrong one: a selection containing something the gate
    cannot explain is not a selection it can justify narrowing on.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(
        gate, "selector_must_run",
        lambda surface: ["test/test_local_gate.py", "--config=evil.ini"],
    )
    assert _is_full(gate.build_plan(_args()))


def test_backend_targets_are_preceded_by_a_double_dash(gate, monkeypatch) -> None:
    """pytest gets ``--``; vitest deliberately does not.

    ``run_scoped_tests.frontend_argv`` carries the measurement for the
    asymmetry: ``vitest run -- <paths>`` stops treating the positionals as
    filters and runs the whole suite, so the report would claim a narrow
    scope while everything ran.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    monkeypatch.setattr(
        gate, "selector_must_run", lambda surface: ["test/test_local_gate.py"],
    )
    _label, cmd, _cwd = gate.build_plan(_args()).commands[1]
    assert cmd[-2] == "--" and cmd[-1] == "test/test_local_gate.py"


def test_vitest_targets_are_not_preceded_by_a_double_dash(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    monkeypatch.setattr(
        gate, "selector_must_run",
        lambda surface: ["website/src/test/AcpAdapter.defaults.test.ts"],
    )
    _label, cmd, _cwd = gate.build_plan(_args()).commands[1]
    assert "--" not in cmd


def test_backend_only_diff_with_no_guards_runs_backend_alone(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    monkeypatch.setattr(gate, "selector_must_run", lambda surface: [])
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["backend (full)"]


# ---------------------------------------------------------------------------
# execution: Node's Windows launchers are .cmd shims
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["npm", "npx"])
def test_node_command_resolves_platform_launcher(gate, monkeypatch, name: str) -> None:
    launcher = rf"C:\Program Files\nodejs\{name}.CMD"
    monkeypatch.setattr(gate.shutil, "which", lambda candidate: launcher)

    assert gate._resolve_command([name, "vitest", "run"]) == [
        launcher,
        "vitest",
        "run",
    ]


def test_non_node_command_is_unchanged(gate) -> None:
    cmd = [gate.sys.executable, "-m", "pytest"]
    assert gate._resolve_command(cmd) is cmd


def test_missing_node_launcher_fails_cleanly(gate, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gate, "build_plan", lambda _args: gate.Plan("test plan"))
    plan = gate.build_plan(None)
    plan.add("frontend", ["npx", "vitest", "run"], gate._REPO_ROOT / "website")
    monkeypatch.setattr(gate, "build_plan", lambda _args: plan)
    monkeypatch.setattr(gate.shutil, "which", lambda _candidate: None)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )

    assert gate.main([]) == 127
    err = capsys.readouterr().err
    assert "FAILED to start" in err
    assert "not found on PATH" in err


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
