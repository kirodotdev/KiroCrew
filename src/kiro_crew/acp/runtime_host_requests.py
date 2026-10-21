"""Requests the agent sends this host that no session turn answers.

Permission requests for unregistered sessions (bounded off-loop answers and their audit), the
host credential callback, MCP sign-in and re-attempt, the consent URL, the engine's MCP
secret store, and ownerless requests.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_answer_tasks``, ``_audit_tasks``, ``_kas_host_auth_logged``,
``_mcp_reattempts``, ``_mcp_sign_in``.

Instance fields read: ``_agent``, ``_answer_cap_wait_secs``, ``_dead``,
``_max_answer_tasks``, ``_pid``, ``_session_queues``, ``_subagent_owner``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_secret_storage
from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp._dispatch import reject_option_id as _reject_option_id
from kiro_crew.acp.client import _is_safe_oauth_url, _loggable_request_id
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError
from kiro_crew.acp.kas_transport import (
    KAS_AUTH_CALLBACK_ERROR_CODE,
    METHOD_KAS_AUTH_GET_ACCESS_TOKEN,
)
from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text
from kiro_crew.acp.runtime_wire import AcpRuntimeStdinStalled
from kiro_crew.acp.session_handle import AcpRuntimeDead
from kiro_crew.acp.types import (
    METHOD_KAS_MCP_RESET_SERVER,
    METHOD_MCP_OAUTH_REQUEST,
    JsonRpcMessage,
)
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


# JSON-RPC 2.0 "Method not found" — the reader loop answers an ownerless
# server→client request with this itself (see _answer_ownerless_request);
# mirrors the private constant AcpClient keeps for its own dispatch sites.
_JSONRPC_METHOD_NOT_FOUND = -32601


# How long an MCP sign-in may hold its slot. The engine answers the reset when
# its connect attempt ends, which the server's connect timeout bounds: 60 s by
# default and at most 600 s. This only frees the slot if no answer ever comes,
# so it sits past the longest attempt: a slot freed while the engine can still
# send that attempt's link would hand the link to the next sign-in.
_MCP_SIGN_IN_TIMEOUT = 630.0
# How long a credential re-attempt (a reset without startOAuth) may hold off a
# sign-in. It opens no consent URL, so the slot does not have to outlast the
# longest connect: past the engine's default 60 s connect timeout it is freed,
# and a URL the engine still sends for it is refused because no sign-in is in
# flight, or would reach a sign-in started since, which shows its own server.
_MCP_REATTEMPT_TIMEOUT = 75.0
# JSON-RPC server-error code for a consent URL Crew did not show anyone.
_MCP_URL_NOT_OPENED = -32000
# JSON-RPC server-error code for a ``_kiro/secret/*`` request Crew refused.
_MCP_SECRET_REFUSED = -32000


# The default: the sessionId is in no roster this runtime remembers, and no
# roster it remembers was truncated either, so the backend genuinely never
# announced this child to us.
_UNREGISTERED_REJECT_REASON = "unregistered_session_auto_reject"


async def _wait_for_answer_capacity(
    self: AcpRuntime,
    msg: JsonRpcMessage,
    *,
    request_kind: str,
    session_id: str = "",
    audit_reason: str | None = None,
) -> bool:
    """Wait briefly for shared answer capacity or condemn a wedged pipe.

    Server-to-client requests require a response, so overflowing answers
    cannot take the notification counted-drop path. A responsive backend
    may fill the set with already-buffered requests before completed-task
    callbacks run; one completion admits the current request. No
    completion within the bound means writes are wedged, so marking the
    runtime dead resolves every pending wait instead of leaving the remote
    requester unanswered indefinitely.
    """

    def _deny() -> bool:
        """Refuse admission, recording the decision first.

        A refusal that reaches a caller which had already been admitted to
        wait must leave a SEL record, or a permission decision that denied a
        real tool invocation is indistinguishable from one never made.
        """
        if audit_reason is not None:
            self._audit_denied_off_loop(msg, session_id, audit_reason)
        return False

    # Deliberately NOT audited: on an already-dead runtime a flooding
    # backend's frames are gated out here, and auditing each one would grow
    # audit tasks without bound — the very failure the cap prevents.
    if self._dead:
        return False
    if len(self._answer_tasks) < self._max_answer_tasks:
        return True

    done, _pending = await asyncio.wait(
        set(self._answer_tasks),
        timeout=self._answer_cap_wait_secs,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if done:
        # asyncio schedules task callbacks separately from waking waiters.
        # Remove completed entries here so admitting the replacement never
        # transiently exceeds the shared cap; the callbacks remain an
        # idempotent cleanup backstop.
        self._answer_tasks.difference_update(done)
        if self._dead:
            # A concurrent waiter condemned the runtime while this one was
            # parked: capacity freed but admission still fails, so this
            # refusal owes an audit like any other.
            return _deny()
        return True

    logger.error(
        "answer-task cap (%d) reached at %s request id=%s%s and no "
        "in-flight answer completed in %gs — backend is flooding frames "
        "while not reading stdin; marking runtime dead so every pending "
        "wait resolves",
        self._max_answer_tasks,
        request_kind,
        _loggable_request_id(msg.id),
        f" for session {_loggable_request_id(session_id)}" if session_id else "",
        self._answer_cap_wait_secs,
    )
    # Audit before condemning the runtime, so the record for this decision
    # cannot race the wait-resolution _mark_dead triggers.
    refusal = _deny()
    self._mark_dead(
        f"{request_kind}-answer task cap reached (backend not reading)", stdin_stalled=True
    )
    return refusal


async def _spawn_answer_task(
    self: AcpRuntime,
    msg: JsonRpcMessage,
    session_id: str,
    *,
    reason: str = _UNREGISTERED_REJECT_REASON,
) -> None:
    """Spawn a bounded off-loop auto-answer for an unroutable permission request.

    Off-loop because the answer's ``send_response`` can block on stdin
    ``drain()`` against a backend that is not reading — awaiting inline
    would freeze the shared reader (every session's demux) on one hostile
    or wedged backend. Bounded because each blocked task is retained in
    ``_answer_tasks``: a backend that floods permission frames while never
    reading stdin would otherwise grow that set until the gateway OOMs.
    At capacity the shared admission wait either observes progress or
    marks the runtime dead so the requester cannot remain unanswered.
    """
    if not await self._wait_for_answer_capacity(
        msg,
        request_kind="permission",
        session_id=session_id,
        audit_reason="answer_task_cap_runtime_dead",
    ):
        return
    _t = asyncio.ensure_future(self._answer_unroutable_permission(msg, session_id, reason=reason))
    self._answer_tasks.add(_t)
    _t.add_done_callback(self._answer_tasks.discard)


async def _answer_unroutable_permission(
    self: AcpRuntime,
    msg: JsonRpcMessage,
    session_id: str,
    *,
    reason: str = _UNREGISTERED_REJECT_REASON,
) -> None:
    """Answer a permission REQUEST for a session with no registered queue.

    The ACP contract for a server→client request is that the client always
    replies; kiro-cli's own TUI answers even unowned-session permission
    requests (``cancelled``) rather than dropping them. Auto-reject is
    deliberate and conservative: never auto-approve here — no policy engine
    has seen this tool call, and an approve would grant an invisible
    escalation. Per-frame WARNING is safe (unlike the drop counter's flood
    case): each request corresponds to one pending tool approval and the
    backend cannot re-emit it without a new turn.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    option_id = _reject_option_id(params)
    if option_id is not None:
        result = {"outcome": {"outcome": "selected", "optionId": option_id}}
    else:
        result = {"outcome": {"outcome": "cancelled"}}
    tool_call = params.get("toolCall")
    raw_title = tool_call.get("title") if isinstance(tool_call, dict) else None
    # The title is backend/LLM-authored and may embed a credential-bearing
    # command line — redact BEFORE truncating (truncation first could clip
    # a secret mid-token so the redaction patterns cannot match it, leaking
    # a credential prefix into the logs).
    # Bound the redaction input (backend-controlled) BEFORE the regex
    # passes, generously above the display cap so a clipped secret
    # cannot straddle the boundary the display truncation makes.
    title = redact_text(str(raw_title)[:4096])[:120] if raw_title else "<unknown>"
    logger.warning(
        "auto-rejected permission request id=%s for session %s "
        "(tool: %s, reason: %s): no surface on this client can answer it "
        "right now; answering with %s so the backend subagent gets a tool "
        "error instead of hanging",
        _loggable_request_id(msg.id),
        _loggable_request_id(session_id),
        title,
        reason,
        result["outcome"]["outcome"],
    )
    try:
        # Bounded send: an answer that cannot be written within the
        # timeout means the backend is not reading its stdin at all —
        # the pipe is wedged, and every further frame from it would
        # stack another blocked task (the OOM vector). Marking the
        # runtime dead resolves EVERY pending wait by teardown, so no
        # request is left unanswered and nothing accumulates. This is
        # the outer guard; send_response's own drain bound fires first
        # on a paused writer and surfaces as AcpRuntimeDead below.
        await asyncio.wait_for(self.send_response(msg.id, result), timeout=30.0)
    except asyncio.TimeoutError:
        logger.error(
            "answer for permission request id=%s could not be written in "
            "30s — backend not reading stdin; marking runtime dead",
            _loggable_request_id(msg.id),
        )
        # Audit BEFORE returning: the denial DECISION was made even
        # though delivery failed — mandatory SEL coverage applies to
        # every decision, not just successfully delivered ones.
        self._audit_denied_off_loop(
            msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
        )
        self._mark_dead("permission-answer write stalled (backend not reading)", stdin_stalled=True)
        return
    except AcpRuntimeStdinStalled:
        # send_response's own no-progress bound fired ahead of the outer
        # guard and already marked the runtime dead; same disposition, same
        # audit reason, so the record does not depend on which bound won.
        self._audit_denied_off_loop(
            msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
        )
        return
    except AcpRuntimeDead:
        # Runtime died mid-answer; the backend's wait dies with it.
        self._audit_denied_off_loop(
            msg, session_id, f"{reason}:runtime_dead_mid_answer", title=title
        )
        return
    except Exception:
        # This coroutine runs as a RETAINED TASK off the reader loop, so
        # an unexpected send failure would otherwise be swallowed with
        # the task — the child never gets an answer and waits on a
        # stranded oneshot, the exact hang this path exists to prevent.
        # A response write that fails for any reason other than the
        # already-handled dead-runtime case means the pipe cannot be
        # trusted: log and mark the runtime dead so the child's wait
        # dies with the process instead of hanging invisibly.
        logger.exception(
            "failed to answer unroutable permission request id=%s — "
            "marking runtime dead so the requester cannot hang",
            _loggable_request_id(msg.id),
        )
        self._audit_denied_off_loop(
            msg, session_id, f"{reason}:send_failed_runtime_dead", title=title
        )
        self._mark_dead("unroutable-permission answer failed")
        return
    # Every permission decision is SEL-audited (repo convention; see
    # _audit_denied_off_loop for the off-loop/lazy-import rationale).
    self._audit_denied_off_loop(msg, session_id, reason, title=title)


def _audit_denied_off_loop(
    self: AcpRuntime,
    msg: JsonRpcMessage,
    session_id: str,
    reason: str,
    *,
    title: str | None = None,
) -> None:
    """SEL-audit a denied permission decision without blocking the caller.

    Every permission decision leaves a SEL record (repo convention; the
    dashboard deny path does the same). Off the calling task because
    ``sel()`` may do blocking filesystem work on first use (e.g. Windows
    ACLs). The decision is already made, so an audit failure must not
    undo or delay it; the failure is swallowed after logging. Lazy
    import: a module-level import of ``kiro_crew.sel`` would be circular
    (same pattern as sandbox.py).
    """
    if title is None:
        _params = msg.params if isinstance(msg.params, dict) else {}
        _tc = _params.get("toolCall")
        _raw = _tc.get("title") if isinstance(_tc, dict) else None
        title = redact_text(str(_raw)[:4096])[:120] if _raw else "<unknown>"
    # Hang-resilience series: every runtime-side denial funnels through
    # here, so one emit covers unroutable/between-turns/cap/send-failure
    # denials. ``reason`` is the closed SEL enum (low-cardinality).
    emit_counter(
        CHILD_PERMISSION_DENIED,
        {"surface": "runtime", "reason": reason},
    )
    request_id = msg.id if isinstance(msg.id, (str, int)) else ""
    # SNAPSHOT the attribution key NOW: the audit closure runs later on a
    # worker thread, and `_subagent_owner` is mutable (unregister/session
    # swap). Reading it at execution time would write the wrong owner —
    # or the bare PID — into an immutable SEL row.
    session_key = f"acp:{self._subagent_owner or self._pid}:{session_id}"

    def _audit() -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key,
                agent="kirocrew",
                source="acp_runtime",
                tool_name=title,
                outcome="denied",
                request_id=request_id,
                error=reason,
            )
        except Exception:
            logger.exception("SEL audit for auto-rejected permission failed")

    audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
    # Retain the task so it cannot be garbage-collected mid-flight; the
    # done callback drops the reference and surfaces nothing (audit
    # failures are already logged inside _audit).
    self._audit_tasks.add(audit_task)
    audit_task.add_done_callback(self._audit_tasks.discard)


