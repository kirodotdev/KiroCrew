"""The native Slack handler keeps its wire behaviour and its surface while its owners move.

``kiro_crew.slack.handler`` is the native Slack turn path's import path and its patch
surface. The goldens below drive its public callbacks against recording doubles and pin
what reaches Slack, the SEL ledger, the stats counters, the session manager and the
provider, in order, as one transcript per scenario.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import enum
import hashlib
import importlib
import importlib.util
import inspect
import json
import pkgutil
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from source_corpus import repo_root

from conftest import MockSlackClient
from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler, handler_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# ── recording doubles ─────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """A JSON-stable rendering: no reprs, so no addresses."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


class _Transcript:
    """One ordered record of everything a scenario's handler call did."""

    def __init__(self) -> None:
        self.rows: list[list[Any]] = []
        #: Host-specific strings (a temp directory) replaced by a stable token, so a
        #: transcript is the same on every platform.
        self.subst: list[tuple[str, str]] = []

    def add(self, kind: str, **payload: Any) -> None:
        self.rows.append([kind, self._scrub(_plain(payload))])

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for raw, token in self.subst:
                value = value.replace(raw, token)
            return value
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def text(self) -> str:
        return json.dumps(self.rows, indent=1, sort_keys=True, ensure_ascii=True)

    def digest(self) -> str:
        return hashlib.sha256(self.text().encode("utf-8")).hexdigest()


class _SharedActions(list):
    """``MockSlackClient.actions`` that also lands each Slack call in the transcript."""

    def __init__(self, log: _Transcript) -> None:
        super().__init__()
        self._log = log

    def append(self, item: Any) -> None:
        super().append(item)
        kind, payload = item
        self._log.add(f"slack.{kind}", **payload)


class _Slack(MockSlackClient):
    def __init__(
        self,
        log: _Transcript,
        *,
        stream: bool = True,
        append_refusals: int = 0,
        fail: tuple[Any, ...] = (),
    ) -> None:
        super().__init__()
        self.actions = _SharedActions(log)
        self._stream_enabled = stream
        self._append_refusals = append_refusals
        self._log = log
        #: ``(method, n)`` refuses the n-th call of *method*; a bare name, the first.
        self._fail = {(f, 1) if isinstance(f, str) else tuple(f) for f in fail}
        self._calls: dict[str, int] = {}

    def _maybe_fail(self, method: str) -> None:
        self._calls[method] = self._calls.get(method, 0) + 1
        if (method, self._calls[method]) in self._fail:
            self._log.add("slack.raised", method=method, nth=self._calls[method])
            raise RuntimeError(f"{method} refused")

    async def post_message(self, channel, text, thread_ts=None):
        self._maybe_fail("post_message")
        return await super().post_message(channel, text, thread_ts)

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._maybe_fail("post_blocks")
        return await super().post_blocks(channel, blocks, text, thread_ts)

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self._maybe_fail("post_ephemeral")
        return await super().post_ephemeral(channel, user_id, text, blocks, thread_ts)

    async def stop_stream(self, channel, ts, final_text=None):
        self._maybe_fail("stop_stream")
        return await super().stop_stream(channel, ts, final_text)

    async def update_message(self, channel, ts, text):
        self._maybe_fail("update_message")
        return await super().update_message(channel, ts, text)

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        self._maybe_fail("start_stream")
        return await super().start_stream(channel, thread_ts, initial_text, team_id, user_id)

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self._maybe_fail("append_task")
        return await super().append_task(channel, ts, task_id, title, status, details, output)

    async def append_stream(self, channel, ts, text):
        self._maybe_fail("append_stream")
        ok = await super().append_stream(channel, ts, text)
        if self._append_refusals > 0:
            self._append_refusals -= 1
            self._log.add("slack.append_stream.refused")
            return False
        return ok

    async def fetch_message_detail(self, channel, ts):
        self._log.add("slack.fetch_message_detail", channel=channel, ts=ts)
        return None


class _Provider:
    """A provider that replays scripted events; a callable event raises instead."""

    def __init__(
        self,
        log: _Transcript,
        events: list[Any],
        *,
        approve_result: Any = None,
        steer: bool = False,
        transient: Any = None,
        compaction: dict | None = None,
    ) -> None:
        self._log = log
        self._events = list(events)
        self._approve_result = approve_result
        self.supports_refusal_steer = steer
        self.last_compaction_transient = transient
        self._compaction = compaction or {"type": "completed"}

    async def stream(self, message, **_kwargs):
        self._log.add("provider.stream", message=message)
        for event in self._events:
            if callable(event):
                raise event()
            yield event

    async def approve_tool(self, request_id, option_id="allow_once"):
        self._log.add("provider.approve_tool", request_id=request_id)
        return self._approve_result

    async def reject_tool(self, request_id):
        self._log.add("provider.reject_tool", request_id=request_id)

    async def steer(self, notice):
        self._log.add("provider.steer", notice=notice)
        return True

    async def compact(self):
        self._log.add("provider.compact")

    async def wait_for_compaction(self, timeout=None):
        self._log.add("provider.wait_for_compaction", timeout=timeout)
        return dict(self._compaction)

    def context_usage_pct(self):
        return 41.0


class _Sessions:
    """The slice of ``SessionManager`` the native handler drives, recorded."""

    def __init__(self, log: _Transcript, provider: _Provider, *, known: bool = False) -> None:
        self._log = log
        self._provider = provider
        self._sessions: dict[str, Any] = {}
        self._known = known
        self._stop_gen = 0
        self._new = True
        self.thread_owner: str | None = None
        self.cancelled: set[str] = set()

    async def get_or_create(self, key, agent=None, channel_id=None, start_priority=None):
        self._log.add(
            "sessions.get_or_create",
            key=key,
            agent=agent,
            channel_id=channel_id,
            start_priority=start_priority,
        )
        new, self._new = self._new, False
        return self._provider, new, False

    def compact_wait_budget_secs(self):
        """The real manager's resolved ``session.compact_wait_secs`` (unset: 300 s)."""
        return 300.0

    def check_context_usage(self, key, provider):
        self._log.add("sessions.check_context_usage", key=key)

    def record_success(self, key):
        self._log.add("sessions.record_success", key=key)

    async def record_failure(self, key):
        self._log.add("sessions.record_failure", key=key)
        return False

    async def try_acquire(self, key):
        self._log.add("sessions.try_acquire", key=key)
        return self._known

    def release(self, key):
        self._log.add("sessions.release", key=key)

    def begin_turn(self, key):
        self._log.add("sessions.begin_turn", key=key)

    async def set_channel(self, key, channel_id):
        self._log.add("sessions.set_channel", key=key, channel_id=channel_id)

    def set_slack_link(self, key, thread_ts, channel_id):
        self._log.add("sessions.set_slack_link", key=key, thread_ts=thread_ts, channel=channel_id)

    def get_session_for_thread(self, thread_ts):
        return self.thread_owner

    async def remove(self, key):
        self._log.add("sessions.remove", key=key)

    async def discard_conversation(self, key):
        self._log.add("sessions.discard_conversation", key=key)

    def has_session(self, key):
        return self._known

    def get_provider(self, key):
        return self._provider if self._known else None

    async def reset(self, key):
        self._log.add("sessions.reset", key=key)

    def get_pid(self, key):
        return None

    def is_cancelled(self, key, msg_ts):
        hit = msg_ts in self.cancelled
        if hit:
            self._log.add("sessions.is_cancelled", key=key, msg_ts=msg_ts)
        return hit

    def stop_generation(self, key):
        return self._stop_gen

    def consume_needs_reinjection(self, key):
        self._log.add("sessions.consume_needs_reinjection", key=key)
        return True

    def mark_needs_reinjection(self, key):
        self._log.add("sessions.mark_needs_reinjection", key=key)

    def open_replay_gap(self, key):
        self._log.add("sessions.open_replay_gap", key=key)

    def close_replay_gap(self, key):
        self._log.add("sessions.close_replay_gap", key=key)

    async def stop_turn(self, key, *, on_soft=None, on_hard=None, **kwargs):
        self._log.add("sessions.stop_turn", key=key, **kwargs)
        if on_soft is not None:
            await on_soft()
        return "soft"


