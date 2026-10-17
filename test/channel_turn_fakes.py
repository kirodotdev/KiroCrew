"""Local stand-ins for the channel turn pipeline's collaborators.

The pipeline (:class:`kiro_crew.messaging.dispatch.ChannelTurns`) is tested
through its own interface with the real ``TurnDriver`` behind it. Only the
collaborators it is handed are substituted, each by one stand-in:

* :class:`FakeSessions` -- the session manager. Every call it receives is
  appended to ONE ordered :attr:`FakeSessions.ledger`, so ordering is
  observable as a plain list rather than through a vocabulary of effects.
* :class:`ScriptedProvider` -- the ACP provider: each prompt plays the next
  scripted turn, a list of events in which an exception instance is raised at
  that point of the stream.
* :class:`RecordingCtxBuilder` -- the context builder, with scriptable hook
  verdicts; it records every ``build_message`` call and skill-body settle.
* :class:`RecordingRenderer` -- a :class:`Renderer` that records every event
  and can fail its close or report an undelivered turn.

Nothing here spawns a process, binds a port, sleeps, or reads a clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.discord.client import EDIT_FAILED, EDIT_OK
from kiro_crew.hooks import HOOK_REPLY, TOOL_DENY
from kiro_crew.messaging.renderer import DONE, OutputEvent, Renderer
from kiro_crew.session_allocation import SessionBusyError, SessionClosingError

#: A capability set no renderer reads in these tests.
CAPABILITIES = SimpleNamespace(max_buttons=0, supports_edit=False)


def text(chunk: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk)


def thinking(chunk: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_THINKING_CHUNK, text=chunk)


def compaction_status(status: str = "failed", usage_pct: float = 95.0) -> AcpEvent:
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status, context_usage_pct=usage_pct)


def complete(stop_reason: str = STOP_REASON_END_TURN) -> AcpEvent:
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)


def tool_call(tool_call_id: str = "tc1", title: str = "Run a tool") -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=tool_call_id, title=title)


def directive(tool: str = "autonudge_stop", tool_call_id: str = "d1") -> list[AcpEvent]:
    """A genuine session directive: the core MCP server's own tool, then its
    encoded result -- what the driver hands a directive consumer."""
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=tool_call_id,
            title=tool,
            tool_name=tool,
            mcp_server_name=session_directive.CORE_MCP_SERVER,
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_output=session_directive.encode(tool, {"reason": "done"}, "Requested."),
            tool_final=True,
        ),
    ]


def permission(request_id: str = "r1", title: str = "Run a tool") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title=title,
        options=[{"optionId": "allow", "name": "Allow"}],
    )


def steer_consumed() -> AcpEvent:
    return AcpEvent(kind=EVENT_STEER_CONSUMED)


def answer(reply: str = "the reply") -> list[Any]:
    """A turn that streams *reply* and ends normally."""
    return [text(reply), complete()]


class ScriptedProvider:
    """An ACP provider whose every prompt plays the next scripted turn.

    A turn is a list of :class:`AcpEvent` and exception instances; an exception
    is raised at its position, so a script can fail mid-stream after text has
    landed. When the scripts run out every further prompt answers ``"ok"``.
    ``transient`` is what ``last_compaction_transient`` reports; ``session_id``
    is what the crew-log emitter reads as the ACP session id.
    """

    def __init__(
        self,
        *turns: list[Any],
        backend: str | None = ACP_BACKEND_KIRO,
        transient: Any = None,
        session_id: str = "",
        cwd: str = "",
        name: str = "provider",
    ) -> None:
        self.turns = [list(t) for t in turns]
        self.prompts: list[str] = []
        self.approved: list[Any] = []
        self.rejected: list[Any] = []
        self.steered: list[str] = []
        self.client = SimpleNamespace(backend=backend)
        self.last_compaction_transient = transient
        self.session_id = session_id
        self.served_model = ""
        self.cwd = cwd
        self.name = name
        #: The host-deny steer is sent only to a provider that says it can carry one.
        self.supports_refusal_steer = True

    def __repr__(self) -> str:
        return f"ScriptedProvider({self.name})"

    async def stream(self, message: str) -> Any:
        self.prompts.append(message)
        script = self.turns.pop(0) if self.turns else answer("ok")
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def approve_tool(self, request_id: Any) -> bool:
        self.approved.append(request_id)
        return True

    async def reject_tool(self, request_id: Any) -> None:
        self.rejected.append(request_id)

    async def steer(self, message: str) -> bool:
        self.steered.append(message)
        return True

    def has_active_turn(self) -> bool:
        return False


class FakeSessions:
    """The session manager, as one ordered ledger of the calls it receives.

    ``providers`` are handed out one per ``get_or_create``; the last one is reused
    once they run out. ``is_new`` / ``resumed`` are what ``get_or_create`` answers;
    ``is_new`` may be a list, one answer per acquire (the last one repeats), the way
    a reacquire after a reset reports a fresh runtime.
    ``busy`` refuses a non-waiting claim, ``closing`` makes ``begin_turn`` refuse
    the dispatch the way the real gate does once shutdown began, and
    ``acquire_raises`` is raised from ``get_or_create``. ``reinjection`` arms the
    one-shot post-compaction flag. ``muted`` keys answer ``is_mirror_paused``.
    """

    def __init__(
        self,
        *providers: ScriptedProvider,
        is_new: bool | list[bool] = False,
        resumed: bool = False,
        busy: bool = False,
        closing: bool = False,
        acquire_raises: BaseException | None = None,
        reinjection: bool = False,
        bound_agent: str = "",
        reset_raises: bool = False,
        on_reset: Callable[["FakeSessions"], None] | None = None,
    ) -> None:
        self.providers = list(providers) or [ScriptedProvider()]
        self.on_reset = on_reset
        #: Whether the manager's replay gap is open for the key right now.
        self.gap_open = False
        self.is_new = is_new
        self.resumed = resumed
        self.busy = busy
        self.closing = closing
        self.acquire_raises = acquire_raises
        self.reinjection_armed = reinjection
        self.bound_agent = bound_agent
        self.reset_raises = reset_raises
        self.ledger: list[tuple[Any, ...]] = []
        self.muted: set[tuple[str, bool]] = set()
        self.mirror_links: dict[str, Any] = {}
        self.origin_links: dict[str, Any] = {}
        self.opted_out: set[str] = set()
        self.stop_gen = 0
        self.max_gen = 0
        self.held = 0

    # ── acquisition ──
    async def get_or_create(
        self,
        key: str,
        *,
        agent: Any = None,
        channel_id: Any = None,
        start_priority: Any = None,
        wait_if_busy: bool = True,
        **extra: Any,
    ) -> Any:
        self.ledger.append(
            (
                "get_or_create",
                key,
                {
                    "agent": agent,
                    "channel_id": channel_id,
                    "start_priority": start_priority,
                    "wait_if_busy": wait_if_busy,
                    **extra,
                },
            )
        )
        if self.busy and not wait_if_busy:
            raise SessionBusyError(key)
        if self.acquire_raises is not None:
            raise self.acquire_raises
        provider = self.providers.pop(0) if len(self.providers) > 1 else self.providers[0]
        self.held += 1
        if not self.bound_agent:
            self.bound_agent = agent or ""
        if isinstance(self.is_new, list):
            is_new = self.is_new.pop(0) if len(self.is_new) > 1 else self.is_new[0]
        else:
            is_new = self.is_new
        return provider, is_new, self.resumed

    def release(self, key: str) -> None:
        self.held -= 1
        self.ledger.append(("release", key))

    def begin_turn(self, key: str) -> None:
        self.ledger.append(("begin_turn", key))
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    def get_agent(self, key: str) -> str:
        return self.bound_agent

    def get_pid(self, key: str) -> Any:
        return None

    # ── session bookkeeping ──
    async def set_channel(self, key: str, conversation_id: str) -> None:
        self.ledger.append(("set_channel", key, conversation_id))

    def set_origin_link(self, key: str, link: Any) -> None:
        self.origin_links[key] = link
        self.ledger.append(("set_origin_link", key))

    def mirror_opt_out(self, key: str) -> bool:
        return key in self.opted_out

    def get_mirror_link(self, key: str) -> Any:
        return self.mirror_links.get(key)

    def set_mirror_link(self, key: str, link: Any) -> None:
        self.mirror_links[key] = link
        self.ledger.append(("set_mirror_link", key))

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return (key, origin) in self.muted

    # ── accounting ──
    def record_success(self, key: str) -> None:
        self.ledger.append(("record_success", key))

    async def record_failure(self, key: str) -> None:
        self.ledger.append(("record_failure", key))

    async def reset(self, key: str) -> None:
        self.ledger.append(("reset", key))
        if self.reset_raises:
            raise RuntimeError("reset failed")
        if self.on_reset is not None:
            self.on_reset(self)

    # ── one-shot re-injection flag ──
    def consume_needs_reinjection(self, key: str) -> bool:
        self.ledger.append(("consume_reinjection", key))
        armed, self.reinjection_armed = self.reinjection_armed, False
        return armed

    def mark_needs_reinjection(self, key: str) -> None:
        self.ledger.append(("mark_reinjection", key))
        self.reinjection_armed = True

    # ── generations and the replay gap ──
    def stop_generation(self, key: str) -> int:
        return self.stop_gen

    def max_generation(self, bucket: str) -> int:
        return self.max_gen

    def open_replay_gap(self, key: str) -> None:
        self.gap_open = True
        self.ledger.append(("open_replay_gap", key))

    def close_replay_gap(self, key: str) -> None:
        self.gap_open = False
        self.ledger.append(("close_replay_gap", key))

    async def await_replay_gap(self, key: str) -> None:
        self.ledger.append(("await_replay_gap", key))

    # ── reading the ledger ──
    def calls(self, name: str) -> list[tuple[Any, ...]]:
        return [entry for entry in self.ledger if entry[0] == name]

    def names(self) -> list[str]:
        return [entry[0] for entry in self.ledger]

    def count(self, name: str) -> int:
        return len(self.calls(name))


class RecordingCtxBuilder:
    """The context builder: records builds and skill-body settles, scripts hooks.

    ``hook_reply`` (a string) makes ``on_message`` answer every message with it;
    ``tool_verdict`` is what ``on_tool_call`` answers (``""`` passes through).
    """

    def __init__(
        self,
        *,
        hook_reply: str | None = None,
        tool_verdict: str = "",
        deny_reason: str = "",
        build_raises: BaseException | None = None,
        ledger: list[tuple[Any, ...]] | None = None,
    ) -> None:
        #: Shared with a :class:`FakeSessions` ledger so a build's place among the
        #: session calls is observable; a private list otherwise.
        self.ledger = ledger if ledger is not None else []
        self.builds: list[dict[str, Any]] = []
        self.settles: list[tuple[str, str]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.build_raises = build_raises
        self._hook_reply = hook_reply
        self._tool_verdict = tool_verdict
        self._deny_reason = deny_reason
        self.hooks = SimpleNamespace(
            on_message=self._on_message,
            on_tool_call=self._on_tool_call,
            auto_approve_subagent_spawn=False,
        )

    def _on_message(self, message: str) -> Any:
        if self._hook_reply is None:
            return SimpleNamespace(action="passthrough", text="")
        return SimpleNamespace(action=HOOK_REPLY, text=self._hook_reply)

    def _on_tool_call(self, title: str, **kw: Any) -> Any:
        self.tool_calls.append({"title": title, **kw})
        reason = self._deny_reason if self._tool_verdict == TOOL_DENY else ""
        return SimpleNamespace(action=self._tool_verdict, reason=reason)

    def build_message(self, message: str, is_new: bool, session_key: str, **kw: Any) -> Any:
        self.builds.append({"text": message, "is_new": is_new, "key": session_key, **kw})
        self.ledger.append(("build_message", session_key))
        if self.build_raises is not None:
            raise self.build_raises
        return message, None

    def commit_skill_bodies(self, session_key: str) -> None:
        self.settles.append(("commit", session_key))
        self.ledger.append(("commit_skill_bodies", session_key))

    def rollback_skill_bodies(self, session_key: str) -> None:
        self.settles.append(("rollback", session_key))
        self.ledger.append(("rollback_skill_bodies", session_key))


class RecordingRenderer(Renderer):
    """A renderer that records what reached it, in order.

    ``events`` holds every dispatched :class:`OutputEvent` kind (and the direct
    ``on_text_chunk`` / ``on_done`` calls a refusal makes); ``text`` the text
    that was shown. ``close_raises`` fails the close the way a real renderer can
    mid-flush; ``delivery_failed`` set to a bool makes it report that. Given a
    ``ledger`` (a :class:`FakeSessions` one), it writes its turn start there, so
    its order against the session calls is observable.
    """

    channel_type = "test"

    def __init__(
        self,
        *,
        close_raises: bool = False,
        delivery_failed: Any = None,
        ledger: list[tuple[Any, ...]] | None = None,
    ) -> None:
        super().__init__(CAPABILITIES)
        self.ledger = ledger
        self.events: list[str] = []
        self.shown: list[str] = []
        self.done: list[OutputEvent] = []
        self.closed = 0
        self.started = 0
        self.close_raises = close_raises
        if delivery_failed is not None:
            self.delivery_failed = delivery_failed

    async def dispatch(self, event: OutputEvent) -> None:
        self.events.append(event.kind)
        if event.kind == DONE:
            self.done.append(event)
        await super().dispatch(event)

    async def on_turn_start(self) -> None:
        self.started += 1
        if self.ledger is not None:
            self.ledger.append(("on_turn_start", ""))

    async def on_text_chunk(self, text: str) -> None:
        self.shown.append(text)

    async def on_thinking(self, text: str) -> None:
        pass

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        pass

    async def on_prompt_choice(self, options: Any, request_id: Any, *args: Any) -> None:
        pass

    async def on_compaction(self, context_usage_pct: float) -> None:
        pass

    async def on_done(self, stop_reason: str = "") -> None:
        self.events.append("on_done")

    async def on_steer_consumed(self, summary: str = "") -> None:
        pass

    async def close(self) -> None:
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("renderer finalization failed")


class RecordingDispatcher:
    """The dispatcher object the pipeline reads ``approval_mode`` and state off."""

    def __init__(self, approval_mode: str = "auto", dashboard_state: Any = None) -> None:
        self.approval_mode = approval_mode
        self.dashboard_state = dashboard_state


class Recorder:
    """Records every call of the adapters a test hands the pipeline."""

    def __init__(self) -> None:
        self.records: list[Any] = []
        self.notices: list[tuple[Any, ...]] = []
        self.surfaced = 0

    async def record(self, record: Any) -> None:
        self.records.append(record)

    async def notice(self, where: Any, key: str, provider: Any) -> None:
        self.notices.append((where, key, provider))

    async def surface(self) -> None:
        self.surfaced += 1


class FakeDiscordClient:
    """The Discord REST client the real ``DiscordRenderer`` posts through.

    ``fail_sends`` answers every send with ``None``, which is what the real client
    does for a revoked token or a dead network, so the renderer reports the turn
    undelivered.
    """

    def __init__(self, *, fail_sends: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str]] = []
        self.fail_sends = fail_sends
        self._mid = 100

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(self, channel_id: str, text: str, **_kw: Any) -> Any:
        self._mid += 1
        self.sent.append((channel_id, text))
        return None if self.fail_sends else str(self._mid)

    async def edit_message(self, channel_id: str, message_id: str, text: str, **_kw: Any) -> bool:
        self.edits.append((message_id, text))
        return not self.fail_sends

    async def edit_message_with_files_outcome(
        self, channel_id: str, message_id: str, text: str, files: Any, **_kw: Any
    ) -> str:
        self.edits.append((message_id, text))
        return EDIT_FAILED if self.fail_sends else EDIT_OK

    async def send_message_with_files(
        self, channel_id: str, text: str, files: Any, **_kw: Any
    ) -> Any:
        return await self.send_message(channel_id, text)

    async def api_json(self, *_a: Any, **_kw: Any) -> Any:
        return None

    def shown(self) -> str:
        """Everything the conversation can read: each send and edit, in order."""
        return "\n".join([t for _c, t in self.sent] + [t for _m, t in self.edits])
