"""A kicked tick that exits 1 says so in its own log and summary.

The kick's step is ``continue-on-error``, so its job stays green and its exit code
reaches nobody. These tests pin the one place a failed kick can be seen: an
``::error::`` line and a section of the step summary, on a kick only, and only when
the tick failed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ci_runner_watchdog_helpers import REPO, WORKFLOW, wd

SCHEDULE_EVENT = "schedule"
NOT_FAILED = sorted(
    value
    for name, value in vars(wd).items()
    if name.startswith("OUTCOME_") and isinstance(value, str) and value not in wd.FAILED_OUTCOMES
)


def _main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
    *,
    event: str,
    outcomes: dict[int, str],
) -> tuple[int, str, str]:
    """Run the entry point over a tick that ended with ``outcomes``; code, stdout, summary."""
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setattr(
        wd, "GitHubApi", lambda *_a, **_k: SimpleNamespace(calls=0, rate_limit_remaining=None)
    )
    # The debounce is not what is under test: this kick is one that runs the full tick.
    monkeypatch.setattr(wd, "kick_is_redundant", lambda *_a, **_k: None)
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: ([], dict(outcomes)))
    summary = tmp_path / "summary.md"
    code = wd.main(["--repo", REPO, "--summary", str(summary)])
    return code, capsys.readouterr().out, summary.read_text(encoding="utf-8")


def _kick_errors(printed: str, failed: list[str]) -> list[str]:
    note = wd.kick_failure_note(failed)
    return [line for line in printed.splitlines() if line == f"::error::{note}"]


@pytest.mark.parametrize("outcome", sorted(wd.FAILED_OUTCOMES))
def test_a_kicked_tick_that_fails_says_so_in_its_log_and_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, outcome: str
) -> None:
    code, printed, summary = _main(
        monkeypatch, tmp_path, capsys, event=wd.KICK_EVENT, outcomes={1: outcome}
    )

    assert code == 1, "the step still fails; the workflow keeps its job green"
    assert len(_kick_errors(printed, [outcome])) == 1
    assert outcome in wd.kick_failure_note([outcome])
    note = wd._md(wd.kick_failure_note([outcome]))
    assert summary.count(note) == 1
    assert summary.index("## CI runner watchdog") < summary.index(
        note
    ), "after the tick's own summary"


@pytest.mark.parametrize("outcome", sorted(wd.FAILED_OUTCOMES))
def test_a_scheduled_tick_that_fails_carries_no_kick_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, outcome: str
) -> None:
    """A scheduled tick's red run is its own notice; the note would be false there."""
    code, printed, summary = _main(
        monkeypatch, tmp_path, capsys, event=SCHEDULE_EVENT, outcomes={1: outcome}
    )

    assert code == 1
    assert _kick_errors(printed, [outcome]) == []
    assert wd._md(wd.kick_failure_note([outcome])) not in summary


@pytest.mark.parametrize("outcomes", [{}, *({1: outcome} for outcome in NOT_FAILED)])
def test_a_kicked_tick_that_passes_carries_no_kick_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, outcomes: dict[int, str]
) -> None:
    code, printed, summary = _main(
        monkeypatch, tmp_path, capsys, event=wd.KICK_EVENT, outcomes=outcomes
    )

    assert code == 0
    assert "::error::" not in printed
    assert "Kicked tick failed" not in summary


def test_the_note_names_every_failed_outcome_of_the_tick_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    failed = sorted(wd.FAILED_OUTCOMES)[:3]
    outcomes = {
        index: outcome for index, outcome in enumerate(failed + failed[:1] + NOT_FAILED[:1])
    }

    code, printed, _summary = _main(
        monkeypatch, tmp_path, capsys, event=wd.KICK_EVENT, outcomes=outcomes
    )

    assert code == 1
    (line,) = _kick_errors(printed, failed)
    for outcome in failed:
        assert line.count(outcome) == 1
    assert NOT_FAILED[0] not in line


def test_the_note_is_true_of_the_workflow_it_describes() -> None:
    """The note says the job stays green on a kick: that is the workflow's own step setting."""
    body = wd._workflow_without_full_line_comments(WORKFLOW.read_text(encoding="utf-8"))
    assert f"continue-on-error: ${{{{ github.event_name == '{wd.KICK_EVENT}' }}}}" in body
    assert "continue-on-error" in wd.kick_failure_note(["x"])
