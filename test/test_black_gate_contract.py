"""The black gate must be real, and the docs must describe the gate that exists.

This repository ran for a long time with `black --check` commented out in CI while
`AGENTS.md` listed `black src/kiro_crew test` as a gate to run before committing.
That is a worse failure than a missing gate: following the documented command
reformats ~95,800 lines across 1,420 pre-existing files, so a contributor either
buries their own diff or knowingly skips a documented step. Both happened.

These tests pin the two halves that have to stay true together -- CI enforces
black, and no document tells anyone to run it in the form that hurts.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_black_formatting.py"
BASELINE = ROOT / ".github" / "black-baseline.txt"
CI = ROOT / ".github" / "workflows" / "ci.yml"

SPEC = importlib.util.spec_from_file_location("check_black_formatting", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _lint_steps() -> list[dict]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        steps = job.get("steps") or []
        if any("isort --check-only" in str(step.get("run", "")) for step in steps):
            return steps
    raise AssertionError("ci.yml has no job running isort --check-only")


def test_ci_actually_runs_the_black_gate() -> None:
    # The whole point: a gate that exists only as a comment is not a gate.
    runs = [str(step.get("run", "")) for step in _lint_steps()]
    assert any(
        "scripts/check_black_formatting.py" in run for run in runs
    ), "ci.yml's lint job no longer runs the black gate"


def test_ci_does_not_run_a_bare_repo_wide_black_check() -> None:
    # A bare `black --check src/ test/` fails on 1,420 pre-existing files, so
    # anyone re-enabling it would have to neuter the gate again to get CI green.
    for run in (str(step.get("run", "")) for step in _lint_steps()):
        if "black" not in run or "check_black_formatting" in run:
            continue
        assert "--check" not in run, (
            f"ci.yml runs a bare black --check, which cannot pass on this "
            f"repository's existing files: {run!r}"
        )


@pytest.mark.parametrize(
    "doc",
    [
        "AGENTS.md",
        "docs/system-specs/common/code-style.md",
        "docs/system-specs/common/testing-conventions.md",
    ],
)
def test_no_document_tells_a_contributor_to_reformat_the_whole_tree(doc: str) -> None:
    # `black src/kiro_crew test` is the exact command that buries a diff under
    # ~95,800 lines of unrelated churn. AGENTS.md carried it for months.
    text = (ROOT / doc).read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("black "):
            continue
        assert "src/kiro_crew test" not in stripped and "src/ test/" not in stripped, (
            f"{doc} instructs a repo-wide reformat: {stripped!r}. Point at "
            "scripts/check_black_formatting.py and per-file formatting instead."
        )


def test_the_baseline_holds_relative_paths_to_files_that_exist() -> None:
    # An absolute path matches nothing on another checkout, so the gate would
    # report every baselined file as a new offender the moment it ran on CI.
    entries = gate._read_baseline(BASELINE)
    assert entries, "the baseline is empty; the gate would demand a full reformat"
    for entry in entries:
        assert not Path(entry).is_absolute(), f"{entry} is absolute"
        assert (ROOT / entry).is_file(), f"{entry} is in the baseline but does not exist"


def test_refreshing_the_baseline_can_only_delete_lines(tmp_path: Path) -> None:
    # This is the rule that keeps the gate from becoming a formality: if a
    # refresh could ADD a path, the fix for a red gate would be to run the
    # refresh, and unformatted code would land unchallenged forever.
    baseline = tmp_path / "black-baseline.txt"
    gate._write_baseline(baseline, {"kept.py", "graduated.py", "vanished.py"})

    # "kept" is still unformatted; "graduated" is now clean; "vanished" is gone;
    # "brand_new.py" is unformatted but unlisted -- a new offender.
    gate._write_baseline(baseline, set(gate._read_baseline(baseline)) & {"kept.py"})

    remaining = set(gate._read_baseline(baseline))
    assert remaining == {"kept.py"}
    assert "brand_new.py" not in remaining


def test_only_this_changes_files_can_be_its_offenders() -> None:
    # CI evaluates a PR's MERGE ref, so an unscoped gate reports files the base
    # branch merged after the baseline was taken -- a PR's colour would depend on
    # other people's formatting hygiene. Observed three times while landing this
    # gate, once per rebase, which is why the scope is pinned rather than trusted.
    source = SCRIPT.read_text(encoding="utf-8")
    assert "unlisted & changed" in source
    # The resolver itself lives in scripts/ratchet_scope.py, which four merge-ref
    # ratchets share: a private copy per gate is how they would come to
    # disagree about the same added line. So this gate must DELEGATE, and the shape
    # assertions below hold against the module that owns the answer.
    assert "ratchet_scope.py" in source, "the gate no longer delegates its scope resolution"
    assert (
        "def _changed_paths" not in source
    ), "the gate has grown a private copy of the shared resolver again"
    scope_source = (ROOT / "scripts" / "ratchet_scope.py").read_text(encoding="utf-8")
    # Both diff shapes: local branch tip, and CI's merge commit whose parents are
    # base and PR head.
    assert '"HEAD^1", "HEAD"' in scope_source, "the merge-vs-first-parent diff is the exact one"
    assert '"HEAD^1", "HEAD^2"' in scope_source
    assert "{base}...HEAD" in scope_source
    # The merge diffs are only exact when HEAD^1 IS the base; a local
    # `git merge origin/main` puts the feature tip first and the same diff
    # would scope to what main brought in. The resolver must verify which
    # parent the base can reach before trusting the merge shape -- and the
    # probe must actually GATE the merge attempts, not merely exist.
    assert (
        '"merge-base", "--is-ancestor", "HEAD^1", base' in scope_source
    ), "the resolver no longer verifies HEAD^1 is the base before taking the merge diff"
    assert "if is_merge and _first_parent_is_base():" in scope_source
    # And it must fail CLOSED when the base is unresolvable: judge everything
    # rather than nothing.
    assert "new_offenders = sorted(unlisted)" in source
    assert "undeterminable (judging the whole tree)" in scope_source
    # And it must SAY which scope it used. The silent fallback is what hid the
    # earlier misbehaviour through two CI rounds.
    assert 'print(f"black gate scope: {scope_label}"' in source


def test_no_operation_can_add_a_path_to_the_baseline() -> None:
    # The rule that keeps the gate from being a formality. With the verdict scoped
    # to the caller's own files there is no reason to absorb a path, so
    # the add-capable operation is gone entirely rather than merely guarded.
    source = SCRIPT.read_text(encoding="utf-8")
    assert "snapshot" not in source.lower(), "an add-capable operation came back"
    assert "survivors = baseline & unformatted" in source
    assert "_write_baseline(args.baseline, survivors)" in source


def test_the_lint_job_fetches_enough_history_to_scope_the_diff() -> None:
    # The scoping is only exact if the checkout has both merge parents. depth 1
    # leaves HEAD^2 unreachable, the scope silently falls back to the whole tree,
    # and the gate is flaky again in a way nothing else would report.
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["backend-lint"]
    checkout = next(step for step in job["steps"] if "checkout" in str(step.get("uses", "")))
    depth = (checkout.get("with") or {}).get("fetch-depth")
    # depth 2 was tried and was NOT enough: a shallow clone truncates parent info
    # and leaves no base ref, so the scope silently fell back to the whole tree and
    # the gate reported a file the base branch had merged. Full history is the only
    # shape proven to work here.
    assert depth == 0, (
        f"backend-lint checkout fetch-depth is {depth!r}; the black gate needs "
        "full history (0) to resolve this change's own diff"
    )


def test_black_exiting_one_with_no_findings_is_not_a_clean_tree() -> None:
    # `python -m black` exits 1 both for "would reformat" and for "no module
    # named black", so an environment without black would otherwise parse as a
    # fully formatted tree -- and --update-baseline would write an EMPTY baseline
    # over every recorded path, destroying the ratchet irrecoverably.
    source = SCRIPT.read_text(encoding="utf-8")
    assert "if proc.returncode == 1 and not found:" in source


class _FakeScope:
    def __init__(self, changed: set[str] | None) -> None:
        self._changed = changed

    def changed_paths(self) -> tuple[set[str] | None, str]:
        return self._changed, "fake scope"


def _stub_black_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Report `version` for black only; other lookups reach the real function,
    since a narrow stub would hijack pytest's own plugin machinery too."""
    real = importlib.metadata.version

    def _version(name: str) -> str:
        return version if name == "black" else real(name)

    monkeypatch.setattr(gate.importlib.metadata, "version", _version)


