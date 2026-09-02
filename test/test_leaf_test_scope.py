"""Pin the leaf-test reduction: every escalation fires, and CI calls the script.

The reduction this guards replaces the sharded backend suite with a small set for
a diff that only MODIFIES leaf test modules. That is safe only while each
escalation below holds, so each is asserted directly rather than trusted from the
script's own self-test -- and the self-test is additionally executed here, so
`--test` rotting is a test failure rather than a silent no-op.

Two of these tests exist because a reviewer found the hole: `test_workflows_presence`
reaches the `test/` tree via `Path(__file__).resolve().parent` + `.glob(...)` and was
missed by the first version's `/ "test"` regex, and five files reach a test module
through `importlib.import_module`, which no import-statement pattern sees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT.joinpath("scripts", "leaf_test_scope.py")
TEST_DIR = REPO_ROOT.joinpath("test")

sys.path.insert(0, str(REPO_ROOT.joinpath("scripts")))

import leaf_test_scope as mod  # noqa: E402  (path set immediately above)


def _resolved(paths: list[str], code: str = "M") -> object:
    return mod.classify({p: code for p in paths})[0]


def _reason(paths: list[str], code: str = "M") -> str:
    return mod.classify({p: code for p in paths})[1]


@pytest.fixture(scope="module")
def clean_leaf() -> str:
    """A leaf nothing imports, nothing mentions, and that is not itself a gate.

    Discovered rather than hardcoded: naming one in a string literal would make
    `mentions_of` find it (that scan covers `scripts/` and `test/`), so the
    fixture would disqualify the very file it selects.
    """
    gates = set(mod.corpus_gates(REPO_ROOT))
    for candidate in sorted(TEST_DIR.glob("test_*.py")):
        rel = mod._rel_posix(candidate, REPO_ROOT)
        if rel in gates:
            continue
        if mod.importers_of({candidate.stem}, REPO_ROOT):
            continue
        if mod.mentions_of({candidate.stem}, REPO_ROOT):
            continue
        return rel
    pytest.fail("no clean leaf test file found in this repo")


def test_script_exists_and_self_test_passes() -> None:
    assert SCRIPT.is_file(), "scripts/leaf_test_scope.py is missing"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--test"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, (
        f"leaf_test_scope.py --test failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


# ── the reduction is only offered for a genuinely test-only diff ───────────────


@pytest.mark.parametrize(
    "path",
    [
        "src/kiro_crew/session.py",
        "src/kiro_crew/dashboard/server.py",
        "docs/architecture/overview.md",
        "README.md",
        "setup.cfg",
        "website/src/App.tsx",
    ],
)
def test_any_non_test_path_escalates(path: str) -> None:
    assert _resolved([path]) is None, f"{path} must not qualify for the leaf reduction"


def test_a_leaf_mixed_with_source_escalates(clean_leaf: str) -> None:
    assert _resolved([clean_leaf, "src/kiro_crew/agent.py"]) is None


# ── shared test input is never a leaf ─────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "test/conftest.py",
        "test/source_corpus.py",
        "test/spawn_test_helpers.py",
        "test/chat_test_helpers.py",
        "test/tmpdir_helpers.py",
        "test/windows-expected-failures.txt",
        "test/windows-collect-ignore.txt",
        "test/fixtures/npm-audit/report.json",
        "test/workflows/some_helper.py",
    ],
)
def test_shared_test_input_escalates(path: str) -> None:
    assert _resolved([path]) is None, f"{path} is shared input and must escalate"


def test_every_named_shared_helper_still_exists() -> None:
    """A rule pinned against a path that no longer exists protects nothing."""
    for name in ("conftest.py", "source_corpus.py", "spawn_test_helpers.py"):
        assert TEST_DIR.joinpath(name).is_file(), f"test/{name} vanished; update this gate"


# ── only a MODIFICATION qualifies: adds and deletes change the file SET ───────


@pytest.mark.parametrize("code", ["A", "D"])
def test_adding_or_deleting_a_leaf_escalates(clean_leaf: str, code: str) -> None:
    """The rule that closes the tree-scanning-gate class by construction.

    A presence gate, an ignore-list assertion, or "every module has a referencing
    test" all assert on the SET of test files. An in-place edit cannot change that
    set; an add, delete or rename can, so those run the full suite.
    """
    assert _resolved([clean_leaf], code) is None
    assert "not a modification" in _reason([clean_leaf], code)


def test_a_rename_is_seen_as_its_delete_and_add_not_as_an_edit() -> None:
    """`--no-renames` is why: an 'R' status would read as a single edit."""
    assert "--no-renames" in SCRIPT.read_text(encoding="utf-8")


# ── a test module another file reaches is shared input, not a leaf ────────────


def test_statically_imported_test_modules_escalate() -> None:
    """These are real cross-test imports in this repo, not hypotheticals."""
    for stem in ("test_chat_slack", "test_connections_warm", "test_error_code_contract"):
        path = f"test/{stem}.py"
        assert TEST_DIR.joinpath(f"{stem}.py").is_file(), f"{path} vanished; update this gate"
        assert _resolved([path]) is None, f"{path} is imported elsewhere and must escalate"


def test_reverse_import_scan_finds_a_function_body_import() -> None:
    """`test_chat_fork_error_codes` imports its gate INSIDE a test body.

    Anchoring the import pattern to column zero would call that module a leaf.
    """
    assert "test_error_code_contract" in mod.importers_of({"test_error_code_contract"}, REPO_ROOT)


def test_a_dynamically_imported_test_module_escalates() -> None:
    """The gap a reviewer found: `importlib.import_module` on a test module.

    Five files reach `test_slack_golden_transcript` that way. No import-statement
    pattern sees it, so the mention check -- which is spelling-independent -- is
    what keeps the module from being misclassified as a leaf.
    """
    stem = "test_slack_golden_transcript"
    assert TEST_DIR.joinpath(f"{stem}.py").is_file(), "fixture module vanished"
    assert (
        mod.importers_of({stem}, REPO_ROOT) == {}
    ), "if a static import appears, this test no longer covers the dynamic path"
    assert stem in mod.mentions_of({stem}, REPO_ROOT)
    assert _resolved([f"test/{stem}.py"]) is None


def test_the_mention_scan_leaves_a_clean_leaf_alone(clean_leaf: str) -> None:
    stem = Path(clean_leaf).stem
    assert mod.mentions_of({stem}, REPO_ROOT) == {}


# ── corpus gates: tests that read the test/ tree as data ──────────────────────


def test_corpus_gate_derivation_is_non_empty() -> None:
    """An empty derivation and a broken scan look identical; fail on empty."""
    assert mod.corpus_gates(REPO_ROOT), "no corpus gates derived, which cannot be true here"


@pytest.mark.parametrize(
    "gate",
    [
        "test/test_coverage_omit_contract.py",
        "test/test_jsondecodeerror_redundancy_ratchet.py",
        "test/test_ci_surface_tests.py",
        # The one the first version MISSED -- it reaches the tree via
        # `Path(__file__).resolve().parent` + `.glob(...)`, never a `/ "test"` join.
        "test/test_workflows_presence.py",
    ],
)
def test_known_corpus_gates_are_derived(gate: str) -> None:
    assert REPO_ROOT.joinpath(gate).is_file(), f"{gate} vanished; update this gate"
    assert gate in mod.corpus_gates(REPO_ROOT)


def test_the_missed_gate_needs_the_file_relative_rule(clean_leaf: str) -> None:
    """Mutation guard: the `/ "test"` join alone must not be enough to find it.

    This is the regression the reviewer caught. If someone narrows the derivation
    back to the join spelling, `test_workflows_presence` drops out and a diff that
    orphans a `workflows/` module merges green.
    """
    presence = REPO_ROOT.joinpath("test", "test_workflows_presence.py")
    text = presence.read_text(encoding="utf-8")
    assert not mod._JOINS_TEST_DIR.search(text), (
        'test_workflows_presence now writes a `/ "test"` join, so it no longer '
        "exercises the file-relative rule -- pick another file-relative gate"
    )
    assert mod._SCANS_A_DIR.search(text) and mod._REACHES_TEST_TREE.search(text)


def test_accepted_run_always_includes_the_corpus_gates(clean_leaf: str) -> None:
    resolved = _resolved([clean_leaf])
    assert isinstance(resolved, tuple)
    changed, gates = resolved
    assert set(mod.corpus_gates(REPO_ROOT)) - {clean_leaf} == set(gates)


def test_a_broken_corpus_scan_forfeits_the_reduction(
    clean_leaf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation guard: if the derivation returns nothing, escalate, never reduce."""
    monkeypatch.setattr(mod, "corpus_gates", lambda root: [])
    assert _resolved([clean_leaf]) is None


