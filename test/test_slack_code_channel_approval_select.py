"""Slack Code channels: the kickoff's Approval mode select and ``/kirocrew trust``.

The kickoff carries an **Approval mode** select (Normal / Reads / Trust)
below the agent picker, preselected with the mode the session holds, and
``/kirocrew trust <normal|reads|all>`` (``ask`` still means normal) sets the same thing from the channel. Both
reuse the agent picker's owner gate, kickoff blocks and sub-command routing, and change
the mode through ``apply_approval_mode`` (the dashboard picker's audited
mutation). Owner only; Trust posts the channel's warning line.

Recorded Slack payloads in, recorded calls out (the agent-picker ``env``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, Env

# The agent picker's harness: fake Slack, store, SEL, owner check, kickoff open.
from test_slack_code_channel_agent_picker import (  # noqa: F401
    ANCHOR,
    CH,
    SLOT,
    STRANGER,
    _bind,
    _open,
    _slash,
    env,
)

import kiro_crew.dashboard.chat_handlers as chat_handlers
from kiro_crew import safety_override as safety_override_mod
from kiro_crew.slack.code_channels import agent_picker, approval_picker, commands
from kiro_crew.slack.code_channels import worker_approvals as wa

OPERATION = approval_picker.OPERATION


class _Sessions:
    def __init__(self) -> None:
        self.policies: dict[str, str] = {}

    def set_approval_policy(self, key: str, policy: str) -> None:
        self.policies[key] = policy


@pytest.fixture
def chan(env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    """A bound code channel whose session can really change approval mode."""
    slot = _bind(env)
    for name, value in (
        ("linked_session_key", ""),
        ("_trust", False),
        ("_trust_reads", False),
        ("_trust_scope", ""),
        ("_approval_futures", {}),
        ("messages", []),
        ("_slack_channel", CH),
        ("_dirty", False),
    ):
        setattr(slot, name, value)
    env.slot = slot
    assert env.state is not None
    env.state._slots = {SLOT: slot}
    env.state.sessions = _Sessions()
    env.state._approval_futures = {}
    env.state.channel_manager = None
    env.mode_rows = []
    monkeypatch.setattr(
        chat_handlers,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: env.mode_rows.append(kw)),
    )
    monkeypatch.setattr(safety_override_mod, "approval_mode_permitted", lambda _m: True)
    monkeypatch.setattr(approval_picker, "live_config", env.cfg)
    approval_picker.reset()
    return env


def _pick(mode: str, *, user: str = OWNER, ts: str = ANCHOR) -> dict[str, Any]:
    """A recorded ``block_actions`` payload for the kickoff's Approval mode select."""
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user, "team_id": "T1"},
        "channel": {"id": CH},
        "message": {"ts": ts, "blocks": []},
        "actions": [
            {
                "type": "static_select",
                "action_id": approval_picker.APPROVAL_SELECT_ACTION_ID,
                "selected_option": {"value": mode},
            }
        ],
    }


def _confirm_button(e: Any) -> dict[str, Any]:
    """The "Switch to <mode>" button of the last private confirmation."""
    blocks = e.slack.ephemeral_blocks[-1]
    assert blocks, "no confirmation was posted"
    return next(el for b in blocks if b.get("type") == "actions" for el in b["elements"])


def _confirm_click(e: Any, *, user: str = OWNER, value: str | None = None) -> dict[str, Any]:
    """A ``block_actions`` payload for that button (an ephemeral: no message ts)."""
    button = _confirm_button(e)
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user, "team_id": "T1"},
        "channel": {"id": CH},
        "container": {"type": "message", "is_ephemeral": True},
        "actions": [
            {
                "type": "button",
                "action_id": approval_picker.APPROVAL_CONFIRM_ACTION_ID,
                "value": button["value"] if value is None else value,
            }
        ],
    }


def _pick_and_confirm(e: Any, mode: str) -> None:
    assert asyncio.run(approval_picker.handle_select(e.orch, _pick(mode)))
    assert asyncio.run(approval_picker.handle_confirm(e.orch, _confirm_click(e)))


def _redrawn(e: Any) -> list[dict[str, Any]]:
    """The kickoff's last re-draw."""
    edits = [blocks for _ch, ts, _text, blocks in e.slack.edits if ts == ANCHOR]
    assert edits, "the kickoff was not re-drawn"
    return edits[-1] or []


