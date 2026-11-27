"""Per-caller adjustments of a shared ``Backend``: folders and roots.

:func:`scope_forward` is the ONE place a stub's request is adjusted for the
caller that sent it, after the gateway has named that caller and before the
frame is written to the backend. A later per-caller rule (call serialization, a
per-caller browser tab) belongs here, not at a second site.

What it does today (:mod:`kiro_crew.mcp_gateway.folder_scope`):

* a ``tools/call`` to a third-party server gets its relative folder arguments
  made absolute against the caller's folder, and a missing required one filled;
* a server's ``roots/list`` is answered by the gateway with its configured
  folders and every chat folder it has served (:func:`serve_roots`), and
  ``notifications/roots/list_changed`` is sent when a new folder joins
  (:func:`note_caller`), so the backend is not recycled and the server is not
  kept one copy per chat.

Kiro Crew's own servers (``supports_caller_identity``) are never adjusted: they
read the caller block themselves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import call_gate, folder_scope, hazards, tab_tenancy
from kiro_crew.mcp_gateway.backend_wire import (
    INTERNAL_STUB_PREFIXES,
    _tool_call_error_text,
    _write_json_line,
    _write_json_lines,
)

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend
    from kiro_crew.mcp_gateway.backend_wire import _PendingRequest


@dataclass
class CallerScopeState:
    """What a shared ``Backend`` knows about its callers (one per backend).

    ``tool_scopes`` is learned from the backend's own complete tools/list
    answers (``None`` = never seen, so nothing is scoped); ``stub_sessions`` is
    the attested session each attached stub forwarded for, pruned on detach;
    ``roots_served`` is set once the gateway answered the server's roots/list
    itself and ``roots_answer`` is the folder list last answered or announced;
    ``call_gate`` serializes tools/call (``None`` = off); ``tab_tools`` is the
    (tabs tool, close tool) pair of a browser server and ``tab_active`` each
    caller context's last tab.
    """

    tool_scopes: Optional[dict[str, Any]] = None
    folder_confinable: bool = False
    stub_sessions: dict[str, str] = field(default_factory=dict)
    roots_served: bool = False
    roots_answer: Optional[list[str]] = None
    scope_tasks: set["asyncio.Task[None]"] = field(default_factory=set)
    call_gate: "Optional[call_gate.CallGate]" = None
    tab_tools: Optional[tuple[str, str]] = None
    tab_active: dict[str, int] = field(default_factory=dict)
    #: The session whose tabs live in the browser's default context, the one
    #: holding the user's persistent profile: the first caller (``_tab_context``).
    default_tab_owner: Optional[str] = None
    #: Directories named in the server's own launch arguments (``note_launch``):
    #: they stay in every roots answer, since roots replace them.
    launch_folders: list[str] = field(default_factory=list)
    #: The tools whose calls take turns (``call_gate.serialized_tools``).
    gated_tools: frozenset[str] = frozenset()
    #: Every chat folder this backend has served, oldest first (``_attached_union``).
    seen_folders: list[str] = field(default_factory=list)
    #: Whether the harness's own initialize offered roots (``offer_roots``).
    harness_offers_roots: bool = True
    #: A roots/list was declined because a caller's folder was not known yet.
    roots_pending: bool = False
    #: The server asked for its roots and the gateway took the request: it
    #: reads paths against roots, so a caller's relative path is the caller's.
    roots_asked: bool = False
    #: ``roots/list`` ids the gateway declined to serve, routed the usual way.
    roots_routed: set[Any] = field(default_factory=set)
    #: Serialized calls never written when the backend went, per stub, as each
    #: stub sent them (``hold_unsent``); its connection takes them back.
    resend: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Set once the backend is gone, so a connection waiting to read wakes.
    gone: asyncio.Event = field(default_factory=asyncio.Event)
    #: Chat folders not served because every served one was in use.
    folders_left_out: int = 0
    #: The forward id a rehandshake retry replaces (``note_refused``).
    refused_fid: str = ""
    #: A roots answer with chat folders is being written (``serve_roots``).
    roots_writing: bool = False


_STATE_ATTR = "_caller_scope"


def state(self: Backend) -> CallerScopeState:
    """This backend's caller state, made on first use and kept on the instance."""
    found = self.__dict__.get(_STATE_ATTR)
    if not isinstance(found, CallerScopeState):
        found = CallerScopeState()
        self.__dict__[_STATE_ATTR] = found
    return found


logger = logging.getLogger("kiro_crew.mcp_gateway.backend")

_ROOTS_LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}


def _adjustable(self: Backend) -> bool:
    return not self.supports_caller_identity


