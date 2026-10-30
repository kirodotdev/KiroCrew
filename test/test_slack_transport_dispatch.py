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

What a turn leaves behind once it ends (the agent recorded in the session
metadata, the auto-title, the error reply held back for a trusted bot and the
partial-reply rescue) is covered in
``test_slack_transport_dispatch_turn_outcomes.py``, and the one-session-per-DM
key and its wiring in ``test_slack_transport_dispatch_flat_dm.py``. The
message timestamp and the capturing session and context-builder stand-ins these
files share live in ``slack_transport_dispatch_helpers.py``, which also binds the
golden-transcript fakes from ``test_slack_golden_transcript``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    FakeSessions,
    RecordingSlackClient,
    ScriptedProvider,
    _CapturingCtxBuilder,
    _CapturingSessions,
    make_event,
)

from kiro_crew.acp.client import (
    AcpError,
    AcpProcessDied,
    AcpPromptBusy,
    AcpRegistrationRateLimited,
    AcpTimeoutError,
    _raise_acp_error,
)
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
)
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import transport_dispatch


def _run_transport(monkeypatch, thread_agent=None, agent_override=None, **extra):
    # Empty configured default -> exercises the canonical-agent fallback.
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_thread_overrides",
        AsyncMock(return_value=None),
    )
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
            **extra,
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
        # A channels policy that denies slack must stop
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


