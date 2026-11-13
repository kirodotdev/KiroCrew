"""``memory.history_idle_hours`` always loads as a usable number of hours.

The consolidator and the gateway multiply it by 3600. A non-finite value there
means history never consolidates, and zero or a negative value means it
consolidates on every check. An unreadable value takes the field default and a
positive one below the floor is raised to it; both are logged, and neither ever
fails the load of an older config file.
"""

from __future__ import annotations

import logging
import math

import pytest
from config_loader_helpers import _load_from_dict, _load_from_dict_with_logs

from kiro_crew.config.fields import field_default
from kiro_crew.config.memory_sections import HISTORY_IDLE_HOURS_MIN, MemoryConfig
from kiro_crew.config.section_builders import _build_memory_config

_DEFAULT = float(field_default(MemoryConfig, "history_idle_hours"))


def _idle_warnings(messages: list[str]) -> list[str]:
    return [m for m in messages if m.startswith("Config: memory.history_idle_hours")]


class TestHistoryIdleHoursLoad:
    def test_absent_key_takes_the_default_silently(self) -> None:
        cfg, messages = _load_from_dict_with_logs({"memory": {}})
        assert cfg.memory.history_idle_hours == _DEFAULT
        assert _idle_warnings(messages) == []

    @pytest.mark.parametrize("raw", [2.5, 6, 0.5, 24.0])
    def test_valid_number_is_kept(self, raw: float) -> None:
        cfg, messages = _load_from_dict_with_logs({"memory": {"history_idle_hours": raw}})
        assert cfg.memory.history_idle_hours == raw
        assert _idle_warnings(messages) == []

    @pytest.mark.parametrize("raw", ["0", "-5", "-0.5", "Infinity", "-Infinity", "NaN", "1e400"])
    def test_unusable_number_takes_the_default_with_a_warning(self, raw: str) -> None:
        # Raw JSON text, so 1e400 overflows on parse as it would on disk. The
        # schema lets every one of these through as a number.
        cfg, messages = _load_from_dict_with_logs('{"memory": {"history_idle_hours": %s}}' % raw)
        assert cfg.memory.history_idle_hours == _DEFAULT
        assert len(_idle_warnings(messages)) == 1

    def test_small_positive_value_is_raised_to_the_floor(self) -> None:
        cfg, messages = _load_from_dict_with_logs({"memory": {"history_idle_hours": 0.1}})
        assert cfg.memory.history_idle_hours == HISTORY_IDLE_HOURS_MIN
        assert len(_idle_warnings(messages)) == 1

    @pytest.mark.parametrize("raw", [None, "abc", "", True, [], {}])
    def test_wrong_type_never_reaches_the_multiplier(self, raw: object) -> None:
        cfg = _load_from_dict({"memory": {"history_idle_hours": raw}})
        secs = cfg.memory.history_idle_hours * 3600
        assert isinstance(secs, float) and math.isfinite(secs) and secs > 0

    def test_digit_string_from_an_older_file_is_read_as_hours(self) -> None:
        cfg = _load_from_dict({"memory": {"history_idle_hours": "3"}})
        assert cfg.memory.history_idle_hours == 3.0
        assert cfg.memory.history_idle_hours * 3600 == 10800.0


class TestHistoryIdleHoursBuilder:
    """The builder holds the rule on its own, without the schema pass in front of it."""

    @pytest.mark.parametrize(
        ("raw", "expected", "warns"),
        [
            ("3", 3.0, False),
            ("1.5", 1.5, False),
            (2, 2.0, False),
            (None, _DEFAULT, True),
            ("abc", _DEFAULT, True),
            (True, _DEFAULT, True),
            (-5, _DEFAULT, True),
            (0, _DEFAULT, True),
            (float("inf"), _DEFAULT, True),
            (float("nan"), _DEFAULT, True),
            (0.25, HISTORY_IDLE_HOURS_MIN, True),
        ],
    )
    def test_value_is_a_positive_finite_float(
        self, raw: object, expected: float, warns: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            hours = _build_memory_config({"history_idle_hours": raw}).history_idle_hours
        assert type(hours) is float
        assert hours == expected
        assert bool(_idle_warnings([r.getMessage() for r in caplog.records])) is warns
