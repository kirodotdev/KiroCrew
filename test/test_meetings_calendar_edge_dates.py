"""A calendar value at the edge of time costs one event, never the whole feed.

``_parse_dt`` converts a wall time in a named zone to UTC. For the first or last
day of the calendar that conversion has no UTC instant and ``astimezone`` raises
``OverflowError``; nothing up the stack catches it, so a single such value in a
remote feed made the manual sync answer 500 and the poller fail every interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal

_WEST = ";TZID=America/Los_Angeles"
_EAST = ";TZID=Asia/Tokyo"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _event(uid: str, *lines: str) -> str:
    body = "".join(f"{line}\r\n" for line in lines)
    return f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{uid}\r\n{body}END:VEVENT\r\n"


def _feed(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def _soon() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)


def test_a_value_with_no_utc_instant_is_skipped_not_raised():
    assert cal._parse_dt("99991231T235959", _WEST) is None
    assert cal._parse_dt("00010101T000000", _EAST) is None


def test_an_edge_dtend_keeps_its_event_with_the_default_end():
    soon = _soon()
    start = _stamp(soon)
    feed = _feed(
        _event("good", f"DTSTART:{start}"),
        _event("edge", f"DTSTART:{start}", f"DTEND{_WEST}:99991231T235959"),
    )
    events = {e.title: e for e in cal.parse_ics(feed)}
    assert sorted(events) == ["edge", "good"]
    assert events["edge"].start == events["good"].start == soon.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert events["edge"].end == (soon + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_an_edge_dtend_falls_back_to_the_events_duration():
    soon = _soon()
    feed = _feed(
        _event(
            "edge",
            f"DTSTART:{_stamp(soon)}",
            f"DTEND{_EAST}:00010101T000000",
            "DURATION:PT2H",
        )
    )
    (event,) = cal.parse_ics(feed)
    assert event.end == (soon + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_an_edge_dtstart_drops_only_that_event():
    soon = _soon()
    feed = _feed(
        _event("edge-west", f"DTSTART{_WEST}:99991231T235959"),
        _event("edge-east", f"DTSTART{_EAST}:00010101T000000"),
        _event("good", f"DTSTART:{_stamp(soon)}", f"DTEND:{_stamp(soon + timedelta(hours=3))}"),
    )
    events = cal.parse_ics(feed)
    assert [e.title for e in events] == ["good"]
    assert events[0].end == (soon + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_values_main_already_reads_are_unchanged():
    # A UTC stamp at the edge has no conversion to overflow, a floating one is read
    # as UTC, and a date-only value stays a whole day.
    assert cal._parse_dt("99991231T235959Z", "") == datetime(
        9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc
    )
    assert cal._parse_dt("99991231T235959", "") == datetime(
        9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc
    )
    assert cal._parse_dt("20260803", "") == datetime(2026, 8, 3, tzinfo=timezone.utc)
    assert cal._parse_dt("20260803T090000", _WEST) == datetime(
        2026, 8, 3, 16, 0, tzinfo=timezone.utc
    )
