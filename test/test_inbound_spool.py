"""Durable inbound spool: the loss it closes, and the bounds that keep it safe.

Gateway shutdown gathers channel teardown and
``SessionManager.close_all()`` concurrently, so a message the platform has
already accepted can be refused by the ``_closing`` gate before its turn ever
opens. Nothing retries it: the payload was discarded and the user was answered
with the channel's generic fault notice.

The fix is deliberately narrow: record the refused message at the refusal
point, and on the next start tell the user, in that same conversation, that it
was never processed -- quoting it so a resend is one tap. It is NOT re-driven as
a turn; see the module docstring for why re-dispatch was built and removed.

Two tests here are the RED-BEFORE pair, and both were proven to fail with the
production change reverted and the tests untouched:

* :func:`test_a_refused_turn_is_spooled_with_its_routing` -- with the spool call
  removed from ``drive_turn``'s ``except SessionClosingError`` branch it fails on
  ``a refused message left no durable trace``, which is the loss itself.
* :func:`test_a_confirmed_notice_removes_the_entry` -- with the notice pass
  unwired the spooled message is never answered and the entry never leaves disk.

The rest pin the properties that make the feature safe rather than merely
present: opt-in per channel, a count cap, an age horizon, per-entry truncation,
dedupe of a double-spool, the egress re-authorization, at-least-once removal
(only after a CONFIRMED send), and the link/lock/fence rules that make the spool
a trust boundary rather than a writable input.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging import inbound_spool as S
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn
from kiro_crew.messaging.inbound_spool import (
    SPOOL_MAX_ENTRIES,
    InboundRoute,
    SpooledInbound,
    record_refusal_sync,
    remove_entry,
    replay_spooled,
    spool_path,
)
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.session_allocation import SessionClosingError

# ── Pipeline stand-ins, mirroring test_messaging_dispatch.py ──────────────────


class _Sessions:
    """Refuses the turn at the closing gate, exactly as the real manager does."""

    def __init__(self, closing: bool = True) -> None:
        self.closing = closing
        self.released = 0
        self.successes = 0
        self.failures = 0

    async def get_or_create(self, key, agent=None, channel_id=None):
        return object(), False, False

    def begin_turn(self, key):
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key, channel_id):
        pass

    def record_success(self, key):
        self.successes += 1

    async def record_failure(self, key):
        self.failures += 1

    def release(self, key):
        self.released += 1

    def get_provider(self, key):
        return object()


class _Renderer:
    async def on_turn_start(self):
        pass

    async def close(self):
        pass


class _CtxBuilder:
    def build_message(self, text, is_new, session_key, **kw):
        return text, None


class _Driver:
    last_stop_reason = ""

    def __init__(self, *a, **kw):
        self._closing_gate = kw.get("closing_gate")

    async def run(self, message):
        if self._closing_gate is not None:
            self._closing_gate()
        return "the reply"


def _patch_pipeline(monkeypatch) -> None:
    async def _permitted(_channel_type):
        return True

    async def _publish(_sessions, _key):
        pass

    async def _embed(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(D, "inbound_permitted", _permitted)
    monkeypatch.setattr(D, "publish_turn_identity", _publish)
    monkeypatch.setattr(D, "run_in_embed_pool", _embed)
    monkeypatch.setattr(D, "TurnDriver", _Driver)


@pytest.fixture()
def spool_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the DEFAULT spool location at a temp home.

    ``spool_path`` resolves ``data_home`` per call (deliberately, so a HOME
    override set after import is honoured), so patching the module attribute is
    what redirects the paths the production call sites use — they take no
    ``path`` argument.
    """
    monkeypatch.setattr(S, "data_home", lambda: tmp_path)
    return tmp_path / "inbound-spool" / "refused.jsonl"


def _turn(route: InboundRoute | None) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="what is the deploy status?",
        renderer=_Renderer(),
        approval_mode="auto",
        inbound_route=route,
    )


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _texts(path: Path) -> list[str]:
    return [row["text"] for row in _entries(path)] if path.exists() else []


def peek_next(**kw: Any) -> SpooledInbound | None:
    """The entry half of :func:`S.peek_next`; the held list is asserted where it matters."""
    entry, _held = S.peek_next(**kw)
    return entry


class _Transport:
    """A connected transport: an egress gate and a send that returns an id."""

    capabilities = TransportCapabilities()

    def __init__(
        self,
        *,
        may_send: bool = True,
        send_raises: bool = False,
        message_id: Any = "mid",
    ) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.send_gate_calls: list[tuple[str, str | None, str]] = []
        self._may_send = may_send
        self._send_raises = send_raises
        self._message_id = message_id

    def may_send_to(self, conversation_id, thread_id=None, *, principal=""):
        self.send_gate_calls.append((conversation_id, thread_id, principal))
        return self._may_send

    async def send_message(self, conversation_id, content, thread_id=None):
        if self._send_raises:
            raise RuntimeError("conversation is gone")
        self.sent.append((conversation_id, content, thread_id))
        return self._message_id


def _spool(path: Path, **kw: Any) -> SpooledInbound:
    entry = SpooledInbound(
        channel_type=kw.pop("channel_type", "telegram"),
        conversation_id=kw.pop("conversation_id", "555"),
        text=kw.pop("text", "please check CI"),
        **kw,
    )
    assert record_refusal_sync(entry, path=path)
    return entry


# ── The loss (red-before on an unwired tree) ──────────────────────────────────


