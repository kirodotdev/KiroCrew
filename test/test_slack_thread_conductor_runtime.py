"""A Conductor thread's worker approval cards, Stop and restart resume (RFC part D).

A Conductor thread is a code-channel binding whose room is one thread of an
ordinary channel (``thread_bindings``). Its runtime paths ask
``thread_runtime.room_runtime_on`` instead of ``code_channels_on``, so they work
with code channels OFF and no code-channel scope, owner-only, and all stay off
when ``slack.thread_conductor`` is off. A code channel keeps every rule it has
(its own suites pass unchanged).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

# The trust-card harness: fake Slack, dashboard, store, SEL and owner check.
from slack_code_channel_surface_helpers import SLOT, env_fixture  # noqa: F401
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_trust_from_slack import _Sessions

import kiro_crew.dashboard.chat_handlers as chat_handlers
import kiro_crew.slack.handler as slack_handler
from kiro_crew import safety_override as safety_override_mod
from kiro_crew.config.loader import ACTIVATION_MENTION, ChannelConfig
from kiro_crew.config.section_builders import _build_slack_config as build_slack_config
from kiro_crew.slack.code_channels import (
    agent_picker,
    approval_picker,
    audience,
)
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import (
    resume,
    routing,
    surface,
    thread_runtime,
)
from kiro_crew.slack.code_channels import worker_approvals as wa
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import ROOM_PRIVATE, ROOM_PUBLIC, Binding
from kiro_crew.slack.handler import _linked_approvals

PRIVATE = "G0PRIVATE01"
PUBLIC = "C0PUBLIC001"
TEAM = "T1"
ANCHOR = "200.000002"
CONDUCTOR = "chat-9-1"
WORKER = "chat-5-1"
REQUEST = "req-w1"
SHARED = "U0SHARED001"
MEMBER = "U0MEMBER001"


def _slot(key: str, *, channel: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        running=False,
        turn_running=False,
        linked_session_key="",
        _trust=False,
        _trust_reads=False,
        _trust_scope="",
        _approval_futures={},
        approval_instance=lambda request_id, message=None: None,
        messages=[],
        _dirty=False,
        _slack_channel=channel,
        _slack_thread_ts=ANCHOR if channel else "",
        _slack_linked=bool(channel),
        title=key,
        agent="",
    )


@pytest.fixture
def thread(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private-channel Conductor thread with one worker, code channels OFF."""
    e = env
    e.thread_on = True
    e.public_ok = False

    def cfg(_orch: Any = None) -> Any:
        return SimpleNamespace(
            slack=SimpleNamespace(
                code_channels=False,
                thread_conductor=e.thread_on,
                thread_conductor_public_channels=e.public_ok,
                code_channel_defaults=SimpleNamespace(
                    folder="", agent="", model="", reasoning_effort=""
                ),
            ),
            agent=SimpleNamespace(default_agent="slack-default"),
            channel_config=lambda ch: ChannelConfig(activation=ACTIVATION_MENTION),
        )

    e.cfg = cfg
    for mod in (routing, cc_binding, wa, approval_picker, agent_picker, resume):
        monkeypatch.setattr(mod, "live_config", cfg)
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    monkeypatch.setattr(slack_handler, "is_owner", lambda u: u == OWNER)
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda u: u == OWNER)
    monkeypatch.setattr(surface, "workspace_admitted", lambda team: team == TEAM)
    from kiro_crew.slack import features

    features.reset_capabilities()
    features.record_granted_scopes(["chat:write"])  # no code-channel scope at all
    audience.clear()
    audience.note_channel_info(
        ChannelInfo(
            channel_id=PRIVATE,
            is_code_channel=False,
            is_private=True,
            shared=False,
            archived=False,
        )
    )
    conductor = _slot(CONDUCTOR, channel=PRIVATE)
    worker = _slot(WORKER)
    worker._created_by = CONDUCTOR
    worker._lineage_minted = True
    worker._posture_inherited = True
    worker.title = "Fix #1"
    e.conductor, e.worker = conductor, worker
    e.binding = e.store.put_thread_binding(
        Binding(
            channel_id=PRIVATE,
            team_id=TEAM,
            session_key=CONDUCTOR,
            anchor_ts=ANCHOR,
            thread=True,
            room=ROOM_PRIVATE,
        )
    )
    e.state.linked[ANCHOR] = conductor
    e.state._slots = {CONDUCTOR: conductor, WORKER: worker}
    e.state.get_slot = lambda key: e.state._slots.get(key)
    e.state.sessions = _Sessions()
    e.state._approval_futures = {}
    e.state.channel_manager = None
    e.state.broadcast_ws = lambda *a, **k: None
    e.resolved = []

    def resolve_slot_approval(slot: Any, request_id: str, approved: bool, *, expected_future=None):
        e.resolved.append((slot.key, request_id, approved))
        fut = slot._approval_futures.get(request_id)
        if fut is None or fut is not expected_future or fut.done():
            return False
        fut.set_result("approved" if approved else "rejected")
        return True

    e.state.resolve_slot_approval = resolve_slot_approval
    e.mode_rows = []
    monkeypatch.setattr(
        chat_handlers,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: e.mode_rows.append(kw)),
    )
    monkeypatch.setattr(safety_override_mod, "approval_mode_permitted", lambda _m: True)
    approval_picker.reset()
    wa.reset()
    _linked_approvals.clear()
    yield e
    wa.reset()
    _linked_approvals.clear()
    audience.clear()


