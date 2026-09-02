"""Tests that _parse_time_string uses config timezone, not system timezone."""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import kiro_crew.mcp_cron as mcp_cron_mod


def test_at_time_uses_config_timezone_not_system():
    """'23:59' interpreted in config tz (Pacific) should show 23:59 Pacific."""
    pacific = ZoneInfo("America/Los_Angeles")

    with patch.object(mcp_cron_mod, "get_local_tz", return_value=("America/Los_Angeles", pacific)):
        result = mcp_cron_mod._parse_time_string("23:59")

    assert isinstance(result, float)
    result_pacific = datetime.fromtimestamp(result, tz=pacific)
    assert result_pacific.hour == 23
    assert result_pacific.minute == 59


def test_at_time_respects_different_timezones():
    """Same time string with different config tz should produce different timestamps.

    Uses 23:59 to avoid flakiness — this time is almost always in the future
    regardless of when the test runs, for both Pacific and Eastern timezones.

    The two ``datetime.now(tz)`` calls inside ``_parse_time_string`` must
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

    with patch.object(mcp_cron_mod, "datetime", _FixedDatetime), \
         patch.object(mcp_cron_mod, "get_local_tz", return_value=("America/Los_Angeles", pacific)):
        result_pacific = mcp_cron_mod._parse_time_string("23:59")

    with patch.object(mcp_cron_mod, "datetime", _FixedDatetime), \
         patch.object(mcp_cron_mod, "get_local_tz", return_value=("America/New_York", eastern)):
        result_eastern = mcp_cron_mod._parse_time_string("23:59")

    assert isinstance(result_pacific, float)
    assert isinstance(result_eastern, float)
    # Pacific is 3 hours behind Eastern, so Pacific 23:59 is 3h later in absolute time
    diff_hours = (result_pacific - result_eastern) / 3600
    assert abs(diff_hours - 3.0) < 0.01