def _approval_select(blocks: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for block in blocks or []:
        accessory = block.get("accessory") or {}
        if accessory.get("action_id") == approval_picker.APPROVAL_SELECT_ACTION_ID:
            return accessory
    return None


def _mode_of(slot: Any) -> str:
    return approval_picker.current_mode(slot)


def _changes(e: Any, mode: str) -> list[dict[str, Any]]:
    return [r for r in e.mode_rows if r["operation"] == f"mode_change:{mode}"]


def _run_slash(e: Any, text: str, *, user: str = OWNER, trigger: str = "T-9") -> list[str]:
    replies: list[str] = []

    async def respond(msg: str) -> None:
        replies.append(msg)

    assert asyncio.run(
        commands.handle_slash(e.orch, _slash(text, user=user, trigger=trigger), respond)
    )
    return replies


# ── The kickoff ───────────────────────────────────────────────────────────


def test_the_kickoff_carries_both_pickers(env: Env) -> None:  # noqa: F811
    blocks = asyncio.run(_open(env))
    ids = [(b.get("accessory") or {}).get("action_id") for b in blocks]
    assert ids.index(agent_picker.AGENT_SELECT_ACTION_ID) < ids.index(
        approval_picker.APPROVAL_SELECT_ACTION_ID
    )
    select = _approval_select(blocks)
    assert select is not None and select["initial_option"]["value"] == "normal"
    assert [o["text"]["text"] for o in select["options"]] == ["Normal", "Reads", "Trust"]


@pytest.mark.parametrize("mode", ["normal", "trust_reads", "trust"])
def test_the_select_starts_on_the_session_mode(mode: str) -> None:
    blocks = agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode=mode)
    select = _approval_select(blocks)
    assert select is not None and select["initial_option"]["value"] == mode
    assert _approval_select(agent_picker.kickoff_blocks("hi", ["a"], "a")) is None


# ── The select ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["trust_reads", "trust"])
def test_the_owner_sets_a_mode_from_the_select(chan: Any, mode: str) -> None:
    _pick_and_confirm(chan, mode)
    assert _mode_of(chan.slot) == mode
    rows = _changes(chan, mode)
    assert rows and rows[0]["caller"] == f"slack-code-channels:{OWNER}"
    assert any("via=select result=set" in r["resources"] for r in chan.sel.ops(OPERATION))
    # The kickoff is re-drawn on the new mode, with who set it.
    blocks = _redrawn(chan)
    assert _approval_select(blocks)["initial_option"]["value"] == mode
    assert any(f"<@{OWNER}>" in str(b) for b in blocks if b.get("type") == "context")
    warnings = [p for p in chan.slack.posts if "*Trust*" in p[1]]
    assert (len(warnings) == 1) == (mode == "trust")
    if warnings:
        assert "`/kirocrew approval normal`" in warnings[0][1] and "Approval mode" in warnings[0][1]
        assert "back to Normal" in warnings[0][1]


def test_the_owner_sets_ask_back_from_the_select(chan: Any) -> None:
    chan.slot._trust = True
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("normal")))
    assert _mode_of(chan.slot) == "normal" and _changes(chan, "normal")


@pytest.mark.parametrize("case", ["non_owner", "not_the_kickoff", "unknown_mode"])
def test_a_refused_pick_changes_nothing(chan: Any, case: str) -> None:
    payload = {
        "non_owner": _pick("trust", user=STRANGER),
        "not_the_kickoff": _pick("trust", ts="999.000001"),
        "unknown_mode": _pick("yolo"),
    }[case]
    assert asyncio.run(approval_picker.handle_select(chan.orch, payload))
    assert _mode_of(chan.slot) == "normal" and chan.mode_rows == []
    assert chan.slack.ephemerals
    assert chan.sel.ops(OPERATION)[-1]["outcome"] == "denied"