# ── the accepted case ─────────────────────────────────────────────────────────


def test_a_modified_leaf_is_accepted_and_is_a_real_reduction(clean_leaf: str) -> None:
    resolved = _resolved([clean_leaf])
    assert isinstance(resolved, tuple)
    changed, gates = resolved
    assert clean_leaf in changed
    assert clean_leaf not in gates, "the changed file must not also run as a gate"
    total = len(list(TEST_DIR.glob("test_*.py")))
    assert len(changed) + len(gates) < total / 5, (
        f"{len(changed) + len(gates)} targets against {total} test files is not a "
        "useful reduction"
    )


def test_empty_diff_escalates() -> None:
    assert _resolved([]) is None
    assert "empty" in _reason([])


def test_accepted_targets_pass_the_shared_admission_check(clean_leaf: str) -> None:
    """Targets become pytest argv, so they go through run_scoped_tests' validator."""
    from run_scoped_tests import validated_targets

    resolved = _resolved([clean_leaf])
    assert isinstance(resolved, tuple)
    changed, gates = resolved
    assert validated_targets(changed + gates, REPO_ROOT) == changed + gates


def test_repeat_is_more_than_one_by_default() -> None:
    """A single isolated pass proves nothing about the flake it gates."""
    assert mod.DEFAULT_REPEAT >= 2


