"""Slack Code channels: opening a channel from a mention, and routing inside one.

Each test names the nearby input it pins (retried event, private origin, a
channel Slack's UI opened, feature_disabled, toggle off, ...). Slack is a
recorded-payload fake; see ``slack_code_channels_helpers``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, SELF_USER, Env, FakeState, make_env, mention

from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import routing
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.creation import ORIGIN_CONTEXT_BLOCK_ID, session_id_for
from kiro_crew.slack.code_channels.routing import Gate
from kiro_crew.slack.code_channels.store import Binding, StoreWriteError
from kiro_crew.testing.wait import async_wait_until, until_parked

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


CLEAN = "fix the flaky login test and open a PR"
# The origin ts "1700000000.000100" as a Slack permalink spells it (no dot).
_TS = "1700000000000100"
# Deadline for joining a routing task, so a lock regression fails here instead of hanging.
_WAIT = 30.0


async def _join(*tasks: asyncio.Task[Any]) -> list[Any]:
    """Join routing tasks under ``_WAIT``; cancel whatever is still running on the way out."""
    try:
        return [await asyncio.wait_for(asyncio.shield(t), timeout=_WAIT) for t in tasks]
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


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
    assert binding.channel_name == "fix-flaky-login-test"
    assert binding.origin_link == ("C0ORIGIN", event["ts"])
    assert [r["outcome"] for r in env.sel.ops("slack.code_channel.create")] == ["allowed"]


async def test_one_line_question_is_answered_in_place(env: Env) -> None:
    env.decision = RouteDecision("stay")
    assert await _open(env) is False
    assert _created(env) == [] and env.slack.posts == [] and env.routed == []


async def test_the_mention_is_acknowledged_before_the_decision_is_made(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_at_decision: list[list[tuple[str, str, str]]] = []

    async def classify(_sessions: Any, _text: str, _context: Any = ()) -> RouteDecision:
        seen_at_decision.append(list(env.slack.reactions))
        return RouteDecision("stay")

    monkeypatch.setattr(routing, "classify", classify)
    event = mention()
    assert await _open(env, event) is False
    # The normal turn's "queued" reaction is on the message while the
    # classifier runs, so the up-to-timeout wait is never silent.
    assert seen_at_decision == [[("C0ORIGIN", event["ts"], "eyes")]]
    # A re-delivered event reuses the stored decision and reacts no second time.
    assert await _open(env, event) is False
    assert len(env.slack.reactions) == 1


async def test_no_acknowledgement_with_reactions_off_or_the_gate_shut(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.decision = RouteDecision("stay")
    base = env.cfg

    def no_reactions(_orch: Any = None) -> Any:
        cfg = base()
        cfg.slack.reactions_enabled = False
        return cfg

    monkeypatch.setattr(routing, "live_config", no_reactions)
    assert await _open(env, mention(ts="1.000001")) is False
    monkeypatch.setattr(routing, "live_config", base)
    env.on = False
    assert await _open(env, mention(ts="1.000002")) is False
    assert env.slack.reactions == []


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
    features.record_granted_scopes(["chat:write", "app_mentions:read"])
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
    # A reconnect re-probes from scratch (scope_probe clears the latch first).
    features.clear_latch()
    features.record_granted_scopes([features.REQUIRED_SCOPE[features.CODE_CHANNELS]])
    env.info("C0CODE")
    later = {**mention(channel="C0CODE", ts="1700000000.000300"), "type": "message"}
    assert (await _gate(env, later)).gate is Gate.DIRECTED


async def test_a_per_user_refusal_does_not_latch(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = _SlackError("user_not_enabled")
    assert await _open(env) is False
    assert features.code_channels_enabled(env.cfg()) is True


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


async def test_zero_config_files_into_code_channels_led_by_the_conductor(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    # No default folder: "Code channels" is created with the conductor as its
    # agent, and the folder's agent applies (nothing set explicitly).
    assert env.folders_created == ["Code channels"]
    assert env.state is not None
    [folder] = env.state.folders
    assert folder["default_agent"] == "kirocrew-conductor"
    assert spec.folder_id == folder["id"] and spec.agent == "kirocrew-conductor"
    assert spec.model == "" and spec.reasoning_effort == "" and spec.project_dir == ""


async def test_an_explicit_default_agent_still_wins_over_the_code_channels_folder(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.defaults.update(
        agent="kirocrew-team-lead", model="claude-opus-4.5", reasoning_effort="high"
    )
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    assert spec.agent == "kirocrew-team-lead"
    assert spec.model == "claude-opus-4.5" and spec.reasoning_effort == "high"
    assert env.state is not None and spec.folder_id == env.state.folders[0]["id"]


async def test_a_second_code_channel_reuses_the_code_channels_folder(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE2"}
    assert await _open(env, mention(ts="1700000000.000200")) is True
    assert env.folders_created == ["Code channels"]
    first, second = (s["spec"].folder_id for s in env.created_sessions)
    assert first == second


async def test_no_default_folder_files_at_top_level_when_the_folder_cannot_be_made(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(*_a: Any, **_k: Any) -> str:
        raise ValueError("folder cap reached")

    monkeypatch.setattr(binding_mod, "ensure_folder_path", refused)
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    # Unfiled, but still the agent the auto folder would have carried: the
    # resolver answers for a not-yet-created auto folder as if it existed.
    assert spec.folder_id == "" and spec.agent == features.AUTO_FOLDER_AGENT


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
            {"id": "parent", "name": "P"},
            {"id": "f1", "name": "Child", "parent_id": "parent", "default_agent": "folder-agent"},
        ]
    )
    env.defaults = {"folder": "f1"}
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/work/origin-repo")
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env) is True
    spec = env.created_sessions[0]["spec"]
    assert spec.agent == "folder-agent"
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


@pytest.mark.parametrize("modifier", ["!incognito", "!temporary"])
async def test_a_privacy_modifier_in_a_code_channel_is_refused_not_run(
    env: Env, modifier: str
) -> None:
    """A code channel is one persistent session, so a no-memory request is
    refused with a notice and audited; nothing reaches the session."""
    _bind(env)
    env.info("C0CODE")
    text = f"{modifier} rotate the staging key"
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": text}
    result = await _gate(env, event)
    assert result.gate is Gate.DIRECTED
    handled = await routing.dispatch(
        env.orch, result, event=event, clean_text=text, sender_id=OWNER, was_mention=False
    )
    assert handled is True
    assert env.routed == []
    assert [p for p in env.slack.posts if routing.PRIVACY_REFUSED_TEXT in str(p)]
    denied = [r for r in env.sel.ops("slack.code_channel.message") if r["outcome"] == "denied"]
    assert denied and "privacy modifier" in denied[0]["error"]


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

    real = f"https://acme.slack.com/archives/C0ORIGIN/p{_TS}"
    assert (await _gate(env, echo(real))).gate is Gate.DROP
    for n, fake in enumerate(
        (
            f"https://acme.slack.com/archives/C0OTHER/p{_TS}",  # other origin
            f"https://acme.slack.com/archives/C0ORIGIN/p{_TS[:-1]}1",  # other ts
            f"https://acme.slack.com.evil.io/archives/C0ORIGIN/p{_TS}",  # other host
            f"http://acme.slack.com/archives/C0ORIGIN/p{_TS}",  # not https
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
    assert binding.channel_name == "ui-task"
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


async def test_a_revoked_channels_anchor_thread_no_longer_reaches_the_old_session(
    env: Env,
) -> None:
    """The dashboard session stays linked to the old anchor after the unbind, so
    every message in that thread (the one that found the revocation, a later
    mention, an unmentioned reply) is dropped and audited, not passed to the
    ordinary linked-thread path. Other messages in the channel pass; a rebind
    reopens the thread."""
    _bind(env)
    env.info("C0CODE", bots=("UOTHERBOT",))
    anchor = "300.000001"

    def in_anchor(ts: str, text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "user": OWNER,
            "channel": "C0CODE",
            "ts": ts,
            "thread_ts": anchor,
            "text": text,
        }

    assert (await _gate(env, in_anchor("301.0", f"<@{SELF_USER}> go on"))).gate is Gate.DROP
    assert env.store.get_binding("C0CODE") is None
    assert (await _gate(env, in_anchor("302.0", f"<@{SELF_USER}> again"))).gate is Gate.DROP
    assert (await _gate(env, in_anchor("303.0", "unmentioned reply"))).gate is Gate.DROP
    denied = [r for r in env.sel.ops("slack.code_channel.message") if r["outcome"] == "denied"]
    assert len(denied) == 2 and all("no longer bound" in r["error"] for r in denied)
    elsewhere = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "304.0", "text": "hi"}
    assert (await _gate(env, elsewhere)).gate is Gate.PASS
    _bind(env)
    env.info("C0CODE")
    assert env.store.revoked("C0CODE") is None
    await _gate(env, in_anchor("305.0", "after a rebind"))
    assert not [
        r
        for r in env.sel.ops("slack.code_channel.message")
        if "no longer bound" in (r.get("error") or "")
    ][2:]


async def test_the_message_finding_a_revocation_is_dropped_even_if_the_unbind_is_not_saved(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    env.info("C0CODE", bots=("UOTHERBOT",))

    def unsaved(_channel: str) -> bool:
        raise StoreWriteError("disk full")

    monkeypatch.setattr(env.store, "drop_binding", unsaved)
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.0",
        "thread_ts": "300.000001",
        "text": f"<@{SELF_USER}> go on",
    }
    assert (await _gate(env, event)).gate is Gate.DROP


async def test_paired_deliveries_of_one_stop_stop_once(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack's ``message`` and ``app_mention`` for one ``@bot !stop`` arrive as
    concurrent tasks; the second waits on the first's verdict instead of
    pressing Stop again (which would escalate to a hard kill)."""
    _bind(env)
    env.info("C0CODE")
    assert env.state is not None
    env.state.linked["300.000001"].running = True
    calls: list[bool] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["escalate"])
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    real_classify = routing.recognition.classify_channel
    lookup = asyncio.Event()

    async def held_classify(*args: Any, **kwargs: Any) -> Any:
        await lookup.wait()  # the channel lookup is still in flight
        return await real_classify(*args, **kwargs)

    monkeypatch.setattr(routing.recognition, "classify_channel", held_classify)
    text = f"<@{SELF_USER}> !stop"
    message = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": text}
    app_mention = {**message, "type": "app_mention"}
    first = asyncio.create_task(_gate(env, message))
    await until_parked(lookup, timeout=_WAIT)
    second = asyncio.create_task(_gate(env, app_mention))
    # The second delivery is parked too (on the first's verdict, or -- without
    # the in-flight record -- on its own lookup) before the lookup answers.
    await async_wait_until(lambda: second._fut_waiter is not None, timeout=_WAIT)
    lookup.set()
    results = await _join(first, second)
    assert [r.gate for r in results] == [Gate.DROP, Gate.DROP]
    assert calls == [True]
    assert routing._gate_pending == {}


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


