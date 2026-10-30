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
        return await approval_picker.set_channel_mode(
            binding, mode, OWNER, via=via, team="T1", **extra
        )

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
            binding, "trust", OWNER, via="approval_card", expected_slot=object(), team="T1"
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


# ── Green round: a rewritten card says how it really ended, with no buttons ──


def _own_card(e: Any) -> tuple[str, None]:
    """A card the channel's own session posted, for its prompt ``req-own``."""
    from test_slack_code_channel_mode_reaches_workers import _post_channel_card

    return _post_channel_card(e), None


def _updates_for(e: Any, ts: str) -> list[Any]:
    return [u for u in e.slack.updates if u[1] == ts]


def _has_buttons(blocks: Any) -> bool:
    return any(b.get("type") == "actions" for b in (blocks or []))


def test_a_card_trust_answered_is_rewritten_without_buttons(relay: Any) -> None:  # noqa: F811
    """chat.update keeps a message's blocks unless new ones are sent."""
    own_ts, _ = _own_card(relay)

    async def go() -> None:
        relay.slot._approval_futures["req-own"] = asyncio.get_running_loop().create_future()
        assert (await _set("trust", via="command")).result == "set"

    asyncio.run(go())
    (u,) = _updates_for(relay, own_ts)
    assert "Ran under Trust" in u[2]
    assert u[3] and not _has_buttons(u[3]) and "Ran under Trust" in json.dumps(u[3])


def test_the_clicked_trust_card_is_rewritten_without_buttons(chan: Any) -> None:  # noqa: F811
    from test_slack_code_channel_trust_from_slack import _click as _trust_click
    from test_slack_code_channel_trust_from_slack import _post_card

    ts = _post_card(chan)

    async def go() -> None:
        chan.slot._approval_futures["req-7"] = asyncio.get_running_loop().create_future()
        assert await approval_picker.handle_trust_call(chan.orch, _trust_click(OWNER, ts))

    asyncio.run(go())
    (u,) = _updates_for(chan, ts)
    assert "now on Trust" in u[2] and u[3] and not _has_buttons(u[3])


