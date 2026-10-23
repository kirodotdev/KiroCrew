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
    """The phase ladder, debounce and stall watchdog, with each timer callback fired by
    hand: the real timers are parked an hour out, so the order is the code's alone."""
    slack = _Slack(log)
    ctrl = handler.StatusReactionController(slack, "C1", "1.0")
    ctrl.set_phase("queued")
    await _settle()
    ctrl.set_phase("thinking")
    ctrl.set_phase(handler._tool_to_phase("mcp__x__Bash"))
    ctrl._fire_debounce()
    await _settle()
    ctrl._on_stall_soft()
    await _settle()
    ctrl._on_stall_hard()
    await _settle()
    ctrl.on_progress()
    await _settle()
    ctrl.pause_stall_watchdog()
    ctrl._on_stall_soft()
    await _settle()
    ctrl.resume_stall_watchdog()
    ctrl.set_phase("error")
    await _settle()
    ctrl.set_phase("thinking")
    ctrl._on_stall_hard()
    await _settle()
    off = handler.StatusReactionController(slack, "C1", "2.0", enabled=False)
    off.set_phase("queued")
    off.finalize()
    await _settle()
    log.add("timers", soft=ctrl._stall_soft_handle is None, hard=ctrl._stall_hard_handle is None)


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
    log.add(
        "phases",
        out=[
            handler._tool_to_phase(t, k)
            for t, k in (
                ("Bash", ""),
                ("x", "edit"),
                ("WebFetch", ""),
                ("y", "fetch"),
                ("mcp__a__Grep", ""),
                ("z", ""),
            )
        ],
    )
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
    "agent_commands": "39b8b3d66b3217fb6317adc4771441310d0643612260626511b63446b106b669",
    "append_refused_then_rotated": "a9a19cb62f8ff1e7a7b1908cbca4c1b540d49637e4986ad37fbfdd6f2cd51d34",
    "auto_approval_mode": "70f52018d5f65bf50cd313b653a57df4fe0c691764352ddfc2ef81dd1b071411",
    "builders_and_filters": "18a6d4f9613d0ee226214b549f0b821676bd9083bc843aadbc27b3e1f2030872",
    "cancelled_messages": "fcfbabbeaa380023b98273c08331bbae08d6971a5c5237c6f3b218d827aac943",
    "cancelled_stop_reason": "38c292699d20bc4d1b460b264e2ca9cb62a3e67ddbf465434566d482b7a1f50c",
    "cmd_allowed_denied": "0802fc63efe6e37f62e532419b17548240110c89100b8b4925703f80306e2621",
    "cmd_allowlist_and_channel": "0b64b0c42209ad4e75596212067a1b7fd2c7dabdba488324355a4cdcca3f9d43",
    "cmd_compact": "6e48bff6ebaa74ae629d99c5f6977e10e73aed3c42f10c7f906cfc91d65f2a62",
    "cmd_dashboard_bad_duration": "38a7cb5332a1ba5ff255ec3303f56379bdd35ad259fede729cbba8f807d1b162",
    "cmd_link_to_dashboard_unavailable": "3a7c8916de8eec9ddf603e7ccaaa8d892e682a9f5d7a56783277ea8e64955fdd",
    "cmd_owner_only_denied": "91655cbd5755fc47ca671f71bff1ef08fb9e5bd93da44cfa302a56330f569275",
    "cmd_sessions": "637197868fc541a4013406679648954e232c2467f86a34afcee2c9c09ba8bf15",
    "cmd_status": "99a5ab8189db5fc1241f21fd9fbc161a6696609d0597bd78cd183b445e00a763",
    "cmd_stop_no_session": "de5c5a671702ba32d7a8fef5d2528b07a3803d772f21f8d5a0aa64206f5b1d42",
    "cmd_stop_running": "1698b578b632c700afdb4c0a717b6ae66493a42dda13e6a9be522506f133262e",
    "cmd_ta_and_project": "0c1a7c878d745efc579eff89b6ebc4de99cb1fdfadecb29560afa5942a370443",
    "cmd_title": "d2d928c6a0e2a5a6b1b0cafe0cad309a92d321947c1e42e4f35de7b240b40bd5",
    "cmd_unknown_bang": "3ac3bfd77e7207cdf6a0a2d5c394d7acdcf42c7833ef8f49d17bdd4ff17ddc4a",
    "cmd_voice": "303e13ef1d640f338b7456d4e070d9e66c66a1173a468b1ad67bb407fd25cbf7",
    "cmd_yolo_status": "5370a104288a5f4cd6c22a392cc9c04d006d7cf1bcdaaf60ff4a95a1a6b2ee16",
    "comment_hold_and_brackets_at_end": "2a567067edee4ea284bf3a3a59d9136943f2c329f8f9f05587e370105c1a8172",
    "compaction_failed_permanent": "b2b310eeab6a25f773777e6d4b3fb1fd7fbdd04f5f103983b85e2a7ad790f3d3",
    "compaction_failed_transient_replay": "5d66704abad764ed644d01d6a0b59c01cdc5c63460a8140b2224401aa191e601",
    "credential_redaction": "0782b2c2f1c02318bb8fae507aa566609357c98c52fa856e63e27564c9452177",
    "delivery_failures": "27aa79d438fdd6b057e6e50055c1db0fc6c840de810ac3618d74297057ac1ab3",
    "error_acp": "8efbab04ebf42b0989c9d19a805da15a77bd032d920f031a215a66f7ea6f2e4c",
    "error_after_partial_text": "816e7d73a1bd9eed97fb1027758343201ff318e64eae58df0e399c4c69384171",
    "error_from_trusted_bot": "f347570f5f67a5ca3659343ec439a0802b29970f757e66c3a5330e530afac193",
    "error_process_died": "d35a33b0b576068801e5fe522cc02df56a62acf4d66d4e58e7e294311cd15815",
    "error_prompt_busy": "3617d636151fb29f5ff88ac9e803a226a1f77bec56fc013e1bf32f9a65484de6",
    "error_timeout": "608f9764fe3ed40a4e758a69d881f2c829e08a6aa89302e9f007a1c1b463db44",
    "error_unexpected": "ae7bc265f3ffd8441f818df443f1586b1e0f3c6d2009d7b707220f0b6e92829a",
    "hook_auto_reply": "f976c5ff2c752a2259381bcfd9fbf6e2fb72ca95fcdb1bbfa53c5ddf98f68f53",
    "hook_deny_and_flag": "128570569dc7d30d1327ba223a903e3d1da2c190df536c018cc8555f04bfb1da",
    "interaction_edges": "80e9ab4e4ba623f57a0360df952f7d6c787348fc81ff69368bbfcfb2ce22bc8f",
    "interactive_approve": "e6f33da7bfb82b921f184c46ac76705f6a2d0bfb3841c230d5be7c30c5783e47",
    "interactive_approve_answered": "cbcfe6a559c59c8314699ebb58b1ca04c0ea7e0cc01b301fc345b93d7f7fc963",
    "interactive_reject": "2caf89795617e42d1a38dddcc05a93bdba195fb10036f67d71438e824e44cfee",
    "interactive_timeout": "8767f772a5d23a1d318c0fe1ff69acc56bad7b024d552a4e4c5f132be2e024a4",
    "interactive_trust_in_dm": "d1f9b4ac5a71a94236bf697afab088161b9dd828f1aa8beee479ed6c02e76bbb",
    "keyword_commands": "0ef1fd95b16ed0bbdcce2999f6632c4be8acc92e943cd4a69c05ccd5ffb2fb73",
    "linked_approval": "8a85aaf602b5281707519634a49589c814694ac0f9c7b71164bbdaa2cc474bc9",
    "linked_dashboard_session": "224ca7d87df8c73e94811b56554a3000701402072c1dd2b46cb770803ccd24b0",
    "linked_thread_routes_to_slot": "ac7b70b93a3f485e84491edcac4cb8eb3e5aff6fa8a54a23846d8dae6be746af",
    "mention_prefixed_bang": "f6ba6be59eb71104d3fa7202fd9bbcf91a5a6376f253db3fda41a256d468ab1e",
    "pinned_answer": "c590ad8ef702c7284d257b0d486a8c6b69f76255d32a5d0ae4dd896ad6f549d5",
    "presentation_raises": "361a0de433fd48b58f859146ae2c68a7d9519ea8181399752673f044c299d2ce",
    "privacy_modifiers": "4061ede8e707257cf7903974e6de0a090562202d606bb78e60b2554d945ec8ba",
    "reasoning_only": "a18cd8aa10fbb3dcbb6897ce5fb316ec5ebc84acd12632c4570a295e7bf6721c",
    "replay_stopped_before_prompt": "148eaa37b40ed17826eae6b36aaa56909402e9e42b20ad1dd8f4c4b8fed192cb",
    "review_mode": "2c6cf08bc3259143b5e8711d64adc1f109ee1f1a6fa5a98caa1be62f3d4951f6",
    "shared_runtime_deaths": "7448fcb0e2a7665b8dfbf9e6343876d16fa9f2dab262ac2db302f9defef129d7",
    "status_reactions": "14c5040b7db5254737f18a51d8aefce91330427f59142ee4dc59106f8bb098dd",
    "streamed_turn": "e10e8c45137bb50f1c38513b9b5c25c7b0450381277d18f3ae7e4e286cd95887",
    "tool_then_rejected_while_streaming": "fb8a0d0b3be37ee59571bf1482fe4c14049899f301ed538ad079da4728af020b",
    "top_level_no_placeholder": "416dbaac1ae3932ccb2df432ea72b0d56ab0e9dd8455eafa04baf1c274172891",
    "transport_floor_refusal": "8bc29ce9634f29549dbcab78cb139f12f0eb565ea92cc8a6856f6aa8eaae1803",
    "trusted_session": "33cdb5f7e51781f7636d4a8591bda14b7b57c094df5590633f5124d4bf05759d",
    "turn_ceiling_and_shutdown": "3ac9dcaeb5c6c42bf8886118f06df788a8b280da346addf25dbc811baf9e2c9d",
    "turn_with_history": "bcc140758baf11312bd9e0bd2ec299c92cc4f51802eb5d8afb0e376d29799583",
    "unstreamed_long_reply": "a338e70172346e12ef9c1d2a7cc155041dfdb54d5e155bd5fb3aa56a58601a94",
    "unstreamed_tool_and_auto_approval": "a771de255da501fa0a5a7a2636fd02cfa115f6bd75e5fd0a058b891bf890acf8",
    "voice_reply_requested": "f34a357cb6cb48ddedca70ba9ac0c4abfea5831ebaf9d9511f25936f311433b2",
    "wait_tool_seals_stream": "da9e4d35f2fb9f1460b361a5b82cfe66096dbc4e4e5243163b984a3df434a037",
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