class _ContextBuilder:
    """The context builder seam: hooks, and a message build that names its inputs."""

    def __init__(self, log: _Transcript, hooks: Any) -> None:
        self._log = log
        self.hooks = hooks
        self.conversation_log = None

    def build_message(self, text, is_new, session_key, **kwargs):
        self._log.add("context.build_message", text=text, is_new=is_new, key=session_key, **kwargs)
        return f"[context]\n{text}", None


class _Sel:
    def __init__(self, log: _Transcript) -> None:
        self._log = log

    def __getattr__(self, name: str):
        def _record(*args: Any, **kwargs: Any) -> None:
            self._log.add(f"sel.{name}", args=list(args), **kwargs)

        return _record


class _StatsFactory:
    def __init__(self, log: _Transcript) -> None:
        self._log = log

    def __call__(self) -> Any:
        log = self._log

        class _Stats:
            def __getattr__(self, name: str):
                def _record(*args: Any, **kwargs: Any) -> Any:
                    log.add(f"stats.{name}")
                    return "STATS-SUMMARY" if name == "summary" else None

                return _record

        return _Stats()


class _Config:
    """The two ``slack`` reads a turn makes and the ``agent`` read the default needs."""

    def __init__(self) -> None:
        self.slack = SimpleNamespace(
            reactions_enabled=True, show_thinking=True, sessions_limit=10, reactions={}
        )
        self.agent = SimpleNamespace(default_agent="")
        self.raw: dict = {}
        self.slack_channels: dict = {}
        self.slack_dm_activation = "always"

    log: "_Transcript | None" = None

    @classmethod
    def load(cls) -> "_Config":
        if cls.log is not None:
            cls.log.add("config.load")
        return cls()

    def channel_config(self, channel: str) -> Any:
        return SimpleNamespace(activation="mention", agent="")


class _StepClock:
    """Each read advances 0.75s, so elapsed footers and the edit throttle are
    deterministic and a changed number of clock reads changes the transcript."""

    def __init__(self) -> None:
        self._now = 1000.0

    def monotonic(self) -> float:
        self._now += 0.75
        return self._now


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    log = _Transcript()
    log.tmp = tmp_path  # type: ignore[attr-defined]

    async def _saved(conversation_log, session_key, user_text, reply_text, **kwargs):
        log.add("history.save_turn", key=session_key, user=user_text, reply=reply_text, **kwargs)
        return "1700000000.000100"

    async def _published(sessions, session_key):
        log.add("identity.publish", key=session_key)

    async def _store(context_builder, session_key):
        log.add("memory.store_for_turn", key=session_key)
        return None

    async def _permitted(channel_type):
        log.add("governance.inbound", channel_type=channel_type)
        return True

    async def _auto_title(*args, **kwargs):
        log.add("auto_title.scheduled", key=args[3], user=args[5], reply=args[6])

    async def _voice(*args, **kwargs):
        log.add("voice.reply", text=args[3], **kwargs)

    def _tts(**kwargs):
        log.add("voice.tts_available", **kwargs)
        return True

    async def _inline(func, /, *args, **kwargs):
        # A thread hop finishes whenever the worker gets the CPU, so a reaction task
        # scheduled before it would land on either side of it in the transcript. Run
        # the work in place: the order then depends on the code alone.
        return func(*args, **kwargs)

    async def _inline_gate(func, /, *args, on_queue_timeout, **kwargs):
        # The gate's queue-bound refusal is the pool's, not the gate's: in place,
        # the call always gets a worker.
        return func(*args, **kwargs)

    for name, value in {
        "sel": lambda: _Sel(log),
        "Stats": _StatsFactory(log),
        "KiroCrewConfig": type("_LoggedConfig", (_Config,), {"log": log}),
        "_orch_cfg": _Config(),
        "time": _StepClock(),
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="0123456789abcdef")),
        "save_conversation_turn_off_loop": _saved,
        "publish_turn_identity": _published,
        "channel_inbound_permitted": _permitted,
        "session_store_for_turn": _store,
        "_maybe_auto_title_slack": _auto_title,
        "_voice_reply_fn": _voice,
        "_tts_available": _tts,
        "run_in_embed_pool": _inline,
        # Companion-plugin agents are discovered under the real home directory, so a
        # host that has some would add them to every agent listing.
        "_iter_cc_agent_names": lambda cc_plugins_dir=None: iter(()),
        "_PHASE_DEBOUNCE_SECS": 3600.0,
        "_STALL_SOFT_SECS": 3600.0,
        "_STALL_HARD_SECS": 7200.0,
        "_owner_id": "UOWNER",
        "_cached_default_agent": None,
        "_dashboard_state": None,
        "_vc": handler._VoiceConfig(),
        "_review_drafts": {},
        "_thread_agents": {},
        "_thread_projects": {},
        "_hydrated_sessions": set(),
        "_pending_approvals": {},
        "_linked_approvals": {},
    }.items():
        monkeypatch.setattr(handler, name, value)
    monkeypatch.setattr(asyncio, "to_thread", _inline)
    # The tool gate hops onto its own pool rather than through to_thread; the same
    # in-place run keeps the transcript order a property of the code.
    monkeypatch.setattr(handler, "run_in_tool_gate_pool", _inline_gate)
    handler._trusted_sessions.clear()
    log.monkeypatch = monkeypatch  # type: ignore[attr-defined]
    yield log
    handler._trusted_sessions.clear()


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def _text(text: str, **kw: Any) -> LLMEvent:
    return LLMEvent(kind="text_chunk", text=text, **kw)


def _complete(stop_reason: str = "end_turn") -> LLMEvent:
    return LLMEvent(kind="complete", stop_reason=stop_reason)


def _permission(request_id: Any, title: str, tool_input: str = "", **kw: Any) -> LLMEvent:
    return LLMEvent(
        kind="permission_request", request_id=request_id, title=title, tool_input=tool_input, **kw
    )