def note_tools_list(self: Backend, pending: "_PendingRequest", msg: dict[str, Any]) -> None:
    """Learn each tool's scope from a complete ``tools/list`` answer on this backend.

    A page of a paginated listing, or an error, leaves the scopes unknown: a
    server whose whole tool set was never seen is not scoped.
    """
    result = msg.get("result")
    if (
        not isinstance(result, dict)
        or pending.list_paginated
        or result.get("nextCursor") is not None
    ):
        return
    tools = result.get("tools")
    if isinstance(tools, list):
        state(self).tool_scopes = folder_scope.scan_tools(tools)
        state(self).folder_confinable = folder_scope.tools_confinable(tools) is True
        if state(self).roots_pending and state(self).folder_confinable:
            # A declined roots/list may be servable now that the tools are known.
            _schedule(self, _send_roots_changed_if_needed(self))
        if _adjustable(self):
            st = state(self)
            # A tab per caller only where callers share the browser: a
            # connection-private backend has one chat and keeps its calls as sent.
            st.tab_tools = None if self.exclusive_token else tab_tenancy.find_tabs_tool(tools)
            st.gated_tools = call_gate.serialized_tools(
                tools, whole_server=st.tab_tools is not None
            )
            if st.call_gate is None and st.gated_tools:
                st.call_gate = call_gate.CallGate(lambda fid, frame: _gated_write(self, fid, frame))
            if tab_tenancy.single_page_browser(tools):
                # No tab to give each chat: every chat gets its own copy. One
                # already shared is recycled, and its stubs respawn privately.
                self._record_hazard(hazards.HAZARD_SINGLE_PAGE_BROWSER, any_client=True)
                if len(_real_stubs(self)) > 1 and not self._dead_reason:
                    reason = "single-page browser server cannot serve several chats; recycling"
                    self._dead_reason = reason
                    task = asyncio.get_running_loop().create_task(
                        self._broadcast_backend_gone(reason)
                    )
                    state(self).scope_tasks.add(task)
                    task.add_done_callback(state(self).scope_tasks.discard)


class _Unwanted(Exception):
    """The call was cancelled, or its chat left, while its tab was prepared."""


def _still_wanted(self: Backend, fid: str, pending: Optional["_PendingRequest"]) -> bool:
    """The call still holds its slot, was not cancelled, and its chat is still here."""
    gate = state(self).call_gate
    return (
        gate is not None
        and gate.wanted(fid)
        and pending is not None
        and not pending.cancelled
        and self._pending_requests.get(fid) is pending
    )


async def _gated_write(self: Backend, fid: str, frame: dict[str, Any]) -> bool:
    """Write one serialized call upstream (``call_gate``), its caller's tab made current first.

    Returns False when nothing was sent: the call was cancelled, its chat left,
    or the gateway answered it. The gate then frees the slot.
    """
    pending = self._pending_requests.get(fid)
    if not _still_wanted(self, fid, pending):
        self._pending_requests.pop(fid, None)
        return False
    assert pending is not None
    if state(self).tab_tools is not None and pending.session_key:
        try:
            prepared = await _prepare_tab(self, fid, frame, pending)
        except _Unwanted:
            self._pending_requests.pop(fid, None)
            return False
        except Exception as exc:
            if not _still_wanted(self, fid, pending):
                self._pending_requests.pop(fid, None)
                return False
            await _answer_locally(
                self, fid, pending, error=f"browser tab could not be prepared: {exc}"
            )
            return False
        if prepared is None:
            return False
        frame = prepared
        if pending.retry_frame is not None:
            # A rehandshake retry re-sends the call as the server received it.
            pending.retry_frame = frame
        if not _still_wanted(self, fid, pending):
            # Cancelled, or its chat left, while the tab was prepared.
            self._pending_requests.pop(fid, None)
            return False
    gate = state(self).call_gate

    def _going_out() -> bool:
        # Decided under the write lock: a cancel that came while this call
        # waited behind another write drops it; from here a cancel goes upstream.
        if not _still_wanted(self, fid, pending):
            self._pending_requests.pop(fid, None)
            return False
        if gate is not None:
            gate.mark_sending(fid)
        return True

    try:
        return await _write_json_lines(self.stdin, [frame], before_write=_going_out)
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._dead_reason = self._dead_reason or f"stdin closed: {exc}"
        raise


def cancel_gated(self: Backend, fid: str) -> bool:
    """Cancel *fid* at the gate; True when it was never written (no upstream cancel owed)."""
    gate = state(self).call_gate
    if gate is None:
        return False
    outcome = gate.cancel(fid)
    if outcome == call_gate.DROPPED:
        self._pending_requests.pop(fid, None)
    return outcome in (call_gate.DROPPED, call_gate.UNSENT)


_INTERNAL_CALL_TIMEOUT_SECS = 60.0