async def _relay(e: Any, slot: Any | None = None) -> tuple[Any, asyncio.Future]:
    slot = slot or e.worker
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures[REQUEST] = fut
    card = await wa.relay(e.state, slot, f"dashboard:{slot.key}", REQUEST, "Run shell", "npm", fut)
    return card, fut


def _click(user: str, card: Any, action_id: str, team: str = TEAM) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "channel": {"id": card.channel},
        "message": {"ts": card.ts},
        "actions": [{"action_id": action_id, "value": card.request_id}],
    }


# ── The runtime gate ──────────────────────────────────────────────────────


def test_the_runtime_gate_is_the_thread_switch_not_the_code_channel_scope(thread: Any) -> None:
    assert cc_binding.code_channels_on(thread.cfg()) is False
    assert thread_runtime.room_runtime_on(thread.binding, thread.cfg()) is True
    thread.thread_on = False
    assert thread_runtime.room_runtime_on(thread.binding, thread.cfg()) is False


# ── Worker approval cards ─────────────────────────────────────────────────


def test_a_worker_card_is_posted_in_the_thread_with_trust_this_thread(thread: Any) -> None:
    card, fut = asyncio.run(_relay(thread))
    assert card is not None and not fut.done()
    channel, blocks, _text, thread_ts = thread.slack.blocks_posts[-1]
    assert (channel, thread_ts) == (PRIVATE, ANCHOR)  # in the thread, never top level
    assert "Trust this thread" in str(blocks) and "Trust this channel" not in str(blocks)
    assert "this channel" not in str(blocks)


def test_the_owner_answers_the_card_in_the_thread(thread: Any) -> None:
    async def go() -> asyncio.Future:
        card, fut = await _relay(thread)
        assert await wa.handle_click(thread.orch, _click(OWNER, card, wa.APPROVE_ACTION_ID))
        return fut

    fut = asyncio.run(go())
    assert fut.result() == "approved"
    assert thread.resolved == [(WORKER, REQUEST, True)]


@pytest.mark.parametrize("user", [SHARED, MEMBER])
def test_a_shared_user_or_other_member_cannot_press_the_card(thread: Any, user: str) -> None:
    async def go() -> asyncio.Future:
        card, fut = await _relay(thread)
        for action in (wa.APPROVE_ACTION_ID, wa.REJECT_ACTION_ID, wa.TRUST_ACTION_ID):
            assert await wa.handle_click(thread.orch, _click(user, card, action))
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and thread.resolved == []
    assert thread.conductor._trust is False and thread.worker._trust is False
    assert thread.mode_rows == []


def test_trust_this_thread_raises_the_conductor_and_its_worker_only(thread: Any) -> None:
    async def go() -> None:
        card, _fut = await _relay(thread)
        assert await wa.handle_click(thread.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))

    asyncio.run(go())
    assert thread.conductor._trust is True
    assert thread.worker._trust is True  # follows its creator, never above it
    assert [r for r in thread.mode_rows if r["operation"] == "mode_change:trust"]
    warned = [p for p in thread.slack.posts if "*Trust*" in p[1]]
    assert warned and warned[-1][0] == PRIVATE and "this thread" in warned[-1][1]
    assert warned[-1][2] == ANCHOR  # said in the thread


def test_a_worker_is_never_above_the_thread(thread: Any) -> None:
    """Lowering the thread to Normal takes the worker down with it."""
    thread.conductor._trust = thread.worker._trust = True
    binding = thread.binding

    async def go() -> Any:
        return await approval_picker.set_channel_mode(
            binding, "normal", OWNER, via="test", orch=thread.orch, team=TEAM
        )

    out = asyncio.run(go())
    assert out.result == "set"
    assert thread.conductor._trust is False and thread.worker._trust is False