async def _answer_get_access_token(self: AcpRuntime, request_id: int | str) -> None:
    """Answer this host's credential callback, by its own method name.

    Kept under this name because it is the shape callers and tests ask for;
    the method the host actually sent is resolved from its harness.
    """
    await self._answer_host_request(request_id, METHOD_KAS_AUTH_GET_ACCESS_TOKEN)


async def _answer_host_request(self: AcpRuntime, request_id: int | str, method: str) -> None:
    """Answer a connection-level request this host's harness claims.

    Runs OFF the reader loop. The response is built by the HARNESS that named
    ``method`` in ``host_answered_methods``, never by a hardcoded answerer:
    the guard upstream already accepts any method a host claims, so a fixed
    answerer would hand one host's credential to another host that happened to
    claim a different method — the mistaken-identity failure this layer exists
    to prevent. For KAS that answer resolves and refreshes under the
    cross-process lock and withholds the refresh token; it is never cached
    here and never logged.

    On any failure the host is sent a JSON-RPC error, which it treats as an
    expired credential and turns into its sign-in prompt, rather than being
    left to hang on the callback. The INFO line is the positive "host is
    drawing its credential from Crew" signal, logged once per runtime and
    only AFTER a response was actually written, so a callback that failed
    never counts as served.
    """
    try:
        result = await self._harness.answer_request(method)
    except HostAuthCallbackError as exc:
        # str(exc) is token-free by construction (see kas_host_auth).
        logger.warning("KAS auth callback failed: %s", exc)
        try:
            await self.send_error(request_id, KAS_AUTH_CALLBACK_ERROR_CODE, str(exc))
        except AcpRuntimeDead:
            pass
        return
    try:
        await self.send_response(request_id, result)
    except AcpRuntimeDead:
        # Process gone before the answer could be written; nothing to do.
        return
    if not self._kas_host_auth_logged:
        self._kas_host_auth_logged = True
        logger.info(
            "KAS auth callback served from Crew vault — agent=%s (PID %s)",
            self._agent or "<none>",
            self._pid,
        )
    else:
        logger.debug(
            "KAS auth callback served from Crew vault — agent=%s (PID %s)",
            self._agent or "<none>",
            self._pid,
        )


