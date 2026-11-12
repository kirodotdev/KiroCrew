"""Tests for the report-only recurring tool-failure detector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kiro_crew.failure_recurrence import find_recurring, normalize_error, scan, signature

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _row(
    *,
    session: str = "s1",
    at: datetime = NOW - timedelta(hours=1),
    outcome: str = "denied",
    tool_kind: str = "execute_bash",
    operation: str = "cat secret.txt",
    error: str = "hook_deny",
    event_type: str = "tool_invocation",
    **extra: object,
) -> dict:
    return {
        "event_type": event_type,
        "timestamp": at.isoformat(),
        "caller_identity": session,
        "outcome": outcome,
        "tool_kind": tool_kind,
        "operation": operation,
        "error": error,
        **extra,
    }


def test_a_shell_command_never_reaches_the_signature_or_the_finding():
    rows = [_row(session="a", operation="curl -H 'Authorization: tok123'"), _row(session="b")]
    [found] = find_recurring(rows)
    assert "curl" not in found.signature and "tok" not in found.signature
    assert found.tool == ""
    assert (found.hits, found.sessions) == (2, 2)


def test_mcp_failures_group_per_tool_name():
    rows = [
        _row(session=s, tool_kind="mcp", operation=tool, outcome="failed", error="execution_failed")
        for tool in ("work_ledger_read", "learn_add")
        for s in ("a", "b")
    ]
    found = find_recurring(rows)
    assert sorted(f.tool for f in found) == ["learn_add", "work_ledger_read"]


def test_mcp_dispatch_refusals_keep_the_tool_name():
    # mcp_shared_dispatch logs the server as tool_kind, with source="mcp".
    rows = [
        _row(
            session="",
            at=NOW - timedelta(days=d),
            tool_kind="github",
            operation=tool,
            source="mcp",
            outcome="rejected_excluded",
            error="",
        )
        for d, tool in ((1, "create_issue"), (2, "merge_pr"))
    ]
    assert find_recurring(rows) == []


def test_turn_limit_is_one_class_whatever_tool_was_running():
    rows = [
        _row(session="a", tool_kind="mcp", operation="fs_write", error="turn_limit"),
        _row(session="b", tool_kind="execute_bash", operation="ls", error="turn_limit"),
    ]
    [found] = find_recurring(rows)
    assert (found.tool, found.hits) == ("turn_limit", 2)


def test_failure_and_blocked_outcomes_count():
    assert signature(_row(outcome="failure")) is not None
    assert signature(_row(outcome="blocked")) is not None


def test_tool_name_and_rule_id_are_clipped():
    rows = [
        _row(session=s, tool_kind="mcp", operation="t" * 500, metadata={"rule_id": "r" * 500})
        for s in ("a", "b")
    ]
    [found] = find_recurring(rows)
    assert (len(found.tool), len(found.rule_id)) == (120, 120)


def test_one_session_burst_on_one_day_is_not_recurrence():
    rows = [_row(session="a", at=NOW - timedelta(minutes=m)) for m in range(1, 6)]
    assert find_recurring(rows) == []


def test_rows_without_a_session_recur_by_distinct_day():
    rows = [_row(session="", at=NOW - timedelta(days=d)) for d in (1, 2)]
    [found] = find_recurring(rows)
    assert (found.sessions, found.days) == (0, 2)


def test_only_failed_tool_invocations_count():
    assert signature(_row(outcome="completed")) is None
    assert signature(_row(event_type="governance_decision")) is None
    assert signature(_row(event_type="api_access")) is None
    assert signature(_row(error="feature disabled")) is None
    assert signature(_row(outcome="rejected_hook_deny")) is not None


def test_error_text_collapses_paths_ids_and_numbers():
    assert normalize_error("cannot read /home/u/a.txt after 3 tries (id deadbeef12)") == (
        "cannot read <path> after <n> tries (id <id>)"
    )


def test_error_text_keeps_wrapping_punctuation_and_collapses_spaces():
    assert (
        normalize_error("  denied:  C:\\x\\y,  12abc  hook_deny ")
        == "denied: <path>, <n>abc hook_deny"
    )


def test_rule_id_from_metadata_splits_signatures():
    rows = [
        _row(session=s, metadata={"rule_id": rule}) for rule in ("r1", "r2") for s in ("a", "b")
    ]
    assert sorted(f.rule_id for f in find_recurring(rows)) == ["r1", "r2"]


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple] = []

    def recent(self, limit=100, *, since=None, until=None):
        self.calls.append((limit, since, until))
        return [r for r in self.rows if since <= datetime.fromisoformat(r["timestamp"]) < until]


def test_scan_trends_against_the_window_before():
    week = timedelta(days=7)
    growing = [_row(session=s, error="busy") for s in ("a", "b", "c")]
    growing_before = [_row(session="z", error="busy", at=NOW - week - timedelta(days=1))]
    steady = [_row(session=s, error="slow") for s in ("a", "b")]
    steady_before = [
        _row(session=s, error="slow", at=NOW - week - timedelta(days=1)) for s in "xyz"
    ]
    fresh = [_row(session=s, error="gone") for s in ("a", "b")]
    log = _Log(growing + growing_before + steady + steady_before + fresh)

    trends = {f.error: (f.trend, f.previous_hits) for f in scan(log, now=NOW, window=week)}

    assert trends == {"busy": ("growing", 1), "slow": ("steady", 3), "gone": ("new", 0)}
    assert log.calls == [(50_000, NOW - 2 * week, NOW)]
