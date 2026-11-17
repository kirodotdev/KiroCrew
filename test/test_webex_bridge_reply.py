"""Answer a bridged tool approval by replying on Webex (RFC notification-bridge-actions, B4g).

Drives the real path end to end: the prompt's hand-off binds the open approval and
publishes the note, the real dispatcher routes it to the real Webex transport (only
its REST client is faked) through the proactive owner-DM leg, which links the posted
message by ``<email>:<message id>`` (Webex addresses a DM by the person's email),
and an inbound reply is read by ``webex.bridge_reply``. Pins the rules
B4d pinned for Telegram:

* only a reply in the note's thread (``parentId``), in a direct room, under the
  owner's email, is taken; a space reply, another person's reply or a reply under
  any other message goes on to the ordinary turn path untouched;
* the owner is the sender's ``personEmail`` against the one configured owner DM;
* only an exact ``approve`` / ``deny`` is an answer, this one request only; the
  thread stays open, so every other message in it, ``trust`` included, is left to
  the ordinary turn path;
* a second, settled-elsewhere or expired reply changes nothing and is audited; with
  the preview off nothing is answered.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)
from webex_dispatch_helpers import FakeCtx, FakeProvider, FakeSessions, _dispatcher, _spin

from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge_actions import ActionOutcome
from kiro_crew.webex import transport_dispatch as webex_dispatch
from kiro_crew.webex.bridge_reply import consume_bridged_reply
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport import WebexTransport

_OWNER = "owner@example.com"
_ROOM = "Y2lzY29zcGFyazovL3VzL1JPT00vZG0"
_POSTED_ID = "Y2lzY29zcGFyazovL3VzL01FU1NBR0UvNw"

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


class _Rest:
    """The Webex REST client: posts to a person by email and returns the message id."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.next_id = _POSTED_ID

    async def send_message(self, conversation_id: str, markdown: str, **_kw: Any) -> str:
        self.sent.append((conversation_id, markdown))
        return self.next_id

    async def head_content(self, *_a: Any, **_kw: Any) -> tuple[str, str, int]:
        """An unknown file, as the real client reports a failed HEAD."""
        return "", "", 0


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
def webex() -> tuple[WebexTransport, _Rest]:
    rest = _Rest()
    return WebexTransport(rest, allowed_emails=[_OWNER]), rest  # type: ignore[arg-type]


@pytest.fixture()
def gateway(tmp_path, monkeypatch, clock, preview, webex):
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    state.owner_id = ""  # No Slack: the Webex owner DM is the only owner.
    state.get_or_create_slot("chat-1")  # The session the approval note speaks for.
    state.register_channel_transport(webex[0])
    state.notification_channel_settings.update("system.approval", deliver_to=["webex"])
    return state


async def _open(state: Any, **overrides: Any) -> asyncio.Future[Any]:
    """Open one approval the way the prompt does, and deliver its note to Webex."""
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
    room_type: str = "direct",
    parent: str = _POSTED_ID,
    files: tuple[str, ...] = (),
) -> WebexInbound:
    return WebexInbound(
        person_email=sender,
        room_id=_ROOM,
        text=text,
        room_type=room_type,
        message_id="Y2lzY29zcGFyazovL3VzL01FU1NBR0UvOQ",
        parent_id=parent,
        file_urls=files,
    )


class _Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _answer(state: Any, msg: WebexInbound) -> tuple[bool, list[str]]:
    notices = _Notices()
    taken = await consume_bridged_reply(state, msg, notices)
    return taken, notices.sent


def _posted_text(rest: _Rest) -> str:
    return " ".join(text for _conversation, text in rest.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word", "value", "notice"),
    [("approve", "approved", "Approved."), (" Deny ", "rejected", "Denied.")],
)
async def test_the_owners_reply_answers_this_one_request(
    gateway, webex, audits, word, value, notice
):
    decision = await _open(gateway)
    [(conversation, text)] = webex[1].sent
    assert conversation == _OWNER, "posted to the owner's own DM, addressed by email"
    assert "git status --short" in text, "the reply prompt shows what it approves"
    assert "approve" in text and "deny" in text
    taken, notices = await _answer(gateway, _reply(word))
    assert taken
    assert decision.result() == value, "the value the dashboard's own button sets"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED
    assert (kw["transport"], kw["actor"], kw["channel"]) == ("webex", _OWNER, _OWNER)
    assert notices == [notice]


@pytest.mark.asyncio
async def test_the_senders_email_is_matched_whatever_its_case(gateway, audits):
    decision = await _open(gateway)
    taken, _notices = await _answer(gateway, _reply("approve", sender="Owner@Example.COM"))
    assert taken and decision.result() == "approved"


@pytest.mark.asyncio
async def test_the_token_is_shown_in_no_text(gateway, webex):
    await _open(gateway)
    [token] = set(gateway.bridge_actions._messages.values())
    nonce = token.rsplit("|", 1)[-1]
    assert nonce and nonce not in _posted_text(webex[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        _reply("approve", room_type="group"),
        _reply("approve", room_type=""),
        _reply("approve", sender="stranger@example.com"),
        _reply("approve", parent="Y2lzY29zcGFyazovL3VzL01FU1NBR0UvOA"),
        _reply("approve", parent=""),
    ],
    ids=["space", "unknown-room-type", "other-person", "non-linked-message", "not-a-reply"],
)
async def test_only_a_reply_to_the_linked_message_in_the_owners_dm_is_taken(gateway, audits, msg):
    decision = await _open(gateway)
    taken, notices = await _answer(gateway, msg)
    assert not taken, "left to the ordinary turn path"
    assert not decision.done()
    assert audits == [] and notices == []


