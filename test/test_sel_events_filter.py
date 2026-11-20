"""Field filters on ``kirocrew security events``: ``select_events`` and its CLI caller.

The command picks rows by operation, outcome, source and caller (exact match) beside the
time window. Every case that runs a real log writes through ``SecurityEventLog``. The
cases that pin today's behavior (no new flag, same output) pass on main.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    _make_event,
    log_fixture,
    reset_singleton,
    sel_dir_fixture,
)

import kiro_crew.cli_commands as cc
from kiro_crew import cli_parser_admin

_BASE = datetime(2026, 8, 21, tzinfo=timezone.utc)

#: (operation, outcome, caller, source), oldest first; one row per minute.
_ROWS = [
    ("execute_bash", "approved", "alice", "dashboard"),
    ("execute_bash", "denied", "alice", "dashboard"),
    ("fs_write", "denied", "bob", "slack"),
    ("execute_bash", "denied", "bob", "slack"),
    ("fs_write", "approved", "alice", "cron"),
    ("execute_bash", "denied", "alice", "cron"),
]


def _seed(log) -> list[dict]:
    """Write ``_ROWS`` and return what the log holds, newest first."""
    for i, (operation, outcome, caller, source) in enumerate(_ROWS):
        log.log(
            _make_event(
                event_id=f"evt{i}",
                timestamp=(_BASE + timedelta(minutes=i)).isoformat(),
                operation=operation,
                outcome=outcome,
                caller_identity=caller,
                source=source,
            )
        )
    return log.recent(limit=100)


def _ids(rows: list[dict]) -> list[str]:
    return [row["event_id"] for row in rows]


def _pick(log, **overrides):
    from kiro_crew.sel_filter import select_events

    kwargs = {"limit": 100, "filters": {}, "since": None, "until": None, "scan_limit": 100}
    kwargs.update(overrides)
    return select_events(log, **kwargs)


def _parse(*argv: str) -> argparse.Namespace:
    root = argparse.ArgumentParser(prog="kirocrew")
    cli_parser_admin.register_security_parser(root.add_subparsers(dest="command"))
    return root.parse_args(["security", "events", *argv])


def _run_cli(log, *argv: str) -> None:
    with patch("kiro_crew.cli_commands.sel", return_value=log):
        cc._security(_parse(*argv))


class TestSelectEvents:
    def test_a_row_is_kept_only_when_every_named_field_matches_exactly(self, log) -> None:
        _seed(log)
        picked = _pick(log, filters={"outcome": "denied", "caller": "alice", "operation": ""})
        assert _ids(picked.rows) == ["evt5", "evt1"]
        assert picked.scan_capped is False

    @pytest.mark.parametrize("value", ["bash", "execute_*", "EXECUTE_BASH", "execute_bash "])
    def test_a_value_is_compared_whole_not_as_a_substring_or_pattern(self, log, value) -> None:
        _seed(log)
        assert _pick(log, filters={"operation": value}).rows == []

    def test_the_limit_cuts_after_the_filter_not_before(self, log) -> None:
        _seed(log)
        picked = _pick(log, limit=2, filters={"outcome": "denied"})
        # Four rows were denied; the newest two are the answer, though the log's own two
        # newest rows (evt5, evt4) include an approved one.
        assert _ids(picked.rows) == ["evt5", "evt3"]

    def test_the_window_bounds_the_rows(self, log) -> None:
        _seed(log)
        picked = _pick(
            log,
            filters={"source": "slack"},
            since=_BASE + timedelta(minutes=3),
            until=_BASE + timedelta(minutes=5),
        )
        assert _ids(picked.rows) == ["evt3"]

    def test_a_scan_that_used_its_budget_says_so(self, log) -> None:
        _seed(log)
        picked = _pick(log, filters={"outcome": "denied"}, scan_limit=3)
        assert _ids(picked.rows) == ["evt5", "evt3"]
        assert picked.scan_capped is True

    def test_a_filled_limit_is_not_a_capped_scan(self, log) -> None:
        _seed(log)
        picked = _pick(log, limit=1, filters={"outcome": "denied"}, scan_limit=3)
        assert _ids(picked.rows) == ["evt5"]
        assert picked.scan_capped is False

    def test_no_field_filter_asks_the_log_for_exactly_the_limit(self) -> None:
        fake = MagicMock()
        fake.recent.return_value = [{"event_id": "e1"}]
        picked = _pick(fake, limit=7, filters={"operation": "", "outcome": ""}, scan_limit=50)
        fake.recent.assert_called_once_with(limit=7, since=None, until=None)
        assert picked.rows == [{"event_id": "e1"}]

    def test_a_forged_non_string_field_is_never_equal_to_a_filter(self) -> None:
        fake = MagicMock()
        fake.recent.return_value = [{"outcome": ["denied"]}, {"outcome": "denied"}]
        picked = _pick(fake, filters={"outcome": "denied"}, scan_limit=50)
        assert picked.rows == [{"outcome": "denied"}]


class TestCli:
    def test_default_text_output_is_unchanged(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "-n", "2")
        out = capsys.readouterr().out
        assert out == (
            "📋 Last 2 security event(s):\n\n"
            "  2026-08-21T00:05:00  [cron] tool_invocation: execute_bash → denied"
            "  (caller: alice)\n"
            "  2026-08-21T00:04:00  [cron] tool_invocation: fs_write → approved"
            "  (caller: alice)\n"
        )

    def test_the_new_options_default_to_nothing(self) -> None:
        args = _parse()
        assert (args.operation, args.outcome, args.source, args.caller) == ("", "", "", "")
        assert args.limit == 20

    def test_text_output_honors_a_field_filter(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "--outcome", "denied", "--caller", "bob")
        out = capsys.readouterr().out
        assert out.count("→ denied") == 2
        assert "alice" not in out
        assert "Last 2 security event(s)" in out

    def test_a_filter_with_no_match_says_so(self, log, capsys) -> None:
        _seed(log)
        _run_cli(log, "--operation", "nothing_like_this")
        assert "No security events matched the filters." in capsys.readouterr().out

    def test_a_scan_that_hit_its_budget_is_named_on_stderr(
        self, log, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _seed(log)
        monkeypatch.setattr("kiro_crew.sel_filter.CLI_SCAN_LIMIT", 3)
        _run_cli(log, "--outcome", "denied", "-n", "10")
        assert "looked at the newest" in capsys.readouterr().err