async def _turn(
    log,
    events,
    *,
    text="hello there",
    thread_ts="1700000000.000001",
    stream=True,
    append_refusals=0,
    channel="C1",
    user="UOWNER",
    provider_kw=None,
    sessions_known=False,
    fail=(),
    owner=None,
    cancelled=(),
    **kw,
) -> tuple[Any, Any]:
    provider = _Provider(log, events, **(provider_kw or {}))
    sessions = _Sessions(log, provider, known=sessions_known)
    sessions.thread_owner = owner
    sessions.cancelled = set(cancelled)
    slack = _Slack(log, stream=stream, append_refusals=append_refusals, fail=fail)
    log.add("call.handle_message", text=text, **{k: _plain(v) for k, v in kw.items()})
    try:
        await handler.handle_message(
            slack, sessions, channel, text, thread_ts, "1700000000.000002", user, **kw
        )
    except Exception as exc:
        # Recorded, not swallowed: a raise is part of what the call does.
        log.add("raised", type=type(exc).__name__)
    await _settle()
    return slack, sessions


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
    for _ in range(1000):
        if handler._pending_approvals or turn.done():
            break
        await asyncio.sleep(0.01)
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
    for _ in range(1000):
        if handler._pending_approvals or turn.done():
            break
        await asyncio.sleep(0.01)
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


#: SHA-256 of each scenario's transcript, recorded by running this module against the
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
    "cmd_stop_running": "7a8a362aa0e8c73282bfcaefe11938c8112f066fbfa8a6bcbdafc0ddb04ab91f",
    "cmd_ta_and_project": "0c1a7c878d745efc579eff89b6ebc4de99cb1fdfadecb29560afa5942a370443",
    "cmd_title": "d2d928c6a0e2a5a6b1b0cafe0cad309a92d321947c1e42e4f35de7b240b40bd5",
    "cmd_unknown_bang": "3ac3bfd77e7207cdf6a0a2d5c394d7acdcf42c7833ef8f49d17bdd4ff17ddc4a",
    "cmd_voice": "303e13ef1d640f338b7456d4e070d9e66c66a1173a468b1ad67bb407fd25cbf7",
    "cmd_yolo_status": "5370a104288a5f4cd6c22a392cc9c04d006d7cf1bcdaaf60ff4a95a1a6b2ee16",
    "comment_hold_and_brackets_at_end": "2a567067edee4ea284bf3a3a59d9136943f2c329f8f9f05587e370105c1a8172",
    "compaction_failed_permanent": "b2b310eeab6a25f773777e6d4b3fb1fd7fbdd04f5f103983b85e2a7ad790f3d3",
    "compaction_failed_transient_replay": "5d66704abad764ed644d01d6a0b59c01cdc5c63460a8140b2224401aa191e601",
    "credential_redaction": "e4d2d4d78118cc00c60c2eadcda62cb5aa29183d4a8c1be48f8462f2e5cf2afd",
    "delivery_failures": "27aa79d438fdd6b057e6e50055c1db0fc6c840de810ac3618d74297057ac1ab3",
    "error_acp": "8efbab04ebf42b0989c9d19a805da15a77bd032d920f031a215a66f7ea6f2e4c",
    "error_after_partial_text": "816e7d73a1bd9eed97fb1027758343201ff318e64eae58df0e399c4c69384171",
    "error_from_trusted_bot": "f347570f5f67a5ca3659343ec439a0802b29970f757e66c3a5330e530afac193",
    "error_process_died": "d35a33b0b576068801e5fe522cc02df56a62acf4d66d4e58e7e294311cd15815",
    "error_prompt_busy": "3617d636151fb29f5ff88ac9e803a226a1f77bec56fc013e1bf32f9a65484de6",
    "error_timeout": "0625bccb70ce8ed8e7e690e5a83b1aa394f5ef1d435188f58e1aa07d9deb2f80",
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
    "linked_approval": "9b73c0ca26c933c2337d11b8a6685a94d1da666515dad2d5d87fb3709b31f126",
    "linked_dashboard_session": "92bcfd061c91e4d37fbd3ab18668fc2105934993d1cf348c81ce523334815bc5",
    "linked_thread_routes_to_slot": "b68c87f29b6bcb92d43686a0992ffb574b4fbb551501969505fc9ea79f15bddf",
    "mention_prefixed_bang": "f6ba6be59eb71104d3fa7202fd9bbcf91a5a6376f253db3fda41a256d468ab1e",
    "pinned_answer": "5b2f1d0f2c8c9d2fd561baa8e7c22df5831784437cdc7ec5f677279d4bb8cffd",
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
    "turn_with_history": "8c45c5b3d4822c45a8429e1c60caa054a70675ff3ea4874c48c72ae9dd8cc611",
    "unstreamed_long_reply": "a338e70172346e12ef9c1d2a7cc155041dfdb54d5e155bd5fb3aa56a58601a94",
    "unstreamed_tool_and_auto_approval": "a771de255da501fa0a5a7a2636fd02cfa115f6bd75e5fd0a058b891bf890acf8",
    "voice_reply_requested": "c7447016937c719eb3bbb1d9e0dc753256f7c4b910cdc450aa01efe6fbbb5f5c",
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


# ── the surface ───────────────────────────────────────────────────────────────

