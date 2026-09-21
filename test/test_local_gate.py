"""Tests for the change-scoped local gate (``scripts/local-gate.py``).

The gate runs the tests a diff is RELATED to, on both surfaces, and leaves the
full suite to CI. These tests pin the contracts that make it safe:

1. **No automatic full run**: no diff shape -- meta, both surfaces, evidence
   only, a large related set -- ever produces the full plan. Only ``--full``
   does, and it is for a human who asks.
2. **Fail closed, not open**: an unreadable diff or an untrustworthy selection
   raises ``GateCannotSee`` (exit 2, run nothing) instead of running everything.
3. **CI parity**: the bucket prefixes here are the SAME ones ci.yml's
   ``changes`` job uses, asserted against the workflow text so the two cannot
   drift apart silently.
"""

from __future__ import annotations

import argparse
import importlib
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
        ("test/test_gatewayd_diag.py", (False, False, True)),
        ("docs/README.md", (False, False, True)),  # catch-all: unrecognised = backend
        ("newtoplevel.cfg", (False, False, True)),
        ("websites/evil.py", (False, False, True)),  # prefix, not substring
        # Evidence media matches NO bucket -- mirrors ci.yml's
        # '!temp-screenshots/**' backend negation.
        ("temp-screenshots/feature/shot.png", (False, False, False)),
        ("temp-screenshotsx/evil.py", (False, False, True)),  # prefix, not substring
    ],
)
def test_classify_buckets(gate, path: str, expected) -> None:
    assert gate.classify([path]) == expected


def test_classify_evidence_does_not_flip_frontend_only(gate) -> None:
    """A screenshots+frontend diff stays frontend-only."""
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
    report the wrong surface. Parse the workflow's ``filters:`` block and
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


def test_electron_filter_is_still_mirrored_from_ci(gate) -> None:
    """The cross-surface set the gate hands vitest (via
    ``run_scoped_tests.cross_surface_targets``) strips ``website/electron/``
    guards the same way ci.yml's frontend-test scope step does. That step is
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
        "scripts/run_scoped_tests.py cross_surface_targets()"
    )


# ---------------------------------------------------------------------------
# build_plan(): never a full suite unless asked; fail closed on doubt
# ---------------------------------------------------------------------------

def _plan_labels(plan) -> list[str]:
    return [label for label, _cmd, _cwd in plan.commands]


def _is_full(plan) -> bool:
    return _plan_labels(plan) == ["backend (full)", "frontend (full)"]


def _related(monkeypatch, gate, table: dict[str, list[str]]) -> None:
    """Stub the selection so build_plan is tested on shape, not on repo content.

    Targets still pass through the real ``backend_argv`` / ``frontend_argv``
    and therefore the real ``validated_targets`` admission check.
    """
    def fake_related(surface, paths, root=None):
        targets = table.get(surface, [])
        return targets, f"related: {len(targets)} test file(s) (full suite deferred to CI)"
    monkeypatch.setattr(gate, "related_targets", fake_related)


def test_full_flag_forces_full_gate(gate) -> None:
    plan = gate.build_plan(_args(full=True))
    assert _is_full(plan)
    assert "--full requested" in plan.reason


def test_full_flag_runs_the_budgeted_auto_with_a_capped_env(gate, monkeypatch) -> None:
    """Even the human-requested full run must not own the whole box -- and the
    bound goes through xdist_budget's knob, not an explicit -n that would bypass
    the budget's memory clamp and shared slots."""
    _label, cmd, _cwd = gate.build_plan(_args(full=True)).commands[0]
    assert cmd[cmd.index("-n") + 1] == "auto"
    assert cmd[cmd.index("--dist") + 1] == "loadgroup"
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    env = gate.pytest_worker_env()
    cap = int(env["PYTEST_XDIST_AUTO_NUM_WORKERS"])
    assert 2 <= cap <= 12


