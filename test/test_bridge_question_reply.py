"""Answer an agent's ask_question card by replying its number (RFC notification-bridge-actions, B4g).

Drives the real path end to end on a real ``DashboardState`` with no Slack: the
agent-ask card opens, its hand-off binds the card for the reply transport's owner DM
and publishes the note, the real bridge dispatcher routes it to the real Telegram,
Discord or Webex transport (only its client is faked), and the owner's reply is read
by that transport's reader. Pins that:

* a card with one single-select question shows its options numbered from 1, inside
  the code fence untrusted text cannot leave, and asks for the number in reply;
* the owner's ``1`` / ``2`` answers the card exactly as the dashboard would for that
  option, on every reply transport;
* a number the card does not offer, ``0``, a word or ``approve`` answers nothing and
  is told to reply with a number; a second reply is stale; a stranger is told nothing;
* a card one reply cannot answer (several questions, multi-select) gets no prompt;
* on Webex, where the note's thread stays open, a word in a question's thread is the
  ordinary conversation, not an answer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)

from kiro_crew.discord.bridge_reply import consume_bridged_reply as discord_reply
from kiro_crew.discord.transport import DiscordInboundMessage, DiscordTransport
from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as approval_mod
from kiro_crew.notifications import bridge_question_note as note_mod
from kiro_crew.notifications.bridge_actions import ActionOutcome
from kiro_crew.telegram.dispatch.bridge_reply import consume_bridged_reply as telegram_reply
from kiro_crew.telegram.transport import TelegramInboundMessage, TelegramTransport
from kiro_crew.webex.bridge_reply import consume_bridged_reply as webex_reply
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport import WebexTransport

_TG_OWNER = "123"
_TG_POSTED = 7
_DC_OWNER = "111111111111111111"
_DC_DM = f"dm-{_DC_OWNER}"
_WX_OWNER = "owner@example.com"
_WX_POSTED = "Y2lzY29zcGFyazovL3VzL01FU1NBR0UvNw"
_ASK = "a" * 32
_SK = "dashboard:chat-1"
_ONE = [
    {
        "question": "Which branch should I ship?",
        "header": "Branch",
        "options": [
            {"label": "main", "description": ""},
            {"label": "release ``` [x](https://example.org) **now**", "description": ""},
        ],
        "multiSelect": False,
    }
]
#: The card's whole window, on the fake clock; nothing sleeps.
_WINDOW = {"ttl_s": 1800.0}  # flake-ok: a deadline on the fake clock, nothing waits


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Sel:
    def __getattr__(self, name: str) -> Any:
        return lambda **kw: None


class _DiscordRest:
    def __init__(self) -> None:
        self.pairs: dict[str, str] = {}
        self.sent: list[str] = []

    async def create_dm_channel(self, user_id: str) -> str:
        self.pairs[f"dm-{user_id}"] = user_id
        return f"dm-{user_id}"

    def cached_dm_recipient(self, channel_id: str) -> str | None:
        return self.pairs.get(channel_id)

    def remember_dm_recipient(self, channel_id: str, user_id: str) -> None:
        self.pairs[channel_id] = user_id

    async def send_message(self, channel_id: str, text: str, **_kw: Any) -> str:
        self.sent.append(text)
        return "7"


class _WebexRest:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, conversation_id: str, markdown: str, **_kw: Any) -> str:
        self.sent.append(markdown)
        return _WX_POSTED


@pytest.fixture()
def audits(monkeypatch) -> list[tuple[ActionOutcome, dict[str, Any]]]:
    seen: list[tuple[ActionOutcome, dict[str, Any]]] = []
    monkeypatch.setattr(
        "kiro_crew.notifications.bridge_action_audit.audit_action",
        lambda registry, token, outcome, **kw: seen.append((outcome, kw)),
    )
    return seen


def _sent(transport: str, rest: Any) -> list[str]:
    if transport == "telegram":
        return [" ".join(str(a) for a in call.args) for call in rest.await_args_list]
    return list(rest.sent)


@pytest.fixture(params=["telegram", "discord", "webex"])
def gateway(request, tmp_path, monkeypatch):
    """A gateway whose only owner is one reply transport's DM, with no Slack."""
    monkeypatch.setattr(actions_mod, "bridge_clock", _Clock())
    monkeypatch.setattr(approval_mod, "enabled", lambda cfg: True)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    state.owner_id = ""
    state.get_or_create_slot("chat-1")
    monkeypatch.setattr(state, "deliver_ws_owners", AsyncMock(return_value=1))
    transport = request.param
    if transport == "telegram":
        client = MagicMock()
        client.send_message = AsyncMock(return_value=_TG_POSTED)
        live: Any = TelegramTransport(client, allowed_user_ids=[int(_TG_OWNER)])
        rest: Any = client.send_message
    elif transport == "discord":
        rest = _DiscordRest()
        live = DiscordTransport(rest, allowed_user_ids=[_DC_OWNER])  # type: ignore[arg-type]
    else:
        rest = _WebexRest()
        live = WebexTransport(rest, allowed_emails=[_WX_OWNER])  # type: ignore[arg-type]
    state.register_channel_transport(live)
    state.notification_channel_settings.update("system.approval", deliver_to=[transport])
    return SimpleNamespace(state=state, transport=transport, rest=rest)


async def _open(gw: Any, questions: list[dict] = _ONE) -> asyncio.Future[Any]:
    """Open the card the way the agent does, and deliver its note to the transport."""
    state = gw.state
    assert await state.open_agent_ask(_ASK, "chat-1", _SK, questions) == 1
    future = state._question_futures[_ASK]
    task = note_mod.schedule_pending_question(
        state,
        ask_id=_ASK,
        slot_key="chat-1",
        session_key=_SK,
        questions=state._agent_asks[_ASK]["questions"],
        session_title="Release notes",
        **_WINDOW,
    )
    assert task is not None
    await task
    await state.notification_bridge.drain()
    return future


