"""Approve or Deny a pending tool approval from Slack (RFC notification-bridge-actions, B4c).

Drives the real path end to end: the prompt's hand-off binds the open approval and
publishes the note, the Slack sink posts it with Approve and Deny buttons, and a
press on a posted button goes through ``interactions.dispatch`` to the registry.
Pins the four rules a chat app answering a tool approval must keep:

* the token is random, single-use, bound to the session, turn and owner, dead at
  the prompt's own deadline, and shown in no text;
* only the owner's press, by the ``user.id`` Slack sends, answers; a stranger's or
  a stale press changes nothing and is audited;
* fail closed: the preview off, no binding, an expired token or a press that does
  not parse gives no answer;
* a card already in a linked Slack thread gets no second copy and no binding.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_actions import (
    SLACK_ACTION_PREFIX,
    ActionOutcome,
    BridgeActionRegistry,
)
from kiro_crew.notifications.bridge_egress import redact_note
from kiro_crew.notifications.bridge_slack import SlackBridgeSink
from kiro_crew.slack import interactions
from kiro_crew.slack.approval_tokens import split_approval_token
from kiro_crew.slack.interactions_approvals import BRIDGE_ACTION_STALE_NOTICE

_OWNER = "U-OWNER"
_DM = "D-OWNER"
_POSTED_TS = "2.0"

#: The prompt's wait window, on the fake clock; nothing sleeps.
_WINDOW_S = 600.0
_PROMPT = {
    "slot_key": "chat-1",
    "session_key": "dashboard:chat-1",
    "request_id": "7",
    "tool_title": "execute_bash",
    "session_title": "Release notes",
    "approval_detail": "git push origin release",
    "answerable": True,
    "crew_log_session": "crew-1",
    "turn": 3,
    "ttl_s": _WINDOW_S,  # flake-ok: a deadline on the fake clock, nothing waits
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeSlack:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.blocks: list[tuple[list[dict[str, Any]], str]] = []
        self.post_ephemeral = AsyncMock()

    async def open_dm(self, user_id: str) -> str | None:
        return _DM if user_id == _OWNER else None

    async def post_message(self, channel: str, text: str) -> str:
        self.texts.append(text)
        return "1.0"

    async def post_blocks(self, channel: str, blocks: list[dict], text: str) -> str:
        self.blocks.append((blocks, text))
        return _POSTED_TS


class _Gateway:
    """The parts of the gateway state the hand-off, the sink and the press read."""

    def __init__(self) -> None:
        self.bridge_actions = BridgeActionRegistry()
        self.owner_id = _OWNER
        self.slack_client = _FakeSlack()
        self.notification_bridge = object()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self.bridged: list[dict[str, Any]] = []

    def _bridge_note(self, note: dict[str, Any], publisher: str = "") -> None:
        self.bridged.append(note)


def _cfg(on: bool) -> Any:
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews={note_mod.PREVIEW: on}))


@pytest.fixture()
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(actions_mod, "bridge_clock", fake)
    return fake


@pytest.fixture()
def preview(monkeypatch) -> dict[str, bool]:
    """The live ``bridge_approvals`` preview, as both the hand-off and the press read it."""
    live = {"on": True}
    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", classmethod(lambda cls: _cfg(live["on"]))
    )
    return live


@pytest.fixture()
def gateway(monkeypatch, clock, preview) -> _Gateway:
    state = _Gateway()
    orch = SimpleNamespace(slack=state.slack_client, dashboard_state=state)
    monkeypatch.setattr("kiro_crew.slack.interactions._orch", orch)
    monkeypatch.setattr("kiro_crew.slack.interactions.is_allowed_user", lambda user_id: True)
    monkeypatch.setattr("kiro_crew.slack.interactions.is_owner", lambda user_id: user_id == _OWNER)
    monkeypatch.setattr(
        "kiro_crew.slack.interactions.channel_inbound_permitted", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("kiro_crew.slack.interactions.channel_refusal", lambda *a, **kw: None)
    return state


@pytest.fixture()
def audits(monkeypatch) -> list[tuple[ActionOutcome, dict[str, Any]]]:
    seen: list[tuple[ActionOutcome, dict[str, Any]]] = []
    monkeypatch.setattr(
        "kiro_crew.notifications.bridge_action_audit.audit_action",
        lambda registry, token, outcome, **kw: seen.append((outcome, kw)),
    )
    return seen


async def _open(state: _Gateway, **overrides: Any) -> tuple[asyncio.Future[Any], dict[str, Any]]:
    """Open one approval the way the prompt does, and deliver its note to Slack."""
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(state, decision, **{**_PROMPT, **overrides})
    assert task is not None
    await task
    [note] = state.bridged
    # Through the dispatcher's own egress copy, as every sink receives it.
    await SlackBridgeSink(state).deliver(redact_note(note))
    return decision, note


def _button(state: _Gateway, choice: str) -> dict[str, Any]:
    [(blocks, _text)] = state.slack_client.blocks
    [button] = [
        b for b in blocks[-1]["elements"] if b["action_id"] == f"{SLACK_ACTION_PREFIX}{choice}"
    ]
    return button


async def _press(button: dict[str, Any], user_id: str = _OWNER) -> None:
    await interactions.dispatch(
        {
            "type": "block_actions",
            "user": {"id": user_id},
            "channel": {"id": _DM},
            "message": {"ts": _POSTED_TS},
            "actions": [{"action_id": button["action_id"], "value": button["value"]}],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "value"), [("approve", "approved"), ("deny", "rejected")])
async def test_the_owners_press_answers_the_open_approval(gateway, audits, choice, value):
    decision, _note = await _open(gateway)
    await _press(_button(gateway, choice))
    assert decision.result() == value, "the value the dashboard's own button sets"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED
    assert (kw["transport"], kw["choice"], kw["actor"]) == ("slack", choice, _OWNER)
    gateway.slack_client.post_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_binding_names_the_session_turn_and_owner(gateway, audits):
    _decision, note = await _open(gateway)
    token = note[note_mod.ACTION_TOKEN_KEY]
    context = gateway.bridge_actions.context_for(token)
    assert context is not None
    assert (context.session_key, context.crew_log_session, context.turn) == (
        "dashboard:chat-1",
        "crew-1",
        3,
    )
    assert context.owner == _OWNER
    assert gateway.bridge_actions.token_for_message("slack", f"{_DM}:{_POSTED_TS}") == token


@pytest.mark.asyncio
async def test_the_token_is_random_and_shown_in_no_text(gateway):
    _decision, note = await _open(gateway)
    token = note[note_mod.ACTION_TOKEN_KEY]
    _key, nonce = split_approval_token(token)
    assert len(nonce) >= 22, "secrets.token_urlsafe(16) or stronger"
    shown = [note["title"], note["body"], *gateway.slack_client.texts]
    [(blocks, fallback)] = gateway.slack_client.blocks
    shown.append(fallback)
    shown += [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert all(nonce not in text for text in shown)
    assert "execute_bash" in blocks[0]["text"]["text"], "the buttons sit under the note"
    assert {b["value"] for b in blocks[-1]["elements"]} == {token}


@pytest.mark.asyncio
async def test_the_buttons_sit_under_what_they_approve(gateway):
    await _open(gateway)
    [(blocks, _text)] = gateway.slack_client.blocks
    assert [b["type"] for b in blocks] == ["section", "section", "actions"]
    assert blocks[1]["text"]["text"] == "```\ngit push origin release\n```"


@pytest.mark.asyncio
async def test_what_they_approve_cannot_close_its_fence_or_render_markup(gateway):
    # Untrusted input that closes the fence and adds a disguised link or a ping
    # must stay literal text inside the one code block above Approve.
    sneaky = "ls ```\n<https://example.org/real-args|read-only status check> <!channel> *bold*"
    await _open(gateway, approval_detail=sneaky)
    [(blocks, _text)] = gateway.slack_client.blocks
    shown = blocks[1]["text"]["text"]
    assert shown.startswith("```\n") and shown.endswith("\n```")
    assert shown.count("```") == 2, "no fence closes inside the detail"
    assert "<https" not in shown and "<!channel>" not in shown
    assert "&lt;https://example.org/real-args|read-only status check&gt;" in shown


@pytest.mark.asyncio
async def test_what_they_approve_is_redacted_and_cut_like_the_native_card(gateway):
    secret = "AKIAIOSFODNN7EXAMPLE"
    await _open(gateway, approval_detail=f"aws s3 ls --key {secret}\n" * 200)
    [(blocks, _text)] = gateway.slack_client.blocks
    shown = blocks[1]["text"]["text"]
    assert secret not in shown
    assert len(shown) <= 2900 + len("```\n\n```")
    assert shown.endswith("… [truncated]\n```")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"answerable": False}, {"approval_detail": ""}, {"approval_detail": " \n "}],
    ids=["not-answerable", "nothing-to-show", "only-blank"],
)
async def test_no_buttons_without_a_plain_answer_or_what_it_approves(gateway, overrides):
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(gateway, decision, **{**_PROMPT, **overrides})
    assert task is not None
    await task
    [note] = gateway.bridged
    assert note_mod.ACTION_TOKEN_KEY not in note
    assert note_mod.APPROVAL_DETAIL_KEY not in note
    assert gateway.bridge_actions._bindings == {}
    await SlackBridgeSink(gateway).deliver(redact_note(note))
    assert gateway.slack_client.blocks == [], "the note is posted as plain text"
    assert len(gateway.slack_client.texts) == 1


@pytest.mark.asyncio
async def test_a_strangers_press_changes_nothing_and_is_audited(gateway, audits):
    decision, _note = await _open(gateway)
    await _press(_button(gateway, "approve"), user_id="U-OTHER")
    assert not decision.done()
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.NOT_OWNER and kw["actor"] == "U-OTHER"
    gateway.slack_client.post_ephemeral.assert_not_awaited()
    # The stranger did not burn the owner's answer.
    await _press(_button(gateway, "deny"))
    assert decision.result() == "rejected"


@pytest.mark.asyncio
async def test_a_second_press_is_a_no_op_with_a_private_notice(gateway, audits):
    decision, _note = await _open(gateway)
    await _press(_button(gateway, "approve"))
    await _press(_button(gateway, "deny"))
    assert decision.result() == "approved"
    assert [outcome for outcome, _ in audits] == [
        ActionOutcome.ADMITTED,
        ActionOutcome.ALREADY_ANSWERED,
    ]
    gateway.slack_client.post_ephemeral.assert_awaited_once()
    assert gateway.slack_client.post_ephemeral.await_args.args == (
        _DM,
        _OWNER,
        BRIDGE_ACTION_STALE_NOTICE,
    )


@pytest.mark.asyncio
async def test_a_press_after_the_prompts_deadline_answers_nothing(gateway, audits, clock):
    decision, _note = await _open(gateway)
    clock.now += _WINDOW_S + 1
    await _press(_button(gateway, "approve"))
    assert not decision.done()
    assert [outcome for outcome, _ in audits] == [ActionOutcome.EXPIRED]


@pytest.mark.asyncio
async def test_with_the_preview_off_no_buttons_are_posted_or_bound(gateway, preview):
    preview["on"] = False
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(gateway, decision, **_PROMPT)
    assert task is not None
    await task
    assert gateway.bridged == []
    assert gateway.bridge_actions._bindings == {}


@pytest.mark.asyncio
async def test_turning_the_preview_off_stops_a_press_on_a_posted_button(gateway, audits, preview):
    decision, _note = await _open(gateway)
    preview["on"] = False
    await _press(_button(gateway, "approve"))
    assert not decision.done()
    assert audits == []


@pytest.mark.asyncio
async def test_a_card_already_in_a_slack_thread_gets_no_second_slack_copy(gateway):
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(gateway, decision, slack_card=True, **_PROMPT)
    assert task is not None
    await task
    [note] = gateway.bridged
    # Still bound, so a reply transport can answer the same decision; Slack itself
    # posts nothing, since the linked card is the one to answer there.
    assert note_mod.ACTION_TOKEN_KEY in note
    with pytest.raises(BridgeTransportUnavailable):
        await SlackBridgeSink(gateway).deliver(redact_note(note))
    assert gateway.slack_client.blocks == [] and gateway.slack_client.texts == []


@pytest.mark.asyncio
async def test_a_gateway_with_no_owner_binds_nothing(gateway):
    gateway.owner_id = ""
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(gateway, decision, **_PROMPT)
    assert task is not None
    await task
    [note] = gateway.bridged
    assert note_mod.ACTION_TOKEN_KEY not in note
    assert gateway.bridge_actions._bindings == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        {"action_id": f"{SLACK_ACTION_PREFIX}approve"},
        {"action_id": f"{SLACK_ACTION_PREFIX}approve", "value": ["not", "a", "token"]},
        {"action_id": f"{SLACK_ACTION_PREFIX}approve", "value": ""},
    ],
    ids=["no-value", "non-string", "empty"],
)
async def test_a_press_that_does_not_parse_answers_nothing(gateway, audits, action):
    decision, _note = await _open(gateway)
    payload = {"user": {"id": _OWNER}, "message": {"ts": _POSTED_TS}, "actions": [action]}
    await interactions._handle_bridge_action(payload, action, action["action_id"], _DM, _OWNER)
    assert not decision.done()
    assert audits == []


@pytest.mark.asyncio
async def test_a_press_with_no_user_answers_nothing(gateway, audits):
    decision, _note = await _open(gateway)
    button = _button(gateway, "approve")
    action = {"action_id": button["action_id"], "value": button["value"]}
    await interactions._handle_bridge_action({}, action, button["action_id"], _DM, "")
    assert not decision.done()
    assert audits == []


@pytest.mark.asyncio
async def test_a_press_on_a_gateway_with_no_registry_answers_nothing(gateway, audits, monkeypatch):
    decision, _note = await _open(gateway)
    button = _button(gateway, "approve")
    monkeypatch.setattr(
        "kiro_crew.slack.interactions._orch",
        SimpleNamespace(slack=gateway.slack_client, dashboard_state=SimpleNamespace()),
    )
    await _press(button)
    assert not decision.done()
    assert audits == []


@pytest.mark.asyncio
async def test_a_channels_policy_denying_slack_stops_the_press(gateway, audits, monkeypatch):
    decision, _note = await _open(gateway)
    monkeypatch.setattr(
        "kiro_crew.slack.interactions.channel_inbound_permitted", AsyncMock(return_value=False)
    )
    await _press(_button(gateway, "approve"))
    assert not decision.done()
    assert audits == []


@pytest.mark.asyncio
async def test_dispatch_routes_a_bridge_press_away_from_the_native_approval_path(
    gateway, monkeypatch
):
    routed = AsyncMock()
    native = AsyncMock()
    monkeypatch.setattr("kiro_crew.slack.interactions._handle_bridge_action", routed)
    monkeypatch.setattr("kiro_crew.slack.interactions._handle_tool_approval", native)
    await _press({"action_id": f"{SLACK_ACTION_PREFIX}approve", "value": "tok"})
    routed.assert_awaited_once()
    native.assert_not_awaited()


def test_the_buttons_offer_approve_and_deny_only():
    from kiro_crew.notifications.bridge_slack import action_blocks

    blocks = action_blocks("note", "tok", "ls")
    choices = {b["action_id"].removeprefix(SLACK_ACTION_PREFIX) for b in blocks[-1]["elements"]}
    assert choices == set(actions_mod.BRIDGED_CHOICES)
    assert set(note_mod.DECISION_VALUES) == set(actions_mod.BRIDGED_CHOICES)
    # A part too long for a section still gets what it approves and its buttons.
    assert [b["type"] for b in action_blocks("x" * 3001, "tok", "ls")] == ["section", "actions"]