def test_an_inherited_tighter_cap_is_never_raised(gate, monkeypatch) -> None:
    """Kiro Crew seeds PYTEST_XDIST_AUTO_NUM_WORKERS at every agent spawn; the
    gate must not hand a run more workers than its parent asked for."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "1")
    assert gate.pytest_worker_env()["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "1"
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "200")
    assert int(gate.pytest_worker_env()["PYTEST_XDIST_AUTO_NUM_WORKERS"]) <= 12


def test_main_passes_the_capped_env_to_every_command(gate, monkeypatch) -> None:
    plan = gate.Plan("test plan")
    plan.add("backend (related)", [gate.sys.executable, "-m", "pytest", "-n", "auto"], gate._REPO_ROOT)
    monkeypatch.setattr(gate, "build_plan", lambda _args: plan)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    seen: list[dict] = []

    def fake_run(cmd, cwd, env=None, **_kwargs):
        seen.append(env or {})
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    assert gate.main([]) == 0
    assert seen and 2 <= int(seen[0]["PYTEST_XDIST_AUTO_NUM_WORKERS"]) <= 12


def test_unreadable_diff_fails_closed(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: None)
    with pytest.raises(gate.GateCannotSee, match="running nothing"):
        gate.build_plan(_args())


def test_unreadable_diff_exits_two_from_main(gate, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: None)
    assert gate.main(["--dry-run"]) == 2
    assert "running nothing" in capsys.readouterr().err


def test_empty_diff_runs_nothing(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: [])
    plan = gate.build_plan(_args())
    assert plan.commands == []
    assert "CI" in plan.reason


def test_empty_diff_dry_run_defers_full_suite_to_ci(gate, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: [])
    rc = gate.main(["--dry-run"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "(full)" not in err
    assert "deferred to CI" in err


def test_meta_diff_does_not_run_full(gate, monkeypatch) -> None:
    """A scripts/ (meta) change gets its related set, not the full gate."""
    monkeypatch.setattr(gate, "changed_files", lambda base: ["scripts/clean.sh"])
    _related(monkeypatch, gate, {"backend": ["test/test_local_gate.py"]})
    plan = gate.build_plan(_args())
    assert not _is_full(plan)
    assert _plan_labels(plan) == ["backend (related)"]
    assert "meta=True" in plan.reason and "deferred to CI" in plan.reason


def test_evidence_only_diff_runs_nothing(gate, monkeypatch) -> None:
    """A screenshots-only diff matches no bucket in CI, which runs its full
    matrix. Locally there is nothing related to run and the full suite stays
    CI's."""
    monkeypatch.setattr(
        gate, "changed_files",
        lambda base: ["temp-screenshots/feature/shot.png"],
    )
    plan = gate.build_plan(_args())
    assert plan.commands == []
    assert "deferred to CI" in plan.reason


def test_both_surfaces_does_not_run_full(gate, monkeypatch) -> None:
    """One backend file plus one frontend file: each surface gets its related
    set; neither surface, and not the pair, is a reason for the full suite."""
    monkeypatch.setattr(
        gate, "changed_files",
        lambda base: ["website/src/App.tsx", "src/kiro_crew/gateway.py"],
    )
    _related(monkeypatch, gate, {
        "backend": ["test/test_gatewayd_diag.py"],
        "frontend": ["src/test/App.deployRoute.test.tsx"],
    })
    plan = gate.build_plan(_args())
    assert not _is_full(plan)
    assert _plan_labels(plan) == ["backend (related)", "frontend (related)"]


def test_no_env_var_selects_full(gate, monkeypatch) -> None:
    """Only the flag reaches the full suite. An env var must not."""
    monkeypatch.setenv("KIROCREW_LOCAL_GATE_FULL", "1")
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    _related(monkeypatch, gate, {"backend": ["test/test_gatewayd_diag.py"]})
    assert not _is_full(gate.build_plan(_args()))


def test_large_related_set_still_runs_as_related(gate, monkeypatch) -> None:
    """Size is printed, never used as a reason to switch to the full suite."""
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/context.py"])
    many = [f"test/test_m{i}.py" for i in range(500)]
    _related(monkeypatch, gate, {"backend": many})
    monkeypatch.setattr(gate, "backend_argv", lambda targets: ["pytest", "--", *targets])
    plan = gate.build_plan(_args())
    labels = _plan_labels(plan)
    assert labels and all(label.startswith("backend (related)") for label in labels)
    assert not any("(full)" in label for label in labels)
    covered = [t for _l, cmd, _c in plan.commands for t in cmd[cmd.index("--") + 1 :]]
    assert covered == many


