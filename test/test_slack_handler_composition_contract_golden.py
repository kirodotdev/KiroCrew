"""Golden transcripts of the native Slack handler, one recorded digest per scenario.

Each ``_scenario_*`` drives callbacks and helpers of ``kiro_crew.slack.handler`` against
the shared recording doubles in ``slack_handler_composition_contract_helpers.py``, plus
the dashboard and history doubles defined here where it needs them, and the SHA-256 of
its transcript (what reaches Slack, the SEL ledger, the stats counters, the session
manager and the provider, in order) is pinned in ``_GOLDEN_DIGESTS``. ``SCENARIOS``
collects the scenarios from this module's globals, so each one is defined here. The base
surface, the composition and the placement guards are pinned in
``test_slack_handler_composition_contract.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from slack_handler_composition_contract_helpers import (  # noqa: F401 -- the fixture is requested by name
    _complete,
    _ContextBuilder,
    _permission,
    _Provider,
    _Sessions,
    _settle,
    _Slack,
    _text,
    _Transcript,
    _turn,
    world_fixture,
)

from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler
from kiro_crew.testing.wait import async_wait_until

# ── golden scenarios ──────────────────────────────────────────────────────────


async def _scenario_streamed_turn(log):
    """Reasoning, a tool, answer text over several edits, an OPTIONS trailer."""
    events = [
        LLMEvent(kind="thinking_chunk", text="weighing the options"),
        _text("Here is the "),
        LLMEvent(kind="tool_call", title="Running: Bash", tool_kind="execute", tool_purpose="list"),
        _text("first part. "),
        _text("And the rest [OPT"),
        _text("IONS: Yes | No]"),
        _complete(),
    ]
    await _turn(log, events)


async def _scenario_unstreamed_long_reply(log):
    """No streaming API: placeholder, cursor edits, a split final update."""
    events = [_text("x" * 2500), _text(" y" * 1500), _text("tail"), _complete()]
    await _turn(log, events, stream=False)


async def _scenario_top_level_no_placeholder(log):
    """A root message (no thread) on a client without streaming."""
    await _turn(log, [_text("short answer"), _complete()], thread_ts=None, stream=False)


async def _scenario_credential_redaction(log):
    """A credential in the stream: per-chunk redaction, overwrite, one notice."""
    events = [
        _text("key AKIAIOSFODNN7EXAMPLE and "),
        _text("see https://evil.example.invalid/x?d=AKIAIOSFODNN7EXAMPLE"),
        _complete(),
    ]
    await _turn(log, events)


async def _scenario_append_refused_then_rotated(log):
    """A refused append rotates the stream; the debt notice discloses the hole.

    Slack refuses the first chunk's append and its retry on the rotated stream, so that
    chunk is delivery debt; the rest lands, and the notice goes out before the seal.
    """
    events = [
        _text("The first part of the answer. "),
        _text("The second part follows here. "),
        _text("And this is the end of it.\n"),
        _complete(),
    ]
    await _turn(log, events, append_refusals=2)


async def _scenario_wait_tool_seals_stream(log):
    events = [
        _text("before wait"),
        LLMEvent(kind="tool_call", title="wait", tool_kind="other"),
        _text("after wait"),
        _complete(),
    ]
    await _turn(log, events)


async def _scenario_auto_approval_mode(log):
    """The answer ("done") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    events = [_permission("r-auto", "Bash", "ls"), _text("done"), _complete()]
    await _turn(log, events, approval_mode=handler.APPROVAL_AUTO)


async def _scenario_transport_floor_refusal(log):
    """The answer ("done") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    events = [_permission("r-floor", "Bash", "ls"), _text("done"), _complete()]
    await _turn(
        log, events, approval_mode=handler.APPROVAL_AUTO, provider_kw={"approve_result": False}
    )


async def _scenario_trusted_session(log):
    """The answer ("ok") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    handler._trusted_sessions["slack:1700000000.000001"] = True
    events = [_permission(7, "Write file", "path"), _text("ok"), _complete()]
    await _turn(log, events, approval_mode=handler.APPROVAL_INTERACTIVE)


async def _interactive(log, action_id, *, channel="D1", click=True, answer="after"):
    events = [
        _permission("r-int", "Bash: rm -rf build", "rm -rf build"),
        _text(answer),
        _complete(),
    ]
    provider = _Provider(log, events)
    sessions = _Sessions(log, provider)
    slack = _Slack(log)
    turn = asyncio.ensure_future(
        handler.handle_message(
            slack,
            sessions,
            channel,
            "please",
            "1700000000.000001",
            "1700000000.000002",
            "UOWNER",
            approval_mode=handler.APPROVAL_INTERACTIVE,
        )
    )
    await async_wait_until(
        lambda: handler._pending_approvals or turn.done(),
        describe=lambda: f"pending={sorted(handler._pending_approvals)} turn_done={turn.done()}",
    )
    keys = sorted(handler._pending_approvals)
    log.add("pending", keys=keys)
    if click and keys:
        msg_ts = keys[0].split(":", 1)[1]
        result = await handler.handle_interaction(
            channel,
            msg_ts,
            action_id,
            user_id="UOWNER",
            thread_ts="1700000000.000001",
            slack=slack,
            sessions=sessions,
        )
        log.add("interaction.result", result=result)
    await asyncio.wait_for(turn, timeout=30)
    await _settle()
    log.add("trusted", keys=sorted(handler._trusted_sessions))


