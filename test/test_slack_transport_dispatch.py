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
    """FakeSessions that records the ``agent`` passed to get_or_create."""

    def __init__(self, provider):
        super().__init__(provider)
        self.agents: list = []

    async def get_or_create(self, session_key, agent=None, channel_id=None):
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