def test_related_set_is_batched_under_the_windows_argv_limit(gate, monkeypatch) -> None:
    """1,000+ targets must never reach one CreateProcess call.

    Windows caps a command line at 32,767 chars; a `session.py` change selects
    ~1,045 files (~36K chars) and the gate died with WinError 206 before pytest
    ran. Each batch's joined argv stays under `ARGV_CHAR_LIMIT`, every target
    lands in exactly one batch in order, and the labels number the batches.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/session.py"])
    many = [f"test/test_widely_referenced_module_{i:04d}.py" for i in range(1100)]
    _related(monkeypatch, gate, {"backend": many})
    monkeypatch.setattr(
        gate, "backend_argv",
        lambda targets: ["python", "-m", "pytest", "-q", "-n", "12", "--no-cov", "--", *targets],
    )
    plan = gate.build_plan(_args())
    cmds = [cmd for _l, cmd, _c in plan.commands]
    assert len(cmds) > 1
    limit = importlib.import_module("run_scoped_tests").ARGV_CHAR_LIMIT
    assert all(len(" ".join(cmd)) <= limit for cmd in cmds)
    covered = [t for cmd in cmds for t in cmd[cmd.index("--") + 1 :]]
    assert covered == many
    labels = _plan_labels(plan)
    assert labels[0] == f"backend (related) 1/{len(cmds)}"
    assert labels[-1] == f"backend (related) {len(cmds)}/{len(cmds)}"


def test_selector_failure_fails_closed(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])

    def boom(surface, paths, root=None):
        raise gate.SelectionUntrustworthy("cross-surface selector failed")

    monkeypatch.setattr(gate, "related_targets", boom)
    with pytest.raises(gate.GateCannotSee, match="cannot select related tests"):
        gate.build_plan(_args())


# ---------------------------------------------------------------------------
# build_plan(): the related shapes
# ---------------------------------------------------------------------------

def test_frontend_only_diff_runs_related_on_both_surfaces(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    _related(monkeypatch, gate, {
        "backend": ["test/test_redaction_mirror_parity.py"],
        "frontend": ["src/test/App.deployRoute.test.tsx"],
    })
    plan = gate.build_plan(_args())
    labels = _plan_labels(plan)
    assert labels == ["backend (related)", "frontend (related)"]
    _label, cmd, _cwd = plan.commands[0]
    assert cmd[-1] == "test/test_redaction_mirror_parity.py"
    assert not any("(full)" in label for label in labels)


def test_surface_with_nothing_related_is_skipped_with_a_note(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    _related(monkeypatch, gate, {"frontend": ["src/test/App.deployRoute.test.tsx"]})
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["frontend (related)"]
    assert any(note.startswith("backend: related: 0") for note in plan.notes)


def test_missing_node_does_not_discard_the_backend_plan(gate, monkeypatch, capsys) -> None:
    """A box without Node still runs the backend related set.

    The frontend related set is non-empty for a backend diff (it carries CI's
    cross-surface specs), so ``frontend_argv`` is reached and cannot find ``npx``.
    That is an environment gap, not an untrustworthy selection: the plan keeps the
    backend command, notes the skipped surface, and exits 0 with a loud line
    saying CI runs those specs. It must NOT surface as ``GateCannotSee``.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    _related(monkeypatch, gate, {
        "backend": ["test/test_gatewayd_diag.py"],
        "frontend": ["src/test/AcpAdapter.defaults.test.ts"],
    })

    def no_node(targets):
        raise gate.LauncherMissing("npx is not on PATH, so the frontend suite cannot be launched")

    monkeypatch.setattr(gate, "frontend_argv", no_node)
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["backend (related)"]
    assert plan.skipped == ["frontend"]
    assert any("NOT RUN HERE" in note and "CI runs them" in note for note in plan.notes)

    monkeypatch.setattr(gate, "build_plan", lambda _args: plan)
    monkeypatch.setattr(
        gate.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0),
    )
    assert gate.main([]) == 0
    err = capsys.readouterr().err
    assert "frontend related set NOT run here" in err


def test_a_missing_runner_is_a_selection_error_everywhere_else(gate) -> None:
    """``LauncherMissing`` stays a ``SelectionUntrustworthy`` so single-surface
    callers (the profile's per-surface gates) still exit 2 on it."""
    assert issubclass(gate.LauncherMissing, gate.SelectionUntrustworthy)


