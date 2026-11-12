"""Answer a bridged tool approval by replying on Telegram (RFC notification-bridge-actions, B4d).

Drives the real path end to end: the prompt's hand-off binds the open approval and
publishes the note, the real dispatcher routes it to the real Telegram transport
(only its HTTP client is faked) through the proactive owner-DM leg, which links the
posted message by ``<chat id>:<message id>``, and an inbound reply is read by
``dispatch.bridge_reply``. Pins the rules a chat reply answering a tool approval
must keep:

* only a reply, in the owner's own private chat, to a linked message is taken;
  a group reply, another chat's reply or a reply to any other message goes on
  to the ordinary turn path untouched;
* the owner is the sender's ``from.id`` against the one configured owner DM,
  never a username;
* only ``approve`` / ``deny`` answer, this one request only; ``trust`` and every
  other word is refused;
* a stranger's, second, settled-elsewhere or expired reply changes nothing and is
  audited; with the preview off nothing is answered.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)
from telegram_helpers import _dispatcher

from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge_actions import ActionOutcome
from kiro_crew.telegram.client import TelegramClient
from kiro_crew.telegram.dispatch.bridge_reply import consume_bridged_reply
from kiro_crew.telegram.transport import TelegramInboundMessage, TelegramTransport

_OWNER = "123"
_POSTED_ID = 7

#: The prompt's wait window, on the fake clock; nothing sleeps.
_WINDOW_S = 600.0
_PROMPT = {
    "slot_key": "chat-1",
    "session_key": "dashboard:chat-1",
    "request_id": "7",
    "tool_title": "execute_bash",
    "session_title": "Release notes",
    "approval_detail": "git status --short",
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


class _Sel:
    def __getattr__(self, name: str) -> Any:
        return lambda **kw: None


@pytest.fixture()
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(actions_mod, "bridge_clock", fake)
    return fake


@pytest.fixture()
def preview(monkeypatch) -> dict[str, bool]:
    """The live ``bridge_approvals`` preview, as the hand-off and the reply read it."""
    live = {"on": True}
    monkeypatch.setattr(note_mod, "enabled", lambda cfg: live["on"])
    return live


@pytest.fixture()
def audits(monkeypatch) -> list[tuple[ActionOutcome, dict[str, Any]]]:
    seen: list[tuple[ActionOutcome, dict[str, Any]]] = []
    monkeypatch.setattr(
        "kiro_crew.notifications.bridge_action_audit.audit_action",
        lambda registry, token, outcome, **kw: seen.append((outcome, kw)),
    )
    return seen


@pytest.fixture()
def telegram() -> tuple[TelegramTransport, AsyncMock]:
    client = MagicMock()
    client.send_message = AsyncMock(return_value=_POSTED_ID)
    return TelegramTransport(client, allowed_user_ids=[int(_OWNER)]), client.send_message


@pytest.fixture()
def gateway(tmp_path, monkeypatch, clock, preview, telegram):
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    state.owner_id = ""  # No Slack: the Telegram owner DM is the only owner.
    state.get_or_create_slot("chat-1")  # The session the approval note speaks for.
    state.register_channel_transport(telegram[0])
    state.notification_channel_settings.update("system.approval", deliver_to=["telegram"])
    return state


async def _open(state: Any, **overrides: Any) -> asyncio.Future[Any]:
    """Open one approval the way the prompt does, and deliver its note to Telegram."""
    decision: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(state, decision, **{**_PROMPT, **overrides})
    assert task is not None
    await task
    await state.notification_bridge.drain()
    return decision


def _reply(
    text: str,
    *,
    sender: str = _OWNER,
    chat: str = _OWNER,
    to: int = _POSTED_ID,
    chat_type: str = "private",
    username: str = "",
) -> TelegramInboundMessage:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id=sender,
        conversation_id=chat,
        text=text,
        chat_type=chat_type,
        reply_to_message_id=to,
        username=username,
    )


class _Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _answer(state: Any, msg: TelegramInboundMessage) -> tuple[bool, list[str]]:
    notices = _Notices()
    taken = await consume_bridged_reply(state, msg, notices)
    return taken, notices.sent


def _posted_text(send: AsyncMock) -> str:
    return " ".join(str(arg) for arg in send.await_args.args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word", "value", "notice"),
    [("approve", "approved", "Approved."), (" Deny ", "rejected", "Denied.")],
)
async def test_the_owners_reply_answers_this_one_request(
    gateway, telegram, audits, word, value, notice
):
    decision = await _open(gateway)
    text = _posted_text(telegram[1])
    assert "git status --short" in text, "the reply prompt shows what it approves"
    assert "approve" in text and "deny" in text
    taken, notices = await _answer(gateway, _reply(word))
    assert taken
    assert decision.result() == value, "the value the dashboard's own button sets"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED
    assert (kw["transport"], kw["actor"], kw["channel"]) == ("telegram", _OWNER, _OWNER)
    assert notices == [notice]


@pytest.mark.asyncio
async def test_the_token_is_shown_in_no_text(gateway, telegram):
    await _open(gateway)
    [token] = set(gateway.bridge_actions._messages.values())
    nonce = token.rsplit("|", 1)[-1]
    assert nonce and nonce not in _posted_text(telegram[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        _reply("approve", chat_type="group", chat="-100200300"),
        _reply("approve", chat_type="supergroup", chat="-100200300"),
        _reply("approve", sender="456", chat="456"),
        _reply("approve", to=_POSTED_ID + 1),
        _reply("approve", to=0),
    ],
    ids=["group", "supergroup", "other-private-chat", "non-linked-message", "not-a-reply"],
)
async def test_only_a_reply_to_the_linked_message_in_the_owners_dm_is_taken(gateway, audits, msg):
    decision = await _open(gateway)
    taken, notices = await _answer(gateway, msg)
    assert not taken, "left to the ordinary turn path"
    assert not decision.done()
    assert audits == [] and notices == []


@pytest.mark.asyncio
async def test_a_sender_who_is_not_the_owner_changes_nothing_and_is_audited(gateway, audits):
    decision = await _open(gateway)
    # The owner's chat, but a different from.id; a matching username counts for nothing.
    taken, notices = await _answer(gateway, _reply("approve", sender="456", username="owner"))
    assert taken
    assert not decision.done()
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.NOT_OWNER and kw["actor"] == "456"
    assert notices == [], "a stranger is told nothing"


@pytest.mark.asyncio
async def test_an_owner_dropped_from_the_allow_list_can_no_longer_answer(gateway, telegram, audits):
    decision = await _open(gateway)
    # Two DM targets: no single owner can be named.
    telegram[0]._allowed = frozenset({_OWNER, "456"})
    taken, _notices = await _answer(gateway, _reply("approve"))
    assert taken and not decision.done()
    assert [outcome for outcome, _ in audits] == [ActionOutcome.NOT_OWNER]


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["trust", "always", "3", "yes", "approve please", "", "✅"])
async def test_any_other_word_is_refused_and_never_trusts(gateway, audits, word):
    decision = await _open(gateway)
    taken, notices = await _answer(gateway, _reply(word))
    assert taken
    assert not decision.done(), "no answer, and never a session-wide grant"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.INVALID_CHOICE
    assert kw["choice"] in {word, "other"}
    assert notices == ["Reply approve or deny to answer this request."]


@pytest.mark.asyncio
async def test_a_second_reply_is_a_no_op_with_a_private_notice(gateway, audits):
    decision = await _open(gateway)
    await _answer(gateway, _reply("approve"))
    taken, notices = await _answer(gateway, _reply("deny"))
    assert taken and decision.result() == "approved"
    assert [outcome for outcome, _ in audits] == [
        ActionOutcome.ADMITTED,
        ActionOutcome.ALREADY_ANSWERED,
    ]
    assert notices == ["This request was already answered or has expired."]


@pytest.mark.asyncio
async def test_a_request_settled_elsewhere_is_not_changed_by_a_reply(gateway, audits):
    decision = await _open(gateway)
    decision.set_result("rejected")  # The dashboard or a Slack press answered first.
    taken, _notices = await _answer(gateway, _reply("approve"))
    assert taken and decision.result() == "rejected"
    assert [outcome for outcome, _ in audits] == [ActionOutcome.ALREADY_ANSWERED]


@pytest.mark.asyncio
async def test_a_reply_after_the_prompts_deadline_answers_nothing(gateway, telegram, audits, clock):
    decision = await _open(gateway)
    clock.now += _WINDOW_S + 1
    # Another binding, posted as its own message, prunes the expired one; the
    # expired note's link still names it.
    telegram[1].return_value = _POSTED_ID + 1
    await _open(gateway, request_id="8")
    taken, notices = await _answer(gateway, _reply("approve"))
    assert taken and not decision.done()
    assert [outcome for outcome, _ in audits] == [ActionOutcome.EXPIRED]
    assert notices == ["This request was already answered or has expired."]


@pytest.mark.asyncio
async def test_turning_the_preview_off_stops_a_reply_to_a_posted_note(gateway, audits, preview):
    decision = await _open(gateway)
    preview["on"] = False
    taken, notices = await _answer(gateway, _reply("approve"))
    assert taken and not decision.done()
    assert audits == [] and notices == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [{"answerable": False}, {"approval_detail": " "}],
    ids=["not-answerable", "nothing-to-show"],
)
async def test_no_reply_prompt_without_a_plain_answer_or_what_it_approves(
    gateway, telegram, overrides
):
    await _open(gateway, **overrides)
    assert "Reply" not in _posted_text(telegram[1])
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_with_the_preview_off_nothing_is_posted_or_linked(gateway, telegram, preview):
    preview["on"] = False
    await _open(gateway)
    telegram[1].assert_not_awaited()
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_a_card_already_in_a_linked_slack_thread_can_still_be_answered_here(
    gateway, telegram, audits
):
    decision = await _open(gateway, slack_card=True)
    assert "Reply" in _posted_text(telegram[1])
    taken, _notices = await _answer(gateway, _reply("approve"))
    assert taken and decision.result() == "approved"


@pytest.mark.asyncio
async def test_a_credential_across_the_cut_is_scrubbed_before_it(gateway, telegram):
    secret = "AKIAIOSFODNN7EXAMPLE"
    # The key straddles the 900-character cut: cutting first would leave a fragment
    # the credential scan does not recognise.
    await _open(gateway, approval_detail="x" * 890 + f" {secret} tail")
    text = _posted_text(telegram[1])
    assert "AKIAIOSF" not in text
    assert "Reply" in text


@pytest.mark.asyncio
async def test_the_reply_note_is_one_message_however_much_its_detail_escapes(gateway, telegram):
    # 900 ampersands render to 4,500 characters of HTML: the detail shrinks so the
    # note is still ONE message, the one a reply quotes.
    await _open(gateway, approval_detail="&" * 2000)
    telegram[1].assert_awaited_once()
    text = _posted_text(telegram[1])
    assert "Reply" in text and len(text) <= 4096
    assert gateway.bridge_actions._messages


@pytest.mark.asyncio
async def test_no_reply_prompt_when_the_note_cannot_fit_one_message(gateway, telegram):
    await _open(gateway, session_title="&" * 4000)
    assert "Reply" not in _posted_text(telegram[1])
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_untrusted_input_stays_literal_in_the_reply_prompt(gateway, telegram):
    sneaky = "ls ```\n[read-only status check](https://example.org/real-args) **bold** <b>x</b>"
    await _open(gateway, approval_detail=sneaky)
    text = _posted_text(telegram[1])
    assert "<a " not in text and "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "[read-only status check](https://example.org/real-args)" in text


@pytest.mark.asyncio
async def test_the_dispatcher_takes_the_reply_before_any_turn(gateway):
    decision = await _open(gateway)
    d, cli, sess = _dispatcher({int(_OWNER)})
    d.dashboard_state = gateway
    await d.handle_message(_reply("approve"))
    assert decision.result() == "approved"
    assert sess.acquired == [], "a bridged reply starts no turn"
    assert any("Approved." in str(text) for text, _ in cli.sent)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"message_id": 55}, 55),
        ({"message_id": True}, 0),
        ({"message_id": "55"}, 0),
        ({"message_id": -1}, 0),
        (None, 0),
        ("junk", 0),
    ],
)
def test_the_reply_target_is_read_defensively(reply, expected):
    envelope: dict[str, Any] = {
        "message_id": 9,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 123},
        "text": "approve",
    }
    if reply is not None:
        envelope["reply_to_message"] = reply
    assert TelegramClient._build_inbound(envelope).reply_to_message_id == expected
