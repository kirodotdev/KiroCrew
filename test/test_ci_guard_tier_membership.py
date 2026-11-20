"""Contract tests for scripts/ci_guard_tier_membership.py and the CI step that runs it.

* MEMBERSHIP -- a file that joins the guard tier is reported, a file that leaves
  it while it still exists fails unless the file says so, and a file that says
  so while it is a member fails too.
* OUTPUT -- a file name cannot start a workflow command of its own, and a long
  list of failures stays inside the annotation limit.
* WIRING -- the gate runs on pull requests only, in a job whose checkout can
  reach the base, and can fail that job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_guard_tier_membership as membership

ROOT = Path(__file__).resolve().parents[1]

# Assembled, so no line of this file is itself a not-a-member line.
MARK = "# guard-tier: " + "not a member -- "
# Formatted, so this module's own text does not read as a test-tree join.
GATE = "FILES = ROOT / {!r}\n".format("test")
LEAF = "def test_x():\n    pass\n"


def _tree(root: Path, **files: str) -> Path:
    test_dir = root / "test"
    test_dir.mkdir()
    for name, text in files.items():
        (test_dir / f"{name}.py").write_text(text, encoding="utf-8")
    return root


def _run(root: Path, base: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    listed = root / "base-tier.txt"
    listed.write_bytes("".join(f"{path}\n" for path in base).encode("utf-8"))
    out = root / "summary.md"
    code = membership.main(["--root", str(root), "--base-tier", str(listed), "--summary", str(out)])
    return code, capsys.readouterr().out, out.read_bytes().decode("utf-8")


def test_a_file_that_joins_the_tier_is_a_notice_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, test_old=GATE, test_new=GATE)
    code, log, summary = _run(root, ["test/test_old.py"], capsys)
    assert code == 0
    assert [line for line in log.splitlines() if line.startswith("::")] == [
        "::notice title=Guard tier::1 test file(s) joined the guard tier; "
        "the job summary lists them."
    ]
    assert "| `test/test_new.py` | joined |" in summary
    assert "\r" not in summary


def test_a_deleted_file_leaves_the_tier_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, test_kept=GATE)
    code, log, summary = _run(root, ["test/test_kept.py", "test/test_removed.py"], capsys)
    assert code == 0 and "::error" not in log
    assert "| `test/test_removed.py` | left: deleted or renamed |" in summary


def test_a_file_edited_out_of_the_tier_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, test_kept=GATE, test_edited=LEAF)
    code, log, summary = _run(root, ["test/test_edited.py", "test/test_kept.py"], capsys)
    assert code == 1
    assert "::error file=test/test_edited.py,title=Guard tier::test/test_edited.py left" in log
    assert "| `test/test_edited.py` | left, unexplained (fails) |" in summary


@pytest.mark.parametrize(
    ("reason", "code"),
    [("it now asserts on one fixture only", 0), ("too short", 1), ("", 1)],
)
def test_only_a_reasoned_line_in_the_file_itself_excuses_leaving(
    reason: str, code: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, test_kept=GATE, test_edited=f"{MARK}{reason}\n{LEAF}")
    got, _log, summary = _run(root, ["test/test_edited.py", "test/test_kept.py"], capsys)
    assert got == code
    if code == 0:
        assert f"left on purpose: {reason}" in summary


def test_the_line_on_a_file_still_in_the_tier_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = f"{MARK}planted ahead of the edit it excuses\n{GATE}"
    root = _tree(tmp_path, test_member=planted)
    code, log, _summary = _run(root, ["test/test_member.py"], capsys)
    assert code == 1
    assert "::error file=test/test_member.py" in log and "Remove the line." in log


def test_a_file_name_cannot_start_a_workflow_command() -> None:
    hostile = "test/test_a\n::error::owned,b:c%.py"
    verdict = membership.Verdict([], [], {}, [hostile], [])
    (line,) = membership.workflow_commands(verdict)
    assert "\n" not in line and "\r" not in line
    assert line.startswith("::error file=test/test_a%0A%3A%3Aerror%3A%3Aowned%2Cb%3Ac%25.py,")
    row = membership.summary(verdict, 1, 0, "a | base").splitlines()
    assert sum(1 for text in row if text.startswith("| `")) == 1
    assert "a \\| base" in row[2]


def test_failures_past_the_annotation_limit_are_counted_not_dropped() -> None:
    paths = [f"test/test_{index:02d}.py" for index in range(membership.MAX_ANNOTATIONS + 3)]
    lines = membership.workflow_commands(membership.Verdict([], [], {}, paths, []))
    assert len(lines) == membership.MAX_ANNOTATIONS + 1
    assert lines[-1].startswith("::error title=Guard tier::3 more file(s) failed")
    table = membership.summary(membership.Verdict([], [], {}, paths, []), 13, 0, "base")
    assert all(f"`{path}`" in table for path in paths)


def test_no_base_to_compare_with_is_an_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, test_kept=GATE)
    monkeypatch.setattr(
        membership.ratchet_scope, "change_merge_base", lambda *a, **k: (None, "shallow clone")
    )
    assert membership.main(["--root", str(root)]) == 2
    assert "shallow clone" in capsys.readouterr().err


# ── Wiring ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["jobs"]


def _step(job: dict[str, Any], name_part: str) -> dict[str, Any]:
    (step,) = [s for s in job["steps"] if name_part in s.get("name", "")]
    return step


def test_the_membership_gate_runs_on_pull_requests_with_a_reachable_base(
    jobs: dict[str, Any],
) -> None:
    job = jobs["backend-ratchets"]
    step = _step(job, "guard-tier membership")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "scripts/ci_guard_tier_membership.py" in step["run"]
    assert '--summary "$GITHUB_STEP_SUMMARY"' in step["run"]
    assert not step.get("continue-on-error")
    (checkout,) = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["fetch-depth"] == 0


def test_the_membership_gate_expands_no_expression_in_its_script(jobs: dict[str, Any]) -> None:
    assert "${{" not in _step(jobs["backend-ratchets"], "guard-tier membership")["run"]
