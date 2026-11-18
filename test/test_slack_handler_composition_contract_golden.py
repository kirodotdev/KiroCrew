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
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import async_wait_until

#: A token by its published format. The AWS documentation example key ID these
#: scenarios used is an identifier the redactor keeps visible, so it redacts nothing.
_TOKEN = github_classic(seed=145)

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
        _text(f"key {_TOKEN} and "),
        _text(f"see https://evil.example.invalid/x?d={_TOKEN}"),
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
        self._deferred_notes: list = []
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

    def resolve_slot_approval(self, slot, request_id, approved, *, expected_future=None):
        self._log.add(
            "dashboard.resolve_slot_approval",
            slot=slot.key,
            request_id=request_id,
            approved=approved,
        )
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
            f"Bash https://evil.example.invalid/?k={_TOKEN}",
            "echo hi",
            slot_key="chat-1",
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
    await _command(log, f"route me https://evil.example.invalid/?t={_TOKEN}")
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
    "agent_commands": "b93ca58a17fc76956d833e54b7ffbd5696eb7515125f4433026426aed0068b4e",
    "append_refused_then_rotated": "74ae47d01df67f0a965be8019ad238f9dafcf06a8a36fd928979968bacd90942",
    "auto_approval_mode": "29509a2d62229a76a6fa55f510926911391f9313d9a6707fe93ebb0cfd8295e2",
    "builders_and_filters": "e3e97a7b178d3f9eb3f9f848454cd0ed5176145027a3450bbcd1213406249ccd",
    "cancelled_messages": "2994cd1c4a6c3ff282f1f2ddefdd5b81a0c8abc3ea88ac27640f186cde719051",
    "cancelled_stop_reason": "226839e24ee48a41dbe3c12937c08dbabdf61d4bb12619e6be2b488a05971c09",
    "cmd_allowed_denied": "beb016ef3125295cf0f42d2fb9eed059f1189de6afa2fd2656088f5f65ab03ab",
    "cmd_allowlist_and_channel": "a65200c6077f9e606d76a838e8adbf1afa83d947f1234b07e1fa3933986a25b4",
    "cmd_compact": "acde6b121f0029b954b26305e0035ef3655ff9c67b80676428e9db634c489fb3",
    "cmd_dashboard_bad_duration": "f1f95df4f18df7b7d7c21e9b222c80b90e00ae21536a36b0f441ddc8bfe45763",
    "cmd_link_to_dashboard_unavailable": "25c5eae0dc48ad7d0125ddb5c432397b8aba852511ab8f061efc6d8ff86c2f2c",
    "cmd_owner_only_denied": "a7755851bd9026b718f74e37ca04b46524f2ae63024053887fbb98f7e9bbccbe",
    "cmd_sessions": "637197868fc541a4013406679648954e232c2467f86a34afcee2c9c09ba8bf15",
    "cmd_status": "99a5ab8189db5fc1241f21fd9fbc161a6696609d0597bd78cd183b445e00a763",
    "cmd_stop_no_session": "a81987d96f25e0732c71ebc18e27ee00148f7768e4ffd0bd39e381aa273f0c1a",
    "cmd_stop_running": "8b80053065f319c5c216d212a01d86344311d51876d9d1ac09c50731e531b484",
    "cmd_ta_and_project": "8b36f1d7d80aacd9063243e17c72bf3582feafdc1b1de182060e3baae2ccb3f4",
    "cmd_title": "b008fbe213c86c0e7051926bd790ae650e4624b75784a5abe0beebe89e68d15e",
    "cmd_unknown_bang": "0565bd2700edba4cc2222b8b9597bdb651c5a78ea7e3adf16a542511f02c0b23",
    "cmd_voice": "50e0d19d8380a1174c4099bc40efa270dc3fa08b574dfef152624ee2ad838ad5",
    "cmd_yolo_status": "18be13a948c8b441fb85e7cdd52b7339509bce6135cb5b0e9dd5acb41d8a87e9",
    "comment_hold_and_brackets_at_end": "156ac4dae1a1f2e4f6bbe265041216701d5c535f0ae9eade87b56aafe729090d",
    "compaction_failed_permanent": "8e17c4182dc6a589a4c8a18fab5e0b27749c01eea3f57dbffebbd7ea755676c7",
    "compaction_failed_transient_replay": "b82af0d3699c9a57b866826e528d2397866269361055ee8524a46f1b3ba63b47",
    "credential_redaction": "e9a2c2a3b96584afb8f3194ad6644f3b9e7e9956cd8a2df9d967143dd05dc4bf",
    "delivery_failures": "3e1a09065665a53e4aac5e0681a8ab729e3a81de30c766bb82950b77e12d411b",
    "error_acp": "06fd2d5469afde23037dbeeff71e1eaec744018ede6ef0fe8cb1d79132ac0666",
    "error_after_partial_text": "ef25f5c91959262c1f7c12c621a7183299b321ccfd5bbb12a209111fcd2ff86f",
    "error_from_trusted_bot": "28c4ce364163847a6daa7c47fc8fe91a341b8dba705fbddf40380e6b7855f527",
    "error_process_died": "84bf034e1a81de8b5d995a776abf24a64f132e14acca2ec53475d7a0adbd106b",
    "error_prompt_busy": "328e6f93adbf66eb25440c99f4ba97689ea228057744f6d372f9bc4759f28d33",
    "error_timeout": "0b380d6808ca34ad207ca6ea018683daf76b87ca63486f8dc246028a554811ab",
    "error_unexpected": "94748466a9d086a4e62dc0ce19b8d922fc3f7adc5efd66692b7fb7ead03d35a5",
    "hook_auto_reply": "f976c5ff2c752a2259381bcfd9fbf6e2fb72ca95fcdb1bbfa53c5ddf98f68f53",
    "hook_deny_and_flag": "10b9ff1ecf3925cb725a923893e7112603f5e9ce92b468795c11cb712cecb86d",
    "interaction_edges": "80e9ab4e4ba623f57a0360df952f7d6c787348fc81ff69368bbfcfb2ce22bc8f",
    "interactive_approve": "7910e6d9314a953f6d1bfc401b42d8438e3deeb5e14cd3911591d2b0549872e2",
    "interactive_approve_answered": "578cb36b02a09c12487dc7a6213ce88bf03deb12882f9b95a7451f4fa7aa277c",
    "interactive_reject": "cc8bdd7e35672b6f12f1ba67242ed5d8f29dca687f9214195c0f719f7b69e536",
    "interactive_timeout": "010ca2d494419cf23be3ac932f9c5a17688390468412fd008a33eff9ae93f291",
    "interactive_trust_in_dm": "272c663033bc6f3c909a8451077b41abc235e7f3e9e14e198b92378e5f46f2fa",
    "keyword_commands": "71cca229aa526865a5eb6301ed375f01a7e1488eacee8ce4b7e5fa18145dabbe",
    "linked_approval": "2345fddc33ddcf0c3153e5a67d543815e135c0fcffc42009ce7265bedb752ed0",
    "linked_dashboard_session": "ecedf09daa6bdda1cd56ac3c2a0b5526f9085add1268244dbda712b1da405a71",
    "linked_thread_routes_to_slot": "03d20e7a8917b6f2bb264c9cb5923905d3240aa9574040de4a7d054dca11ef12",
    "mention_prefixed_bang": "7ef124d7c4d786368f2a5636922467cfdc9d16f5e879b5cb18bbc9a788b43dee",
    "pinned_answer": "007c75173cdc33e068bd9d578b9d45386f64d330b01e59dfdab46268bbcbd47d",
    "presentation_raises": "84346dcfca503b2becffe402f0a33e2f55839967049a5b9322486441f4c3bfa4",
    "privacy_modifiers": "de5ca3b278d8cfc2c651eb7bd03a00f7e89a883d2391fe19a66b484e93610322",
    "reasoning_only": "6375727423d844ae68f7e7477cc4bcf9887df592d607b2831e0e694a10eb47bb",
    "replay_stopped_before_prompt": "749a1cedba19eef4067b0ab5669f9d9b394f49d96aa6e61c0ccb310cc487519a",
    "review_mode": "93262f8371c3e7f94825fdedbd9de6c3595ac6b3c1664606e3c910364062961f",
    "shared_runtime_deaths": "9eca1aef40b253cd0d1bc127d5261947c6518da02e4c4be684865cac6d90f844",
    "status_reactions": "0c53791195fe55281c9103c0dbe4e8b101d63bfaba331f0856f6fe82fa35f9cc",
    "streamed_turn": "77f9fa5a4978301a6e1edd5df453230fc656b91b0ce4928db86444ff440aa4b8",
    "tool_then_rejected_while_streaming": "a9de9e7b2b6a32c76ecc102074985b6ef5ec81bb642f1aab9628f37d0c936680",
    "top_level_no_placeholder": "a902ae33e7c04a9f44e48bbb95673cea8cc19d3ef2d08da445694662820f4b26",
    "transport_floor_refusal": "e05a6f7876506a74ba121b858e7efacb950977221d12c4859e24a74d145dc141",
    "trusted_session": "f6f137a62a8308591a242c5964a4c50c4c24d842f26b796ccf7a12bd84653fd7",
    "turn_ceiling_and_shutdown": "007400c245c31698a0e019a54a3c48a4fac8a525c85a556137680ff74bda5231",
    "turn_with_history": "1f18b5eb2ff01b64f886b1556b51352c78e3633334206870d9a5f12aa470330c",
    "unstreamed_long_reply": "8c1c1fef5bf150b5d5782e8eb207e441a6425673f5f37ee7aa9076f0d01e9997",
    "unstreamed_tool_and_auto_approval": "fbd822b1838c1ef3d1eee93990997623af256fb1333996fa321457a1498f310c",
    "voice_reply_requested": "de80b651354969dd70e73a271a6e6719226dfa77f961f1df2694a16de9f948e4",
    "wait_tool_seals_stream": "be9f2cc50bf179a53d895a00ce6cc4b24b5f1f156c911ff72d73e17b78539ae6",
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