def test_the_dispatcher_routes_the_select(chan: Any, monkeypatch: Any) -> None:
    seen: list[str] = []

    async def select(_orch: Any, p: dict[str, Any]) -> bool:
        seen.append(p["actions"][0]["selected_option"]["value"])
        return True

    monkeypatch.setattr(approval_picker, "handle_select", select)
    action = approval_picker.APPROVAL_SELECT_ACTION_ID
    assert asyncio.run(wa.dispatch_card_click(chan.orch, _pick("trust"), action))
    assert seen == ["trust"]


def test_a_non_owners_pick_is_refused_by_the_dispatcher(chan: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack import interactions

    monkeypatch.setattr(interactions, "_orch", chan.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in chan.allowed)
    monkeypatch.setattr(interactions, "sel", lambda: chan.sel)
    asyncio.run(interactions.dispatch(_pick("trust", user=STRANGER)))
    assert _mode_of(chan.slot) == "normal"
    assert chan.slack.ephemerals[-1][1] == STRANGER
    # The picker's own decision row, beside the generic interactive one.
    assert chan.sel.ops(OPERATION)[-1]["outcome"] == "denied"
    assert f"user={STRANGER}" in chan.sel.ops(OPERATION)[-1]["resources"]


# ── /kirocrew trust ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("approval reads", "trust_reads"),
        ("approval trust", "trust"),
    ],
)
def test_the_owner_sets_a_mode_with_the_command(chan: Any, text: str, mode: str) -> None:
    replies = _run_slash(chan, text)
    assert _mode_of(chan.slot) == mode and _changes(chan, mode)
    assert replies and "now on" in replies[-1]
    assert _approval_select(_redrawn(chan))["initial_option"]["value"] == mode


def test_approval_normal_turns_it_off(chan: Any) -> None:
    chan.slot._trust = True
    replies = _run_slash(chan, "approval normal")
    assert _mode_of(chan.slot) == "normal" and "*Normal*" in replies[-1]


def test_the_bare_command_says_the_mode_and_names_the_command_after_it(chan: Any) -> None:
    """The help names the command after the mode: ``approval normal``, never a
    ``trust`` command that turns Trust off."""
    chan.slot._trust_reads = True
    replies = _run_slash(chan, "approval")
    assert "*Reads*" in replies[-1] and chan.mode_rows == []
    for word in ("normal", "reads", "trust"):
        assert f"/kirocrew approval {word}`" in replies[-1]
    assert "trust normal" not in replies[-1] and "Trust reads" not in replies[-1]


@pytest.mark.parametrize("word", ["ask", "all"])
def test_words_that_never_shipped_are_not_modes(chan: Any, word: str) -> None:
    chan.slot._trust = True
    replies = _run_slash(chan, f"approval {word}")
    assert _mode_of(chan.slot) == "trust" and chan.mode_rows == []
    assert "isn't a mode" in replies[-1]


def test_kirocrew_trust_is_not_a_code_channel_command(chan: Any) -> None:
    """Only ``approval`` sets the mode: ``trust`` is left to the app's own command."""

    async def respond(_reply: str) -> None:
        raise AssertionError("a code channel must not answer /kirocrew trust")

    assert asyncio.run(commands.handle_slash(chan.orch, _slash("trust normal"), respond)) is False


def test_an_unknown_word_is_refused_and_escaped(chan: Any) -> None:
    replies = _run_slash(chan, "approval <!channel>")
    assert _mode_of(chan.slot) == "normal" and chan.mode_rows == []
    assert "<!channel>" not in replies[-1] and "&lt;!channel&gt;" in replies[-1]


def test_a_non_owner_command_is_refused(chan: Any) -> None:
    replies = _run_slash(chan, "approval trust", user=STRANGER)
    assert _mode_of(chan.slot) == "normal" and chan.mode_rows == []
    assert replies == [approval_picker.OWNER_ONLY_TEXT]
    row = chan.sel.ops("slack.code_channel.command")[-1]
    assert (row["outcome"], row["error"]) == ("denied", "approval mode change refused")


def test_the_agent_commands_denial_row_is_unchanged(chan: Any) -> None:
    _run_slash(chan, "agent builder", user=STRANGER, trigger="T-agent")
    row = chan.sel.ops("slack.code_channel.command")[-1]
    assert (row["outcome"], row["error"]) == ("denied", "agent change refused")