#: Every module-level name ``handler.py`` bound at the base this split was cut from:
#: what it defined and what it imported, private names included, because the
#: gateway, the dashboard, the messaging transport and the tests read private names
#: off it. A name bound only by ``import <module>`` of a stdlib module is left out
#: (SD39): nothing reads ``json`` or ``re`` off the facade, and pinning one would
#: fail on the removal of an unused import. The project module objects it imports
#: ARE pinned, because owner functions read them as facade globals.
_BASE_NAMES = frozenset("""
    ACTIVATION_REVIEW APPROVAL_AUTO APPROVAL_INTERACTIVE AcpError AcpProcessDied
    AcpPromptBusy AcpTimeoutError Any ConfigReadError ContextBuilder ConversationLog
    CronService DEFAULT_PROVIDER DELIVERY_DEBT_NOTICE DENY_CAUSE_APPROVAL_TIMEOUT
    DENY_CAUSE_POLICY EVENT_COMPLETE EVENT_PERMISSION_REQUEST EVENT_TEXT_CHUNK
    EVENT_THINKING_CHUNK EVENT_TOOL_CALL HOOK_REPLY HUMAN_TURN_META_KEY HistoryConsolidator
    InboundRoute Iterator KiroCrewConfig LLMEvent LLMProvider MessageContext
    OUTCOME_REJECTED_TRANSPORT_FLOOR PROVIDER_PIPER PROVIDER_SYSTEM Path PostedOptions
    SESSIONS_INCLUDE_ENDED_ARGS SLACK_MSG_LIMIT STEER_NOTICE_BOUND_SECS
    STOP_DECLINED_COMPACTING_TEXT STOP_REASON_CANCELLED STOP_REASON_COMPACTION_FAILED
    STOP_REASON_END_TURN SafetyOverride SensitiveAgentSpecPathError SessionClosingError
    SessionManager SlackClientOps StartPriority Stats StatusReactionController
    StreamRedactor SubagentManager TOOL_AUTO_APPROVE TOOL_DENY TRUNCATION_NOTICE
    TYPE_CHECKING Task TaskRunner ThreadReplies TurnCeilingExceeded UnknownMemoryStore
    _ACTION_APPROVE _ACTION_REJECT _ACTION_TRUST _APPROVAL_TIMEOUT _BANG_TO_SLASH
    _CC_AGENT_NAME_RE _CIRCUIT_BREAKER_THRESHOLD _CODING_KINDS _CODING_TOOLS
    _COMMENT_HOLD_MAX _COMPACTION_FAILED_RETRIES _COMPACTION_RETRY_NOTICE _CURSOR
    _CompactionReplay _DEFAULT_PHASE_EMOJIS _EDIT_INTERVAL _IMMEDIATE_PHASES
    _INCOGNITO_TOKEN_RE _LinkedApproval _LinkedApprovalEvent _NO_RESPONSE _OUTCOME_APPROVED
    _OUTCOME_REJECTED _PHASE_DEBOUNCE_SECS _PHASE_EMOJIS _PendingApproval
    _RESTRICTED_WRITE_MSG _REVIEW_DRAFT_MAX _REVIEW_DRAFT_TTL _REVIEW_PLACEHOLDER_TS
    _SLACK_OWNED_FIELDS _SLACK_SECTION_TEXT_LIMIT _STALL_EMOJI_HARD _STALL_EMOJI_SOFT
    _STALL_HARD_SECS _STALL_SOFT_SECS _STATUS_WORKING _STEER_NOTICE_BOUND_SECS
    _STREAM_CONTINUED _TEMPORARY_TOKEN_RE _TERMINAL_PHASES _THINKING _THINKING_PLACEHOLDER
    _THINKING_PREVIEW_LIMIT _TRUNCATION_MARKER _VoiceConfig _WEB_KINDS _WEB_TOOLS
    _YOLO_TTL_SECS _add_phase_reaction _add_trusted_session _allowed_users
    _append_footer_actions _apply_privacy_mode _at_tag_line_start _background_tasks
    _build_approval_blocks _build_phase_emojis _build_sessions_blocks _build_title_prompt
    _cached_default_agent _collect_recent_sessions_off_loop _comment_hold_is_protocol
    _condense_thinking _convert_tables _dashboard_state _discover_project_agents
    _display_redactor _filter_options_brackets _get_agent_for_session _get_auto_title_lock
    _get_default_agent _grant_linked_trust _handle_compact_command _handle_cron_command
    _handle_run_command _handle_sessions_command _handle_slash_command
    _handle_spawn_command _hydrate_conv_flags _hydrate_thread_overrides _hydrated_sessions
    _is_sessions_keyword _is_slack_restricted _iter_cc_agent_names _linked_approvals
    _linked_slots_for _linked_trust_grantable _list_all_agent_names _mark_incognito
    _mark_temporary _mark_titled _maybe_auto_title_slack _message_surface_limit
    _open_channels _orch_cfg _orphan_rejects _owner_id _pending_approvals
    _persist_channel_config _read_thread_overrides _reject_orphaned_tool _reload_orch_cfg
    _request_approval _resolve_agent_name _resolve_cc_agent_name _resolve_comment_hold
    _review_drafts _review_drafts_get _review_drafts_pop _review_drafts_set
    _safe_final_update _safe_update _safe_voice_reply _set_default_agent
    _shared_trusted_sessions _should_auto_approve_spawn _steer_host_deny _str_or_default
    _thread_agents _thread_incognito _thread_projects _thread_temporary _titled_threads
    _tool_to_phase _tracking_channels _trusted_sessions _tts_available
    _validate_length_scale _vc _voice_reply_fn add_trusted_session admit_inbound_callback
    adopt_slack_config agent_spec_stems annotations append_and_surface
    apply_config_duration auto_title await_replay_gap build_cancelled_turn_preamble
    build_session_replay build_timing_footer build_working_blocks cancel_background_tasks
    canonical_key cast channel_inbound_permitted clear_trusted_sessions
    compact_unsupported_backend compact_unsupported_reply compaction_in_flight config_path
    consume_reinjection consume_stop_declined copy_slack_fields count_redaction_tags
    cron_command_reply current_context dataclass decline_stop deprecation_warning_block
    describe_grant_lifetime describe_new_grant disable_yolo effective_session_key
    enable_yolo_with_ttl event_is_spawn_run expire_slack_options extract_options
    fetch_thread_parent get_dashboard_state get_orch_cfg grant_declared_yolo
    handle_interaction handle_message has_noted_turn has_prior_turns hook_gate_kwargs
    is_allowed_user is_control_tag_tail is_markdown_spec is_open_channel is_owner
    is_sensitive_path is_session_trusted is_slack_born is_slack_session_trusted
    is_thread_incognito is_thread_temporary is_tracked_channel is_wait_identity
    is_yolo_mode iter_agent_spec_files kiro_agents_dir load_voice_reply_config logger
    maybe_apply_privacy_modifiers maybe_handle_keyword_command maybe_route_linked_thread
    mint_options_token name_grant note_turn note_user_stop options_control_is_stale
    parent_prompt_text peek_data_home phase_emojis post_linked_approval privacy_mode
    project_agent_files project_agent_name publish_turn_identity read_agent_spec_strict
    read_config_text rearm_reinjection record_interaction_event record_thread_parent redact
    redact_credentials redact_exfiltration_urls redact_for_display redact_local_paths
    redaction_notice refresh_phase_emojis remember_slack_options render_one_for_slack
    replies_since_last_turn resolve_configured_provider resolve_linked_approval
    rollback_skill_bodies run_config_write run_in_embed_pool runtime_death
    safe_read_file_bytes safety_override save_conversation_turn_off_loop sel
    session_stop_generation session_store_for_turn sessions_include_ended set_allowed_users
    set_dashboard_state set_open_channels set_orch_cfg set_owner_id set_tracking_channels
    set_yolo_mode slack_cfg spawn_command_reply split_message steer_refusal_notice
    stop_reason_landed strip_control_comments strip_thinking_tags task_command_reply
    track_background_task turn_ceiling update_config_locked validated_config_string
    window_for_provider_client yolo_policy_permits
""".split())