def test_a_refused_turn_is_spooled_with_its_routing(monkeypatch, spool_home: Path) -> None:
    """The message survives a shutdown refusal instead of being discarded.

    RED-BEFORE: with no spool wired into ``drive_turn``'s
    ``except SessionClosingError`` branch, the refusal leaves nothing on disk and
    the user's text is gone for good — the loss this spool exists to close.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions(closing=True)

    asyncio.run(
        drive_turn(
            _turn(
                InboundRoute(
                    conversation_id="userA", user_id="userA", text="what is the deploy status?"
                )
            ),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert spool_home.exists(), "a refused message left no durable trace"
    rows = _entries(spool_home)
    assert len(rows) == 1
    assert rows[0]["text"] == "what is the deploy status?"
    assert rows[0]["channel_type"] == "weixin"
    assert rows[0]["conversation_id"] == "userA", (
        "the reply target must be the transport-addressable id, not the " "session-attribution id"
    )
    assert rows[0]["user_id"] == "userA"
    assert rows[0]["spooled_at"] > 0
    assert sessions.failures == 0, "a restart is not a session fault"
    assert sessions.successes == 0, "no turn ran"


def test_a_channel_that_declares_no_route_is_not_spooled(monkeypatch, spool_home: Path) -> None:
    """Adoption is opt-in, so an un-adopted channel is byte-identical to before.

    Without this, every channel would be half-enrolled: an entry written with no
    addressable reply target posts the notice nowhere and looks like a silent
    drop with extra disk writes.
    """
    _patch_pipeline(monkeypatch)

    asyncio.run(drive_turn(_turn(None), sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    assert not spool_home.exists()


def test_a_successful_turn_writes_nothing(monkeypatch, spool_home: Path) -> None:
    """Zero cost on the happy path — the property that makes this affordable.

    It is also what makes the notice truthful: a turn that completed just before
    exit was never written, so nobody is told to resend something that was
    answered.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions(closing=False)

    asyncio.run(
        drive_turn(
            _turn(InboundRoute(conversation_id="userA")),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert sessions.successes == 1
    assert not spool_home.exists()


def test_a_media_only_refusal_is_spooled_for_the_notice(spool_home: Path) -> None:
    """An uncaptioned photo is still a message the platform marked delivered.

    Refusing to WRITE it made media-only the one message shape this spool
    silently dropped. Recorded with an empty body and a nonzero dropped count,
    which is what the notice names.
    """
    wrote = asyncio.run(
        S.spool_refused_turn(
            channel_type="telegram",
            route=InboundRoute(conversation_id="1", text="   ", attachments_dropped=2),
        )
    )
    assert wrote is True
    rows = _entries(spool_home)
    assert rows[0]["attachments_dropped"] == 2


def test_a_refusal_with_neither_text_nor_media_is_not_spooled(spool_home: Path) -> None:
    """Nothing at all means nothing to notify about."""
    wrote = asyncio.run(
        S.spool_refused_turn(
            channel_type="telegram",
            route=InboundRoute(conversation_id="1", text="   "),
        )
    )
    assert wrote is False
    assert not spool_home.exists()


# ── The notice pass ───────────────────────────────────────────────────────────


def test_a_confirmed_notice_removes_the_entry(spool_home: Path) -> None:
    """The fix, end to end: the user is told, in their conversation, to resend.

    RED-BEFORE: with the pass unwired the message is never answered and the
    entry never leaves disk.
    """
    _spool(spool_home, conversation_id="555", thread_id="7", text="please check CI")
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert report.notified and not report.dropped and not report.unconfirmed
    assert len(transport.sent) == 1
    conversation, content, thread = transport.sent[0]
    assert (conversation, thread) == ("555", "7"), "the notice went to the wrong conversation"
    assert "> please check CI" in content, "the notice must quote the message so a resend is a tap"
    assert "restarting" in content, "the notice must name the real cause, not a generic fault"
    assert not spool_home.exists(), "a confirmed notice left the entry to be noticed again"


def test_a_dropped_attachment_is_named_in_the_notice(spool_home: Path) -> None:
    _spool(spool_home, text="see attached", attachments_dropped=2)
    transport = _Transport()

    asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert "2 attachment(s)" in transport.sent[0][1]


def test_a_media_only_entry_is_noticed(spool_home: Path) -> None:
    _spool(spool_home, text="", attachments_dropped=1)
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert report.notified and "1 attachment(s)" in transport.sent[0][1]


def test_the_notice_is_delivered_at_least_once(spool_home: Path) -> None:
    """An UNCONFIRMED send keeps the entry; the next start notices it again.

    This is the direction the notice-only design can afford: a duplicate notice
    is a repeated line, never a repeated side effect. A raise and an empty
    message id are both "unconfirmed" -- neither is evidence the user saw it.
    """
    _spool(spool_home, message_id="a", text="raises")
    _spool(spool_home, message_id="b", text="empty id")

    class _RaisesForOne(_Transport):
        async def send_message(self, conversation_id, content, thread_id=None):
            if "raises" in content:
                raise RuntimeError("platform 5xx")
            self.sent.append((conversation_id, content, thread_id))
            return ""

    transport = _RaisesForOne()
    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert sorted(report.unconfirmed) == sorted([f"telegram:555:{i}" for i in ("a", "b")])
    assert not report.notified and not report.dropped
    assert _texts(spool_home) == ["raises", "empty id"], "an unconfirmed notice removed the entry"

    # Next start, the platform is back: both are noticed and removed.
    second = _Transport()
    again = asyncio.run(replay_spooled(transports={"telegram": second}))
    assert len(again.notified) == 2 and len(second.sent) == 2
    assert not spool_home.exists()


def test_an_unconfirmed_entry_does_not_block_the_rest(spool_home: Path) -> None:
    """One bad route must not park the whole queue behind it."""
    _spool(spool_home, message_id="a", conversation_id="dead", text="dead route")
    _spool(spool_home, message_id="b", conversation_id="live", text="live route")

    class _DeadRoute(_Transport):
        async def send_message(self, conversation_id, content, thread_id=None):
            if conversation_id == "dead":
                raise RuntimeError("gone")
            return await super().send_message(conversation_id, content, thread_id)

    transport = _DeadRoute()
    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert report.unconfirmed == ["telegram:dead:a"] and report.notified == ["telegram:live:b"]
    assert _texts(spool_home) == ["dead route"]


def test_a_transport_that_returns_no_ids_is_still_confirmed(spool_home: Path) -> None:
    """WeCom and Feishu return ``""`` on SUCCESS and raise on failure.

    Reading their empty id as "unconfirmed" would notice the same message on
    every start until the horizon. The capability is what says which idiom the
    transport follows, so it is read rather than assumed.
    """
    _spool(spool_home, channel_type="wecom")

    class _NoIds(_Transport):
        capabilities = TransportCapabilities(returns_message_id=False)

    transport = _NoIds(message_id="")
    report = asyncio.run(replay_spooled(transports={"wecom": transport}))

    assert report.notified and not report.unconfirmed
    assert len(transport.sent) == 1
    assert not spool_home.exists()


def test_a_crash_between_send_and_removal_re_notices(spool_home: Path) -> None:
    """The at-least-once half, driven through the real pass.

    The entry is removed only after the send returns, so a process that dies in
    between leaves it on disk and the next start notices it again -- the user
    sees a repeated line, and never a lost message.
    """
    _spool(spool_home, message_id="m", text="once more")

    class _DiesAfterSend(_Transport):
        async def send_message(self, conversation_id, content, thread_id=None):
            self.sent.append((conversation_id, content, thread_id))
            raise KeyboardInterrupt("the gateway went down after the send")

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(replay_spooled(transports={"telegram": _DiesAfterSend()}))
    assert _texts(spool_home) == ["once more"], "a crash after the send lost the entry"

    transport = _Transport()
    asyncio.run(replay_spooled(transports={"telegram": transport}))
    assert len(transport.sent) == 1 and not spool_home.exists()


def test_a_crash_on_the_first_entry_keeps_the_rest(spool_home: Path) -> None:
    for index in range(3):
        _spool(spool_home, message_id=f"m-{index}", text=f"message {index}")

    class _Dies(_Transport):
        async def send_message(self, conversation_id, content, thread_id=None):
            raise KeyboardInterrupt("mid-pass")

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(replay_spooled(transports={"telegram": _Dies()}))

    assert _texts(spool_home) == ["message 0", "message 1", "message 2"]


def test_a_second_pass_has_nothing_left_to_do(spool_home: Path) -> None:
    _spool(spool_home)
    transport = _Transport()

    asyncio.run(replay_spooled(transports={"telegram": transport}))
    again = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert len(transport.sent) == 1 and again.total == 0


def test_entries_are_noticed_oldest_first(spool_home: Path) -> None:
    for index in range(3):
        _spool(spool_home, message_id=f"m-{index}", text=f"message {index}")
    transport = _Transport()

    asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert [content.splitlines()[-1] for _, content, _ in transport.sent] == [
        "> message 0",
        "> message 1",
        "> message 2",
    ]


def test_an_over_cap_quote_is_truncated_visibly_not_sliced_by_the_transport(
    spool_home: Path,
) -> None:
    """The notice prefixes the quote, so a message that fit on the way in may not fit now.

    RED-BEFORE: with no sizing, a maximum-length message plus the prefix exceeds
    ``max_message_chars``; a transport that slices to its cap and still returns an
    id would confirm a notice whose tail was silently cut, and the entry would be
    removed with text the user never saw echoed. Truncated HERE, and marked.
    """
    _spool(spool_home, text="x" * 1900)

    class _Capped(_Transport):
        capabilities = TransportCapabilities(max_message_chars=1900)

    transport = _Capped()
    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert report.notified
    content = transport.sent[0][1]
    assert len(content) <= 1900, "the notice exceeded the transport cap"
    assert "too long to quote in full" in content, "the truncation must be visible"
    assert "restarting" in content, "the prefix must survive the cut"


def test_a_short_quote_is_not_truncated(spool_home: Path) -> None:
    _spool(spool_home, text="short")

    class _Capped(_Transport):
        capabilities = TransportCapabilities(max_message_chars=1900)

    transport = _Capped()
    asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert "too long to quote" not in transport.sent[0][1]
    assert "> short" in transport.sent[0][1]


def test_the_notice_is_made_display_safe_for_the_transport(spool_home: Path) -> None:
    """A message can carry a broadcast mention; echoing it verbatim would fire it."""
    _spool(spool_home, channel_type="slack", text="@channel is CI down")

    transport = _Transport()
    asyncio.run(replay_spooled(transports={"slack": transport}))

    content = transport.sent[0][1]
    assert "@channel is CI down" not in content, "a broadcast mention was echoed live"
    assert "@\u200bchannel is CI down" in content


# ── Holding: an absent channel is not a revoked one ───────────────────────────


def test_an_unconnected_channel_entry_is_held_for_the_next_start(spool_home: Path) -> None:
    """A channel absent THIS run may have merely failed to start.

    Dropping the entry turned a transient startup failure -- a token refresh that
    timed out, a socket that came up late -- into permanent loss of a message the
    platform marked delivered. Holding costs nothing, because the age horizon
    still expires an entry for a channel the operator genuinely turned off.
    """
    _spool(spool_home, channel_type="discord", text="still here?")

    report = asyncio.run(replay_spooled(transports={}))

    assert report.held and not report.dropped and not report.notified
    assert _texts(spool_home) == [
        "still here?"
    ], "the entry was discarded on a run where its channel was simply not up"

    transport = _Transport()
    second = asyncio.run(replay_spooled(transports={"discord": transport}))
    assert second.notified and len(transport.sent) == 1


def test_a_held_entry_never_leaves_disk_during_the_pass(spool_home: Path) -> None:
    """The predicate runs UNDER the lock and skips in place; nothing is requeued."""
    _spool(spool_home, channel_type="discord", message_id="d", text="held")
    _spool(spool_home, channel_type="telegram", message_id="t", text="live")
    seen_on_disk: list[list[str]] = []

    class _Observing(_Transport):
        async def send_message(self, conversation_id, content, thread_id=None):
            seen_on_disk.append(_texts(spool_home))
            return await super().send_message(conversation_id, content, thread_id)

    asyncio.run(replay_spooled(transports={"telegram": _Observing()}))

    assert seen_on_disk == [["held", "live"]], "an entry left disk before its send was confirmed"
    assert _texts(spool_home) == ["held"]


def test_a_held_entry_keeps_its_original_arrival_order(spool_home: Path) -> None:
    for index in range(3):
        _spool(spool_home, channel_type="discord", message_id=f"m-{index}", text=f"m {index}")

    asyncio.run(replay_spooled(transports={}))

    assert _texts(spool_home) == ["m 0", "m 1", "m 2"]


def test_a_held_entry_still_expires_on_the_age_horizon(spool_home: Path) -> None:
    """Holding must not become an unbounded queue: the horizon is the bound."""
    assert record_refusal_sync(
        SpooledInbound(channel_type="discord", conversation_id="1", text="old"),
        path=spool_home,
        now=1_000.0,
    )
    asyncio.run(replay_spooled(transports={}, now=1_000.0 + 10))
    assert spool_home.exists()

    asyncio.run(replay_spooled(transports={}, now=1_000.0 + S.SPOOL_MAX_AGE_SECS + 1))
    assert not spool_home.exists(), "a held entry outlived the horizon"


# ── Authorization is re-decided at replay time ───────────────────────────────


def test_the_notice_is_withheld_from_a_revoked_conversation(spool_home: Path) -> None:
    """A spooled entry is not a standing grant for the conversation it names.

    The gateway was down for the whole window this feature spans, so the peer may
    have left the roster or the Topic been de-allow-listed since. The notice is a
    proactive send, which is exactly what ``may_send_to`` governs -- and a revoked
    entry is REMOVED, not held: there is nothing this module may ever do with it.
    """
    _spool(spool_home, channel_type="teams", conversation_id="conv-1", user_id="u-1")
    transport = _Transport(may_send=False)

    report = asyncio.run(replay_spooled(transports={"teams": transport}))

    assert transport.sent == [], "output reached a conversation that is no longer authorized"
    assert report.dropped and not report.notified
    assert transport.send_gate_calls == [("conv-1", None, "u-1")], (
        "the principal must be passed, or a transport that authorizes by peer "
        "cannot make the decision"
    )
    assert not spool_home.exists()


def test_a_revoked_route_drop_is_audited(spool_home: Path, monkeypatch) -> None:
    """The ``may_send_to`` denial is a permission decision that also deletes a message.

    RED-BEFORE: without the audit the SEL trail shows the channel scope granted
    and then nothing, so a revoked route silently losing its notice looks exactly
    like nothing having been spooled. Same record shape as the cross-surface
    proactive send's identical denial.
    """
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        S,
        "sel",
        lambda: type("Sel", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
    )
    _spool(spool_home, channel_type="teams", conversation_id="conv-1", user_id="u-1")

    asyncio.run(replay_spooled(transports={"teams": _Transport(may_send=False)}))

    assert len(recorded) == 1, "the denial was not audited"
    assert recorded[0]["operation"] == "channel.proactive_send_authorize"
    assert recorded[0]["outcome"] == "denied"
    assert recorded[0]["source"] == "teams"
    assert recorded[0]["caller"] == "u-1"


def test_an_allowed_route_is_audited_too(spool_home: Path, monkeypatch) -> None:
    """The grant is the decision that puts the gateway's own text into a conversation."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        S,
        "sel",
        lambda: type("Sel", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
    )
    _spool(spool_home, channel_type="teams", conversation_id="conv-1", user_id="u-1")

    asyncio.run(replay_spooled(transports={"teams": _Transport()}))

    outcomes = [(r["operation"], r["outcome"]) for r in recorded]
    assert ("channel.proactive_send_authorize", "allowed") in outcomes


def test_a_transport_with_no_egress_gate_is_refused_the_notice(spool_home: Path) -> None:
    """Fails closed: a spool entry is the one input that did not come from the platform."""

    class _NoGate:
        capabilities = TransportCapabilities()

        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send_message(self, conversation_id, content, thread_id=None):
            self.sent.append(conversation_id)
            return "mid"

    _spool(spool_home, channel_type="teams")
    transport = _NoGate()

    report = asyncio.run(replay_spooled(transports={"teams": transport}))

    assert transport.sent == []
    assert report.dropped


def test_a_channel_denied_by_governance_gets_no_notice_and_is_held(
    spool_home: Path, monkeypatch
) -> None:
    """The ``channels`` policy ceiling is asked before every notice, fail-closed.

    RED-BEFORE: without the vet, a channel the operator denied while the gateway
    was down still receives the notice, because the transport is CONNECTED and
    ``may_send_to`` answers only about the route. Held rather than dropped: the
    route is not revoked, the channel is governed off, and the horizon bounds it.
    """
    calls: list[tuple[str, str, str]] = []

    def deny(scope, item, *, session_key, tool_name, **kw):
        calls.append((scope, item, tool_name))
        return type("D", (), {"permitted": False})()

    # Patched where it is BOUND (module-scope import), as the top-level-imports
    # rule requires; patching the defining module would leave the bound name.
    monkeypatch.setattr(S, "vet_and_audit", deny)
    _spool(spool_home, channel_type="telegram")
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert transport.sent == [], "a governance-denied channel received the notice"
    assert report.held and not report.notified and not report.dropped
    assert calls == [("channels", "telegram", "inbound_spool.notice")]
    assert _texts(spool_home) == ["please check CI"], "a held entry must stay on disk"


def test_a_governance_evaluation_failure_denies(spool_home: Path, monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("profile store unavailable")

    monkeypatch.setattr(S, "vet_and_audit", boom)
    _spool(spool_home)
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert transport.sent == [] and report.held


def test_a_raising_egress_gate_is_read_as_revoked(spool_home: Path) -> None:
    class _Raises(_Transport):
        def may_send_to(self, conversation_id, thread_id=None, *, principal=""):
            raise RuntimeError("roster unavailable")

    _spool(spool_home)
    transport = _Raises()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert transport.sent == [] and report.dropped


def test_a_threaded_route_is_authorized_by_the_thread_roster_alone(spool_home: Path) -> None:
    """The principal is withheld for a threaded route; a DM route still carries it.

    Discord's ``may_send_to`` falls from a thread not in ``_allowed_threads`` to
    ``principal in _allowed``, assuming a thread route names no principal. A spooled
    thread entry names the sender, so passing it would let a still-allowed sender
    authorize a notice into a revoked thread.
    """
    _spool(spool_home, channel_type="discord", conversation_id="thr", thread_id="thr", user_id="u")
    _spool(spool_home, channel_type="discord", conversation_id="dm", user_id="u", message_id="m")
    transport = _Transport()

    asyncio.run(replay_spooled(transports={"discord": transport}))

    assert sorted(transport.send_gate_calls) == sorted([("thr", "thr", ""), ("dm", None, "u")])


def test_a_revoked_discord_thread_gets_no_notice_even_from_an_allowed_sender(
    spool_home: Path,
) -> None:
    """Against the REAL Discord egress gate, not a stand-in.

    RED-BEFORE: with ``principal=entry.user_id`` passed for the thread route, the
    still-allowed sender authorizes via the DM arm and the notice lands in a
    thread that is off the roster.
    """
    from kiro_crew.discord.transport import DiscordTransport

    class _Client:
        pass

    transport = DiscordTransport(_Client(), allowed_user_ids=["42"])  # type: ignore[arg-type]
    # No allowed threads: the spooled thread was auto-created, memory-only, and
    # is gone after the restart -- the ordinary post-restart state.
    sent: list[tuple[str, str | None]] = []

    async def send_message(conversation_id, content, thread_id=None):
        sent.append((conversation_id, thread_id))
        return "mid"

    transport.send_message = send_message  # type: ignore[method-assign]
    _spool(
        spool_home, channel_type="discord", conversation_id="9001", thread_id="9001", user_id="42"
    )

    report = asyncio.run(replay_spooled(transports={"discord": transport}))

    assert sent == [], "the notice was posted into a thread the roster no longer allows"
    assert report.dropped and not report.notified


# ── Dedupe: a double-spool of the SAME message, and nothing more ─────────────


def test_the_same_message_spooled_twice_yields_one_entry(spool_home: Path) -> None:
    """A retry loop around the refusal point must not multiply the entry."""
    for _ in range(3):
        _spool(spool_home, message_id="m-1")

    assert len(_entries(spool_home)) == 1


def test_two_messages_with_no_platform_id_stay_distinct(spool_home: Path) -> None:
    """An entry the platform gave no id is never collapsed against another."""
    _spool(spool_home, text="first")
    _spool(spool_home, text="second")

    assert set(_texts(spool_home)) == {"first", "second"}


def test_two_identical_bodies_with_no_platform_id_both_survive(spool_home: Path) -> None:
    """Repeating yourself is ordinary; losing the second message is not.

    A body digest is the obvious-looking identity for a channel with no message
    id, and it is a data-loss bug: two identical messages hash the same, so one
    accepted message would be silently discarded. The hazard it would guard
    against does not exist -- the refusal is one ``except`` branch that runs once
    per turn.
    """
    _spool(spool_home, channel_type="weixin", conversation_id="peer-1", text="status")
    _spool(spool_home, channel_type="weixin", conversation_id="peer-1", text="status")

    assert _texts(spool_home) == [
        "status",
        "status",
    ], "the second 'status' was discarded — the user asked twice and is told once"


def test_two_identical_id_less_bodies_are_each_noticed_once(spool_home: Path) -> None:
    """Removal is by ONE occurrence, so a shared trace id removes one, not both."""
    _spool(spool_home, channel_type="weixin", conversation_id="peer-1", text="status")
    _spool(spool_home, channel_type="weixin", conversation_id="peer-1", text="status")
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"weixin": transport}))

    assert len(transport.sent) == 2 and len(report.notified) == 2
    assert not spool_home.exists()


def test_an_entry_with_no_platform_id_has_no_dedupe_identity(spool_home: Path) -> None:
    """The empty key is the contract, not an accident of formatting."""
    without = SpooledInbound(channel_type="weixin", conversation_id="p", text="hi")
    with_id = SpooledInbound(
        channel_type="telegram", conversation_id="1", text="hi", message_id="m-1"
    )

    assert without.dedupe_key == ""
    assert with_id.dedupe_key == "telegram:1:m-1"
    assert without.trace_id.startswith("weixin:p:")


# ── Bounds: a crash-loop must not grow the spool ─────────────────────────────


def test_the_count_cap_keeps_the_newest_entries(spool_home: Path) -> None:
    """Newest-wins, so a wedged gateway cannot accumulate a notice storm."""
    for index in range(SPOOL_MAX_ENTRIES + 10):
        _spool(spool_home, message_id=f"m-{index}", text=f"message {index}")

    rows = _entries(spool_home)
    assert len(rows) == SPOOL_MAX_ENTRIES
    assert rows[-1]["text"] == f"message {SPOOL_MAX_ENTRIES + 9}"
    assert rows[0]["text"] == "message 10", "the oldest entries should be the ones dropped"


def test_the_age_horizon_drops_a_stale_entry_on_read(spool_home: Path) -> None:
    """A day-old unanswered message is noise, and the horizon is what expires it.

    Applied on READ as well as on write, because an entry that was legal when
    written can sit past the horizon while the gateway is down.
    """
    assert record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="old"),
        path=spool_home,
        now=1_000.0,
    )

    assert peek_next(path=spool_home, now=1_000.0 + S.SPOOL_MAX_AGE_SECS + 1) is None
    assert not spool_home.exists(), "an all-stale spool must not be re-read on every start"


def test_a_stale_entry_does_not_survive_a_later_write(spool_home: Path) -> None:
    assert record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="old", message_id="a"),
        path=spool_home,
        now=1_000.0,
    )
    assert record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="new", message_id="b"),
        path=spool_home,
        now=1_000.0 + S.SPOOL_MAX_AGE_SECS + 1,
    )

    assert _texts(spool_home) == ["new"]


def test_an_over_cap_body_is_truncated_and_says_so(spool_home: Path) -> None:
    """One paste must not be the whole budget, and truncation must be visible."""
    _spool(spool_home, text="x" * (S.TEXT_CAP * 2))

    text = _entries(spool_home)[0]["text"]
    assert len(text) <= S.TEXT_CAP
    assert "truncated" in text


def test_the_pass_is_bounded_by_the_count_cap(spool_home: Path) -> None:
    """A transport that never confirms cannot spin the pass forever."""
    for index in range(5):
        _spool(spool_home, message_id=f"m-{index}")
    transport = _Transport(message_id="")

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert len(transport.sent) == 5, "each unconfirmed entry is attempted exactly once per pass"
    assert len(report.unconfirmed) == 5


# ── The store primitives ──────────────────────────────────────────────────────


def test_peek_returns_the_oldest_without_removing_it(spool_home: Path) -> None:
    for index in range(3):
        _spool(spool_home, message_id=f"m-{index}", text=f"message {index}")

    first = peek_next(path=spool_home)

    assert first is not None and first.text == "message 0", "oldest first"
    assert _texts(spool_home) == ["message 0", "message 1", "message 2"], "peek removed an entry"


def test_peek_holds_an_unconnected_channel_and_skips_a_seen_entry(spool_home: Path) -> None:
    held_entry = _spool(spool_home, channel_type="discord", message_id="d", text="held")
    seen_entry = _spool(spool_home, channel_type="telegram", message_id="s", text="seen")
    _spool(spool_home, channel_type="telegram", message_id="t", text="take")

    entry, held = S.peek_next(path=spool_home, connected={"telegram"}, skip={seen_entry.trace_id})

    assert entry is not None and entry.text == "take"
    assert held == [held_entry.trace_id], "a held entry must be reported, not silently skipped"
    assert _texts(spool_home) == ["held", "seen", "take"], "peek removed an entry"


def test_a_failed_removal_notices_once_per_pass_not_once_per_iteration(
    spool_home: Path, monkeypatch
) -> None:
    """A spool that cannot be rewritten must not turn one entry into 128 notices.

    RED-BEFORE: with the removal result ignored, the entry stays on disk and
    actionable, ``peek_next`` returns it again, and the bounded loop sends the
    same notice ``SPOOL_MAX_ENTRIES`` times in one pass. The pass now remembers
    every entry it has acted on; the next start retries the removal.
    """
    _spool(spool_home, message_id="a", text="once, please")
    _spool(spool_home, message_id="b", text="me too")
    monkeypatch.setattr(S, "remove_entry", lambda entry, *, path=None: False)
    transport = _Transport()

    report = asyncio.run(replay_spooled(transports={"telegram": transport}))

    assert len(transport.sent) == 2, "an entry was noticed more than once in a single pass"
    assert sorted(report.unconfirmed) == sorted(["telegram:555:a", "telegram:555:b"])
    assert not report.notified, "a notice whose entry is still on disk is not 'done'"
    assert _texts(spool_home) == ["once, please", "me too"]


def test_remove_takes_exactly_one_entry(spool_home: Path) -> None:
    entries = [
        _spool(spool_home, message_id=f"m-{index}", text=f"message {index}") for index in range(3)
    ]

    assert remove_entry(entries[1], path=spool_home) is True
    assert _texts(spool_home) == ["message 0", "message 2"]
    assert remove_entry(entries[1], path=spool_home) is False, "removing twice must be a no-op"


def test_the_spool_file_is_removed_once_drained(spool_home: Path) -> None:
    """An emptied spool must not sit on disk being re-read on every start."""
    entry = _spool(spool_home)

    assert remove_entry(entry, path=spool_home) is True
    assert not spool_home.exists()
    assert peek_next(path=spool_home) is None


def test_a_failed_unlink_of_the_last_entry_does_not_resurrect_it(
    spool_home: Path, monkeypatch
) -> None:
    """The removal is the atomic replace, not the unlink.

    On Windows an AV scanner or indexer holding a handle makes ``unlink`` fail
    routinely. If the last entry were removed by unlink alone, that failure would
    leave it on disk to be noticed again on every start until the horizon. So the
    remainder is always written (empty when nothing is left) and the unlink is a
    tidy-up that gates nothing.
    """
    entry = _spool(spool_home, message_id="only", text="once")
    real_unlink = Path.unlink

    def _sharing_violation(self, *args, **kwargs):
        if self == spool_home:
            raise PermissionError(32, "sharing violation")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _sharing_violation)

    assert remove_entry(entry, path=spool_home) is True
    assert peek_next(path=spool_home) is None, "the removed entry came back after a failed unlink"


# ── Robustness: this runs during shutdown and during boot ────────────────────


def test_an_unreadable_spool_is_left_untouched(spool_home: Path, monkeypatch) -> None:
    """A transient read failure must defer the pass, not erase the queue.

    Every writer rewrites the file from what it read, and the reader unlinks a
    file it read as empty. Reporting EIO as ``[]`` therefore had the next read
    delete every queued message.
    """
    _spool(spool_home, text="keep me")
    real_open = S._open_own_file

    def _flaky_open(path):
        if Path(path) == spool_home:
            raise PermissionError(13, "transient")
        return real_open(path)

    monkeypatch.setattr(S, "_open_own_file", _flaky_open)
    assert peek_next(path=spool_home) is None
    monkeypatch.undo()

    assert spool_home.exists(), "the spool was deleted after a read failure"
    assert _texts(spool_home) == ["keep me"]


def test_an_unreadable_spool_refuses_a_write_rather_than_overwriting(
    spool_home: Path, monkeypatch
) -> None:
    _spool(spool_home, text="keep me")
    real_open = S._open_own_file

    def _flaky_open(path):
        if Path(path) == spool_home:
            raise PermissionError(13, "transient")
        return real_open(path)

    monkeypatch.setattr(S, "_open_own_file", _flaky_open)
    wrote = record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="new"),
        path=spool_home,
    )
    monkeypatch.undo()

    assert wrote is False
    assert _texts(spool_home) == ["keep me"]


def test_a_write_failure_degrades_to_the_old_loss(tmp_path: Path) -> None:
    """Never the thing that fails shutdown — a refusal to write is just today."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file", encoding="utf-8")

    wrote = record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="hi"),
        path=blocked / "spool.jsonl",
    )

    assert wrote is False


