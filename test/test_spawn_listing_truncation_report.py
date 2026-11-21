"""``GET /api/spawn`` reports a truncated persisted half once per change.

The dashboard polls the listing, so a steady truncated state must not repeat the
WARNING on every poll: it is a WARNING when truncation starts or the scan window
first saturates, DEBUG while it holds, and one INFO line when the listing is
whole again. An internal caller's session-scoped listing is not remembered.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import subagent_persistence
from kiro_crew.dashboard.handlers import messaging

_TRUNCATED = "persisted spawn listing truncated"
_COMPLETE = "persisted spawn listing complete again"


@pytest.fixture
def reports(monkeypatch):
    """Call the reporter and return the ``(level, message)`` lines it logged.

    The package logger does not propagate to root, so caplog cannot be relied on;
    the handler's own logger calls are recorded instead.
    """
    monkeypatch.setattr(messaging, "_persisted_listing_reports", {})
    lines: list[tuple[int, str]] = []
    for name, level in (
        ("warning", logging.WARNING),
        ("info", logging.INFO),
        ("debug", logging.DEBUG),
    ):
        monkeypatch.setattr(
            messaging.logger,
            name,
            lambda msg, *a, _level=level, **k: lines.append((_level, str(msg) % a)),
        )

    def _report(overflow: int, lower_bound: bool = False, scope: str | None = ""):
        lines.clear()
        messaging._report_persisted_truncation(overflow, lower_bound, scope=scope)
        return [line for line in lines if line[1].startswith((_TRUNCATED, _COMPLETE))]

    return _report


def _levels(lines: list) -> list[int]:
    return [level for level, _ in lines]


def test_a_steady_truncation_warns_once_then_logs_at_debug(reports) -> None:
    first = reports(1)
    assert _levels(first) == [logging.WARNING]
    assert "1 eligible run(s) past the" in first[0][1]
    for overflow in (1, 15, 3):
        later = reports(overflow)
        assert _levels(later) == [logging.DEBUG]
        assert f"{overflow} eligible run(s)" in later[0][1]


def test_a_listing_whole_again_logs_one_info_line_then_nothing(reports) -> None:
    reports(4)
    assert reports(0) == [(logging.INFO, _COMPLETE)]
    assert reports(0) == []
    assert _levels(reports(2)) == [logging.WARNING]


def test_an_untruncated_listing_never_logs(reports) -> None:
    assert reports(0) == []


def test_the_scan_window_saturating_warns_once_even_while_truncated(reports) -> None:
    reports(3)
    saturated = reports(0, lower_bound=True)
    assert _levels(saturated) == [logging.WARNING]
    assert "at least 0 eligible run(s)" in saturated[0][1]
    assert "scan window saturated" in saturated[0][1]
    assert _levels(reports(2, lower_bound=True)) == [logging.DEBUG]
    # Leaving the saturated window is no new shortfall to warn about.
    assert _levels(reports(2)) == [logging.DEBUG]
    assert _levels(reports(2, lower_bound=True)) == [logging.WARNING]


def test_each_listing_scope_keeps_its_own_report(reports) -> None:
    assert _levels(reports(2, scope="")) == [logging.WARNING]
    assert _levels(reports(2, scope="some-app")) == [logging.WARNING]
    assert reports(0, scope="some-app") == [(logging.INFO, _COMPLETE)]
    assert _levels(reports(2, scope="")) == [logging.DEBUG]


def test_the_remembered_scopes_are_bounded_and_forget_the_stalest(reports, monkeypatch) -> None:
    monkeypatch.setattr(messaging, "_PERSISTED_LISTING_REPORT_SCOPES", 2)
    reports(1, scope="app-a")
    reports(1, scope="app-b")
    # A repeat refreshes app-a, so app-b is the stalest when app-c arrives.
    assert _levels(reports(1, scope="app-a")) == [logging.DEBUG]
    reports(1, scope="app-c")
    assert list(messaging._persisted_listing_reports) == ["app-a", "app-c"]
    # The forgotten scope warns afresh, which is the cost of forgetting it.
    assert _levels(reports(1, scope="app-b")) == [logging.WARNING]
    assert len(messaging._persisted_listing_reports) == 2


def test_an_oversized_scope_is_never_kept_and_warns_every_time(reports) -> None:
    longest = "a" * messaging._PERSISTED_LISTING_SCOPE_MAX_LEN
    assert _levels(reports(1, scope=longest)) == [logging.WARNING]
    assert _levels(reports(1, scope=longest)) == [logging.DEBUG]
    oversized = longest + "b"
    for _ in range(2):
        assert _levels(reports(1, scope=oversized)) == [logging.WARNING]
    assert reports(0, scope=oversized) == []
    assert list(messaging._persisted_listing_reports) == [longest]


def test_an_unlistable_runs_folder_reads_as_unread_not_empty(monkeypatch) -> None:
    def refuse(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(subagent_persistence.os, "scandir", refuse)
    result = subagent_persistence.read_panel_records(keep=5, max_age_secs=60)
    assert result.records == [] and result.unread


def test_a_missing_runs_folder_is_an_empty_population(monkeypatch) -> None:
    def missing(_path):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(subagent_persistence.os, "scandir", missing)
    result = subagent_persistence.read_panel_records(keep=5, max_age_secs=60)
    assert result == subagent_persistence.PanelRecords([], 0, False)
    assert not result.unread


def test_an_internal_listing_warns_every_time_and_is_not_remembered(reports) -> None:
    for _ in range(3):
        assert _levels(reports(2, scope=None)) == [logging.WARNING]
    assert reports(0, scope=None) == []
    assert messaging._persisted_listing_reports == {}