#: Each definition moved out of ``handler.py`` and the owner its responsibility puts
#: it in.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "access": tuple("""
        _reload_orch_cfg add_trusted_session adopt_slack_config cancel_background_tasks
        copy_slack_fields disable_yolo enable_yolo_with_ttl get_dashboard_state
        get_orch_cfg is_allowed_user is_open_channel is_owner is_slack_session_trusted
        is_tracked_channel is_yolo_mode set_allowed_users set_dashboard_state
        set_open_channels set_orch_cfg set_owner_id set_tracking_channels set_yolo_mode
        slack_cfg track_background_task
        """.split()),
    "approvals": tuple("""
        _LinkedApproval _LinkedApprovalEvent _PendingApproval _build_approval_blocks
        _grant_linked_trust _linked_slots_for _linked_trust_grantable post_linked_approval
        resolve_linked_approval
        """.split()),
    "commands": tuple("""
        _handle_compact_command _handle_cron_command _handle_run_command
        _handle_slash_command _handle_spawn_command _is_sessions_keyword
        """.split()),
    "finalize": tuple("""
        _append_footer_actions _maybe_auto_title_slack _review_drafts_get
        _review_drafts_pop _review_drafts_set build_timing_footer
        """.split()),
    "inbound": tuple("""
        _apply_privacy_mode _get_agent_for_session _get_default_agent _hydrate_conv_flags
        _hydrate_thread_overrides _is_slack_restricted _persist_channel_config
        _read_thread_overrides _set_default_agent maybe_apply_privacy_modifiers
        maybe_route_linked_thread
        """.split()),
    "reactions": tuple("""
        StatusReactionController _add_phase_reaction _tool_to_phase phase_emojis
        refresh_phase_emojis
        """.split()),
    "stream": tuple("""
        _at_tag_line_start _comment_hold_is_protocol _filter_options_brackets
        _resolve_comment_hold _safe_final_update _safe_update
        """.split()),
    "turn_context": tuple("""

        """.split()),
    "voice": tuple("""
        _safe_voice_reply _str_or_default load_voice_reply_config
        """.split()),
}

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)

#: Functions and classes the split introduced: phases of ``handle_message``,
#: ``handle_interaction`` and ``_handle_slash_command`` that each moved out as one
#: piece, each with the owner it lives in.
_SPLIT_HELPERS: dict[str, str] = {
    "_AnswerStream": "stream",
    "_bang_agent": "commands",
    "_bang_allowlist": "commands",
    "_bang_channel": "commands",
    "_bang_dashboard": "commands",
    "_bang_link_to_dashboard": "commands",
    "_bang_project": "commands",
    "_bang_stop": "commands",
    "_bang_thread_agent": "commands",
    "_bang_title": "commands",
    "_bang_voice": "commands",
    "_bang_yolo": "commands",
    "_grant_late_trust": "approvals",
    "_mirror_to_dashboard": "finalize",
    "_post_review_draft": "finalize",
    "_reply_by_voice": "voice",
    "_resolve_linked_click": "approvals",
    "_route_bang_command": "commands",
    "_thread_context": "turn_context",
    "_thread_meta_fallback": "turn_context",
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every moved name,
#: captured from the one-module file before the split.
_BASE_SHAPE_DIGEST = "d0818fced3d594d181317b548004673b7eaf31a24ad071cd96433bcc3f43b666"

#: Definitions that stay in the facade file, each because a repository guard, a
#: contract or the import-time body reads it there. The reason per entry is in
#: ``docs/system-specs/modules/slack-gateway.md`` ("Native handler composition").
_FACADE_DEFS = (
    "_display_redactor",
    "_should_auto_approve_spawn",
    "_condense_thinking",
    "_build_phase_emojis",
    "_VoiceConfig",
    "_CompactionReplay",
    "_discover_project_agents",
    "_resolve_agent_name",
    "_iter_cc_agent_names",
    "_resolve_cc_agent_name",
    "_list_all_agent_names",
    "MessageContext",
    "maybe_handle_keyword_command",
    "handle_message",
    "_reject_orphaned_tool",
    "_steer_host_deny",
    "_request_approval",
    "handle_interaction",
    "_handle_sessions_command",
)


_FACADE = handler.__name__
_FACADE_PATH = Path(handler.__file__).resolve()
_OWNER_PACKAGE = handler_runtime.__name__
_OWNER_DIR = Path(handler_runtime.__file__).resolve().parent
_ROOT = repo_root()
_SRC = _ROOT / "src"


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines, methods included."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members += [(f"{name}.{m}", v) for m, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=180,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    assert len(_BASE_NAMES) == 323
    assert sorted(name for name in _BASE_NAMES if not hasattr(handler, name)) == []


def test_a_fresh_interpreter_sees_the_surface_and_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it, so none loads lazily on a
    later call and the import order stays the one the one-module file had; the
    public names resolve in a process that imports nothing else first."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 100
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.slack.handler as handler
        owners = sys.argv[1].split(",")
        missing = [n for n in sys.argv[2:] if not hasattr(handler, n)]
        unloaded = [o for o in owners if f"kiro_crew.slack.handler_runtime.{o}" not in sys.modules]
        assert missing == [] and unloaded == [], (missing, unloaded)
        print("ok")
        """,
        ",".join(_BASE_OWNERS),
        *public,
    )


