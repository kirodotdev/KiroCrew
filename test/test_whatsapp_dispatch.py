"""WhatsAppDispatcher tests: commands, turn construction, busy/steer, groups.

Drives the reply path end to end with fakes standing in for the ACP provider,
SessionManager, ContextBuilder and transport — an inbound message really does
produce an outbound WhatsApp send through the shared TurnDriver. neonize is
never imported: the client is a fake and the transport is a lightweight stand-in.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.messaging.driver import APPROVAL_AUTO
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL, GroupVerdict
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher


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

    async def stream(self, message: str):
        self.prompts.append(message)
        yield FakeEvent(EVENT_TEXT_CHUNK, self.reply)
        yield FakeEvent(EVENT_COMPLETE)

    def has_active_turn(self) -> bool:
        return False


class FakeSessions:
    def __init__(self, provider: Any = None, busy: bool = False) -> None:
        self.provider = provider or FakeProvider()
        self._busy = busy
        self.released = 0
        self.successes = 0
        self.failures = 0
        self.channels: dict[str, str] = {}

    def is_busy(self, key: str) -> bool:
        return self._busy

    async def get_or_create(self, key, agent=None, channel_id=None):
        return self.provider, True, False

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
    class agent:
        default_agent = "kirocrew"
        approval_mode = "auto"

    class messaging:
        idle_reset_minutes = 0
        daily_reset_hour = -1
        dm_scope = "user"


class FakeGroupGate:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    def record_unprompted_reply(self, scope: str) -> None:
        self.recorded.append(scope)


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.pending_verdicts: dict[int, GroupVerdict] = {}
        self.group_gate = FakeGroupGate()
        self._fail = fail

    async def send_message(self, jid: str, text: str) -> str:
        if self._fail:
            raise RuntimeError("wa send timeout")
        self.sent.append((jid, text))
        return "mid-1"


class FakeClient:
    def __init__(self) -> None:
        self.typing: list[bool] = []

    async def send_typing(self, jid: str, active: bool) -> None:
        self.typing.append(active)


def _make(provider=None, busy=False, transport_fail=False):
    client = FakeClient()
    sessions = FakeSessions(provider=provider, busy=busy)
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
    return InboundMessage(
        channel_type="whatsapp", user_id=user, conversation_id=conv, text=text
    )


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


def test_compact_command_sets_awaiting_without_a_turn():
    provider = FakeProvider()
    d, _client, _sessions, transport = _make(provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert provider.prompts == []
    assert any("compact" in t.lower() for _, t in transport.sent)


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

        def append(self, key, role, text):
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
    transport.pending_verdicts[id(inbound)] = GroupVerdict(
        respond=True, may_steer=False
    )
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