@pytest.mark.parametrize("failure", ["timeout", "feature_disabled"])
@pytest.mark.parametrize("activation", ["review", "off"])
async def test_a_failed_lookup_still_honours_the_stored_origins_gate(
    env: Env, failure: str, activation: str
) -> None:
    from kiro_crew.config.loader import ACTIVATION_REVIEW

    _bind(env, origin_channel_id="C0ORIGIN")
    env.activation["C0ORIGIN"] = ACTIVATION_REVIEW if activation == "review" else ACTIVATION_OFF
    env.api.responses["conversations.info"] = (
        TimeoutError() if failure == "timeout" else _SlackError(failure)
    )
    event = {
        "type": "app_mention",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.0",
        "thread_ts": "300.000001",
        "text": f"<@{SELF_USER}> ship it",
    }
    # Unrecognized is not ours, but the bound origin's review/off still holds:
    # the reply must not take the normal path and publish unreviewed.
    assert (await _gate(env, event)).gate is Gate.DROP
    [row] = env.sel.ops("slack.code_channel.message")
    assert row["outcome"] == "denied" and "origin=C0ORIGIN" in row["resources"]


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
    env.store.update_binding("C0CODE", channel_name="fix-login", wrapped_up_at=7.0)
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
    # The channel's own facts carry over; only the session is new.
    assert binding.channel_name == "fix-login" and binding.wrapped_up_at == 7.0
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
    assert stopped == [("chat-9-1", {"source": "slack", "escalate": True})]
    assert env.sel.ops("slack.code_channel.stop")[0]["outcome"] == "allowed"