def test_no_owner_function_keeps_its_own_globals_once_the_entry_points_load(
    tmp_path: Path,
) -> None:
    """The facade binds every owner's names (its re-export blocks) before ``compose`` runs
    at its foot, so an import cycle that reached an owner function in between would hold
    one still running on the owner's globals. Load the gateway and the dashboard server
    first, as a process does, then sweep every live function compiled from an owner."""
    _run_child(
        tmp_path,
        """
        import gc, os, types
        import kiro_crew.slack.gateway
        import kiro_crew.dashboard.server
        import kiro_crew.slack.handler as handler

        def norm(path):
            return os.path.normcase(os.path.realpath(path))

        # Only a REACHABLE function can be called. compose replaces each owner function,
        # and an import can leave the replaced one in a reference cycle (a caught
        # exception's frames, say) that only the collector frees; whether it has run
        # yet depends on allocation, so collect before looking.
        gc.collect()
        root = norm(os.path.join(os.path.dirname(handler.__file__), "handler_runtime"))
        package = norm(os.path.join(root, "__init__.py"))
        owned = [
            fn
            for fn in gc.get_objects()
            if isinstance(fn, types.FunctionType)
            and norm(fn.__code__.co_filename).startswith(root + os.sep)
            and norm(fn.__code__.co_filename) != package
        ]
        stray = sorted(fn.__qualname__ for fn in owned if fn.__globals__ is not vars(handler))
        assert len(owned) > 100 and stray == [], (len(owned), stray)
        print("ok")
        """,
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__`` (nor did the one-module file), so every
    public binding goes out. Loaded through ``exec_module`` from a probe file."""
    assert not hasattr(handler, "__all__")
    probe = tmp_path / "slack_handler_star_probe.py"
    probe.write_text("from kiro_crew.slack.handler import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("slack_handler_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("is_tracked_channel", "post_linked_approval", "build_timing_footer", "slack_cfg"):
        assert getattr(module, name) is getattr(handler, name)


def test_the_shared_trackers_are_still_the_shared_objects() -> None:
    """The aliases conftest clears and the channel-neutral owners read are the very
    objects ``messaging`` keeps, not copies an owner could hold."""
    from kiro_crew.config import paths
    from kiro_crew.messaging import auto_title, privacy_mode, session_trust

    assert handler._trusted_sessions is session_trust._trusted_sessions
    assert handler._thread_temporary is privacy_mode._temporary
    assert handler._thread_incognito is privacy_mode._incognito
    assert handler._titled_threads is auto_title._titled
    assert handler.is_thread_temporary is privacy_mode.is_temporary
    assert handler.is_thread_incognito is privacy_mode.is_incognito
    assert handler._get_auto_title_lock is auto_title.get_lock
    assert handler.kiro_agents_dir is paths.kiro_agents_dir


def _facade_importers() -> dict[str, set[str]]:
    """``{module path: names}`` every source file imports from the facade by name."""
    found: dict[str, set[str]] = {}
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        if path.resolve() == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _FACADE not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == _FACADE:
                found.setdefault(path.relative_to(_SRC).as_posix(), set()).update(
                    alias.name for alias in node.names
                )
    return found


def test_every_name_a_production_module_imports_from_the_facade_resolves() -> None:
    """Many of these imports are function-local, so a name that stopped resolving
    would first fail at request time rather than at import."""
    importers = {
        path: names
        for path, names in _facade_importers().items()
        if not path.startswith("kiro_crew/slack/handler_runtime/")
    }
    assert len(importers) >= 12
    imported = {name for names in importers.values() for name in names}
    assert sorted(name for name in imported if not hasattr(handler, name)) == []
    assert {"handle_message", "is_tracked_channel", "_vc", "load_voice_reply_config"} <= imported


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(_BASE_OWNERS)


def _shape(obj: object) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The facade's binding of a moved name is the owner's object, in the owner its
    responsibility names, and no name is defined by two owners."""
    placed = {**{n: o for o, names in _BASE_OWNERS.items() for n in names}, **_SPLIT_HELPERS}
    strays = [
        f"{owner}:{name}"
        for name, owner in placed.items()
        if getattr(handler, name) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _BASE_OWNERS.values() for name in group]
    assert len(names) == len(set(names)) == 70
    assert not set(_SPLIT_HELPERS) & set(names)


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(f"{name} {_shape(getattr(handler, name))}" for name in _MOVED)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _module_assignments(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the facade's namespace, so
    a test that rebinds one there is the binding every function sees -- which holds
    only while no owner keeps a copy."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"_pending_approvals", "_linked_approvals", "_vc", "_owner_id", "logger"} <= state
    assert {"_thread_agents", "_PHASE_EMOJIS", "_trusted_sessions", "_orphan_rejects"} <= state
    owned = {stem: sorted(_module_assignments(src)) for stem, src in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(handler, name)
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_every_definition_is_in_exactly_one_place() -> None:
    """The facade keeps what ``_FACADE_DEFS`` names and the owners hold the rest:
    together they are the one-module file's definitions, each once, plus the phases
    the split lifted out of three long functions."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEFS)
    assert len(defined | _MOVED) == len(defined) + len(_MOVED) == 89


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.slack.handler`` keeps seeing the moved sites."""
    assert handler.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 30


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.slack.handler.<name>`` reaches an owner function only
    because the function reads the facade's globals, not its own."""
    functions = _owner_functions()
    assert len(functions) >= 110
    strays = [
        label
        for label, fn in functions
        if fn.__globals__ is not vars(handler) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_handler_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_handler_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    facade surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a logged failure. The sweep makes it a test failure instead."""
    namespace = vars(handler)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function() -> None:
    """The contract the rebinding exists for, on seams the suite actually patches:
    the owner id the access predicates read, the redactor the approval prompt runs,
    and the default-agent writer the ``!agent`` command calls."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "_owner_id", "UPATCHED")
        assert handler.is_allowed_user("UPATCHED") is True
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "redact_credentials", lambda text: ("<scrubbed>", []))
        blocks = handler._build_approval_blocks(
            SimpleNamespace(request_id=1, title="t", tool_input="", tool_purpose="")
        )
        assert "<scrubbed>" in json.dumps(blocks)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "is_yolo_mode", lambda: True)
        assert handler.is_yolo_mode() is True


def test_a_global_write_from_an_owner_lands_on_the_facade() -> None:
    """``set_owner_id``, ``set_allowed_users`` and ``set_dashboard_state`` declare
    ``global`` and live in an owner. ``compose`` replaces a function's
    ``__globals__``, so the ``STORE_GLOBAL`` writes the facade's namespace."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "_owner_id", "")
        mp.setattr(handler, "_allowed_users", set())
        mp.setattr(handler, "_dashboard_state", None)
        handler.set_owner_id("UWRITE")
        handler.set_allowed_users({"UWRITE"})
        sentinel = object()
        handler.set_dashboard_state(sentinel)
        assert handler._owner_id == "UWRITE" and handler.is_owner("UWRITE")
        assert handler._allowed_users == {"UWRITE"}
        assert handler.get_dashboard_state() is sentinel
        assert "_owner_id" not in vars(_owner("access"))


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner module
    function resolves back through its own ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        if "." in fn.__qualname__:
            continue  # a method: resolved through its class below
        if getattr(sys.modules[fn.__module__], fn.__qualname__, None) is not fn:
            wrong.append(label)
    assert wrong == []


def test_a_moved_class_keeps_its_owner_module() -> None:
    """SD35(5): a moved class names the module that defines it; nothing renders the
    class text to a user, so it is not relabelled."""
    assert handler.StatusReactionController.__module__ == f"{_OWNER_PACKAGE}.reactions"
    assert handler._PendingApproval.__module__ == f"{_OWNER_PACKAGE}.approvals"
    assert handler._AnswerStream.__module__ == f"{_OWNER_PACKAGE}.stream"
    method = handler.StatusReactionController.set_phase
    assert method.__globals__ is vars(handler)


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(handler.post_linked_approval)
    assert source.startswith("async def post_linked_approval(")
    assert inspect.getsourcefile(handler.post_linked_approval) == _owner("approvals").__file__


def test_the_compose_copy_matches_its_siblings() -> None:
    """Each composition keeps its own copy of the technique so no package imports
    another's, and the copies stay identical."""

    def body(rel: str) -> str:
        text = (_SRC / "kiro_crew" / rel / "__init__.py").read_text(encoding="utf-8")
        return text[text.index("def compose(") :]

    assert body("slack/handler_runtime") == body("hook_runtime")


# ── one edge ──────────────────────────────────────────────────────────────────


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _runtime_project_imports(source: str) -> list[int]:
    """Lines where a module imports a project module at runtime (outside
    ``TYPE_CHECKING`` and outside function bodies), spelled any way."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    in_functions = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    lines = []
    for node in ast.walk(tree):
        if id(node) in guarded or id(node) in in_functions:
            continue
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = [("." * node.level) + (node.module or "")]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            targets = [str(node.args[0].value)]
        if any(t.startswith(("kiro_crew", ".")) for t in targets):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import stream\n", True),
        ("from .stream import _AnswerStream\n", True),
        ("from kiro_crew.slack import handler\n", True),
        ("import kiro_crew.slack.handler as handler\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.slack.handler')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.slack.handler import sel\n", False),
        ("def f():\n    from kiro_crew.slack.blocks import review_draft_blocks\n", False),
        ("import asyncio\nfrom typing import Any\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_runtime_project_imports(source)) is flagged


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib only, plus its ``TYPE_CHECKING``
    names from the facade: the gateway imports the handler at boot, so an owner adds
    no import edge to that path, and the agent-SDK boundary sees no new edge."""
    offenders = {
        stem: lines
        for stem, source in _owner_sources().items()
        if (lines := _runtime_project_imports(source))
    }
    assert offenders == {}
    for source in _owner_sources().values():
        tree = ast.parse(source)
        guarded = [
            node
            for node in ast.walk(tree)
            if id(node) in _type_checking_nodes(tree) and isinstance(node, ast.ImportFrom)
        ]
        assert {node.module for node in guarded} <= {_FACADE}


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "handler_runtime" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = " ".join(a.name for a in node.names)
            if _OWNER_PACKAGE in module:
                importers.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert importers == []


def _non_literal_defaults(source: str) -> list[str]:
    """Owner defaults evaluated from a name: an owner's own namespace binds no state,
    so such a default would be read at owner import from the wrong module, or fail."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None and not isinstance(default, ast.Constant):
                    found.append(f"{node.name}:{node.lineno}")
    return found


def test_an_owner_default_is_a_literal() -> None:
    assert _non_literal_defaults("def f(a=LIMIT):\n    return a\n") == ["f:1"]
    assert _non_literal_defaults("def f(a=3, *, b=None):\n    return a\n") == []
    assert {
        stem: d for stem, src in _owner_sources().items() if (d := _non_literal_defaults(src))
    } == {}


# ── the patch reach ───────────────────────────────────────────────────────────


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.slack":
            aliases |= {a.asname or a.name for a in node.names if a.name == "handler"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target, value = node.targets[0], node.value
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or (
                isinstance(value, ast.Call)
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == _FACADE
                and ast.unparse(value.func).endswith(("import_module", "__import__"))
            ):
                aliases.add(target.id)
                changed = True
    return aliases


def _dotted_target(expr: ast.AST) -> str | None:
    """The facade attribute a dotted-string patch target names, if it is the facade's."""
    if not (isinstance(expr, ast.Constant) and isinstance(expr.value, str)):
        return None
    prefix = f"{_FACADE}."
    rest = expr.value[len(prefix) :] if expr.value.startswith(prefix) else ""
    return rest.split(".", 1)[0] or None


def _facade_writes(source: str) -> set[str]:
    """Every facade attribute *source* patches, rebinds, deletes or asserts identity on:
    ``monkeypatch.setattr``/``delattr``/``setitem`` in the object and dotted-string
    forms, ``mock.patch`` / ``patch.object`` through any alias, plain attribute
    assignment, and ``is`` comparisons."""
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    found: set[str] = set()

    def _attr_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases:
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found |= {a for a in (_attr_of(t) for t in targets) if a}
        elif isinstance(node, ast.Delete):
            found |= {a for a in (_attr_of(t) for t in node.targets) if a}
        elif isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            kwargs = {k.arg: k.value for k in node.keywords}
            positional = list(node.args)
            if call.endswith(
                ("setattr", "delattr", "setitem", "object", "multiple")
            ) or re.fullmatch(r"(\w+\.)*(patch|mock)", call):
                target = kwargs.get("target") or (positional[0] if positional else None)
                if target is None:
                    continue
                name = _dotted_target(target)
                if name:
                    found.add(name)
                elif ast.unparse(target) in aliases:
                    attr = kwargs.get("attribute") or (
                        positional[1] if len(positional) > 1 else None
                    )
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        found.add(attr.value)
                    else:
                        found |= {k for k in kwargs if k not in ("target", "attribute")}
        elif isinstance(node, ast.Compare) and any(isinstance(op, ast.Is) for op in node.ops):
            for side in (node.left, *node.comparators):
                attr = _attr_of(side)
                if attr:
                    found.add(attr)
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import kiro_crew.slack.handler as h\nmonkeypatch.setattr(h, '_EDIT_INTERVAL', 0)\n",
            {"_EDIT_INTERVAL"},
        ),
        ("from kiro_crew.slack import handler\nmonkeypatch.setattr(handler, 'os', 1)\n", {"os"}),
        ("monkeypatch.setattr('kiro_crew.slack.handler.is_owner', None)\n", {"is_owner"}),
        ("from unittest import mock\nmock.patch('kiro_crew.slack.handler.sel')\n", {"sel"}),
        ("patch('kiro_crew.slack.handler.Path.home')\n", {"Path"}),
        (
            "from kiro_crew.slack import handler as h\npatch.object(h, '_PendingApproval')\n",
            {"_PendingApproval"},
        ),
        ("import kiro_crew.slack.handler as h\nh.sel = lambda: None\n", {"sel"}),
        (
            "import kiro_crew.slack.handler as h\nassert h._trusted_sessions is t\n",
            {"_trusted_sessions"},
        ),
        ("import kiro_crew.slack.handler as h\nx = h._EDIT_INTERVAL\n", set()),
        ("monkeypatch.setattr('kiro_crew.slack.events.is_owner', None)\n", set()),
        ("monkeypatch.setattr(other, '_owner_id', 1)\n", set()),
    ],
)
def test_the_patch_target_reader_sees_every_spelling(source: str, expected: set[str]) -> None:
    """The detector finds each form and ignores a read, a sibling module's target and
    an unrelated object, so the sweep below is neither vacuous nor over-broad."""
    assert _facade_writes(source) == expected


