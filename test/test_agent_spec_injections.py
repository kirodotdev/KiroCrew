"""The edition seam that names entries another tool injects into Crew's own specs."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import agent_spec_injections as injections

_EXPECTED = {"name": "x", "tools": []}

pytestmark = pytest.mark.usefixtures("empty_registry")


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(injections, "_FILTERS", {})


def _drop_extra(spec: dict[str, Any]) -> dict[str, Any] | None:
    if spec.pop("extra", None) != 1:
        return None
    return spec


def test_nothing_registered_matches_nothing():
    assert injections.registered_spec_injection_filters() == ()
    assert injections.only_registered_injection({**_EXPECTED, "extra": 1}, _EXPECTED) is None


def test_a_filter_that_restores_the_template_is_named():
    injections.register_spec_injection_filter("drop-extra", _drop_extra)

    assert injections.only_registered_injection({**_EXPECTED, "extra": 1}, _EXPECTED) == (
        "drop-extra"
    )


def test_the_spec_already_equal_to_the_template_needs_no_filter():
    injections.register_spec_injection_filter("drop-extra", _drop_extra)

    assert injections.only_registered_injection(dict(_EXPECTED), _EXPECTED) is None


def test_a_near_miss_or_a_wrong_json_type_is_not_a_match():
    injections.register_spec_injection_filter("drop-extra", _drop_extra)

    assert injections.only_registered_injection({**_EXPECTED, "extra": 2}, _EXPECTED) is None
    spec = {"name": "x", "tools": [], "extra": 1, "more": True}
    assert injections.only_registered_injection(spec, _EXPECTED) is None
    assert (
        injections.only_registered_injection({"name": "x", "tools": 0, "extra": 1}, _EXPECTED)
        is None
    )


def test_the_filter_works_on_a_copy():
    seen: list[dict[str, Any]] = []

    def record(spec: dict[str, Any]) -> dict[str, Any] | None:
        seen.append(spec)
        return _drop_extra(spec)

    injections.register_spec_injection_filter("record", record)
    spec = {**_EXPECTED, "extra": 1}

    assert injections.only_registered_injection(spec, _EXPECTED) == "record"
    assert spec == {**_EXPECTED, "extra": 1}
    assert seen[0] is not spec


def test_a_raising_filter_is_no_match_and_later_filters_still_run():
    def boom(spec: dict[str, Any]) -> dict[str, Any] | None:
        raise RuntimeError("boom")

    injections.register_spec_injection_filter("boom", boom)
    injections.register_spec_injection_filter("drop-extra", _drop_extra)

    assert injections.only_registered_injection({**_EXPECTED, "extra": 1}, _EXPECTED) == (
        "drop-extra"
    )


def test_registration_replaces_by_name_and_unregisters():
    injections.register_spec_injection_filter("f", lambda spec: None)
    injections.register_spec_injection_filter("f", _drop_extra)
    assert [name for name, _ in injections.registered_spec_injection_filters()] == ["f"]

    injections.unregister_spec_injection_filter("f")
    assert injections.registered_spec_injection_filters() == ()


@pytest.mark.parametrize("name", ["", None])
def test_a_filter_needs_a_name(name):
    with pytest.raises(ValueError):
        injections.register_spec_injection_filter(name, _drop_extra)  # type: ignore[arg-type]


def test_a_filter_must_be_callable():
    with pytest.raises(TypeError):
        injections.register_spec_injection_filter("f", "not callable")  # type: ignore[arg-type]
