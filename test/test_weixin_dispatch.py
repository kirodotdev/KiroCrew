"""Dispatcher + renderer tests for the Weixin channel.

Proves the reply path end to end with fakes standing in for the ACP provider,
SessionManager and ContextBuilder — i.e. an inbound message really does produce
an outbound iLink send through the shared TurnDriver.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.messaging.driver import APPROVAL_AUTO
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.weixin.client import ContextTokenStore, TypingTicketCache
from kiro_crew.weixin.commands import parse_command
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
from kiro_crew.weixin.transport_dispatch import WeixinDispatcher
from kiro_crew.weixin.turn_renderer import WeixinRenderer, _strip_options


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeClient:
    def __init__(self):
        self.sent: list[dict] = []
        self.typing: list[int] = []

    async def send_message(self, *, to, text, context_token, client_id):
        self.sent.append({"to": to, "text": text, "context_token": context_token})
        return {"errcode": 0}

    async def get_config(self, *, user_id, context_token):
        return {"typing_ticket": "tk-1"}

    async def send_typing(self, *, to_user_id, typing_ticket, status):
        self.typing.append(status)

    async def connect(self):
        return None

    async def close(self):
        return None


class FakeEvent:
    """Minimal AcpEvent stand-in the TurnDriver understands."""

    def __init__(self, kind, text=""):
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

    def __init__(self, reply="hello from the agent"):
        self.reply = reply
        self.prompts: list[str] = []
        self.compacted = False

    async def stream(self, message):
        self.prompts.append(message)
        from kiro_crew.messaging.driver import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        yield FakeEvent(EVENT_TEXT_CHUNK, self.reply)
        yield FakeEvent(EVENT_COMPLETE)

    def has_active_turn(self):
        return False

    async def compact(self):
        self.compacted = True

    async def wait_for_compaction(self, timeout=0):
        return True


class FakeSessions:
    def __init__(self, provider=None, busy=False):
        self.provider = provider or FakeProvider()
        self._busy = busy
        self.released = 0
        self.successes = 0
        self.failures = 0
        self.channels: dict[str, str] = {}
        self.acquired = False

    def is_busy(self, key):
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

    def check_context_usage(self, key, provider):
        return 10.0

    async def try_acquire(self, key):
        self.acquired = True
        return True

    def has_session(self, key):
        return True

    def max_generation(self, *a, **kw):
        # seed_generation() probes for the highest existing generation; a fresh
        # fake has none.
        return 0


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a, **kw):
        class R:
            action = ""

        return R()


class FakeCtxBuilder:
    def __init__(self):
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

    class weixin:
        soft_threshold_pct = 80
        hard_threshold_pct = 95


def _make(tmp_path, provider=None, busy=False):
    client = FakeClient()
    sessions = FakeSessions(provider=provider, busy=busy)
    d = WeixinDispatcher(
        sessions=sessions,
        ctx_builder=FakeCtxBuilder(),
        cfg=FakeCfg(),
        account_id="acct1",
        ctx_store=ContextTokenStore(str(tmp_path)),
        typing_cache=TypingTicketCache(),
        approval_mode=APPROVAL_AUTO,
    )
    d.client = client
    return d, client, sessions


def _msg(text="hi", user="userA"):
    return InboundMessage(
        channel_type="weixin", user_id=user, conversation_id=user, text=text
    )


# ── renderer ──────────────────────────────────────────────────────────────────
def test_strip_options_removes_dashboard_only_affordance():
    assert _strip_options("answer\n[OPTIONS: a | b]") == "answer"


def test_renderer_buffers_and_sends_once_on_done(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client,
        "userA",
        WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)),
        account_id="acct1",
        typing_cache=TypingTicketCache(),
    )

    async def go():
        await r.on_text_chunk("part one ")
        await r.on_text_chunk("part two")
        assert client.sent == []  # nothing sent mid-turn (iLink can't edit)
        await r.on_done()

    asyncio.run(go())
    assert len(client.sent) == 1
    assert client.sent[0]["text"] == "part one part two"


def test_renderer_on_done_is_idempotent(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)), account_id="acct1",
    )

    async def go():
        await r.on_text_chunk("x")
        await r.on_done()
        await r.on_done()
        await r.close()

    asyncio.run(go())
    assert len(client.sent) == 1


def test_renderer_close_without_done_emits_error_text(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)), account_id="acct1",
    )
    asyncio.run(r.close())
    assert len(client.sent) == 1
    assert "出错" in client.sent[0]["text"]


def test_renderer_strips_options_from_the_reply(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)), account_id="acct1",
    )

    async def go():
        await r.on_text_chunk("answer\n[OPTIONS: a | b]")
        await r.on_done()

    asyncio.run(go())
    assert client.sent[0]["text"] == "answer"


def test_renderer_echoes_the_stored_context_token(tmp_path):
    client = FakeClient()
    store = ContextTokenStore(str(tmp_path))
    store.set("acct1", "userA", "ctx-42")
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES, ctx_store=store, account_id="acct1"
    )

    async def go():
        await r.on_text_chunk("hi")
        await r.on_done()

    asyncio.run(go())
    assert client.sent[0]["context_token"] == "ctx-42"


def test_renderer_chunks_an_oversized_answer(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)), account_id="acct1",
    )

    async def go():
        await r.on_text_chunk("\n\n".join(["z" * 500] * 20))
        await r.on_done()

    asyncio.run(go())
    assert len(client.sent) > 1
    assert all(len(s["text"]) <= WEIXIN_CAPABILITIES.max_message_chars for s in client.sent)


def test_renderer_tool_and_thinking_events_emit_nothing(tmp_path):
    client = FakeClient()
    r = WeixinRenderer(
        client, "userA", WEIXIN_CAPABILITIES,
        ctx_store=ContextTokenStore(str(tmp_path)), account_id="acct1",
    )

    async def go():
        await r.on_thinking("secret reasoning")
        await r.on_tool_call("id", "execute_bash")
        await r.on_compaction(50.0)
        await r.on_prompt_choice([], "")
        await r.on_done()

    asyncio.run(go())
    # One bubble total, and reasoning never leaks into it.
    assert len(client.sent) == 1
    assert "secret reasoning" not in client.sent[0]["text"]


# ── dispatcher ────────────────────────────────────────────────────────────────
def test_dispatcher_drives_a_turn_and_replies(tmp_path):
    """The headline behaviour: inbound message -> agent reply delivered."""
    provider = FakeProvider("42 is the answer")
    d, client, sessions = _make(tmp_path, provider=provider)
    asyncio.run(d.handle_message(_msg("what is 6*7?")))

    assert [s["text"] for s in client.sent] == ["42 is the answer"]
    # The context builder's output is what reached the model.
    assert provider.prompts == ["[ctx]what is 6*7?"]
    assert sessions.successes == 1
    assert sessions.released == 1  # semaphore always released


def test_dispatcher_sets_channel_id_for_a_new_session(tmp_path):
    d, _, sessions = _make(tmp_path)
    asyncio.run(d.handle_message(_msg(user="userB")))
    assert list(sessions.channels.values()) == ["weixin:userB"]


def test_dispatcher_session_key_is_namespaced_and_stable(tmp_path):
    d, _, _ = _make(tmp_path)
    k1 = d._session_key("userA")
    k2 = d._session_key("userA")
    assert k1 == k2
    assert "weixin" in k1


def test_new_command_starts_a_fresh_session_without_a_turn(tmp_path):
    provider = FakeProvider()
    d, client, sessions = _make(tmp_path, provider=provider)
    before = d._session_key("userA")
    asyncio.run(d.handle_message(_msg("/new")))
    after = d._session_key("userA")

    assert provider.prompts == []  # no LLM turn for a command
    assert before != after  # generation advanced
    assert "新对话" in client.sent[0]["text"]


def test_compact_command_compacts_without_a_turn(tmp_path):
    provider = FakeProvider()
    d, client, sessions = _make(tmp_path, provider=provider)
    asyncio.run(d.handle_message(_msg("/compact")))
    assert provider.compacted is True
    assert provider.prompts == []
    assert sessions.released == 1  # acquired for compaction, then released


def test_busy_session_does_not_start_a_second_turn(tmp_path):
    provider = FakeProvider()
    d, client, _ = _make(tmp_path, provider=provider, busy=True)
    asyncio.run(d.handle_message(_msg("second message")))
    # No turn ran; the user was told to wait (provider can't steer).
    assert provider.prompts == []
    assert any("稍后" in s["text"] for s in client.sent)


def test_turn_failure_records_failure_and_still_releases(tmp_path):
    class Boom(FakeProvider):
        async def stream(self, message):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

    d, client, sessions = _make(tmp_path, provider=Boom())
    asyncio.run(d.handle_message(_msg("trigger failure")))

    assert sessions.failures == 1
    assert sessions.released == 1
    # The user still gets a bubble rather than silence.
    assert len(client.sent) == 1
    assert "出错" in client.sent[0]["text"]


def test_delivery_failure_is_not_recorded_as_success(tmp_path):
    """An undelivered reply must fail the turn, not persist as a success.

    Regression: the renderer used to swallow send errors, so a send timeout left
    the dispatcher recording + persisting a reply the user never received.
    """
    rows: list[tuple[str, str]] = []

    class FailingClient(FakeClient):
        async def send_message(self, *, to, text, context_token, client_id):
            raise RuntimeError("ilink send timeout")

    class Log:
        def append(self, key, role, text):
            rows.append((role, text))

        def set_title(self, key, title):
            pass

    d, _, sessions = _make(tmp_path)
    d.client = FailingClient()
    d.conv_log = Log()
    asyncio.run(d.handle_message(_msg("will not be delivered")))

    assert sessions.successes == 0
    assert sessions.failures == 1
    # Never persist an assistant reply that never arrived.
    assert not any(r[0] == "assistant" for r in rows)
    assert sessions.released == 1


def test_persist_turn_writes_user_and_assistant_rows(tmp_path):
    class Log:
        def __init__(self):
            self.rows: list[tuple[str, str]] = []
            self.title = ""

        def append(self, key, role, text):
            self.rows.append((role, text))

        def set_title(self, key, title):
            self.title = title

    log = Log()
    d, _, _ = _make(tmp_path)
    d.conv_log = log
    asyncio.run(d.handle_message(_msg("remember this")))
    assert ("user", "remember this") in log.rows
    assert any(r[0] == "assistant" for r in log.rows)
    assert log.title == "remember this"


def test_parse_command_accepts_chinese_aliases():
    assert parse_command("新对话") == "new"
    assert parse_command("清空") == "new"
    assert parse_command("  /COMPACT  ") == "compact"
    assert parse_command("just chatting") is None


def test_dispatcher_agent_falls_back_to_kirocrew(tmp_path):
    d, _, _ = _make(tmp_path)
    assert d._resolve_agent() == "kirocrew"


def test_governance_deny_drops_the_message_before_any_turn(tmp_path, monkeypatch):
    """A channels-policy deny added AFTER connect must stop dispatch per message.

    The startup gate only blocks CONNECTING, so without this per-message recheck
    an inbound DM would drive an unauthorized turn until the gateway restarted.
    """
    import kiro_crew.messaging.dispatch as mod

    provider = FakeProvider()
    d, client, sessions = _make(tmp_path, provider=provider)

    async def deny(_channel_type):
        return False

    monkeypatch.setattr(mod, "channel_inbound_permitted", deny)
    asyncio.run(d.handle_message(_msg("should be dropped")))

    assert provider.prompts == []  # no turn ran
    assert client.sent == []  # and nothing was sent back
    assert sessions.successes == 0


def test_governance_permit_allows_the_turn(tmp_path, monkeypatch):
    import kiro_crew.messaging.dispatch as mod

    provider = FakeProvider("allowed")
    d, client, _ = _make(tmp_path, provider=provider)

    async def permit(_channel_type):
        return True

    monkeypatch.setattr(mod, "channel_inbound_permitted", permit)
    asyncio.run(d.handle_message(_msg("go")))
    assert [s["text"] for s in client.sent] == ["allowed"]


def test_pipeline_enforces_the_gate_even_if_a_channel_forgets(tmp_path, monkeypatch):
    """drive_turn itself denies — an adopter cannot bypass governance.

    The channel-side check is a fail-fast (it must run before command acks and
    generation bumps, which are side effects a denied sender must not cause).
    This pins the pipeline's own backstop: calling drive_turn directly with a
    denied channel_type must not start the renderer or acquire a session, so a
    future channel that forgets its early check still cannot run a turn.
    """
    import kiro_crew.messaging.dispatch as mod

    provider = FakeProvider()
    d, client, sessions = _make(tmp_path, provider=provider)

    async def deny(_channel_type):
        return False

    monkeypatch.setattr(mod, "channel_inbound_permitted", deny)

    started: list[str] = []

    class SpyRenderer:
        capabilities = WEIXIN_CAPABILITIES

        async def on_turn_start(self):
            started.append("start")

        async def close(self):
            started.append("close")

    asyncio.run(
        mod.drive_turn(
            mod.ChannelTurn(
                channel_type="weixin",
                session_key="weixin:kirocrew:direct:u1",
                conversation_id="weixin:u1",
                agent="kirocrew",
                user_text="should never run",
                renderer=SpyRenderer(),
                approval_mode=APPROVAL_AUTO,
            ),
            sessions=sessions,
            ctx_builder=d.ctx_builder,
        )
    )

    assert started == []  # no typing indicator, no close -> returned before try
    assert provider.prompts == []  # no turn ran
    assert sessions.successes == 0


def test_soft_threshold_notice_fires_once(tmp_path):
    d, client, sessions = _make(tmp_path)
    sessions.check_context_usage = lambda k, p: 85.0  # type: ignore[assignment]
    asyncio.run(d.handle_message(_msg("one")))
    asyncio.run(d.handle_message(_msg("two")))
    notices = [s for s in client.sent if "上下文已较长" in s["text"]]
    assert len(notices) == 1  # nudge is not repeated every turn


def test_hard_threshold_forces_compaction(tmp_path):
    provider = FakeProvider()
    d, client, sessions = _make(tmp_path, provider=provider)
    sessions.check_context_usage = lambda k, p: 99.0  # type: ignore[assignment]
    asyncio.run(d.handle_message(_msg("long convo")))
    assert provider.compacted is True
    assert any("已自动压缩" in s["text"] for s in client.sent)


def test_tool_gate_denies_when_hooks_deny(tmp_path):
    """The security gate is wired: a hook DENY reaches the driver's tool_gate."""
    from kiro_crew.hooks import TOOL_DENY

    d, _, _ = _make(tmp_path)

    class DenyHooks(FakeHooks):
        def on_tool_call(self, *a, **kw):
            class R:
                action = TOOL_DENY

            return R()

    d.ctx_builder.hooks = DenyHooks()
    # Drive a turn so the gate closure is constructed with the deny hook, then
    # assert the closure maps the hook result onto the driver's contract.
    captured: dict[str, Any] = {}

    import kiro_crew.messaging.dispatch as mod

    real_driver = mod.TurnDriver

    class CapturingDriver(real_driver):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            captured["tool_gate"] = kw.get("tool_gate")
            super().__init__(*a, **kw)

    mod.TurnDriver = CapturingDriver  # type: ignore[misc]
    try:
        asyncio.run(d.handle_message(_msg("run a tool")))
    finally:
        mod.TurnDriver = real_driver  # type: ignore[misc]

    gate = captured.get("tool_gate")
    assert gate is not None
    assert gate(FakeEvent("permission")) == "deny"
