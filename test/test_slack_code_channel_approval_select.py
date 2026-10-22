"""Slack Code channels: the kickoff's Approval mode select and ``/kirocrew trust``.

The kickoff carries an **Approval mode** select (Ask / Trust reads / Trust)
below the agent picker, preselected with the mode the session holds, and
``/kirocrew trust <ask|reads|all>`` sets the same thing from the channel. Both
reuse #21991's owner gate, kickoff blocks and sub-command routing, and change
the mode through ``apply_approval_mode`` (the dashboard picker's audited
mutation). Owner only; Trust posts the channel's warning line.

Recorded Slack payloads in, recorded calls out (the agent-picker ``env``).
"""

from __future__ import annotations

import asyncio
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
    assert [o["text"]["text"] for o in select["options"]] == ["Ask", "Trust reads", "Trust"]


@pytest.mark.parametrize("mode", ["normal", "trust_reads", "trust"])
def test_the_select_starts_on_the_session_mode(mode: str) -> None:
    blocks = agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode=mode)
    select = _approval_select(blocks)
    assert select is not None and select["initial_option"]["value"] == mode
    assert _approval_select(agent_picker.kickoff_blocks("hi", ["a"], "a")) is None


# ── The select ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["trust_reads", "trust"])
def test_the_owner_sets_a_mode_from_the_select(chan: Any, mode: str) -> None:
    assert asyncio.run(approval_picker.handle_select(chan.orch, _pick(mode)))
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
        assert "`/kirocrew trust ask`" in warnings[0][1] and "Approval mode" in warnings[0][1]


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


@pytest.mark.parametrize(("word", "mode"), [("reads", "trust_reads"), ("all", "trust")])
def test_the_owner_sets_a_mode_with_the_command(chan: Any, word: str, mode: str) -> None:
    replies = _run_slash(chan, f"trust {word}")
    assert _mode_of(chan.slot) == mode and _changes(chan, mode)
    assert replies and "now on" in replies[-1]
    assert _approval_select(_redrawn(chan))["initial_option"]["value"] == mode


def test_trust_ask_turns_it_off(chan: Any) -> None:
    chan.slot._trust = True
    replies = _run_slash(chan, "trust ask")
    assert _mode_of(chan.slot) == "normal" and "Ask" in replies[-1]


def test_bare_trust_says_the_mode(chan: Any) -> None:
    chan.slot._trust_reads = True
    replies = _run_slash(chan, "trust")
    assert "*Trust reads*" in replies[-1] and chan.mode_rows == []


def test_an_unknown_word_is_refused_and_escaped(chan: Any) -> None:
    replies = _run_slash(chan, "trust <!channel>")
    assert _mode_of(chan.slot) == "normal" and chan.mode_rows == []
    assert "<!channel>" not in replies[-1] and "&lt;!channel&gt;" in replies[-1]


def test_a_non_owner_command_is_refused(chan: Any) -> None:
    replies = _run_slash(chan, "trust all", user=STRANGER)
    assert _mode_of(chan.slot) == "normal" and chan.mode_rows == []
    assert replies == [approval_picker.NOT_ALLOWED_TEXT]
    row = chan.sel.ops("slack.code_channel.command")[-1]
    assert (row["outcome"], row["error"]) == ("denied", "approval mode change refused")


def test_the_agent_commands_denial_row_is_unchanged(chan: Any) -> None:
    _run_slash(chan, "agent builder", user=STRANGER, trigger="T-agent")
    row = chan.sel.ops("slack.code_channel.command")[-1]
    assert (row["outcome"], row["error"]) == ("denied", "agent change refused")


def test_trust_outside_a_code_channel_is_not_ours(chan: Any) -> None:
    payload = {**_slash("trust all"), "channel_id": "C0ELSEWHERE"}

    async def respond(_msg: str) -> None:
        raise AssertionError("not ours to answer")

    assert asyncio.run(commands.handle_slash(chan.orch, payload, respond)) is False
    assert _mode_of(chan.slot) == "normal"


def test_trust_with_the_feature_off_is_not_ours(chan: Any) -> None:
    chan.on = False

    async def respond(_msg: str) -> None:
        raise AssertionError("not ours to answer")

    assert asyncio.run(commands.handle_slash(chan.orch, _slash("trust all"), respond)) is False
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
    assert _mode_of(chan.slot) == "normal"  # the slot's own flags already say Ask
    replies = _run_slash(chan, "trust ask")
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
    _run_slash(chan, "trust ask")
    assert _changes(chan, "normal")  # went through apply_approval_mode


def test_the_kickoff_says_when_yolo_is_on(monkeypatch: Any) -> None:
    override = _Override(False)
    monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
    blocks = agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode="normal")
    assert approval_picker.YOLO_ON_NOTE in str(blocks)
    override.active = False
    assert approval_picker.YOLO_ON_NOTE not in str(
        agent_picker.kickoff_blocks("hi", ["a"], "a", approval_mode="normal")
    )


def test_the_reply_does_not_claim_yolo_ended_when_it_was_off(chan: Any) -> None:
    replies = _run_slash(chan, "trust all")
    assert approval_picker.YOLO_OFF_TEXT.strip() not in replies[-1]
