"""Envelope + registry contract tests for the structured event log."""

from __future__ import annotations

import dataclasses
import json

import pytest

from kiro_crew.events.base import (
    REGISTRY,
    Event,
    RawEvent,
    kind_of,
    parse,
    register,
    serialize,
)


def _dummy_value(annotation: str):
    """A representative value for a kinds.py field annotation."""
    if "int" in annotation:
        return 7
    if "float" in annotation:
        return 1.5
    if "bool" in annotation:
        return True
    return "x"


def _instance_of(cls: type[Event]) -> Event:
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in ("key", "ts_ms"):
            continue
        kwargs[f.name] = _dummy_value(str(f.type))
    return cls(key="k-1", ts_ms=1_755_000_000_000, **kwargs)


def test_every_registered_kind_roundtrips() -> None:
    assert REGISTRY, "kinds.py must register at least one event type"
    for kind, cls in sorted(REGISTRY.items()):
        event = _instance_of(cls)
        line = serialize(event, src="test")
        parsed = parse(line)
        assert parsed is not None, kind
        assert parsed.kind == kind
        assert parsed.src == "test"
        assert parsed.event == event, f"{kind} did not roundtrip"


def test_envelope_shape_is_stable() -> None:
    cls = next(iter(REGISTRY.values()))
    rec = json.loads(serialize(_instance_of(cls), src="s"))
    assert rec["v"] == 1
    assert set(rec) <= {"v", "kind", "src", "key", "ts_ms", "data"}
    assert isinstance(rec["data"], dict)


def test_type_violating_payload_degrades_to_raw_event() -> None:
    # A known kind whose retained field violates the declared type must not
    # construct a typed event — the schema would be a lie. bool-in-int is
    # the sneaky case (bool subclasses int).
    for bad_data in ({"turns": "seven"}, {"turns": True}, {"result_bytes": 3.5}):
        line = json.dumps(
            {
                "v": 1,
                "kind": "subagent/completed",
                "src": "t",
                "seq": 1,
                "key": "subagent:x",
                "ts_ms": 5,
                "data": bad_data,
            }
        )
        parsed = parse(line)
        assert parsed is not None
        assert isinstance(parsed.event, RawEvent), bad_data
        assert parsed.event.payload == bad_data
    # A well-typed payload still constructs the typed event.
    ok = json.dumps(
        {
            "v": 1,
            "kind": "subagent/completed",
            "src": "t",
            "seq": 2,
            "key": "subagent:x",
            "ts_ms": 6,
            "data": {"turns": 7},
        }
    )
    parsed_ok = parse(ok)
    assert parsed_ok is not None
    assert not isinstance(parsed_ok.event, RawEvent)


def test_non_dict_data_is_rejected_as_corrupt() -> None:
    # A PRESENT non-dict data violates the envelope; substituting {} would
    # fabricate a typed event with invented defaults. Absent data stays a
    # legitimately empty payload.
    bad = json.dumps(
        {
            "v": 1,
            "kind": "cron/registered",
            "src": "s",
            "seq": 1,
            "key": "cron:x",
            "ts_ms": 5,
            "data": [1, 2],
        }
    )
    assert parse(bad) is None
    explicit_null = json.dumps(
        {
            "v": 1,
            "kind": "cron/registered",
            "src": "s",
            "seq": 3,
            "key": "cron:x",
            "ts_ms": 7,
            "data": None,
        }
    )
    assert parse(explicit_null) is None
    absent = json.dumps(
        {"v": 1, "kind": "cron/registered", "src": "s", "seq": 2, "key": "cron:x", "ts_ms": 6}
    )
    assert parse(absent) is not None


def test_boolean_envelope_numerics_are_rejected() -> None:
    # bool subclasses int: ts_ms true must not become epoch millisecond 1,
    # and a bool `v` must fall back to its default rather than reading as 1.
    bad_ts = json.dumps({"v": 1, "kind": "k", "src": "s", "key": "x", "ts_ms": True})
    assert parse(bad_ts) is None
    bool_v = json.dumps({"v": True, "kind": "future/k", "src": "s", "key": "x", "ts_ms": 5})
    parsed = parse(bool_v)
    assert parsed is not None
    assert parsed.v == 0