def test_trust_outside_a_code_channel_is_not_ours(chan: Any) -> None:
    payload = {**_slash("approval trust"), "channel_id": "C0ELSEWHERE"}

    async def respond(_msg: str) -> None:
        raise AssertionError("not ours to answer")

    assert asyncio.run(commands.handle_slash(chan.orch, payload, respond)) is False
    assert _mode_of(chan.slot) == "normal"


def test_trust_with_the_feature_off_is_not_ours(chan: Any) -> None:
    chan.on = False

    async def respond(_msg: str) -> None:
        raise AssertionError("not ours to answer")

    assert asyncio.run(commands.handle_slash(chan.orch, _slash("approval trust"), respond)) is False
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust"))) is False
    assert _mode_of(chan.slot) == "normal"


class _Override:
    """A live YOLO grant (declared or ad hoc) the slot's own flags do not show."""

    def __init__(self, declared: bool) -> None:
        self.is_declared = declared
        self.active = True
        self.ended: list[str] = []

    def is_active(self) -> bool:
        return self.active

    def deactivate(self, who: str) -> None:
        self.ended.append(who)
        self.active = False


@pytest.mark.parametrize("declared", [True, False])
def test_ask_ends_a_live_yolo_grant_even_when_the_slot_reads_ask(
    chan: Any, monkeypatch: Any, declared: bool
) -> None:
    override = _Override(declared)
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
    assert _mode_of(chan.slot) == "normal"  # the slot's own flags already say Normal
    replies = _run_slash(chan, "approval normal")
    assert override.ended and not override.active
    assert _changes(chan, "normal")
    assert "already" not in replies[-1]
    assert approval_picker.YOLO_OFF_TEXT.strip() in replies[-1]


def test_the_select_ends_a_live_yolo_grant_too(chan: Any, monkeypatch: Any) -> None:
    override = _Override(True)
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("normal")))
    assert override.ended and _changes(chan, "normal")


def test_an_unreadable_override_never_reads_as_unchanged(chan: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(approval_picker, "_yolo_active", lambda: True)
    _run_slash(chan, "approval normal")
    assert _changes(chan, "normal")  # went through apply_approval_mode


def test_the_kickoff_says_when_yolo_is_on(monkeypatch: Any) -> None:
    override = _Override(False)
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
    blocks = agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode="normal")
    assert approval_picker.YOLO_ON_NOTE in str(blocks)
    # It leads with the effect, says how far it reaches and what turns it off.
    assert approval_picker.YOLO_ON_NOTE.startswith("⚡ Every tool runs without asking")
    assert "every session" in approval_picker.YOLO_ON_NOTE
    assert "whatever this select shows" in approval_picker.YOLO_ON_NOTE
    assert "`yolo off`" in approval_picker.YOLO_ON_NOTE
    assert "whatever this says" not in approval_picker.YOLO_ON_NOTE
    override.active = False
    assert approval_picker.YOLO_ON_NOTE not in str(
        agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode="normal")
    )


def _trust_button(blocks: list[dict[str, Any]], action_id: str) -> dict[str, Any]:
    return next(
        e
        for b in blocks
        if b.get("type") == "actions"
        for e in b["elements"]
        if e.get("action_id") == action_id
    )


def test_trust_this_channel_confirms_with_a_red_button_on_both_cards() -> None:
    """The riskiest action in the feature: its dialog's confirm is danger red,
    as the select's own "Switch to Trust" is, on the channel's card and on a
    worker's card, whose dialog says it is the worker's call."""
    from kiro_crew.slack.code_channels import worker_approvals as wa
    from kiro_crew.slack.handler import _build_approval_blocks, _LinkedApprovalEvent

    channel_card = _build_approval_blocks(
        _LinkedApprovalEvent("r1", "Running: ls", "ls"),  # type: ignore[arg-type]
        is_dm=False,
        channel_trust=True,
    )
    own = _trust_button(channel_card, approval_picker.TRUST_CALL_ACTION_ID)["confirm"]
    worker = _trust_button(wa._blocks("head", "r1", "Running: ls", "ls"), wa.TRUST_ACTION_ID)
    for dialog in (own, worker["confirm"]):
        assert dialog["style"] == "danger"
        assert dialog["confirm"]["text"] == approval_picker.TRUST_CALL_LABEL
    _t, switch = approval_picker.confirm_blocks("trust", "chat-1-1")
    assert "danger" in json.dumps(switch)  # the select's own Trust step: the same colour
    assert "the worker's call" in worker["confirm"]["text"]["text"]
    assert "whole channel on Trust" in worker["confirm"]["text"]["text"]
    assert "the worker's call" not in own["text"]["text"]