def test_a_corrupt_line_is_skipped_rather_than_failing_the_boot(spool_home: Path) -> None:
    """The pass runs on the boot path, so a hand-edited spool must not raise."""
    spool_home.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(
        SpooledInbound(
            channel_type="telegram", conversation_id="1", text="ok", spooled_at=9e9
        ).to_dict()
    )
    spool_home.write_text(f"not json\n{good}\n{{}}\n", encoding="utf-8")

    entry = peek_next(path=spool_home, now=9e9)

    assert entry is not None and entry.text == "ok"


def test_an_entry_with_no_reply_target_is_refused(spool_home: Path) -> None:
    """Fails closed: an unaddressable entry would send the notice nowhere."""
    assert SpooledInbound.from_dict({"channel_type": "telegram", "text": "hi"}) is None
    assert SpooledInbound.from_dict({"conversation_id": "1", "text": "hi"}) is None
    assert SpooledInbound.from_dict({"channel_type": "telegram", "conversation_id": "1"}) is None


def test_the_default_spool_path_lives_under_the_data_home(spool_home: Path) -> None:
    assert spool_path() == spool_home


def test_an_entry_records_only_what_the_notice_reads() -> None:
    """No field ridden along "for later": every persisted key is consumed by the pass.

    No ``session_key`` or ``chat_type``: a re-dispatch design would own its own
    record, and a field nothing reads is a field nothing tests.
    """
    keys = set(SpooledInbound(channel_type="t", conversation_id="c", text="x").to_dict())
    assert keys == {
        "channel_type",
        "conversation_id",
        "text",
        "user_id",
        "thread_id",
        "message_id",
        "attachments_dropped",
        "spooled_at",
    }
    # A record from the earlier shape still parses: unknown keys are ignored.
    legacy = {"channel_type": "t", "conversation_id": "c", "text": "x", "session_key": "s"}
    assert SpooledInbound.from_dict(legacy) is not None


