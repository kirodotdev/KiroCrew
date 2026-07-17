"""GATES A7 + B4 — run event stream construction & JSON-only persistence.

A7: the runner emits exactly the documented event stream — ``EventStream`` can
build every type in ``EVENT_TYPES`` with its required ``data`` keys, in monotonic
``seq`` order, and the validator rejects off-contract events.

B4: persistence is JSON only (never pickle/marshal) — ``serialize_events`` /
``deserialize_events`` round-trip through ``json`` and reject non-JSON input.

See ``docs/system-specs/modules/workflows.md`` (run event stream) and
``docs/dynamic-workflows/GATES.md`` (A7, B4).
"""

from __future__ import annotations

import json

import pytest

import kiro_crew.workflows as wf
from kiro_crew.workflows.events import (
    REQUIRED_DATA_KEYS,
    EventStream,
    deserialize_events,
    serialize_events,
    validate_event,
)

TS = "2026-06-18T00:00:00Z"


def _full_stream() -> EventStream:
    return EventStream("wf_test")


# --------------------------------------------------------------------------- #
# A7 — vocabulary coverage: a builder exists for every event type, and the
# required-data table covers exactly EVENT_TYPES.
# --------------------------------------------------------------------------- #


def test_required_keys_cover_exactly_event_types() -> None:
    assert set(REQUIRED_DATA_KEYS) == set(wf.EVENT_TYPES)


def test_every_event_type_has_a_builder_and_validates() -> None:
    """Build one of every event type; each must pass validation with its keys."""
    s = _full_stream()
    built = [
        s.run_started(TS, name="t", args={}, script_hash="h", budget_total=None),
        s.phase_started(TS, title="Review"),
        s.agent_started(TS, agent_id="a1", label="lbl", phase="Review", call_index=0),
        s.agent_progress(TS, agent_id="a1", turns=2, last_tool="grep", elapsed_s=1.5),
        s.agent_finished(TS, agent_id="a1", result_summary="ok", ok=True),
        s.log(TS, message="hi"),
        s.budget_update(TS, spent=100, remaining=900),
        s.approval_requested(TS, prompt="ok?", approval_id="ap1"),
        s.run_finished(TS, result={"x": 1}, duration_s=3.0),
        s.run_failed(TS, error="boom", where="exec"),
        s.run_cancelled(TS, reason="user"),
    ]
    produced_types = {e.type for e in built}
    assert produced_types == set(wf.EVENT_TYPES)
    for e in built:
        validate_event(e)  # must not raise


def test_seq_is_monotonic_from_zero() -> None:
    s = _full_stream()
    e0 = s.run_started(TS, name="t", args={}, script_hash="h", budget_total=10)
    e1 = s.phase_started(TS, title="P")
    e2 = s.log(TS, message="m")
    assert [e0.seq, e1.seq, e2.seq] == [0, 1, 2]
    assert all(e.run_id == "wf_test" for e in (e0, e1, e2))


# --------------------------------------------------------------------------- #
# A7 — validation rejects off-contract events
# --------------------------------------------------------------------------- #


def test_validate_rejects_unknown_type() -> None:
    ev = wf.WorkflowEvent(run_id="r", seq=0, ts=TS, type="not_real", data={})
    with pytest.raises(ValueError):
        validate_event(ev)


def test_validate_rejects_missing_required_keys() -> None:
    # agent_started requires agent_id/label/phase/call_index
    ev = wf.WorkflowEvent(run_id="r", seq=0, ts=TS, type="agent_started", data={"agent_id": "a"})
    with pytest.raises(ValueError) as exc:
        validate_event(ev)
    assert "missing required data keys" in str(exc.value)


def test_validate_allows_extra_keys() -> None:
    ev = wf.WorkflowEvent(run_id="r", seq=0, ts=TS, type="log", data={"message": "m", "extra": 1})
    validate_event(ev)  # forward-compatible: extra keys OK


# --------------------------------------------------------------------------- #
# B4 — JSON-only persistence
# --------------------------------------------------------------------------- #


def test_round_trip_through_json() -> None:
    s = _full_stream()
    events = [
        s.run_started(TS, name="t", args={"k": "v"}, script_hash="h", budget_total=None),
        s.log(TS, message="hello"),
        s.run_finished(TS, result={"done": True}, duration_s=1.0),
    ]
    text = serialize_events(events)
    # It is genuinely JSON (not a pickle/binary blob).
    assert isinstance(text, str)
    parsed = json.loads(text)
    assert isinstance(parsed, list) and len(parsed) == 3

    back = deserialize_events(text)
    assert [e.to_json() for e in back] == [e.to_json() for e in events]


def test_serialize_output_is_pure_json() -> None:
    s = _full_stream()
    text = serialize_events([s.log(TS, message="m")])
    # json.loads must succeed — proves no pickle/marshal framing.
    json.loads(text)


def test_deserialize_rejects_non_json() -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        deserialize_events("\x80\x04\x95not-json")  # pickle-ish bytes as text


def test_deserialize_rejects_non_array_json() -> None:
    with pytest.raises(ValueError):
        deserialize_events('{"not": "an array"}')


def test_events_module_has_no_pickle_import() -> None:
    """B4 guard: the persistence module must not import pickle/marshal/shelve."""
    import kiro_crew.workflows.events as ev_mod

    src = __import__("inspect").getsource(ev_mod)
    for banned in ("import pickle", "import marshal", "import shelve"):
        assert banned not in src
