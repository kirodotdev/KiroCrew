"""Unit tests for ``coerce_config_field`` — the shared nested-value guard.

Raw config readers guard the document root but must also coerce the nested
values they consume, so a hand-edited ``config.json`` with a wrong-typed field
falls back to the default the validated loader uses rather than raising
(``int([])``, a char loop over an int, ``.get`` on a list). This helper gives
every such reader the loader's posture in one place: a wrong-typed value degrades
to the default and is logged; an absent key takes the default silently.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.config.loader import coerce_config_field


class TestCoerceConfigField:
    def test_a_correctly_typed_value_is_returned(self) -> None:
        assert coerce_config_field({"n": 7}, "n", int, 10) == 7
        assert coerce_config_field({"s": "ok"}, "s", str, "def") == "ok"
        assert coerce_config_field({"xs": [1, 2]}, "xs", list, []) == [1, 2]
        assert coerce_config_field({"d": {"a": 1}}, "d", dict, {}) == {"a": 1}

    def test_an_absent_key_takes_the_default_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert coerce_config_field({}, "n", int, 10) == 10
        assert caplog.records == []  # absent is the unconfigured state, not a degradation

    @pytest.mark.parametrize("bad", [[], {}, "x", 1.5, None])
    def test_a_wrong_typed_int_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"n": bad}, "n", int, 10) == 10

    @pytest.mark.parametrize("bad", [1, [], {}, None, True])
    def test_a_wrong_typed_str_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"s": bad}, "s", str, "def") == "def"

    @pytest.mark.parametrize("bad", [1, "x", {}, None])
    def test_a_wrong_typed_list_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"xs": bad}, "xs", list, []) == []

    @pytest.mark.parametrize("bad", [1, "x", [], None])
    def test_a_wrong_typed_dict_field_degrades_to_the_default(self, bad: object) -> None:
        assert coerce_config_field({"d": bad}, "d", dict, {"fallback": True}) == {"fallback": True}

    def test_a_degrade_logs_a_warning_naming_the_key_and_got_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            coerce_config_field({"fetch_top_n": []}, "fetch_top_n", int, 10)
        assert any("fetch_top_n" in r.message and "list" in r.message for r in caplog.records)

    def test_a_bool_is_not_accepted_for_an_int_field(self) -> None:
        """JSON ``true``/``false`` is distinct from a number: a bool where an int
        is expected degrades, so a mistyped ``true`` cannot pose as ``1``."""
        assert coerce_config_field({"n": True}, "n", int, 10) == 10
        assert coerce_config_field({"n": False}, "n", int, 10) == 10

    def test_a_bool_is_accepted_for_a_bool_field(self) -> None:
        assert coerce_config_field({"b": True}, "b", bool, False) is True
        assert coerce_config_field({"b": False}, "b", bool, True) is False

    def test_a_tuple_of_types_accepts_any_member(self) -> None:
        assert coerce_config_field({"v": 1}, "v", (int, float), 0) == 1
        assert coerce_config_field({"v": 1.5}, "v", (int, float), 0) == 1.5
        assert coerce_config_field({"v": "x"}, "v", (int, float), 0) == 0