class _Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _answer(gw: Any, text: str, *, stranger: bool = False) -> tuple[bool, list[str]]:
    notices = _Notices()
    if gw.transport == "telegram":
        sender = "456" if stranger else _TG_OWNER
        msg: Any = TelegramInboundMessage(
            channel_type="telegram",
            user_id=sender,
            conversation_id=_TG_OWNER,
            text=text,
            chat_type="private",
            reply_to_message_id=_TG_POSTED,
        )
        taken = await telegram_reply(gw.state, msg, notices)
    elif gw.transport == "discord":
        msg = DiscordInboundMessage(
            channel_type="discord",
            user_id="333" if stranger else _DC_OWNER,
            conversation_id=_DC_DM,
            text=text,
            reply_to_message_id="7",
        )
        taken = await discord_reply(gw.state, msg, notices)
    else:
        msg = WebexInbound(
            person_email="stranger@example.com" if stranger else _WX_OWNER,
            room_id="ROOM",
            text=text,
            room_type="direct",
            parent_id=_WX_POSTED,
        )
        taken = await webex_reply(gw.state, msg, notices)
    return taken, notices.sent


@pytest.mark.asyncio
async def test_the_note_numbers_the_options_inside_the_fence_and_asks_for_one(gateway):
    await _open(gateway)
    [text] = _sent(gateway.transport, gateway.rest)
    assert "Which branch should I ship?" in text
    assert "Reply with the number of your answer to this message." in text
    assert "1. main · 2. release" in text
    if gateway.transport != "telegram":  # Telegram's send is the rendered HTML.
        fence = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
        assert fence.startswith("1. main · 2. release")
        assert "```" not in fence, "an agent-authored label cannot close the fence"
    [token] = set(gateway.state.bridge_actions._messages.values())
    assert token.rsplit("|", 1)[-1] not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
async def test_the_owners_number_answers_the_card_as_the_dashboard_would(gateway, audits, number):
    future = await _open(gateway)
    labels = [o["label"] for o in _ONE[0]["options"]]
    taken, notices = await _answer(gateway, f" {number} ")
    assert taken
    assert future.result() == {_ONE[0]["question"]: labels[number - 1]}
    assert gateway.state.recent_question_resolutions()[_ASK] == "answered"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED and kw["choice"] == f"option_{number - 1}"
    assert notices == ["Answered."]


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["3", "0", "01", "²", "١"])
async def test_a_number_the_card_does_not_offer_answers_nothing(gateway, audits, word):
    future = await _open(gateway)
    taken, notices = await _answer(gateway, word)
    assert not future.done()
    if gateway.transport == "webex" and not (word.isascii() and word.isdigit()):
        assert not taken, "not a number: the thread's ordinary conversation"
        return
    assert taken
    assert [outcome for outcome, _ in audits] == [ActionOutcome.INVALID_CHOICE]
    assert notices == ["Reply with the number of an answer to answer this question."]


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["approve", "main", "yes please"])
async def test_a_word_answers_no_question(gateway, audits, word):
    future = await _open(gateway)
    taken, notices = await _answer(gateway, word)
    assert not future.done()
    if gateway.transport == "webex":
        assert not taken and audits == [], "the thread's ordinary conversation"
        return
    assert taken
    assert [outcome for outcome, _ in audits] == [ActionOutcome.INVALID_CHOICE]
    assert notices == ["Reply with the number of an answer to answer this question."]


@pytest.mark.asyncio
async def test_a_second_reply_is_stale(gateway, audits):
    future = await _open(gateway)
    await _answer(gateway, "1")
    taken, notices = await _answer(gateway, "2")
    assert taken and future.result() == {_ONE[0]["question"]: "main"}
    assert [outcome for outcome, _ in audits] == [
        ActionOutcome.ADMITTED,
        ActionOutcome.ALREADY_ANSWERED,
    ]
    assert notices == ["This request was already answered or has expired."]


@pytest.mark.asyncio
async def test_a_stranger_changes_nothing_and_is_told_nothing(gateway, audits):
    future = await _open(gateway)
    taken, notices = await _answer(gateway, "1", stranger=True)
    assert not future.done() and notices == []
    if gateway.transport == "webex":
        # The note is linked under the owner's email: another person's reply names
        # no linked message at all.
        assert not taken and audits == []
        return
    assert taken
    assert [outcome for outcome, _ in audits] == [ActionOutcome.NOT_OWNER]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [["main", "x" * 1200], ["main · 2. delete prod", "release"]],
    ids=["an-option-that-cannot-show-whole", "a-label-carrying-the-separator"],
)
async def test_options_the_owner_cannot_read_whole_get_no_prompt(gateway, labels):
    card = [{**_ONE[0], "options": [{"label": label, "description": ""} for label in labels]}]
    await _open(gateway, card)
    assert "Reply" not in " ".join(_sent(gateway.transport, gateway.rest))
    assert gateway.state.bridge_actions._messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "questions",
    [
        [_ONE[0], {**_ONE[0], "question": "And which tag?"}],
        [{**_ONE[0], "multiSelect": True}],
    ],
    ids=["two-questions", "multi-select"],
)
async def test_a_card_one_reply_cannot_answer_gets_no_prompt(gateway, questions):
    await _open(gateway, questions)
    assert "Reply" not in " ".join(_sent(gateway.transport, gateway.rest))
    assert gateway.state.bridge_actions._messages == {}