def test_everything_is_off_when_the_switch_is_off(thread: Any) -> None:
    async def go() -> tuple[Any, bool]:
        card, fut = await _relay(thread)
        assert card is not None
        thread.thread_on = False
        handled = await wa.handle_click(thread.orch, _click(OWNER, card, wa.APPROVE_ACTION_ID))
        assert not fut.done()
        thread.slack.blocks_posts.clear()
        again, _ = await _relay(thread)  # a new prompt after the switch went off
        return again, handled

    again, handled = asyncio.run(go())
    assert handled is False  # not ours to answer: the click falls through
    assert again is None and thread.slack.blocks_posts == []
    assert thread.resolved == []


def test_a_public_room_gets_no_card_without_the_opt_in(thread: Any) -> None:
    thread.store.put_thread_binding(
        Binding(
            channel_id=PUBLIC,
            team_id=TEAM,
            session_key="chat-8-1",
            anchor_ts="300.000003",
            thread=True,
            room=ROOM_PUBLIC,
        )
    )
    other = _slot("chat-8-1", channel=PUBLIC)
    other._slack_thread_ts = "300.000003"
    thread.state.linked["300.000003"] = other
    audience.note_channel_info(
        ChannelInfo(
            channel_id=PUBLIC, is_code_channel=False, is_private=False, shared=False, archived=False
        )
    )
    thread.state._slots["chat-8-1"] = other
    worker = _slot("chat-8-2")
    worker._created_by = "chat-8-1"
    worker._lineage_minted = True
    worker._posture_inherited = True
    thread.state._slots["chat-8-2"] = worker
    card, _ = asyncio.run(_relay(thread, worker))
    assert card is None and thread.slack.blocks_posts == []
    thread.public_ok = True
    card, _ = asyncio.run(_relay(thread, worker))
    assert card is not None and thread.slack.blocks_posts[-1][3] == "300.000003"


def test_a_room_that_turned_shared_gets_no_card(thread: Any) -> None:
    audience.note_channel_info(
        ChannelInfo(
            channel_id=PRIVATE, is_code_channel=False, is_private=True, shared=True, archived=False
        )
    )
    card, _ = asyncio.run(_relay(thread))
    assert card is None and thread.slack.blocks_posts == []


# ── Stop ──────────────────────────────────────────────────────────────────


