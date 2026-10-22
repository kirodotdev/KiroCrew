"""Slack Code channels: a worker's tool approval is asked in its channel too.

A worker the channel's own session created (``_created_by``) runs with at most
the channel's mode, so on Ask its prompt would show only in the dashboard. It is
relayed top-level into the channel, headed with the worker's title; Approve /
Reject resolve the worker's own future through the dashboard's slot-scoped
resolver; only the owner may answer; a prompt decided elsewhere updates the
card in place; and a session that is not this channel's worker is never relayed.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER

# The trust-card harness: a bound channel whose session is a real-shaped slot.
from test_slack_code_channel_agent_surface import CH, SLOT, env  # noqa: F401
from test_slack_code_channel_trust_from_slack import _slot, chan  # noqa: F401

from kiro_crew.dashboard.state import DENY_CAUSE_APPROVAL_NO_BUDGET, DENY_CAUSE_APPROVAL_TIMEOUT
from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import worker_approvals as wa

WORKER = "chat-5-1"
WORKER_SESSION = f"dashboard:{WORKER}"
REQUEST = "req-w1"


@pytest.fixture
def relay(chan: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    monkeypatch.setattr(wa, "live_config", chan.cfg)
    worker = _slot(key=WORKER, channel="")
    worker._slack_linked = False
    worker._slack_channel = None
    worker._slack_thread_ts = None
    worker._created_by = SLOT
    worker.title = "Fix #21964"
    chan.worker = worker
    chan.state._slots[WORKER] = worker
    chan.state.get_slot = lambda key: chan.state._slots.get(key)
    wa.reset()
    yield chan
    wa.reset()
    from kiro_crew.slack.code_channels import surface

    surface._archived_here.discard(CH)


async def _run_relay(e: Any, slot: Any | None = None, tool_input: str = "npm test") -> Any:
    """Raise one prompt on *slot* (the worker by default) and hand it to the relay."""
    slot = slot or e.worker
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures[REQUEST] = fut
    card = await wa.relay(
        e.state, slot, f"dashboard:{slot.key}", REQUEST, "Run shell", tool_input, fut
    )
    return card, fut


def _done(card: Any) -> asyncio.Future:
    """A finished relay task stand-in carrying *card*."""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(card)
    return fut


def _click(user: str, card: Any, action_id: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": card.channel},
        "message": {"ts": card.ts},
        "actions": [{"action_id": action_id, "value": card.request_id}],
    }


def _rows(e: Any) -> list[dict[str, Any]]:
    return e.sel.ops(wa.OPERATION)


# ── Posting ───────────────────────────────────────────────────────────────


def test_a_worker_approval_posts_top_level_in_the_channel(relay: Any) -> None:
    card, fut = asyncio.run(_run_relay(relay))
    assert card is not None and not fut.done()
    channel, blocks, text, thread_ts = relay.slack.blocks_posts[-1]
    assert channel == CH and thread_ts is None
    assert blocks[0]["text"]["text"] == "🔐 *Worker: Fix #21964* wants to run:"
    assert "npm test" in blocks[1]["text"]["text"]
    buttons = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert [b["action_id"] for b in buttons] == [
        wa.APPROVE_ACTION_ID,
        wa.TRUST_ACTION_ID,
        wa.REJECT_ACTION_ID,
    ]
    assert [b.get("style") for b in buttons].count("primary") == 1
    assert "Fix #21964" in text
    assert any("result=posted" in r["resources"] for r in _rows(relay))


def test_the_worker_title_cannot_ping_the_channel(relay: Any) -> None:
    relay.worker.title = "Fix it <!channel> *now*"
    asyncio.run(_run_relay(relay))
    head = relay.slack.blocks_posts[-1][1][0]["text"]["text"]
    assert "<!channel>" not in head and "&lt;!channel&gt;" in head


@pytest.mark.parametrize(
    "case", ["no_creator", "other_creator", "replaced_session", "linked_worker", "off", "archived"]
)
def test_a_session_that_is_not_this_channels_worker_is_never_relayed(relay: Any, case: str) -> None:
    worker = relay.worker
    if case == "no_creator":
        worker._created_by = ""
    elif case == "other_creator":
        other = _slot(key="chat-8-1", channel="")
        relay.state._slots[other.key] = other
        worker._created_by = other.key
    elif case == "replaced_session":
        # The channel's session was closed and replaced: the old slot's children
        # are not the channel's workers any more.
        relay.state.linked["100.000001"] = _slot(key="chat-7-1")
    elif case == "linked_worker":
        worker._slack_linked = True
    elif case == "off":
        relay.on = False
    elif case == "archived":
        from kiro_crew.slack.code_channels import surface

        surface.fence_archived(CH)
    card, fut = asyncio.run(_run_relay(relay))
    assert card is None and not fut.done()
    assert relay.slack.blocks_posts == []


# ── Answering from the channel ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action_id", "result", "line"),
    [
        (wa.APPROVE_ACTION_ID, "approved", "Approved by"),
        (wa.REJECT_ACTION_ID, "rejected", "Rejected by"),
    ],
)
def test_the_owner_answers_a_worker_approval(
    relay: Any, action_id: str, result: str, line: str
) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, action_id))
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card, fut

    card, fut = asyncio.run(go())
    assert fut.result() == result
    assert relay.resolved == [(WORKER, REQUEST, result == "approved")]
    updates = [u for u in relay.slack.updates if u[1] == card.ts]
    assert len(updates) == 1 and line in updates[0][2]  # finish did not overwrite it
    assert all(b.get("type") != "actions" for b in updates[0][3])
    assert _rows(relay)[-1]["outcome"] == "allowed"


def test_a_non_owner_is_refused_and_nothing_is_answered(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click("USTRANGER", card, wa.APPROVE_ACTION_ID))
        return card, fut

    card, fut = asyncio.run(go())
    assert not fut.done() and relay.resolved == []
    assert relay.slack.ephemerals[-1][1] == "USTRANGER"
    assert not relay.slack.updates


def test_a_stale_card_is_refused(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        fut.set_result("approved")  # answered in the dashboard meanwhile
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.REJECT_ACTION_ID))
        return card, fut

    _card, fut = asyncio.run(go())
    assert fut.result() == "approved" and relay.resolved == []
    assert relay.slack.ephemerals[-1][2] == wa.NOT_LIVE_TEXT


def test_trust_this_channel_on_a_worker_card(relay: Any) -> None:
    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return card, fut

    _card, fut = asyncio.run(go())
    assert fut.result() == "approved"
    assert relay.slot._trust is True  # the channel's session
    assert relay.worker._trust is True  # a running worker follows the channel
    assert [r for r in relay.mode_rows if r["operation"] == "mode_change:trust"]
    assert [p for p in relay.slack.posts if "*Trust*" in p[1]]


# ── Answered elsewhere ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "deny_cause", "host_ended", "key"),
    [
        ("approved", "", False, "approved"),
        ("rejected", "", False, "rejected"),
        ("rejected", DENY_CAUSE_APPROVAL_TIMEOUT, False, "timeout"),
        ("rejected", DENY_CAUSE_APPROVAL_NO_BUDGET, False, "ended"),
        ("rejected", "", True, "ended"),
    ],
)
def test_a_prompt_decided_elsewhere_updates_the_card_in_place(
    relay: Any, outcome: str, deny_cause: str, host_ended: bool, key: str
) -> None:
    async def go() -> Any:
        card, fut = await _run_relay(relay)
        fut.set_result(outcome)  # the dashboard, the window or the host decided it
        await wa.finish(_done(card), outcome, deny_cause=deny_cause, host_ended=host_ended)
        return card

    card = asyncio.run(go())
    channel, ts, text, blocks = relay.slack.updates[-1]
    assert (channel, ts) == (CH, card.ts)
    assert text == wa.ANSWERED_ELSEWHERE[key]
    assert all(b.get("type") != "actions" for b in blocks)
    assert f"{CH}:{card.ts}" not in wa._cards


# ── Starting the relay from the runner ────────────────────────────────────


@pytest.mark.parametrize("case", ["born_resolved", "already_linked", "no_creator", "linked_slot"])
def test_start_relays_nothing_it_must_not(relay: Any, case: str) -> None:
    meta: dict[str, Any] = {}
    linked_ts = None
    if case == "born_resolved":
        meta = {"resolved": "rejected"}
    elif case == "already_linked":
        linked_ts = "400.000001"
    elif case == "no_creator":
        relay.worker._created_by = ""
    elif case == "linked_slot":
        relay.worker._slack_linked = True

    async def go() -> Any:
        fut = asyncio.get_running_loop().create_future()
        return wa.start(
            relay.state,
            relay.worker,
            WORKER_SESSION,
            REQUEST,
            "Run shell",
            "ls",
            fut,
            perm_meta=meta,
            linked_ts=linked_ts,
        )

    assert asyncio.run(go()) is None
    assert relay.slack.blocks_posts == []


def test_start_posts_in_a_task_and_finish_retires_it(relay: Any) -> None:
    assert not inspect.iscoroutinefunction(wa.start)  # never an await before the runner's try

    async def go() -> Any:
        fut = asyncio.get_running_loop().create_future()
        relay.worker._approval_futures[REQUEST] = fut
        task = wa.start(
            relay.state,
            relay.worker,
            WORKER_SESSION,
            REQUEST,
            "Run shell",
            "ls",
            fut,
            perm_meta={},
            linked_ts=None,
        )
        assert isinstance(task, asyncio.Task)
        fut.set_result("approved")
        await wa.finish(task, "approved", deny_cause="", host_ended=False)
        return task

    task = asyncio.run(go())
    assert (
        relay.slack.blocks_posts and relay.slack.updates[-1][2] == wa.ANSWERED_ELSEWHERE["approved"]
    )
    assert task.done()


def test_finish_absorbs_a_cancelled_relay(relay: Any) -> None:
    async def go() -> None:
        async def slow() -> Any:
            await asyncio.sleep(10)

        task = asyncio.ensure_future(slow())
        await asyncio.sleep(0)
        task.cancel()
        await wa.finish(task, "rejected", deny_cause="", host_ended=True)

    asyncio.run(go())
    assert relay.slack.updates == []


@pytest.mark.parametrize(
    ("answer", "settled"),
    [
        ("approved", wa.ANSWERED_ELSEWHERE["approved"]),
        ("rejected", wa.ANSWERED_ELSEWHERE["rejected"]),
        ("unanswered", wa.ANSWERED_ELSEWHERE["timeout"]),
        ("stop", wa.ANSWERED_ELSEWHERE["ended"]),
        ("cancel_turn", wa.ANSWERED_ELSEWHERE["ended"]),
    ],
)
def test_a_real_worker_turn_relays_its_prompt_and_settles_the_card(
    relay: Any, answer: str, settled: str  # noqa: F811
) -> None:
    """A worker's dashboard turn posts its prompt in the channel, and the card says
    how the prompt ended once the dashboard (or the host) decided it."""
    from slack_code_channel_turns import prompt_in_worker

    asyncio.run(
        prompt_in_worker(
            relay,
            worker_key=WORKER,
            title="Fix #21964",
            conductor_key=SLOT,
            channel=CH,
            anchor="100.000001",
            answer=answer,
        )
    )
    channel, blocks, _text, thread_ts = relay.slack.blocks_posts[-1]
    assert (channel, thread_ts) == (CH, None)
    assert blocks[0]["text"]["text"] == "🔐 *Worker: Fix #21964* wants to run:"
    ts = f"300.{relay.slack._n:06d}"
    lines = [u[2] for u in relay.slack.updates if u[1] == ts]
    assert len(lines) == 1 and settled in lines[0]


def test_a_real_worker_turn_relays_no_prompt_born_rejected(relay: Any) -> None:  # noqa: F811
    """With no approval window the runner declines the prompt as it raises it; the
    relay is told so (through the row's meta) and posts nothing in the channel."""
    from unittest.mock import patch

    from slack_code_channel_turns import prompt_in_worker

    from kiro_crew.dashboard import chat_runner

    posts = len(relay.slack.blocks_posts)
    with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
        asyncio.run(
            prompt_in_worker(
                relay,
                worker_key=WORKER,
                title="Fix #21964",
                conductor_key=SLOT,
                channel=CH,
                anchor="100.000001",
                answer="rejected",
            )
        )
    assert len(relay.slack.blocks_posts) == posts


# ── A card spent while the owner's click was in flight ────────────────────


def test_trust_on_a_spent_worker_card_changes_no_mode(relay: Any, monkeypatch: Any) -> None:
    from kiro_crew import safety_override as safety_override_mod

    async def go() -> tuple[Any, asyncio.Future]:
        card, fut = await _run_relay(relay)

        def permitted(_mode: str) -> bool:
            # The dashboard answers the prompt while the policy check is in flight.
            fut.get_loop().call_soon_threadsafe(fut.set_result, "rejected")
            import time

            time.sleep(0.05)
            return True

        monkeypatch.setattr(safety_override_mod, "approval_mode_permitted", permitted)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return card, fut

    _card, fut = asyncio.run(go())
    assert fut.result() == "rejected"
    assert relay.slot._trust is False
    assert not [r for r in relay.mode_rows if r["operation"] == "mode_change:trust"]
    assert not [p for p in relay.slack.posts if "*Trust*" in p[1]]


def test_tool_arguments_cannot_break_out_of_the_code_fence(relay: Any) -> None:
    asyncio.run(_run_relay(relay, tool_input="echo ``` <!channel> ``` done"))
    texts = [b["text"]["text"] for b in relay.slack.blocks_posts[-1][1] if b.get("text")]
    assert all("<!channel>" not in t for t in texts)


def test_the_dispatcher_routes_worker_card_controls(relay: Any, monkeypatch: Any) -> None:
    seen: list[str] = []

    async def click(_orch: Any, p: dict[str, Any]) -> bool:
        seen.append(p["actions"][0]["action_id"])
        return True

    monkeypatch.setattr(wa, "handle_click", click)
    card = SimpleNamespace(channel=CH, ts="300.000009", request_id=REQUEST)
    for action_id in sorted(wa.ACTION_IDS):
        assert asyncio.run(
            wa.dispatch_card_click(relay.orch, _click(OWNER, card, action_id), action_id)
        )
    assert sorted(seen) == sorted(wa.ACTION_IDS)
    assert not asyncio.run(wa.dispatch_card_click(relay.orch, {}, "approve_tool"))
    assert approval_picker.TRUST_CALL_ACTION_ID not in wa.ACTION_IDS


def test_a_code_channel_card_footer_cannot_ping_the_channel() -> None:
    from kiro_crew.slack.handler import _build_approval_blocks, _LinkedApprovalEvent

    event = _LinkedApprovalEvent(REQUEST, "Run <!here> now", "ls")
    code = _build_approval_blocks(event, is_dm=False, channel_trust=True)  # type: ignore[arg-type]
    plain = _build_approval_blocks(event, is_dm=False)  # type: ignore[arg-type]
    assert "<!here>" not in code[-1]["elements"][0]["text"]
    assert "<!here>" in plain[-1]["elements"][0]["text"]  # every other card is unchanged


class _DeclaredOverride:
    """A declared YOLO grant: a slot-scoped Trust deactivates it first (an await)."""

    is_declared = True

    def __init__(self, on_deactivate: Any) -> None:
        self._on_deactivate = on_deactivate

    def deactivate(self, _who: str) -> None:
        self._on_deactivate()

    def is_active(self) -> bool:
        return False


def _spend_during_deactivate(monkeypatch: Any, fut: asyncio.Future) -> None:
    import time

    import kiro_crew.dashboard.chat_handlers as chat_handlers

    def spend() -> None:
        # The dashboard answers the prompt while apply_approval_mode awaits.
        fut.get_loop().call_soon_threadsafe(fut.set_result, "rejected")
        time.sleep(0.05)

    override = _DeclaredOverride(spend)
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)


