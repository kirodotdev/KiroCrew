"""Every declared-``str`` field of an agent record holds a ``str`` after load.

``config.json`` is hand- and agent-writable, so a wrong-typed value in any
``agents.<name>`` key must collapse to the field's declared default at the load
boundary, exactly as ``model``, ``triggers`` and ``display_name`` already do. A
wrong-typed key then behaves like an absent key, and no downstream reader has to
re-derive the type (``kirocrew agent list`` formats four of these with a width
spec, where ``None`` / a list / a dict raises ``TypeError`` from ``__format__``).
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.cli import main
from kiro_crew.config.fields import field_default
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig

# The five fields that had no load-time guard and now collapse to their default;
# the three that already had one are covered by their own tests
# (model/triggers/display_name) and are asserted here only through the
# whole-record sweep below.
PINNED_FIELDS = (
    "kiro_agent",
    "workspace",
    "description",
    "source",
    "telegram_account",
)

# Declared ``str`` but deliberately NOT pinned. The ``memory_store`` default is
# the shared store, and the ``member_id`` default ``""`` reads as "no identity
# yet", so collapsing either would turn off a fail-closed check in
# ``memory_stores.py``. The resolvers refuse the value as written instead.
KEPT_AS_WRITTEN = ("memory_store", "member_id")

# Every JSON shape a hand edit can produce that is not a string.
NON_STRINGS = (None, 1, 1.5, True, [], ["a"], {}, {"k": "v"})


def _load(data: dict) -> KiroCrewConfig:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def _declared_str_fields() -> tuple[str, ...]:
    return tuple(
        f.name
        for f in dataclasses.fields(KiroCrewAgentConfig)
        if f.type in ("str", str) and f.default is not dataclasses.MISSING
    )


class TestSafeStr:
    def test_a_string_is_returned_as_stored(self) -> None:
        from kiro_crew.config.fields import _safe_str

        assert _safe_str("x", "d") == "x"
        assert _safe_str("", "d") == ""

    @pytest.mark.parametrize("bad", NON_STRINGS, ids=repr)
    def test_a_non_string_falls_back_to_the_default(self, bad: object) -> None:
        from kiro_crew.config.fields import _safe_str

        assert _safe_str(bad, "d") == "d"


class TestAgentRecordStrFieldsPinnedAtLoad:
    @pytest.mark.parametrize("field_name", PINNED_FIELDS)
    @pytest.mark.parametrize("bad", NON_STRINGS, ids=repr)
    def test_a_non_string_collapses_to_the_declared_default(
        self, field_name: str, bad: object
    ) -> None:
        cfg = _load({"agents": {"x": {field_name: bad}}, "default_agent": "x"})
        got = getattr(cfg.agents["x"], field_name)
        assert got == field_default(KiroCrewAgentConfig, field_name)
        assert isinstance(got, str)

    @pytest.mark.parametrize("field_name", PINNED_FIELDS)
    def test_a_string_still_loads_verbatim(self, field_name: str) -> None:
        cfg = _load({"agents": {"x": {field_name: "kept as written"}}, "default_agent": "x"})
        assert getattr(cfg.agents["x"], field_name) == "kept as written"

    @pytest.mark.parametrize("field_name", PINNED_FIELDS)
    def test_a_wrong_typed_key_reads_like_an_absent_key(self, field_name: str) -> None:
        wrong = _load({"agents": {"x": {field_name: {"not": "a str"}}}, "default_agent": "x"})
        absent = _load({"agents": {"x": {}}, "default_agent": "x"})
        assert getattr(wrong.agents["x"], field_name) == getattr(absent.agents["x"], field_name)

    def test_every_declared_str_field_holds_a_str_after_load(self) -> None:
        """The whole-record invariant, so a future ``str`` field cannot ship unguarded."""
        names = tuple(n for n in _declared_str_fields() if n not in KEPT_AS_WRITTEN)
        assert set(PINNED_FIELDS) <= set(names)
        for bad in NON_STRINGS:
            cfg = _load({"agents": {"x": {name: bad for name in names}}, "default_agent": "x"})
            record = cfg.agents["x"]
            for name in names:
                assert isinstance(getattr(record, name), str), (name, bad)


class TestIdentityFieldsStayAsWritten:
    """A malformed binding or identity is refused downstream, never collapsed to the default."""

    @pytest.mark.parametrize("field_name", KEPT_AS_WRITTEN)
    @pytest.mark.parametrize("bad", NON_STRINGS, ids=repr)
    def test_a_non_string_is_not_pinned_to_the_default(self, field_name: str, bad: object) -> None:
        cfg = _load({"agents": {"x": {field_name: bad}}, "default_agent": "x"})
        assert getattr(cfg.agents["x"], field_name) == bad
        assert getattr(cfg.agents["x"], field_name) != field_default(
            KiroCrewAgentConfig, field_name
        )


class TestCliAgentListSurvivesWrongTypedFields:
    """``kirocrew agent list`` width-formats four record fields; none of them raises there."""

    @pytest.mark.parametrize("field_name", ("kiro_agent", "workspace", "memory_store", "source"))
    @pytest.mark.parametrize("bad", (None, [], {}), ids=repr)
    def test_the_listing_prints_instead_of_raising(
        self,
        field_name: str,
        bad: object,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"agents": {"x": {field_name: bad}}, "default_agent": "x"}),
            encoding="utf-8",
        )
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
        ):
            main()
        out = capsys.readouterr().out
        assert any(line.startswith("x ") for line in out.splitlines())
        if field_name == "memory_store":
            assert f"(invalid: {type(bad).__name__})" in out