def _pinned_version() -> str:
    match = gate.BLACK_PIN.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert match, "pyproject.toml no longer carries a black== pin for the gate to read"
    return match.group(1)


def test_a_prune_under_the_wrong_black_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prune revokes an exemption for good, so a wrong black must not write one.
    baseline = tmp_path / "black-baseline.txt"
    gate._write_baseline(baseline, {"kept.py", "graduated.py"})
    before = baseline.read_bytes()

    _stub_black_version(monkeypatch, "0.0.0-not-the-pin")
    # Reached only if the guard runs after the scan, which is the ordering bug.
    monkeypatch.setattr(
        gate, "_unformatted", lambda targets: pytest.fail("black ran before the version check")
    )

    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--update-baseline", "--baseline", str(baseline)])

    message = str(excinfo.value)
    # Both versions, so the reader knows which one to change.
    assert "0.0.0-not-the-pin" in message
    assert _pinned_version() in message
    assert baseline.read_bytes() == before, "a refused prune rewrote the baseline anyway"


def test_a_prune_refuses_when_pyproject_carries_no_black_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No pin means nothing to verify against, so it fails closed instead.
    baseline = tmp_path / "black-baseline.txt"
    gate._write_baseline(baseline, {"kept.py"})
    before = baseline.read_bytes()

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.black]\n", encoding="utf-8")
    monkeypatch.setattr(
        gate, "_unformatted", lambda targets: pytest.fail("black ran before the version check")
    )

    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--update-baseline", "--baseline", str(baseline)])

    assert "no black== pin" in str(excinfo.value)
    assert baseline.read_bytes() == before, "a refused prune rewrote the baseline anyway"


