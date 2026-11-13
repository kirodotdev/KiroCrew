"""Answer a bridged tool approval by replying on Discord (RFC notification-bridge-actions, B4g).

Drives the real path end to end: the prompt's hand-off binds the open approval and
publishes the note, the real dispatcher routes it to the real Discord transport
(only its REST client is faked) through the proactive owner-DM leg, which links the
posted message by ``<channel id>:<message id>``, and an inbound reply is read by
``discord.bridge_reply``. Pins the rules B4d pinned for Telegram:

* only a reply, outside any thread, to a linked message is taken; a thread reply,
  another channel's reply or a reply to any other message goes on to the ordinary
  turn path untouched;
* the owner is the sender's ``author.id`` against the one configured owner DM, and
  the channel must be that owner's own DM by the transport's DM pairing;
* only ``approve`` / ``deny`` answer, this one request only; ``trust`` and every
  other word is refused;
* a stranger's, second, settled-elsewhere or expired reply changes nothing and is
  audited; with the preview off nothing is answered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from discord_helpers import _dispatcher
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)

from kiro_crew.discord.bridge_reply import consume_bridged_reply
from kiro_crew.discord.client_gateway import _reply_message_id
from kiro_crew.discord.transport import DiscordInboundMessage, DiscordTransport
from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge_actions import ActionOutcome

_OWNER = "111111111111111111"
_DM = f"dm-{_OWNER}"
_POSTED_ID = "7"

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
    """The Discord REST client: opens DMs (recording the pairing) and posts."""

    def __init__(self) -> None:
        self.pairs: dict[str, str] = {}
        self.sent: list[tuple[str, str]] = []
        self.next_id = _POSTED_ID

    async def create_dm_channel(self, user_id: str) -> str:
        self.pairs[f"dm-{user_id}"] = user_id
        return f"dm-{user_id}"

    def cached_dm_recipient(self, channel_id: str) -> str | None:
        return self.pairs.get(channel_id)

    def remember_dm_recipient(self, channel_id: str, user_id: str) -> None:
        self.pairs[channel_id] = user_id

    async def send_message(self, channel_id: str, text: str, **_kw: Any) -> str:
        self.sent.append((channel_id, text))
        return self.next_id


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
def discord() -> tuple[DiscordTransport, _Rest]:
    rest = _Rest()
    return DiscordTransport(rest, allowed_user_ids=[_OWNER]), rest  # type: ignore[arg-type]


@pytest.fixture()
def gateway(tmp_path, monkeypatch, clock, preview, discord):
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    state.owner_id = ""  # No Slack: the Discord owner DM is the only owner.
    state.get_or_create_slot("chat-1")  # The session the approval note speaks for.
    state.register_channel_transport(discord[0])
    state.notification_channel_settings.update("system.approval", deliver_to=["discord"])
    return state


async def _open(state: Any, **overrides: Any) -> asyncio.Future[Any]:
    """Open one approval the way the prompt does, and deliver its note to Discord."""
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
    channel: str = _DM,
    to: str = _POSTED_ID,
    thread: str | None = None,
) -> DiscordInboundMessage:
    return DiscordInboundMessage(
        channel_type="discord",
        user_id=sender,
        conversation_id=channel,
        text=text,
        thread_id=thread,
        reply_to_message_id=to,
    )


class _Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def _answer(state: Any, msg: DiscordInboundMessage) -> tuple[bool, list[str]]:
    notices = _Notices()
    taken = await consume_bridged_reply(state, msg, notices)
    return taken, notices.sent


def _posted_text(rest: _Rest) -> str:
    return " ".join(text for _channel, text in rest.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word", "value", "notice"),
    [("approve", "approved", "Approved."), (" Deny ", "rejected", "Denied.")],
)
async def test_the_owners_reply_answers_this_one_request(
    gateway, discord, audits, word, value, notice
):
    decision = await _open(gateway)
    [(channel, text)] = discord[1].sent
    assert channel == _DM, "posted to the owner's own DM"
    assert "git status --short" in text, "the reply prompt shows what it approves"
    assert "approve" in text and "deny" in text
    taken, notices = await _answer(gateway, _reply(word))
    assert taken
    assert decision.result() == value, "the value the dashboard's own button sets"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED
    assert (kw["transport"], kw["actor"], kw["channel"]) == ("discord", _OWNER, _DM)
    assert notices == [notice]


@pytest.mark.asyncio
async def test_the_token_is_shown_in_no_text(gateway, discord):
    await _open(gateway)
    [token] = set(gateway.bridge_actions._messages.values())
    nonce = token.rsplit("|", 1)[-1]
    assert nonce and nonce not in _posted_text(discord[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        _reply("approve", thread="222"),
        _reply("approve", channel="dm-333", sender="333"),
        _reply("approve", to="8"),
        _reply("approve", to=""),
    ],
    ids=["thread", "other-channel", "non-linked-message", "not-a-reply"],
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
    taken, notices = await _answer(gateway, _reply("approve", sender="333"))
    assert taken
    assert not decision.done()
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.NOT_OWNER and kw["actor"] == "333"
    assert notices == [], "a stranger is told nothing"


@pytest.mark.asyncio
async def test_a_channel_not_paired_to_the_owner_cannot_answer(gateway, discord, audits):
    decision = await _open(gateway)
    # The linked channel now belongs to someone else by the transport's DM pairing.
    discord[1].pairs[_DM] = "333"
    taken, notices = await _answer(gateway, _reply("approve"))
    assert taken and not decision.done()
    assert [outcome for outcome, _ in audits] == [ActionOutcome.NOT_OWNER]
    assert notices == []


@pytest.mark.asyncio
async def test_an_owner_dropped_from_the_allow_list_can_no_longer_answer(gateway, discord, audits):
    decision = await _open(gateway)
    # Two DM targets: no single owner can be named.
    discord[0]._allowed = frozenset({_OWNER, "333"})
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
async def test_a_reply_after_the_prompts_deadline_answers_nothing(gateway, discord, audits, clock):
    decision = await _open(gateway)
    clock.now += _WINDOW_S + 1
    # Another binding, posted as its own message, prunes the expired one; the
    # expired note's link still names it.
    discord[1].next_id = "8"
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
async def test_with_the_preview_off_nothing_is_posted_or_linked(gateway, discord, preview):
    preview["on"] = False
    await _open(gateway)
    assert discord[1].sent == []
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_the_reply_note_is_one_discord_message_however_long_its_detail(gateway, discord):
    await _open(gateway, approval_detail="x" * 5000)
    [(_channel, text)] = discord[1].sent
    assert "Reply" in text and len(text) <= 1900
    assert gateway.bridge_actions._messages


@pytest.mark.asyncio
async def test_a_detail_the_display_pass_lengthens_still_fits_one_message(gateway, discord):
    # Every "@" gains a zero-width space in the delivery leg's mention defang, so the
    # fit must be measured as sent: 900 of them would otherwise spill past 1,900.
    await _open(gateway, approval_detail="@" * 900)
    [(_channel, text)] = discord[1].sent
    assert "Reply" in text and len(text) <= 1900
    assert len(gateway.bridge_actions._messages) == 1


@pytest.mark.asyncio
async def test_a_note_the_leg_splits_is_linked_to_nothing(gateway, discord, monkeypatch):
    # Whatever lengthens a note past one message, a part without the prompt is not
    # consent: a split note gets no link, so no reply to any part answers it.
    monkeypatch.setattr("kiro_crew.notifications.bridge_transport._ONE_DISCORD_MESSAGE", 10_000)
    decision = await _open(gateway, session_title="t" * 2500)
    assert len(discord[1].sent) > 1
    assert gateway.bridge_actions._messages == {}
    taken, _notices = await _answer(gateway, _reply("approve"))
    assert not taken and not decision.done()


@pytest.mark.asyncio
async def test_no_reply_prompt_when_the_note_cannot_fit_one_discord_message(gateway, discord):
    await _open(gateway, session_title="t" * 1900)
    assert "Reply" not in _posted_text(discord[1])
    assert gateway.bridge_actions._messages == {}


@pytest.mark.asyncio
async def test_untrusted_input_stays_inside_the_code_fence(gateway, discord):
    await _open(gateway, approval_detail="ls ```\n[ok](https://example.org/x) **bold**")
    [(_channel, text)] = discord[1].sent
    fence = text.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    assert "```" not in fence, "a backtick run in the input cannot close the fence"
    assert "[ok](https://example.org/x)" in fence


@pytest.mark.asyncio
async def test_the_dispatcher_takes_the_reply_before_any_turn(gateway):
    decision = await _open(gateway)
    d, cli, sess = _dispatcher({_OWNER})
    # The wiring the gateway does: the state rides the session-resume controller,
    # never ``register_channel_transport``, whose dispatch callback is a closure.
    assert not hasattr(d, "dashboard_state")
    d._session_resume.dashboard_state = gateway
    await d.handle_message(_reply("approve"))
    assert decision.result() == "approved"
    assert sess.acquired == [], "a bridged reply starts no turn"
    assert any("Approved." in str(text) for text, _ in cli.sent)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"type": 19, "message_reference": {"message_id": "55"}}, "55"),
        ({"type": 0, "message_reference": {"message_id": "55"}}, ""),
        ({"type": 19, "message_reference": {"message_id": 55}}, ""),
        ({"type": 19, "message_reference": {"message_id": "5x"}}, ""),
        ({"type": 19, "message_reference": "junk"}, ""),
        ({"type": 19}, ""),
    ],
    ids=["reply", "not-a-reply-type", "int-id", "not-digits", "junk-reference", "no-reference"],
)
def test_the_reply_target_is_read_defensively(payload, expected):
    assert _reply_message_id(payload) == expected
