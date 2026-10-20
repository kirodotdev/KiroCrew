"""Recording doubles for the native Slack handler goldens.

``_Transcript`` is the ordered record a scenario produces. ``_Slack``, ``_Provider``,
``_Sessions`` and ``_ContextBuilder`` write rows to it as the handler drives them, beside
the SEL, stats, config and clock stand-ins the ``world`` fixture patches onto
``kiro_crew.slack.handler``. ``_turn`` runs one ``handle_message`` call on a fresh
``_Slack``, ``_Provider`` and ``_Sessions``; the ``LLMEvent`` builders and ``_settle`` sit
beside it. Only ``test_slack_handler_composition_contract_golden.py`` imports this module.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler

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


@pytest.fixture(name="world")
def world_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
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