class TestTransportAdmissionSpool:
    def test_session_closing_spools_the_unopened_slack_turn(self, monkeypatch):
        from kiro_crew.session_allocation import SessionClosingError

        async def reject_turn(*_args, **_kwargs):
            raise SessionClosingError("automatic update owns admission")

        spool = AsyncMock()
        monkeypatch.setattr(_CapturingSessions, "get_or_create", reject_turn)
        monkeypatch.setattr(transport_dispatch, "spool_refused_turn", spool)
        monkeypatch.setattr(transport_dispatch, "_is_slack_restricted", lambda _key: False)

        sessions = _run_transport(monkeypatch)

        spool.assert_awaited_once()
        assert spool.await_args.kwargs["channel_type"] == "slack"
        route = spool.await_args.kwargs["route"]
        assert route.conversation_id == "C1"
        assert route.text == "hello"
        assert route.user_id == "U_OWNER"
        assert route.thread_id == _MSG_TS
        assert route.message_id == _MSG_TS
        assert route.attachments_dropped == 0
        assert sessions.agents == []

    @pytest.mark.parametrize("site", ["admission", "dispatch"])
    def test_spool_records_the_pre_ingestion_message(self, monkeypatch, site):
        """The ingested *text* inlines temp paths and file contents.

        Both refusal points -- the admission gate and a SessionClosingError at
        dispatch -- must record what the user sent and how many files it had,
        never the turn's prompt.
        """
        from kiro_crew.session_allocation import SessionClosingError

        routes: list = []
        if site == "admission":

            async def refuse(_sessions, *, channel_type, route, restricted=False):
                routes.append(route)
                return False

            monkeypatch.setattr(transport_dispatch, "admit_inbound_callback", refuse)
        else:

            async def reject_turn(*_args, **_kwargs):
                raise SessionClosingError("automatic update owns admission")

            async def spool(*, channel_type, route):
                routes.append(route)

            monkeypatch.setattr(_CapturingSessions, "get_or_create", reject_turn)
            monkeypatch.setattr(transport_dispatch, "spool_refused_turn", spool)
            monkeypatch.setattr(transport_dispatch, "_is_slack_restricted", lambda _key: False)

        _run_transport(monkeypatch, spool_text="look at this", spool_attachments=2)

        assert len(routes) == 1
        assert routes[0].text == "look at this"
        assert routes[0].attachments_dropped == 2

    @pytest.mark.parametrize(
        ("extra", "text", "dropped"),
        [
            ({"spool_text": "look at this", "spool_attachments": 2}, "look at this", 2),
            # No pre-ingestion capture (interaction re-dispatch): text as is.
            ({}, "approve the plan", 0),
        ],
    )
    def test_native_admission_spools_the_pre_ingestion_message(
        self, monkeypatch, extra, text, dropped
    ):
        turn_text = (
            "hello\n/tmp/kiro-attach-dead.png\n\n[File: notes.txt]\nbody"
            if extra
            else "approve the plan"
        )
        routes: list = []

        async def refuse(_sessions, *, channel_type, route, restricted=False):
            routes.append(route)
            return False

        monkeypatch.setattr(slack_handler, "admit_inbound_callback", refuse)
        monkeypatch.setattr(
            slack_handler, "channel_inbound_permitted", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(slack_handler, "_hydrate_thread_overrides", AsyncMock())
        monkeypatch.setattr(slack_handler, "_hydrate_conv_header", AsyncMock())
        monkeypatch.setattr(slack_handler, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(slack_handler, "_is_slack_restricted", lambda _key: False)

        asyncio.run(
            slack_handler.handle_message(
                RecordingSlackClient(),
                _CapturingSessions(ScriptedProvider([])),
                "C1",
                turn_text,
                None,
                _MSG_TS,
                "U_OWNER",
                **extra,
            )
        )

        assert len(routes) == 1
        assert routes[0].text == text
        assert routes[0].attachments_dropped == dropped

    def test_session_closing_does_not_spool_restricted_slack_turn(self, monkeypatch, caplog):
        from kiro_crew.session_allocation import SessionClosingError

        async def reject_turn(*_args, **_kwargs):
            raise SessionClosingError("automatic update owns admission")

        spool = AsyncMock()
        monkeypatch.setattr(_CapturingSessions, "get_or_create", reject_turn)
        monkeypatch.setattr(transport_dispatch, "spool_refused_turn", spool)
        monkeypatch.setattr(transport_dispatch, "_is_slack_restricted", lambda _key: True)
        caplog.set_level("INFO", logger=transport_dispatch.logger.name)

        _run_transport(monkeypatch)

        spool.assert_not_awaited()
        msgs = [r.getMessage() for r in caplog.records if r.name == transport_dispatch.logger.name]
        assert any("gateway is shutting down" in m for m in msgs), msgs


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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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


class _VerdictSessions(FakeSessions):
    """Books the turn's verdict, its resets and its context check in one ledger.

    ``breaker_trips`` makes ``record_failure`` answer as the real breaker does at
    its threshold: it resets the session itself and returns True. Each reset ends
    the runtime that is live, and a same-key message claims a successor while the
    reset is awaited.
    """

    def __init__(self, provider, *, breaker_trips=False):
        super().__init__(provider)
        self.breaker_trips = breaker_trips
        self.ledger: list[str] = []
        self.runtimes = ["first"]
        self.killed: list[str] = []

    def record_success(self, session_key):
        self.ledger.append("success")

    async def record_failure(self, session_key):
        self.ledger.append("failure")
        if self.breaker_trips:
            await self.reset(session_key)
            return True
        return False

    async def reset(self, session_key):
        self.ledger.append("reset")
        self.killed.append(self.runtimes[-1])
        self.runtimes.append(f"successor {len(self.runtimes)}")

    def check_context_usage(self, key, provider):
        self.ledger.append("context_check")
        return 0.0


class TestTransportUnfinishedTurn:
    """A turn the backend never finished is a failure on this path too, and resets.

    ``TurnDriver.run`` follows the partial text with the notice that the next
    message starts the agent again, on every path that drives it. This dispatcher
    makes that true: it books a failure and resets the session, once.
    """

    def _run(self, monkeypatch, stop_reason, *, breaker_trips=False):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
        slack = RecordingSlackClient()
        sessions = _VerdictSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="half an answer"),
                    make_event(EVENT_COMPLETE, stop_reason=stop_reason),
                ]
            ),
            breaker_trips=breaker_trips,
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
        return sessions, slack

    @pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER])
    def test_an_unfinished_turn_is_a_failure_and_resets_the_session(self, monkeypatch, stop_reason):
        sessions, slack = self._run(monkeypatch, stop_reason)
        shown = "\n".join(str(v) for (_m, kw) in slack.transcript for v in kw.values())
        assert "did not finish its answer" in shown, shown
        # No success, one failure, one reset, and no context check on the runtime
        # the reset replaced.
        assert sessions.ledger == ["failure", "reset"]
        assert sessions.killed == ["first"]

    @pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER])
    def test_at_the_breaker_threshold_an_unfinished_turn_resets_once(
        self, monkeypatch, stop_reason
    ):
        """The failure that trips the breaker resets the session inside
        ``record_failure``. A successor a same-key message started during that reset
        is the live runtime afterwards, so the turn must not reset it again."""
        sessions, _slack = self._run(monkeypatch, stop_reason, breaker_trips=True)
        assert "failure" in sessions.ledger and "success" not in sessions.ledger, sessions.ledger
        killed = sessions.killed
        assert killed == ["first"], f"the dispatcher reset again after the breaker did: {killed}"
        assert sessions.ledger == ["failure", "reset"]

    def test_a_finished_turn_is_a_success_and_keeps_the_session(self, monkeypatch):
        sessions, _slack = self._run(monkeypatch, STOP_REASON_END_TURN)
        assert sessions.ledger == ["success", "context_check"]
        assert sessions.killed == []


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
    monkeypatch.setattr(
        transport_dispatch,
        "_hydrate_thread_overrides",
        AsyncMock(return_value=None),
    )
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