async def test_a_stop_declined_by_compaction_says_so_and_a_fresh_press_can_escalate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each fresh press escalates (the compaction escape is reachable from Slack),
    the decline is reported in the channel, and a redelivered ``!stop`` stops
    nothing a second time."""
    _bind(env)
    env.info("C0CODE")
    assert env.state is not None
    env.state.linked["300.000001"].running = True
    calls: list[bool] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["escalate"])
        if len(calls) == 1:
            return {"ok": True, "info": "compacting", "compacting": True}
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u == OWNER)
    bang = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": "301.0", "text": "!stop"}
    assert (await _gate(env, bang)).gate is Gate.DROP
    assert (await _gate(env, dict(bang))).gate is Gate.DROP  # Slack's redelivery
    assert calls == [True]
    assert [p for p in env.slack.posts if routing.COMPACTING_STOP_TEXT in str(p)]
    native = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, native, OWNER) is True
    assert calls == [True, True]


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


async def test_the_first_message_is_the_request_alone_with_the_framing_as_context(
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    queued: list[dict[str, Any]] = []
    real_create = routing.create_bound_session

    async def create_with_a_context_queue(state: Any, spec: Any, **kwargs: Any) -> Any:
        slot = await real_create(state, spec, **kwargs)
        slot._pending_context = queued
        slot._deferred_notes = []
        slot.append_pending_context = lambda entry: queued.append(entry) is None
        return slot

    monkeypatch.setattr(routing, "create_bound_session", create_with_a_context_queue)
    assert await _open(env, mention(thread_ts="1700000000.000001")) is True
    # The code channel's echo (and the dashboard row) show only what was asked.
    [(_, message, _)] = env.routed
    assert message == CLEAN.strip()
    [entry] = queued
    assert entry["source"] == routing.SEED_CONTEXT_SOURCE
    assert entry["content"].startswith("[Slack code channel]")


async def test_a_binding_that_cannot_be_saved_leaves_no_session_behind(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}

    def put_fails(_binding: Any) -> Any:
        raise StoreWriteError("disk full")

    monkeypatch.setattr(env.store, "put_binding", put_fails)
    # The error reaches create_bound_session's before_publish, which is where
    # the real implementation retracts the slot and its link.
    assert await _open(env) is True
    assert env.store.get_binding("C0CODE") is None
    assert env.routed == []
    assert "could not start its session" in env.slack.posts[-1][1]


@pytest.mark.parametrize("modifier", ["!temporary", "!incognito"])
async def test_a_privacy_modifier_keeps_the_request_on_the_normal_path(
    env: Env, modifier: str
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    text = f"{modifier} {CLEAN}"
    event = mention(text=f"<@{SELF_USER}> {text}")
    # Classified or not, a request that asks for no persistent memory never
    # lands in a code channel's persistent session.
    assert await routing.maybe_open_code_channel(env.orch, event, text, OWNER) is False
    assert _created(env) == [] and env.classified == [] and env.routed == []
    [row] = env.sel.ops("slack.code_channel.open")
    assert row["outcome"] == "denied"


async def test_a_thread_already_in_a_privacy_mode_keeps_the_request_on_the_normal_path(
    env: Env,
) -> None:
    from kiro_crew.messaging import privacy_mode

    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    event = mention(thread_ts="1700000000.000001")
    privacy_mode.mark_incognito("slack:1700000000.000001")  # the key handle_message marks
    try:
        assert await routing.maybe_open_code_channel(env.orch, event, CLEAN, OWNER) is False
    finally:
        privacy_mode.reset()
    assert _created(env) == [] and env.classified == []


async def test_a_trusted_bot_in_a_bound_channel_honours_the_stored_origin(env: Env) -> None:
    _bind(env, origin_channel_id="C0ORIGIN")
    env.activation["C0ORIGIN"] = ACTIVATION_OFF
    event = {"type": "message", "bot_id": "BOTHER", "channel": "C0CODE", "ts": "301.0"}
    event["text"] = "refactor the login module"
    result = await routing.inbound_gate(
        env.orch,
        event,
        text=event["text"],
        sender_id="BOTHER",
        authorized=True,
        from_trusted_bot=True,
    )
    assert result.gate is Gate.DROP
    [row] = env.sel.ops("slack.code_channel.message")
    assert row["outcome"] == "denied" and "origin=C0ORIGIN" in row["resources"]


async def test_attachment_temps_of_a_message_no_session_took_are_deleted(
    env: Env, tmp_path: Any
) -> None:
    _bind(env)
    env.info("C0CODE")
    env.route_result = False
    upload = tmp_path / "log.txt"
    upload.write_text("x")
    event = mention(channel="C0CODE")
    result = await _gate(env, event)
    assert await routing.dispatch(
        env.orch,
        result,
        event=event,
        clean_text=CLEAN,
        sender_id=OWNER,
        was_mention=True,
        attachment_paths=[str(upload)],
    )
    assert not upload.exists()


async def test_queued_uploads_survive_the_turns_ahead_of_them(env: Env, tmp_path: Any) -> None:
    binding = _bind(env)
    env.info("C0CODE")
    assert env.state is not None
    slot = env.state.linked[binding.anchor_ts]
    queued = [0]
    slot.queue_depth = lambda: queued[0]
    uploads = []
    for n in (1, 2):
        upload = tmp_path / f"shot{n}.png"
        upload.write_bytes(b"png")
        uploads.append(upload)
        event = mention(channel="C0CODE", ts=f"30{n}.0")
        result = await _gate(env, event)
        assert await routing.dispatch(
            env.orch,
            result,
            event=event,
            clean_text=CLEAN,
            sender_id=OWNER,
            was_mention=True,
            attachment_paths=[str(upload)],
        )
    # A's turn ends while B is still queued behind it: B's upload must survive
    # (and A's too, since the files are released together when the queue drains).
    turn = routing.hooks.TurnEvent(channel_id="C0CODE", session_key="chat-9-1", source="slack")
    queued[0] = 1
    await routing._temps_on_turn_end(binding, turn)
    assert all(u.exists() for u in uploads)
    # B's turn ends with nothing queued: both are released.
    queued[0] = 0
    await routing._temps_on_turn_end(binding, turn)
    assert not any(u.exists() for u in uploads)


async def test_a_lost_seed_receipt_is_reconciled_from_the_session_not_replayed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}

    def receipt_fails(_session_id: str) -> Any:
        raise StoreWriteError("disk full")

    real_record = env.store.record_seeded
    monkeypatch.setattr(env.store, "record_seeded", receipt_fails)
    assert await _open(env) is True
    assert len(env.routed) == 1
    # The session accepted the request: its transcript holds the message.
    assert env.state is not None
    [slot] = [s for s in env.state.linked.values() if s.key == "chat-1-1"]
    slot.messages = [{"role": "user", "content": CLEAN}]
    monkeypatch.setattr(env.store, "record_seeded", real_record)
    routing._gate_seen.clear()
    assert await _open(env) is True  # re-delivered after a restart
    assert len(env.routed) == 1  # not replayed
    route = next(iter(env.store._routes.values()))
    assert route.seeded_at  # and the receipt is now recorded


async def test_no_receipt_is_written_until_the_first_turn_is_on_disk(env: Env) -> None:
    """A crash after the receipt but before the request is saved would lose the
    request for good (re-delivery skips a seeded route), so an unsaved first
    turn leaves the route unseeded and posts no "Opened" note; the re-delivery
    then finishes the job."""
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.persist_result = False
    assert await _open(env) is True
    assert len(env.routed) == 1 and env.persisted == ["C0CODE"]
    route = next(iter(env.store._routes.values()))
    assert not route.seeded_at
    assert not [p for p in env.slack.posts if "Opened <#C0CODE>" in str(p)]
    env.persist_result = True
    routing._gate_seen.clear()
    assert await _open(env) is True  # re-delivered after the restart
    assert next(iter(env.store._routes.values())).seeded_at
    assert [p for p in env.slack.posts if "Opened <#C0CODE>" in str(p)]


async def test_persisting_the_first_turn_saves_the_bound_session_strictly(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``_persist_request``: a confirmed save (``best_effort=False``),
    False on a refused or failed save, and False with no session."""
    from kiro_crew.dashboard import chat_persistence

    binding = _bind(env)
    calls: list[dict[str, Any]] = []

    async def save(state: Any, slot: Any, **kw: Any) -> bool:
        calls.append(kw)
        return outcome.pop(0)

    outcome: list[Any] = [True, False]
    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", save)
    monkeypatch.setattr(routing, "_dashboard_state", lambda: env.state)
    monkeypatch.setattr(routing, "bound_slot", lambda _s, _b: SimpleNamespace(key="chat-9-1"))
    assert await env.real_persist_request(binding) is True
    assert await env.real_persist_request(binding) is False
    assert calls[0]["best_effort"] is False and calls[0]["force"] is True

    async def broken(state: Any, slot: Any, **kw: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", broken)
    assert await env.real_persist_request(binding) is False
    monkeypatch.setattr(routing, "bound_slot", lambda _s, _b: None)
    assert await env.real_persist_request(binding) is False


# ── Thread follow-ups: which unmentioned replies reach the classifier ───────

_ROOT = "1700000000.000100"


class _Sessions:
    """The orchestrator's session index: the bot holds the *held* threads."""

    def __init__(self, *held: str) -> None:
        self.held = set(held)

    def has_session(self, ts: str) -> bool:
        return ts in self.held

    def get_session_for_thread(self, ts: str) -> None:
        return None


def _reply(ts: str = "1700000000.000200", **extra: Any) -> dict[str, Any]:
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0ORIGIN",
        "ts": ts,
        "thread_ts": _ROOT,
        "text": "grab an issue from the backlog",
        "team": "T1",
    }
    event.update(extra)
    return event


