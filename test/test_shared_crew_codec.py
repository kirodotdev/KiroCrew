"""Shared Crew record codec: field readers refuse malformed values and keep unknown keys."""

from __future__ import annotations

import enum

import pytest

from kiro_crew.shared_crew.codec import (
    ContractError,
    enum_or,
    merge_extra,
    opt_float,
    opt_int,
    opt_str,
    read_schema_version,
    req_enum,
    req_float,
    req_int,
    req_str,
    require_mapping,
    split_extra,
)


class Color(enum.Enum):
    RED = "red"
    UNKNOWN = "unknown"


def test_require_mapping_refuses_a_non_object() -> None:
    assert require_mapping({"a": 1}, "rec") == {"a": 1}
    with pytest.raises(ContractError) as exc:
        require_mapping([1], "rec")
    assert (exc.value.code, exc.value.field) == ("malformed_record", "rec")


def test_schema_version_defaults_to_one_and_refuses_bad_values() -> None:
    assert read_schema_version({}) == 1
    assert read_schema_version({"schema_version": 3}) == 3
    for bad in (0, -1, True, "1", 1.5):
        with pytest.raises(ContractError):
            read_schema_version({"schema_version": bad})


def test_strings() -> None:
    assert req_str({"a": "x"}, "a") == "x"
    assert req_str({"a": ""}, "a", allow_empty=True) == ""
    for raw in ({}, {"a": ""}, {"a": 3}):
        with pytest.raises(ContractError):
            req_str(raw, "a")
    assert opt_str({}, "a") is None
    assert opt_str({"a": None}, "a") is None
    assert opt_str({"a": "y"}, "a") == "y"
    with pytest.raises(ContractError):
        opt_str({"a": 1}, "a")


def test_floats_refuse_bools_strings_and_non_finite() -> None:
    assert opt_float({}, "t") is None
    assert opt_float({"t": 2}, "t") == 2.0
    assert req_float({"t": 1.5}, "t") == 1.5
    # 10**400 is valid JSON but too big for a float: a ContractError, not OverflowError.
    for bad in (True, "1", float("nan"), float("inf"), 10**400):
        with pytest.raises(ContractError):
            opt_float({"t": bad}, "t")
    with pytest.raises(ContractError):
        req_float({}, "t")


def test_ints_respect_the_minimum() -> None:
    assert opt_int({}, "n") is None
    assert opt_int({"n": 0}, "n") == 0
    assert req_int({"n": 5}, "n", minimum=1) == 5
    for bad in (True, 1.0, "2", -1):
        with pytest.raises(ContractError):
            opt_int({"n": bad}, "n")
    with pytest.raises(ContractError):
        req_int({"n": 0}, "n", minimum=1)
    with pytest.raises(ContractError):
        req_int({}, "n")


def test_req_enum_refuses_an_unknown_value() -> None:
    assert req_enum({"c": "red"}, "c", Color) is Color.RED
    with pytest.raises(ContractError) as exc:
        req_enum({"c": "blue"}, "c", Color)
    assert exc.value.code == "unknown_enum_value"


def test_enum_or_falls_back_for_a_newer_or_missing_value() -> None:
    assert enum_or({"c": "red"}, "c", Color, Color.UNKNOWN) is Color.RED
    assert enum_or({"c": "blue"}, "c", Color, Color.UNKNOWN) is Color.UNKNOWN
    assert enum_or({}, "c", Color, Color.UNKNOWN) is Color.UNKNOWN


def test_unknown_keys_survive_and_owned_keys_win() -> None:
    raw = {"id": "a", "future": {"x": 1}}
    extra = split_extra(raw, frozenset({"id"}))
    assert extra == {"future": {"x": 1}}
    assert merge_extra({"id": "b"}, {**extra, "id": "stale"}) == {"id": "b", "future": {"x": 1}}