def test_a_prune_under_the_pinned_black_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other arm: a matching version must not turn the guard into a wall.
    baseline = tmp_path / "black-baseline.txt"
    gate._write_baseline(baseline, {"kept.py", "graduated.py"})

    _stub_black_version(monkeypatch, _pinned_version())
    monkeypatch.setattr(gate, "_unformatted", lambda targets: {"kept.py"})

    assert gate.main(["--update-baseline", "--baseline", str(baseline)]) == 0
    assert set(gate._read_baseline(baseline)) == {"kept.py"}


def test_the_read_only_gate_still_runs_under_a_mismatched_black(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate records nothing, so a skew there must not block a drifted black.
    baseline = tmp_path / "black-baseline.txt"
    gate._write_baseline(baseline, {"kept.py"})
    # A baseline entry whose file is gone is reported as stale, so the entry must exist.
    (tmp_path / "kept.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "_load_scope", lambda: _FakeScope(None))

    _stub_black_version(monkeypatch, "0.0.0-not-the-pin")
    monkeypatch.setattr(gate, "_unformatted", lambda targets: {"kept.py"})

    assert gate.main(["--baseline", str(baseline)]) == 0


# --- Scoped work: black runs on the changed files, not the whole tree ---------
#
# Fork PRs always land on a hosted runner, where black over the whole tree took
# ~13 min of a 25 min job and backend-lint timed out about one run in three. The
# verdict was already scoped to the change; these pin that the WORK is too, and
# that the conditions which must still see the whole tree do, and that editing the
# gate itself does not.


def _scoped_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    changed: set[str] | None,
    unformatted: set[str],
    baseline: list[str],
    files: list[str],
) -> tuple[list[tuple[str, ...]], list[str]]:
    """Run gate.main() against a fake tree; return (black target calls, argv)."""
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    baseline_file = tmp_path / "baseline.txt"
    baseline_file.write_text("".join(f"{entry}\n" for entry in baseline), encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_unformatted(targets: tuple[str, ...]) -> set[str]:
        calls.append(tuple(targets))
        # Black only reports files it was pointed at.
        return {
            path
            for path in unformatted
            if any(path == t or path.startswith(t.rstrip("/") + "/") for t in targets)
        }

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "_unformatted", fake_unformatted)
    monkeypatch.setattr(gate, "_load_scope", lambda: _FakeScope(changed))
    return calls, ["--baseline", str(baseline_file)]


def test_scoped_targets_are_only_the_changed_python_files_under_the_gated_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("src/a.py", "test/b.py", "src/notes.md", "scripts/tool.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    changed = {"src/a.py", "test/b.py", "src/notes.md", "scripts/tool.py", "src/deleted.py"}
    # Not python, outside src/ and test/, and deleted files are all left out.
    assert gate._scoped_targets(changed) == ("src/a.py", "test/b.py")
    assert gate._scoped_targets(set()) == ()


def test_stub_files_are_scoped_in_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # black formats .pyi as well as .py; a directory scan found stubs, so the
    # explicit file list must not drop them.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "types.pyi").write_text("x: int\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    assert gate._scoped_targets({"src/types.pyi"}) == ("src/types.pyi",)


def test_an_unknown_scope_still_judges_the_whole_tree() -> None:
    assert gate._scoped_targets(None) is None


@pytest.mark.parametrize("trigger", sorted(gate.FULL_TREE_TRIGGERS))
def test_changing_a_black_config_or_baseline_file_forces_the_whole_tree(trigger: str) -> None:
    assert gate._scoped_targets({"src/a.py", trigger}) is None


@pytest.mark.parametrize(
    "gate_script", ["scripts/check_black_formatting.py", "scripts/bounded_black.py"]
)
def test_editing_the_gate_itself_stays_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate_script: str
) -> None:
    # The scoping change's own heads timed out backend-lint three times out of
    # three because the gate script was a whole-tree trigger: a PR that edits the
    # gate could never pass the gate on a hosted runner. Gate bugs are this file's
    # job and the main-branch audit's, not a 15-minute whole-tree black run's.
    assert (ROOT / gate_script).is_file(), gate_script
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    assert gate._scoped_targets({"src/a.py", gate_script}) == ("src/a.py",)


def test_every_full_tree_trigger_is_a_real_file() -> None:
    # A renamed trigger would silently stop forcing the whole tree.
    for trigger in gate.FULL_TREE_TRIGGERS:
        assert (ROOT / trigger).is_file(), trigger


def test_a_pr_runs_black_only_on_its_changed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/a.py", "README.md"},
        unformatted=set(),
        baseline=["src/old.py"],
        files=["src/a.py", "src/old.py", "src/other.py"],
    )
    assert gate.main(argv) == 0
    assert calls == [("src/a.py",)], "black was pointed at more than the changed file"