async def _internal_call(self: Backend, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One gateway-originated ``tools/call``, run while the caller's slot is held.

    A server whose MCP session was lost behind the pipe refuses it as not
    initialized; it is then handshaken again and asked once more, as a
    forwarded call would be (``backend_handshake._retry_after_rehandshake``).
    """
    from kiro_crew.mcp_gateway.backend_handshake import (
        _REHANDSHAKE_STUB_SENTINEL,
        _is_lost_session_error,
    )

    params = {"name": tool, "arguments": arguments}
    reply = await _internal_send(self, params, handshake_first=False)
    if _is_lost_session_error(reply) and self._upstream_init_frame is not None:
        reply = await _internal_send(
            self, params, handshake_first=True, sentinel=_REHANDSHAKE_STUB_SENTINEL
        )
    failure = _tool_call_error_text(reply)
    if failure is not None:
        raise RuntimeError(failure)
    return reply


async def _internal_send(
    self: Backend, params: dict[str, Any], *, handshake_first: bool, sentinel: str = ""
) -> dict[str, Any]:
    from kiro_crew.mcp_gateway.backend_mcp_apps import _APPS_STUB_SENTINEL
    from kiro_crew.mcp_gateway.backend_wire import _PendingRequest

    now_ms = time.monotonic() * 1000.0
    fid = self._next_forward_id()
    fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
    self._pending_requests[fid] = _PendingRequest(
        stub_uuid=_APPS_STUB_SENTINEL,
        original_id=None,
        method="tools/call",
        # Stamped like every forwarded request: the wedge sweep reads an
        # unset start as a request as old as the host's uptime.
        t_start_ms=now_ms,
        apps_future=fut,
    )
    frames: list[dict[str, Any]] = []
    init_fid = ""
    if handshake_first and self._upstream_init_frame is not None:
        init_fid = self._next_forward_id()
        self._pending_requests[init_fid] = _PendingRequest(
            stub_uuid=sentinel, original_id=None, method="initialize", t_start_ms=now_ms
        )
        frames += [
            {**self._upstream_init_frame, "id": init_fid},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
    frames.append({"jsonrpc": "2.0", "id": fid, "method": "tools/call", "params": params})
    try:
        await _write_json_lines(self.stdin, frames)
        return await asyncio.wait_for(fut, timeout=_INTERNAL_CALL_TIMEOUT_SECS)
    finally:
        self._pending_requests.pop(fid, None)


async def _answer_locally(
    self: Backend, fid: str, pending: "_PendingRequest", *, text: str = "", error: str = ""
) -> None:
    """Reply to the caller from the gateway and free the slot; the server never sees the call."""
    self._pending_requests.pop(fid, None)
    content = [{"type": "text", "text": error or text}]
    result: dict[str, Any] = (
        {"content": content, "isError": True} if error else {"content": content}
    )
    try:
        await self._reply_locally(pending.stub_uuid, pending.original_id, result=result)
    finally:
        gate = state(self).call_gate
        if gate is not None:
            gate.release(fid)


async def _prepare_tab(
    self: Backend, fid: str, frame: dict[str, Any], pending: "_PendingRequest"
) -> Optional[dict[str, Any]]:
    """Make the caller's own tab current and map its tab arguments; ``None`` = answered here."""
    tools = state(self).tab_tools
    assert tools is not None
    tabs_tool, close_tool = tools
    session = pending.session_key
    # The tab calls below are the gateway's, made for this caller: they are
    # governed as the caller's own call of the tabs tool, and audited.
    denial = await asyncio.to_thread(_tab_call_denial, self, pending, tabs_tool)
    if denial is not None:
        _audit_tab_call(self, pending, tabs_tool, "denied", reason=denial)
        await _answer_locally(
            self, fid, pending, error=f"browser tab for this chat refused: {denial}"
        )
        return None

    async def _tab_call(arguments: dict[str, Any]) -> dict[str, Any]:
        _audit_tab_call(self, pending, tabs_tool, "allowed", action=str(arguments.get("action")))
        return await _internal_call(self, tabs_tool, arguments)

    ctx = _tab_context(self, session, claim=True)
    listing = tab_tenancy.parse_tabs(tab_tenancy.result_text(await _tab_call({"action": "list"})))
    if not _still_wanted(self, fid, pending):
        raise _Unwanted()
    mine = tab_tenancy.own_tabs(listing, ctx)
    current = next((t for t in listing if t.current), None)
    if current is not None and current.context != ctx:
        # Remember where the chat that owns the current tab was, before moving off it.
        owner_tabs = tab_tenancy.own_tabs(listing, current.context)
        state(self).tab_active[current.context] = owner_tabs.index(current)
    name = pending.tool_name
    args = dict(pending.tool_arguments or {})
    if close_tool and name == close_tool:
        for tab in sorted(mine, key=lambda t: t.index, reverse=True):
            if not _still_wanted(self, fid, pending):
                raise _Unwanted()
            await _tab_call({"action": "close", "index": tab.index})
        state(self).tab_active.pop(ctx, None)
        await _answer_locally(
            self, fid, pending, text="### Result\nClosed this chat's browser tabs."
        )
        return None
    if name == tabs_tool:
        action = args.get("action")
        if action == "new":
            # The caller's tab always opens in the caller's context, whatever it
            # named, or its own listings and selection would not find it.
            if ctx:
                args["isolatedContext"] = ctx
            else:
                args.pop("isolatedContext", None)
            return _with_arguments(frame, args)
        if action in ("select", "close") and "index" in args:
            index = tab_tenancy.map_index(args["index"], mine)
            if index is None:
                await _answer_locally(self, fid, pending, error=f"Tab {args['index']!r} not found")
                return None
            if action == "select":
                state(self).tab_active[ctx] = int(args["index"])
            args["index"] = index
            return _with_arguments(frame, args)
    if not mine:
        await _tab_call({"action": "new", **({"isolatedContext": ctx} if ctx else {})})
        state(self).tab_active[ctx] = 0
        return _with_arguments(frame, args) if name == tabs_tool else frame
    if current is None or current.context != ctx:
        active = min(state(self).tab_active.get(ctx, len(mine) - 1), len(mine) - 1)
        await _tab_call({"action": "select", "index": mine[active].index})
    return frame


def _tab_call_denial(self: Backend, pending: "_PendingRequest", tabs_tool: str) -> Optional[str]:
    """Why the caller may not call *tabs_tool* (policy ∩ profile), or ``None``. BLOCKING.

    The same decision an app callback gets (``app_call._governance_denial``),
    for the agent this caller's stub attached under. A stub that names no agent
    is governed by its session's surface profile; the check is never skipped.
    """
    from kiro_crew.mcp_gateway.app_call import _governance_denial

    return _governance_denial(
        self.pool_key.server_name,
        tabs_tool,
        pending.session_key,
        agent=self.agent_for_stub(pending.stub_uuid),
    )


def _audit_tab_call(
    self: Backend,
    pending: "_PendingRequest",
    tabs_tool: str,
    outcome: str,
    *,
    action: str = "",
    reason: str = "",
) -> None:
    """One SEL event per tab call the gateway makes (or refuses) for a caller."""
    from kiro_crew.sel import SecurityEventLog

    try:
        SecurityEventLog().log_api_access(
            caller=pending.session_key or "unknown",
            operation="mcp-gateway.tab-call",
            outcome=outcome,
            source="gateway",
            resources=(
                f"server={self.pool_key.server_name} tool={tabs_tool} "
                f"for={pending.tool_name} action={action or '-'} reason={reason or '-'}"
            ),
        )
    except Exception:  # pragma: no cover - audit must never break the call
        logger.debug("SEL audit of a gateway tab call failed", exc_info=True)


def _tab_context(self: Backend, session_key: str, *, claim: bool = False) -> str:
    """The browser context *session_key*'s tabs live in; ``""`` is the default one.

    The first caller keeps the default context, so a lone chat still uses the
    browser's persistent profile (its cookies and logins); every later caller
    gets its own isolated context. *claim* makes the first caller the owner.
    """
    st = state(self)
    if st.default_tab_owner is None and claim:
        st.default_tab_owner = session_key
    return "" if st.default_tab_owner == session_key else tab_tenancy.context_name(session_key)


def _with_arguments(frame: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    raw = frame.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {**frame, "params": {**params, "arguments": args}}


def scope_reply(self: Backend, pending: "_PendingRequest", msg: dict[str, Any]) -> dict[str, Any]:
    """A reply as the caller should read it: only its own browser tabs, numbered from 0."""
    if state(self).tab_tools is None or pending.method != "tools/call" or not pending.session_key:
        return msg
    tools = state(self).tab_tools
    if tools is not None and pending.tool_name == tools[0]:
        _note_tab_closed(self, pending, msg)
    return tab_tenancy.rewrite_reply(
        msg,
        _tab_context(self, pending.session_key),
        whole=tools is not None and pending.tool_name == tools[0],
    )


def _note_tab_closed(self: Backend, pending: "_PendingRequest", msg: dict[str, Any]) -> None:
    """After the caller closed one of its tabs, keep its saved tab on the same page.

    Its tabs after the closed one move down by one, so a saved tab past it does
    too; a closed saved tab leaves the one that took its place.
    """
    args = pending.tool_arguments or {}
    result = msg.get("result")
    if (
        args.get("action") != "close"
        or not isinstance(result, dict)
        or result.get("isError") is True
    ):
        return
    closed = args.get("index")
    if isinstance(closed, bool) or not isinstance(closed, (int, float)) or int(closed) != closed:
        return
    ctx = _tab_context(self, pending.session_key)
    active = state(self).tab_active.get(ctx)
    if active is not None and closed < active:
        state(self).tab_active[ctx] = active - 1


def note_refused(self: Backend, fid: str) -> bool:
    """Remember the forward id a rehandshake retry is about to replace; always True."""
    state(self).refused_fid = fid
    return True


def carry_refused_slot(self: Backend, pending: "_PendingRequest") -> None:
    """Move the refused call's serialized slot to its retry, before the retry is written."""
    fid, state(self).refused_fid = state(self).refused_fid, ""
    if fid:
        carry_call_slot(self, fid, pending)


def carry_call_slot(self: Backend, old_fid: str, pending: "_PendingRequest") -> None:
    """A rehandshake re-sent the call *old_fid*: its retry keeps the serialized slot."""
    gate = state(self).call_gate
    if gate is None or gate.in_flight != old_fid:
        return
    retry = next(
        (
            fid
            for fid, p in self._pending_requests.items()
            if p.stub_uuid == pending.stub_uuid
            and p.original_id == pending.original_id
            and p.method == pending.method
        ),
        None,
    )
    if retry is None:
        gate.release(old_fid)
    else:
        gate.rekey(old_fid, retry)


def gates_call(self: Backend, tool_name: str) -> bool:
    """Whether a ``tools/call`` of *tool_name* takes its turn at the gate."""
    st = state(self)
    return st.call_gate is not None and tool_name in st.gated_tools


def end_call(self: Backend, fid: str) -> None:
    """A reply for *fid* arrived: free the serialized slot it held, if any."""
    gate = state(self).call_gate
    if gate is not None:
        gate.release(fid)


def _schedule(self: Backend, coro: Any) -> None:
    """Run *coro* beside the caller, kept so it is not collected mid-flight."""
    task = asyncio.get_running_loop().create_task(coro)
    state(self).scope_tasks.add(task)
    task.add_done_callback(state(self).scope_tasks.discard)


def _real_stubs(self: Backend) -> list[str]:
    """The attached stubs that are chats, not the gateway's own internal ones."""
    return [s for s in self._stub_inboxes if not s.startswith(INTERNAL_STUB_PREFIXES)]


_MAX_SEEN_FOLDERS = 64


def _note_folder(self: Backend, session_key: str) -> None:
    """Add *session_key*'s folder to the folders this backend serves, once.

    At the bound, the oldest folder no attached chat is in makes room; only
    when every one is in use is the new folder left out, logged once per
    backend and counted (``folders_left_out``).
    """
    if not local_session(session_key):
        # Another person's folder never joins a shared server's roots.
        return
    folder = folder_scope.session_folder(session_key)
    st = state(self)
    seen = st.seen_folders
    if not folder or folder in seen:
        return
    if len(seen) >= _MAX_SEEN_FOLDERS:
        in_use = {
            folder_scope.session_folder(st.stub_sessions.get(s, "")) for s in _real_stubs(self)
        }
        idle = next((f for f in seen if f not in in_use), None)
        if idle is None:
            st.folders_left_out += 1
            if st.folders_left_out == 1:
                logger.warning(
                    "backend pid=%s: %d chat folders in use; a new one is not served",
                    self.pid,
                    len(seen),
                )
            return
        seen.remove(idle)
    seen.append(folder)


def _attached_union(self: Backend) -> Optional[list[str]]:
    """The roots to serve, or ``None`` when no folder is known yet.

    Every chat folder this backend has served stays until the backend ends. A
    chat's stub connection comes and goes during the session (a turn, a
    reconnect), so dropping a folder when its stub detaches would take it from
    a chat still open; and all chats belong to one local user, so a folder seen by
    one is fine for the others. shortcut: bounded at ``_MAX_SEEN_FOLDERS``;
    a folder past the bound is not served, revisit if one backend serves more.
    """
    st = state(self)
    for stub in _real_stubs(self):
        key = st.stub_sessions.get(stub)
        if key:
            _note_folder(self, key)
    # The server's own configured folders come first and stay: a roots answer
    # replaces the directories it was started with.
    own = st.launch_folders
    roots = own + [f for f in st.seen_folders if f not in own]
    return roots or None


async def _send_roots_changed_if_needed(self: Backend) -> None:
    st = state(self)
    if not st.roots_served and not (st.roots_pending and st.folder_confinable):
        return
    union = _attached_union(self)
    if union is None or union == state(self).roots_answer:
        return
    # Remembered as announced: the server re-asks and is answered this list.
    state(self).roots_answer = union
    try:
        await _write_json_line(self.stdin, _ROOTS_LIST_CHANGED)
    except (BrokenPipeError, ConnectionResetError, OSError):
        logger.debug("backend pid=%s: roots list_changed not delivered", self.pid)


async def note_caller(self: Backend, stub_uuid: str, caller: Optional[CallerContext]) -> None:
    """Remember which session *stub_uuid* speaks for; tell the server when its roots grew."""
    if not _adjustable(self):
        return
    st = state(self)
    if caller is None or not caller.session_key:
        # An unnamed caller (a warm-pool stub not claimed yet) is not local.
        st.stub_sessions.pop(stub_uuid, None)
        await _retire_if_union_shared(self, "an unnamed chat")
        return
    if (
        st.stub_sessions.get(stub_uuid) == caller.session_key
        and folder_scope.session_folder(caller.session_key) in st.seen_folders
        and not (st.roots_pending or st.roots_served)
    ):
        return
    st.stub_sessions[stub_uuid] = caller.session_key
    if not local_session(caller.session_key) and await _retire_if_union_shared(
        self, "a non-local chat"
    ):
        return
    _note_folder(self, caller.session_key)
    # Also re-checked for an unchanged caller: its folder may have arrived on a
    # later claim. The union comparison keeps this to one notice per change.
    await _send_roots_changed_if_needed(self)


async def _retire_if_union_shared(self: Backend, who: str) -> bool:
    """Retire a pooled backend whose roots hold chat folders; True when it did.

    A chat that may be another person's joined: the backend goes before that
    chat's first frame reaches it, and its replacement keeps roots per chat.
    """
    st = state(self)
    if self.exclusive_token or not (st.roots_served or st.roots_writing):
        return False
    reason = f"{who} joined a server serving chat-folder roots; recycling"
    logger.info("backend pid=%s: %s", self.pid, reason)
    self._dead_reason = self._dead_reason or reason
    await self._broadcast_backend_gone(reason)
    return True


def seed_caller_folder(self: Backend, caller: Optional[CallerContext]) -> None:
    """Serve *caller*'s folder from the start: a replacement is primed before its stub attaches.

    Without it the replacement's first ``roots/list``, asked during the
    replayed handshake, finds no folder and is declined.
    """
    if _adjustable(self) and caller is not None and caller.session_key:
        _note_folder(self, caller.session_key)


async def forget_stub(self: Backend, stub_uuid: str) -> None:
    """Drop *stub_uuid*'s session. Its folder stays served (``_attached_union``)."""
    state(self).stub_sessions.pop(stub_uuid, None)


def launch_folders(args: list[str], work_dir: str) -> list[str]:
    """The arguments that name an existing directory, made absolute. BLOCKING (stat)."""
    found: list[str] = []
    for arg in args:
        if not isinstance(arg, str) or not arg or arg.startswith("-") or len(arg) > 4096:
            continue
        path = os.path.normpath(os.path.join(work_dir or "", os.path.expanduser(arg)))
        if os.path.isabs(path) and os.path.isdir(path) and path not in found:
            found.append(path)
    return found


#: Whether every chat on this daemon belongs to one local user, so one backend
#: may serve several chats' folders as its roots: set only by an owner-verified
#: ``owner-port`` announce saying Shared Crew is off on the owning gateway
#: (``daemon.control._apply_owner_port``). Starts False and stays False for an
#: unowned daemon or an unverifiable sender: the roots stay per chat, as on base.
_SHARED_ROOTS = [False]


def set_shared_roots(allowed: bool) -> bool:
    """Record the owning gateway's answer; returns the previous one."""
    previous = _SHARED_ROOTS[0]
    _SHARED_ROOTS[0] = allowed is True
    return previous


def shared_roots() -> bool:
    """Whether this daemon may serve a union of chat folders as a backend's roots."""
    return _SHARED_ROOTS[0]


#: Session keys of the operator's own surfaces: a dashboard chat (and its
#: subagents) and the ``kirocrew chat`` REPL. Any other key, a messaging
#: channel, a cron, anything new or unreadable, may be another person's.
_LOCAL_SESSION_PREFIXES = ("dashboard:",)
_LOCAL_SESSION_KEYS = frozenset({"cli_chat"})


def local_session(session_key: Any) -> bool:
    """Whether *session_key* is one of the operator's own local surfaces."""
    return isinstance(session_key, str) and (
        session_key in _LOCAL_SESSION_KEYS or session_key.startswith(_LOCAL_SESSION_PREFIXES)
    )


def union_allowed(self: Backend) -> bool:
    """Whether this backend may serve several chats' folders as its roots now.

    Both must hold: the owner said Shared Crew is off, and every attached chat
    is a local one. A stub whose session is not known yet counts as not local.
    """
    if not shared_roots():
        return False
    st = state(self)
    return all(local_session(st.stub_sessions.get(stub)) for stub in _real_stubs(self))


def serves_shared_roots(self: Backend) -> bool:
    """Whether the gateway already answered this backend's roots with chat folders."""
    return state(self).roots_served


def note_launch(self: Backend, folders: list[str]) -> None:
    """Keep the directories the server was configured with (from :func:`launch_folders`)."""
    if _adjustable(self):
        state(self).launch_folders = list(folders)


def gone_before_send(reason: str) -> Exception:
    """A ``BackendGone`` for a frame that provably never left the gateway.

    The connection's respawn forwards such a request once to the replacement
    instead of failing it (``daemon.connection``); a frame that was written
    may have run, so its failure stands.
    """
    from kiro_crew.mcp_gateway.backend_wire import BackendGone

    exc = BackendGone(reason)
    exc.never_sent = True  # type: ignore[attr-defined]
    return exc


def never_sent(exc: BaseException) -> bool:
    """Whether *exc* says its frame never reached the server (``gone_before_send``)."""
    return getattr(exc, "never_sent", False) is True


def hold_unsent(self: Backend) -> dict[str, dict[str, Any]]:
    """Empty the call gate of a backend that is gone; the unsent calls by forward id."""
    gate = state(self).call_gate
    return {} if gate is None else dict(gate.fail_all())


def keep_for_resend(self: Backend, stub_uuid: str, frame: dict[str, Any]) -> bool:
    """Hold *frame*, never written to the server, for *stub_uuid*'s connection.

    False for the gateway's own stubs (an app callback): no connection reads
    them back, so their caller gets the ``backend gone`` error at once.
    """
    if stub_uuid.startswith(INTERNAL_STUB_PREFIXES):
        return False
    state(self).resend.setdefault(stub_uuid, []).append(frame)
    return True


def wake_resenders(self: Backend) -> None:
    """Wake every connection waiting on this backend (``gone_event``)."""
    state(self).gone.set()


def gone_event(self: Backend) -> Optional[asyncio.Event]:
    """Set when this backend is gone; ``None`` when it has no gate, so nothing is held."""
    st = state(self)
    return st.gone if st.call_gate is not None else None


def cancelled_ids(lines: Iterable[bytes]) -> set[Any]:
    """Request ids the stub cancelled in *lines* (frames read but not yet handled)."""
    found: set[Any] = set()
    for line in lines:
        msg = parse_json_object_line(line)
        params = msg.get("params") if msg is not None else None
        if (
            msg is not None
            and msg.get("method") == "notifications/cancelled"
            and isinstance(params, dict)
            and isinstance(params.get("requestId"), (str, int))
        ):
            found.add(params["requestId"])
    return found


def names_one_of(request_id: Any, ids: set[Any]) -> bool:
    """Whether *request_id* (stub-authored JSON) is one of *ids*; never raises."""
    return isinstance(request_id, (str, int)) and request_id in ids


def take_resend(
    self: Backend, stub_uuid: str, consumed: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    """The calls held for *stub_uuid* (``keep_for_resend``), in the order they were sent.

    *consumed* is a frame of the stub's that will not be forwarded: when it
    is a cancellation, the held call it names is dropped, never resent.
    """
    held = state(self).resend.pop(stub_uuid, [])
    params = consumed.get("params") if isinstance(consumed, dict) else None
    if (
        not isinstance(consumed, dict)
        or consumed.get("method") != "notifications/cancelled"
        or not isinstance(params, dict)
        or "requestId" not in params
    ):
        return held
    return [f for f in held if f.get("id") != params["requestId"]]


DISCOVER_METHOD = "server/discover"


async def answer_discover(self: Backend, stub_uuid: str, msg: dict[str, Any]) -> bool:
    """Answer a stub's ``server/discover`` with "method not found"; True when answered.

    The handshake-free protocol revision has no ``roots/list``, so a
    server that needs the chat's folder (server-filesystem given no folder) can
    never get it there. The harness falls back to ``initialize`` on this
    answer, the same one Kiro Crew's own servers give, and the gateway then
    serves roots. Kiro Crew's own servers are not adjusted.
    """
    if not _adjustable(self) or msg.get("method") != DISCOVER_METHOD or "id" not in msg:
        return False
    await self._reply_locally(
        stub_uuid,
        msg.get("id"),
        error={"code": -32601, "message": "Method not found"},
    )
    return True


def offer_roots(self: Backend, msg: dict[str, Any]) -> dict[str, Any]:
    """The upstream ``initialize`` with the roots capability offered; the gateway answers roots/list.

    A harness may not offer roots itself, and a server that needs them (a
    filesystem server given no folder) then has nothing to work on and exits.
    Whether the harness offered them is kept: a roots/list the gateway declines
    goes to the harness only when it did.
    """
    if not _adjustable(self) or not union_allowed(self):
        return msg
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg
    caps = params.get("capabilities")
    caps = dict(caps) if isinstance(caps, dict) else {}
    st = state(self)
    st.harness_offers_roots = isinstance(caps.get("roots"), dict)
    if st.harness_offers_roots and caps["roots"].get("listChanged") is True:
        return msg
    caps["roots"] = {**(caps.get("roots") or {}), "listChanged": True}
    return {**msg, "params": {**params, "capabilities": caps}}


def scope_forward(
    self: Backend, stub_uuid: str, msg: dict[str, Any], caller: Optional[CallerContext]
) -> dict[str, Any]:
    """*msg* adjusted for *caller*; the forward choke point's single hook. Never raises."""
    if not _adjustable(self):
        return msg
    if caller is None or msg.get("method") != "tools/call":
        return msg
    params = msg.get("params")
    if not isinstance(params, dict) or state(self).tool_scopes is None:
        return msg
    name = params.get("name")
    scopes = state(self).tool_scopes
    scope = scopes.get(name) if scopes is not None and isinstance(name, str) else None
    folder = folder_scope.session_folder(caller.session_key)
    arguments = params.get("arguments")
    scoped = folder_scope.scope_arguments(
        arguments, scope, folder, folder_user=_reads_paths_against_roots(self)
    )
    if scoped is arguments:
        return msg
    out = dict(msg)
    out["params"] = {**params, "arguments": scoped}
    return out


def _reads_paths_against_roots(self: Backend) -> bool:
    """Whether a caller's relative path means its own folder on this server.

    Once the gateway answered roots; also from the moment the server asked for
    them, since the answer waits on the tool list and a call may arrive first.
    """
    st = state(self)
    return union_allowed(self) and (st.roots_served or (st.roots_asked and st.folder_confinable))


async def _learn_tool_scopes(self: Backend) -> None:
    """Ask the backend for its tool list on an internal stub; the answer is harvested."""
    if state(self).tool_scopes is None:
        await self.probe_tool_surface()


def may_serve_roots(self: Backend) -> bool:
    """Cheap pre-check: could :func:`serve_roots` answer now? (No IO, no await.)"""
    return (
        _adjustable(self)
        and union_allowed(self)
        and _attached_union(self) is not None
        and (state(self).tool_scopes is None or state(self).folder_confinable)
    )


def claim_roots_request(self: Backend, msg: dict[str, Any]) -> bool:
    """Take a server ``roots/list`` to answer it from the gateway. False: route it as before.

    The decision may need the tool list, whose answer the stdout pump has to
    read, so it runs as a task beside the pump; a request it declines is handed
    back to the pump's routing once, marked so it is not claimed again.
    """
    st = state(self)
    if not union_allowed(self):
        # Shared Crew, or a chat that is not local: the request takes the
        # base route, which keeps this server one copy per chat.
        return False
    msg_id = msg.get("id")
    if msg_id in st.roots_routed:
        st.roots_routed.discard(msg_id)
        return False
    if not may_serve_roots(self) and st.harness_offers_roots:
        return False
    st.roots_asked = True
    task = asyncio.get_running_loop().create_task(_serve_or_route_roots(self, msg))
    st.scope_tasks.add(task)
    task.add_done_callback(st.scope_tasks.discard)
    return True


async def _serve_or_route_roots(self: Backend, msg: dict[str, Any]) -> None:
    try:
        if await serve_roots(self, msg):
            return
    except Exception:  # pragma: no cover - defensive; fall back to routing
        logger.warning("backend pid=%s: serving roots/list raised", self.pid, exc_info=True)
    if await decline_offered_roots(self, msg):
        return
    state(self).roots_routed.add(msg.get("id"))
    await self._route_backend_message(msg, msg.get("id"), msg.get("method"))


async def decline_offered_roots(self: Backend, msg: dict[str, Any]) -> bool:
    """Answer a roots/list the gateway cannot serve when only the gateway offered roots.

    The harness never offered roots, so routing the request there would only
    reach a client that cannot answer it, and record evidence the server would
    never have produced on this harness. The server gets "not available now"
    and is told ``roots/list_changed`` once every caller's folder is known.
    """
    st = state(self)
    if not _adjustable(self) or st.harness_offers_roots:
        return False
    if _attached_union(self) is None:
        st.roots_pending = True
    reply = {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32601, "message": "roots are not available for this server yet"},
    }
    try:
        await _write_json_line(self.stdin, reply)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # the pump reports the dead backend
    return True


async def serve_roots(self: Backend, msg: dict[str, Any]) -> bool:
    """Answer a server ``roots/list`` with the folders it serves. False: not served.

    Served only when every tool can be scoped and at least one folder is
    known; otherwise the caller of this keeps the earlier routing (deliver to
    the one stub, or recycle a shared backend).
    """
    if not may_serve_roots(self):
        return False
    if state(self).tool_scopes is None:
        await _learn_tool_scopes(self)
    if not state(self).folder_confinable:
        return False
    # Read again: a caller may have attached or left while the list was fetched,
    # and a chat that is not local takes the union away.
    union = _attached_union(self)
    if union is None or not union_allowed(self):
        return False
    reply = {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {"roots": [{"uri": folder_scope.file_uri(f)} for f in union]},
    }
    # Marked before the write: a chat that is not local attaching while it
    # drains then retires this backend (``note_caller``).
    state(self).roots_writing = True
    try:
        await _write_json_line(self.stdin, reply)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return True  # the pump reports the dead backend; nothing to route
    finally:
        state(self).roots_writing = False
    first = not state(self).roots_served
    state(self).roots_served = True
    state(self).roots_pending = False
    state(self).roots_answer = union
    if first:
        key = self.pool_key
        identity = hazards.launch_identity(
            key.command_args_hash, key.effective_env_hash, key.binary_version
        )
        # The gateway answers this server's roots itself, so the request is not
        # evidence that it must run once per chat.
        hazards.retract_observed(key.server_name, hazards.HAZARD_ROOTS_REQUESTED, identity)
        logger.info(
            "backend pid=%s: gateway answers roots/list for %r with %d folder(s)",
            self.pid,
            key.server_name,
            len(union),
        )
    # A caller that joined while the reply was being written found the roots
    # not served yet and announced nothing: announce its folder now.
    await _send_roots_changed_if_needed(self)
    return True
