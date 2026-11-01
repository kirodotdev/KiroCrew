"""Pins the closed attribute lists shared by the usage metrics.

``trigger_source``, ``origin`` and ``session_kind`` are labels on exported series,
so each list is spelled out here and compared exactly: adding or removing a value
is a reviewed change to what leaves the machine, not a side effect of an edit
elsewhere. Each rollup is also checked to be TOTAL over the session-source labels
it reads, so a new label cannot ship without a mapping.
"""

from __future__ import annotations

import pytest

from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.crew_log.entry_types_lifecycle import ACTOR_VALUES
from kiro_crew.messaging.link import TELEMETRY_CHANNELS
from kiro_crew.metrics import usage_attrs as ua


def test_trigger_source_values_are_pinned():
    assert ua.TRIGGER_SOURCE_VALUES == (
        "user",
        "cron",
        "heartbeat",
        "monitor",
        "subagent",
        "workflow",
        "webhook",
        "app",
        "agent",
        "system",
        "other",
    )


@pytest.mark.parametrize("raw", [None, "", "User", "person", 3, "dashboard"])
def test_unknown_trigger_is_other_never_user(raw):
    assert ua.trigger_source(raw) == "other"


def test_every_turn_actor_has_a_trigger_mapping():
    """A new crew-log actor must be mapped deliberately."""
    for actor in ACTOR_VALUES:
        got = ua.trigger_from_turn_actor(actor, person_send=True)
        assert got in ua.TRIGGER_SOURCE_VALUES
        if actor != "other":
            assert got != "other", f"actor {actor!r} has no trigger mapping"


@pytest.mark.parametrize(
    "actor,expected",
    [
        ("app", "app"),
        ("crew", "agent"),
        ("cron", "cron"),
        ("autonudge", "monitor"),
        ("subagent", "subagent"),
        ("gateway", "system"),
        ("other", "other"),
        ("brand-new", "other"),
        (None, "other"),
    ],
)
def test_actor_mapping(actor, expected):
    assert ua.trigger_from_turn_actor(actor, person_send=True) == expected
    assert ua.trigger_from_turn_actor(actor, person_send=False) == expected


def test_user_actor_needs_a_person_send():
    assert ua.trigger_from_turn_actor("user", person_send=True) == "user"
    assert ua.trigger_from_turn_actor("user", person_send=False) == "other"


def test_origin_values_are_pinned():
    assert ua.ORIGIN_VALUES == (
        "slack",
        "discord",
        "telegram",
        "whatsapp",
        "webex",
        "wecom",
        "teams",
        "weixin",
        "imessage",
        "feishu",
        "unified",
        "dashboard",
        "cli",
        "webhook",
        "api",
        "internal",
        "other",
    )
    assert len(ua.ORIGIN_VALUES) == 17
    assert tuple(CHANNEL_SESSION_NAMESPACES) == ua.ORIGIN_VALUES[:11]


def test_every_session_source_label_has_an_origin_and_a_kind():
    """Total over ``TELEMETRY_CHANNELS``: no label falls through to a default."""
    for label in TELEMETRY_CHANNELS:
        if label in CHANNEL_SESSION_NAMESPACES:
            continue
        assert label in ua.ORIGIN_OF_SESSION_SOURCE, f"{label!r} has no origin"
        assert label in ua.SESSION_KIND_OF_SOURCE, f"{label!r} has no session_kind"
    assert set(ua.ORIGIN_OF_SESSION_SOURCE.values()) <= set(ua.ORIGIN_VALUES)
    assert set(ua.SESSION_KIND_OF_SOURCE.values()) <= set(ua.SESSION_KIND_VALUES)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("slack:T1:C1:123.4", "slack"),
        ("telegram:42", "telegram"),
        ("chat-12-1785445181", "dashboard"),
        ("dashboard:abc", "dashboard"),
        ("cli_chat", "cli"),
        ("cron:job", "internal"),
        ("subagent:x", "internal"),
        ("_hb", "internal"),
        ("", "other"),
        (None, "other"),
        ("??", "other"),
    ],
)
def test_origin_of_key(key, expected):
    assert ua.origin_of(key) == expected


def test_explicit_origin_wins_only_when_closed():
    assert ua.origin_of("chat-1-2", explicit="webhook") == "webhook"
    assert ua.origin_of("chat-1-2", explicit="api") == "api"
    assert ua.origin_of("chat-1-2", explicit="free text") == "dashboard"


def test_session_kind_values_are_pinned():
    assert ua.SESSION_KIND_VALUES == (
        "chat",
        "channel",
        "cron",
        "subagent",
        "workflow_step",
        "task_runner",
        "heartbeat",
        "background",
        "other",
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        ("dashboard", "chat"),
        ("cli", "chat"),
        ("slack", "channel"),
        ("cron", "cron"),
        ("workflow_pool", "workflow_step"),
        ("taskrunner", "task_runner"),
        ("consolidation", "background"),
        ("unknown", "other"),
        ("never-seen", "other"),
    ],
)
def test_session_kind_rollup(source, expected):
    assert ua.session_kind_of_source(source) == expected


def test_session_start_trigger():
    # A kind the key fixes always wins over the running turn's trigger.
    assert ua.session_start_trigger("subagent", explicit="user") == "subagent"
    assert ua.session_start_trigger("cron") == "cron"
    assert ua.session_start_trigger("task_runner") == "workflow"
    assert ua.session_start_trigger("background") == "system"
    # A chat or channel session takes the opener's statement, else other.
    assert ua.session_start_trigger("chat", explicit="user") == "user"
    assert ua.session_start_trigger("chat", explicit="agent") == "agent"
    assert ua.session_start_trigger("chat") == "other"
    assert ua.session_start_trigger("channel", explicit="nonsense") == "other"
    for kind in ua.SESSION_KIND_VALUES:
        assert ua.session_start_trigger(kind) in ua.TRIGGER_SOURCE_VALUES
        assert ua.session_start_trigger(kind) != "user"


def test_current_trigger_context():
    assert ua.current_trigger() is None
    token = ua.set_current_trigger("monitor")
    try:
        assert ua.current_trigger() == "monitor"
    finally:
        ua._CURRENT_TRIGGER.reset(token)
    token = ua.set_current_trigger("bogus")
    try:
        assert ua.current_trigger() == "other"
    finally:
        ua._CURRENT_TRIGGER.reset(token)
