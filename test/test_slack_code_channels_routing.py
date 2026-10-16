"""Slack Code channels: opening a channel from a mention, and routing inside one.

Each test names the nearby input it pins (retried event, private origin, a
channel Slack's UI opened, feature_disabled, toggle off, ...). Slack is a
recorded-payload fake; see ``slack_code_channels_helpers``.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, SELF_USER, Env, FakeState, make_env, mention

from kiro_crew.config.loader import ACTIVATION_OFF
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import routing
from kiro_crew.slack.code_channels import settings as cc_settings
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.creation import ORIGIN_CONTEXT_BLOCK_ID, session_id_for
from kiro_crew.slack.code_channels.routing import Gate
from kiro_crew.slack.code_channels.store import Binding

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


CLEAN = "fix the flaky login test and open a PR"


class _SlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


def _created(env: Env) -> list[dict[str, Any]]:
    return [body for method, body in env.api.calls if method == "agents.conversations.create"]


async def _open(env: Env, event: dict[str, Any] | None = None) -> bool:
    event = event or mention()
    return await routing.maybe_open_code_channel(env.orch, event, CLEAN, OWNER)


def _bind(env: Env, channel: str = "C0CODE", **kw: Any) -> Binding:
    binding = Binding(
        channel_id=channel,
        team_id="T1",
        session_key="chat-9-1",
        anchor_ts="300.000001",
        origin_channel_id=kw.pop("origin_channel_id", "C0ORIGIN"),
        origin_ts=kw.pop("origin_ts", "1700000000.000100"),
        **kw,
    )
    env.store.put_binding(binding)
    assert env.state is not None
    env.state.linked[binding.anchor_ts] = SimpleNamespace(key="chat-9-1", running=False)
    return binding


async def _gate(
    env: Env, event: dict[str, Any], *, authorized: bool = True
) -> routing.InboundResult:
    return await routing.inbound_gate(
        env.orch,
        event,
        text=event.get("text", ""),
        sender_id=event.get("user", ""),
        authorized=authorized,
        from_trusted_bot=False,
    )


# ── Opening a code channel from a mention ────────────────────────────────────


async def test_multi_step_mention_opens_a_code_channel_with_the_thread_as_context(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.slack.thread = [
        {"ts": "1700000000.000001", "user": "UALICE", "text": "parent: CI is red"},
        {"ts": "1700000000.000050", "user": "UALICE", "text": "login test flakes on CI"},
        {"ts": "1700000000.000100", "user": OWNER, "text": "the mention itself"},
    ]
    event = mention(thread_ts="1700000000.000001")
    assert await _open(env, event) is True

    [body] = _created(env)
    assert body["name"] == "fix-flaky-login-test"
    assert body["origin_channel_id"] == "C0ORIGIN" and body["origin_message_ts"] == event["ts"]
    assert body["session_id"] == session_id_for("BBOT", "T1", "C0ORIGIN", event["ts"])
    # Kickoff top-level in the new channel, then the pointer in the origin thread.
    kickoff, pointer = env.slack.posts
    assert kickoff[0] == "C0CODE" and kickoff[2] is None
    assert pointer == (
        "C0ORIGIN",
        "Opened <#C0CODE> for this. I'll work there.",
        event["thread_ts"],
    )
    # The bound session gets the request plus the earlier thread messages.
    [(channel, seed, sender)] = env.routed
    assert channel == "C0CODE" and sender == OWNER
    assert (
        seed.startswith(CLEAN)
        and "login test flakes on CI" in seed
        and "the mention itself" not in seed
    )
    binding = env.store.get_binding("C0CODE")
    assert binding is not None and binding.anchor_ts == "200.000001"
    assert binding.session_key == "chat-1-1" and binding.created_by_us
    assert binding.origin_link == ("C0ORIGIN", event["ts"])
    assert [r["outcome"] for r in env.sel.ops("slack.code_channel.create")] == ["allowed"]


async def test_one_line_question_is_answered_in_place(env: Env) -> None:
    env.decision = RouteDecision("stay")
    assert await _open(env) is False
    assert _created(env) == [] and env.slack.posts == [] and env.routed == []


async def test_retried_event_opens_no_second_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    assert await _open(env) is True  # Slack re-delivers the same event
    assert len(_created(env)) == 1
    assert len(env.classified) == 1  # the stored decision is reused
    assert len(env.routed) == 1


async def test_restart_between_create_and_bind_reuses_slack_idempotency(env: Env) -> None:
    calls: list[dict[str, Any]] = []

    def create(body: dict[str, Any]) -> Any:
        calls.append(body)
        return {"ok": True, "channel_id": "C0CODE"}

    env.api.responses["agents.conversations.create"] = create
    env.state = None  # no dashboard: the create never happens
    assert await _open(env) is False
    env.state = FakeState()
    assert await _open(env) is True
    assert len(calls) == 1


async def test_ambiguous_transport_failure_reconciles_with_the_same_session_id(env: Env) -> None:
    attempts: list[dict[str, Any]] = []

    def create(body: dict[str, Any]) -> Any:
        attempts.append(body)
        if len(attempts) == 1:
            return ConnectionResetError("dropped after send")
        return {"ok": True, "channel_id": "C0CODE"}

    env.api.responses["agents.conversations.create"] = create
    assert await _open(env) is True
    assert len(attempts) == 2 and attempts[0]["session_id"] == attempts[1]["session_id"]


async def test_private_origin_privacy_is_inherited_not_overridden(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "G0CODE"}
    assert await _open(env, mention(channel="G0PRIVATE")) is True
    [body] = _created(env)
    assert body["origin_channel_id"] == "G0PRIVATE"
    assert "is_private" not in body  # Slack defaults to the origin's privacy


@pytest.mark.parametrize("code", ["feature_disabled", "missing_scope"])
async def test_feature_unavailable_degrades_to_an_in_place_answer(env: Env, code: str) -> None:
    env.api.responses["agents.conversations.create"] = _SlackError(code)
    assert await _open(env) is False
    assert env.slack.posts == [] and env.routed == []
    [row] = env.sel.ops("slack.code_channel.create")
    assert row["outcome"] == "error" and row["error"] == code


async def test_toggle_on_without_the_scope_makes_no_slack_call_and_no_decision(env: Env) -> None:
    cc_settings.note_granted_scopes(["chat:write", "app_mentions:read"])
    assert await _open(env) is False
    _bind(env)
    result = await _gate(env, {**mention(channel="C0CODE"), "type": "message"})
    assert result.gate is Gate.PASS
    assert env.api.calls == [] and env.classified == [] and env.sel.rows == []


async def test_first_feature_disabled_latches_code_channels_off_for_the_process(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = _SlackError("feature_disabled")
    assert await _open(env) is False  # answered in place, nothing posted in Slack
    assert env.slack.posts == [] and env.slack.ephemerals == []
    calls_after_first = len(env.api.calls)
    # The next mention -- a different message -- is the normal path, untouched.
    assert await _open(env, mention(ts="1700000000.000200")) is False
    assert len(env.api.calls) == calls_after_first and len(env.classified) == 1
    _bind(env)
    result = await _gate(env, {**mention(channel="C0CODE"), "type": "message"})
    assert result.gate is Gate.PASS
    # A reconnect re-probes and clears the latch.
    cc_settings.note_granted_scopes([cc_settings.REQUIRED_SCOPE])
    env.info("C0CODE")
    later = {**mention(channel="C0CODE", ts="1700000000.000300"), "type": "message"}
    assert (await _gate(env, later)).gate is Gate.DIRECTED


async def test_a_per_user_refusal_does_not_latch(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = _SlackError("user_not_enabled")
    assert await _open(env) is False
    assert cc_settings.code_channels_on(env.cfg()) is True


async def test_toggle_off_makes_no_slack_call_and_no_decision(env: Env) -> None:
    env.on = False
    assert await _open(env) is False
    result = await _gate(env, {**mention(channel="C0CODE"), "type": "message"})
    assert result.gate is Gate.PASS
    assert (
        await routing.dispatch(
            env.orch, result, event=mention(), clean_text=CLEAN, sender_id=OWNER, was_mention=True
        )
        is False
    )
    assert env.api.calls == [] and env.classified == [] and env.sel.rows == []


async def test_mention_inside_an_existing_code_channel_opens_no_second_channel(env: Env) -> None:
    _bind(env)
    env.info("C0CODE")
    event = mention(channel="C0CODE")
    result = await _gate(env, event)
    assert result.gate is Gate.DIRECTED
    handled = await routing.dispatch(
        env.orch, result, event=event, clean_text=CLEAN, sender_id=OWNER, was_mention=True
    )
    assert handled is True
    assert _created(env) == [] and env.classified == []
    assert env.routed == [("C0CODE", CLEAN, OWNER)]


async def test_bot_messages_never_open_a_channel(env: Env) -> None:
    assert await _open(env, mention(bot_id="B0OTHER")) is False
    assert await _open(env, mention(subtype="message_changed")) is False
    assert env.classified == []


# ── Session settings for a new channel ───────────────────────────────────────


async def test_no_default_folder_files_at_top_level_on_slacks_default_agent(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    assert spec.folder_id == "" and spec.agent == "slack-default"
    assert spec.model == "" and spec.reasoning_effort == "" and spec.project_dir == ""


async def test_defaults_choose_folder_agent_model_and_effort(env: Env) -> None:
    assert env.state is not None
    env.state.folders.append({"id": "f1", "name": "Slack work", "default_agent": "folder-agent"})
    env.defaults = {
        "folder": "f1",
        "agent": "picked",
        "model": "m-1",
        "reasoning_effort": "high",
    }
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    assert (spec.folder_id, spec.agent, spec.model, spec.reasoning_effort) == (
        "f1",
        "picked",
        "m-1",
        "high",
    )
    assert "*Slack work* folder" in env.slack.posts[0][1]


async def test_folder_default_agent_wins_over_slacks_and_folder_without_project(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert env.state is not None
    env.state.folders.extend(
        [
            {"id": "parent", "name": "P", "default_agent": "parent-agent"},
            {"id": "f1", "name": "Child", "parent_id": "parent"},
        ]
    )
    env.defaults = {"folder": "f1"}
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/work/origin-repo")
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    assert spec.agent == "parent-agent"
    # The folder declares no project, so the origin thread's bound one is used.
    assert spec.project_dir == "/work/origin-repo"


async def test_a_deleted_default_folder_files_at_top_level(env: Env) -> None:
    env.defaults = {"folder": "gone"}
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    assert env.created_sessions[0]["spec"].folder_id == ""


# ── Inside a code channel ────────────────────────────────────────────────────


async def test_plain_message_in_our_channel_is_directed_without_a_mention(env: Env) -> None:
    _bind(env)
    env.info("C0CODE")
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.0",
        "text": "also fix lint",
    }
    result = await _gate(env, event)
    assert result.gate is Gate.DIRECTED
    assert [r["outcome"] for r in env.sel.ops("slack.code_channel.message")] == ["allowed"]


async def test_message_mentioning_only_other_people_is_left_to_them(env: Env) -> None:
    _bind(env)
    env.info("C0CODE")
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.0",
        "text": "<@UALICE> thoughts?",
    }
    assert (await _gate(env, event)).gate is Gate.DROP
    both = {**event, "ts": "302.0", "text": f"<@UALICE> <@{SELF_USER}> go ahead"}
    assert (await _gate(env, both)).gate is Gate.DIRECTED


async def test_slacks_origin_echo_is_suppressed_but_lookalikes_are_not(env: Env) -> None:
    _bind(env, origin_channel_id="C0ORIGIN", origin_ts="1700000000.000100")
    env.info("C0CODE")

    def echo(link: str, n: int = 0) -> dict[str, Any]:
        return {
            "type": "message",
            "user": OWNER,
            "channel": "C0CODE",
            "ts": f"301.{n:06d}",
            "text": "Context",
            "blocks": [
                {
                    "type": "context",
                    "block_id": ORIGIN_CONTEXT_BLOCK_ID,
                    "elements": [{"type": "mrkdwn", "text": f"<{link}|Context> from <#C0ORIGIN>"}],
                }
            ],
        }

    real = "https://acme.slack.com/archives/C0ORIGIN/p1700000000000100"
    assert (await _gate(env, echo(real))).gate is Gate.DROP
    for n, fake in enumerate(
        (
            "https://acme.slack.com/archives/C0OTHER/p1700000000000100",  # other origin
            "https://acme.slack.com/archives/C0ORIGIN/p1700000000000101",  # other ts
            "https://acme.slack.com.evil.io/archives/C0ORIGIN/p1700000000000100",  # other host
            "http://acme.slack.com/archives/C0ORIGIN/p1700000000000100",  # not https
        )
    ):
        assert (await _gate(env, echo(fake, n + 1))).gate is Gate.DIRECTED, fake


async def test_channel_opened_from_slacks_ui_is_adopted_on_first_message(env: Env) -> None:
    env.info("C0UI", origin=("C0ORIGIN", "1700000000.000100"), name="ui-task")
    event = {"type": "message", "user": OWNER, "channel": "C0UI", "ts": "301.0", "text": "start"}
    result = await _gate(env, event)
    assert result.gate is Gate.DIRECTED and result.binding is None
    assert await routing.dispatch(
        env.orch, result, event=event, clean_text="start", sender_id=OWNER, was_mention=False
    )
    binding = env.store.get_binding("C0UI")
    assert binding is not None and not binding.created_by_us
    assert binding.origin_link == ("C0ORIGIN", "1700000000.000100")
    assert env.routed == [("C0UI", "start", OWNER)]
    assert env.sel.ops("slack.code_channel.adopt")[0]["outcome"] == "allowed"


async def test_another_agents_code_channel_is_not_ours(env: Env) -> None:
    env.info("C0THEIRS", bots=("UOTHERBOT",))
    event = {"type": "message", "user": OWNER, "channel": "C0THEIRS", "ts": "301.0", "text": "hi"}
    assert (await _gate(env, event)).gate is Gate.PASS


async def test_assignment_is_revalidated_and_revocation_unbinds(env: Env) -> None:
    _bind(env)
    env.info("C0CODE", bots=("UOTHERBOT",))
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "hi"}
    assert (await _gate(env, event)).gate is Gate.PASS
    assert env.store.get_binding("C0CODE") is None


async def test_ordinary_channels_are_negatively_cached(env: Env) -> None:
    env.info("C0PLAIN", code=False)
    event = {"type": "message", "user": OWNER, "channel": "C0PLAIN", "ts": "301.0", "text": "hi"}
    assert (await _gate(env, event)).gate is Gate.PASS
    assert (await _gate(env, event)).gate is Gate.PASS
    assert env.api.methods().count("conversations.info") == 1


async def test_lookup_failure_keeps_the_binding_but_takes_the_normal_path(env: Env) -> None:
    _bind(env)
    env.api.responses["conversations.info"] = TimeoutError()
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "hi"}
    # Unknown is never read as ours: fail closed to today's behaviour.
    assert (await _gate(env, event)).gate is Gate.PASS
    assert env.store.get_binding("C0CODE") is not None


@pytest.mark.parametrize("code", ["feature_disabled", "missing_scope"])
async def test_unavailable_recognition_falls_back_to_the_normal_path(env: Env, code: str) -> None:
    _bind(env)
    env.api.responses["conversations.info"] = _SlackError(code)
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "hi"}
    assert (await _gate(env, event)).gate is Gate.PASS


async def test_user_who_is_not_allowed_is_denied_and_audited(env: Env) -> None:
    _bind(env)
    env.info("C0CODE")
    event = {
        "type": "message",
        "user": "USTRANGER",
        "channel": "C0CODE",
        "ts": "301.0",
        "text": "rm -rf",
    }
    assert (await _gate(env, event, authorized=False)).gate is Gate.DROP
    [row] = env.sel.ops("slack.code_channel.message")
    assert row["outcome"] == "denied" and row["caller"] == "USTRANGER"
    assert env.slack.ephemerals and env.slack.ephemerals[0][1] == "USTRANGER"
    assert env.routed == []


async def test_authorization_follows_the_origin_conversation(env: Env) -> None:
    _bind(env, origin_channel_id="C0ORIGIN")
    env.info("C0CODE")
    env.activation["C0ORIGIN"] = ACTIVATION_OFF
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "go"}
    assert (await _gate(env, event)).gate is Gate.DROP
    [row] = env.sel.ops("slack.code_channel.message")
    assert row["outcome"] == "denied" and "origin=C0ORIGIN" in row["resources"]


async def test_closed_dashboard_session_is_reopened_on_the_next_message(env: Env) -> None:
    _bind(env)
    assert env.state is not None
    env.state.linked.clear()  # the user closed the tab
    env.info("C0CODE")
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.0",
        "text": "continue",
    }
    result = await _gate(env, event)
    assert await routing.dispatch(
        env.orch, result, event=event, clean_text="continue", sender_id=OWNER, was_mention=False
    )
    binding = env.store.get_binding("C0CODE")
    assert binding is not None and binding.session_key == "chat-1-1"
    assert env.routed == [("C0CODE", "continue", OWNER)]


async def test_dms_are_never_code_channels(env: Env) -> None:
    event = {"type": "message", "user": OWNER, "channel": "D0DM", "ts": "301.0", "text": "hi"}
    assert (await _gate(env, event)).gate is Gate.PASS
    assert env.api.calls == []


# ── Stop ─────────────────────────────────────────────────────────────────────


async def test_native_stop_stops_the_bound_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    assert env.state is not None
    env.state.linked["300.000001"].running = True
    stopped: list[Any] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        stopped.append((slot.key, kwargs))
        return {"info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u == OWNER)
    event = {
        "type": "agent_session_stopped",
        "channel": "C0CODE",
        "user": OWNER,
        "streaming_message_ts": [],
    }
    assert await routing.handle_session_stopped(env.orch, event, OWNER) is True
    assert stopped == [("chat-9-1", {"source": "slack", "escalate": False})]
    assert env.sel.ops("slack.code_channel.stop")[0]["outcome"] == "allowed"


async def test_native_stop_when_idle_clears_the_status(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: True)
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) is True
    assert (
        "agents.sessions.setStatus",
        {"channel_id": "C0CODE", "status": "active"},
    ) in env.api.calls
    assert env.slack.posts == []  # the button gets no "Nothing running." reply


async def test_native_stop_from_a_stranger_is_denied(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: False)
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": "USTRANGER"}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) is True
    assert env.sel.ops("slack.code_channel.stop")[0]["outcome"] == "denied"


async def test_stop_for_a_thread_session_or_with_the_toggle_off_is_not_ours(env: Env) -> None:
    _bind(env)
    thread_stop = {"channel": "C0CODE", "thread_ts": "1.0", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, thread_stop, OWNER) is False
    env.on = False
    channel_stop = {"channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, channel_stop, OWNER) is False
    assert env.sel.rows == []


async def test_bang_stop_in_a_code_channel_stops_the_bound_session(env: Env) -> None:
    _bind(env)
    env.info("C0CODE")
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "!stop"}
    assert (await _gate(env, event)).gate is Gate.DROP
    assert env.slack.posts == [("C0CODE", "Nothing running.", None)]


async def test_whole_channel_stop_reaches_us_through_the_agent_session_runtime(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.agent_sessions import session_events

    _bind(env)
    monkeypatch.setattr("kiro_crew.slack.events.is_allowed_user", lambda u: u == OWNER)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u == OWNER)
    session_events.register_channel_stop_handler(routing.handle_session_stopped)
    try:
        env.orch.sessions = SimpleNamespace()
        event = {
            "type": "agent_session_stopped",
            "channel": "C0CODE",
            "user": OWNER,
            "event_ts": "9.1",
        }
        assert await session_events.handle_stopped(env.orch, event) == "channel"
    finally:
        session_events.unregister_channel_stop_handler(routing.handle_session_stopped)
    assert (
        "agents.sessions.setStatus",
        {"channel_id": "C0CODE", "status": "active"},
    ) in env.api.calls
