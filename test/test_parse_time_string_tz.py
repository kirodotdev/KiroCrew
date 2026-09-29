"""Tests that parse_time_string uses config timezone, not system timezone.

Patches ``kiro_crew.cron`` rather than ``kiro_crew.mcp_cron``: the parser lives
beside ``get_local_tz`` in ``cron`` so both one-shot entry points (the
``cron_add`` MCP tool and ``POST /api/crons``) share one implementation, and a
patch has to target the module whose globals the function actually reads.
"""

import time
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import kiro_crew.cron as cron_mod


def test_at_time_uses_config_timezone_not_system():
    """'23:59' interpreted in config tz (Pacific) should show 23:59 Pacific."""
    pacific = ZoneInfo("America/Los_Angeles")

    with patch.object(cron_mod, "get_local_tz", return_value=("America/Los_Angeles", pacific)):
        result = cron_mod.parse_time_string("23:59")

    assert isinstance(result, float)
    result_pacific = datetime.fromtimestamp(result, tz=pacific)
    assert result_pacific.hour == 23
    assert result_pacific.minute == 59


def test_at_time_respects_different_timezones():
    """Same time string with different config tz should produce different timestamps.

    Uses 23:59 to avoid flakiness — this time is almost always in the future
    regardless of when the test runs, for both Pacific and Eastern timezones.

    The two ``datetime.now(tz)`` calls inside ``parse_time_string`` must
    observe the same wall-clock instant so that 23:59-in-Pacific and
    23:59-in-Eastern resolve to the same calendar date in their tz.
    Without this, when UTC is in the late-night-Pacific / early-morning-
    Eastern window (~07–12 UTC), Eastern's ``now`` already crossed
    midnight while Pacific's hasn't — and ``replace(hour=23, minute=59)``
    produces dates a full day apart, breaking the 3-hour diff invariant.
    """
    pacific = ZoneInfo("America/Los_Angeles")
    eastern = ZoneInfo("America/New_York")

    # Pin the wall clock so both tz-aware nows refer to the same instant.
    # 18:00 UTC = 11:00 Pacific = 14:00 Eastern — same calendar date in
    # both tz, and earlier than 23:59 so no rollover into "tomorrow".
    fixed_utc = datetime(2026, 5, 14, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

    with patch.object(cron_mod, "datetime", _FixedDatetime), \
         patch.object(cron_mod, "get_local_tz", return_value=("America/Los_Angeles", pacific)):
        result_pacific = cron_mod.parse_time_string("23:59")

    with patch.object(cron_mod, "datetime", _FixedDatetime), \
         patch.object(cron_mod, "get_local_tz", return_value=("America/New_York", eastern)):
        result_eastern = cron_mod.parse_time_string("23:59")

    assert isinstance(result_pacific, float)
    assert isinstance(result_eastern, float)
    # Pacific is 3 hours behind Eastern, so Pacific 23:59 is 3h later in absolute time
    diff_hours = (result_pacific - result_eastern) / 3600
    assert abs(diff_hours - 3.0) < 0.01


def test_explicit_zone_is_read_instead_of_the_configured_one():
    """A job's own timezone reads the clock time; the configured zone is not consulted.

    The configured zone is UTC here, the default when nothing is set, and the
    job's zone is 14 hours ahead of it, so reading "23:59" in the wrong one lands
    at 13:59 in the job's zone rather than 23:59.
    """
    kiritimati = ZoneInfo("Pacific/Kiritimati")

    with patch.object(cron_mod, "get_local_tz", return_value=("UTC", ZoneInfo("UTC"))):
        result = cron_mod.parse_time_string("23:59", "Pacific/Kiritimati")

    assert isinstance(result, float)
    local = datetime.fromtimestamp(result, tz=kiritimati)
    assert (local.hour, local.minute) == (23, 59)


def test_explicit_zone_leaves_a_relative_time_alone():
    """A relative "in 2 hours" names an instant, so the zone argument cannot move it."""
    before = time.time()
    with patch.object(cron_mod, "get_local_tz", return_value=("UTC", ZoneInfo("UTC"))):
        result = cron_mod.parse_time_string("in 2 hours", "Pacific/Kiritimati")

    assert isinstance(result, float)
    assert before + 7200 <= result <= time.time() + 7200


def test_iso_time_inside_a_spring_forward_gap_is_refused():
    result = cron_mod.parse_time_string("2027-03-14 02:30", "America/Los_Angeles")

    assert result == (
        "Error: 02:30 on 2027-03-14 does not exist in America/Los_Angeles "
        "(the zone's clocks skip it); pick a time the zone has"
    )


def test_a_whole_day_skipped_across_the_date_line_is_refused_without_a_dst_cause():
    result = cron_mod.parse_time_string("2011-12-30 10:00", "Pacific/Apia")

    assert result == (
        "Error: 10:00 on 2011-12-30 does not exist in Pacific/Apia "
        "(the zone's clocks skip it); pick a time the zone has"
    )
    assert "daylight" not in result
    assert "hour" not in result


def test_a_half_hour_spring_forward_gap_is_refused_and_its_end_resolves():
    refused = cron_mod.parse_time_string("2027-10-03 02:15", "Australia/Lord_Howe")
    resolved = cron_mod.parse_time_string("2027-10-03 02:30", "Australia/Lord_Howe")

    assert refused == (
        "Error: 02:15 on 2027-10-03 does not exist in Australia/Lord_Howe "
        "(the zone's clocks skip it); pick a time the zone has"
    )
    assert "daylight" not in refused
    assert "hour" not in refused
    expected = datetime(2027, 10, 3, 2, 30, tzinfo=ZoneInfo("Australia/Lord_Howe")).timestamp()
    assert resolved == expected


def test_tomorrow_clock_time_that_resolves_into_a_gap_is_refused():
    fixed = datetime(2027, 3, 13, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    with patch.object(cron_mod, "datetime", _FixedDatetime):
        result = cron_mod.parse_time_string("tomorrow 2:30am", "America/Los_Angeles")

    assert isinstance(result, str)
    assert result.startswith("Error: 02:30 on 2027-03-14 does not exist")


def test_configured_zone_refuses_a_spring_forward_gap():
    pacific = ZoneInfo("America/Los_Angeles")

    with patch.object(
        cron_mod,
        "get_local_tz",
        return_value=("America/Los_Angeles", pacific),
    ):
        result = cron_mod.parse_time_string("2027-03-14 02:30")

    assert isinstance(result, str)
    assert "does not exist in America/Los_Angeles" in result


def test_wall_clock_above_the_supported_range_is_refused():
    result = cron_mod.parse_time_string("9999-12-31 23:59", "America/Los_Angeles")

    assert result == (
        "Error: 9999-12-31 23:59 in America/Los_Angeles is outside the supported date range"
    )


def test_wall_clock_below_the_supported_range_is_refused():
    result = cron_mod.parse_time_string("0001-01-01 00:00", "Asia/Tokyo")

    assert result == "Error: 0001-01-01 00:00 in Asia/Tokyo is outside the supported date range"


def test_far_future_wall_clock_inside_the_supported_range_is_resolved():
    result = cron_mod.parse_time_string("2099-12-31 23:59", "America/Los_Angeles")

    assert isinstance(result, float)


def test_repeated_fall_back_time_resolves_to_its_first_occurrence():
    result = cron_mod.parse_time_string("2027-11-07 01:30", "America/Los_Angeles")

    assert result == 1825576200.0


def test_repeated_clock_time_in_the_second_pass_resolves_to_that_pass():
    fixed_utc = datetime(2027, 11, 7, 9, 10, tzinfo=ZoneInfo("UTC"))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

    with patch.object(cron_mod, "datetime", _FixedDatetime):
        result = cron_mod.parse_time_string("1:45am", "America/Los_Angeles")

    # The first occurrence is already past, so the second pass is the only schedulable answer.
    assert result == 1825580700.0


def test_time_just_after_the_spring_forward_gap_keeps_seconds():
    result = cron_mod.parse_time_string("2027-03-14 03:00:45", "America/Los_Angeles")

    expected = datetime(
        2027,
        3,
        14,
        3,
        0,
        45,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    ).timestamp()
    assert result == expected
