"""Agent-role ACP method dispatch: Kiro Crew as an ACP agent for an editor.

An editor spawns Kiro Crew and drives it over stdio, exactly as Kiro Crew drives
kiro-cli today. This module answers the client->agent method set and streams a
turn's output back as ``session/update`` notifications.

Running a turn is delegated to an injected ``PromptHandler`` so this layer stays
free of gateway internals; wiring it to the dashboard chat runner is a separate
concern. The handler receives a ``SessionSink`` and drives the editor's UI
through it — most importantly ``request_permission``, which is what surfaces an
inline diff with accept/reject in the editor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from kiro_crew.acp.types import (
    ACP_VALID_STOP_REASONS,
    CAP_LOAD_SESSION,
    CAP_SESSION_CAPABILITIES,
    CAP_SESSION_LIST,
    CAP_SESSION_RESUME,
    CONFIG_OPTION_TYPE_BOOLEAN,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    METHOD_CANCEL,
    METHOD_ELICITATION_CREATE,
    METHOD_INITIALIZE,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LIST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    OPTION_ALLOW_ONCE,
    OPTION_REJECT_ONCE,
    OUTCOME_SELECTED,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_AVAILABLE_COMMANDS,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
    UPDATE_PLAN,
    UPDATE_SESSION_INFO,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    UPDATE_USER_MESSAGE_CHUNK,
)
from kiro_crew.acp_server.mcp_config import (
    McpConfigError,
    StdioMcpServer,
    parse_mcp_servers,
)
from kiro_crew.acp_server.transport import AgentTransport

logger = logging.getLogger(__name__)

# Strict ACP v1: the public protocol numbers versions with a single integer and
# the current baseline is 1. Kiro Crew supports exactly this version and always
# negotiates to it — it never echoes back an unrecognised value a peer offers,
# which would claim to speak a protocol variant it does not.
SUPPORTED_PROTOCOL_VERSION = 1

# Back-compat alias for importers. The server negotiates to
# SUPPORTED_PROTOCOL_VERSION regardless of what the peer offers.
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSION

AGENT_NAME = "kirocrew"


@dataclass
class PromptRequest:
    """One ``session/prompt`` turn."""

    session_id: str
    text: str
    # The original ACP content blocks, preserved alongside the flattened text so
    # a backend that can act on structured content (e.g. resource links) has it
    # at the boundary rather than only the lossy text projection.
    content_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SelectorState:
    """One session's advertised ACP selectors: reasoning-effort modes + config options.

    ``modes`` is a wire ``SessionModeState`` (``{currentModeId, availableModes}``)
    or None when the backend advertises no mode selector. ``config_options`` is a
    list of wire ``SessionConfigOption`` dicts (e.g. the model dropdown) or None.
    Both are the exact shapes the ACP v1 schema places on session/new|load|resume
    and on the set_* responses/notifications, so the server passes them through
    verbatim rather than re-deriving them.
    """

    modes: dict[str, Any] | None = None
    config_options: list[dict[str, Any]] | None = None

    def response_fields(self) -> dict[str, Any]:
        """The subset of {modes, configOptions} that is actually advertised."""
        out: dict[str, Any] = {}
        if self.modes is not None:
            out["modes"] = self.modes
        if self.config_options is not None:
            out["configOptions"] = self.config_options
        return out


class SelectorBusyError(RuntimeError):
    """The backing slot is running a prompt outside this ACP connection."""


@dataclass(frozen=True)
class ElicitationResult:
    """A client response to one standard ``elicitation/create`` request."""

    action: str
    content: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.action == "accept" and self.content is not None


def _elicitation_result(raw: Any) -> ElicitationResult:
    """Decode one elicitation response without treating malformed data as input."""
    if not isinstance(raw, dict):
        return ElicitationResult("cancel")
    action = raw.get("action")
    if action in ("decline", "cancel"):
        return ElicitationResult(action)
    content = raw.get("content")
    if action == "accept" and isinstance(content, dict):
        return ElicitationResult("accept", content)
    return ElicitationResult("cancel")


def _merge_selector_state(
    previous: "SelectorState | None", refreshed: "SelectorState | None"
) -> SelectorState:
    """Retain advertised choices when a best-effort refresh is degraded."""
    return SelectorState(
        modes=(
            refreshed.modes
            if refreshed is not None and refreshed.modes is not None
            else previous.modes if previous is not None else None
        ),
        config_options=(
            refreshed.config_options
            if refreshed is not None and refreshed.config_options is not None
            else previous.config_options if previous is not None else None
        ),
    )


def _set_current_mode(state: SelectorState, mode_id: str) -> SelectorState:
    modes = dict(state.modes) if isinstance(state.modes, dict) else None
    if modes is not None:
        modes["currentModeId"] = mode_id
    return SelectorState(modes=modes, config_options=state.config_options)


def _set_current_config_value(state: SelectorState, config_id: str, value: str) -> SelectorState:
    options = [dict(option) for option in state.config_options or []]
    for option in options:
        if option.get("id") == config_id:
            option["currentValue"] = value
            break
    return SelectorState(modes=state.modes, config_options=options or None)


def _mode_ids(state: "SelectorState | None") -> set[str]:
    """The set of advertised ``modeId`` values, from a SelectorState snapshot."""
    if state is None or not isinstance(state.modes, dict):
        return set()
    modes = state.modes.get("availableModes")
    if not isinstance(modes, list):
        return set()
    ids: set[str] = set()
    for m in modes:
        if isinstance(m, dict):
            mid = m.get("id")
            if isinstance(mid, str):
                ids.add(mid)
    return ids


def _config_option(state: "SelectorState | None", config_id: str) -> dict[str, Any] | None:
    """The advertised SessionConfigOption with ``id == config_id``, or None."""
    if state is None or not state.config_options:
        return None
    for opt in state.config_options:
        if isinstance(opt, dict) and opt.get("id") == config_id:
            return opt
    return None


def _select_value_ids(option: dict[str, Any]) -> set[str]:
    """The valid ``value`` ids of a select SessionConfigOption (flat or grouped)."""
    options = option.get("options")
    values: set[str] = set()
    if not isinstance(options, list):
        return values
    for entry in options:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("value"), str):
            values.add(entry["value"])  # ungrouped {value, name}
        elif isinstance(entry.get("options"), list):
            for sub in entry["options"]:  # grouped {group, name, options:[...]}
                if isinstance(sub, dict) and isinstance(sub.get("value"), str):
                    values.add(sub["value"])
    return values


def _normalize_available_commands(value: Any) -> list[dict[str, Any]] | None:
    """Return strict ACP command records, or None when discovery was unavailable."""
    if not isinstance(value, list):
        return None
    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip().lstrip("/")
        if not name or any(char.isspace() for char in name) or name in seen:
            continue
        raw_description = item.get("description")
        description = raw_description if isinstance(raw_description, str) else ""
        command: dict[str, Any] = {"name": name, "description": description}
        raw_input = item.get("input")
        if isinstance(raw_input, dict) and isinstance(raw_input.get("hint"), str):
            command["input"] = {"hint": raw_input["hint"]}
        commands.append(command)
        seen.add(name)
    return commands


@dataclass
class _Session:
    session_id: str
    cwd: str = ""
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    # True while a session/prompt turn is executing. Concurrent prompts for one
    # session are rejected (see _handle_prompt), which also keeps the single
    # per-session ``cancelled`` event unambiguous: only one turn ever clears it.
    in_flight: bool = False
    # Validated, session-scoped stdio MCP servers the client asked for. Parsed
    # at session/new|load|resume; process supervision is owned by the gateway.
    mcp_servers: list[StdioMcpServer] = field(default_factory=list)
    # Form elicitations are sent only when the ACP client explicitly advertised
    # support during initialize; editor identity is never used as a proxy.
    elicitation_supported: bool = False
    # True while a session/set_mode or session/set_config_option mutation is in
    # flight. Selector mutations and prompt turns are mutually exclusive per
    # session: each rejects while the other holds (see _handle_set_mode /
    # _handle_set_config_option / _handle_prompt), which serializes provider
    # recreation with turn execution. Checked-and-set synchronously (no await
    # between), so the guard is race-free on the single event loop.
    selector_in_progress: bool = False
    # The selectors (effort modes + config options) last advertised for this
    # session — cached from session/new|load|resume and refreshed after every
    # successful mutation. An incoming modeId/configId/value is validated against
    # THIS snapshot, so an unadvertised id is rejected (-32602) without a
    # round-trip. None means the backend advertised no selectors.
    selectors: "SelectorState | None" = None


class SessionSink:
    """Streams one turn's output to the editor and gates tools through it.

    Handed to the ``PromptHandler``; every method maps onto an ACP frame for the
    session it was built for.
    """

    def __init__(self, transport: AgentTransport, session: _Session) -> None:
        self._transport = transport
        self._session = session

    @property
    def supports_elicitation(self) -> bool:
        """Whether this session's ACP client explicitly supports form elicitation."""
        return self._session.elicitation_supported

    @property
    def cancelled(self) -> bool:
        """True once the editor sent ``session/cancel`` for this session."""
        return self._session.cancelled.is_set()

    async def _update(self, update: dict[str, Any]) -> None:
        await self._transport.send_notification(
            METHOD_SESSION_UPDATE,
            {"sessionId": self._session.session_id, "update": update},
        )

    async def send_text(self, text: str, *, message_id: str | None = None) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
            "content": {"type": "text", "text": text},
        }
        if message_id:
            update["messageId"] = message_id
        await self._update(update)

    async def send_user_text(self, text: str, *, message_id: str | None = None) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": UPDATE_USER_MESSAGE_CHUNK,
            "content": {"type": "text", "text": text},
        }
        if message_id:
            update["messageId"] = message_id
        await self._update(update)

    async def send_thought(self, text: str) -> None:
        await self._update(
            {
                "sessionUpdate": UPDATE_AGENT_THOUGHT_CHUNK,
                "content": {"type": "text", "text": text},
            }
        )

    async def send_session_info(self, title: str) -> None:
        """Publish the current dashboard title through ACP's metadata update."""
        await self._update({"sessionUpdate": UPDATE_SESSION_INFO, "title": title})

    async def send_options(self, options: list[str]) -> None:
        """Send structured Kiro Crew reply options through namespaced ACP metadata."""
        if not options:
            return
        await self._update(
            {
                "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": ""},
                "_meta": {"kirocrew": {"options": list(options)}},
            }
        )

    async def send_plan(
        self, entries: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None
    ) -> None:
        """Publish the complete execution plan through the standard ACP update."""
        update: dict[str, Any] = {"sessionUpdate": UPDATE_PLAN, "entries": entries}
        if metadata:
            update["_meta"] = metadata
        await self._update(update)

    async def send_tool_call(
        self,
        tool_call_id: str,
        title: str,
        kind: str,
        *,
        status: str = "pending",
        raw_input: dict[str, Any] | None = None,
        content: list[dict[str, Any]] | None = None,
        locations: list[dict[str, Any]] | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": UPDATE_TOOL_CALL,
            "toolCallId": tool_call_id,
            "title": title,
            "kind": kind,
            "status": status,
        }
        if raw_input is not None:
            update["rawInput"] = raw_input
        if content is not None:
            update["content"] = content
        # Editors implement "follow the agent" by watching this field, so
        # empty/None omits it — the schema treats absence as "no target".
        if locations:
            update["locations"] = locations
        await self._update(update)

    async def send_tool_call_update(
        self,
        tool_call_id: str,
        *,
        status: str,
        content: list[dict[str, Any]] | None = None,
        locations: list[dict[str, Any]] | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": UPDATE_TOOL_CALL_UPDATE,
            "toolCallId": tool_call_id,
            "status": status,
        }
        if content is not None:
            update["content"] = content
        if locations:
            update["locations"] = locations
        await self._update(update)

    async def request_permission(
        self,
        tool_call: dict[str, Any],
        *,
        allow_label: str = "Allow",
        reject_label: str = "Reject",
    ) -> bool:
        """Ask the editor to approve one tool call. Returns True when allowed.

        ``tool_call`` is passed through verbatim, so a caller that includes a
        ``content`` block of ``{"type": "diff", "path", "oldText", "newText"}``
        gets the editor's inline diff review with accept/reject — the diff rides
        in this request rather than needing a separate correlated notification.

        Options are emitted in the public-ACP shape (``optionId``/``name``); the
        optionId is the kind, so the outcome maps back without extra bookkeeping.
        Any non-``selected`` outcome, an unrecognised optionId, or a transport
        failure denies — permission is fail-closed.
        """
        params = {
            "sessionId": self._session.session_id,
            "toolCall": tool_call,
            "options": [
                {"optionId": OPTION_ALLOW_ONCE, "name": allow_label, "kind": OPTION_ALLOW_ONCE},
                {"optionId": OPTION_REJECT_ONCE, "name": reject_label, "kind": OPTION_REJECT_ONCE},
            ],
        }
        # Race the editor's answer against this session's cancellation. A cancel
        # (session/cancel) or a transport EOF must not leave the turn blocked on
        # the 120s request timeout: if cancellation wins, deny (fail-closed) and
        # cancel the outbound request so its pending future/id is cleaned up.
        # Transport EOF already fails the pending future closed via
        # AgentTransport._fail_pending -> send_request raises -> we deny below.
        send_task = asyncio.ensure_future(
            self._transport.send_request(METHOD_REQUEST_PERMISSION, params)
        )
        cancel_task = asyncio.ensure_future(self._session.cancelled.wait())
        try:
            await asyncio.wait({send_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task.done() and not send_task.done():
                logger.info("permission request cancelled by session/cancel; denying")
                send_task.cancel()  # triggers send_request's pending-id cleanup
                with contextlib.suppress(BaseException):
                    await send_task
                return False
            try:
                result = send_task.result()
            except Exception:  # incl. asyncio.TimeoutError / transport EOF - deny
                logger.warning("permission request failed; denying", exc_info=True)
                return False
            return _permission_granted(result)
        finally:
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task

    async def create_elicitation(self, params: dict[str, Any]) -> ElicitationResult:
        """Request structured user input through standard ``elicitation/create``.

        Unlike a permission request, an accepted form is ordinary user input;
        cancellation, malformed responses, and transport failure produce no answer.
        """
        if not self._session.elicitation_supported:
            return ElicitationResult("unsupported")
        request = dict(params)
        request["sessionId"] = self._session.session_id
        request.pop("toolCallId", None)
        send_task = asyncio.ensure_future(
            self._transport.send_request(METHOD_ELICITATION_CREATE, request)
        )
        cancel_task = asyncio.ensure_future(self._session.cancelled.wait())
        try:
            await asyncio.wait({send_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task.done() and not send_task.done():
                send_task.cancel()
                with contextlib.suppress(BaseException):
                    await send_task
                return ElicitationResult("cancel")
            try:
                return _elicitation_result(send_task.result())
            except Exception:
                logger.warning("elicitation request failed", exc_info=True)
                return ElicitationResult("cancel")
        finally:
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task


def _permission_granted(result: Any) -> bool:
    """Interpret a ``session/request_permission`` response. Fail-closed."""
    if not isinstance(result, dict):
        return False
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        return False
    if outcome.get("outcome") != OUTCOME_SELECTED:
        return False  # "cancelled" — the editor dismissed the prompt.
    return outcome.get("optionId") == OPTION_ALLOW_ONCE


PromptHandler = Callable[[PromptRequest, SessionSink], Awaitable[str]]


class SessionBackend(Protocol):
    """Backs ACP session lifecycle with an external store (e.g. dashboard slots).

    Given a backend, ``AcpAgentServer`` maps ACP sessions onto that store rather
    than keeping them only in this process: ``session/new`` creates a backing
    session (a dashboard chat slot), ``session/load`` re-points at one, and
    ``session/cancel`` stops it. Without a backend the server keeps its
    self-contained behaviour — uuid session ids and ``loadSession=false``.

    Optional, getattr-detected hooks (not part of this Protocol so a backend may
    omit them, exactly like ``configure_session_mcp``):

    * ``get_available_commands(session_id) -> list[dict]`` — slash commands to
      advertise through the standard ``available_commands_update`` session update.
    * ``get_session_selectors(session_id) -> SelectorState`` — the effort modes
      and config options to advertise for a session. Its presence is what makes
      the server implement ``session/set_mode`` and ``session/set_config_option``
      (advertised via ``modes`` / ``configOptions`` on new|load|resume); without
      it those methods answer method-not-found, unchanged.
    * ``set_session_mode(session_id, mode_id) -> SelectorState`` — apply an effort
      mode and return the refreshed snapshot; raise to signal an internal failure.
    * ``set_session_config_option(session_id, config_id, value) -> SelectorState``
      — apply a config option (e.g. model) and return the refreshed snapshot.
    * ``set_session_info_handler(handler)`` — register an async
      ``(session_id, title)`` callback for live backing-session metadata updates.
      The HTTP backend uses it to forward dashboard ``slot_title`` WebSocket
      events through standard ACP ``session_info_update`` notifications.
    * ``set_session_message_handler(handler)`` — register an async
      ``(session_id, role, content, message_id)`` callback for finalized
      dashboard messages on a session the ACP process owns.
    * ``register_session_info(session_id)`` — authorize title notifications for
      one session owned by this ACP process.
    """

    supports_load: bool
    supports_list: bool
    supports_resume: bool

    async def create_session(self, cwd: str) -> str:
        """Create a backing session scoped to *cwd*; return its id."""
        ...

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        """Activate a session and return its conversation for ACP replay."""
        ...

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Return an ACP ``session/list`` response."""
        ...

    async def resume_session(self, session_id: str, cwd: str) -> None:
        """Resume a backing session without replaying its history."""
        ...

    async def delete_session(self, session_id: str) -> None:
        """Delete a backing session created by this adapter (error-path cleanup).

        Called when a just-created session/new fails after the backing slot was
        created (e.g. MCP hosting failed), so a failed handshake does not leave an
        orphan slot behind. Best-effort: implementations should not raise.
        """
        ...

    async def cancel(self, session_id: str) -> None:
        """Stop the backing session's in-flight turn."""
        ...


class AcpAgentServer:
    """Serves the ACP agent method set over an ``AgentTransport``."""

    def __init__(
        self,
        transport: AgentTransport,
        prompt_handler: PromptHandler,
        *,
        agent_version: str = "0",
        session_backend: "SessionBackend | None" = None,
    ) -> None:
        self._transport = transport
        self._prompt_handler = prompt_handler
        self._agent_version = agent_version
        self._backend = session_backend
        self._sessions: dict[str, _Session] = {}
        self._protocol_version: Any = DEFAULT_PROTOCOL_VERSION
        self._form_elicitation_supported = False
        # Retain refs to fire-and-forget backend-cancel tasks so they are not
        # garbage-collected mid-flight (asyncio only weakly references them).
        self._cancel_tasks: set[asyncio.Task[None]] = set()
        subscribe = getattr(self._backend, "set_session_info_handler", None)
        if callable(subscribe):
            subscribe(self._handle_session_info)
        subscribe_messages = getattr(self._backend, "set_session_message_handler", None)
        if callable(subscribe_messages):
            subscribe_messages(self._handle_session_message)
        subscribe_plans = getattr(self._backend, "set_session_plan_handler", None)
        if callable(subscribe_plans):
            subscribe_plans(self._handle_session_plan)

    async def serve(self) -> None:
        """Run until the editor closes the pipe."""
        await self._transport.run(self._on_request, self._on_notification)

    async def _handle_session_info(self, session_id: str, title: str) -> None:
        """Forward dashboard metadata only to a session this ACP process owns."""
        session = self._sessions.get(session_id)
        if session is not None:
            await SessionSink(self._transport, session).send_session_info(title)

    async def _handle_session_message(
        self, session_id: str, role: str, content: str, message_id: str
    ) -> None:
        """Forward a finalized dashboard row only to its owning editor session."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        sink = SessionSink(self._transport, session)
        if role == "user":
            await sink.send_user_text(content, message_id=message_id)
        elif role == "assistant":
            await sink.send_text(content, message_id=message_id)

    def _register_session_info(self, session_id: str) -> None:
        register = getattr(self._backend, "register_session_info", None)
        if callable(register):
            register(session_id)

    # ── dispatch ──

    async def _on_request(self, method: str, params: dict[str, Any], req_id: Any) -> None:
        if method == METHOD_INITIALIZE:
            await self._handle_initialize(params, req_id)
        elif method == METHOD_SESSION_NEW:
            await self._handle_session_new(params, req_id)
        elif method == METHOD_SESSION_LIST and self._supports("supports_list"):
            await self._handle_session_list(params, req_id)
        elif method == METHOD_SESSION_RESUME and self._supports("supports_resume"):
            await self._handle_session_resume(params, req_id)
        elif method == METHOD_PROMPT:
            await self._handle_prompt(params, req_id)
        elif method == METHOD_SESSION_LOAD and self._supports("supports_load"):
            await self._handle_session_load(params, req_id)
        elif method == METHOD_SET_MODE and self._selector_backend() is not None:
            await self._handle_set_mode(params, req_id)
        elif method == METHOD_SET_CONFIG_OPTION and self._selector_backend() is not None:
            await self._handle_set_config_option(params, req_id)
        else:
            # session/set_model is ALWAYS method-not-found here: it is kiro's
            # proprietary client-side method, not standard ACP, so the agent-role
            # server never serves it. session/set_mode and
            # session/set_config_option reach here only when NO selector backend
            # is wired (standalone in-process server, or a backend without
            # get_session_selectors) — Kiro Crew must NOT no-op them into a false
            # success, which would make an editor believe a switch took effect.
            logger.info("unsupported client->agent request %s", method)
            await self._transport.send_error(
                req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}"
            )

    async def _handle_session_plan(self, session_id: str, plan: dict[str, Any]) -> None:
        """Forward a complete dashboard task plan to its owning editor session."""
        session = self._sessions.get(session_id)
        entries = plan.get("entries") if isinstance(plan, dict) else None
        if session is None or not isinstance(entries, list):
            return
        metadata = plan.get("_meta") if isinstance(plan.get("_meta"), dict) else None
        await SessionSink(self._transport, session).send_plan(entries, metadata=metadata)

    def _command_backend(self) -> "Any | None":
        if self._backend is not None and callable(
            getattr(self._backend, "get_available_commands", None)
        ):
            return self._backend
        return None

    async def _advertise_available_commands(self, session_id: str) -> None:
        """Announce the backend's slash commands without failing session setup."""
        backend = self._command_backend()
        if backend is None:
            return
        try:
            value = await backend.get_available_commands(session_id)
        except Exception:
            logger.warning("command discovery failed for %s", session_id, exc_info=True)
            return
        commands = _normalize_available_commands(value)
        if commands is None:
            return
        await self._emit_update(
            session_id,
            {
                "sessionUpdate": UPDATE_AVAILABLE_COMMANDS,
                "availableCommands": commands,
            },
        )

    def _supports(self, capability: str) -> bool:
        return bool(self._backend is not None and getattr(self._backend, capability, False))

    def _selector_backend(self) -> "Any | None":
        """The backend iff it advertises selectors (has ``get_session_selectors``).

        Its presence is the single capability switch for the model/effort
        selectors: when absent, ``session/set_mode`` and
        ``session/set_config_option`` fall through to method-not-found and no
        ``modes``/``configOptions`` are advertised — the pre-selector behaviour.
        """
        if self._backend is not None and callable(
            getattr(self._backend, "get_session_selectors", None)
        ):
            return self._backend
        return None

    async def _selectors_free(self, session: "_Session", req_id: Any) -> bool:
        """Return True if no prompt/selector op holds *session*, else reject -32602.

        Selector mutations serialize with prompt turns and with each other: each
        is refused while the other is in flight. On the accept path this awaits
        nothing, so the caller's immediate ``selector_in_progress = True`` is
        atomic with this check on the single event loop (no interleaving).
        """
        if session.in_flight:
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a prompt is in progress for this session",
            )
            return False
        if session.selector_in_progress:
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a configuration change is already in progress for this session",
            )
            return False
        return True

    async def _emit_update(self, session_id: str, update: dict[str, Any]) -> None:
        """Send one ``session/update`` notification for *session_id*."""
        await self._transport.send_notification(
            METHOD_SESSION_UPDATE, {"sessionId": session_id, "update": update}
        )

    async def _selector_fields(self, session_id: str, session: "_Session") -> dict[str, Any]:
        """Fetch + cache the session's selectors; return ``{modes, configOptions}``.

        Best-effort by design: selector discovery must never fail
        ``session/new|load|resume``, so a backend error (or a non-SelectorState
        return) advertises no selectors rather than aborting the lifecycle call.
        """
        backend = self._selector_backend()
        if backend is None:
            return {}
        try:
            state = await backend.get_session_selectors(session_id)
        except Exception:
            logger.warning("selector discovery failed for %s", session_id, exc_info=True)
            session.selectors = None
            return {}
        if not isinstance(state, SelectorState):
            session.selectors = None
            return {}
        session.selectors = state
        return state.response_fields()

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == METHOD_CANCEL:
            session_id = str(params.get("sessionId", ""))
            session = self._sessions.get(session_id)
            if session is not None:
                session.cancelled.set()
            if self._backend is not None and session_id:
                # Stop the backing dashboard turn too; the local flag only stops
                # our stream translation, not the turn the gateway owns.
                task = asyncio.create_task(self._safe_cancel(session_id))
                self._cancel_tasks.add(task)
                task.add_done_callback(self._cancel_tasks.discard)
            return
        logger.debug("ignoring notification %s", method)

    async def _safe_cancel(self, session_id: str) -> None:
        try:
            await self._backend.cancel(session_id)  # type: ignore[union-attr]
        except Exception:
            logger.warning("backend cancel failed for %s", session_id, exc_info=True)

    # ── handlers ──

    async def _handle_initialize(self, params: dict[str, Any], req_id: Any) -> None:
        # Strict ACP v1 negotiation. We support exactly SUPPORTED_PROTOCOL_VERSION
        # and always respond with it, so the client learns the version we will
        # actually speak and can decide whether to proceed. We never echo an
        # unrecognised value the peer offered — that would claim to speak a
        # protocol variant we do not.
        offered = params.get("protocolVersion")
        if offered is not None and offered != SUPPORTED_PROTOCOL_VERSION:
            logger.info(
                "client offered protocol version %r; negotiating to %d",
                offered,
                SUPPORTED_PROTOCOL_VERSION,
            )
        self._protocol_version = SUPPORTED_PROTOCOL_VERSION
        client_capabilities = params.get("clientCapabilities")
        elicitation = (
            client_capabilities.get("elicitation")
            if isinstance(client_capabilities, dict)
            else None
        )
        self._form_elicitation_supported = bool(
            isinstance(elicitation, dict) and isinstance(elicitation.get("form"), dict)
        )
        supports_load = self._supports("supports_load")
        session_capabilities: dict[str, dict[str, Any]] = {}
        if self._supports("supports_list"):
            session_capabilities[CAP_SESSION_LIST] = {}
        if self._supports("supports_resume"):
            session_capabilities[CAP_SESSION_RESUME] = {}
        capabilities: dict[str, Any] = {CAP_LOAD_SESSION: supports_load}
        if session_capabilities:
            capabilities[CAP_SESSION_CAPABILITIES] = session_capabilities
        await self._transport.send_result(
            req_id,
            {
                "protocolVersion": self._protocol_version,
                "agentCapabilities": capabilities,
                "agentInfo": {"name": AGENT_NAME, "version": self._agent_version},
            },
        )

    async def _handle_session_new(self, params: dict[str, Any], req_id: Any) -> None:
        cwd = await self._require_abs_cwd(params, req_id)
        if cwd is None:
            return
        servers = await self._parse_mcp_or_error(params, req_id)
        if servers is None:
            return
        if self._backend is not None:
            try:
                session_id = await self._backend.create_session(cwd)
            except Exception:
                logger.exception("backend create_session failed")
                await self._transport.send_error(
                    req_id, JSONRPC_INTERNAL_ERROR, "Failed to create session"
                )
                return
        else:
            session_id = f"{AGENT_NAME}-{uuid.uuid4().hex[:12]}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            cwd=cwd,
            mcp_servers=servers,
            elicitation_supported=self._form_elicitation_supported,
        )
        self._register_session_info(session_id)
        if not await self._host_session_mcp(session_id, servers, req_id):
            # session/new created the backing slot above; a failed MCP setup must
            # not leave it orphaned on the dashboard. load/resume never reach here
            # for a self-created slot — their slot pre-existed and is not owned by
            # this adapter, so it is preserved.
            if self._backend is not None:
                await self._delete_backing_session(session_id)
            return
        fields = await self._selector_fields(session_id, self._sessions[session_id])
        await self._transport.send_result(req_id, {"sessionId": session_id, **fields})
        await self._advertise_available_commands(session_id)

    async def _delete_backing_session(self, session_id: str) -> None:
        """Best-effort delete of a backend slot this adapter just created."""
        delete = getattr(self._backend, "delete_session", None)
        if delete is None:
            return
        try:
            await delete(session_id)
        except Exception:
            logger.warning("failed to delete orphan backing session %s", session_id, exc_info=True)

    async def _handle_session_load(self, params: dict[str, Any], req_id: Any) -> None:
        assert self._backend is not None  # gated by _on_request
        session_id = await self._require_session_id(params, req_id)
        if session_id is None:
            return
        cwd = await self._require_abs_cwd(params, req_id)
        if cwd is None:
            return
        servers = await self._parse_mcp_or_error(params, req_id)
        if servers is None:
            return
        try:
            messages = await self._backend.load_session(session_id, cwd)
        except Exception:
            logger.exception("backend load_session failed for %s", session_id)
            await self._transport.send_error(
                req_id, JSONRPC_INTERNAL_ERROR, "Failed to load session"
            )
            return
        session = _Session(
            session_id=session_id,
            cwd=cwd,
            mcp_servers=servers,
            elicitation_supported=self._form_elicitation_supported,
        )
        self._sessions[session_id] = session
        self._register_session_info(session_id)
        if not await self._host_session_mcp(session_id, servers, req_id):
            return
        sink = SessionSink(self._transport, session)
        for index, message in enumerate(messages):
            role = message.get("role", "")
            text = message.get("content", "")
            if not text:
                continue
            message_id = f"history-{index}"
            if role == "user":
                await sink.send_user_text(text, message_id=message_id)
            elif role in ("assistant", "streaming"):
                await sink.send_text(text, message_id=message_id)
        fields = await self._selector_fields(session_id, session)
        await self._transport.send_result(req_id, fields)
        await self._advertise_available_commands(session_id)

    async def _handle_session_list(self, params: dict[str, Any], req_id: Any) -> None:
        # ACP: ListSessionsRequest.cwd is optional, but when supplied it "Must be
        # an absolute path". Reject a present-but-non-absolute value with -32602
        # rather than silently passing an unusable filter to the backend.
        raw_cwd = params.get("cwd")
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str) or not raw_cwd or not os.path.isabs(raw_cwd):
                await self._transport.send_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "'cwd' must be an absolute path",
                )
                return
            cwd: str | None = raw_cwd
        else:
            cwd = None
        cursor = params.get("cursor") if isinstance(params.get("cursor"), str) else None
        assert self._backend is not None
        try:
            result = await self._backend.list_sessions(cwd=cwd, cursor=cursor)
        except Exception:
            logger.exception("backend list_sessions failed")
            await self._transport.send_error(
                req_id, JSONRPC_INTERNAL_ERROR, "Failed to list sessions"
            )
            return
        await self._transport.send_result(req_id, result)

    async def _handle_session_resume(self, params: dict[str, Any], req_id: Any) -> None:
        assert self._backend is not None
        session_id = await self._require_session_id(params, req_id)
        if session_id is None:
            return
        cwd = await self._require_abs_cwd(params, req_id)
        if cwd is None:
            return
        servers = await self._parse_mcp_or_error(params, req_id)
        if servers is None:
            return
        try:
            await self._backend.resume_session(session_id, cwd)
        except Exception:
            logger.exception("backend resume_session failed for %s", session_id)
            await self._transport.send_error(
                req_id, JSONRPC_INTERNAL_ERROR, "Failed to resume session"
            )
            return
        self._sessions[session_id] = _Session(
            session_id=session_id,
            cwd=cwd,
            mcp_servers=servers,
            elicitation_supported=self._form_elicitation_supported,
        )
        self._register_session_info(session_id)
        if not await self._host_session_mcp(session_id, servers, req_id):
            return
        fields = await self._selector_fields(session_id, self._sessions[session_id])
        await self._transport.send_result(req_id, fields)
        await self._advertise_available_commands(session_id)

    async def _handle_prompt(self, params: dict[str, Any], req_id: Any) -> None:
        session_id = await self._require_session_id(params, req_id)
        if session_id is None:
            return
        blocks_error = _validate_prompt_blocks(params.get("prompt"))
        if blocks_error is not None:
            await self._transport.send_error(req_id, JSONRPC_INVALID_PARAMS, blocks_error)
            return
        session = self._sessions.get(session_id)
        if session is None:
            # session/prompt DOES exist; only the sessionId parameter is invalid
            # (unknown/stale). That is -32602 Invalid params, not -32601 Method
            # not found — a strict client branching on the code must not read a
            # bad session id as "this agent doesn't implement session/prompt".
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, f"Unknown session: {session_id}"
            )
            return
        if session.selector_in_progress:
            # A model/effort switch is applying (it recreates the provider). Do
            # not start a turn on top of it — mirror the reverse rejection in
            # _selectors_free so the two are strictly serialized.
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a configuration change is in progress for this session",
            )
            return
        if session.in_flight:
            # One turn per session. A second concurrent prompt would race the
            # shared per-session cancel scope (cancelled.clear() below). Reject
            # rather than corrupt the in-flight turn's cancellation.
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a prompt is already in progress for this session",
            )
            return

        session.in_flight = True
        # A turn is one cancel scope; a stale flag from the previous turn would
        # abort this one before it starts. Safe to clear because in_flight
        # guarantees no other turn is observing this event.
        session.cancelled.clear()
        try:
            sink = SessionSink(self._transport, session)
            blocks = params.get("prompt") or []
            request = PromptRequest(
                session_id=session_id,
                text=prompt_blocks_to_text(blocks),
                content_blocks=list(blocks),
            )
            try:
                stop_reason = await self._prompt_handler(request, sink)
            except asyncio.CancelledError:
                await self._transport.send_result(req_id, {"stopReason": STOP_REASON_CANCELLED})
                raise
            except Exception:
                logger.exception("prompt handler failed for session %s", session_id)
                # A handler fault is an internal error, not a normal turn end.
                # Reply with a JSON-RPC internal error rather than an out-of-schema
                # stopReason="error" (not a valid ACP stop reason). The editor
                # keeps the session usable and can prompt again.
                await self._transport.send_error(req_id, JSONRPC_INTERNAL_ERROR, "Turn failed")
                return
            if session.cancelled.is_set():
                await self._transport.send_result(req_id, {"stopReason": STOP_REASON_CANCELLED})
                return
            final = stop_reason or STOP_REASON_END_TURN
            if final not in ACP_VALID_STOP_REASONS:
                # A handler signalled a non-standard failure sentinel (e.g. the
                # bare "error" the HTTP backend returns when the gateway is
                # unreachable). Map it to an internal error so an editor never
                # receives an out-of-schema stop reason.
                logger.warning(
                    "non-conformant stop reason %r for session %s; replying -32603",
                    final,
                    session_id,
                )
                await self._transport.send_error(req_id, JSONRPC_INTERNAL_ERROR, "Turn failed")
                return
            await self._transport.send_result(req_id, {"stopReason": final})
        finally:
            session.in_flight = False

    # ── selector handlers (session/set_mode, session/set_config_option) ──

    async def _handle_set_mode(self, params: dict[str, Any], req_id: Any) -> None:
        """``session/set_mode``: switch the session's reasoning-effort mode.

        Standard ACP v1: the response is empty and the new mode is announced with
        a ``current_mode_update`` notification. Strict errors — a malformed
        ``modeId``, an unknown/stale session, or a modeId not among the advertised
        ``availableModes`` answer ``-32602``; a backend/apply failure answers
        ``-32603`` and announces nothing, so the client's view stays at the
        last-known-good mode (rollback).
        """
        backend = self._selector_backend()
        assert backend is not None  # gated by _on_request
        session_id = await self._require_session_id(params, req_id)
        if session_id is None:
            return
        mode_id = params.get("modeId")
        if not isinstance(mode_id, str) or not mode_id:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, "'modeId' must be a non-empty string"
            )
            return
        session = self._sessions.get(session_id)
        if session is None:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, f"Unknown session: {session_id}"
            )
            return
        if mode_id not in _mode_ids(session.selectors):
            # Not among availableModes advertised for this session — an
            # unadvertised id, not an internal fault.
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, f"Unknown mode: {mode_id}"
            )
            return
        if not await self._selectors_free(session, req_id):
            return
        session.selector_in_progress = True
        try:
            state = await backend.set_session_mode(session_id, mode_id)
        except SelectorBusyError:
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a prompt is in progress for this session",
            )
            return
        except Exception:
            logger.exception("set_session_mode failed for session %s", session_id)
            await self._transport.send_error(
                req_id, JSONRPC_INTERNAL_ERROR, "Failed to set session mode"
            )
            return
        finally:
            session.selector_in_progress = False
        refreshed = state if isinstance(state, SelectorState) else None
        session.selectors = _set_current_mode(
            _merge_selector_state(session.selectors, refreshed), mode_id
        )
        await self._transport.send_result(req_id, {})
        current = mode_id
        if session.selectors is not None and isinstance(session.selectors.modes, dict):
            reported = session.selectors.modes.get("currentModeId")
            if isinstance(reported, str) and reported:
                current = reported
        await self._emit_update(
            session_id, {"sessionUpdate": UPDATE_CURRENT_MODE, "currentModeId": current}
        )

    async def _handle_set_config_option(self, params: dict[str, Any], req_id: Any) -> None:
        """``session/set_config_option``: switch a config option (e.g. model).

        Standard ACP v1: the response carries the full refreshed ``configOptions``
        and the change is also announced with a ``config_option_update``
        notification. Strict errors — malformed params, an unknown/stale session,
        an unknown ``configId``, or a value not among that option's advertised
        select values answer ``-32602``; a backend/apply failure answers
        ``-32603`` and announces nothing (rollback).

        Kiro Crew advertises only single-value ``select`` options (the model
        picker), so a boolean-typed request payload is rejected as unadvertised.
        """
        backend = self._selector_backend()
        assert backend is not None  # gated by _on_request
        session_id = await self._require_session_id(params, req_id)
        if session_id is None:
            return
        config_id = params.get("configId")
        if not isinstance(config_id, str) or not config_id:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, "'configId' must be a non-empty string"
            )
            return
        if params.get("type") == CONFIG_OPTION_TYPE_BOOLEAN:
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                f"config option {config_id!r} does not accept a boolean value",
            )
            return
        value = params.get("value")
        if not isinstance(value, str) or not value:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, "'value' must be a non-empty string"
            )
            return
        session = self._sessions.get(session_id)
        if session is None:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, f"Unknown session: {session_id}"
            )
            return
        option = _config_option(session.selectors, config_id)
        if option is None:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, f"Unknown config option: {config_id}"
            )
            return
        if value not in _select_value_ids(option):
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                f"Unknown value {value!r} for config option {config_id!r}",
            )
            return
        if not await self._selectors_free(session, req_id):
            return
        session.selector_in_progress = True
        try:
            state = await backend.set_session_config_option(session_id, config_id, value)
        except SelectorBusyError:
            await self._transport.send_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "a prompt is in progress for this session",
            )
            return
        except Exception:
            logger.exception("set_session_config_option failed for session %s", session_id)
            await self._transport.send_error(
                req_id, JSONRPC_INTERNAL_ERROR, "Failed to set config option"
            )
            return
        finally:
            session.selector_in_progress = False
        refreshed = state if isinstance(state, SelectorState) else None
        session.selectors = _set_current_config_value(
            _merge_selector_state(session.selectors, refreshed), config_id, value
        )
        options = session.selectors.config_options or []
        # SetSessionConfigOptionResponse REQUIRES configOptions; the notification
        # carries the same full set.
        await self._transport.send_result(req_id, {"configOptions": options})
        await self._emit_update(
            session_id, {"sessionUpdate": UPDATE_CONFIG_OPTION, "configOptions": options}
        )

    # ── validation helpers ──

    async def _require_abs_cwd(self, params: dict[str, Any], req_id: Any) -> str | None:
        """Return the request's ``cwd`` if it is a non-empty absolute path.

        ACP requires ``cwd`` on new/load/resume to be an absolute path. On a bad
        value this answers ``-32602`` and returns None so the caller returns.
        """
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, "'cwd' must be a non-empty absolute path"
            )
            return None
        return cwd

    async def _require_session_id(self, params: dict[str, Any], req_id: Any) -> str | None:
        """Return the request's ``sessionId`` if it is a non-empty string."""
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            await self._transport.send_error(
                req_id, JSONRPC_INVALID_PARAMS, "'sessionId' must be a non-empty string"
            )
            return None
        return session_id

    async def _parse_mcp_or_error(
        self, params: dict[str, Any], req_id: Any
    ) -> "list[StdioMcpServer] | None":
        """Parse ``mcpServers`` structurally, or answer ``-32602`` and return None.

        An unsupported transport or a malformed entry fails the request rather
        than being silently ignored — a client must know the server it asked for
        will not be available.
        """
        try:
            return parse_mcp_servers(params.get("mcpServers"))
        except McpConfigError as exc:
            await self._transport.send_error(req_id, JSONRPC_INVALID_PARAMS, str(exc))
            return None

    async def _apply_session_mcp(
        self, session_id: str, servers: "list[StdioMcpServer]"
    ) -> str | None:
        """Register a session's validated stdio MCP set through the backend.

        Returns ``None`` on success, or a client-safe error message when hosting
        fails. The backend's optional ``configure_session_mcp`` hook validates
        each server (preflight) under Kiro Crew's sandbox/credential policy and
        registers the set for this session's slot; the model-side provider spawns
        the servers on the next prompt and owns them for the session's life.

        The hook is called for EVERY new/load/resume, including with an empty
        set: an empty registration clears any config a prior turn left on the
        slot, so a ``session/load`` / ``session/resume`` replacement never leaves
        stale servers behind. When there are no servers AND the backend has no
        hook (e.g. the standalone in-process server), there is nothing to clear
        and nothing to host — that is a benign no-op, not a failure.

        Failures are surfaced, never swallowed: a client that supplied a server
        Kiro Crew cannot host is told so (the caller turns the message into an ACP
        error) rather than handed a session that silently lacks the tools. A
        backend without the hook that is asked to host a non-empty set is itself
        a surfaced failure — accepting a config that will never run is exactly
        what this must avoid.
        """
        configure = getattr(self._backend, "configure_session_mcp", None)
        if configure is None:
            if not servers:
                return None
            return (
                f"{len(servers)} MCP server(s) were requested but this session "
                "backend cannot host client-supplied MCP servers"
            )
        try:
            await configure(session_id, servers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # McpSpawnError (and any hook error marked acp_client_safe) carries a
            # secret-safe, actionable message; forward it. Anything else is an
            # unexpected fault — log it and return a generic message so no
            # internal detail leaks to the editor.
            if getattr(exc, "acp_client_safe", False):
                logger.warning("MCP host failed for session %s: %s", session_id, exc)
                return str(exc)
            logger.exception("MCP configuration crashed for session %s", session_id)
            return "Failed to start the requested MCP servers"
        return None

    async def _host_session_mcp(
        self, session_id: str, servers: "list[StdioMcpServer]", req_id: Any
    ) -> bool:
        """Host *servers*, or fail the request. Returns True to continue.

        On failure the just-created session is dropped and a JSON-RPC internal
        error (carrying the client-safe reason) is sent, so the caller must
        ``return`` immediately when this returns False.
        """
        error = await self._apply_session_mcp(session_id, servers)
        if error is None:
            return True
        self._sessions.pop(session_id, None)
        await self._transport.send_error(req_id, JSONRPC_INTERNAL_ERROR, error)
        return False


def extract_prompt_text(params: dict[str, Any]) -> str:
    """Flatten a ``session/prompt`` request's content blocks to text."""
    blocks = params.get("prompt")
    if not isinstance(blocks, list):
        return ""
    return prompt_blocks_to_text(blocks)


def _validate_prompt_blocks(blocks: Any) -> str | None:
    """Structurally validate a ``session/prompt`` ``prompt`` value.

    Returns an error message for a ``-32602`` reply, or None when valid. The
    ``prompt`` array is required (ACP session/prompt has no valid form without
    it); an *empty* array is valid — a prompt carrying no content blocks. Each
    block must be an object with a non-empty string ``type``.
    """
    if blocks is None:
        return "'prompt' is required and must be an array of content blocks"
    if not isinstance(blocks, list):
        return "'prompt' must be an array of content blocks"
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            return f"prompt[{index}] must be an object"
        btype = block.get("type")
        if not isinstance(btype, str) or not btype:
            return f"prompt[{index}] must have a non-empty string 'type'"
    return None


def prompt_blocks_to_text(blocks: list[Any]) -> str:
    """Convert ACP prompt content blocks to text for a text-only chat core.

    A documented, preserve-what-we-can conversion — NOT a blind collapse to a
    ``[type]`` token:
      - ``text``            → its text
      - ``resource_link``   → its ``uri`` (a handle the model can act on)
      - ``resource``        → the embedded ``resource.text`` if present, else its ``uri``
      - ``image``/``audio`` → ``[image: <uri>]`` when a uri/name is present; a
        bare inline-data block carries no textual handle and is omitted, since a
        text-only core cannot convey the bytes.
    An unknown block type falls back to any ``text`` field it carries, else is
    omitted rather than emitting a mystery token. Blocks are concatenated with no
    separator, preserving the historical behaviour for plain text prompts.
    """
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            parts.append(str(b.get("text", "")))
        elif btype == "resource_link":
            uri = b.get("uri")
            if isinstance(uri, str) and uri:
                parts.append(uri)
        elif btype == "resource":
            res = b.get("resource")
            if isinstance(res, dict):
                text = res.get("text")
                uri = res.get("uri")
                if isinstance(text, str) and text:
                    parts.append(text)
                elif isinstance(uri, str) and uri:
                    parts.append(uri)
        elif btype in ("image", "audio"):
            ref = b.get("uri") or b.get("name")
            if isinstance(ref, str) and ref:
                parts.append(f"[{btype}: {ref}]")
        else:
            text = b.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)