async def _scenario_interactive_approve(log):
    """The answer ("after") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    await _interactive(log, handler._ACTION_APPROVE)


async def _scenario_interactive_approve_answered(log):
    """An approved call followed by an answer the stream releases: a delivered turn."""
    await _interactive(log, handler._ACTION_APPROVE, answer="Approved, and the build is clean.\n")


async def _scenario_interactive_trust_in_dm(log):
    """The answer ("after") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    await _interactive(log, handler._ACTION_TRUST)


async def _scenario_interactive_reject(log):
    await _interactive(log, handler._ACTION_REJECT, channel="C1")


async def _scenario_interactive_timeout(log):
    log.monkeypatch.setattr(handler, "_APPROVAL_TIMEOUT", 0.2)
    await _interactive(log, handler._ACTION_APPROVE, click=False)


async def _scenario_hook_deny_and_flag(log):
    from kiro_crew.hooks import HookManager, HooksConfig

    hooks = HookManager(HooksConfig.from_dict({"auto_deny_tools": ["danger*"]}))
    cb = _ContextBuilder(log, hooks)
    events = [
        LLMEvent(kind="tool_call", title="danger-tool", tool_kind="other"),
        _permission("r-deny", "danger-tool", "x"),
        _text("blocked"),
        _complete(),
    ]
    await _turn(
        log,
        events,
        context_builder=cb,
        approval_mode=handler.APPROVAL_INTERACTIVE,
        provider_kw={"steer": True},
    )


async def _scenario_review_mode(log):
    await _turn(log, [_text("draft reply"), _complete()], channel_activation=ACTIVATION_REVIEW)


async def _error_turn(log, exc_factory, **kw):
    await _turn(log, [exc_factory], **kw)


async def _scenario_error_after_partial_text(log):
    await _turn(log, [_text("partial"), lambda: RuntimeError("boom")])


async def _scenario_error_timeout(log):
    await _error_turn(log, lambda: AcpTimeoutError(partial_output="half an answer"))


async def _scenario_error_process_died(log):
    await _error_turn(log, lambda: AcpProcessDied("gone"))


async def _scenario_error_prompt_busy(log):
    await _error_turn(log, lambda: AcpPromptBusy("busy"))


async def _scenario_error_acp(log):
    await _error_turn(log, lambda: AcpError("bad frame"))


async def _scenario_error_unexpected(log):
    await _error_turn(log, lambda: RuntimeError("boom"))


async def _scenario_error_from_trusted_bot(log):
    await _error_turn(log, lambda: AcpError("bad frame"), from_trusted_bot=True)


async def _scenario_compaction_failed_transient_replay(log):
    events = [_complete(STOP_REASON_COMPACTION_FAILED)]
    await _turn(log, events, provider_kw={"transient": True})