@pytest.mark.asyncio
async def test_an_owner_dropped_from_the_allow_list_can_no_longer_answer(gateway, webex, audits):
    decision = await _open(gateway)
    # Two DM targets: no single owner can be named.
    webex[0]._allowed = frozenset({_OWNER, "second@example.com"})
    taken, notices = await _answer(gateway, _reply("approve"))
    assert taken and not decision.done()
    assert [outcome for outcome, _ in audits] == [ActionOutcome.NOT_OWNER]
    assert notices == [], "an unbound sender is told nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "word", ["trust", "always", "3", "yes", "approve please", "", "✅", "/new", "/help"]
)
async def test_any_other_word_in_the_thread_is_left_to_the_turn_path(gateway, audits, word):
    decision = await _open(gateway)
    taken, notices = await _answer(gateway, _reply(word))
    assert not taken, "the thread stays open: only an answer word is an answer"
    assert not decision.done(), "no answer, and never a session-wide grant"
    assert audits == [] and notices == []


@pytest.mark.asyncio
async def test_the_conversation_goes_on_in_the_thread_after_the_answer(gateway, audits):
    decision = await _open(gateway)
    await _answer(gateway, _reply("approve"))
    taken, notices = await _answer(gateway, _reply("thanks, now also run the tests"))
    assert decision.result() == "approved"
    assert not taken and notices == []
    assert [outcome for outcome, _ in audits] == [ActionOutcome.ADMITTED]


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
async def test_a_reply_after_the_prompts_deadline_answers_nothing(gateway, webex, audits, clock):
    decision = await _open(gateway)
    clock.now += _WINDOW_S + 1
    # Another binding, posted as its own message, prunes the expired one; the
    # expired note's link still names it.
    webex[1].next_id = "Y2lzY29zcGFyazovL3VzL01FU1NBR0UvOA"
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
async def test_with_the_preview_off_nothing_is_posted_or_linked(gateway, webex, preview):
    preview["on"] = False
    await _open(gateway)
    assert webex[1].sent == []
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_the_reply_note_is_one_webex_message_however_wide_its_detail(gateway, webex):
    # Four-byte characters: the cut must count bytes, not characters.
    await _open(gateway, approval_detail="𝔁" * 5000)
    [(_conversation, text)] = webex[1].sent
    assert "Reply" in text and len(text.encode("utf-8")) <= 7000
    assert gateway.bridge_actions._messages


@pytest.mark.asyncio
async def test_no_reply_prompt_when_the_note_cannot_fit_one_webex_message(gateway, webex):
    await _open(gateway, session_title="t" * 6500)
    assert "Reply" not in _posted_text(webex[1])
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_untrusted_input_stays_inside_the_code_fence(gateway, webex):
    await _open(gateway, approval_detail="ls ```\n[ok](https://example.org/x) **bold**")
    [(_conversation, text)] = webex[1].sent
    fence = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    assert "```" not in fence, "a backtick run in the input cannot close the fence"
    assert "[ok](https://example.org/x)" in fence


@pytest.mark.asyncio
async def test_the_dispatcher_takes_the_reply_before_any_turn(gateway, webex):
    decision = await _open(gateway)
    sess = FakeSessions(FakeProvider([]))
    d = _dispatcher(sess, FakeCtx(), webex[1])
    # The wiring the gateway does: the transport dispatches into the dispatcher's
    # own bound method, so registering the transport hands the dispatcher the state.
    gateway.register_channel_transport(
        WebexTransport(webex[1], allowed_emails=[_OWNER], dispatch=d.handle_message)
    )
    assert d.dashboard_state is gateway
    await d.handle_message(_reply("approve"))
    assert decision.result() == "approved"
    assert sess.acquired == [], "a bridged reply starts no turn"
    assert any("Approved." in text for _conversation, text in webex[1].sent)


@pytest.mark.asyncio
async def test_a_reply_carrying_a_file_stays_a_turn(gateway, webex):
    decision = await _open(gateway)
    sess = FakeSessions(FakeProvider([]))
    d = _dispatcher(sess, FakeCtx(), webex[1])
    d.dashboard_state = gateway
    await d.handle_message(_reply("approve", files=("https://webexapis.com/v1/contents/1",)))
    assert not decision.done(), "a file is content for a turn, not an answer"


@pytest.mark.asyncio
async def test_a_bridged_reply_never_answers_a_native_prompt_in_the_same_dm(gateway, webex):
    decision = await _open(gateway)
    sess = FakeSessions(FakeProvider([]))
    d = _dispatcher(sess, FakeCtx(), webex[1])
    d.dashboard_state = gateway
    msg = _reply("approve")
    key = d._approval_key(webex_dispatch._route_of(msg))
    webex_dispatch._APPROVALS.reserve(key, 1)
    native = asyncio.create_task(
        webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
    )
    try:
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(msg)
        assert decision.result() == "approved", "the reply answers the note it replies to"
        assert webex_dispatch._APPROVALS.has_pending(key), "the native prompt still waits"
        await d.handle_message(_reply("approve", parent=""))
        assert (await native)[0] is True, "a plain reply still answers it"
    finally:
        native.cancel()