def test_backend_only_diff_runs_related_not_full(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    _related(monkeypatch, gate, {
        "backend": ["test/test_gatewayd_diag.py"],
        "frontend": ["src/test/AcpAdapter.defaults.test.ts"],
    })
    plan = gate.build_plan(_args())
    assert _plan_labels(plan) == ["backend (related)", "frontend (related)"]
    _label, be_cmd, be_cwd = plan.commands[0]
    assert be_cwd == gate._REPO_ROOT
    assert be_cmd[be_cmd.index("-n") + 1] == "auto"  # budgeted; the cap rides in the env
    _label, fe_cmd, fe_cwd = plan.commands[1]
    assert fe_cwd == gate._REPO_ROOT / "website"
    assert fe_cmd[-1] == "src/test/AcpAdapter.defaults.test.ts"


# ---------------------------------------------------------------------------
# admission: a selected target must never reach a runner as an option
# ---------------------------------------------------------------------------

HOSTILE_TARGETS = [
    "--config=evil.ini",       # reaches pytest as an OPTION, not a path
    "-p=no:randomly",
    "../outside.py",           # escapes the tree
    "test/does_not_exist.py",  # named but absent: the selection is stale
]


@pytest.mark.parametrize("hostile", HOSTILE_TARGETS)
def test_hostile_backend_target_fails_closed(gate, monkeypatch, hostile: str) -> None:
    """A target that could act as an option or escape the tree runs NOTHING.

    The selection's output is spliced straight into a pytest argv, so a file
    named ``--config=evil.ini`` would be read as a FLAG rather than a path.
    There is no shell (argv is always a list), so the exposure is argument
    injection -- and a test runner's own flags are quite enough to do damage.

    The old contract fell OPEN to the full suite here. Now the gate exits 2:
    a selection it cannot explain is not one it may run, and the full suite is
    not a hiding place for that.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    _related(monkeypatch, gate, {"backend": [hostile]})
    with pytest.raises(gate.GateCannotSee, match="cannot select related tests"):
        gate.build_plan(_args())


@pytest.mark.parametrize("hostile", ["--reporter=evil", "../outside.test.ts"])
def test_hostile_frontend_target_fails_closed(gate, monkeypatch, hostile: str) -> None:
    """Same admission on the vitest hand-off.

    Checked separately because the frontend path re-roots each target to
    ``cwd=website`` first, so it validates against a different root and a
    single shared assertion would not prove both.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    _related(monkeypatch, gate, {"frontend": [hostile]})
    with pytest.raises(gate.GateCannotSee):
        gate.build_plan(_args())


def test_one_hostile_target_condemns_the_whole_selection(gate, monkeypatch) -> None:
    """A good target beside a bad one must not be run as a partial plan.

    Dropping the bad entry and keeping the rest would be the tempting
    behaviour and the wrong one: a selection containing something the gate
    cannot explain is not a selection it can justify running.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    _related(monkeypatch, gate, {"backend": ["test/test_local_gate.py", "--config=evil.ini"]})
    with pytest.raises(gate.GateCannotSee):
        gate.build_plan(_args())


def test_backend_targets_are_preceded_by_a_double_dash(gate, monkeypatch) -> None:
    """pytest gets ``--``; vitest deliberately does not.

    ``run_scoped_tests.frontend_argv`` carries the measurement for the
    asymmetry: ``vitest run -- <paths>`` stops treating the positionals as
    filters and runs the whole suite, so the report would claim a narrow
    scope while everything ran.
    """
    monkeypatch.setattr(gate, "changed_files", lambda base: ["website/src/App.tsx"])
    _related(monkeypatch, gate, {"backend": ["test/test_local_gate.py"]})
    _label, cmd, _cwd = gate.build_plan(_args()).commands[0]
    assert cmd[-2] == "--" and cmd[-1] == "test/test_local_gate.py"


def test_vitest_targets_are_not_preceded_by_a_double_dash(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "changed_files", lambda base: ["src/kiro_crew/gateway.py"])
    _related(monkeypatch, gate, {"frontend": ["src/test/AcpAdapter.defaults.test.ts"]})
    _label, cmd, _cwd = gate.build_plan(_args()).commands[0]
    assert "--" not in cmd


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
    plan = gate.Plan("test plan")
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

def test_dry_run_full_exits_zero_and_prints_plan(gate, capsys) -> None:
    rc = gate.main(["--dry-run", "--full"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "local-gate:" in err
    assert "backend (full)" in err
    assert "frontend (full)" in err


def test_dry_run_against_the_real_repo_never_plans_a_full_suite(gate, monkeypatch, capsys) -> None:
    """Whatever the diff looks like, the default plan run END TO END is related-only.

    The selection is real: ``related_targets`` scans this checkout's actual test
    tree for the changed files, which is what the stubbed-``_related`` tests
    above never exercise. The DIFF is pinned, though, not read from the
    developer's working tree. ``changed_files`` reports every untracked path, so
    a checkout carrying a large evidence tree (a sweep's ``hygiene/tmp`` with
    tens of thousands of files) hands the reference matcher an alternation of
    that size and the scan runs for minutes -- the test then measured the
    developer's clutter, not the gate. One file per surface is the realistic
    dirty tree; only a ``(full)`` surface label -- the ``--full`` plan's own
    marker -- would break the invariant.
    """
    monkeypatch.setattr(
        gate, "changed_files",
        lambda base: ["src/kiro_crew/gateway.py", "website/src/App.tsx"],
    )
    rc = gate.main(["--dry-run"])
    err = capsys.readouterr().err
    assert rc in (0, 2), err
    assert "(full)" not in err
    if rc == 0:
        # Every rc==0 plan names CI as the full suite's owner, in one of two
        # spellings depending on whether there was anything related to select.
        assert "full suite deferred to CI" in err or "the full suite runs in CI" in err, err