@pytest.fixture
def stops(thread: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    stopped: list[str] = []

    async def stop_slot_turn(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        if not slot.running:
            return {"ok": True, "info": "not running"}
        stopped.append(slot.key)
        slot.running = False
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", stop_slot_turn)
    thread.state.subagents = None
    thread.store.record_worker(WORKER, CONDUCTOR)
    thread.conductor.running = thread.worker.running = True
    monkeypatch.setattr(routing, "_dashboard_state", lambda: thread.state)
    return stopped


_EVENTS = iter(range(10, 10_000))


def _bang_stop(user: str, text: str = "!stop") -> Any:
    # A fresh ts per message: the gate remembers each message's verdict.
    ts = f"200.{next(_EVENTS):06d}"
    event = {"channel": PRIVATE, "thread_ts": ANCHOR, "ts": ts, "user": user}
    return event, text, user


def _gate(e: Any, user: str, text: str = "!stop") -> Any:
    event, text, user = _bang_stop(user, text)
    return asyncio.run(
        routing.inbound_gate(
            e.orch, event, text=text, sender_id=user, authorized=True, from_trusted_bot=False
        )
    )


def test_stop_in_the_thread_halts_the_conductor_and_its_workers(thread: Any, stops: list[str]):
    result = _gate(thread, OWNER)
    assert result.gate is routing.Gate.DROP
    assert sorted(stops) == sorted([CONDUCTOR, WORKER])
    said = [p for p in thread.slack.posts if p[0] == PRIVATE]
    assert said and said[-1][2] == ANCHOR  # answered in the thread
    assert "this channel" not in said[-1][1]


@pytest.mark.parametrize("user", [SHARED, MEMBER])
def test_stop_is_owner_only(thread: Any, stops: list[str], user: str) -> None:
    result = _gate(thread, user)
    assert result.gate is routing.Gate.DROP
    assert stops == []
    assert thread.conductor.running and thread.worker.running


def test_other_messages_in_the_thread_pass_through_the_gate(thread: Any) -> None:
    assert _gate(thread, OWNER, "hello").gate is routing.Gate.PASS


def test_stop_still_stops_a_running_conductor_with_the_switch_off(thread: Any, stops: list[str]):
    thread.thread_on = False
    assert _gate(thread, OWNER).gate is routing.Gate.DROP
    assert CONDUCTOR in stops
    # Idle and off: the word is not ours.
    thread.conductor.running = False
    assert _gate(thread, OWNER).gate is routing.Gate.PASS


# ── Restart resume ────────────────────────────────────────────────────────


@pytest.fixture
def interrupted(thread: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A conductor whose turn a restart cut, resumed through the fake continue."""
    thread.continued = []
    turn = {"id": "t1"}

    async def interrupted_turn(slot: Any, *, pressed: bool = False) -> str:
        return turn["id"] if slot is thread.conductor else ""

    async def continue_(state: Any, slot: Any, still_ok=None, **kw: Any) -> str:
        thread.continued.append(slot.key)
        return ""

    monkeypatch.setattr(resume, "_interrupted_turn", interrupted_turn)
    monkeypatch.setattr(resume, "_continue", continue_)
    monkeypatch.setattr(
        resume, "_dispatch_token", lambda slot, lineage=False: f"d{len(thread.continued)}"
    )
    monkeypatch.setattr(resume, "_record_row", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(resume, "_own_workers", lambda key: [])
    monkeypatch.setattr(resume, "_worker_mirror", lambda s, w: "")
    thread.turn = turn
    return thread


def test_a_restart_resumes_the_thread_once_and_posts_one_notice_in_it(interrupted: Any) -> None:
    e = interrupted
    reports = asyncio.run(resume.resume_after_restart(e.state))
    assert len(reports) == 1 and reports[0].resumed == [CONDUCTOR]
    assert e.continued == [CONDUCTOR]
    channel, blocks, _text, thread_ts = e.slack.blocks_posts[-1]
    assert (channel, thread_ts) == (PRIVATE, ANCHOR)
    assert "Stop" in str(blocks) and "this channel" not in str(blocks)

    # Cut again on the same turn: held, never resumed twice, owner-only Resume.
    e.slack.blocks_posts.clear()
    reports = asyncio.run(resume.resume_after_restart(e.state))
    assert reports[0].held == [CONDUCTOR] and e.continued == [CONDUCTOR]
    channel, blocks, _text, thread_ts = e.slack.blocks_posts[-1]
    assert (channel, thread_ts) == (PRIVATE, ANCHOR)
    assert resume.RESUME_ACTION_PREFIX in str(blocks)
    assert "Only" in str(blocks)


def _button(e: Any, blocks: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    for block in blocks:
        for el in block.get("elements", []) + (
            [block["accessory"]] if "accessory" in block else []
        ):
            if str(el.get("action_id", "")).startswith(prefix):
                return el
    raise AssertionError(prefix)


def test_only_the_owner_can_press_the_threads_resume(interrupted: Any) -> None:
    e = interrupted
    asyncio.run(resume.resume_after_restart(e.state))
    asyncio.run(resume.resume_after_restart(e.state))  # held now
    channel, blocks, _t, _ts = e.slack.blocks_posts[-1]
    button = _button(e, blocks, resume.RESUME_ACTION_PREFIX)
    ts = next(iter(e.slack.blocks_posts))[0] and f"300.{e.slack._n:06d}"

    def press(user: str) -> dict[str, Any]:
        return {
            "type": "block_actions",
            "team": {"id": TEAM},
            "user": {"id": user},
            "channel": {"id": PRIVATE},
            "message": {"ts": ts},
            "actions": [button],
        }

    for user in (SHARED, MEMBER):
        assert asyncio.run(resume.handle_resume_click(e.orch, press(user))) is True
    assert e.continued == [CONDUCTOR]  # nothing new ran
    e.turn["id"] = "t2"  # the owner's press is a fresh decision on a cut turn
    assert asyncio.run(resume.handle_resume_click(e.orch, press(OWNER))) is True
    assert e.continued == [CONDUCTOR, CONDUCTOR]


def test_nothing_resumes_with_the_switch_off_or_the_thread_ended(interrupted: Any) -> None:
    e = interrupted
    e.thread_on = False
    assert asyncio.run(resume.resume_after_restart(e.state)) == []
    e.thread_on = True
    e.store.end_thread_binding(PRIVATE, ANCHOR)
    assert asyncio.run(resume.resume_after_restart(e.state)) == []
    assert e.continued == [] and e.slack.blocks_posts == []


# ── the config keys ───────────────────────────────────────────────────────


def test_the_config_keys_default_off_and_read_only_a_literal_true() -> None:
    assert build_slack_config({}).thread_conductor is False
    assert build_slack_config({}).thread_conductor_public_channels is False
    loose = build_slack_config({"thread_conductor": "true", "thread_conductor_public_channels": 1})
    assert loose.thread_conductor is False and loose.thread_conductor_public_channels is False
    strict = build_slack_config(
        {"thread_conductor": True, "thread_conductor_public_channels": True}
    )
    assert strict.thread_conductor is True and strict.thread_conductor_public_channels is True