async def _dispatch_reply(env: Env, event: dict[str, Any]) -> bool:
    return await routing.dispatch(
        env.orch,
        routing.PASS,
        event=event,
        clean_text=str(event["text"]),
        sender_id=str(event["user"]),
        was_mention=False,
    )


async def test_unmentioned_message_in_a_thread_the_bot_is_not_in_is_not_classified(
    env: Env,
) -> None:
    env.orch.sessions = _Sessions()  # the bot holds no thread here
    assert await _dispatch_reply(env, _reply()) is False
    assert env.classified == [] and env.slack.reactions == [] and env.api.calls == []


async def test_a_follow_up_is_classified_once_however_often_slack_delivers_it(env: Env) -> None:
    env.orch.sessions = _Sessions(_ROOT)
    env.decision = RouteDecision("stay")
    assert await _dispatch_reply(env, _reply()) is False
    assert await _dispatch_reply(env, _reply()) is False  # re-delivered
    assert env.classified == ["grab an issue from the backlog"]
    assert len(env.slack.reactions) == 1  # acknowledged once, before the decision


@pytest.mark.parametrize(
    "extra",
    [
        {"bot_id": "B0OTHER"},
        {"subtype": "message_changed"},
        {"subtype": "channel_join"},
        {"text": "<@UALICE> can you take this one?"},
        {"text": "!agent kirocrew-team-lead"},
        {"channel": "D0DM"},
        {"thread_ts": "1700000000.000200"},  # a thread's own root, not a reply
    ],
)
async def test_only_a_persons_reply_addressed_to_us_is_classified(
    env: Env, extra: dict[str, Any]
) -> None:
    env.orch.sessions = _Sessions(_ROOT, "1700000000.000200")
    assert await _dispatch_reply(env, _reply(**extra)) is False
    assert env.classified == []


