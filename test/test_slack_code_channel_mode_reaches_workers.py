"""A code channel's approval mode reaches the workers its session already started.

Kyle's build-5 test: the channel was put on Trust, yet the conductor's running
worker still posted an approval card, because the mode only reached workers
started afterwards. Now a channel mode change (the card's Trust button, the
kickoff select, ``/kirocrew trust``) applies the same mode to the channel
session's own live workers through the same audited path, raising and lowering;
a raise to Trust answers their pending prompts, and the channel's own, the way
a mid-wait dashboard switch does, and each Slack card says so in place. Nothing
outside that lineage changes, and a person-only question card is never answered.

The approval card in a code channel also shows the command once.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_agent_surface import CH, SLOT, env  # noqa: F401
from test_slack_code_channel_trust_from_slack import ANCHOR, _slot, chan  # noqa: F401
from test_slack_code_channel_worker_approvals import (  # noqa: F401
    REQUEST,
    WORKER,
    _click,
    _done,
    _run_relay,
    relay,
)

from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import worker_approvals as wa
from kiro_crew.slack.code_channels.store import store
from kiro_crew.slack.handler import post_linked_approval


def _set(mode: str, via: str = "select") -> Any:
    async def go() -> Any:
        binding = await asyncio.to_thread(store().get_binding, CH)
        return await approval_picker.set_channel_mode(binding, mode, OWNER, via=via)

    return go()


def _worker_rows(e: Any) -> list[dict[str, Any]]:
    return [r for r in e.sel.ops(approval_picker.OPERATION) if f"worker={WORKER}" in r["resources"]]


# ── 1. The mode reaches the channel's live workers ────────────────────────


def test_an_existing_worker_inherits_a_raise_and_a_lower(relay: Any) -> None:  # noqa: F811
    async def go() -> None:
        assert (await _set("trust")).result == "set"
        assert relay.worker._trust is True
        assert (await _set("normal")).result == "set"

    asyncio.run(go())
    assert relay.slot._trust is False and relay.worker._trust is False
    assert relay.worker._trust_reads is False
    # Each worker change is the dashboard switch's own audited mutation ...
    assert [r["operation"] for r in relay.mode_rows].count("mode_change:trust") == 2
    # ... plus this feature's row naming the worker, once per change.
    rows = _worker_rows(relay)
    assert len(rows) == 2 and all("result=worker_set" in r["resources"] for r in rows)
    assert "mode=trust" in rows[0]["resources"] and "mode=normal" in rows[1]["resources"]


def test_a_worker_is_lowered_to_a_channel_already_on_ask(relay: Any) -> None:  # noqa: F811
    """The channel holds Ask already; a worker someone raised is lowered to it."""
    relay.worker._trust = True
    assert asyncio.run(_set("normal")).result == "unchanged"
    assert relay.worker._trust is False


def test_trust_reads_reaches_the_worker_too(relay: Any) -> None:  # noqa: F811
    asyncio.run(_set("trust_reads"))
    assert relay.worker._trust_reads is True and relay.worker._trust is False


@pytest.mark.parametrize("case", ["other_creator", "no_creator", "linked", "grandchild"])
def test_an_unrelated_session_is_untouched(relay: Any, case: str) -> None:  # noqa: F811
    other = _slot(key="chat-8-1", channel="")
    other._slack_linked = case == "linked"
    other._created_by = {
        "other_creator": "chat-3-1",
        "no_creator": "",
        "linked": SLOT,
        "grandchild": WORKER,
    }[case]
    relay.state._slots[other.key] = other

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        other._approval_futures["req-x"] = fut
        await _set("trust")
        return fut

    fut = asyncio.run(go())
    assert other._trust is False and not fut.done()
    assert not [r for r in relay.sel.ops(approval_picker.OPERATION) if "chat-8-1" in r["resources"]]


# ── 2. A raise to Trust answers pending cards, which say so in place ──────


def _post_channel_card(e: Any, tool_input: str = "ls") -> str:
    ts = asyncio.run(
        post_linked_approval(
            e.slack,
            CH,
            None,
            "req-own",
            f"dashboard:{SLOT}",
            "Run shell",
            tool_input,
            channel_trust=True,
        )
    )
    assert ts
    return ts


def test_pending_cards_resolve_on_a_raise(relay: Any) -> None:  # noqa: F811
    own_ts = _post_channel_card(relay)

    async def go() -> tuple[asyncio.Future, asyncio.Future, Any]:
        own = asyncio.get_running_loop().create_future()
        relay.slot._approval_futures["req-own"] = own
        card, worker_fut = await _run_relay(relay)
        assert (await _set("trust", via="command")).result == "set"
        # The worker's own cleanup runs after its prompt is answered.
        await wa.finish(_done(card), worker_fut.result(), deny_cause="", host_ended=False)
        return own, worker_fut, card

    own, worker_fut, card = asyncio.run(go())
    assert own.result() == "approved" and worker_fut.result() == "approved"
    by_ts = {u[1]: u for u in relay.slack.updates}
    assert "Ran under Trust" in by_ts[own_ts][2]
    assert "Ran under Trust" in by_ts[card.ts][2]  # not "approved in the dashboard"
    assert all(b.get("type") != "actions" for b in by_ts[card.ts][3] or [])
    # The runner's cleanup keeps the re-written channel card rather than deleting it.
    assert approval_picker.keep_card(CH, own_ts) is True
    assert approval_picker.keep_card(CH, own_ts) is False  # consumed once


def test_a_lower_answers_no_pending_card(relay: Any) -> None:  # noqa: F811
    relay.slot._trust_reads = True

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await _set("normal")
        return fut

    assert not asyncio.run(go()).done()


def test_a_person_only_question_card_is_never_auto_answered(relay: Any) -> None:  # noqa: F811
    """``ask_question`` cards wait in ``_question_futures``, which no Trust sweeps."""

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        relay.state._question_futures = {"ask-1": fut}
        relay.state._pending_questions = {"ask-1": {"slot": WORKER}}
        await _set("trust")
        return fut

    fut = asyncio.run(go())
    assert not fut.done() and relay.worker._trust is True


# ── 3. Trust this channel on a worker's card ──────────────────────────────


def test_trust_on_a_worker_card_raises_the_whole_channel(relay: Any) -> None:  # noqa: F811
    sibling = _slot(key="chat-5-2", channel="")
    sibling._slack_linked = False
    sibling._created_by = SLOT
    relay.state._slots[sibling.key] = sibling

    async def go() -> tuple[Any, asyncio.Future, asyncio.Future]:
        card, fut = await _run_relay(relay)
        other = asyncio.get_running_loop().create_future()
        sibling._approval_futures["req-s"] = other
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card, fut, other

    card, fut, other = asyncio.run(go())
    assert fut.result() == "approved" and other.result() == "approved"
    assert relay.slot._trust and relay.worker._trust and sibling._trust
    last = [u for u in relay.slack.updates if u[1] == card.ts][-1]
    assert "Trust" in last[2] and f"<@{OWNER}>" in last[2]
    assert any("result=approved" in r["resources"] for r in wa_rows(relay))


def wa_rows(e: Any) -> list[dict[str, Any]]:
    return e.sel.ops(wa.OPERATION)


# ── 4. The card shows the command once ────────────────────────────────────


def _card_blocks(e: Any, tool_input: str, channel_trust: bool = True) -> list[dict[str, Any]]:
    asyncio.run(
        post_linked_approval(
            e.slack,
            CH,
            None if channel_trust else "1.0",
            REQUEST,
            f"dashboard:{SLOT}",
            "Running: git status",
            tool_input,
            channel_trust=channel_trust,
        )
    )
    return e.slack.blocks_posts[-1][1]


def _texts(blocks: list[dict[str, Any]]) -> list[str]:
    out = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    out += [e["text"] for b in blocks if b.get("type") == "context" for e in b["elements"]]
    return out


def test_a_code_channel_card_shows_the_command_once(chan: Any) -> None:  # noqa: F811
    texts = _texts(_card_blocks(chan, json.dumps({"command": "git status"})))
    assert sum("git status" in t for t in texts) == 1
    assert "```git status```" in texts
    assert not any('"command"' in t for t in texts)


def test_other_arguments_still_show_as_json(chan: Any) -> None:  # noqa: F811
    texts = _texts(
        _card_blocks(chan, json.dumps({"command": "git status", "cwd": "/repo", "timeout": 30}))
    )
    assert sum("git status" in t for t in texts) == 1
    extra = [t for t in texts if '"cwd"' in t]
    assert len(extra) == 1 and '"command"' not in extra[0] and "30" in extra[0]


def test_an_ordinary_card_is_unchanged(chan: Any) -> None:  # noqa: F811
    texts = _texts(_card_blocks(chan, json.dumps({"command": "git status"}), channel_trust=False))
    assert any('"command"' in t for t in texts)  # the raw input, as before
    assert any(t.startswith(":lock:") and "Running: git status" in t for t in texts)


# ── Review fixes: commit-time fences on a worker's grant ──────────────────


def _during_grant(e: Any, monkeypatch: pytest.MonkeyPatch, on_call: int, act: Any) -> None:
    """Run *act* inside the *on_call*-th grant's policy await (1-based)."""
    from kiro_crew import safety_override as safety_override_mod

    calls = {"n": 0}

    def permitted(_mode: str) -> bool:
        if _mode == "yolo":  # the override's own policy read, not a grant
            return e.mode_permitted
        calls["n"] += 1
        if calls["n"] == on_call:
            act()
        return e.mode_permitted

    monkeypatch.setattr(safety_override_mod, "approval_mode_permitted", permitted)