def test_a_worker_card_spent_inside_the_mode_change_grants_nothing(
    relay: Any, monkeypatch: Any
) -> None:
    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        _spend_during_deactivate(monkeypatch, fut)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    fut = asyncio.run(go())
    assert fut.result() == "rejected"
    assert relay.slot._trust is False
    assert relay.state.sessions.policies.get(f"dashboard:{SLOT}", "") == ""
    assert relay.slack.ephemerals[-1][2] == approval_picker.NOT_LIVE_TEXT
    assert not [r for r in relay.mode_rows if r["operation"] == "mode_change:trust"]


def test_cancelling_the_turn_during_finish_is_not_swallowed(relay: Any) -> None:
    """A Stop / deadline while the relay is still posting must still cancel the turn."""
    after: list[str] = []

    async def turn(relay_task: asyncio.Task) -> None:
        await wa.finish(relay_task, "approved", deny_cause="", host_ended=False)
        after.append("ran the approved path")

    async def go() -> tuple[asyncio.Task, asyncio.Task]:
        started = asyncio.Event()

        async def slow_relay() -> Any:
            started.set()
            await asyncio.sleep(10)

        relay_task = asyncio.ensure_future(slow_relay())
        owner = asyncio.ensure_future(turn(relay_task))
        await started.wait()
        await asyncio.sleep(0)
        owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return owner, relay_task

    owner, relay_task = asyncio.run(go())
    assert owner.cancelled() and after == []
    assert relay_task.cancelled()  # stopped, so no card can be posted late