def mcp_sign_in_holds(self: AcpRuntime, session_id: str, server_name: str) -> bool:
    """Whether the runtime holds this session and server's sign-in slot."""
    return self._mcp_sign_in == (session_id, server_name)


def begin_mcp_sign_in(self: AcpRuntime, session_id: str, server_name: str) -> bool:
    """Start an explicit OAuth sign-in for one MCP server of one session.

    Sends ``_kiro/mcp/resetServer`` with ``startOAuth`` in the background: the
    engine answers only when the connect attempt ends, which is after the user
    signs in or after the server's connect timeout. The consent URL arrives
    meanwhile as ``_kiro/openExternalUrl`` and is delivered to *session_id*.

    Returns False, starting nothing, when the host sends no consent URL, the
    session is not registered, or another sign-in is in flight: the URL
    request names neither session nor server, so a second concurrent sign-in
    would make its link unattributable. The caller offers again later.

    The slot is held until the reset is answered, even after its session
    unregisters: that session's link can still arrive, and it is refused
    because the session's queue is gone rather than handed to a newer
    sign-in.
    """
    if not self._harness.opens_external_urls or self._dead:
        return False
    if self._mcp_sign_in is not None or session_id not in self._session_queues:
        return False
    if self._mcp_reattempts:
        # A consent URL the engine sent for a re-attempt would be handed
        # to this sign-in's server; wait until the re-attempts are answered.
        return False
    self._mcp_sign_in = (session_id, server_name)
    task = asyncio.ensure_future(self._run_mcp_sign_in(session_id, server_name))
    self._answer_tasks.add(task)
    task.add_done_callback(self._answer_tasks.discard)
    return True


