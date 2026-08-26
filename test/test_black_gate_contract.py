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
    # Both diff shapes: local branch tip, and CI's merge commit whose parents are
    # base and PR head.
    assert '"HEAD^1", "HEAD"' in source, "the merge-vs-first-parent diff is the exact one"
    assert '"HEAD^1", "HEAD^2"' in source
    assert "{base}...HEAD" in source
    # And it must fail CLOSED when the base is unresolvable: judge everything
    # rather than nothing.
    assert "new_offenders = sorted(unlisted)" in source
    # And it must SAY which scope it used. The silent fallback is what hid the
    # earlier misbehaviour through two CI rounds.
    assert 'print(f"black gate scope: {scope_label}"' in source


def test_no_operation_can_add_a_path_to_the_baseline() -> None:
    # The rule that keeps the gate from being a formality. With the verdict scoped
    # to the caller's own files there is no longer any reason to absorb a path, so
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