def test_a_card_declined_during_the_grant_never_says_trust_ran(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from test_slack_code_channel_mode_reaches_workers import _during_grant

    own_ts, _ = _own_card(relay)
    holder: dict[str, Any] = {}
    # 1 = the channel's grant: the dashboard declines the card during its await.
    _during_grant(relay, monkeypatch, 1, lambda: holder["fut"].set_result("rejected"))

    async def go() -> None:
        holder["fut"] = asyncio.get_running_loop().create_future()
        relay.slot._approval_futures["req-own"] = holder["fut"]
        assert (await _set("trust", via="select")).result == "set"

    asyncio.run(go())
    assert not any("Ran under Trust" in u[2] for u in _updates_for(relay, own_ts))
    # Not kept: the runner deletes the declined card, as it does any decided one.
    assert approval_picker.keep_card(CH, own_ts) is False


def test_a_card_answered_during_a_failed_grant_is_left_to_the_runner(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The prompt ends during the grant's await and the grant is then refused:
    nothing may have kept the card from the runner's delete."""
    from test_slack_code_channel_mode_reaches_workers import _during_grant
    from test_slack_code_channel_trust_from_slack import _click as _trust_click
    from test_slack_code_channel_trust_from_slack import _post_card

    ts = _post_card(chan)
    holder: dict[str, Any] = {}
    seen: dict[str, bool] = {}

    def decline_and_clean_up() -> None:
        holder["fut"].set_result("rejected")
        # What the runner's finally does once the prompt is decided.
        seen["kept"] = approval_picker.keep_card(CH, ts)

    _during_grant(chan, monkeypatch, 1, decline_and_clean_up)

    async def go() -> None:
        holder["fut"] = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures["req-7"] = holder["fut"]
        assert await approval_picker.handle_trust_call(chan.orch, _trust_click(OWNER, ts))

    asyncio.run(go())
    assert seen["kept"] is False  # so the runner deleted it
    assert not _updates_for(chan, ts)


def test_a_claimed_worker_card_its_own_click_answered_keeps_the_clicks_line(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The owner's Approve lands on a card a Trust sweep has claimed: the
    sweep's settle must not overwrite the click's own line."""

    async def go() -> Any:
        card, _fut = await _run_relay(relay)
        claimed = wa.claim_pending(WORKER)  # what a Trust sweep does first
        assert claimed == [card]
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.APPROVE_ACTION_ID))
        await wa.settle_claimed(claimed, swept=set(), line="unused")
        return card

    card = asyncio.run(go())
    lines = [u[2] for u in _updates_for(relay, card.ts)]
    assert lines == [wa.APPROVED_BY.format(user=OWNER)]


def test_a_clicked_card_declined_during_the_change_never_says_approved(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from test_slack_code_channel_mode_reaches_workers import _during_grant
    from test_slack_code_channel_trust_from_slack import _click as _trust_click
    from test_slack_code_channel_trust_from_slack import _post_card

    ts = _post_card(chan)
    holder: dict[str, Any] = {}

    def decline() -> None:
        holder["fut"].set_result("rejected")

    _during_grant(chan, monkeypatch, 1, decline)

    async def go() -> None:
        holder["fut"] = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures["req-7"] = holder["fut"]
        assert await approval_picker.handle_trust_call(chan.orch, _trust_click(OWNER, ts))

    asyncio.run(go())
    texts = [u[2] for u in _updates_for(chan, ts)]
    assert not any("Approved" in t for t in texts)
    assert chan.slot._trust is False  # the fence refused: the prompt had gone
    assert chan.slack.ephemerals[-1][2] == approval_picker.NOT_LIVE_TEXT


def test_a_worker_card_approved_elsewhere_during_the_grant_never_says_trust_ran(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from test_slack_code_channel_mode_reaches_workers import _during_grant
    from test_slack_code_channel_worker_approvals import _done

    holder: dict[str, Any] = {}
    # 1 = the channel's grant, 2 = the worker's: approved in the dashboard first.
    _during_grant(relay, monkeypatch, 2, lambda: holder["fut"].set_result("approved"))

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        holder["fut"] = fut
        assert (await _set("trust", via="command")).result == "set"
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card

    card = asyncio.run(go())
    lines = [u[2] for u in _updates_for(relay, card.ts)]
    assert lines == [wa.CLAIMED_ENDED["approved"]]


def test_a_worker_trust_click_answered_elsewhere_mid_change_is_not_claimed(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The channel's grant commits; the worker's prompt is then approved in the
    dashboard while the worker's own grant awaits: the click must not say it
    approved it."""
    from test_slack_code_channel_mode_reaches_workers import _during_grant

    holder: dict[str, Any] = {}
    # 1 = the channel's grant, 2 = the worker's
    _during_grant(relay, monkeypatch, 2, lambda: holder["fut"].set_result("approved"))

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        holder["fut"] = fut
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return card

    card = asyncio.run(go())
    assert relay.slot._trust is True  # the channel's Trust took
    assert [u[2] for u in _updates_for(relay, card.ts)] == [wa.CLAIMED_ENDED["approved"]]
    assert relay.slack.ephemerals[-1][2] == wa.NOT_LIVE_TEXT


# ── Restack review: #21991's agent-switch hardening reaches the mode change ──


@pytest.mark.parametrize("gate", ["owner_removed", "origin_closed", "slack_denied"])
def test_a_raise_grants_nothing_once_a_gate_closes_after_authorize(
    relay: Any, monkeypatch: pytest.MonkeyPatch, gate: str  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import routing

    if gate == "owner_removed":
        from kiro_crew.slack import handler

        monkeypatch.setattr(handler, "is_allowed_user", lambda _u: False)
    elif gate == "origin_closed":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: False)
    else:
        from kiro_crew.messaging import identity

        async def denied(_ch: str) -> bool:
            return False

        monkeypatch.setattr(identity, "channel_inbound_permitted", denied)

    async def go() -> tuple[Any, asyncio.Future]:
        own = asyncio.get_running_loop().create_future()
        relay.slot._approval_futures["req-own"] = own
        out = await _set("trust", via="command")
        return out, own

    out, own = asyncio.run(go())
    assert out.result == "refused" and out.text == approval_picker.NOT_ALLOWED_TEXT
    assert relay.slot._trust is False and relay.worker._trust is False and not own.done()


def test_a_gate_closing_at_the_grant_stops_a_worker_raise(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The owner is removed during the worker's grant await: the worker is not raised."""
    from test_slack_code_channel_mode_reaches_workers import _during_grant

    from kiro_crew.slack import handler

    relay.slot._trust = True  # only the worker is granted

    def remove_owner() -> None:
        monkeypatch.setattr(handler, "is_allowed_user", lambda _u: False)

    _during_grant(relay, monkeypatch, 1, remove_owner)
    asyncio.run(_set("trust", via="command"))
    assert relay.worker._trust is False


def test_a_lowering_still_lands_after_a_gate_closes(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from kiro_crew.slack import handler

    relay.slot._trust = True
    relay.worker._trust = True
    monkeypatch.setattr(handler, "is_allowed_user", lambda _u: False)
    assert asyncio.run(_set("normal", via="command")).result == "set"
    assert relay.slot._trust is False and relay.worker._trust is False


def test_the_window_loads_off_the_loop_before_the_grant(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import agent_picker

    loaded: list[Any] = []

    async def load(slot: Any) -> bool:
        loaded.append(slot)
        return True

    monkeypatch.setattr(agent_picker, "_load_window", load)
    asyncio.run(_set("trust", via="command"))
    assert relay.slot in loaded and relay.worker in loaded


def test_a_window_that_cannot_load_changes_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import agent_picker

    async def fail(_slot: Any) -> bool:
        return False

    monkeypatch.setattr(agent_picker, "_load_window", fail)
    out = asyncio.run(_set("trust", via="command"))
    assert out.result == "unavailable" and out.text == approval_picker.NO_SESSION_TEXT
    assert relay.slot._trust is False and relay.worker._trust is False


def test_a_worker_approve_grants_nothing_once_the_owner_is_removed(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from kiro_crew.slack import handler

    real = approval_picker.authorize

    async def authorize_then_remove(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        monkeypatch.setattr(handler, "is_allowed_user", lambda _u: False)
        return out

    monkeypatch.setattr(approval_picker, "authorize", authorize_then_remove)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.APPROVE_ACTION_ID))
        return fut

    assert not asyncio.run(go()).done()
