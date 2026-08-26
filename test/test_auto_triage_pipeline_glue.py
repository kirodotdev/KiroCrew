"""Pins the glued-plus-torn recovery path in the auto-triage-pipeline fold.

Separate from the main suite because it targets one specific data-loss shape
found by review: a well-formed record glued to a TORN one on the same line. The
recovery walk used to abandon the whole line the moment any piece failed to
decode, which threw away the intact leading record along with the broken tail --
and that combination is exactly what a glued write followed by a mid-append tail
produces in a log that is being read while it is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_triage_pipeline.backend import pipeline_fold as fold


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(fold, "_workspace", lambda: root)
    return root


def _event(issue: int, name: str, ts: str = "2026-08-24T10:00:00Z") -> str:
    """One event line. `ts` is a parameter because ORDER is load-bearing for the
    residency tests: whether an item is inside a step is decided by its LAST
    transition, so two events need distinguishable timestamps."""
    return json.dumps({"ts": ts, "event": name, "issue": issue})


def test_intact_record_survives_being_glued_to_a_torn_one(workspace: Path) -> None:
    """A good record glued in front of a torn tail must still be counted."""
    good = _event(4242, "pr_opened")
    log = workspace / fold.AUDIT_LOG_NAME
    # One clean line, then a line carrying a COMPLETE record glued to a truncated
    # one -- the shape a missing trailing newline plus a mid-append read produces.
    log.write_text(
        _event(1111, "implement_start") + "\n" + good + '{"ts": "2026-08-24T10:05:00Z", "eve\n',
        encoding="utf-8",
    )

    result = fold.fold_pipeline()

    # The intact pr_opened must be folded, not discarded with its broken neighbour.
    verify = next(s for s in result.steps if s.key == "verify")
    assert verify.entered == 1, "the recovered pr_opened was dropped with the torn tail"
    # And the unreadable remainder is still reported, exactly once.
    assert result.unparseable == 1
    assert result.total_events == 2


def test_torn_line_alone_is_reported_and_loses_nothing_else(workspace: Path) -> None:
    """A line that is only a torn fragment yields one unparseable and no records."""
    log = workspace / fold.AUDIT_LOG_NAME
    log.write_text(_event(1111, "implement_start") + "\n" + '{"ts": "2026-08-2\n', encoding="utf-8")

    result = fold.fold_pipeline()

    assert result.total_events == 1
    assert result.unparseable == 1


def test_glue_inside_a_string_value_is_not_a_split_point(workspace: Path) -> None:
    """A literal '}{' inside a string must not be treated as a record boundary."""
    tricky = json.dumps(
        {
            "ts": "2026-08-24T10:00:00Z",
            "event": "implement_start",
            "issue": 7,
            "details": {"detail": "brace pair }{ inside a value"},
        }
    )
    second = _event(8, "implement_start")
    log = workspace / fold.AUDIT_LOG_NAME
    log.write_text(tricky + second + "\n", encoding="utf-8")

    result = fold.fold_pipeline()

    assert result.unparseable == 0
    assert result.total_events == 2
    implement = next(s for s in result.steps if s.key == "implement")
    assert implement.entered == 2


def test_a_re_entered_item_is_in_flight_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item that entered, exited, and entered AGAIN is inside the step.

    In-flight used to be `entered - left` over sets, which is blind to ORDER: once an
    item appeared on the departure side it stayed gone forever. Re-entry is routine
    here -- the implement step logs 197 starts across 113 distinct items -- so most
    re-worked items were missing from the count operators read to find a stall.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(fold, "_workspace", lambda: root)
    (root / fold.AUDIT_LOG_NAME).write_text(
        "\n".join(
            [
                _event(4242, "implement_start", "2026-08-24T10:00:00Z"),
                _event(4242, "pr_opened", "2026-08-24T11:00:00Z"),
                # Re-worked: started again after the pull request was opened.
                _event(4242, "implement_start", "2026-08-24T12:00:00Z"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    implement = next(s for s in fold.fold_pipeline().steps if s.key == "implement")

    assert implement.in_flight == 1, "a re-entered item must count as in flight again"
    # And the event/throughput split still holds: two starts, one item.
    assert implement.entered == 2
    assert implement.distinct_entered == 1


def test_l1_lists_a_re_entered_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1's resident list must agree with L0's in-flight count.

    They answer the same question, so a step card reading 1 while its table shows no
    rows would be the view contradicting itself.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(fold, "_workspace", lambda: root)
    monkeypatch.setattr(fold, "_issue_cache_dir", lambda owner, repo: tmp_path / "cache")
    (root / fold.AUDIT_LOG_NAME).write_text(
        "\n".join(
            [
                _event(4242, "implement_start", "2026-08-24T10:00:00Z"),
                _event(4242, "pr_opened", "2026-08-24T11:00:00Z"),
                _event(4242, "implement_start", "2026-08-24T12:00:00Z"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = fold.list_step_items("implement", owner="acme", repo="widget")

    assert [r.number for r in rows] == [4242]


def test_an_item_that_only_exited_is_not_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse, so the fix is not merely 'everything counts as inside'."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(fold, "_workspace", lambda: root)
    (root / fold.AUDIT_LOG_NAME).write_text(
        "\n".join(
            [
                _event(99, "implement_start", "2026-08-24T10:00:00Z"),
                _event(99, "pr_opened", "2026-08-24T11:00:00Z"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    implement = next(s for s in fold.fold_pipeline().steps if s.key == "implement")
    assert implement.in_flight == 0


def test_every_declared_numeric_column_exists_on_the_session_payload() -> None:
    """The column names the view gates on must match the payload's own keys.

    ``populated_columns`` tells the frontend which numeric columns carry data, and
    the frontend renders only those. If a payload key is ever renamed without
    updating ``SESSION_NUMERIC_COLUMNS``, the column silently disappears from
    every table instead of failing anywhere -- a rename would ship as missing data.
    This makes that drift a test failure at the point the two are supposed to
    agree.
    """
    payload = fold.SessionRow(slot="s").to_dict()
    missing = [c for c in fold.SESSION_NUMERIC_COLUMNS if c not in payload]
    assert not missing, f"declared columns absent from the payload: {missing}"