def test_run_repeats_the_changed_files_but_runs_the_gates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gates are deterministic content assertions; repeating them buys nothing.

    At ~3 minutes a pass over ~158 files, repeating them would also dominate the
    run and undo the reduction.
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 0

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "validated_targets", lambda targets, root: list(targets))
    assert mod.run(["test/a.py"], ["test/g1.py", "test/g2.py"], 3) == 0
    assert len(calls) == 4, "expected one gate pass plus three changed-file passes"
    assert "test/g1.py" in calls[0] and "test/a.py" not in calls[0]
    for call in calls[1:]:
        assert "test/a.py" in call and "test/g1.py" not in call


def test_run_argv_disables_coverage_and_ends_option_parsing(clean_leaf: str) -> None:
    argv = mod.pytest_argv([clean_leaf])
    assert "--no-cov" in argv, "a subset's coverage is not comparable to the repo floor"
    assert "--" in argv, "option parsing must end before selector-provided paths"
    assert argv.index("--") < argv.index(clean_leaf)


# ── path form: the Windows shard caught this, so pin it on every platform ─────


def test_rel_posix_normalises_a_windows_style_path() -> None:
    """The only Linux-detectable form of the bug that broke the Windows shard.

    `str(PureWindowsPath(...).relative_to(...))` yields backslashes, which
    `validated_targets` refuses outright. Asserting on real `Path` objects would
    pass on Linux either way, so drive the helper with an explicitly Windows path.
    """
    win_root = PureWindowsPath(r"D:\a\KiroCrew\KiroCrew")
    win_file = win_root / "test" / "test_ai_agent_runner_coverage.py"
    assert str(win_file.relative_to(win_root)) == r"test\test_ai_agent_runner_coverage.py"
    assert mod._rel_posix(win_file, win_root) == "test/test_ai_agent_runner_coverage.py"


def test_no_derived_path_carries_a_backslash() -> None:
    """Vacuous on POSIX by construction; it is the Windows shard this guards."""
    for gate in mod.corpus_gates(REPO_ROOT):
        assert "\\" not in gate, f"corpus gate is not POSIX-form: {gate!r}"


# ── CI must actually use it, and must not orphan the coverage jobs ────────────


def test_ci_workflow_invokes_the_selector() -> None:
    """A selector nothing calls is a dead path that looks exactly like a live one."""
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    assert "leaf_test_scope.py" in ci, "ci.yml does not call the leaf-test selector"