def test_the_reply_does_not_claim_yolo_ended_when_it_was_off(chan: Any) -> None:
    replies = _run_slash(chan, "approval trust")
    assert approval_picker.YOLO_OFF_TEXT.strip() not in replies[-1]


# ── Seams with the agent picker ───────────────────────────────────────────


def test_a_pick_from_a_workspace_not_admitted_posts_nothing(
    chan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels import surface

    monkeypatch.setattr(surface, "workspace_admitted", lambda _team: False)
    # A stale picker: no live kickoff to check first, so the workspace must be.
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust", ts="999.000001")))
    assert chan.slack.ephemerals == [] and _mode_of(chan.slot) == "normal"
    rows = chan.sel.ops(approval_picker.OPERATION)
    assert rows and rows[-1]["error"] == "enterprise_origin_mismatch"


def test_a_select_value_that_is_no_mode_is_audited_as_invalid(chan: Any) -> None:
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust result=set")))
    rows = chan.sel.ops(approval_picker.OPERATION)
    assert rows and all("result=set via" not in r["resources"] for r in rows)
    assert f"mode={agent_picker.INVALID_NAME}" in rows[-1]["resources"]


def test_a_rebind_during_a_raise_points_at_the_live_kickoff(
    chan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    async def load_and_rebind(_slot: Any) -> bool:
        current = chan.store.get_binding(CH)
        assert current is not None
        chan.store.replace_binding(replace(current, session_key="chat-other-1", anchor_ts="2.0"))
        return True

    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust")))
    click = _confirm_click(chan)
    monkeypatch.setattr(agent_picker, "_load_window", load_and_rebind)
    # The confirmation names the session it was asked for: the rebind lands
    # inside the change, which points at the live kickoff.
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, click))
    assert _mode_of(chan.slot) == "normal"
    assert chan.slack.ephemerals[-1][2] == approval_picker.REBOUND_TEXT.format(command="kirocrew")
    assert "agent <name>" not in approval_picker.REBOUND_TEXT


def test_a_cancelled_pick_puts_the_select_back(chan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def cancelled(*_a: Any, **_k: Any) -> Any:
        raise asyncio.CancelledError

    chan.slot._trust = True  # Normal applies straight from the select
    monkeypatch.setattr(approval_picker, "set_channel_mode", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(approval_picker.handle_select(chan.orch, _pick("normal")))
    assert _approval_select(_redrawn(chan)) is not None


def test_both_pickers_show_only_a_valid_command_name(chan: Any) -> None:
    chan.orch.slack_command = "bad name!"
    assert agent_picker._command_name(chan.orch) == "kirocrew"
    chan.orch.slack_command = "crew"
    assert agent_picker._command_name(chan.orch) == "crew"


@pytest.mark.parametrize(("mode", "label"), [("trust_reads", "Reads"), ("trust", "Trust")])
def test_a_raise_is_confirmed_privately_naming_the_mode(chan: Any, mode: str, label: str) -> None:
    """Block Kit confirms a select as a whole, so the select has no dialog; a pick
    of Reads or Trust changes nothing and asks privately, naming the mode."""
    assert "confirm" not in approval_picker.approval_block("normal")["accessory"]
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick(mode)))
    assert _mode_of(chan.slot) == "normal" and _changes(chan, mode) == []
    channel, user, text = chan.slack.ephemerals[-1]
    assert (channel, user) == (CH, OWNER)
    assert f"*{label}*" in text and approval_picker.MODE_EFFECT[mode] in text
    button = _confirm_button(chan)
    assert button["text"]["text"] == f"Switch to {label}"
    assert len(button["text"]["text"]) <= 75  # Slack's button-label limit
    # The select goes back to the mode the session really has.
    assert _approval_select(_redrawn(chan))["initial_option"]["value"] == "normal"
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, _confirm_click(chan)))
    assert _mode_of(chan.slot) == mode