def test_a_pr_touching_no_python_never_starts_black(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"README.md"},
        unformatted=set(),
        baseline=[],
        files=["README.md"],
    )
    assert gate.main(argv) == 0
    assert calls == []


def test_a_changed_unformatted_file_outside_the_baseline_is_still_an_offender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/a.py"},
        unformatted={"src/a.py"},
        baseline=[],
        files=["src/a.py"],
    )
    assert gate.main(argv) == 1
    assert "src/a.py" in capsys.readouterr().out


def test_an_untouched_unformatted_file_is_not_this_prs_offender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/a.py"},
        unformatted={"src/elsewhere.py"},
        baseline=[],
        files=["src/a.py", "src/elsewhere.py"],
    )
    assert gate.main(argv) == 0


def test_a_touched_baselined_file_that_became_clean_must_be_pruned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/old.py"},
        unformatted=set(),
        baseline=["src/old.py"],
        files=["src/old.py"],
    )
    assert gate.main(argv) == 1
    assert "src/old.py" in capsys.readouterr().out


def test_an_untouched_baselined_file_is_not_checked_so_it_cannot_block_a_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Whole-tree graduation is the main-branch audit's job (RATCHET_SCOPE_WHOLE_TREE).
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/a.py"},
        unformatted=set(),
        baseline=["src/old.py"],
        files=["src/a.py", "src/old.py"],
    )
    assert gate.main(argv) == 0


def test_a_baseline_entry_for_a_file_that_no_longer_exists_is_still_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A delete or rename shows only the new path in `git diff --name-only`, so the
    # old entry is not in the changed set and would otherwise go stale unseen.
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"src/renamed.py"},
        unformatted=set(),
        baseline=["src/gone.py"],
        files=["src/renamed.py"],
    )
    assert gate.main(argv) == 1
    assert "src/gone.py" in capsys.readouterr().out


def test_the_whole_tree_is_measured_when_the_scope_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed=None,
        unformatted=set(),
        baseline=[],
        files=["src/a.py"],
    )
    assert gate.main(argv) == 0
    assert calls == [gate.DEFAULT_TARGETS]


def test_a_black_pin_change_measures_the_whole_tree_and_catches_graduation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A new pin can make an untouched baselined file clean; only the whole tree sees it.
    calls, argv = _scoped_gate(
        monkeypatch,
        tmp_path,
        changed={"pyproject.toml"},
        unformatted=set(),
        baseline=["src/old.py"],
        files=["pyproject.toml", "src/old.py"],
    )
    assert gate.main(argv) == 1
    assert calls == [gate.DEFAULT_TARGETS]
    assert "src/old.py" in capsys.readouterr().out