def _posix_bash() -> str | None:
    """A real POSIX bash, never Windows' WSL launcher.

    On a GitHub Windows runner `shutil.which("bash")` resolves to
    `C:\\Windows\\System32\\bash.exe`, which is the WSL *launcher*: with no
    distribution installed it prints "Windows Subsystem for Linux has no installed
    distributions" and exits 1. The guard below reads that non-zero exit as the
    workflow step being broken, so it failed on Windows for a reason that has
    nothing to do with the step. Git Bash ships on those runners, so find it and
    skip only when no POSIX shell exists at all.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        # `<...>/Git/cmd/git.exe` and `<...>/Git/bin/git.exe` both sit one level
        # under the install root, so the sibling `bin/bash.exe` is the same hop.
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if root:
            candidates.append(str(Path(root) / "Git" / "bin" / "bash.exe"))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        # Any PATH bash is fine EXCEPT the System32 one, which is the WSL stub.
        if entry and "system32" not in entry.lower():
            candidates.append(str(Path(entry) / "bash.exe"))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def _leaf_scope_step_body() -> str:
    """The `run:` body of ci.yml's `Decide leaf-test scope` step, verbatim.

    Extracted rather than duplicated: a copy of the shell in this test would pass
    while the workflow's real body was broken, which is the failure mode the guard
    exists to catch.
    """
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    marker = "      - name: Decide leaf-test scope"
    assert marker in ci, "the leaf-scope step was renamed; update this guard"
    after = ci.split(marker, 1)[1]
    assert "run: |" in after, "leaf-scope step has no run: block"
    lines = after.split("run: |", 1)[1].splitlines()
    body: list[str] = []
    for line in lines:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:] if line.startswith("          ") else line)
    text = "\n".join(body).strip("\n")
    assert "GITHUB_OUTPUT" in text, "extracted the wrong block"
    return text


@pytest.mark.parametrize(
    ("full_run", "base_sha", "case"),
    [
        ("false", "", "push build to main -- no PR base"),
        ("true", "deadbeef", "PR carrying the ci-full-run label"),
    ],
)
def test_the_leaf_scope_step_survives_set_u_on_the_full_suite_paths(
    tmp_path: Path, full_run: str, base_sha: str, case: str
) -> None:
    """Both full-suite paths skip the assignments, so every var must be pre-set.

    A reviewer caught `gates` unset here: the step runs `set -uo pipefail`, the
    heredoc expands `$gates` unconditionally, and only the `else` branch assigned
    it -- so a push to main and every `ci-full-run` PR crashed the `changes` job
    and took all dependent jobs with it. This runs the REAL step body under the
    same shell flags GitHub Actions uses, so any future unbound variable in that
    step fails here instead of in CI.
    """
    bash = _posix_bash()
    if bash is None:
        pytest.skip("no POSIX bash on this host to run the workflow step body under")
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [bash, "-eo", "pipefail", "-c", _leaf_scope_step_body()],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "FULL_RUN": full_run,
            "BASE_SHA": base_sha,
            # Forward slashes: bash treats a backslash inside the quoted
            # redirection target as an escape, not a separator.
            "GITHUB_OUTPUT": out.as_posix(),
        },
        check=False,
    )
    assert proc.returncode == 0, (
        f"the leaf-scope step failed on the {case} path "
        f"(rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    assert "unbound variable" not in proc.stderr
    written = out.read_text(encoding="utf-8")
    assert "leaf_tests=false" in written, "a full-suite path must not claim a leaf run"
    assert "leaf_gates<<" in written, "the gates output must still be emitted"


def test_ci_keeps_an_escape_hatch_to_the_full_matrix() -> None:
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    assert "ci-full-run" in ci, "the full-matrix escape hatch must remain available"


def test_the_coverage_jobs_know_about_leaf_runs() -> None:
    """A reviewer found this: a leaf run uploads no coverage artifacts.

    `coverage-combine` is gated on `only_frontend != 'true'`, which is FALSE for a
    leaf diff (it is a backend diff), so without naming the leaf flag the job runs
    with nothing to combine and fails -- and `coverage-gate` then demands it be
    exactly 'success'. Both must recognise the leaf case.
    """
    ci = REPO_ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
    combine = ci.index("coverage-combine:")
    gate = ci.index("coverage-gate:")
    assert "leaf_tests" in ci[combine:gate], (
        "coverage-combine does not mention leaf_tests, so a leaf run would combine "
        "artifacts that were never uploaded"
    )
    assert "leaf_tests" in ci[gate:], (
        "coverage-gate does not mention leaf_tests, so it would demand a successful "
        "coverage-combine on a run that deliberately produced none"
    )