def test_unknown_kind_parses_as_raw_event() -> None:
    line = json.dumps(
        {
            "v": 1,
            "kind": "future/thing",
            "src": "newer-build",
            "seq": 9,
            "key": "k",
            "ts_ms": 5,
            "data": {"payload_field": [1, 2, 3]},
        }
    )
    parsed = parse(line)
    assert parsed is not None
    assert isinstance(parsed.event, RawEvent)
    assert parsed.event.kind == "future/thing"
    assert parsed.event.payload == {"payload_field": [1, 2, 3]}
    assert kind_of(parsed.event) == "future/thing"
    # Round-trip preserves the payload verbatim.
    rec = json.loads(serialize(parsed.event, src="relay"))
    assert rec["kind"] == "future/thing"
    assert rec["data"] == {"payload_field": [1, 2, 3]}


def test_unknown_field_on_known_kind_is_tolerated() -> None:
    kind, cls = sorted(REGISTRY.items())[0]
    line = json.dumps(
        {
            "v": 1,
            "kind": kind,
            "src": "newer-build",
            "seq": 0,
            "key": "k",
            "ts_ms": 5,
            "data": {"field_added_in_v99": "surprise"},
        }
    )
    parsed = parse(line)
    assert parsed is not None
    assert isinstance(parsed.event, cls)


def test_missing_required_field_falls_back_to_raw() -> None:
    # A required field is only expressible with kw_only=True (the base class
    # carries defaults); parse() must degrade to RawEvent when it is absent.
    @register
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class _Strict(Event):
        KIND = "testonly/strict"
        mandatory: str

    line = json.dumps(
        {
            "v": 1,
            "kind": "testonly/strict",
            "src": "s",
            "seq": 0,
            "key": "k",
            "ts_ms": 1,
            "data": {},
        }
    )
    parsed = parse(line)
    assert parsed is not None
    assert isinstance(parsed.event, RawEvent)
    assert parsed.event.kind == "testonly/strict"


def test_unknown_envelope_key_is_tolerated() -> None:
    # The additive-only rule promises a LATER writer may add an envelope key
    # (a distributed-trace slot is the concrete planned case) without breaking
    # a reader shipped today. That promise is what makes deferring such a slot
    # free, so pin it: an unrecognised top-level key is ignored, not rejected,
    # and the typed event still parses.
    cls = next(iter(REGISTRY.values()))
    rec = json.loads(serialize(_instance_of(cls), src="test"))
    rec["trace"] = {"trace_id": "t" * 32, "span_id": "s" * 16}
    rec["some_future_key"] = 42
    parsed = parse(json.dumps(rec))
    assert parsed is not None
    assert not isinstance(parsed.event, RawEvent)
    assert parsed.kind == kind_of(_instance_of(cls))


@pytest.mark.parametrize("bad", ["not json", "[]", '"str"', json.dumps({"kind": 5})])
def test_garbage_lines_return_none(bad: str) -> None:
    assert parse(bad) is None


def test_register_rejects_malformed_kind() -> None:
    with pytest.raises(ValueError):

        @register
        @dataclasses.dataclass(frozen=True)
        class _NoSlash(Event):
            KIND = "noslash"

    with pytest.raises(ValueError):

        @register
        @dataclasses.dataclass(frozen=True)
        class _Upper(Event):
            KIND = "Domain/Action"


def test_register_rejects_duplicate_kind() -> None:
    @register
    @dataclasses.dataclass(frozen=True)
    class _First(Event):
        KIND = "testonly/dup"

    with pytest.raises(ValueError):

        @register
        @dataclasses.dataclass(frozen=True)
        class _Second(Event):
            KIND = "testonly/dup"