def test_member_memory_refusal_redacts_before_posting(monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.memory_stores import UnknownMemoryStore

    private_path = "/home/alice/.kiro/crew/memory_stores/member-one/memory.db"
    credential = "AKIAIOSFODNN7EXAMPLE"
    failure = UnknownMemoryStore(f"memory_unavailable: cannot open {private_path}; {credential}")
    monkeypatch.setattr(
        transport_dispatch, "session_store_for_turn", AsyncMock(side_effect=failure)
    )
    slack, sessions = _run_transport_text(monkeypatch, "hello there")
    posted = "\n".join(_posts(slack))
    assert "memory_unavailable:" in posted
    assert private_path not in posted and "alice" not in posted
    assert credential not in posted
    assert sessions.agents == []


def _classified_acp_error(data: str) -> AcpError:
    """An ``AcpError`` built the way the ACP client builds one from an error frame."""
    try:
        _raise_acp_error({"code": -32603, "message": "Internal error", "data": data})
    except AcpError as exc:
        return exc
    raise AssertionError("_raise_acp_error did not raise")


def test_classified_acp_error_reaches_the_user(monkeypatch):
    from unittest.mock import AsyncMock

    failure = _classified_acp_error("The monthly usage limit has been reached.")
    monkeypatch.setattr(
        transport_dispatch, "session_store_for_turn", AsyncMock(side_effect=failure)
    )
    slack, _sessions = _run_transport_text(monkeypatch, "hello there")
    assert f"❌ {failure}" in _posts(slack)


def test_classified_acp_error_is_redacted_on_the_wire(monkeypatch):
    from unittest.mock import AsyncMock

    private_path = "/Users/alice/secret/x"
    failure = _classified_acp_error(
        f"The monthly usage limit has been reached. cannot read {private_path}"
    )
    assert failure.user_worded and private_path in str(failure)
    monkeypatch.setattr(
        transport_dispatch, "session_store_for_turn", AsyncMock(side_effect=failure)
    )
    slack, _sessions = _run_transport_text(monkeypatch, "hello there")
    posted = "\n".join(_posts(slack))
    assert "❌ The monthly usage limit has been reached. cannot read " in posted
    assert private_path not in posted and "alice" not in posted


class TestTransportErrorText:
    """The reply posted when a transport turn raises.

    A classified backend ``AcpError`` (e.g. a usage limit) reaches the user as its
    own redacted, length-capped reason; everything else keeps the generic text.
    """

    def test_classified_acp_error_shows_its_reason(self):
        err = _classified_acp_error("The monthly usage limit has been reached.")
        assert err.usage_limit
        assert transport_dispatch._transport_error_text(err) == f"❌ {err}"
        assert "monthly usage limit" in str(err)

    def test_prompt_busy_shows_its_reason(self):
        err = _classified_acp_error("Prompt already in progress")
        assert isinstance(err, AcpPromptBusy)
        assert transport_dispatch._transport_error_text(err) == f"❌ {err}"

    def test_prompt_busy_with_provider_text_keeps_the_generic_reply(self):
        # Busy named only in ``message``: the formatter's curated busy branch
        # reads ``data``, so the text is the fallback's provider passthrough.
        try:
            _raise_acp_error(
                {
                    "code": -32603,
                    "message": "Prompt already in progress",
                    "data": "provider raw text <!here> secret-ish",
                }
            )
        except AcpPromptBusy as exc:
            err = exc
        else:
            raise AssertionError("_raise_acp_error did not raise AcpPromptBusy")
        assert not err.user_worded
        assert (
            transport_dispatch._transport_error_text(err)
            == transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_classified_acp_error_is_redacted_before_posting(self):
        private_path = "/Users/alice/secret/x"
        credential = "AKIAIOSFODNN7EXAMPLE"
        err = AcpError(
            f"Backend refused: cannot read {private_path}; key {credential}",
            transient=False,
            user_worded=True,
        )
        text = transport_dispatch._transport_error_text(err)
        assert text.startswith("❌ Backend refused: cannot read ")
        assert private_path not in text and "alice" not in text
        assert credential not in text

    @pytest.mark.parametrize("split", ["AKIA*{}*", "AKIA**{}**", "AKIA_{}_", "AKIA`{}`"])
    def test_a_key_split_by_display_markup_is_redacted(self, split):
        rest = "IOSFODNN7EXAMPLE"
        err = _classified_acp_error(
            "The monthly usage limit has been reached. key " + split.format(rest)
        )
        assert err.user_worded and rest in str(err)
        text = transport_dispatch._transport_error_text(err)
        assert text.startswith("❌ The monthly usage limit has been reached.")
        assert rest not in text

    def test_classified_acp_error_is_truncated(self):
        cap = transport_dispatch._TRANSPORT_ERROR_DETAIL_MAX_CHARS
        err = _classified_acp_error("The monthly usage limit has been reached. " + "x" * cap)
        assert err.user_worded and len(str(err)) > cap
        assert transport_dispatch._transport_error_text(err) == "❌ " + str(err)[:cap]

    @pytest.mark.parametrize(
        "message",
        [
            "ACP process exited (code=1): Traceback (most recent call last) ...",
            "Shutdown in progress",
        ],
    )
    def test_unclassified_acp_error_keeps_generic_text(self, message):
        assert transport_dispatch._transport_error_text(AcpError(message)) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_timeout_keeps_generic_text(self):
        assert transport_dispatch._transport_error_text(AcpTimeoutError("t")) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_process_died_keeps_generic_text(self):
        assert transport_dispatch._transport_error_text(AcpProcessDied("d")) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_rate_limited_process_death_keeps_generic_text(self):
        err = AcpRegistrationRateLimited("registration throttled")
        assert err.transient is True
        assert transport_dispatch._transport_error_text(err) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    @pytest.mark.parametrize(
        "frame",
        [
            {"code": -32603, "message": "Internal error", "data": {"provider": {"raw": 1}}},
            {"code": -32603, "message": "Internal error"},
            {"code": -32000, "message": "Something odd", "data": None},
            {"code": -32603, "message": "Internal error", "data": "upstream: at /srv/app.py:3"},
        ],
        ids=["unrecognised-data", "no-data", "other-code", "provider-passthrough"],
    )
    def test_fallback_wording_keeps_generic_text(self, frame):
        """The formatter's fallback shows the provider's own text or the raw error
        dict. It carries ``transient=False`` like a curated message, so the gate
        must key on ``user_worded`` or this text would reach the channel."""
        try:
            _raise_acp_error(frame)
        except AcpError as err:
            assert err.transient is False and not err.user_worded
            assert transport_dispatch._transport_error_text(err) == (
                transport_dispatch._TRANSPORT_ERROR_TEXT
            )
        else:
            raise AssertionError("_raise_acp_error did not raise")

    def test_retryable_error_keeps_generic_text(self):
        """A worded but retryable failure (throttling) keeps the generic reply, as
        the Telegram transport does."""
        err = _classified_acp_error("ThrottlingException: Rate exceeded")
        assert err.transient is True and err.user_worded
        assert transport_dispatch._transport_error_text(err) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_non_acp_fault_keeps_generic_text(self):
        assert transport_dispatch._transport_error_text(RuntimeError("internal detail")) == (
            transport_dispatch._TRANSPORT_ERROR_TEXT
        )

    def test_slack_markup_in_the_reason_cannot_notify_anyone(self):
        """A provider error can echo arbitrary text; ``<!channel>`` or ``<@U…>`` in
        it must reach Slack as inert text, not a channel-wide or user ping."""
        text = transport_dispatch._transport_error_text(
            _classified_acp_error(
                "The monthly usage limit has been reached. <!channel> ping <@U0VICTIM> & more"
            )
        )
        assert "<!channel>" not in text and "<@U0VICTIM>" not in text
        assert "&lt;!channel&gt;" in text and "&lt;@U0VICTIM&gt;" in text
        assert "&amp; more" in text


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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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


class TestTransportNativeParity:
    def _prep(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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


def _arm_reinjection(sessions) -> dict:
    """Give the session stand-in the real manager's one-shot flag surface."""
    ledger: dict = {"consumed": [], "marks": 0, "armed": True}

    def _consume(key):
        ledger["consumed"].append(key)
        was = ledger["armed"]
        ledger["armed"] = False
        return was

    def _mark(key):
        ledger["marks"] += 1
        ledger["armed"] = True

    sessions.consume_needs_reinjection = _consume
    sessions.mark_needs_reinjection = _mark
    return ledger


class TestTransportCompactionReinjection:
    """The transport turn loop is its own copy, so it must consume the flag itself.

    ``session_compaction`` marks ``needs_reinjection`` after an in-place compaction
    dropped the session-start context. A turn loop that does not read it runs
    every turn after ``/compact`` without the skills index or the response-preferences
    block.
    """

    def _prep(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    def _run(self, sessions, cb):
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack=RecordingSlackClient(),
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

    def _provider(self):
        return ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )

    def test_a_compacted_session_forwards_the_flag_to_build_message(self, monkeypatch):
        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder()
        sessions = _CapturingSessions(self._provider())
        ledger = _arm_reinjection(sessions)
        self._run(sessions, cb)
        assert ledger["consumed"] == [canonical_key(_MSG_TS)]
        assert cb.captured.get("needs_reinjection") is True
        # Landed: consumed exactly once, and NOT put back.
        assert ledger["marks"] == 0 and ledger["armed"] is False

    def test_a_session_stand_in_without_the_flag_gets_the_false_default(self, monkeypatch):
        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder()
        sessions = _CapturingSessions(self._provider())
        assert not hasattr(sessions, "consume_needs_reinjection")
        self._run(sessions, cb)
        assert cb.captured.get("needs_reinjection") is False

    def test_a_cancelled_consuming_turn_puts_the_flag_back(self, monkeypatch):
        # A /stop completes the turn normally with stop_reason "cancelled", and
        # the backend drops that turn from its transcript -- the re-injected
        # context goes with it, so the flag must come back like a raised turn.
        self._prep(monkeypatch)
        cb = _CapturingCtxBuilder()
        sessions = _CapturingSessions(
            ScriptedProvider([make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)])
        )
        ledger = _arm_reinjection(sessions)
        self._run(sessions, cb)
        assert cb.captured.get("needs_reinjection") is True
        assert ledger["marks"] == 1 and ledger["armed"] is True

    def test_a_failed_consuming_turn_puts_the_flag_back(self, monkeypatch):
        # The flag is cleared BEFORE build_message; a driver fault on that very
        # turn discards the prompt carrying the re-injected context. Without the
        # re-arm the session runs without it until the NEXT compaction -- the
        # contract the dashboard runner keeps in its finally, applied here.
        self._prep(monkeypatch)

        class _DyingDriver:
            def __init__(self, *a, **k):
                pass

            async def run(self, message):
                raise RuntimeError("backend died before streaming")

        monkeypatch.setattr(transport_dispatch, "TurnDriver", _DyingDriver)
        cb = _CapturingCtxBuilder()
        sessions = _CapturingSessions(self._provider())
        ledger = _arm_reinjection(sessions)
        failures: list = []

        async def _record_failure(key):
            failures.append(key)

        sessions.record_failure = _record_failure
        self._run(sessions, cb)
        assert cb.captured.get("needs_reinjection") is True
        assert failures == [canonical_key(_MSG_TS)]
        assert ledger["marks"] == 1 and ledger["armed"] is True

    @pytest.mark.parametrize(("status", "marks"), [("completed", 1), ("failed", 0)])
    def test_a_backend_compaction_arms_the_flag_for_the_next_turn(self, monkeypatch, status, marks):
        # The backend compacted its own window mid-turn: the session-start context
        # is gone even though this turn landed, so the next turn must send it.
        self._prep(monkeypatch)
        sessions = _CapturingSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPACTION_STATUS, text=status),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )
        ledger = _arm_reinjection(sessions)
        ledger["armed"] = False
        self._run(sessions, _CapturingCtxBuilder())
        assert ledger["marks"] == marks and ledger["armed"] is bool(marks)

    def test_a_backend_compaction_arms_the_flag_when_the_turn_then_dies(self, monkeypatch):
        self._prep(monkeypatch)

        class _CompactThenDie(ScriptedProvider):
            async def stream(self, message):
                self.stream_calls += 1
                yield make_event(EVENT_COMPACTION_STATUS, text="completed")
                raise RuntimeError("backend died after compacting")

        sessions = _CapturingSessions(_CompactThenDie([]))
        ledger = _arm_reinjection(sessions)
        ledger["armed"] = False

        async def _record_failure(key):
            return None

        sessions.record_failure = _record_failure
        self._run(sessions, _CapturingCtxBuilder())
        assert ledger["marks"] == 1 and ledger["armed"] is True


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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
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
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )

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


