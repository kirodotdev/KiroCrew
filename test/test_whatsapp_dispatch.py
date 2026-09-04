"""WhatsAppDispatcher tests: commands, turn construction, busy/steer, groups.

Drives the reply path end to end with fakes standing in for the ACP provider,
SessionManager, ContextBuilder and transport — an inbound message really does
produce an outbound WhatsApp send through the shared TurnDriver. neonize is
never imported: the client is a fake and the transport is a lightweight stand-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.messaging.driver import APPROVAL_AUTO
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.whatsapp.commands import (
    COMPACT_AUTO_MANAGED_TEXT,
    COMPACT_AUTO_TEXT,
    COMPACT_BUSY_TEXT,
    COMPACT_FAILED_TEXT,
    COMPACT_NOTHING_TEXT,
    COMPACTED_TEXT,
    CONTEXT_LONG_TEXT,
)
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL, GroupVerdict
from kiro_crew.whatsapp.transport_dispatch import (
    REACTION_DONE,
    REACTION_FAILED,
    REACTION_WORKING,
    WhatsAppDispatcher,
)


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeEvent:
    def __init__(self, kind: str, text: str = "") -> None:
        self.kind = kind
        self.text = text
        self.title = ""
        self.tool_kind = ""
        self.tool_purpose = ""
        self.tool_call_id = ""
        self.options: list[dict] = []
        self.request_id = ""
        self.context_usage_pct = 0.0
        self.stop_reason = ""
        self.raw_tool_params = None
        self.shell_command = None
        self.is_shell = False


class FakeProvider:
    supports_steer = False

    def __init__(self, reply: str = "hello from the agent") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        #: Compaction is what the threshold path and ``/compact`` must actually
        #: reach, so both halves are recorded separately: a compact that is never
        #: awaited to completion can still be reported as done.
        self.compacts = 0
        self.waits = 0
        self.compact_raises = False

    async def stream(self, message: str):
        self.prompts.append(message)
        yield FakeEvent(EVENT_TEXT_CHUNK, self.reply)
        yield FakeEvent(EVENT_COMPLETE)

    def has_active_turn(self) -> bool:
        return False

    async def compact(self, context: str = "") -> None:
        if self.compact_raises:
            raise RuntimeError("kiro-cli refused the compact")
        self.compacts += 1

    async def wait_for_compaction(self, *a: Any, **kw: Any) -> dict:
        self.waits += 1
        return {}


class FakeSessions:
    def __init__(
        self,
        provider: Any = None,
        busy: bool = False,
        context_pct: float = 0.0,
        acquirable: bool = True,
        session_exists: bool = True,
        persisted_generations: dict[str, int] | None = None,
    ) -> None:
        self.provider = provider or FakeProvider()
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        #: What ``max_generation`` reports per durable bucket, standing in for the
        #: session map on disk. Empty means a machine that has never run this
        #: channel, which is the only case an unseeded counter gets right.
        self.persisted_generations = dict(persisted_generations or {})
        self.generation_lookups: list[str] = []
        self._busy = busy
        self.released = 0
        self.successes = 0
        self.failures = 0
        self.channels: dict[str, str] = {}
        #: What ``check_context_usage`` reports. In production that call is also
        #: the sole trigger for the backend autocompactor, so the keys it was
        #: called with are what proves the channel reaches context accounting at
        #: all -- a threshold notice that never happens is indistinguishable from
        #: a channel with no compaction whatsoever.
        self.context_pct = context_pct
        self.context_checks: list[str] = []
        self._acquirable = acquirable
        self._session_exists = session_exists

    def is_busy(self, key: str) -> bool:
        return self._busy

    def check_context_usage(self, key: str, provider: Any) -> float:
        self.context_checks.append(key)
        return self.context_pct

    async def try_acquire(self, key: str) -> bool:
        return self._acquirable

    def has_session(self, key: str) -> bool:
        return self._session_exists

    def max_generation(self, bucket: str) -> int:
        self.generation_lookups.append(bucket)
        return self.persisted_generations.get(bucket, 0)

    async def get_or_create(self, key, agent=None, channel_id=None):
        return self.provider, True, False

    def begin_turn(self, key: str) -> None:
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key, channel_id):
        self.channels[key] = channel_id

    def record_success(self, key):
        self.successes += 1

    async def record_failure(self, key):
        self.failures += 1

    def release(self, key):
        self.released += 1

    def get_provider(self, key):
        return self.provider

    def get_pid(self, key):
        return None  # skip identity publication in tests


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a, **kw):
        class R:
            action = ""

        return R()


class FakeCtxBuilder:
    def __init__(self) -> None:
        self.hooks = FakeHooks()

    def build_message(self, text, is_new, session_key, **kw):
        return (f"[ctx]{text}", {})


class FakeCfg:
    """Config stand-in whose sections are PER-INSTANCE.

    Nested classes would make ``d.cfg.messaging.dm_scope = "unified"`` a class
    mutation shared by every later test in the module, which is how a suite
    acquires an order dependency that only shows up when something else is added
    ahead of it. Sections are built per instance so a test that repoints one
    cannot reach any other.
    """

    def __init__(self) -> None:
        self.agent = SimpleNamespace(default_agent="kirocrew", approval_mode="auto")
        self.messaging = SimpleNamespace(idle_reset_minutes=0, daily_reset_hour=-1, dm_scope="user")
        # The shipped defaults. WhatsAppConfig.__post_init__ guarantees
        # soft <= hard, so a test never has to reason about an inverted pair.
        self.whatsapp = SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95)


class FakeGroupGate:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    def record_unprompted_reply(self, scope: str) -> None:
        self.recorded.append(scope)


class FakeTransport:
    def __init__(self, fail: bool = False, is_operator: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.pending_verdicts: dict[int, GroupVerdict] = {}
        self.group_gate = FakeGroupGate()
        self.pending_message_id: dict[int, str] = {}
        #: Phase reactions go through the TRANSPORT, not the client: it owns the
        #: echo tracker, because a reaction is a message and echoes back.
        self.reactions: list[tuple[str, str]] = []
        self._fail = fail
        #: Mirrors the real operator-only predicate; a test flips it to stand in
        #: for a group member or a stranger, who must never resolve an approval.
        self._is_operator = is_operator

    async def send_message(self, jid: str, text: str) -> str:
        if self._fail:
            raise RuntimeError("wa send timeout")
        self.sent.append((jid, text))
        return "mid-1"

    async def react(self, jid: str, sender: str, message_id: str, emoji: str) -> bool:
        self.reactions.append((message_id, emoji))
        return True

    def is_operator(self, msg) -> bool:
        return self._is_operator


class FakeClient:
    def __init__(self) -> None:
        self.typing: list[bool] = []
        self.reactions: list[tuple[str, str]] = []

    async def send_typing(self, jid: str, active: bool) -> None:
        self.typing.append(active)

    async def react(self, jid: str, sender: str, message_id: str, emoji: str) -> bool:
        self.reactions.append((message_id, emoji))
        return True


def _make(provider=None, busy=False, transport_fail=False, **session_kwargs):
    client = FakeClient()
    sessions = FakeSessions(provider=provider, busy=busy, **session_kwargs)
    transport = FakeTransport(fail=transport_fail)
    d = WhatsAppDispatcher(
        FakeCfg(),
        sessions,
        FakeCtxBuilder(),
        approval_mode=APPROVAL_AUTO,
    )
    d.client = client
    d.transport = transport
    return d, client, sessions, transport


_DM = "447700900000@s.whatsapp.net"
_GROUP = "12345-67890@g.us"


def _msg(text="hi", conv=_DM, user="447700900000"):
    return InboundMessage(channel_type="whatsapp", user_id=user, conversation_id=conv, text=text)


# ── dispatcher: DM happy path ───────────────────────────────────────────────
def test_dispatcher_drives_a_turn_and_replies():
    provider = FakeProvider("42 is the answer")
    d, _client, sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("what is 6*7?")))

    assert [t for _, t in transport.sent] == ["42 is the answer"]
    assert provider.prompts == ["[ctx]what is 6*7?"]
    assert sessions.successes == 1
    assert sessions.released == 1


def test_dispatcher_sets_channel_id_for_a_new_session():
    d, _client, sessions, _transport = _make()
    asyncio.run(d.handle_message(_msg()))
    assert list(sessions.channels.values()) == [f"whatsapp:{_DM}"]


def test_session_key_is_namespaced_and_stable():
    d, _client, _sessions, _transport = _make()
    k1 = d._session_key(_DM)
    k2 = d._session_key(_DM)
    assert k1 == k2
    assert "whatsapp" in k1


def test_group_scope_uses_forum_chat_type_in_session_key():
    d, _client, _sessions, _transport = _make()
    assert d._session_key(_GROUP) != d._session_key(_DM)


# ── dispatcher: commands ────────────────────────────────────────────────────
def test_new_command_starts_a_fresh_session_without_a_turn():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    before = d._session_key(_DM)
    asyncio.run(d.handle_message(_msg("/new")))
    after = d._session_key(_DM)

    assert provider.prompts == []  # no LLM turn for a command
    assert before != after  # generation advanced
    assert any("fresh session" in t.lower() for _, t in transport.sent)


def test_compact_command_compacts_in_place_without_a_turn():
    provider = FakeProvider()
    d, _client, sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert provider.prompts == []  # no LLM turn for a command
    assert (provider.compacts, provider.waits) == (1, 1)
    assert [t for _, t in transport.sent] == [COMPACTED_TEXT]
    assert sessions.released == 1, "the turn semaphore must always be handed back"


def test_compact_command_declined_on_auto_managed_backend():
    # A backend that cannot serve /compact gets the informational reply and
    # compact() is NEVER dispatched (#8156).
    provider = FakeProvider()
    provider.manual_compact_unsupported_backend = "kas"
    d, _client, sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert (provider.compacts, provider.waits) == (0, 0)
    assert [t for _, t in transport.sent] == [COMPACT_AUTO_MANAGED_TEXT]
    assert sessions.released == 1, "the turn semaphore must always be handed back"


def test_compact_none_capability_preserves_dispatch():
    # The ABC's None (supported) default keeps the existing dispatch.
    provider = FakeProvider()
    provider.manual_compact_unsupported_backend = None
    d, _client, _sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert (provider.compacts, provider.waits) == (1, 1)
    assert [t for _, t in transport.sent] == [COMPACTED_TEXT]


def test_the_hard_threshold_declines_silently_on_auto_managed_backend():
    # No /compact to dispatch and no notice: the backend compacts on its own
    # as context fills (#8156).
    provider = FakeProvider("answered")
    provider.manual_compact_unsupported_backend = "kas"
    d, _client, _sessions, transport = _make(provider=provider, context_pct=96.0)
    asyncio.run(d.handle_message(_msg("a long conversation")))
    assert (provider.compacts, provider.waits) == (0, 0)
    assert [t for _, t in transport.sent] == ["answered"]


def test_the_soft_nudge_is_suppressed_on_auto_managed_backend():
    # The nudge advises /compact, which this backend refuses — it compacts on
    # its own, so there is nothing for the user to act on (#8156).
    provider = FakeProvider("answered")
    provider.manual_compact_unsupported_backend = "kas"
    d, _client, _sessions, transport = _make(provider=provider, context_pct=85.0)
    asyncio.run(d.handle_message(_msg("first")))
    assert [t for _, t in transport.sent] == ["answered"]


# ── dispatcher: busy / steering ─────────────────────────────────────────────
def test_busy_session_without_steer_asks_to_resend():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider, busy=True)
    asyncio.run(d.handle_message(_msg("second message")))
    assert provider.prompts == []
    assert any("resend" in t.lower() for _, t in transport.sent)


def test_busy_session_folds_into_current_reply_when_steerable():
    class Steering(FakeProvider):
        supports_steer = True

        def __init__(self):
            super().__init__()
            self.steered: list[str] = []

        def has_active_turn(self):
            return True

        async def steer(self, text):
            self.steered.append(text)
            return True

    provider = Steering()
    d, _client, _sessions, transport = _make(provider=provider, busy=True)
    asyncio.run(d.handle_message(_msg("more context")))
    assert provider.steered == ["more context"]
    assert any("folded" in t.lower() for _, t in transport.sent)


def test_busy_flips_free_reprocesses_the_message():
    """If the session frees between is_busy checks, the message is re-handled."""
    provider = FakeProvider("late reply")

    class Flaky(FakeSessions):
        def __init__(self):
            super().__init__(provider=provider)
            self._calls = 0

        def is_busy(self, key):
            self._calls += 1
            # busy for the _drive gate, then free for _handle_busy's recheck.
            return self._calls == 1

    d = WhatsAppDispatcher(FakeCfg(), Flaky(), FakeCtxBuilder(), approval_mode=APPROVAL_AUTO)
    d.client = FakeClient()
    transport = FakeTransport()
    d.transport = transport
    asyncio.run(d.handle_message(_msg("hello")))
    assert [t for _, t in transport.sent] == ["late reply"]


# ── dispatcher: governance + failures ───────────────────────────────────────
def test_governance_deny_drops_before_any_turn(monkeypatch):
    import kiro_crew.messaging.dispatch as mod

    async def deny(_ct):
        return False

    monkeypatch.setattr(mod, "channel_inbound_permitted", deny)
    provider = FakeProvider()
    d, _client, sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("dropped")))
    assert provider.prompts == []
    assert transport.sent == []
    assert sessions.successes == 0


def test_delivery_failure_records_failure_not_success():
    provider = FakeProvider("undelivered")
    d, _client, sessions, _transport = _make(provider=provider, transport_fail=True)
    asyncio.run(d.handle_message(_msg("will fail to send")))
    assert sessions.successes == 0
    assert sessions.failures == 1
    assert sessions.released == 1


def test_provider_exception_records_failure_and_releases():
    class Boom(FakeProvider):
        async def stream(self, message):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

    d, _client, sessions, transport = _make(provider=Boom())
    asyncio.run(d.handle_message(_msg("trigger failure")))
    assert sessions.failures == 1
    assert sessions.released == 1
    # close() still flushes an error bubble to the user.
    assert transport.sent and "went wrong" in transport.sent[-1][1].lower()


# ── dispatcher: persistence ─────────────────────────────────────────────────
def test_persist_turn_writes_user_and_assistant_rows():
    class Log:
        def __init__(self):
            self.rows: list[tuple[str, str]] = []
            self.title = ""

        def append(self, key, role, text, mid=None):
            self.rows.append((role, text))

        def set_title(self, key, title):
            self.title = title

    log = Log()
    d, _client, _sessions, _transport = _make(provider=FakeProvider("stored"))
    d.conv_log = log
    asyncio.run(d.handle_message(_msg("remember this")))
    assert ("user", "remember this") in log.rows
    assert ("assistant", "stored") in log.rows
    assert log.title == "remember this"


def test_persist_turn_is_a_noop_without_a_conv_log():
    d, _client, sessions, _transport = _make()
    d.conv_log = None
    # Exercised via the real turn path; must not raise.
    asyncio.run(d.handle_message(_msg("no log configured")))
    assert sessions.successes == 1


# ── dispatcher: group / unprompted turns ────────────────────────────────────
def test_unprompted_group_reply_injects_rules_and_records_cooldown():
    provider = FakeProvider("here is help")
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("anyone know python?", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, unprompted=True, rules="Help with python questions.", may_steer=False
    )
    asyncio.run(d.handle_message(inbound))

    # The silence contract + rules were prepended to what reached the model.
    assert provider.prompts and "Help with python questions." in provider.prompts[0]
    assert "anyone know python?" in provider.prompts[0]
    # A delivered unprompted reply starts the group cooldown.
    assert transport.group_gate.recorded == [_GROUP]
    assert [t for _, t in transport.sent] == ["here is help"]


def test_unprompted_group_silence_delivers_nothing_and_skips_cooldown():
    provider = FakeProvider(SILENCE_SENTINEL)
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("off-topic chatter", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, unprompted=True, rules="Only answer python.", may_steer=False
    )
    asyncio.run(d.handle_message(inbound))

    assert transport.sent == []  # sentinel suppressed
    assert transport.group_gate.recorded == []  # no cooldown started


def test_group_command_from_non_operator_is_ignored():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("/new", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(respond=True, may_steer=False)
    asyncio.run(d.handle_message(inbound))
    assert provider.prompts == []
    assert transport.sent == []


def test_group_command_from_operator_runs():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("/new", conv=_GROUP, user="447700900000")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(respond=True, may_steer=True)
    asyncio.run(d.handle_message(inbound))
    assert provider.prompts == []
    assert any("fresh session" in t.lower() for _, t in transport.sent)


def test_say_swallows_out_of_band_send_errors():
    d, _client, _sessions, _transport = _make()
    d.transport = FakeTransport(fail=True)
    # _say catches and logs; must not raise.
    asyncio.run(d._say(_DM, "hi"))


def test_say_is_a_noop_when_transport_is_missing():
    d, _client, _sessions, _transport = _make()
    d.transport = None
    asyncio.run(d._say(_DM, "hi"))


def test_a_dm_command_from_a_non_operator_is_ignored():
    """Reachable on shipped config, and it costs the operator their session.

    ``messaging.dm_scope = "unified"`` collapses every direct DM into one
    ``unified:{agent}`` bucket, so with ``dm_policy`` ``allowlist`` or ``open``
    an admitted peer shares the operator's session key. There is no group
    verdict on a DM, so the steer gate alone says yes and the peer's ``/new``
    would bump the generation on the conversation the operator is using.
    """
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    transport._is_operator = False  # an allow-listed peer, not the linked account
    inbound = _msg("/new", user="447711111111")
    asyncio.run(d.handle_message(inbound))
    assert provider.prompts == []
    assert transport.sent == [], "a non-operator got a command receipt"


def test_a_dm_command_from_the_operator_still_runs():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("/new", user="447700900000")
    asyncio.run(d.handle_message(inbound))
    assert provider.prompts == []
    assert transport.sent, "the operator's own /new was swallowed"


def test_a_muted_unprompted_group_turn_starts_no_cooldown():
    """The cooldown must follow DELIVERY, not the renderer's own bookkeeping.

    ``drive_turn`` substitutes ``SilentRenderer`` into its LOCAL name when the
    conversation is muted, so this renderer's ``on_done`` never runs and its
    ``suppressed`` flag stays False. Gating on ``not suppressed`` therefore
    started a cooldown for a reply nobody received, which then silenced the next
    unprompted turn that actually had something to say.
    """
    provider = FakeProvider("a real answer")
    d, _client, _sessions, transport = _make(provider=provider)
    inbound = _msg("anything", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, may_steer=False, unprompted=True, rules="be helpful"
    )
    import kiro_crew.messaging.dispatch as dispatch_mod

    # Mute the conversation exactly the way the dashboard disconnect does.
    original = dispatch_mod.conversation_is_muted
    dispatch_mod.conversation_is_muted = lambda sessions, turn: True
    try:
        asyncio.run(d.handle_message(inbound))
    finally:
        dispatch_mod.conversation_is_muted = original

    assert transport.sent == [], "a muted conversation must receive nothing"
    assert (
        transport.group_gate.recorded == []
    ), "a cooldown was recorded for a reply that was never delivered"


def test_a_failed_send_starts_no_cooldown():
    """Same rule from the other direction: the send raised, so nothing landed."""
    provider = FakeProvider("a real answer")
    d, _client, _sessions, transport = _make(provider=provider, transport_fail=True)
    inbound = _msg("anything", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, may_steer=False, unprompted=True, rules="be helpful"
    )
    asyncio.run(d.handle_message(inbound))
    assert transport.group_gate.recorded == []


def _captured_turn(monkeypatch, dispatcher, inbound):
    """The ChannelTurn the dispatcher hands the shared pipeline.

    Asserted directly because approval mode has no cheaper observable: its effect
    is whether a permission request is approved, which needs a provider driving
    the full ladder. What matters here is the dispatcher's CHOICE.
    """
    import kiro_crew.whatsapp.transport_dispatch as mod

    seen = {}

    async def fake_drive_turn(turn, **kwargs):
        seen["turn"] = turn

    monkeypatch.setattr(mod, "drive_turn", fake_drive_turn)
    asyncio.run(dispatcher.handle_message(inbound))
    return seen.get("turn")


def test_a_non_operator_turn_never_inherits_auto_approval(monkeypatch):
    """`auto` is the operator's grant, not an admitted stranger's.

    dm_policy="open" admits anyone and a configured group admits its members. If
    the configured mode were passed through for them, their turn would
    auto-approve tool calls on the operator's machine.
    """
    d, _client, _sessions, transport = _make()
    d.approval_mode = APPROVAL_AUTO
    transport._is_operator = False
    turn = _captured_turn(monkeypatch, d, _msg("do something", user="447711111111"))
    assert turn is not None
    assert (
        turn.approval_mode == "interactive"
    ), "a non-operator's turn must fall back to the driver's deny-by-default"
    assert turn.decider is None, "and it must get no decider to answer with"


def test_the_operator_keeps_the_configured_approval_mode(monkeypatch):
    d, _client, _sessions, transport = _make()
    d.approval_mode = APPROVAL_AUTO
    turn = _captured_turn(monkeypatch, d, _msg("do something", user="447700900000"))
    assert turn is not None and turn.approval_mode == APPROVAL_AUTO


def test_a_non_operator_cannot_steer_the_running_turn():
    """Steering injects text into a turn ALREADY RUNNING. Under a unified DM
    scope that turn is the operator's, so an admitted peer must not redirect it.
    """

    class Steerable:
        supports_steer = True

        def __init__(self) -> None:
            self.steered: list[str] = []

        def has_active_turn(self) -> bool:
            return True

        async def steer(self, text: str) -> bool:
            self.steered.append(text)
            return True

    provider = Steerable()
    d, _client, _sessions, transport = _make(provider=provider, busy=True)
    transport._is_operator = False
    asyncio.run(d.handle_message(_msg("actually do this instead", user="447711111111")))
    assert provider.steered == [], "a non-operator steered the operator's turn"
    assert transport.sent, "they should still get the busy receipt"


def test_the_operator_can_still_steer():
    class Steerable:
        supports_steer = True

        def __init__(self) -> None:
            self.steered: list[str] = []

        def has_active_turn(self) -> bool:
            return True

        async def steer(self, text: str) -> bool:
            self.steered.append(text)
            return True

    provider = Steerable()
    d, _client, _sessions, transport = _make(provider=provider, busy=True)
    asyncio.run(d.handle_message(_msg("also check the logs", user="447700900000")))
    assert provider.steered == ["also check the logs"]


def test_a_non_operator_turn_denies_every_tool(monkeypatch):
    """Deny-by-default via the approval MODE is not sufficient on its own.

    The PreToolUse hook can answer `auto_approve` and a session carrying Trust
    short-circuits, both ahead of the interactive ladder. An untrusted sender's
    turn therefore carries the explicit switch as well: they can talk to the
    agent, but they cannot make it act.
    """
    d, _client, _sessions, transport = _make()
    transport._is_operator = False
    turn = _captured_turn(monkeypatch, d, _msg("read my files", user="447711111111"))
    assert turn is not None and turn.deny_all_tools is True


def test_the_operator_turn_does_not_deny_tools(monkeypatch):
    d, _client, _sessions, transport = _make()
    turn = _captured_turn(monkeypatch, d, _msg("read my files", user="447700900000"))
    assert turn is not None and turn.deny_all_tools is False


def test_a_non_operator_never_shares_the_operators_unified_session():
    """`dm_scope="unified"` collapses every direct DM into one bucket, which is
    right for the operator (WhatsApp and the dashboard are one conversation) and
    wrong for anyone else: the bucket carries the operator's history, so an
    admitted peer could ask what was discussed and be told.
    """
    d, _client, _sessions, transport = _make()
    d.cfg.messaging.dm_scope = "unified"
    operator_msg = _msg("hi", user="447700900000")
    peer_msg = _msg("hi", conv="447711111111@s.whatsapp.net", user="447711111111")

    operator_key = d._session_key(operator_msg.conversation_id, is_operator=True)
    peer_key = d._session_key(peer_msg.conversation_id, is_operator=False)

    assert operator_key.startswith("unified:"), "the operator keeps cross-surface continuity"
    assert not peer_key.startswith("unified:"), "a peer must not land in the shared bucket"
    assert operator_key != peer_key


# ── phase reactions: the marker must report the OUTCOME ─────────────────────
def _react_msg(d, text="hi", conv=_DM, user="447700900000"):
    """An inbound message the transport can already draw a reaction on.

    The real transport records the platform message id during dispatch; the fake
    starts empty, and `_react` returns early without an id, so a reaction test
    that skips this asserts on silence and passes against anything.
    """
    msg = _msg(text, conv=conv, user=user)
    d.transport.pending_message_id[id(msg)] = "mid-op"
    return msg


def test_a_successful_turn_draws_the_success_reaction():
    d, _client, _sessions, transport = _make(provider=FakeProvider("done"))
    asyncio.run(d.handle_message(_react_msg(d)))
    assert [e for _, e in transport.reactions] == [REACTION_WORKING, REACTION_DONE]


def test_a_failed_turn_draws_the_failure_reaction_despite_delivering_the_notice():
    """An errored turn still SENDS something: the apology notice. So the
    renderer's ``delivered`` flag is True by the time the dispatcher reads it,
    and a marker derived from it alone stamps the failure with a success tick.
    """

    class Exploding(FakeProvider):
        async def stream(self, message: str):
            self.prompts.append(message)
            raise RuntimeError("provider died mid-turn")
            yield  # pragma: no cover - generator marker

    d, _client, _sessions, transport = _make(provider=Exploding())
    asyncio.run(d.handle_message(_react_msg(d, "do the thing")))

    assert transport.sent, "the operator is told the turn failed"
    assert [e for _, e in transport.reactions] == [REACTION_WORKING, REACTION_FAILED]


def test_a_muted_conversation_draws_no_reactions_at_all():
    """Mute swaps in ``SilentRenderer``, so this renderer's flags describe
    nothing that was sent. Reacting anyway would answer a silence the operator
    asked for with a warning sign.
    """

    class Muted(FakeSessions):
        def is_mirror_paused(self, key, *, origin=False):
            return True

    d, _client, _sessions, transport = _make()
    d.sessions = Muted(provider=FakeProvider("done"))
    asyncio.run(d.handle_message(_react_msg(d)))
    assert transport.reactions == []


# ── /compact: the receipt has to describe what actually happened ─────────────
def test_compact_command_on_a_busy_session_asks_for_a_retry():
    """A refused acquire with a live session means a turn holds the provider.

    Compacting through it would interleave JSON-RPC on one provider and race the
    transcript, so the command declines -- and says so, because the operator's
    next move (ask again) differs from the no-session case (nothing to wait for).
    """
    provider = FakeProvider()
    d, _client, sessions, transport = _make(
        provider=provider, acquirable=False, session_exists=True
    )
    asyncio.run(d.handle_message(_msg("/compact")))
    assert provider.compacts == 0
    assert [t for _, t in transport.sent] == [COMPACT_BUSY_TEXT]
    assert sessions.released == 0, "nothing was acquired, so nothing may be released"


def test_compact_command_without_a_session_says_there_is_nothing_to_compact():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(
        provider=provider, acquirable=False, session_exists=False
    )
    asyncio.run(d.handle_message(_msg("/compact")))
    assert provider.compacts == 0
    assert [t for _, t in transport.sent] == [COMPACT_NOTHING_TEXT]


def test_compact_command_reports_a_failure_and_still_releases():
    provider = FakeProvider()
    provider.compact_raises = True
    d, _client, sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert [t for _, t in transport.sent] == [COMPACT_FAILED_TEXT]
    assert sessions.released == 1, "a failed compaction must not strand the semaphore"


# ── post-turn context accounting (ChannelTurn.notice) ───────────────────────
def test_every_turn_reaches_context_accounting():
    """``notice`` is the channel's ONLY reach into ``check_context_usage``.

    That call is the sole trigger for the backend autocompactor, so a
    ``ChannelTurn`` built without ``notice=`` leaves the channel with no
    compaction of any kind -- neither the threshold nudge nor the background one
    -- and nothing anywhere reports it.
    """
    d, _client, sessions, _transport = _make(provider=FakeProvider("answered"))
    asyncio.run(d.handle_message(_msg("a normal message")))
    assert sessions.context_checks == [d._session_key(_DM)]


def test_the_hard_threshold_compacts_and_reports_it_afterwards():
    provider = FakeProvider("answered")
    d, _client, _sessions, transport = _make(provider=provider, context_pct=96.0)
    asyncio.run(d.handle_message(_msg("a long conversation")))
    assert (provider.compacts, provider.waits) == (1, 1)
    assert [t for _, t in transport.sent] == ["answered", COMPACT_AUTO_TEXT]


def test_a_failed_hard_threshold_compaction_claims_nothing():
    provider = FakeProvider("answered")
    provider.compact_raises = True
    d, _client, _sessions, transport = _make(provider=provider, context_pct=96.0)
    asyncio.run(d.handle_message(_msg("a long conversation")))
    assert [t for _, t in transport.sent] == ["answered"]


def test_the_soft_threshold_nudges_once_per_conversation():
    """One nudge, not one per turn: past the soft threshold every subsequent
    turn is also past it, so an unguarded notice would append the same paragraph
    to every reply until the operator acted on it."""
    provider = FakeProvider("answered")
    d, _client, _sessions, transport = _make(provider=provider, context_pct=85.0)
    asyncio.run(d.handle_message(_msg("first")))
    asyncio.run(d.handle_message(_msg("second")))
    assert provider.compacts == 0, "the soft threshold nudges, it does not compact"
    assert [t for _, t in transport.sent].count(CONTEXT_LONG_TEXT) == 1


def test_compacting_rearms_the_soft_nudge():
    """``/compact`` clears the flag, so the nudge can fire again once the
    context refills -- otherwise one compaction silences the warning forever."""
    provider = FakeProvider("answered")
    d, _client, _sessions, transport = _make(provider=provider, context_pct=85.0)
    asyncio.run(d.handle_message(_msg("first")))
    asyncio.run(d.handle_message(_msg("/compact")))
    asyncio.run(d.handle_message(_msg("second")))
    assert [t for _, t in transport.sent].count(CONTEXT_LONG_TEXT) == 2


def test_an_unprompted_group_turn_compacts_but_stays_silent():
    """The compaction is session hygiene and runs; the NOTICE would break the
    silence contract, which exists so the agent can stay out of a conversation
    it was not addressed in."""
    provider = FakeProvider("here is help")
    d, _client, sessions, transport = _make(provider=provider, context_pct=96.0)
    inbound = _msg("anyone know python?", conv=_GROUP, user="447711111111")
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, unprompted=True, rules="Help with python questions.", may_steer=False
    )
    asyncio.run(d.handle_message(inbound))

    assert sessions.context_checks, "context accounting still has to run"
    assert (provider.compacts, provider.waits) == (1, 1)
    assert [t for _, t in transport.sent] == ["here is help"]


def test_a_muted_conversation_gets_the_compaction_but_no_notice():
    """Mute is a delivery switch, not a licence to let the window overflow: the
    session still gets compacted, and the operator is simply not told."""

    class Muted(FakeSessions):
        def is_mirror_paused(self, key, *, origin=False):
            return True

    provider = FakeProvider("answered")
    d, _client, _sessions, transport = _make()
    d.sessions = Muted(provider=provider, context_pct=96.0)
    asyncio.run(d.handle_message(_msg("a long conversation")))

    assert d.sessions.context_checks, "context accounting still has to run"
    assert (provider.compacts, provider.waits) == (1, 1)
    assert transport.sent == [], "a muted conversation must receive nothing"


def test_a_suppressed_soft_nudge_is_not_spent():
    """The awaiting flag records that a nudge WAS SENT.

    Setting it while the text is suppressed would spend the single nudge this
    conversation gets on a message nobody read, so the operator would never be
    warned once delivery came back.
    """

    class Muted(FakeSessions):
        muted = True

        def is_mirror_paused(self, key, *, origin=False):
            return self.muted

    provider = FakeProvider("answered")
    d, _client, _sessions, transport = _make()
    sessions = Muted(provider=provider, context_pct=85.0)
    d.sessions = sessions
    asyncio.run(d.handle_message(_msg("first, while muted")))
    assert transport.sent == []

    sessions.muted = False
    asyncio.run(d.handle_message(_msg("second, after unmuting")))
    assert CONTEXT_LONG_TEXT in [t for _, t in transport.sent]


# ── a non-operator never receives the operator's assembled context ──────────
def test_a_non_operator_turn_carries_only_minimal_context(monkeypatch):
    """Denying their TOOLS does not close this: the disclosure is in the PROMPT.

    ``dm_policy="open"`` admits a stranger and a configured group admits its
    members. Their turn is assembled by the same context builder, so without
    this switch the operator's memory, lessons, skills and history are in the
    prompt before any tool runs and can simply be read back in the reply.
    """
    d, _client, _sessions, transport = _make()
    transport._is_operator = False
    turn = _captured_turn(
        monkeypatch, d, _msg("what have you been working on?", user="447711111111")
    )
    assert turn is not None and turn.minimal_context is True


def test_the_operator_turn_carries_their_full_context(monkeypatch):
    d, _client, _sessions, _transport = _make()
    turn = _captured_turn(monkeypatch, d, _msg("what were we doing?", user="447700900000"))
    assert turn is not None and turn.minimal_context is False


def test_the_operators_own_group_turn_is_also_minimal(monkeypatch):
    """The property belongs to the SESSION, not to the sender.

    A group's key is the group, so one session serves every member. Keying this on
    the sender alone still leaked one turn later: the operator addressing the agent
    in a group injected their memory, lessons and skills into that shared session,
    and ACP replays native history, so a member's own minimal-context turn could be
    answered out of it. A group turn is therefore minimal for everyone, including
    the operator.
    """
    d, _client, _sessions, transport = _make()
    d.transport.group_gate.verdict = GroupVerdict(respond=True, may_steer=True)
    transport.pending_verdicts.clear()
    turn = _captured_turn(
        monkeypatch,
        d,
        _msg("what were we doing?", conv=_GROUP, user="447700900000"),
    )
    assert turn is not None, "the operator's group turn should have been driven"
    assert turn.minimal_context is True


# ── restart safety: the generation counter is seeded from disk ──────────────
def test_a_persisted_generation_seeds_the_counter():
    """A gateway restart must not resurrect a conversation ``/new`` discarded.

    The counter is in-memory. Unseeded it restarts at 0, so the next ``/new``
    advances 0 -> 1 and lands straight back on the ``:gen1`` already on disk,
    resuming the history the operator explicitly threw away.
    """
    d, _client, sessions, _transport = _make()
    bucket = "whatsapp:kirocrew:direct:" + _DM
    sessions.persisted_generations[bucket] = 1

    assert d._conv.current_gen(_DM) == 1, "the on-disk generation was not read"
    assert bucket in sessions.generation_lookups
    # And /new must advance PAST it rather than colliding with it.
    assert d._conv.bump_gen(_DM) == 2


def test_a_group_scope_seeds_from_its_forum_bucket():
    """A group keeps its full forum bucket whatever ``dm_scope`` says, so the
    seed has to ask about that bucket -- reading a direct-chat one answers 0 for
    a conversation that has generations on disk."""
    d, _client, sessions, _transport = _make()
    sessions.persisted_generations["whatsapp:kirocrew:forum:" + _GROUP] = 3
    assert d._conv.current_gen(_GROUP) == 3


def test_the_operators_unified_bucket_is_the_one_seeded():
    """Under ``dm_scope="unified"`` the operator's conversation lives in
    ``unified:{agent}``. Seeding from a per-peer bucket would read 0 and put the
    resurrection straight back."""
    d, _client, sessions, _transport = _make()
    d.cfg.messaging.dm_scope = "unified"
    sessions.persisted_generations["unified:kirocrew"] = 4
    assert d._conv.current_gen(_DM) == 4


def test_a_fresh_machine_still_starts_at_generation_zero():
    d, _client, _sessions, _transport = _make()
    assert d._conv.current_gen(_DM) == 0
