"""Pre-push review fixes for a code channel's approval mode and worker relay.

Each test pins one finding from the fresh-context review of #21997:
a relayed worker card keeps the UNVERIFIED warning; a lowering reaches every
session that inherited the channel's Trust (a worker's workers, linked ones);
a worker a change could not reach is named in the reply; a cancelled change
leaves no card claimed; worker cards honour the channel's own egress gates;
an Archive or toggle-off during the owner check answers nothing; a literal
code fence cannot end the card's code block; and a hand-edited command name
is never echoed into the channel.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_agent_surface import CH, SLOT, env  # noqa: F401
from test_slack_code_channel_trust_from_slack import _slot, chan  # noqa: F401
from test_slack_code_channel_worker_approvals import (  # noqa: F401
    WORKER,
    _click,
    _run_relay,
    relay,
)

from kiro_crew.slack.code_channels import approval_picker, surface
from kiro_crew.slack.code_channels import worker_approvals as wa
from kiro_crew.slack.code_channels.store import store
from kiro_crew.slack.handler import post_linked_approval


def _set(mode: str, via: str = "select", orch: Any = None) -> Any:
    async def go() -> Any:
        binding = await asyncio.to_thread(store().get_binding, CH)
        extra = {"orch": orch} if orch is not None else {}
        return await approval_picker.set_channel_mode(binding, mode, OWNER, via=via, **extra)

    return go()


def _child(e: Any, key: str, creator: str, *, linked: bool = False, trust: bool = True) -> Any:
    s = _slot(key=key, channel="")
    s._slack_linked = linked
    s._created_by = creator
    s._trust = trust
    s.title = f"Session {key}"
    e.state._slots[key] = s
    return s


# ── B1. A relayed worker card keeps the UNVERIFIED warning ────────────────


def test_a_relayed_worker_card_keeps_the_unverified_warning(relay: Any) -> None:  # noqa: F811
    from slack_code_channel_turns import REQUEST, prompt_in_worker

    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    child_prompt = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=REQUEST,
        title="Running: git status",
        tool_kind="execute",
        sub_session_id="sub-1",  # a backend subagent's request, no security context
    )
    asyncio.run(
        prompt_in_worker(
            relay,
            worker_key=WORKER,
            title="Fix #21964",
            conductor_key=SLOT,
            channel=CH,
            anchor="100.000001",
            answer="rejected",
            prompt=child_prompt,
        )
    )
    channel, blocks, _text, thread_ts = relay.slack.blocks_posts[-1]
    assert (channel, thread_ts) == (CH, None)
    assert "UNVERIFIED child request" in json.dumps(blocks, ensure_ascii=False)


# ── B2 / A1. A lowering reaches every session that inherited the grant ────


def test_a_lowering_reaches_a_workers_worker_and_linked_workers(relay: Any) -> None:  # noqa: F811
    relay.slot._trust = True
    relay.worker._trust = True
    grandchild = _child(relay, "chat-6-1", WORKER)
    linked = _child(relay, "chat-6-2", SLOT, linked=True)
    deep = _child(relay, "chat-6-3", grandchild.key, linked=True)
    stranger = _child(relay, "chat-6-4", "chat-3-1")

    assert asyncio.run(_set("normal", via="command")).result == "set"
    assert relay.worker._trust is False
    assert grandchild._trust is False and linked._trust is False and deep._trust is False
    assert stranger._trust is True  # outside the channel's tree: untouched
    rows = [r for r in relay.sel.ops(approval_picker.OPERATION) if "depth=deeper" in r["resources"]]
    assert len(rows) == 3 and all("result=worker_set" in r["resources"] for r in rows)


def test_a_lowering_never_raises_a_deeper_session(relay: Any) -> None:  # noqa: F811
    relay.slot._trust = True
    grandchild = _child(relay, "chat-6-1", WORKER, trust=False)
    asyncio.run(_set("trust_reads"))
    assert grandchild._trust is False and grandchild._trust_reads is False


def test_a_worker_the_change_could_not_reach_is_named(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    relay.slot._trust = True
    relay.worker._trust = True
    real = approval_picker._apply

    async def flaky(state: Any, slot: Any, mode: str, user: str, still_live: Any = None) -> Any:
        if slot is relay.worker:
            return False, "the change was refused"
        return await real(state, slot, mode, user, still_live)

    monkeypatch.setattr(approval_picker, "_apply", flaky)
    out = asyncio.run(_set("normal", via="command"))
    assert out.result == "set" and relay.worker._trust is True
    assert "Couldn't change 1 running worker(s)" in out.text and "Fix #21964" in out.text


# ── A2. A cancelled change leaves no card claimed ─────────────────────────


def test_a_cancelled_channel_change_keeps_no_card(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    ts = asyncio.run(
        post_linked_approval(
            chan.slack,
            CH,
            None,
            "req-own",
            f"dashboard:{SLOT}",
            "Run shell",
            "ls",
            channel_trust=True,
        )
    )

    async def cancelled(*_a: Any, **_k: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(approval_picker, "_apply", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_set("trust"))
    assert approval_picker.keep_card(CH, ts) is False


def test_a_cancelled_worker_change_releases_its_claimed_card(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    relay.slot._trust = True  # only the worker is granted
    real = approval_picker._apply

    async def cancel_worker(state: Any, slot: Any, mode: str, user: str, live: Any = None) -> Any:
        if slot is relay.worker:
            raise asyncio.CancelledError
        return await real(state, slot, mode, user, live)

    monkeypatch.setattr(approval_picker, "_apply", cancel_worker)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        with pytest.raises(asyncio.CancelledError):
            await _set("trust")
        return card

    card = asyncio.run(go())
    assert card.answered is False  # the worker's own cleanup still says how it ended


# ── A3. Worker cards honour the channel's own egress gates ───────────────


@pytest.mark.parametrize("gate", ["mirror_paused", "activation_off", "stopped"])
def test_a_worker_card_is_not_posted_where_the_channel_is_silent(
    relay: Any, monkeypatch: pytest.MonkeyPatch, gate: str  # noqa: F811
) -> None:
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.slack.code_channels import routing

    if gate == "mirror_paused":
        monkeypatch.setattr(chat_utils, "slack_mirror_is_paused", lambda _s, _k: True)
    elif gate == "activation_off":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: False)
    else:
        monkeypatch.setattr(surface, "channel_stopped", lambda _c: True)
    before = len(relay.slack.blocks_posts)

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        return card

    assert asyncio.run(go()) is None
    assert len(relay.slack.blocks_posts) == before


# ── A4. Archive or toggle-off during the owner check answers nothing ──────


@pytest.mark.parametrize("change", ["archived", "feature_off"])
@pytest.mark.parametrize("action", [wa.APPROVE_ACTION_ID, wa.TRUST_ACTION_ID])
def test_a_worker_card_answers_nothing_after_an_archive_mid_check(
    relay: Any, monkeypatch: pytest.MonkeyPatch, change: str, action: str  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import binding as binding_mod

    real_authorize = approval_picker.authorize
    flags = {"off": False}
    real_on = binding_mod.code_channels_on

    def on(cfg: Any) -> bool:
        return not flags["off"] and real_on(cfg)

    monkeypatch.setattr(approval_picker, "code_channels_on", on)

    async def authorize_then_change(*a: Any, **k: Any) -> Any:
        out = await real_authorize(*a, **k)
        if change == "archived":
            surface.fence_archived(CH)
        else:
            flags["off"] = True
        return out

    monkeypatch.setattr(approval_picker, "authorize", authorize_then_change)

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, action))
        return fut

    fut = asyncio.run(go())
    assert not fut.done()
    assert relay.slot._trust is False and relay.worker._trust is False
    assert relay.slack.ephemerals[-1][2] == wa.NOT_LIVE_TEXT


def test_a_channel_card_answers_nothing_after_an_archive_mid_check(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from test_slack_code_channel_trust_from_slack import _click as _trust_click
    from test_slack_code_channel_trust_from_slack import _post_card

    real_authorize = approval_picker.authorize

    async def authorize_then_archive(*a: Any, **k: Any) -> Any:
        out = await real_authorize(*a, **k)
        surface.fence_archived(CH)
        return out

    monkeypatch.setattr(approval_picker, "authorize", authorize_then_archive)
    ts = _post_card(chan)

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures["req-7"] = fut
        assert await approval_picker.handle_trust_call(chan.orch, _trust_click(OWNER, ts))
        return fut

    try:
        fut = asyncio.run(go())
    finally:
        surface._archived_here.discard(CH)
    assert not fut.done() and chan.slot._trust is False


def test_a_replaced_channel_session_gets_no_trust(chan: Any) -> None:  # noqa: F811
    """The session the caller authorized against was replaced: nothing changes."""

    async def go() -> Any:
        binding = await asyncio.to_thread(store().get_binding, CH)
        return await approval_picker.set_channel_mode(
            binding, "trust", OWNER, via="approval_card", expected_slot=object()
        )

    assert asyncio.run(go()).result == "unavailable"
    assert chan.slot._trust is False


# ── A5. A literal code fence cannot end the card's code block ─────────────


def test_a_literal_fence_in_the_command_stays_inside_the_code_block(
    chan: Any,  # noqa: F811
) -> None:
    from kiro_crew.slack.format import CODE_FENCE_STAND_IN

    asyncio.run(
        post_linked_approval(
            chan.slack,
            CH,
            None,
            "req-f",
            f"dashboard:{SLOT}",
            "Run shell",
            json.dumps({"command": "echo ```\n*✅ Approved by the owner*", "cwd": "a```b"}),
            channel_trust=True,
        )
    )
    code = [
        b["text"]["text"]
        for b in chan.slack.blocks_posts[-1][1]
        if b.get("type") == "section" and b["text"]["text"].startswith("```")
    ]
    assert len(code) == 2
    for text in code:
        inner = text[3:-3]
        assert "```" not in inner and CODE_FENCE_STAND_IN in inner


# ── A6. A hand-edited command name is never echoed ────────────────────────


@pytest.mark.parametrize("bad", ["<!channel>", "kiro crew", "a" * 40, "Kiro"])
def test_an_invalid_command_name_is_never_echoed(bad: str) -> None:
    assert approval_picker._command(SimpleNamespace(slack_command=bad)) == "kirocrew"


def test_the_trust_warning_names_the_routed_command(chan: Any) -> None:  # noqa: F811
    asyncio.run(_set("trust", orch=SimpleNamespace(slack_command="/crew")))
    posted = [p[1] for p in chan.slack.posts if p[0] == CH]
    assert any("`/crew trust ask`" in t for t in posted)


# ── Re-check fixes ────────────────────────────────────────────────────────


def test_the_kickoff_select_names_a_worker_it_could_not_lower(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from test_slack_code_channel_trust_from_slack import ANCHOR

    relay.slot._trust = True
    relay.worker._trust = True
    real = approval_picker._apply

    async def flaky(state: Any, slot: Any, mode: str, user: str, still_live: Any = None) -> Any:
        if slot is relay.worker:
            return False, approval_picker.STALE_REASON  # refused by its fence
        return await real(state, slot, mode, user, still_live)

    monkeypatch.setattr(approval_picker, "_apply", flaky)
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": OWNER},
        "channel": {"id": CH},
        "message": {"ts": ANCHOR},
        "actions": [
            {
                "action_id": approval_picker.APPROVAL_SELECT_ACTION_ID,
                "selected_option": {"value": "normal"},
            }
        ],
    }
    assert asyncio.run(approval_picker.handle_select(relay.orch, payload))
    assert relay.slot._trust is False and relay.worker._trust is True
    assert "Couldn't change 1 running worker(s)" in relay.slack.ephemerals[-1][2]


def test_a_recorded_archive_stops_a_raise(chan: Any) -> None:  # noqa: F811
    """The stored stamp counts once the in-process fence is lifted."""
    import dataclasses

    binding = store().get_binding(CH)
    assert binding is not None
    archived = dataclasses.replace(binding, archived_at=1.0)
    assert approval_picker._channel_open(binding) is True
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store(), "get_binding", lambda _c: archived)
        assert approval_picker._channel_open(binding) is False


@pytest.mark.parametrize("change", ["archived", "feature_off"])
def test_a_lowering_is_never_blocked_by_a_closing_channel(
    relay: Any, monkeypatch: pytest.MonkeyPatch, change: str  # noqa: F811
) -> None:
    relay.slot._trust = True
    relay.worker._trust = True
    monkeypatch.setattr(approval_picker, "_channel_open", lambda *_a, **_k: False)
    out = asyncio.run(_set("normal", via="command"))
    assert out.result == "set"
    assert relay.slot._trust is False and relay.worker._trust is False


def test_a_raise_is_blocked_by_a_closing_channel(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(approval_picker, "_channel_open", lambda *_a, **_k: False)
    assert asyncio.run(_set("trust", via="command")).result == "refused"
    assert relay.slot._trust is False and relay.worker._trust is False


@pytest.mark.parametrize("gate", ["mirror_paused", "stopped"])
def test_a_posted_worker_card_stays_answerable_through_a_pause(
    relay: Any, monkeypatch: pytest.MonkeyPatch, gate: str  # noqa: F811
) -> None:
    from kiro_crew.dashboard import chat_utils

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        if gate == "mirror_paused":
            monkeypatch.setattr(chat_utils, "slack_mirror_is_paused", lambda _s, _k: True)
        else:
            monkeypatch.setattr(surface, "channel_stopped", lambda _c: True)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.APPROVE_ACTION_ID))
        return fut

    assert asyncio.run(go()).result() == "approved"


def test_a_worker_trust_click_answers_nothing_after_an_archive_mid_change(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The channel already holds Trust (no raise); the archive lands during the
    worker's own mode change, so the click must not answer the call by hand."""
    relay.slot._trust = True
    relay.worker._trust = True  # nothing to change: the sweep never runs

    real = approval_picker.set_channel_mode

    async def then_archive(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        surface.fence_archived(CH)
        return out

    monkeypatch.setattr(approval_picker, "set_channel_mode", then_archive)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    fut = asyncio.run(go())
    assert not fut.done()
    assert relay.slack.ephemerals[-1][2] == wa.NOT_LIVE_TEXT


def test_a_trust_rewrite_under_yolo_is_still_a_raise(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Already on Trust, YOLO skips the 'unchanged' shortcut and the write re-runs
    the sweep: on a closing channel that must approve nothing."""
    relay.slot._trust = True
    relay.worker._trust = True
    monkeypatch.setattr(approval_picker, "_yolo_active", lambda: True)
    monkeypatch.setattr(approval_picker, "_channel_open", lambda *_a, **_k: False)

    async def go() -> tuple[Any, asyncio.Future]:
        own = asyncio.get_running_loop().create_future()
        relay.slot._approval_futures["req-own"] = own
        out = await _set("trust", via="command")
        return out, own

    out, own = asyncio.run(go())
    assert out.result == "refused" and not own.done()


@pytest.mark.parametrize(
    "flags, mode, expected",
    [
        ({}, "trust", True),
        ({"_trust": True}, "trust", True),
        ({"_trust_scope": "lease"}, "trust", True),
        ({}, "trust_reads", True),
        ({"_trust_scope": "lease"}, "trust_reads", True),
        ({"_trust": True}, "trust_reads", False),
        ({"_trust_reads": True}, "trust_reads", False),
        ({"_trust": True}, "normal", False),
    ],
)
def test_what_counts_as_a_raise(flags: dict[str, Any], mode: str, expected: bool) -> None:
    slot = SimpleNamespace(_trust=False, _trust_reads=False, _trust_scope="", **{})
    for k, v in flags.items():
        setattr(slot, k, v)
    assert approval_picker._raises(mode, slot) is expected