def test_a_replacement_under_the_workers_key_is_never_raised(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    relay.slot._trust = True  # the channel holds Trust: only the worker is granted
    stranger = _slot(key=WORKER, channel="")
    stranger._slack_linked = False
    stranger._created_by = "chat-3-1"
    _during_grant(relay, monkeypatch, 1, lambda: relay.state._slots.__setitem__(WORKER, stranger))

    async def go() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        stranger._approval_futures["req-x"] = fut
        await _set("trust")
        return fut

    fut = asyncio.run(go())
    assert stranger._trust is False and not fut.done()
    assert relay.worker._trust is False
    assert any("worker_refused" in r["resources"] for r in _worker_rows(relay))


def test_a_lowering_during_the_worker_grant_wins(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    def lower() -> None:
        relay.slot._trust = False  # the dashboard put the channel back on Normal

    _during_grant(relay, monkeypatch, 2, lower)  # 1 = the channel's, 2 = the worker's
    asyncio.run(_set("trust"))
    assert relay.worker._trust is False


def test_a_worker_card_spent_during_an_unchanged_trust_grants_nothing(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    relay.slot._trust = True
    holder: dict[str, Any] = {}
    _during_grant(relay, monkeypatch, 1, lambda: holder["fut"].set_result("rejected"))

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        holder["fut"] = fut
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        return fut

    fut = asyncio.run(go())
    assert fut.result() == "rejected"
    assert relay.worker._trust is False
    assert relay.slack.ephemerals[-1][2] == approval_picker.NOT_LIVE_TEXT


def test_a_decoded_command_is_redacted_again(chan: Any) -> None:  # noqa: F811
    key = "AKIA" + "QWERTYUIOPASDFGH"
    escaped = "".join(f"\\u{ord(c):04x}" for c in key)
    raw = '{"command": "echo ' + escaped + '", "env": "' + escaped + '"}'
    texts = _texts(_card_blocks(chan, raw))
    assert not any(key in t for t in texts)
    assert sum("[REDACTED" in t for t in texts) == 2


# ── Review fixes: card text and end states ────────────────────────────────


def test_an_unverified_title_stays_on_the_card(chan: Any) -> None:  # noqa: F811
    asyncio.run(
        post_linked_approval(
            chan.slack,
            CH,
            None,
            REQUEST,
            f"dashboard:{SLOT}",
            "⚠️ UNVERIFIED child request (security context missing — title is "
            "agent-authored): Running: git status",
            json.dumps({"command": "git status"}),
            channel_trust=True,
        )
    )
    texts = _texts(chan.slack.blocks_posts[-1][1])
    assert any(t.startswith(":lock:") and "UNVERIFIED" in t for t in texts)


def test_a_title_that_says_more_than_the_command_stays(chan: Any) -> None:  # noqa: F811
    asyncio.run(
        post_linked_approval(
            chan.slack,
            CH,
            None,
            REQUEST,
            f"dashboard:{SLOT}",
            "Delete the release branch",
            json.dumps({"command": "git push origin :release"}),
            channel_trust=True,
        )
    )
    texts = _texts(chan.slack.blocks_posts[-1][1])
    assert any("Delete the release branch" in t for t in texts)


def test_a_command_with_hidden_characters_is_shown_raw(chan: Any) -> None:  # noqa: F811
    raw = json.dumps({"command": "echo safe\u202e;rm -rf ~"})  # escaped by json.dumps
    texts = _texts(_card_blocks(chan, raw))
    assert not any("\u202e" in t for t in texts)
    assert any("\\u202e" in t for t in texts)


@pytest.mark.parametrize("rewritten", [True, False], ids=["trust-rewrote-it", "plain"])
def test_a_real_turn_keeps_a_card_the_trust_rewrote(
    chan: Any, rewritten: bool  # noqa: F811
) -> None:
    """A decided prompt's card is deleted, unless the Trust sweep re-wrote it in place."""
    from slack_code_channel_turns import prompt_in_channel_session
    from turn_harness import APPROVED

    def _sweep(channel: str, ts: str) -> None:
        # What set_channel_mode does to the channel's own pending cards before
        # its Trust sweep answers them (the sweep then writes "Ran under Trust").
        if rewritten:
            approval_picker._kept_cards.add(f"{channel}:{ts}")

    asyncio.run(
        prompt_in_channel_session(
            chan, slot_key=SLOT, channel=CH, anchor=ANCHOR, answer=APPROVED, during=_sweep
        )
    )
    ts = chan.slack.blocks_posts[-1]
    card_ts = f"300.{chan.slack._n:06d}"
    assert ts[0] == CH
    assert chan.slack.deleted == ([] if rewritten else [(CH, card_ts)])


def test_a_clicked_channel_card_is_written_once(chan: Any) -> None:  # noqa: F811
    from test_slack_code_channel_trust_from_slack import _click as _trust_click
    from test_slack_code_channel_trust_from_slack import _post_card

    ts = _post_card(chan)

    async def go() -> None:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures["req-7"] = fut
        assert await approval_picker.handle_trust_call(chan.orch, _trust_click(OWNER, ts))

    asyncio.run(go())
    mine = [u for u in chan.slack.updates if u[1] == ts]
    assert len(mine) == 1 and "now on Trust" in mine[0][2]
    assert approval_picker.keep_card(CH, ts) is True  # and the runner keeps it


def test_a_clicked_worker_card_is_written_once(relay: Any) -> None:  # noqa: F811
    async def go() -> Any:
        card, fut = await _run_relay(relay)
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card

    card = asyncio.run(go())
    mine = [u for u in relay.slack.updates if u[1] == card.ts]
    assert len(mine) == 1 and "Ran under Trust" not in mine[0][2]


def test_a_claimed_card_rejected_meanwhile_says_so(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    relay.slot._trust = True
    holder: dict[str, Any] = {}
    _during_grant(relay, monkeypatch, 1, lambda: holder["fut"].set_result("rejected"))

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        holder["fut"] = fut
        await _set("trust")
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card

    card = asyncio.run(go())
    mine = [u for u in relay.slack.updates if u[1] == card.ts]
    assert len(mine) == 1 and mine[0][2] == wa.ANSWERED_ELSEWHERE["rejected"]


def test_a_colon_prefixed_warning_title_stays() -> None:
    from kiro_crew.slack.format import title_restates_command

    assert title_restates_command("Running: git status", "git status")
    assert title_restates_command("Running: git log --one…", "git log --oneline -5")
    assert not title_restates_command("WARNING - production action: git status", "git status")
    assert not title_restates_command("Deploy: git status", "git status")


def test_an_origin_card_rejected_during_its_grant_is_settled(
    relay: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Channel Trust commits, then the worker's own prompt is rejected mid-grant."""
    holder: dict[str, Any] = {}
    # 1 = the channel's grant, 2 = the worker's
    _during_grant(relay, monkeypatch, 2, lambda: holder["fut"].set_result("rejected"))

    async def go() -> Any:
        card, fut = await _run_relay(relay)
        holder["fut"] = fut
        assert await wa.handle_click(relay.orch, _click(OWNER, card, wa.TRUST_ACTION_ID))
        await wa.finish(_done(card), fut.result(), deny_cause="", host_ended=False)
        return card

    card = asyncio.run(go())
    mine = [u for u in relay.slack.updates if u[1] == card.ts]
    assert mine and mine[-1][2] == wa.ANSWERED_ELSEWHERE["rejected"]
    assert all(b.get("type") != "actions" for b in mine[-1][3])
