"""The reset time a usage-limit error names is read once, where the error is raised.

The backend words a spent allowance with the time it opens again ("... resets
5:50pm"). The error path reads that clause once, at raise time. Pinned here:

* the parser reads the clauses the backend writes and refuses what it cannot read,
  returning ``None`` with a warning and never raising;
* the raised limit error carries the parsed time as ``usage_limit_reset_at``;
* a clause that cannot be read leaves the raised error exactly what it was;
* the limit's transcript row carries the time as ``reset_at`` only when one was read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kiro_crew import limit_reset
from kiro_crew.acp.client import AcpError, _raise_acp_error
from kiro_crew.limit_reset import MAX_RESET_AHEAD_SECS, parse_limit_reset
from kiro_crew.testing.clock import ManualClock

CHICAGO = ZoneInfo("America/Chicago")
# A Thursday, 14:00 local.
NOW = datetime(2026, 11, 12, 14, 0, tzinfo=CHICAGO)
_REQ = "8182dc3a-700a-4832-bd91-563bf5875626"


def _at(text: str) -> datetime | None:
    found = parse_limit_reset(text, NOW)
    return None if found is None else datetime.fromtimestamp(found, CHICAGO)


def _local(day: int, hour: int, minute: int) -> datetime:
    """A November 2026 moment in Chicago: the instant the clause names, with no margin."""
    return datetime(2026, 11, day, hour, minute, tzinfo=CHICAGO)


class TestParseLimitReset:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("You've hit your session limit \u00b7 resets 5:50pm", _local(12, 17, 50)),
            ("5-hour limit reached - resets 3pm", _local(12, 15, 0)),
            ("resets at 17:50", _local(12, 17, 50)),
            # A time already gone today is tomorrow's.
            ("resets 1pm", _local(13, 13, 0)),
            ("You've reached your weekly limit. It resets Monday at 9am.", _local(16, 9, 0)),
            ("resets Thursday 3pm", _local(12, 15, 0)),
            # The same weekday as today, earlier: next week.
            ("resets Thursday 1pm", _local(19, 13, 0)),
            ("resets in 2 hours 10 minutes", _local(12, 16, 10)),
            ("resets in 1h 30m", _local(12, 15, 30)),
            # A named zone is the clause's own: 3pm in London is 9am in Chicago.
            ("resets 3pm (Europe/London)", _local(13, 9, 0)),
            (f"session limit resets 5:50pm (request_id: {_REQ})", _local(12, 17, 50)),
        ],
    )
    def test_reads_the_clauses_the_backend_writes(self, text, expected):
        assert _at(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "resets 3",
            "resets 13pm",
            "resets at 25:00",
            "resets Nov 15 at 9am",
            "resets on 2026-11-15 9am",
            "resets in 400 hours",
            "resets in 0 minutes",
            "resets in " + "9" * 5000 + " hours",
            # A zone that does not load: reading it in the host's zone would be wrong.
            "resets 5pm (PST)",
            "resets 5pm (Not/AZone)",
        ],
    )
    def test_a_clause_it_cannot_use_is_dropped_with_a_warning(self, text, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.limit_reset"):
            assert parse_limit_reset(text, NOW) is None
        assert "dropped" in caplog.text or "could not be read" in caplog.text
        assert "9999" not in caplog.text

    @pytest.mark.parametrize("text", ["The monthly usage limit has been reached", "", None])
    def test_no_reset_clause_is_none_without_a_warning(self, text, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.limit_reset"):
            assert parse_limit_reset(text, NOW) is None
        assert caplog.text == ""

    def test_a_weekly_reset_across_a_dst_change_keeps_its_clock_time(self, local_tz):
        # The host clock as the default reads it: a fixed offset (CDT) on a Friday
        # before the change back to CST. Monday 9am is 9am CST, not 8am.
        local_tz("America/Chicago")
        friday = datetime(2026, 10, 30, 14, 0, tzinfo=CHICAGO).astimezone()
        assert friday.tzinfo == timezone(timedelta(hours=-5), friday.tzname())
        found = parse_limit_reset(
            "You've reached your weekly limit. It resets Monday at 9am.", friday
        )
        assert found == datetime(2026, 11, 2, 9, 0, tzinfo=CHICAGO).timestamp()

    def test_a_named_zone_reads_across_a_dst_change_too(self):
        friday = datetime(2026, 10, 30, 14, 0, tzinfo=CHICAGO)
        found = parse_limit_reset("It resets Monday at 9am (America/Chicago).", friday)
        assert found == datetime(2026, 11, 2, 9, 0, tzinfo=CHICAGO).timestamp()

    def test_the_result_is_ahead_and_inside_the_ceiling(self):
        found = parse_limit_reset("resets in 7 days", NOW)
        assert found is not None
        assert 0 < found - NOW.timestamp() <= MAX_RESET_AHEAD_SECS

    def test_a_failure_while_reading_is_none_not_an_exception(self, _floor_monkeypatch, caplog):
        def boom(_tail, _now):
            raise RuntimeError("secret sentence")

        _floor_monkeypatch.setattr(limit_reset, "_clock_at", boom)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.limit_reset"):
            assert parse_limit_reset("resets 5pm", NOW) is None
        assert "RuntimeError" in caplog.text
        assert "secret sentence" not in caplog.text


def _frame(data: str) -> dict:
    return {"code": -32603, "message": "Internal error", "data": data}


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


class TestTheRaiseTag:
    def test_the_raised_error_carries_the_parsed_time(self, monkeypatch):
        clock = ManualClock()
        clock.install(monkeypatch, limit_reset, time=False, datetime=True)
        exc = _raised(
            _frame(f"You've hit your session limit \u00b7 resets in 2 hours (request_id: {_REQ})")
        )
        assert exc.usage_limit is True
        assert exc.usage_limit_reset_at == clock.time() + 7200

    def test_a_limit_with_no_reset_clause_has_no_time(self):
        exc = _raised(_frame(f"The monthly usage limit has been reached (request_id: {_REQ})"))
        assert exc.usage_limit is True
        assert exc.usage_limit_reset_at is None

    def test_an_error_that_is_not_a_limit_has_no_time(self):
        exc = _raised(_frame("something resets in 2 hours"))
        assert exc.usage_limit is False
        assert exc.usage_limit_reset_at is None

    def test_an_unreadable_clause_leaves_the_error_as_it_was(self):
        read = _raised(_frame("You've hit your session limit \u00b7 resets in 2 hours"))
        unread = _raised(_frame("You've hit your session limit \u00b7 resets Nov 15 at 9am"))
        assert unread.usage_limit is True
        assert unread.usage_limit_reset_at is None
        assert type(unread) is type(read)
        assert unread.transient is read.transient


class TestTheRowMeta:
    """The transcript row a limit error leaves carries the time, for the card to show."""

    def test_a_readable_reset_rides_on_the_row_as_an_iso_instant(self, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta
        from kiro_crew.dashboard.chat_utils import USAGE_LIMIT_KIND

        clock = ManualClock()
        clock.install(monkeypatch, limit_reset, time=False, datetime=True)
        exc = _raised(_frame("You've hit your session limit \u00b7 resets in 2 hours"))
        meta = _terminal_error_meta(exc)
        assert meta is not None
        assert meta["kind"] == USAGE_LIMIT_KIND
        at = datetime.fromisoformat(str(meta["reset_at"]))
        assert at.tzinfo is not None
        assert at.timestamp() == clock.time() + 7200

    @pytest.mark.parametrize("reset_at", [None, True, "5:50pm", float("nan"), 1e30])
    def test_no_readable_reset_keeps_the_row_mains_shape(self, reset_at):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta
        from kiro_crew.dashboard.chat_utils import USAGE_LIMIT_KIND

        e = AcpError("The monthly usage limit has been reached.", transient=False)
        e.usage_limit = True
        e.usage_limit_reset_at = reset_at
        assert _terminal_error_meta(e) == {"kind": USAGE_LIMIT_KIND}

    def test_a_limit_with_no_reset_clause_writes_mains_row(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta
        from kiro_crew.dashboard.chat_utils import USAGE_LIMIT_KIND

        exc = _raised(_frame(f"The monthly usage limit has been reached (request_id: {_REQ})"))
        assert _terminal_error_meta(exc) == {"kind": USAGE_LIMIT_KIND}
