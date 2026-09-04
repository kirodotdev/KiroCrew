"""Unit tests for ``handle_message_transport`` agent resolution (Stage 3).

Locks the messaging-transport fix that makes ``spawn_run`` available: the
transport session must be created under a kiro agent that carries the
``kirocrew-core`` MCP server (which provides ``spawn_run``), not under
kiro-cli's bare built-in default.

Directly asserting "the ``spawn_run`` tool is loaded" would require spawning a
real kiro-cli process, so we lock the deterministic invariant that guarantees
it instead: the agent name passed to ``get_or_create`` is non-empty and
resolves to the canonical ``"kirocrew"`` agent when no thread override /
``default_agent`` is configured, and a thread override still wins.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
)
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import transport_dispatch

# Reuse the golden module's fakes without triggering the stdlib 'test' collision.
_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_MSG_TS = "1700000000.000100"


class _CapturingSessions(FakeSessions):
    """FakeSessions that records the ``agent`` passed to get_or_create.

    ``agents`` records TURN acquisitions only. The shared background session is
    tracked separately in ``background_keys``: a successful turn now also fires
    the fire-and-forget auto-title, which takes that session, and folding its
    acquire into ``agents`` would turn every "exactly one acquire, under this
    agent" assertion into a count of unrelated background work.
    """

    def __init__(self, provider):
        super().__init__(provider)
        self.agents: list = []
        self.background_keys: list = []

    async def get_or_create(self, session_key, agent=None, channel_id=None):
        from kiro_crew.session import BACKGROUND_KEY

        if session_key == BACKGROUND_KEY:
            self.background_keys.append(session_key)
        else:
            self.agents.append(agent)
        return await super().get_or_create(session_key, agent=agent, channel_id=channel_id)


def _run_transport(monkeypatch, thread_agent=None, agent_override=None):
    # Empty configured default -> exercises the canonical-agent fallback.
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)

    thread_map: dict = {}
    if thread_agent is not None:
        # Thread overrides are keyed by the canonical namespaced session key
        # (slack:<ts>), matching handle_message/handle_message_transport
        # derivation since the session-key canonicalization fix.
        thread_map[canonical_key(_MSG_TS)] = thread_agent
    monkeypatch.setattr(transport_dispatch, "_thread_agents", thread_map)

    slack = RecordingSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _CapturingSessions(provider)

    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=None,
            agent_override=agent_override,
        )
    )
    return sessions


class TestTransportAgentResolution:
    def test_falls_back_to_kirocrew_agent(self, monkeypatch):
        sessions = _run_transport(monkeypatch)
        # Empty default_agent must NOT pass None (kiro built-in, no
        # kirocrew-core -> no spawn_run); it resolves to canonical "kirocrew".
        assert sessions.agents == [transport_dispatch._DEFAULT_KIROCREW_AGENT]
        assert sessions.agents == ["kirocrew"]

    def test_thread_override_wins(self, monkeypatch):
        sessions = _run_transport(monkeypatch, thread_agent="kirocrew-research")
        assert sessions.agents == ["kirocrew-research"]

    def test_channel_override_used_when_no_thread_override(self, monkeypatch):
        # Per-channel agent (slack.channels.<id>.agent) is honored on transport.
        sessions = _run_transport(monkeypatch, agent_override="ops-agent")
        assert sessions.agents == ["ops-agent"]

    def test_thread_override_beats_channel_override(self, monkeypatch):
        sessions = _run_transport(
            monkeypatch, thread_agent="kirocrew-research", agent_override="ops-agent"
        )
        assert sessions.agents == ["kirocrew-research"]

    def test_channels_deny_drops_transport_message_before_session(self, monkeypatch, tmp_path):
        # HIGH (GPT round-8): a channels policy that denies slack must stop
        # handle_message_transport BEFORE it acquires a session — removing the gate
        # would let a denied transport message start a turn. Regression-locks the
        # transport call site (distinct from the native handle_message gate).
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["discord"]}},
                }
            )
        )
        try:
            sessions = _run_transport(monkeypatch)
            # Gate dropped the message before session acquisition.
            assert (
                sessions.agents == []
            ), "denied slack transport message must not acquire a session"
        finally:
            gp.reset_store()


class TestTransportBookkeepingIsolation:
    """A raise in the final success SEL audit must not fall through to the
    outer except and re-record the already-successful turn as a failure."""

    def test_success_audit_raise_does_not_record_failure(self, monkeypatch):
        from unittest.mock import MagicMock

        calls = {"success": 0, "failure": 0}

        class _TrackSessions(FakeSessions):
            def record_success(self, key):
                calls["success"] += 1

            async def record_failure(self, key):
                calls["failure"] += 1

        # Make ONLY the final success audit raise (leave other sel calls inert).
        def _sel_factory():
            obj = MagicMock()

            def _log(**kw):
                if kw.get("operation") == "transport_dispatch.handle":
                    raise RuntimeError("disk full")

            obj.log_api_access.side_effect = _log
            return obj

        monkeypatch.setattr(transport_dispatch, "sel", _sel_factory)
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _TrackSessions(provider)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
            )
        )
        # Turn recorded success once; the audit failure was swallowed.
        assert calls == {"success": 1, "failure": 0}


# ── Keyword commands on the transport path (spawn/run/cron/sessions) ──
from kiro_crew.slack import handler as _handler  # noqa: E402


class _FakeSubagentMgr:
    """spawn list -> manager.running (empty -> 'No subagents running.')."""

    running: list = []


class _FakeTaskRunner:
    """task run status -> idle status -> 'No task running.'."""

    running = False

    def status(self):
        return {}


class _FakeCronService:
    """cron list -> no jobs -> 'No cron jobs scheduled.'."""

    def list_jobs(self, include_disabled=False):
        return []


def _run_transport_text(
    monkeypatch,
    text,
    *,
    user_id="U_OWNER",
    subagent_manager=None,
    task_runner=None,
    cron_service=None,
):
    """Drive ``handle_message_transport`` with the keyword-command services.

    Returns ``(slack, sessions)``. ``sessions.agents == []`` proves NO LLM
    session was acquired — i.e. the message was intercepted as a keyword
    command and no LLM turn ran. A non-empty ``agents`` means the message fell
    through to the normal LLM turn.
    """
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    slack = RecordingSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _CapturingSessions(provider)

    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text=text,
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id=user_id,
            context_builder=None,
            conversation_log=None,
            subagent_manager=subagent_manager,
            task_runner=task_runner,
            cron_service=cron_service,
        )
    )
    return slack, sessions


def _posts(slack):
    return [kw["text"] for (m, kw) in slack.transcript if m == "post_message"]


class TestTransportKeywordCommands:
    """spawn/run/cron/sessions must be intercepted on the transport path via
    the shared ``maybe_handle_keyword_command`` — no LLM turn, reply posted."""

    def test_spawn_intercepted_no_llm_turn(self, monkeypatch):
        slack, sessions = _run_transport_text(
            monkeypatch, "spawn list", subagent_manager=_FakeSubagentMgr()
        )
        assert sessions.agents == []  # no LLM session acquired
        assert "No subagents running." in _posts(slack)

    def test_run_intercepted_no_llm_turn(self, monkeypatch):
        slack, sessions = _run_transport_text(
            monkeypatch, "task run status", task_runner=_FakeTaskRunner()
        )
        assert sessions.agents == []
        assert "No task running." in _posts(slack)

    def test_cron_intercepted_no_llm_turn(self, monkeypatch):
        slack, sessions = _run_transport_text(
            monkeypatch, "cron list", cron_service=_FakeCronService()
        )
        assert sessions.agents == []
        assert "No cron jobs scheduled." in _posts(slack)

    def test_sessions_denied_for_unauthorized(self, monkeypatch):
        # Deny-by-default: an unauthorized caller is refused, still no LLM turn.
        monkeypatch.setattr(_handler, "is_owner", lambda uid: False)
        monkeypatch.setattr(_handler, "is_allowed_user", lambda uid: False)
        slack, sessions = _run_transport_text(monkeypatch, "sessions", user_id="U_STRANGER")
        assert sessions.agents == []
        assert "_Permission denied._" in _posts(slack)

    def test_sessions_authorized_renders_view(self, monkeypatch):
        # Authorized caller reaches the shared sessions renderer (stubbed) and
        # no LLM turn runs. handle_sessions defaults True on the transport path.
        called = {}

        async def _fake_sessions_cmd(*a, **k):
            called["hit"] = True

        monkeypatch.setattr(_handler, "is_owner", lambda uid: True)
        monkeypatch.setattr(_handler, "_handle_sessions_command", _fake_sessions_cmd)
        slack, sessions = _run_transport_text(monkeypatch, "sessions")
        assert called.get("hit") is True
        assert sessions.agents == []  # sessions view, no LLM turn

    def test_plain_text_falls_through_to_llm(self, monkeypatch):
        # A non-command message must NOT be intercepted even with all services
        # present: the LLM session IS acquired and the turn runs.
        slack, sessions = _run_transport_text(
            monkeypatch,
            "hello there",
            subagent_manager=_FakeSubagentMgr(),
            task_runner=_FakeTaskRunner(),
            cron_service=_FakeCronService(),
        )
        assert sessions.agents == ["kirocrew"]  # LLM session WAS acquired


# ── Privacy modifiers (!temporary / !incognito) on the transport path ──


class TestTransportPrivacyModifiers:
    """!temporary / !incognito must take effect on the default-ON transport
    path (set the durable flag, mark the session restricted) and the modifier
    token must never reach the LLM."""

    # Privacy flags are keyed by the canonical namespaced session key
    # (slack:<ts>) since the session-key canonicalization fix.
    _KEY = canonical_key(_MSG_TS)

    def _clear_flags(self, session_key):
        _handler._thread_temporary.pop(session_key, None)
        _handler._thread_incognito.pop(session_key, None)

    def test_incognito_only_marks_and_skips_llm(self, monkeypatch):
        self._clear_flags(self._KEY)
        # "!incognito" alone: apply the flag, post confirmation, NO LLM turn.
        slack, sessions = _run_transport_text(monkeypatch, "!incognito")
        assert sessions.agents == []  # no LLM session acquired
        assert _handler._is_slack_restricted(self._KEY) is True
        assert _handler.is_thread_incognito(self._KEY) is True
        self._clear_flags(self._KEY)

    def test_temporary_only_marks_and_skips_llm(self, monkeypatch):
        self._clear_flags(self._KEY)
        slack, sessions = _run_transport_text(monkeypatch, "!temporary")
        assert sessions.agents == []
        assert _handler._is_slack_restricted(self._KEY) is True
        assert _handler.is_thread_temporary(self._KEY) is True
        self._clear_flags(self._KEY)

    def test_incognito_prefix_marks_then_runs_llm_without_token(self, monkeypatch):
        # "!incognito <task>": flag set AND the turn runs, but the LLM sees the
        # task text with the "!incognito" token stripped (no leak).
        self._clear_flags(self._KEY)
        captured = {}

        class _CtxBuilder:
            class hooks:  # noqa: N801 - stub attribute, not a real class use
                @staticmethod
                def on_message(text):
                    from kiro_crew.hooks import HookResult

                    return HookResult.passthrough()

            def build_message(self, text, is_new, session_key, **kw):
                captured["text"] = text
                return text, {}

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="ok"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="!incognito summarize the logs",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=_CtxBuilder(),
                conversation_log=None,
            )
        )
        # Flag applied, LLM turn ran, and the token was stripped from the prompt.
        assert _handler.is_thread_incognito(self._KEY) is True
        assert sessions.agents == ["kirocrew"]
        assert "!incognito" not in captured["text"]
        assert "summarize the logs" in captured["text"]
        self._clear_flags(self._KEY)


# ── reactions_enabled passthrough on the transport path ──


class TestTransportReactionsEnabled:
    """The transport path must honor slack.reactions_enabled (passed from the
    events gate). When False, SlackRenderer builds no StatusReactionController,
    so no add_reaction calls are emitted."""

    def _run(self, monkeypatch, reactions_enabled):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
                reactions_enabled=reactions_enabled,
            )
        )
        return [m for m, _ in slack.transcript]

    def test_reactions_disabled_emits_no_add_reaction(self, monkeypatch):
        methods = self._run(monkeypatch, reactions_enabled=False)
        assert "add_reaction" not in methods

    def test_reactions_enabled_emits_add_reaction(self, monkeypatch):
        methods = self._run(monkeypatch, reactions_enabled=True)
        assert "add_reaction" in methods


# ── Native-behavior parity on the transport path (round-4 findings) ──


class _RecordingConsolidator:
    def __init__(self):
        self.calls: list = []

    def maybe_consolidate(self, key):
        self.calls.append(key)


class _CapturingCtxBuilder:
    """Records build_message kwargs; hooks.on_message is passthrough by default."""

    def __init__(self, hook_result=None):
        self.captured: dict = {}

        class _Hooks:
            def on_message(self, text):
                from kiro_crew.hooks import HookResult

                return hook_result or HookResult.passthrough()

        self.hooks = _Hooks()

    def build_message(self, text, is_new, session_key, **kw):
        self.captured = kw
        return text, {}


class TestTransportNativeParity:
    def _prep(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    def _provider(self):
        return ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )

    def test_hook_reply_short_circuits_no_llm_turn(self, monkeypatch):
        from kiro_crew.hooks import HookResult

        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder(hook_result=HookResult.reply("canned answer"))
        slack = RecordingSlackClient()
        sessions = _CapturingSessions(self._provider())
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hi",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=cb,
                conversation_log=None,
            )
        )
        # Hook answered → no LLM session acquired, canned reply posted.
        assert sessions.agents == []
        posts = [kw["text"] for m, kw in slack.transcript if m == "post_message"]
        assert "canned answer" in posts

    def test_consolidate_called_on_success(self, monkeypatch):
        self._prep(monkeypatch)
        cons = _RecordingConsolidator()
        slack = RecordingSlackClient()
        sessions = _CapturingSessions(self._provider())
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="do it",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
                consolidator=cons,
            )
        )
        # maybe_consolidate receives the canonical namespaced session key.
        assert cons.calls == [canonical_key(_MSG_TS)]

    def test_user_display_name_reaches_build_message(self, monkeypatch):
        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder()
        slack = RecordingSlackClient()
        sessions = _CapturingSessions(self._provider())
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hi",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=cb,
                conversation_log=None,
                user_display_name="Alice",
            )
        )
        assert cb.captured.get("user_display_name") == "Alice"


class TestTransportTemporaryBlocksMemoryReads:
    """``!temporary`` must block memory READS on the DEFAULT transport path.

    ``messaging.use_transport`` defaults True, so this is the live path, and the
    shared ``NOTICE_TEMPORARY`` promises the thread "won't read or save memory".
    The write half is covered by the ``_is_slack_restricted`` gates; the read half
    is one kwarg, and omitting it leaves memories and lessons in the prompt of a
    thread the user was told reads nothing.
    """

    _KEY = canonical_key(_MSG_TS)

    def _prep(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    def _dispatch(self, monkeypatch):
        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=RecordingSlackClient(),
                sessions=_CapturingSessions(provider),
                channel="C1",
                text="what did we decide yesterday",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=cb,
                conversation_log=None,
            )
        )
        return cb

    def test_a_temporary_thread_blocks_memory_reads(self, monkeypatch):
        _handler._mark_temporary(self._KEY)
        try:
            cb = self._dispatch(monkeypatch)
        finally:
            _handler._thread_temporary.pop(self._KEY, None)
        assert cb.captured["blocks_reads"] is True

    def test_an_incognito_thread_still_reads_memory(self, monkeypatch):
        """The predicate is temporary-only, never the combined restricted one.

        Incognito is documented as reading memory and refusing only to write, so
        widening this to ``_is_slack_restricted`` would silently take memory away
        from a mode that is supposed to keep it.
        """
        _handler._mark_incognito(self._KEY)
        try:
            cb = self._dispatch(monkeypatch)
        finally:
            _handler._thread_incognito.pop(self._KEY, None)
        assert cb.captured["blocks_reads"] is False


class TestTransportStatusIdentitySeam:
    """The `status` shortcut must use the platform identity seam
    (current_context().identity.status_line) — the CPP boundary native
    migrated to — not a direct SSO-stub import."""

    def test_status_uses_identity_seam(self, monkeypatch):
        from types import SimpleNamespace

        class _Identity:
            async def status_line(self, prefix=""):
                return f"{prefix}=OK"

        monkeypatch.setattr(
            transport_dispatch,
            "current_context",
            lambda: SimpleNamespace(identity=_Identity()),
        )
        slack = RecordingSlackClient()
        sessions = _CapturingSessions(ScriptedProvider([]))
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="status",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
            )
        )
        posts = [kw["text"] for m, kw in slack.transcript if m == "post_message"]
        # Status reply includes the identity seam's suffix; no LLM session.
        assert any(" · sso=OK" in p for p in posts), posts
        assert sessions.agents == []

    def test_no_direct_sso_stub_import(self):
        import kiro_crew.slack.transport_dispatch as td

        assert not hasattr(td, "get_sso_status_line")


class TestTransportToolGateWiring:
    """End-to-end: the transport path wires context_builder.hooks.on_tool_call
    into the TurnDriver as the PreToolUse gate, so a TOOL_DENY (sensitive-path /
    governance / deny-list) rejects the tool WITHOUT ever prompting the owner —
    even though the default gate mode is interactive."""

    def test_hook_deny_rejects_tool_without_prompt(self, monkeypatch):
        from kiro_crew.hooks import HookResult, ToolHookResult

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        class _Hooks:
            def on_message(self, text):
                return HookResult.passthrough()

            def on_tool_call(self, tool_name, **kw):
                # Simulate a sensitive-path / governance hard deny.
                return ToolHookResult.deny("sensitive path")

        class _CtxBuilder:
            hooks = _Hooks()

            def build_message(self, text, is_new, session_key, **kw):
                return text, {}

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(
                    EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="fs_write",
                    raw_tool_params={"path": "~/.aws/credentials"},
                    options=[{"id": "approve"}],
                ),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="read my creds",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=_CtxBuilder(),
                conversation_log=None,
                approval_mode="interactive",
            )
        )
        # Hard-denied by the hook → rejected, never approved, and NO approval
        # buttons were posted (post_blocks with "Tool approval requested") for
        # the owner to click. (The turn-end timing footer also uses post_blocks,
        # so filter on the approval text specifically.)
        assert provider.rejected == ["rq1"]
        assert provider.approved == []
        assert not any(
            m == "post_blocks" and kw.get("text") == "Tool approval requested"
            for m, kw in slack.transcript
        )


# ── Durable privacy flags hydrated BEFORE the early restriction checks ──


class TestHydrationBeforeHook:
    """After a gateway restart the in-memory incognito/temporary maps start
    empty. Hydration MUST run before the hook auto-reply's _is_slack_restricted
    check, otherwise a HOOK_REPLY on a durably-incognito thread gets logged to
    the conversation log (privacy-parity regression, restart-only)."""

    def test_hook_reply_on_hydrated_incognito_thread_is_not_logged(self, monkeypatch):
        _handler._thread_incognito.pop(canonical_key(_MSG_TS), None)

        # Simulate the durable-flag restore: hydration marks the thread incognito
        # (as the real _hydrate_conv_flags would from the conversation log).
        def _fake_hydrate(sessions, session_key):
            _handler._thread_incognito[session_key] = None

        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", _fake_hydrate)
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)

        saved: list = []

        async def _fake_save(*a, **k):
            saved.append((a, k))

        monkeypatch.setattr(
            transport_dispatch,
            "save_conversation_turn_off_loop",
            _fake_save,
        )

        class _CtxBuilder:
            class hooks:  # noqa: N801 - stub attribute, not a real class use
                @staticmethod
                def on_message(text):
                    from kiro_crew.hooks import HookResult

                    return HookResult.reply("canned answer")

        slack = RecordingSlackClient()
        sessions = _CapturingSessions(ScriptedProvider([]))

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hi",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=_CtxBuilder(),
                conversation_log=object(),
            )
        )

        # The canned hook reply WAS posted (the hook short-circuited the turn)...
        assert any(
            m == "post_message" and kw.get("text") == "canned answer" for m, kw in slack.transcript
        )
        # ...but because hydration ran FIRST, the thread is restricted, so the
        # turn was NOT written to the conversation log. Pre-fix (hydrate after
        # the hook) `saved` would be non-empty.
        assert saved == []
        assert _handler.is_thread_incognito(canonical_key(_MSG_TS)) is True
        _handler._thread_incognito.pop(canonical_key(_MSG_TS), None)


class TestConversationLogAgentMetadata:
    """The transport's user-turn write creates the session file — its metadata
    header records the agent only when that creating append supplies it.
    Pre-fix, the dashboard listed every Slack-spawned session as the "default"
    agent even though the turn ran under the resolved agent (issue: agent chip
    shows "default" for Slack sessions)."""

    def test_transport_turn_records_agent_in_session_metadata(self, monkeypatch, tmp_path):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)
        log = ConversationLog(base_dir=tmp_path)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=log,
                agent_override=None,
            )
        )

        listed = log.list_sessions()
        assert listed, "transport turn should have persisted a session file"
        # The session the dashboard lists must carry the agent the turn ran
        # under — not fall back to "default" because the header omitted it.
        assert listed[0].get("agent") == "sales-agent"

    def test_options_stamp_fallback_records_agent_when_receipt_write_failed(
        self, monkeypatch, tmp_path
    ):
        """The options-stamp fallback write is file-creating exactly when the
        user-turn receipt write failed — it must supply the agent for the same
        reason the receipt write does, or the session is pinned to "default"."""
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        provider = ScriptedProvider(
            [
                # An [OPTIONS:] trailer makes the renderer invoke stamp_options,
                # routing persistence through _persist_and_stamp.
                make_event(EVENT_TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = _CapturingSessions(provider)
        log = ConversationLog(base_dir=tmp_path)

        # Fail the FIRST append (the user-turn receipt) so the stamp fallback
        # becomes the write that creates the session file.
        real_append = log.append
        state = {"failed": False}

        def _flaky_append(*args, **kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise OSError("simulated receipt-write failure")
            return real_append(*args, **kwargs)

        monkeypatch.setattr(log, "append", _flaky_append)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=log,
                agent_override=None,
            )
        )

        listed = log.list_sessions()
        assert listed, "options-stamp fallback should have persisted a session file"
        assert listed[0].get("agent") == "sales-agent"

    def test_hook_reply_write_records_agent_in_session_metadata(self, monkeypatch, tmp_path):
        """A hook-answered FIRST message is the write that creates the session
        file — it runs before session acquisition, so the agent must be
        resolved early (native handler parity) or the session is pinned to
        "default" forever."""
        from kiro_crew.history import ConversationLog
        from kiro_crew.hooks import HookResult

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        slack = RecordingSlackClient()
        sessions = _CapturingSessions(self._noop_provider())
        cb = _CapturingCtxBuilder(hook_result=HookResult.reply("canned answer"))
        log = ConversationLog(base_dir=tmp_path)

        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="ping",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=cb,
                conversation_log=log,
                agent_override=None,
            )
        )

        # The hook short-circuited the turn — no LLM session was spawned.
        assert sessions.agents == []
        listed = log.list_sessions()
        assert listed, "hook auto-reply should have persisted a session file"
        assert listed[0].get("agent") == "sales-agent"

    @staticmethod
    def _noop_provider():
        return ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )


# ── Auto-title on the transport path ───────────────────────────────────


class _TitlingProvider(ScriptedProvider):
    """The turn's events on the first stream, a title on the naming turn.

    ``ScriptedProvider`` deliberately returns an EMPTY stream for every call after
    the first, which is exactly why the missing auto-title never showed up in this
    file: an empty naming stream reads as SKIP, and SKIP is silent.
    """

    def __init__(self, events, title="Deploy the gateway"):
        super().__init__(events)
        self._title = title

    async def stream(self, message: str):
        self.stream_calls += 1
        if self.stream_calls == 1:
            for ev in self._events:
                yield ev
            return
        self.title_prompt = message
        yield make_event(EVENT_TEXT_CHUNK, text=self._title)
        yield make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


class _TitleSessions(_CapturingSessions):
    """Adds the two calls ``background_turn`` makes around the naming turn."""

    def __init__(self, provider):
        super().__init__(provider)
        self.released: list = []
        self.recycled = 0

    def release(self, key):
        self.released.append(key)

    async def recycle_background(self):
        self.recycled += 1


def _run_transport_titling(monkeypatch, *, restricted=False, conversation_log=None):
    """Drive one successful transport turn and drain the fire-and-forget tasks."""
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    monkeypatch.setattr(transport_dispatch, "_is_slack_restricted", lambda _key: restricted)

    slack = RecordingSlackClient()
    provider = _TitlingProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _TitleSessions(provider)

    async def _drive():
        await transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text="deploy the gateway please",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=conversation_log,
        )
        # The auto-title is fire-and-forget through handler's tracked-task set,
        # so draining that set is the deterministic join point.
        #
        # Scoped to THIS loop's tasks. The set is a process global and every test
        # gets a fresh loop, so an earlier test in the same xdist worker can leave
        # a task behind whose loop is already closed — gathering it raises
        # "The future belongs to a different loop", which passes under ``-n0`` and
        # fails only under ``-n auto``.
        loop = asyncio.get_running_loop()
        pending = [t for t in list(_handler._background_tasks) if t.get_loop() is loop]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_drive())
    return slack, sessions, provider


def _titles(slack):
    return [kw["title"] for (m, kw) in slack.transcript if m == "set_thread_title"]


class TestTransportAutoTitle:
    """The transport path titles a conversation after its first successful turn.

    It never did, while ``messaging.use_transport`` defaults True — so on a
    default install NO Slack session was ever LLM-titled and every surface fell
    back to a deterministic truncation.
    """

    def test_a_successful_turn_titles_the_thread(self, monkeypatch):
        """Mutation: delete the auto-title block in ``handle_message_transport``
        — red, which is the state this path shipped in."""
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert _titles(slack) == ["Deploy the gateway"]
        assert provider.stream_calls == 2  # the turn, then the naming turn

    def test_the_naming_turn_sees_the_exchange(self, monkeypatch):
        _slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert "deploy the gateway please" in provider.title_prompt
        assert "hi" in provider.title_prompt

    def test_a_restricted_session_is_not_titled(self, monkeypatch):
        """A temporary/incognito session persists nothing, so there is nothing to
        name and no background turn to spend on it.

        Mutation: drop the ``not _is_slack_restricted(...)`` clause — red.
        """
        slack, _sessions, provider = _run_transport_titling(monkeypatch, restricted=True)
        assert _titles(slack) == []
        assert provider.stream_calls == 1

    def test_a_second_turn_does_not_retitle(self, monkeypatch):
        """The claim is shared, so the conversation is named once.

        Mutation: replace ``auto_title.try_claim`` with ``mark_titled`` — red.
        """
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        slack2, _s2, provider2 = _run_transport_titling(monkeypatch)
        assert _titles(slack) == ["Deploy the gateway"]
        assert _titles(slack2) == []
        assert provider2.stream_calls == 1

    def test_the_native_claim_suppresses_the_transport_one(self, monkeypatch):
        """One tracker across both paths: a thread the native loop already
        claimed is not titled again here.

        Mutation: give ``auto_title`` a per-module tracker per channel — red.
        """
        _handler._mark_titled(canonical_key(_MSG_TS), "manual")
        slack, _sessions, provider = _run_transport_titling(monkeypatch)
        assert _titles(slack) == []
        assert provider.stream_calls == 1

    def test_an_auto_title_dispatch_failure_does_not_record_a_failure(self, monkeypatch):
        """Bookkeeping isolation, same contract as every other step here.

        Mutation: remove the ``try/except`` around the auto-title dispatch — red,
        because the raise falls through to the outer handler and the
        already-successful turn is re-recorded as a failure.
        """
        calls = {"success": 0, "failure": 0}

        class _TrackSessions(_TitleSessions):
            def record_success(self, key):
                calls["success"] += 1

            async def record_failure(self, key):
                calls["failure"] += 1

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        def _boom(_key):
            raise RuntimeError("tracker exploded")

        monkeypatch.setattr(transport_dispatch.auto_title, "try_claim", _boom)

        slack = RecordingSlackClient()
        sessions = _TrackSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=None,
            )
        )
        assert calls == {"success": 1, "failure": 0}


class TestTransportTrustedBotErrorSuppression:
    """Echo-loop guard parity with native handle_message (issue #6638).

    A failed turn on a trusted-bot message must NOT post the transport error
    reply: in a mutual-mesh setup the reply is itself a bot-authored event the
    peer admits, so replying opens an unbounded error-reply ping-pong.
    """

    def _run_failing_turn(self, monkeypatch, *, from_trusted_bot: bool):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        # Deterministic failure inside the outer try: renderer construction
        # raises before any session work.
        def _boom(*a, **k):
            raise RuntimeError("forced turn failure")

        monkeypatch.setattr(transport_dispatch, "SlackRenderer", _boom)

        slack = RecordingSlackClient()
        provider = ScriptedProvider([])
        sessions = _CapturingSessions(provider)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="hello",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="B_TRUSTED" if from_trusted_bot else "U_OWNER",
                context_builder=None,
                conversation_log=None,
                from_trusted_bot=from_trusted_bot,
            )
        )
        errors = [
            kw["text"]
            for method, kw in slack.transcript
            if method == "post_message" and "Something went wrong" in kw.get("text", "")
        ]
        status_clears = [kw for method, kw in slack.transcript if method == "set_thread_status"]
        return errors, status_clears

    def test_error_reply_suppressed_for_trusted_bot(self, monkeypatch):
        errors, status_clears = self._run_failing_turn(monkeypatch, from_trusted_bot=True)
        assert errors == []
        # The thread status must still be cleared: suppression covers only the
        # error MESSAGE, or a stale "working" status pins to the thread forever.
        assert any(kw.get("status") == "" for kw in status_clears)

    def test_error_reply_posted_for_human_sender(self, monkeypatch):
        errors, status_clears = self._run_failing_turn(monkeypatch, from_trusted_bot=False)
        assert len(errors) == 1
        assert any(kw.get("status") == "" for kw in status_clears)


class TestTransportPartialProgressRescue:
    """A turn killed mid-flight must persist what the model already produced.

    Before this, the user row was durable but partial assistant output lived
    only in the renderer, so every retry re-read a transcript that ended at the
    question and started over. Observed 2026-09-02: a ~28-minute transient
    backend outage burned five consecutive attempts on one Slack thread, each
    re-deriving the same ticket ids before dying again, with the session file
    still 625 bytes at the end of it.
    """

    def _run_dying_turn(
        self,
        monkeypatch,
        tmp_path,
        *,
        streamed: str,
        break_user_row: bool = False,
        break_user_row_after_write: bool = False,
        finish_first: bool = False,
    ):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "sales-agent")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

        # Stream some text, THEN die — the shape of a real transient fault, and
        # the one the old code discarded. TurnDriver takes the renderer as its
        # second positional arg.
        #
        # The stand-in keeps NO buffer of its own on purpose. The rescue reads the
        # renderer's delivery ledger, so the only way these tests can pass is if
        # the text actually went out through ``_append_stream`` and Slack
        # acknowledged it — a seam wired to nothing, or wired to text that was
        # merely produced, fails here.
        class _DyingDriver:
            def __init__(self, client, renderer, *a, **k):
                self._renderer = renderer

            async def run(self, message):
                if streamed:
                    await self._renderer.on_text_chunk(streamed)
                if finish_first:
                    # The reply COMPLETED, then something downstream faulted (a
                    # footer post that 4xxs). Same ``except`` branch, but the
                    # transcript is whole.
                    await self._renderer.on_done()
                raise RuntimeError("backend died mid-stream")

        monkeypatch.setattr(transport_dispatch, "TurnDriver", _DyingDriver)

        slack = RecordingSlackClient()
        sessions = _CapturingSessions(ScriptedProvider([]))
        log = ConversationLog(base_dir=tmp_path)
        if break_user_row:
            # Reproduce the real shape of the lost receipt: the FIRST user-row
            # append raises (a history lock timeout held by a concurrent
            # writer), so ``_logged_user_turn`` stays False. One-shot on
            # purpose — the lock is transient, and the rescue's own write has to
            # be allowed to succeed or the test would prove nothing about it.
            _real_append = log.append
            _broke: list[bool] = []

            def _append(key, role, content, **kw):
                if role == "user" and not _broke:
                    _broke.append(True)
                    raise RuntimeError("history lock timeout")
                return _real_append(key, role, content, **kw)

            monkeypatch.setattr(log, "append", _append)
        if break_user_row_after_write:
            # The other half of the same ambiguity, and the one that makes a
            # two-row rescue corrupting: the row IS written and the append then
            # raises. ``ConversationLog.append`` really does work in this order —
            # write, invalidate caches, ``_maybe_rotate`` — and only the ``stat``
            # inside the rotation is guarded, so an oversized transcript whose
            # rewrite faults lands here. ``_logged_user_turn`` stays False even
            # though the question is durable.
            _real_append_2 = log.append
            _broke_2: list[bool] = []

            def _append_after(key, role, content, **kw):
                result = _real_append_2(key, role, content, **kw)
                if role == "user" and not _broke_2:
                    _broke_2.append(True)
                    raise RuntimeError("transcript rotation failed after append")
                return result

            monkeypatch.setattr(log, "append", _append_after)
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=slack,
                sessions=sessions,
                channel="C1",
                text="reconcile the two ledgers",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=None,
                conversation_log=log,
            )
        )
        rows = []
        for path in sorted(Path(tmp_path).rglob("*.jsonl")):
            for line in path.read_text().splitlines():
                if line.strip():
                    rows.append(__import__("json").loads(line))
        return rows, slack

    def test_partial_reply_is_persisted_with_continue_marker(self, monkeypatch, tmp_path):
        from kiro_crew.slack.renderer import PARTIAL_TURN_MARKER

        rows, slack = self._run_dying_turn(
            monkeypatch, tmp_path, streamed="Analysis so far: the first two checks passed."
        )
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1, "the partial reply should be rescued exactly once"
        content = assistant[0]["content"]
        # The established finding survives for the retry to build on...
        assert "the first two checks passed" in content
        # ...and is unambiguously flagged as cut off, so the next turn continues
        # instead of treating the work as already reported.
        assert content.endswith(PARTIAL_TURN_MARKER)
        # The failure is still surfaced to the user — rescue is additive.
        assert any(
            "Something went wrong" in kw.get("text", "")
            for method, kw in slack.transcript
            if method == "post_message"
        )

    def test_no_assistant_row_when_nothing_was_streamed(self, monkeypatch, tmp_path):
        """A turn that died before producing any text must not persist an empty
        assistant row — that would fabricate a reply the model never made."""
        rows, _ = self._run_dying_turn(monkeypatch, tmp_path, streamed="")
        assert [r for r in rows if r.get("role") == "assistant"] == []
        # The question itself is still durable (unchanged pre-existing behavior).
        assert [r for r in rows if r.get("role") == "user"]

    def test_no_rescue_when_the_user_receipt_outcome_is_unknown(self, monkeypatch, tmp_path):
        """A receipt append that RAISED leaves the row's fate unknowable, so the
        rescue must write nothing rather than guess.

        ``ConversationLog.append`` writes the row and only then invalidates
        caches and calls ``_maybe_rotate``, whose own guard covers just the
        ``stat``. So a rotation fault on an oversized transcript raises with the
        row already durable, while the lock timeout simulated here raises with
        nothing written — and the dispatcher cannot tell them apart. Writing both
        rows would duplicate the question in the first case; writing the
        assistant row alone would orphan the answer in the second. Recording
        nothing leaves the retry exactly where it was before this change.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Checked the first two ledgers; both reconcile.",
            break_user_row=True,
        )
        assert [r for r in rows if r.get("role") == "assistant"] == [], (
            "an assistant row here would either follow a duplicated question or "
            "have no question at all"
        )
        # And the rescue must not have invented a second copy of the question.
        assert [r.get("role") for r in rows].count("user") <= 1

    def test_rescue_never_duplicates_a_question_the_receipt_already_wrote(
        self, monkeypatch, tmp_path
    ):
        """The receipt append can raise with the row ALREADY on disk.

        ``append`` writes the row, then invalidates caches, then rotates; only
        the ``stat`` inside the rotation is guarded. An oversized transcript
        whose rewrite faults therefore raises after the question is durable, and
        ``_logged_user_turn`` stays False. A rescue that read that flag as
        "no question on disk" and wrote both rows would append a SECOND copy of
        the user's prompt — a corrupted transcript, worse than the missed rescue
        this PR exists to fix.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Checked the first two ledgers; both reconcile.",
            break_user_row_after_write=True,
        )
        user_rows = [r for r in rows if r.get("role") == "user"]
        assert len(user_rows) == 1, "the question must appear exactly once"
        assert user_rows[0]["content"] == "reconcile the two ledgers"
        assert [r for r in rows if r.get("role") == "assistant"] == []

    def test_rescued_text_keeps_its_leading_indentation(self, monkeypatch, tmp_path):
        """Indentation is content in a transcript, so the rescue persists raw.

        Stripping is only how emptiness is decided. A fenced block or a nested
        list that loses its left margin changes meaning, and neither
        success-path write trims it, so the failure path must not either.
        """
        streamed = "    indented line\n\ttabbed line"
        rows, _ = self._run_dying_turn(monkeypatch, tmp_path, streamed=streamed)
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"].startswith(streamed)

    def test_a_completed_reply_is_not_persisted_as_partial(self, monkeypatch, tmp_path):
        """A fault AFTER the stream finished must not stamp the cutoff marker.

        ``on_done`` having run means the reply is whole; a later failure (a footer
        post that 4xxs) still unwinds through the rescue's ``except``. Marking
        that complete text as cut off would be worse than not rescuing at all —
        the marker is a standing instruction to resume, so the next turn would
        continue work that had in fact finished.
        """
        from kiro_crew.slack.renderer import PARTIAL_TURN_MARKER

        rows, _ = self._run_dying_turn(
            monkeypatch,
            tmp_path,
            streamed="Both ledgers reconcile; totals match.",
            finish_first=True,
        )
        assert not [
            r
            for r in rows
            if r.get("role") == "assistant" and PARTIAL_TURN_MARKER in r.get("content", "")
        ], "a finished reply must never be flagged as cut off"

    def test_the_rescued_row_keeps_its_slack_user_provenance(self, monkeypatch, tmp_path):
        """The rescue must record WHO asked, like every success-path write.

        ``source_user`` is half of ``history.PROVENANCE_FIELDS``; a row missing it
        loses its Slack attribution, so a later rewrite credits the row to the
        dashboard rather than the person who sent the message.
        """
        rows, _ = self._run_dying_turn(
            monkeypatch, tmp_path, streamed="Partial finding worth keeping."
        )
        assistant = [r for r in rows if r.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].get("source_user") == "U_OWNER"