async def _scenario_compaction_failed_permanent(log):
    """The answer ("x") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    events = [_text("x"), _complete(STOP_REASON_COMPACTION_FAILED)]
    await _turn(log, events, provider_kw={"transient": False})


async def _scenario_cancelled_stop_reason(log):
    await _turn(log, [_text("partial"), _complete("cancelled")])


async def _scenario_voice_reply_requested(log):
    handler._vc.global_enabled = True

    await _turn(log, [_text("spoken " * 20), _complete()])


async def _command(log, text, *, user="UOWNER", known=False, thread_ts="1700000000.000001", **kw):
    await _turn(
        log,
        [_text("model answer"), _complete()],
        text=text,
        user=user,
        thread_ts=thread_ts,
        sessions_known=known,
        **kw,
    )


async def _scenario_cmd_status(log):
    await _command(log, "status")


async def _scenario_cmd_unknown_bang(log):
    await _command(log, "!frobnicate now")


async def _scenario_cmd_owner_only_denied(log):
    await _command(log, "!agent foo", user="USTRANGER")


async def _scenario_cmd_allowed_denied(log):
    await _command(log, "!stop", user="USTRANGER")


async def _scenario_cmd_yolo_status(log):
    await _command(log, "!yolo")


async def _scenario_cmd_voice(log):
    await _command(log, "!voice on")
    await _command(log, "!voice engine neural")
    await _command(log, "!voice speed 90%")
    await _command(log, "!voice")
    await _command(log, "!voice off")


async def _scenario_cmd_title(log):
    await _command(log, "!title My AKIAIOSFODNN7EXAMPLE thread")
    await _command(log, "!title")


async def _scenario_cmd_ta_and_project(log):
    await _command(log, "!ta")
    await _command(log, "!ta off")
    await _command(log, "!project")
    await _command(log, "!project off")


async def _scenario_cmd_allowlist_and_channel(log):
    await _command(log, "!allowlist @someone")
    await _command(log, "!channel")
    await _command(log, "!channel bogus")
    await _command(log, "!channel always", user="USTRANGER")


async def _scenario_cmd_stop_no_session(log):
    await _command(log, "!stop")


async def _scenario_cmd_stop_running(log):
    await _command(log, "!stop", known=True)


async def _scenario_cmd_compact(log):
    await _command(log, "!compact", known=True)
    await _command(log, "!compact", known=False)
    await _command(log, "!compact", user="USTRANGER")


async def _scenario_cmd_link_to_dashboard_unavailable(log):
    await _command(log, "!link-to-dashboard")


async def _scenario_cmd_dashboard_bad_duration(log):
    await _command(log, "!dashboard forever")


async def _scenario_cmd_sessions(log):
    await _command(log, "sessions", user="USTRANGER")


async def _scenario_mention_prefixed_bang(log):
    await _command(log, "<@UBOT|kirocrew> !voice")


async def _scenario_interaction_edges(log):
    slack = _Slack(log)
    sessions = _Sessions(log, _Provider(log, []))
    for kwargs in (
        {"user_id": ""},
        {"user_id": "USTRANGER"},
        {"user_id": "UOWNER"},
        {"user_id": "UOWNER", "action": handler._ACTION_TRUST, "thread_ts": "1700000000.000009"},
        {
            "user_id": "UOWNER",
            "action": handler._ACTION_TRUST,
            "thread_ts": "1700000000.000009",
            "slack": slack,
        },
    ):
        action = kwargs.pop("action", handler._ACTION_APPROVE)
        result = await handler.handle_interaction(
            "D1", "1000000.000000", action, sessions=sessions, **kwargs
        )
        log.add("interaction.result", action=action, result=result)
    slack._fetch_thread_replies_result = [{"user": "UOWNER"}]
    result = await handler.handle_interaction(
        "D1",
        "1000000.000000",
        handler._ACTION_TRUST,
        user_id="UOWNER",
        thread_ts="1700000000.000009",
        slack=slack,
        sessions=sessions,
    )
    log.add("interaction.result", action="late-trust-owner", result=result)
    log.add("trusted", keys=sorted(handler._trusted_sessions))


class _Slot:
    def __init__(self, key: str, *, running: bool = False) -> None:
        self.key = key
        self.running = running
        self.linked_session_key = ""
        self._trust = False
        self.messages: list = []
        self.queued: list = []

    def queue_append(self, text, **kwargs):
        self.queued.append(text)


class _Dashboard:
    def __init__(self, log: _Transcript, slots: dict[str, _Slot], linked: Any = None) -> None:
        self._log = log
        self._slots = slots
        self._linked = linked
        self._background_tasks: set = set()

    def get_linked_slot(self, thread_ts):
        self._log.add("dashboard.get_linked_slot", thread_ts=thread_ts)
        return self._linked

    def resolve_approval(self, request_id, approved):
        self._log.add("dashboard.resolve_approval", request_id=request_id, approved=approved)
        return True

    def push_slots_update(self):
        self._log.add("dashboard.push_slots_update")

    def get_or_create_slot(self, *args, **kwargs):
        raise AssertionError("not reached")


async def _scenario_linked_approval(log):
    slot = _Slot("chat-1")
    log.monkeypatch.setattr(handler, "_dashboard_state", _Dashboard(log, {"chat-1": slot}))
    slack = _Slack(log)
    for channel, action in (
        ("C1", handler._ACTION_APPROVE),
        ("D1", handler._ACTION_REJECT),
        ("D1", handler._ACTION_TRUST),
    ):
        ts = await handler.post_linked_approval(
            slack,
            channel,
            "1700000000.000001",
            "req-9",
            "dashboard:chat-1",
            "Bash https://evil.example.invalid/?k=AKIAIOSFODNN7EXAMPLE",
            "echo hi",
        )
        log.add("linked.posted", ts=ts, registry=sorted(handler._linked_approvals))
        result = await handler.handle_interaction(
            channel, ts, action, user_id="UOWNER", sessions=_Sessions(log, _Provider(log, []))
        )
        log.add("interaction.result", action=action, result=result)
    handler.resolve_linked_approval("D1", "missing")


async def _scenario_linked_thread_routes_to_slot(log):
    def _surface(state, slot, role, content, cls="", **kwargs):
        log.add("dashboard.append_and_surface", slot=slot.key, role=role, content=content, **kwargs)
        return {}

    log.monkeypatch.setattr(handler, "append_and_surface", _surface)
    log.monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda state, slot: {"linked": 1}
    )
    slot = _Slot("chat-2", running=True)
    log.monkeypatch.setattr(handler, "_dashboard_state", _Dashboard(log, {}, linked=slot))
    await _command(log, "route me https://evil.example.invalid/?t=AKIAIOSFODNN7EXAMPLE")
    await _command(log, "!voice")
    await _command(log, "sessions")
    await _command(log, "intruder", user="USTRANGER")
    log.add("slot.queued", queued=slot.queued)


async def _scenario_keyword_commands(log):
    async def _spawn(text, manager, session_key):
        log.add("keyword.spawn", text=text, key=session_key)
        return "spawned!" if text.startswith("spawn") else None

    async def _task(text, runner, session_key=""):
        log.add("keyword.run", text=text, key=session_key)
        return "running!" if text.startswith("run") else None

    async def _cron(text, cron_service, source, caller):
        log.add("keyword.cron", text=text, source=source, caller=caller)
        return "cron!" if text.startswith("cron") else None

    log.monkeypatch.setattr(handler, "spawn_command_reply", _spawn)
    log.monkeypatch.setattr(handler, "task_command_reply", _task)
    log.monkeypatch.setattr(handler, "cron_command_reply", _cron)
    services = {
        "subagent_manager": object(),
        "task_runner": object(),
        "cron_service": object(),
        "conversation_log": object(),
    }
    for text in ("spawn a helper", "run spec.md", "cron list", "nothing special"):
        await _command(log, text, **services)


async def _scenario_privacy_modifiers(log):
    from kiro_crew.messaging import privacy_mode

    async def _apply(mode, session_key, **kwargs):
        log.add("privacy.apply_mode", mode=mode, key=session_key, source=kwargs.get("source"))
        if mode == privacy_mode.MODE_INCOGNITO and session_key.endswith("refused"):
            raise privacy_mode.PrivacyModeRefused(mode, session_key, "limit")
        await kwargs["notify"](f"{mode} on")
        await kwargs["on_applied"](mode)

    log.monkeypatch.setattr(privacy_mode, "apply_mode", _apply)
    await _command(log, "!temporary")
    await _command(log, "!incognito tell me a secret")
    await _command(log, "<@UBOT> !temporary !compact")
    out = await handler.maybe_apply_privacy_modifiers(
        "!incognito hi",
        "!incognito hi",
        "slack:refused",
        "UOWNER",
        "C1",
        _Slack(log),
        _Sessions(log, _Provider(log, [])),
        "1700000000.000003",
        link_thread=False,
    )
    log.add("privacy.result", out=out)


async def _scenario_status_reactions(log):
    """The one status reaction: the ack where no native status shows, taken down at the
    reply; nothing where it does; the error reaction on failure; nothing when disabled."""
    slack = _Slack(log)
    ctrl = handler.StatusReactionController(slack, "C1", "1.0")
    ctrl.start(native_status=False)
    ctrl.start(native_status=False)
    await _settle()
    ctrl.finalize()
    ctrl.finalize(error=True)
    await _settle()
    native = handler.StatusReactionController(slack, "C1", "2.0")
    native.start(native_status=True)
    native.finalize()
    await _settle()
    failed = handler.StatusReactionController(slack, "C1", "3.0")
    failed.start(native_status=False)
    failed.finalize(error=True)
    await _settle()
    off = handler.StatusReactionController(slack, "C1", "4.0", enabled=False)
    off.start(native_status=False)
    off.finalize(error=True)
    await _settle()


async def _scenario_builders_and_filters(log):
    class _Ev:
        def __init__(self, request_id, title, tool_input="", tool_purpose=""):
            self.request_id, self.title = request_id, title
            self.tool_input, self.tool_purpose = tool_input, tool_purpose

    for event, is_dm, source in (
        (_Ev(5, "Bash AKIAIOSFODNN7EXAMPLE", "x" * 3200, "clean up"), True, ""),
        (_Ev("r", "Write", ""), False, "cron"),
    ):
        log.add("blocks.approval", blocks=handler._build_approval_blocks(event, is_dm, source))
    for elapsed, provider in ((5.4, None), (75.0, _Provider(log, [])), (3601.0, object())):
        log.add("blocks.footer", out=handler.build_timing_footer(elapsed, provider))
    log.add(
        "blocks.footer_actions",
        out=handler._append_footer_actions(
            [{"type": "actions", "elements": []}], ["A", "B"], "1.0", None, object(), "tok"
        ),
    )
    log.add("blocks.footer_actions", out=handler._append_footer_actions([], None, "1.0", None, 1))
    log.add("thinking", out=handler._condense_thinking("word " * 200))
    hold, buffer = "", ""
    for chunk in (
        "Hello [x] and [OPTIONS: a",
        " | b] end\n",
        "<!-- keep-",
        "visible -->",
        "\nmore",
    ):
        hold, buffer = handler._filter_options_brackets(chunk, hold, buffer)
        log.add("filter", hold=hold, buffer=buffer)
    log.add(
        "hold",
        out=handler._resolve_comment_hold("<!-- keep-visible -->", "x <!-- keep-visible -->"),
    )
    log.add("hold", out=handler._resolve_comment_hold("<!-- nope", "x <!-- nope"))
    log.add("phase_table", out=handler._build_phase_emojis({"done": None, "bogus": "x"}))
    log.add(
        "keyword",
        out=[
            handler._is_sessions_keyword(t)
            for t in ("sessions", " Sessions all ", "sessions please", "", "session")
        ],
    )
    log.add(
        "redactor",
        out=handler._display_redactor("AKIAIOSFODNN7EXAMPLE https://evil.example.invalid/?a=b"),
    )
    log.add("notice", out=handler.DELIVERY_DEBT_NOTICE)


class _ConversationLog:
    def __init__(self, log: _Transcript, meta: dict | None = None) -> None:
        self._log = log
        self._meta = meta or {}

    def get_metadata(self, session_key):
        self._log.add("history.get_metadata", key=session_key)
        return dict(self._meta)

    def get_metadata_status(self, session_key):
        # Hydration's read: the same metadata line as get_metadata, reported readable.
        return self.get_metadata(session_key), True

    def update_metadata(self, session_key, values):
        self._log.add("history.update_metadata", key=session_key, values=values)

    def set_title(self, session_key, title):
        self._log.add("history.set_title", key=session_key, title=title)


async def _scenario_turn_with_history(log):
    """A cold start in a thread: replayed history, the cancelled-turn preamble, the
    thread-replies block, re-injection consumed, the replies watermark moved."""
    from kiro_crew.hooks import HookManager, HooksConfig
    from kiro_crew.slack.thread_replies import ThreadReplies

    def _replay(conversation_log, session_key, *, model_window=None):
        log.add("history.replay", key=session_key, model_window=model_window)
        return "REPLAYED HISTORY"

    def _preamble(conversation_log, session_key):
        log.add("history.cancelled_preamble", key=session_key)
        return "PREAMBLE"

    async def _prior(conversation_log, session_key):
        log.add("history.has_prior_turns", key=session_key)
        return False

    async def _replies(slack, channel, thread_ts, msg_ts, *, session_key, first_turn):
        log.add(
            "thread.replies",
            thread_ts=thread_ts,
            msg_ts=msg_ts,
            key=session_key,
            first_turn=first_turn,
        )
        return ThreadReplies(text="UNSEEN REPLIES", read_ok=True)

    def _noted(session_key, thread_ts, msg_ts):
        log.add("thread.note_turn", key=session_key, thread_ts=thread_ts, msg_ts=msg_ts)

    mp = log.monkeypatch
    mp.setattr(handler, "build_session_replay", _replay)
    mp.setattr(handler, "build_cancelled_turn_preamble", _preamble)
    mp.setattr(handler, "has_prior_turns", _prior)
    mp.setattr(handler, "replies_since_last_turn", _replies)
    mp.setattr(handler, "note_turn", _noted)
    history = _ConversationLog(log, {"agent": "pinned-agent"})
    cb = _ContextBuilder(log, HookManager(HooksConfig()))
    cb.conversation_log = history
    provider = _Provider(log, [_text("answer [OPTIONS: Go | Stop]"), _complete()])
    sessions = _Sessions(log, provider)
    sessions._sessions["slack:1700000000.000001"] = SimpleNamespace(prev_turn_cancelled=True)
    slack = _Slack(log)
    await handler.handle_message(
        slack,
        sessions,
        "C1",
        "and now?",
        "1700000000.000001",
        "1700000000.000002",
        "UOWNER",
        context_builder=cb,
        conversation_log=history,
        channel_agent="chan-agent",
        user_display_name="Owner",
        had_voice_input=True,
    )
    await _settle()
    failing = _Provider(log, [lambda: AcpError("late")])
    await handler.handle_message(
        slack,
        _Sessions(log, failing),
        "C1",
        "again",
        "1700000000.000001",
        "1700000000.000004",
        "UOWNER",
        context_builder=cb,
        conversation_log=history,
    )
    await _settle()


async def _scenario_agent_commands(log):
    agents = log.tmp / "agents"
    agents.mkdir()
    (agents / "kiro-foo.json").write_text('{"name": "foo-declared"}', encoding="utf-8")
    (agents / "kirocrew-lite.json").write_text("{}", encoding="utf-8")
    project = log.tmp / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "baz.json").write_text('{"name": "baz"}', encoding="utf-8")
    import os

    log.subst.append((os.path.realpath(project), "<PROJECT>"))
    log.subst.append((str(project), "<PROJECT>"))

    def _set_default(name):
        log.add("config.set_default_agent", name=name)

    def _persist_channel(channel_id, activation=None, agent=None):
        log.add("config.persist_channel", channel=channel_id, activation=activation, agent=agent)

    mp = log.monkeypatch
    mp.setattr(handler, "kiro_agents_dir", lambda: agents)
    # The temp directory's place differs per platform (and on Windows may fall under
    # a path the sensitive-path policy names), so the verdict is pinned to the one
    # every recording got: an ordinary directory.
    mp.setattr(handler, "is_sensitive_path", lambda *args, **kwargs: False)
    mp.setattr(handler, "_set_default_agent", _set_default)
    mp.setattr(handler, "_persist_channel_config", _persist_channel)
    for text in (
        "!agent",
        "!agent zzz",
        "!agent foo",
        "!agent off",
        "!agent a b",
        "!ta foo",
        "!ta",
        "!channel agent foo",
        "!channel agent off",
        "!channel agent",
        "!channel mention",
        f"!project {project}",
        "!project",
        "!ta baz",
    ):
        await _command(log, text, conversation_log=_ConversationLog(log))
    log.add("overrides", agents=handler._thread_agents, projects=sorted(handler._thread_projects))


async def _scenario_tool_then_rejected_while_streaming(log):
    """The answer ("looking") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins."""
    events = [
        _text("looking"),
        LLMEvent(kind="tool_call", title="Running: Read", tool_kind="read", tool_purpose="peek"),
        _permission("r-rej", "Bash", "rm x"),
        _text("never"),
        _complete(),
    ]
    provider = _Provider(log, events)
    sessions = _Sessions(log, provider)
    slack = _Slack(log)
    turn = asyncio.ensure_future(
        handler.handle_message(
            slack,
            sessions,
            "C1",
            "go",
            "1700000000.000001",
            "1700000000.000002",
            "UOWNER",
            approval_mode=handler.APPROVAL_INTERACTIVE,
        )
    )
    await async_wait_until(
        lambda: handler._pending_approvals or turn.done(),
        describe=lambda: f"pending={sorted(handler._pending_approvals)} turn_done={turn.done()}",
    )
    key = sorted(handler._pending_approvals)[0]
    await handler.handle_interaction(
        "C1", key.split(":", 1)[1], handler._ACTION_REJECT, user_id="UOWNER", sessions=sessions
    )
    await asyncio.wait_for(turn, timeout=30)
    await _settle()


async def _scenario_unstreamed_tool_and_auto_approval(log):
    events = [
        LLMEvent(kind="tool_call", title="Running: Bash", tool_kind="execute"),
        _permission("r1", "Bash", "ls"),
        _text("after tool"),
        _complete(),
    ]
    await _turn(log, events, stream=False, approval_mode=handler.APPROVAL_AUTO)


async def _scenario_comment_hold_and_brackets_at_end(log):
    events = [
        _text("Answer [x] text\n"),
        _text("<!-- keep-visible -->"),
        _complete(),
    ]
    await _turn(log, events)
    await _turn(log, [_text("tail <!-- nope"), _complete()])
    await _turn(log, [_text("ends in a [bracket"), _complete()])


async def _scenario_reasoning_only(log):
    """A turn with reasoning and no answer, then one whose answer is withheld.

    The second answer ("x") is one short alphanumeric run, which the stream redactor holds back
    as a possible credential: ``stop_stream`` still posts it, and the turn is booked as a
    failed delivery. That is the base behaviour this golden pins.
    """
    await _turn(log, [LLMEvent(kind="thinking_chunk", text="hmm " * 300), _complete()])
    await _turn(log, [_text("x"), _complete()], thread_ts=None)


async def _scenario_hook_auto_reply(log):
    from kiro_crew.hooks import AutoReplyHook, HookManager, HooksConfig

    hooks = HookManager(
        HooksConfig(auto_replies=[AutoReplyHook(pattern="ping", reply="pong", exact=True)])
    )
    await _turn(
        log,
        [_complete()],
        text="ping",
        context_builder=_ContextBuilder(log, hooks),
        conversation_log=_ConversationLog(log),
    )


async def _scenario_linked_dashboard_session(log):
    """A thread the session map routes to a dashboard session: re-hydrate, mirror."""
    slot = _Slot("chat-7")
    slot.appended = []  # type: ignore[attr-defined]
    slot.append = lambda role, text, cls, meta=None: slot.appended.append(  # type: ignore[attr-defined]
        (role, text, cls, meta)
    )
    slot._on_message = None  # type: ignore[attr-defined]
    log.monkeypatch.setattr(handler, "_dashboard_state", _Dashboard(log, {"chat-7": slot}))
    await _turn(
        log,
        [_text("linked answer"), _complete()],
        owner="dashboard:chat-7",
        conversation_log=_ConversationLog(log),
    )
    log.add("slot.appended", rows=slot.appended)  # type: ignore[attr-defined]


async def _scenario_pinned_answer(log):
    await _turn(
        log,
        [_text("pinned reply"), _complete()],
        route_pinned=True,
        asker_key="cron:job-1",
        target_slot_name="missing",
        conversation_log=_ConversationLog(log),
        owner="dashboard:other",
    )


async def _scenario_cancelled_messages(log):
    await _turn(log, [_text("never"), _complete()], cancelled=("1700000000.000002",))


async def _scenario_turn_ceiling_and_shutdown(log):
    from kiro_crew.messaging import turn_ceiling
    from kiro_crew.session import SessionClosingError

    class _Refused(turn_ceiling.TurnCeilingExceeded):
        announce = True

        def __init__(self):
            Exception.__init__(self, "paused at the ceiling")

    def _gate(session_key, inner=None, **kw):
        def _run():
            raise _Refused()

        return _run

    log.monkeypatch.setattr(turn_ceiling, "gate", _gate)
    await _turn(log, [_complete()])

    def _closing(session_key, inner=None, **kw):
        def _run():
            raise SessionClosingError("closing")

        return _run

    log.monkeypatch.setattr(turn_ceiling, "gate", _closing)
    await _turn(log, [_complete()])


async def _scenario_shared_runtime_deaths(log):
    from kiro_crew import runtime_death

    log.monkeypatch.setattr(runtime_death, "caused_by_this_session", lambda client: False)
    try:
        for _ in range(6):
            await _turn(log, [lambda: AcpProcessDied("gone")])
    finally:
        runtime_death.clear_shared_deaths("slack:1700000000.000001")


async def _scenario_delivery_failures(log):
    await _turn(
        log,
        [_text("answer"), _complete()],
        stream=False,
        fail=(("post_message", 1), ("post_message", 2), ("post_message", 3)),
    )
    await _turn(log, [_text("answer"), _complete()], stream=False, fail=("update_message",))
    await _turn(log, [_text("pick [OPTIONS: A | B]"), _complete()], fail=(("post_blocks", 2),))
    await _turn(
        log,
        [_text("pick [OPTIONS: A | B]"), _complete()],
        fail=(("post_blocks", 2), ("post_message", 2)),
    )
    await _turn(log, [_text("working block")], fail=("post_blocks",))
    await _turn(log, [_text("sealed"), _complete()], fail=("stop_stream",))
    await _turn(
        log,
        [_text("draft"), _complete()],
        channel_activation=ACTIVATION_REVIEW,
        fail=("post_ephemeral",),
    )


async def _scenario_presentation_raises(log):
    events = [
        _text("one "),
        LLMEvent(kind="tool_call", title="Running: Grep", tool_kind="search"),
        _text("two"),
        _complete(),
    ]
    await _turn(log, events, fail=("start_stream",))
    await _turn(log, events, fail=("append_task", "append_stream", ("append_stream", 3)))
    await _turn(log, events, stream=False, fail=("post_message",))


async def _scenario_replay_stopped_before_prompt(log):
    events = [_complete(STOP_REASON_COMPACTION_FAILED)]
    provider = _Provider(log, events, transient=True)
    sessions = _Sessions(log, provider)

    async def _reset(key):
        log.add("sessions.reset", key=key)
        sessions._stop_gen += 1

    sessions.reset = _reset  # type: ignore[method-assign]
    await handler.handle_message(
        _Slack(log), sessions, "C1", "hi", "1700000000.000001", "1700000000.000002", "UOWNER"
    )
    await _settle()


SCENARIOS = {
    name.removeprefix("_scenario_"): fn
    for name, fn in sorted(globals().items())
    if name.startswith("_scenario_")
}


#: SHA-256 of each scenario's transcript, recorded by running these scenarios against the
#: one-module ``handler.py`` the split was cut from. A refactor that changes what any
#: scenario sends, audits, counts or asks of the session manager changes its digest; the
#: failure prints the transcript it produced.
_GOLDEN_DIGESTS: dict[str, str] = {
    "agent_commands": "53f8669524467a6e0535e78cb6d7c76320d2941050133cf251e8840208e5e878",
    "append_refused_then_rotated": "9c1eedd5bddda91331b57115e526c08f99efd760808df9e158568d09f55d2bbc",
    "auto_approval_mode": "306f8e3b049b7630c658941173cf939e4750de03c8159847a66848808b034fec",
    "builders_and_filters": "e83b3068f914e362d4738a90e786aa0065c719f516678efb0b6a72daee1eab84",
    "cancelled_messages": "ecbdff98df1658e87972dc20679d6c3eb80d050a383a74f39f4ee06478c68c3a",
    "cancelled_stop_reason": "a597b2ba7db789e3eab7fad4e42c9ee89391d8c4010e01ae5c3f943a0e107a64",
    "cmd_allowed_denied": "0802fc63efe6e37f62e532419b17548240110c89100b8b4925703f80306e2621",
    "cmd_allowlist_and_channel": "0b64b0c42209ad4e75596212067a1b7fd2c7dabdba488324355a4cdcca3f9d43",
    "cmd_compact": "b70200b397f2c31b14e23658e410ed24509d3db9a8581262cd721a2892793431",
    "cmd_dashboard_bad_duration": "38a7cb5332a1ba5ff255ec3303f56379bdd35ad259fede729cbba8f807d1b162",
    "cmd_link_to_dashboard_unavailable": "3a7c8916de8eec9ddf603e7ccaaa8d892e682a9f5d7a56783277ea8e64955fdd",
    "cmd_owner_only_denied": "91655cbd5755fc47ca671f71bff1ef08fb9e5bd93da44cfa302a56330f569275",
    "cmd_sessions": "637197868fc541a4013406679648954e232c2467f86a34afcee2c9c09ba8bf15",
    "cmd_status": "99a5ab8189db5fc1241f21fd9fbc161a6696609d0597bd78cd183b445e00a763",
    "cmd_stop_no_session": "de5c5a671702ba32d7a8fef5d2528b07a3803d772f21f8d5a0aa64206f5b1d42",
    "cmd_stop_running": "1698b578b632c700afdb4c0a717b6ae66493a42dda13e6a9be522506f133262e",
    "cmd_ta_and_project": "da91983c4edfed45f574c7c82096c52ab7f94d02aa2b005a8abb698c9b15e58b",
    "cmd_title": "5f25036c41f40fff19e669411f763e9568c27bac69821dd769da1e49c536e205",
    "cmd_unknown_bang": "3ac3bfd77e7207cdf6a0a2d5c394d7acdcf42c7833ef8f49d17bdd4ff17ddc4a",
    "cmd_voice": "3688c2b4bd978ee8cd953b840db52c9d27ad24eb6bf9e5746c5832ba9b8ad95e",
    "cmd_yolo_status": "5370a104288a5f4cd6c22a392cc9c04d006d7cf1bcdaaf60ff4a95a1a6b2ee16",
    "comment_hold_and_brackets_at_end": "d0fd5228d627cd7a7c7a9e9d340cb7b82f6227512a6004c2045d9b77ce25722f",
    "compaction_failed_permanent": "12425d478afceb49e320ea9ea894199006a9905edf42d2467d43daba9d7881db",
    "compaction_failed_transient_replay": "0288ec1f38a1eb4f4ab8c73ca19cf7f7434715a6671b2c14b4a0ff046dbc0e51",
    "credential_redaction": "fb606f07f49ed7c4b3bb6b0ce484ac99fd7d8879a8cd287daca999b93d2d80e0",
    "delivery_failures": "6b2655727d8f6981481f77732fce4e09c3e3589d2632d06c54814783bd228380",
    "error_acp": "8efbab04ebf42b0989c9d19a805da15a77bd032d920f031a215a66f7ea6f2e4c",
    "error_after_partial_text": "816e7d73a1bd9eed97fb1027758343201ff318e64eae58df0e399c4c69384171",
    "error_from_trusted_bot": "f347570f5f67a5ca3659343ec439a0802b29970f757e66c3a5330e530afac193",
    "error_process_died": "d35a33b0b576068801e5fe522cc02df56a62acf4d66d4e58e7e294311cd15815",
    "error_prompt_busy": "3617d636151fb29f5ff88ac9e803a226a1f77bec56fc013e1bf32f9a65484de6",
    "error_timeout": "608f9764fe3ed40a4e758a69d881f2c829e08a6aa89302e9f007a1c1b463db44",
    "error_unexpected": "ae7bc265f3ffd8441f818df443f1586b1e0f3c6d2009d7b707220f0b6e92829a",
    "hook_auto_reply": "f976c5ff2c752a2259381bcfd9fbf6e2fb72ca95fcdb1bbfa53c5ddf98f68f53",
    "hook_deny_and_flag": "0cf7af0e949723738338810993c3454dbe2da49ca88c231905b2a68fc36944eb",
    "interaction_edges": "80e9ab4e4ba623f57a0360df952f7d6c787348fc81ff69368bbfcfb2ce22bc8f",
    "interactive_approve": "b20c7df4968528b252f984f62503202294c5c0caa75eb04be3231f2310ef5110",
    "interactive_approve_answered": "08dd0d7d62480853b98cff28fa65d158d327339f2dd69ed2b74fbbbfe8c6356a",
    "interactive_reject": "7e51857cb81a6b1806af280a1f441a7962f3f2e4e7ce8dd3cb440f6dfc1960c6",
    "interactive_timeout": "e2a9e8c0d6398cbe68f81f17eb94ade5c20921c6221d4100f521ac5736448e30",
    "interactive_trust_in_dm": "8b16590ebe23568228ed607a24202a50b8edc4e80aef76e5774c7bef013e5ad5",
    "keyword_commands": "2acbad12808587a76ab0d64101da6d512b3ba8f0bc1f28ed2dab246d7a88b386",
    "linked_approval": "8a85aaf602b5281707519634a49589c814694ac0f9c7b71164bbdaa2cc474bc9",
    "linked_dashboard_session": "e6506d984333f8528fd5f563d745448b0d4c3a83d28a3fa900b6bb8f222f3856",
    "linked_thread_routes_to_slot": "0a481316843499aaa985a421a16e4c2510f5a1ff7b1a9a56e9f6cf4a37832e6e",
    "mention_prefixed_bang": "48e18741859e097ccf60e7d04b6c70546baa391b3451b065b997348a4a5d7734",
    "pinned_answer": "28c748b1cf0d80d87ab2efccaf102d311fcc583d2a63992bc2fb86a5fb829016",
    "presentation_raises": "648684b8c85a999655782b4553854eea6e4b85302ecf65951643680add824f8d",
    "privacy_modifiers": "4d2844c96b28e07a815c7e1b3d2e943d5bd70e3fb7457dd4106f6cf4781d12ec",
    "reasoning_only": "c89a99216bc42101b006b40d7dce9f582dae7fe5c8c53731764517c963dfc88f",
    "replay_stopped_before_prompt": "a47f827241dc1d097d5a9e3f154f58f6031e925ebe35dc0196713445e56a812f",
    "review_mode": "89331664b810662dc31b0a11065705d42ef651896c65e7f6a89a32958509a53a",
    "shared_runtime_deaths": "7448fcb0e2a7665b8dfbf9e6343876d16fa9f2dab262ac2db302f9defef129d7",
    "status_reactions": "0c53791195fe55281c9103c0dbe4e8b101d63bfaba331f0856f6fe82fa35f9cc",
    "streamed_turn": "9bcdb4e071c8a4399e0f4e42b40c7103e4bf35572323d07fa08c4f93573f98e2",
    "tool_then_rejected_while_streaming": "470973604465d0e22ca8e541d8ff6039b68cdf04ceb0cbadb5d109d9aecfa124",
    "top_level_no_placeholder": "da9023327a43497b07e9ca44f6cb54e53eed7d1cfb8a048e4d09531e52cabce7",
    "transport_floor_refusal": "9fb31cc46463b655120bcd722e0a1a7b70c07037bc472d606b76c451bab30ea3",
    "trusted_session": "07445e20db083c7eaf2f75b3590ad82a9637bc4add04a00226c42b26b3ec4058",
    "turn_ceiling_and_shutdown": "e19f2cde78799109801f78a2eea9e872d4ca85828085cb2d59ae4ff03b0864cd",
    "turn_with_history": "db9b4f05d1388068d09b10eb0b2da0817ad379083046c29d43381f76757ead10",
    "unstreamed_long_reply": "24762f37e6a615577ccdb6f679d8c5cc8bc217a41620d12a1c1306d454cb4a46",
    "unstreamed_tool_and_auto_approval": "cfe992c51ae6285338d5081142175a990428ecd963ab312766c8439b8f697c1f",
    "voice_reply_requested": "24c013573da87bd3c996764096b05815b5104674ed61feca7897e9ad10eac93f",
    "wait_tool_seals_stream": "198489a363db046df74be769a5ee27de977f3d53c65d8a145d7209d23fbfb394",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_golden_transcript(world, scenario: str) -> None:
    await SCENARIOS[scenario](world)
    digest = world.digest()
    assert digest == _GOLDEN_DIGESTS.get(
        scenario
    ), f"GOLDEN-DIGEST {scenario} {digest}\n{world.text()}"


def test_every_scenario_has_exactly_one_recorded_digest() -> None:
    assert sorted(_GOLDEN_DIGESTS) == sorted(SCENARIOS)


@pytest.mark.asyncio
async def test_a_refused_append_rotates_and_discloses_the_debt_before_the_seal(world) -> None:
    """The order inside the rotation golden, which its digest pins but does not name: the
    refused append rotates the stream (the continuation opens with ``_STREAM_CONTINUED``),
    the retry there is refused too so that chunk is delivery debt, the next chunk lands,
    and ``DELIVERY_DEBT_NOTICE`` reaches the stream before the seal that ends it."""
    await SCENARIOS["append_refused_then_rotated"](world)
    kinds = [row[0] for row in world.rows]
    starts = [i for i, kind in enumerate(kinds) if kind == "slack.start_stream"]
    assert len(starts) == 2
    assert world.rows[starts[1]][1]["text"] == handler._STREAM_CONTINUED
    assert kinds.count("slack.append_stream.refused") == 2
    landed = [
        i
        for i, row in enumerate(world.rows)
        if row[0] == "slack.append_stream"
        and starts[1] < i
        and row[1]["text"] != handler.DELIVERY_DEBT_NOTICE
        and kinds[i + 1 : i + 2] != ["slack.append_stream.refused"]
    ]
    notice = [
        i
        for i, row in enumerate(world.rows)
        if row[0] == "slack.append_stream" and row[1]["text"] == handler.DELIVERY_DEBT_NOTICE
    ]
    seal = max(i for i, kind in enumerate(kinds) if kind == "slack.stop_stream")
    assert landed and len(notice) == 1
    assert starts[1] < landed[0] < notice[0] < seal
    assert "sessions.record_success" in kinds[seal:]
    assert "stats.inc_message_failed" not in kinds