class TestTeardownReleasesSessionOnCancel:
    """A ``!stop`` cancelling the turn task while ``renderer.close()`` awaits
    must still release the session semaphore.

    ``close()`` now awaits (it clears the thread status on a cancelled turn),
    so a cancellation can land inside it and raise ``CancelledError`` -- which
    is not an ``Exception`` and escapes the dispatcher's ``except Exception``
    teardown guard. The session release must sit in a ``finally`` so the
    permit is always returned; otherwise the semaphore stays locked and every
    later message in that conversation wedges until the gateway restarts.
    """

    def _run(self, monkeypatch, *, close_raises):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
        monkeypatch.setattr(
            transport_dispatch,
            "_hydrate_thread_overrides",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
        # Keep the post-release dashboard refresh from reaching real wiring.
        monkeypatch.setattr(
            transport_dispatch,
            "_refresh_dashboard_tab",
            AsyncMock(return_value=None),
        )

        real_renderer_cls = transport_dispatch.SlackRenderer

        class _CancelClosingRenderer(real_renderer_cls):
            async def close(self):
                if close_raises is not None:
                    raise close_raises
                await super().close()

        monkeypatch.setattr(transport_dispatch, "SlackRenderer", _CancelClosingRenderer)

        released: list[str] = []

        class _ReleaseRecordingSessions(FakeSessions):
            def release(self, session_key):
                released.append(session_key)

        slack = RecordingSlackClient()
        sessions = _ReleaseRecordingSessions(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )

        async def drive():
            await transport_dispatch.handle_message_transport(
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

        return released, drive

    def test_release_happens_when_close_is_cancelled(self, monkeypatch):
        released, drive = self._run(monkeypatch, close_raises=asyncio.CancelledError())
        turn_key = canonical_key(_MSG_TS)
        # The CancelledError propagates out of the dispatcher (as it must, to
        # not swallow the stop), but the session is released on the way out.
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(drive())
        assert turn_key in released, "session semaphore leaked when close() was cancelled"

    def test_release_happens_when_close_raises(self, monkeypatch):
        # A plain Exception in close() is swallowed and the release still runs.
        released, drive = self._run(monkeypatch, close_raises=RuntimeError("boom"))
        asyncio.run(drive())
        assert canonical_key(_MSG_TS) in released

    def test_release_happens_on_normal_close(self, monkeypatch):
        released, drive = self._run(monkeypatch, close_raises=None)
        asyncio.run(drive())
        assert canonical_key(_MSG_TS) in released