def _test_sources() -> list[tuple[str, str]]:
    paths = sorted((_ROOT / "test").rglob("*.py"))
    out = []
    for path in paths:
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _FACADE in text or "kiro_crew.slack import handler" in text:
            out.append((path.relative_to(_ROOT).as_posix(), text))
    return out


def _patched_facade_names() -> set[str]:
    return {name for _, text in _test_sources() for name in _facade_writes(text)}


def test_the_corpus_sweep_finds_the_seams_the_suite_is_known_to_patch() -> None:
    patched = _patched_facade_names()
    assert len(patched) >= 60, sorted(patched)
    assert {
        "is_yolo_mode",
        "is_tracked_channel",
        "sel",
        "_build_approval_blocks",
        "Path",
        "is_owner",
        "_set_default_agent",
        "_PendingApproval",
        "is_allowed_user",
        "publish_turn_identity",
        "_get_default_agent",
        "_resolve_agent_name",
        "disable_yolo",
        "_pending_approvals",
        "KiroCrewConfig",
        "_collect_recent_sessions_off_loop",
        "is_sensitive_path",
        "os",
        "_owner_id",
        "kiro_agents_dir",
        "_EDIT_INTERVAL",
        "_STALL_SOFT_SECS",
        "_trusted_sessions",
    } <= patched, sorted(patched)
    assert sorted(name for name in patched if not hasattr(handler, name)) == []