async def _run_mcp_sign_in(self: AcpRuntime, session_id: str, server_name: str) -> None:
    """Hold the sign-in slot until the engine's reset answer arrives."""
    entry = (session_id, server_name)
    try:
        await self._send_and_await(
            METHOD_KAS_MCP_RESET_SERVER,
            {"sessionId": session_id, "serverName": server_name, "startOAuth": True},
            timeout=_MCP_SIGN_IN_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 -- the next offer retries
        logger.info(
            "MCP sign-in for %s ended without an answer: %s",
            sanitize_sink_text(server_name, NAME_CAP),
            type(exc).__name__,
        )
    finally:
        if self._mcp_sign_in == entry:
            self._mcp_sign_in = None


def begin_mcp_reattempt(self: AcpRuntime, session_id: str, server_name: str) -> bool:
    """Ask the engine to connect a failed MCP server of one session again.

    For a server that failed to start for want of a credential that may
    exist now. Sends ``_kiro/mcp/resetServer`` with ``startOAuth``
    false in the background; the server's next ``_kiro/mcp/status`` entry
    reports whether it connected. The host that takes this request is the
    one that sends consent URLs, so the same capability gates both.

    Returns False, starting nothing, on a host without the request, when
    the session is not registered, while an OAuth sign-in holds the slot
    (a consent URL this reset produced would be handed to that sign-in's
    server), or while a re-attempt of the same server is still unanswered.
    Re-attempts of different servers may run together: none of them opens
    a consent URL, and one that arrives with no sign-in in flight is
    refused.
    """
    if not self.reattempts_mcp_servers:
        return False
    entry = (session_id, server_name)
    if self._mcp_sign_in is not None or session_id not in self._session_queues:
        return False
    if entry in self._mcp_reattempts:
        return False
    self._mcp_reattempts.add(entry)
    task = asyncio.ensure_future(self._run_mcp_reattempt(session_id, server_name))
    self._answer_tasks.add(task)
    task.add_done_callback(self._answer_tasks.discard)
    return True


async def _run_mcp_reattempt(self: AcpRuntime, session_id: str, server_name: str) -> None:
    """Hold the re-attempt until the engine's reset answer arrives."""
    entry = (session_id, server_name)
    try:
        await self._send_and_await(
            METHOD_KAS_MCP_RESET_SERVER,
            {"sessionId": session_id, "serverName": server_name, "startOAuth": False},
            timeout=_MCP_REATTEMPT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 -- the next turn may try again
        logger.info(
            "MCP re-attempt for %s ended without an answer: %s",
            sanitize_sink_text(server_name, NAME_CAP),
            type(exc).__name__,
        )
    finally:
        self._mcp_reattempts.discard(entry)


async def _answer_open_external_url(self: AcpRuntime, request_id: int | str, params: Any) -> None:
    """Deliver an MCP consent URL to the session whose sign-in is in flight.

    The URL becomes an ordinary ``_kiro.dev/mcp/oauth_request`` frame on that
    session's queue, so it takes the same safety checks and banner as the
    kiro-cli engine's own request. It is refused with a JSON-RPC error when
    no sign-in is in flight (the link cannot be attributed to a server), when
    the owning session is gone, or when the URL is not http(s), which the
    banner would refuse. The engine raises on that error, so its connect
    attempt ends at once instead of waiting for a callback no user was shown.
    """
    url = params.get("url") if isinstance(params, dict) else None
    entry = self._mcp_sign_in
    queue = self._session_queues.get(entry[0]) if entry is not None else None
    refusal = ""
    if entry is None:
        refusal = "no MCP sign-in is in flight"
    elif queue is None:
        refusal = "the MCP sign-in's session has left"
    elif not isinstance(url, str) or not _is_safe_oauth_url(url):
        refusal = "the consent URL is not an http(s) URL"
    else:
        session_id, server_name = entry
        await queue.put(
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": session_id, "serverName": server_name, "oauthUrl": url},
            )
        )
    try:
        if refusal:
            logger.info("MCP consent URL not opened: %s", refusal)
            await self.send_error(request_id, _MCP_URL_NOT_OPENED, refusal)
        else:
            await self.send_response(request_id, {"success": True})
    except AcpRuntimeDead:
        return


async def _answer_secret_request(
    self: AcpRuntime,
    server: kas_secret_storage.SecretStorageServer,
    request_id: int | str,
    method: str,
    params: Any,
) -> None:
    """Answer one ``_kiro/secret/*`` request from *server*, off the reader loop.

    A refusal goes back as a JSON-RPC error whose message names only the
    category; the request's value is never logged.
    """
    try:
        result = await server.answer(method, params)
    except kas_secret_storage.SecretStorageRefused as exc:
        logger.info("MCP grant store request refused: %s", exc)
        try:
            await self.send_error(request_id, _MCP_SECRET_REFUSED, str(exc))
        except AcpRuntimeDead:
            pass
        return
    try:
        await self.send_response(request_id, result)
    except AcpRuntimeDead:
        return


async def _answer_ownerless_request(self: AcpRuntime, request_id: int | str, method: str) -> None:
    """Answer a server→client request that names no session with -32601.

    Runs OFF the reader loop (same shape as the KAS auth callback) so a
    stalled stdin drain cannot block stdout demux for every multiplexed
    session. The routed case — an unknown request WITH a sessionId — is
    deliberately not handled here: it is delivered to that session's queue
    and answered once by its dispatch loop (``server_request_unknown``).
    """
    logger.debug(
        "Ownerless server request answered -32601 — method=%s id=%s",
        _loggable_request_id(method),
        _loggable_request_id(request_id),
    )
    try:
        await self.send_error(request_id, _JSONRPC_METHOD_NOT_FOUND, "Method not found")
    except AcpRuntimeDead:
        pass