def test_the_pass_never_raises_on_a_transport_fault(spool_home: Path) -> None:
    """A send that raises is "unconfirmed", not a boot failure."""
    _spool(spool_home)

    report = asyncio.run(replay_spooled(transports={"telegram": _Transport(send_raises=True)}))

    assert report.unconfirmed and _texts(spool_home) == ["please check CI"]


def test_a_cancelled_refusal_handler_still_lands_the_write(spool_home: Path) -> None:
    """The write is off-loop AND survives the caller's cancellation.

    The handler that reaches the refusal is a task ``close_all`` is about to
    cancel. A bare ``await asyncio.to_thread(...)`` there is a cancellation point
    that would orphan the write; blocking the loop instead would stall a shutdown
    already racing a deadline. ``asyncio.shield`` keeps both: the caller is
    cancelled, the write is not, and the entry is on disk when the loop drains.
    """
    import threading

    started = threading.Event()
    release = threading.Event()
    real_sync = S.record_refusal_sync

    def slow_sync(entry, *, path=None, now=None):
        started.set()
        assert release.wait(5), "test harness never released the write"
        return real_sync(entry, path=path, now=now)

    async def scenario() -> None:
        S.record_refusal_sync = slow_sync  # type: ignore[assignment]
        try:
            task = asyncio.create_task(
                S.record_refusal(
                    SpooledInbound(channel_type="telegram", conversation_id="1", text="survive")
                )
            )
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not spool_home.exists(), "the write had not been released yet"
            release.set()
            # Let the shielded inner task finish before the loop closes.
            for _ in range(50):
                if spool_home.exists():
                    break
                await asyncio.sleep(0.02)
        finally:
            S.record_refusal_sync = real_sync  # type: ignore[assignment]

    asyncio.run(scenario())

    assert _texts(spool_home) == ["survive"], "the caller's cancel orphaned the write"