def test_normal_applies_straight_from_the_select(chan: Any) -> None:
    """Normal only takes grants away: no confirmation."""
    chan.slot._trust = True
    blocks_before = len(chan.slack.ephemeral_blocks)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("normal")))
    assert _mode_of(chan.slot) == "normal"
    assert all(b is None for b in chan.slack.ephemeral_blocks[blocks_before:])


@pytest.mark.parametrize("case", ["expired", "other_session", "unknown_mode", "garbled"])
def test_a_stale_confirmation_changes_nothing(chan: Any, case: str) -> None:
    import time

    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust")))
    mode, session, asked = _confirm_button(chan)["value"].split("|")
    value = {
        "expired": f"{mode}|{session}|{int(asked) - approval_picker.CONFIRM_TTL_SECS - 5}",
        "other_session": f"{mode}|chat-other-1|{int(time.time())}",
        "unknown_mode": f"yolo|{session}|{int(time.time())}",
        "garbled": "trust",
    }[case]
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, _confirm_click(chan, value=value)))
    assert _mode_of(chan.slot) == "normal"
    assert chan.slack.ephemerals[-1][2] == approval_picker.CONFIRM_STALE_TEXT


def test_someone_elses_confirmation_click_changes_nothing(chan: Any) -> None:
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust")))
    assert asyncio.run(
        approval_picker.handle_confirm(chan.orch, _confirm_click(chan, user=STRANGER))
    )
    assert _mode_of(chan.slot) == "normal"
    assert chan.slack.ephemerals[-1][2] == approval_picker.OWNER_ONLY_TEXT


def test_normal_is_the_command_word(chan: Any) -> None:
    chan.slot._trust = True
    replies = _run_slash(chan, "approval normal")
    assert _mode_of(chan.slot) == "normal"
    assert replies and "now on" in replies[-1]
    shown = _run_slash(chan, "approval", trigger="T-shown")
    assert "approval normal" in shown[-1] and " ask" not in shown[-1]


def test_the_kickoff_says_what_each_mode_does() -> None:
    """Block Kit options carry no description: a context line right under the
    select explains Normal, Reads and Trust."""
    blocks = agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode="normal")
    index = next(i for i, b in enumerate(blocks) if _approval_select([b]) is not None)
    note = blocks[index + 1]
    assert note["type"] == "context"
    text = note["elements"][0]["text"]
    assert text == approval_picker.MODES_NOTE
    for word in ("Normal", "Reads", "read-only", "Trust"):
        assert word in text


def test_a_confirmation_is_good_for_one_click(chan: Any) -> None:
    """Lowered again after the switch, the same button cannot raise it a second time."""
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust")))
    click = _confirm_click(chan)
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, click))
    assert _mode_of(chan.slot) == "trust"
    _run_slash(chan, "approval normal", trigger="T-lower")
    assert _mode_of(chan.slot) == "normal"
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, click))
    assert _mode_of(chan.slot) == "normal"
    assert chan.slack.ephemerals[-1][2] == approval_picker.CONFIRM_STALE_TEXT


def test_a_pick_of_the_current_mode_never_raises_past_a_drop(
    chan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session holds Trust, so the pick applies without asking; should it drop
    to Normal while that change waits, nothing raises it back."""
    chan.slot._trust = True
    real = agent_picker._load_window

    async def lowered_meanwhile(slot: Any) -> bool:
        chan.slot._trust = False  # the dashboard put it on Normal
        return await real(slot)

    monkeypatch.setattr(agent_picker, "_load_window", lowered_meanwhile)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust")))
    assert _mode_of(chan.slot) == "normal" and _changes(chan, "trust") == []


def test_under_yolo_a_pick_of_the_current_mode_asks_first(
    chan: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under YOLO the select is no reading of the mode: a Reads or Trust pick asks."""
    chan.slot._trust_reads = True
    monkeypatch.setattr(approval_picker, "_yolo_active", lambda: True)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick("trust_reads")))
    assert _changes(chan, "trust_reads") == []
    assert _confirm_button(chan)["text"]["text"] == "Switch to Reads"
    assert asyncio.run(approval_picker.handle_confirm(chan.orch, _confirm_click(chan)))
    assert _changes(chan, "trust_reads")