def _runtime_shadows(source: str) -> dict[str, list[int]]:
    """Names an owner binds in a way that would shadow the facade's at call time: a
    FUNCTION-LOCAL import (it wins over the facade global for that call) or a
    MODULE-LEVEL assignment (owner state ``compose`` never moves). A module-level
    import is inert and is not reported; nor is a ``global``-declared write."""
    tree = ast.parse(source)
    inside_a_function = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) in inside_a_function:
            for alias in node.names:
                found.setdefault((alias.asname or alias.name).split(".")[0], []).append(node.lineno)
    for name in _module_assignments(source):
        found.setdefault(name, []).append(0)
    return found


def test_the_binding_sweep_reports_a_shadow_and_ignores_an_inert_import() -> None:
    assert "sel" in _runtime_shadows(
        "def f():\n    from kiro_crew.sel import sel\n\n    return sel\n"
    )
    assert "CAP" in _runtime_shadows("CAP = 7\n")
    assert "os" not in _runtime_shadows("import os\n\n\ndef f():\n    return os.name\n")
    assert "X" not in _runtime_shadows("def f(v):\n    global X\n\n    X = v\n")


def test_no_owner_binds_a_name_the_tests_patch_on_the_facade() -> None:
    """The seam the technique rests on: a patch of ``handler.<name>`` reaches a moved
    call site only while that site reads the name out of the facade's globals."""
    patched = _patched_facade_names()
    found = {
        stem: sorted(name for name in _runtime_shadows(source) if name in patched)
        for stem, source in _owner_sources().items()
    }
    assert {stem: names for stem, names in found.items() if names} == {}


# ── the placement guards ──────────────────────────────────────────────────────


def _calls_in(function: str, attr: str) -> int:
    """``.attr(...)`` calls directly in *function*'s own body, nested defs excluded --
    the shape ``test_transport_permission_floor`` counts per enclosing function."""
    tree = ast.parse(_FACADE_PATH.read_text(encoding="utf-8"))
    node = next(
        n
        for n in tree.body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == function
    )
    count = 0
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call) and getattr(child.func, "attr", None) == attr:
            count += 1
        stack.extend(ast.iter_child_nodes(child))
    return count


def test_the_facade_still_carries_what_the_source_guards_read() -> None:
    """Repository guards read these constructs in ``slack/handler.py`` by path, AST,
    text or ``inspect.getsource``, so they constrain WHERE the turn's decisions live.
    Each needle names the guard that reads it; moving one fails here first, with the
    reason, rather than as an unexplained red in that guard."""
    source = _FACADE_PATH.read_text(encoding="utf-8")
    handle = inspect.getsource(handler.handle_message)
    assert _calls_in("handle_message", "approve_tool") == 4  # test_transport_permission_floor
    assert _calls_in("handle_interaction", "approve_tool") == 1  # test_transport_permission_floor
    assert source.count(".reject_tool(") == 4  # test_messaging_deny_notice
    assert source.count("await _steer_host_deny(") == 2  # test_messaging_deny_notice
    gate_calls = re.findall(
        r"await run_in_tool_gate_pool\(\s*context_builder\.hooks\.on_tool_call,", source
    )
    assert len(gate_calls) == 2  # test_hooks
    assert "**hook_gate_kwargs(event)" in source  # test_hooks
    for needle in (
        "turn_ceiling.gate(",  # test_turn_ceiling
        "except TurnCeilingExceeded",  # test_turn_ceiling
        "_working_ts = await",  # test_turn_ceiling
        "sessions.begin_turn(session_key)",  # test_turn_ceiling
        "sessions.check_context_usage(session_key, client)",  # test_reinjection_gate
        "consume_reinjection(",  # test_reinjection_gate
        "rearm_reinjection(",  # test_reinjection_gate
        "runtime_death.caused_by_this_session(client)",  # test_runtime_death_is_a_process_event
        "runtime_death.note_shared_death(",  # test_runtime_death_is_a_process_event
        "_memory_store = await session_store_for_turn(context_builder, candidate_key)",
        "memory_store=_memory_store",  # test_memory_v2_isolation
        "StreamRedactor",  # test_security_posture (the "Slack messages" sink row)
        "introduced by reply decorator",  # test_security_posture (baseline log census)
        'logger.warning("Credential redacted in response: %s", w)',  # SAST baseline
        'logger.warning("Credential redacted in thinking: %s", w)',  # SAST baseline
        "auto_title.pin_record(conversation_log, session_key)",  # test_messaging_auto_title
        "auto_title.try_claim(session_key)",  # test_messaging_auto_title
        "_options_verdict_deferred = bool(",  # test_messaging_auto_title
        "_turn_row_ts = await save_conversation_turn_off_loop(",  # test_messaging_auto_title
        "if thread_owner_key is None and not route_pinned:",  # test_options_click_validation
        "project_agent_files(",  # test_agent_spec_hardened_reads
        "read_agent_spec_strict(",  # test_agent_spec_hardened_reads
    ):
        assert needle in source, needle
    assert source.count("save_conversation_turn_off_loop(") >= 7  # test_persist_off_loop
    for needle in (
        "admit_inbound_callback(",  # test_update_check_install_aware
        "mint_options_token(",  # test_options_click_validation
        "_options_owner =",  # test_slack_options_lifecycle
        "maybe_handle_keyword_command",  # test_slack_options_lifecycle
        "await expire_slack_options(",  # test_slack_options_lifecycle
    ):
        assert needle in handle, needle
    tree = ast.parse(source)
    assert "_build_phase_emojis" in {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }  # import-time table: the facade body runs before compose


def test_no_owner_holds_a_construct_a_handler_guard_counts() -> None:
    """The other half: a guard that counts a construct in ``handler.py`` would lose
    it silently if it moved into an owner, so no owner may hold one."""
    needles = (
        ".approve_tool(",
        ".reject_tool(",
        ".on_tool_call(",
        "begin_turn(",
        "check_context_usage(",
        "compact_if_needed(",
        "consume_reinjection(",
        "rearm_reinjection(",
        "caused_by_this_session",
        "note_shared_death",
        "session_store_for_turn(",
        "save_conversation_turn_off_loop(",
        "mint_options_token(",
        "try_claim(",
        "resolve_pre_turn(",
        "mark_temporary(",
        "mark_incognito(",
        "remaining // 60",
        "asyncio.to_thread(name_grant",
        'title == "spawn_run"',
        "project_agent_files(",
        "read_agent_spec_strict(",
        "_CC_AGENT_NAME_RE",
    )
    held = {
        stem: [needle for needle in needles if needle in source]
        for stem, source in _owner_sources().items()
    }
    assert {stem: hits for stem, hits in held.items() if hits} == {}


def test_a_frame_in_an_owner_is_attributed_to_slack() -> None:
    """A loop stall whose outermost Kiro Crew frame is an owner function still reads
    as the Slack surface (``stall_attribution``'s slack rule names the package)."""
    from kiro_crew import stall_attribution

    frames = stall_attribution.parse_frames(
        [
            '  File "/opt/venv/lib/python3.12/site-packages/kiro_crew/slack/handler_runtime/'
            'stream.py", line 1 in on_text'
        ]
    )
    assert stall_attribution.classify_surface(frames) == "slack"