def test_two_concurrent_refusals_both_survive(spool_home: Path) -> None:
    """The read-modify-write is serialized, so neither message is overwritten."""
    import concurrent.futures

    entries = [
        SpooledInbound(
            channel_type="telegram",
            conversation_id="1",
            text=f"message {index}",
            message_id=f"m-{index}",
        )
        for index in range(24)
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda entry: record_refusal_sync(entry, path=spool_home), entries))

    assert all(results)
    assert set(_texts(spool_home)) == {
        f"message {index}" for index in range(24)
    }, "a concurrent refusal overwrote another message"


# ── The spool is a trust boundary ─────────────────────────────────────────────


def test_a_linked_spool_directory_is_refused_for_read_and_write(tmp_path: Path) -> None:
    """A link planted before the fence existed must not be followed.

    The fence only holds from the build that ships it. A same-UID agent on an
    older build could plant a symlink at ``inbound-spool``; every open in this
    module would then resolve inside the link's target, which the fence never
    covered, and a JSON record forged there would post a notice quoting text the
    user never sent into a conversation of the forger's choosing. So a linked
    directory is refused BEFORE the lock, the read, the write or the unlink.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    forged = SpooledInbound(
        channel_type="telegram", conversation_id="1", text="forged", spooled_at=9e9
    )
    (elsewhere / "refused.jsonl").write_text(json.dumps(forged.to_dict()) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "inbound-spool").symlink_to(elsewhere, target_is_directory=True)
    spool = home / "inbound-spool" / "refused.jsonl"

    assert peek_next(path=spool, now=9e9) is None, "a forged record behind a link was read"
    assert (elsewhere / "refused.jsonl").exists(), "the link's target was mutated"
    assert (
        record_refusal_sync(
            SpooledInbound(channel_type="telegram", conversation_id="1", text="hi"), path=spool
        )
        is False
    )
    assert (elsewhere / "refused.jsonl").read_text(encoding="utf-8").count(
        "\n"
    ) == 1, "a write through the linked directory landed in its target"


def test_a_linked_spool_leaf_is_refused(tmp_path: Path) -> None:
    """The leaf too: ``os.open`` follows a final-component link."""
    elsewhere = tmp_path / "elsewhere.jsonl"
    forged = SpooledInbound(
        channel_type="telegram", conversation_id="1", text="forged", spooled_at=9e9
    )
    elsewhere.write_text(json.dumps(forged.to_dict()) + "\n", encoding="utf-8")
    spool_dir = tmp_path / "inbound-spool"
    spool_dir.mkdir()
    spool = spool_dir / "refused.jsonl"
    spool.symlink_to(elsewhere)

    assert peek_next(path=spool, now=9e9) is None
    assert elsewhere.exists()
    assert (
        record_refusal_sync(
            SpooledInbound(channel_type="telegram", conversation_id="1", text="hi"), path=spool
        )
        is False
    )


def test_a_hard_linked_spool_leaf_is_refused(tmp_path: Path) -> None:
    """A second name for the inode is another way to feed this module bytes it did not write."""
    spool_dir = tmp_path / "inbound-spool"
    spool_dir.mkdir()
    spool = spool_dir / "refused.jsonl"
    assert record_refusal_sync(
        SpooledInbound(channel_type="telegram", conversation_id="1", text="hi"), path=spool
    )
    os.link(spool, tmp_path / "alias.jsonl")

    assert peek_next(path=spool) is None
    assert spool.exists(), "the hard-linked spool was mutated"


def test_every_spool_write_is_owner_only() -> None:
    """Every rewrite goes through ``atomic_write(restrict_to_owner=True)``.

    Another same-UID reader is the threat model, and a rewrite that forgot the
    flag would widen the mode on the very next pass.
    """
    import inspect

    for fn in (S.record_refusal_sync, S.peek_next, S.remove_entry):
        source = inspect.getsource(fn)
        assert source.count("atomic_write(") == source.count("restrict_to_owner=True"), fn.__name__
        assert "restrict_to_owner=True" in source, fn.__name__


def test_the_spool_directory_is_fenced_from_agent_file_tools() -> None:
    """A file an agent could write is a way to post a notice as the gateway.

    Each entry names a conversation and carries text the notice quotes into it
    verbatim. The egress recheck narrows the forge path to conversations still
    authorized, which is not a boundary — the boundary is the file being
    unreachable. Read matters too: an entry holds the verbatim text of a message
    the operator sent.
    """
    from kiro_crew.security import _CREW_SECRET_LEAVES

    assert "inbound-spool" in _CREW_SECRET_LEAVES
    assert spool_path().parent.name == "inbound-spool", (
        "the fence entry is directory-scoped, so the spool must live in that "
        "directory and not merely be named after it"
    )


def test_the_spool_directory_is_masked_in_agent_sandboxes() -> None:
    """The file fence covers tools; this covers a spawned command."""
    from kiro_crew.sandbox import _CREW_HIDDEN_LEAVES

    assert "inbound-spool" in _CREW_HIDDEN_LEAVES


# ── Channel wiring: what each adopter declares as its route ──────────────────


def test_the_spooled_text_is_the_user_message_not_the_model_prompt(
    monkeypatch, spool_home: Path
) -> None:
    """A transformed prompt must never be what gets spooled or quoted back.

    WhatsApp's rules mode prepends the group's private operating rules and its
    silence instructions to the model prompt. Spooling ``ChannelTurn.user_text``
    would put those rules in the entry, and the restart notice quotes the entry
    verbatim — publishing them into the conversation.
    """
    _patch_pipeline(monkeypatch)
    turn = ChannelTurn(
        channel_type="whatsapp",
        session_key="whatsapp:agentA:direct:p1",
        conversation_id="whatsapp:p1",
        agent="agentA",
        user_text="<private rules the operator configured>\n\nwhen is the deploy?",
        renderer=_Renderer(),
        approval_mode="auto",
        inbound_route=InboundRoute(conversation_id="p1", text="when is the deploy?"),
    )

    asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    text = _entries(spool_home)[0]["text"]
    assert text == "when is the deploy?"
    assert "private rules" not in text, "the operator's rules were spooled"


def test_the_turn_prompt_is_never_a_fallback_for_a_route_with_no_text(
    monkeypatch, spool_home: Path
) -> None:
    """An empty route text does NOT reach for ``ChannelTurn.user_text``.

    The fallback looked like a convenience for a channel whose two strings are
    identical, and it was a disclosure: a media-only rules-mode message has an
    EMPTY route text and a ``user_text`` that is the private rules -- so the
    fallback spooled the rules and the notice quoted them. With no attachments and
    no text there is nothing to spool; the prompt is never it.
    """
    _patch_pipeline(monkeypatch)
    turn = ChannelTurn(
        channel_type="whatsapp",
        session_key="whatsapp:agentA:direct:p1",
        conversation_id="whatsapp:p1",
        agent="agentA",
        user_text="<private rules>\n\n",
        renderer=_Renderer(),
        approval_mode="auto",
        inbound_route=InboundRoute(conversation_id="p1"),
    )

    asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    assert not spool_home.exists(), "the rules were spooled via the prompt fallback"


def test_a_media_only_rules_mode_message_spools_no_rules(monkeypatch, spool_home: Path) -> None:
    """Uncaptioned photo, media denied, rules-mode prompt: only the count is kept."""
    _patch_pipeline(monkeypatch)
    turn = ChannelTurn(
        channel_type="whatsapp",
        session_key="whatsapp:agentA:direct:p1",
        conversation_id="whatsapp:p1",
        agent="agentA",
        user_text="<private rules>\n\n",
        renderer=_Renderer(),
        approval_mode="auto",
        inbound_route=InboundRoute(conversation_id="p1", text="", attachments_dropped=1),
    )

    asyncio.run(drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))

    rows = _entries(spool_home)
    assert rows[0]["text"] == "" and rows[0]["attachments_dropped"] == 1
    assert "private rules" not in json.dumps(rows)


def test_weixin_drive_takes_the_pre_ingestion_originals() -> None:
    """Ingestion destroys both values, so ``_drive`` cannot recover them itself.

    ``_ingest_or_refuse`` clears ``inbound.attachments`` and rewrites the text with
    temp paths. Reading either at the dispatch site therefore reported ZERO
    dropped attachments for a media message and would have quoted on-disk temp
    paths into the notice.
    """
    import inspect

    from kiro_crew.weixin.transport_dispatch import WeixinDispatcher

    parameters = inspect.signature(WeixinDispatcher._drive).parameters
    assert "original_text" in parameters
    assert "original_attachments" in parameters


def test_weixin_route_text_has_no_fallback_to_the_ingested_prompt() -> None:
    """``original_text`` only: the ingested form inlines temp paths."""
    import inspect

    from kiro_crew.weixin.transport_dispatch import WeixinDispatcher

    source = inspect.getsource(WeixinDispatcher._drive)
    assert "text=original_text," in source
    assert "original_text or text" not in source


def test_no_transport_still_carries_a_replay_hook() -> None:
    """Re-dispatch was removed on purpose; a stray override would be dead code
    reviewers keep re-deriving gates for."""
    from kiro_crew.discord.transport import DiscordTransport
    from kiro_crew.messaging.transport import MessagingTransport
    from kiro_crew.telegram.transport import TelegramTransport
    from kiro_crew.weixin.transport import WeixinTransport
    from kiro_crew.whatsapp.transport import WhatsAppTransport

    for cls in (
        MessagingTransport,
        TelegramTransport,
        DiscordTransport,
        WeixinTransport,
        WhatsAppTransport,
    ):
        assert not hasattr(cls, "replay_inbound"), cls.__name__
    assert not hasattr(S, "ReplayOutcome")
    assert not hasattr(S, "conversation_moved_on")
    assert S.__all__ == ["InboundRoute", "replay_spooled", "spool_refused_turn"]
