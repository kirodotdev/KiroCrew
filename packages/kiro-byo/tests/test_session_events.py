from __future__ import annotations

from types import SimpleNamespace

from kiro_byo.session_events import (
    append_event,
    append_message,
    bootstrap_events,
    clear_messages,
    compact_to_summary,
    derive_messages,
    ensure_event_projection,
    revise_last_assistant,
    truncate_messages,
    validate_events,
)


def _session(messages=None):
    return SimpleNamespace(messages=list(messages or []), events=[])


def test_event_log_round_trips_message_projection_and_sequence() -> None:
    session = _session()
    append_message(session, {"role": "user", "content": "hello"})
    append_message(session, {"role": "assistant", "content": "hi"})
    append_message(session, {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]})
    append_message(session, {"role": "tool", "tool_call_id": "1", "content": "ok"})
    assert [row["seq"] for row in session.events] == [0, 1, 2, 3]
    assert [row["type"] for row in session.events] == [
        "user/message", "assistant/message", "assistant/attempt", "tool/result"
    ]
    assert validate_events(session.events)
    assert derive_messages(session.events) == session.messages


def test_event_log_represents_truncate_revision_compaction_and_clear_without_rewriting_history() -> None:
    session = _session()
    append_message(session, {"role": "user", "content": "u"})
    append_message(session, {"role": "assistant", "content": "draft", "_kiro_provider_context": {"x": 1}})
    before = len(session.events)
    revise_last_assistant(session, {"role": "assistant", "content": "reviewed"})
    assert len(session.events) == before + 1
    assert session.messages[-1] == {"role": "assistant", "content": "reviewed"}
    assert derive_messages(session.events) == session.messages

    append_message(session, {"role": "user", "content": "later"})
    truncate_messages(session, 2, reason="rollback")
    assert derive_messages(session.events) == session.messages

    compact_to_summary(session, {"role": "system", "content": "summary"})
    assert derive_messages(session.events) == [{"role": "system", "content": "summary"}]
    clear_messages(session, reason="reset")
    assert derive_messages(session.events) == []


def test_legacy_messages_bootstrap_into_canonical_log() -> None:
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "session"},
    ]
    events = bootstrap_events(messages)
    assert validate_events(events)
    assert derive_messages(events) == messages


def test_compat_direct_message_mutation_becomes_explicit_reset_event() -> None:
    session = _session()
    append_message(session, {"role": "user", "content": "one"})
    session.messages.append({"role": "assistant", "content": "legacy direct mutation"})
    ensure_event_projection(session)
    assert session.events[-1]["type"] == "transcript/reset"
    assert derive_messages(session.events) == session.messages


def test_validate_events_rejects_sequence_gaps() -> None:
    events = []
    append_event(events, "turn/start", {})
    events[0]["seq"] = 2
    assert not validate_events(events)


def test_provider_context_is_a_separate_event_but_rejoins_message_projection() -> None:
    session = _session()
    message = {
        "role": "assistant",
        "content": "answer",
        "_kiro_provider_context": {
            "provider_id": "p",
            "transport": "openai-responses",
            "data": {"response_id": "resp_1"},
        },
    }
    append_message(session, message)
    assert [event["type"] for event in session.events] == ["assistant/message", "provider/context"]
    assert "_kiro_provider_context" not in session.events[0]["data"]["message"]
    assert derive_messages(session.events) == [message]


def test_route_pin_projection_is_event_sourced_and_compat_syncable() -> None:
    from kiro_byo.session_events import derive_route_pins, ensure_route_pin_projection, set_route_pin

    session = SimpleNamespace(messages=[], events=[], route_pins={})
    set_route_pin(session, "tiga:glitter", "p1", "m1", reason="sticky")
    assert session.route_pins == {"tiga:glitter": ("p1", "m1")}
    assert session.events[-1]["type"] == "routing/pin"
    assert derive_route_pins(session.events) == session.route_pins

    # Old/direct callers may still mutate the cache. Persistence converts that
    # mutation into another semantic pin event rather than writing side state.
    session.route_pins["tiga:glitter"] = ("p2", "m2")
    ensure_route_pin_projection(session)
    assert derive_route_pins(session.events) == {"tiga:glitter": ("p2", "m2")}