async def test_follow_ups_skip_the_classifier_with_code_channels_off(env: Env) -> None:
    env.orch.sessions = _Sessions(_ROOT)
    env.on = False
    assert await _dispatch_reply(env, _reply()) is False
    assert env.classified == [] and env.api.calls == []


async def test_follow_ups_skip_the_classifier_in_a_review_mode_origin(env: Env) -> None:
    env.orch.sessions = _Sessions(_ROOT)
    env.activation["C0ORIGIN"] = ACTIVATION_REVIEW
    assert await _dispatch_reply(env, _reply()) is False
    assert env.classified == []


async def test_a_thread_whose_root_mention_opened_a_channel_is_not_classified(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    assert await _open(env, mention(ts=_ROOT)) is True
    assert len(env.classified) == 1
    env.orch.sessions = _Sessions(_ROOT)
    assert await _dispatch_reply(env, _reply()) is False
    assert len(env.classified) == 1


async def test_concurrent_follow_ups_in_one_thread_open_one_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.orch.sessions = _Sessions(_ROOT)
    gate = asyncio.Event()
    real = routing.classify

    async def slow_classify(sessions: Any, text: str, context: Any = ()) -> RouteDecision:
        await gate.wait()
        return await real(sessions, text, context)

    monkeypatch.setattr(routing, "classify", slow_classify)
    first = asyncio.create_task(_dispatch_reply(env, _reply()))
    second = asyncio.create_task(_dispatch_reply(env, _reply(ts="1700000000.000300")))
    await asyncio.sleep(0)
    gate.set()
    # The second waited, then saw the channel the first opened.
    assert await _join(first, second) == [True, False]
    assert len(env.classified) == 1
    assert routing._follow_up_locks == {}


async def test_the_follow_up_that_opened_a_channel_still_finds_it_when_redelivered(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.orch.sessions = _Sessions(_ROOT)
    env.route_result = False  # the first turn does not get there (a restart)
    assert await _dispatch_reply(env, _reply()) is True
    assert [r for r in env.store._routes.values() if r.seeded_at] == []
    env.route_result = True
    routing._gate_seen.clear()
    # Slack re-delivers it: it is the thread's opener, not a later reply, so it
    # is handled here (never answered in the thread) and its first turn lands.
    assert await _dispatch_reply(env, _reply()) is True
    assert len(env.classified) == 1 and len(env.routed) == 2
    assert [r.seeded_at > 0 for r in env.store._routes.values()] == [True]


async def test_a_mention_and_a_reply_in_one_thread_open_one_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    env.orch.sessions = _Sessions(_ROOT)
    gate = asyncio.Event()
    real = routing.classify

    async def slow_classify(sessions: Any, text: str, context: Any = ()) -> RouteDecision:
        await gate.wait()
        return await real(sessions, text, context)

    monkeypatch.setattr(routing, "classify", slow_classify)
    root_mention = mention(ts="1700000000.000150", thread_ts=_ROOT)
    mention_task = asyncio.create_task(
        routing.dispatch(
            env.orch,
            routing.PASS,
            event=root_mention,
            clean_text=CLEAN,
            sender_id=OWNER,
            was_mention=True,
        )
    )
    await asyncio.sleep(0)
    reply = asyncio.create_task(_dispatch_reply(env, _reply()))
    await asyncio.sleep(0)
    gate.set()
    # The reply waited for the mention's decision, then saw its channel.
    assert await _join(mention_task, reply) == [True, False]
    assert len(_created(env)) == 1 and len(env.classified) == 1
    assert routing._follow_up_locks == {}


async def test_a_redelivered_opener_finds_its_channel_after_a_later_one_opened(
    env: Env,
) -> None:
    created = iter(["C0CODE", "C0SECOND"])
    env.api.responses["agents.conversations.create"] = lambda _b: {
        "ok": True,
        "channel_id": next(created),
    }
    env.orch.sessions = _Sessions(_ROOT)
    assert await _dispatch_reply(env, _reply()) is True
    # A later mention in the thread opens a second channel (mention behaviour).
    assert await _open(env, mention(ts="1700000000.000300", thread_ts=_ROOT)) is True
    routing._gate_seen.clear()
    env.orch.sessions = _Sessions()  # after a restart: no thread session in memory
    # The first reply, re-delivered, is still its channel's opener.
    assert await _dispatch_reply(env, _reply()) is True
    assert len(env.classified) == 2 and len(env.routed) == 2
