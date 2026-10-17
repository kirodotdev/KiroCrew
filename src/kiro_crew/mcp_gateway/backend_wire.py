"""The JSON-RPC wire vocabulary a ``Backend`` and its collaborators share.

The bounded line writer every frame to a backend's stdin goes through, the pending-request
record that maps a gateway-minted id back to the stub that asked, :class:`BackendGone`, the
stub-id prefixes that mark a request as the gateway's own, the server-error code, the caller
and tenant ``_meta`` blocks the gateway strips from and injects into every forwarded frame
(its trust boundary), and the log-safe rendering of the identifiers and error text a server
sends back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.mcp_caller import (
    CALLER_META_KEY,
    TENANT_META_KEY,
    CallerContext,
    build_caller_meta,
    build_tenant_meta,
)
from kiro_crew.security import redact

#: Stub-uuid prefix for a gateway-internal ``tools/list`` asked on a backend's
#: own behalf (see :meth:`Backend.probe_tool_surface`).
TOOL_SURFACE_STUB_PREFIX = "__tool_surface__"

#: Stub-uuid prefixes that mark a request as the gateway's own rather than a
#: session's. Neither the MCP Apps render path nor the model-visibility filter
#: applies to these: they carry no model to protect and no app to render for.
#: ``str.startswith`` takes the tuple directly, so adding a prefix here is the
#: whole registration.
INTERNAL_STUB_PREFIXES: tuple[str, ...] = ("__app_call__", TOOL_SURFACE_STUB_PREFIX)


# Upper bound on a single ``_write_json_line`` drain (writing a forwarded
# request to a backend's stdin). A backend that has stopped reading its stdin
# must not hang the forwarding coroutine — and the shared heartbeat sweeper —
# forever. On timeout the write raises a pipe error so the caller recycles the
# wedged backend. Mirrors gatewayd's bounded reply drain.
_WRITE_DRAIN_TIMEOUT_SECS = 30.0


# JSON-RPC error code for a gateway-refused request (server-defined range).
_JSONRPC_SERVER_ERROR = -32000


class BackendGone(RuntimeError):
    """Raised when a caller tries to forward into a backend that is dead
    or has lost its stdin pipe. The connection handler catches this and
    emits a clean JSON-RPC error to the originating stub."""


# Named by the path callers import it from: an error chain and a traceback render a
# raised class as ``module.qualname``, so this module's name must not leak into them.
BackendGone.__module__ = "kiro_crew.mcp_gateway.backend"


@dataclass
class _PendingRequest:
    """Tracks an in-flight request so the stdout pump can restore the
    stub's original JSON-RPC id on the response. ``stub_uuid`` can also
    be the sentinel ``"__init__"`` for the gateway's upstream initialize
    request — that case is handled separately in :meth:`Backend._route_backend_line`.

    ``t_start_ms`` is the monotonic clock at forward time. The stdout
    pump uses it to compute an authoritative gateway-e2e duration for
    every completed request — the only point where we can claim "this is
    MCP gateway wall time" without LLM inference mixed in.
    """

    stub_uuid: str
    original_id: Any
    method: str
    t_start_ms: float = 0.0
    # Monotonic ms of the last in-flight report that named this request
    # (``mcp_shared.INFLIGHT_NOTIFICATION``); 0 when never reported. The wedge
    # detector ages a request from this rather than from ``t_start_ms``, so a
    # parked call that keeps reporting never reads as hung.
    t_progress_ms: float = 0.0
    # A stub forwarded ``notifications/cancelled`` for this request. The
    # backend answers a cancelled call with no response, so the wedge detector
    # skips it; it stays in the table for notification attribution and for the
    # last-stub recycle check.
    cancelled: bool = False
    # The request's ``params._meta.progressToken`` if it set one, so a
    # server-emitted ``notifications/progress`` can be routed back to the
    # owning stub instead of broadcast across co-pooled tenants.
    progress_token: Any = None
    # MCP Apps: captured at forward time so the response-interception path can
    # populate the spool record without the original request params (which are
    # not otherwise retained). ``session_key`` is the authoritative caller id;
    # ``tool_name`` is ``params.name`` for a tools/call. Both empty for
    # non-tools/call requests and gateway-internal sentinels.
    session_key: str = ""
    tool_name: str = ""
    # True when a ``tools/list`` request carried a pagination ``cursor``, so its
    # response is a CONTINUATION page rather than the session's whole tool set.
    # Captured from the REQUEST because the response side cannot tell a final
    # page (no ``nextCursor``) from a complete listing on its own.
    list_paginated: bool = False
    # The tools/call ``params.arguments`` object, captured so an intercepted
    # app render can forward the ORIGINATING inputs to the app (SEP-1865
    # ``ui/notifications/tool-input``). ``None`` for non-tools/call requests.
    tool_arguments: Optional[dict] = None
    # Set only on the gateway-originated ``resources/read`` pending (stub_uuid
    # == ``_APPS_STUB_SENTINEL``): the future the stdout pump resolves with the
    # backend's resources/read response so the parked fetch coroutine wakes.
    apps_future: Optional["asyncio.Future[dict[str, Any]]"] = None
    # ``params.uri`` of a forwarded ``resources/subscribe`` /
    # ``resources/unsubscribe``, captured so the response arm can apply the
    # lease transition the reply confirms or refuses. Empty for every other
    # method.
    resource_uri: str = ""
    # For a ``resources/subscribe``/``unsubscribe`` on an identity-capable
    # server: the caller whose block was injected into the forward. Recorded
    # into ``_grant_callers`` when the server GRANTS, so a later release can
    # be sent as the principal that actually holds the grant — releasing as
    # the connection (or as whatever caller a claim rekey later mapped the
    # stub to) would be adjudicated as a different principal and could
    # revoke a co-tenant's live subscription.
    caller: Optional["CallerContext"] = None
    # Set only on a gateway-originated post-respawn subscribe replay
    # (stub_uuid == ``_RELEASE_STUB_SENTINEL``): the stub whose routing grant
    # the replayed subscribe re-establishes on success.
    replay_stub: str = ""
    # The stub that ORIGINATED a subscribe later converted to the lease
    # sentinel (retract, eviction, detach). Sentinelizing removes the
    # pending from ``stub_uuid``-keyed accounting, and without this the
    # per-stub subscription cap loses sight of it — a stub cycling
    # subscribe/unsubscribe against an unresponsive server would grow the
    # pending table without bound. Cap accounting only; never routed to.
    origin_stub: str = ""
    # The frame as forwarded upstream, kept only for a method in
    # ``_REHANDSHAKE_RETRY_METHODS`` so the request can be sent once more after
    # the backend is handshaken again. ``None`` for every other request, and on
    # the retry itself, which is never retried a second time.
    retry_frame: Optional[dict[str, Any]] = None
    # Set only on a re-sent ``initialize`` (stub_uuid ==
    # ``_REHANDSHAKE_STUB_SENTINEL``): the ``attempt_id`` it was sent under, a
    # log join key read by no decision. Empty for every other request.
    attempt_id: str = ""


# Named by the path callers import it from, as ``BackendGone`` is.
_PendingRequest.__module__ = "kiro_crew.mcp_gateway.backend"


def _strip_caller_meta(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``msg`` with any stub-supplied
    ``params._meta.kirocrew.caller`` / ``params._meta.kirocrew.tenant``
    unconditionally removed.

    The gateway is the trust boundary: stubs are untrusted clients and must
    never be able to forge their caller identity by pre-populating
    ``_meta.kirocrew.caller`` in the request. This function is called on
    EVERY forwarded request regardless of method so a malicious stub cannot
    sneak a forged caller block through non-tools/call methods.

    BOTH gateway-owned blocks are stripped here, in ONE place, deliberately: the
    tenant nonce decides which namespace an unnamed co-tenant's per-tenant state
    lands in, so a stub allowed to supply its own could choose to land in a
    PEER's namespace — the same collision the nonce prevents, only chosen instead
    of accidental. Giving the nonce its own strip function would add a second
    site that every future forward path has to remember; both call sites of this
    one (``forward_from_stub`` and ``_handle_initialize``) already exist.
    """
    out = dict(msg)
    params = out.get("params")
    if not isinstance(params, dict):
        return out
    meta_raw = params.get("_meta")
    if not isinstance(meta_raw, dict):
        return out
    forged = [key for key in (CALLER_META_KEY, TENANT_META_KEY) if key in meta_raw]
    if not forged:
        return out
    # The caller block is a FLAT ``params._meta[CALLER_META_KEY]`` key
    # ("kirocrew.caller") — exactly the shape build_caller_meta writes and
    # CallerContext.from_meta reads. Strip that flat key. (An earlier nested
    # "_meta[kirocrew][caller]" strip was a no-op against the real wire format,
    # so a stub-forged block survived whenever no authoritative identity was
    # injected over it — e.g. a stub that registered without a session_key —
    # enabling cross-tenant identity forgery. Stripping on EVERY forwarded
    # request closes that regardless of whether injection happens.)
    params = dict(params)
    meta = dict(meta_raw)
    for key in forged:
        del meta[key]
    if meta:
        params["_meta"] = meta
    else:
        del params["_meta"]
    out["params"] = params
    return out


# Maximum length of the error string carried in the per-call failure WARNING.
# The point is a greppable breadcrumb an operator can anchor on, not the whole
# payload; a multi-KB tool error would otherwise push the useful fields
# (server, tool, session) off the end of a wrapped log line.
_TOOL_ERROR_LOG_MAX = 300

# Upper bound on the untrusted error prefix handed to ``redact`` before the
# final ``_TOOL_ERROR_LOG_MAX`` truncation. Far larger than the logged cap (so
# redaction still sees enough context to match a secret spanning a few hundred
# bytes) but small enough that the credential/exfiltration scan on the shared
# stdout pump is bounded regardless of the frame size.
_TOOL_ERROR_PRESCAN_MAX = 8192


def _tool_call_error_text(msg: dict[str, Any]) -> Optional[str]:
    """Return a truncated error string when ``msg`` is a FAILED tool-call
    response, else ``None``.

    Two distinct wire shapes mean "this tool call failed":

    * a JSON-RPC ``error`` object — the server could not run the tool at all;
    * a ``result`` carrying ``isError: true`` — the tool ran and reported a
      failure in its own content (the MCP ``CallToolResult`` error channel).

    Both reach the model as a failed call. A plain ``"error" not in msg``
    check misses the second shape (``isError`` lives inside ``result``), so
    this recognises both and extracts a short, greppable description for the
    breadcrumb.

    Returns ``None`` for a settled success and for a malformed frame carrying
    neither ``error`` nor ``result`` (nothing to attribute as a failure).
    """
    err = msg.get("error")
    if isinstance(err, dict):
        text = err.get("message")
        if not isinstance(text, str) or not text:
            text = json.dumps(err, separators=(",", ":"))
    elif isinstance(err, str) and err:
        text = err
    else:
        result = msg.get("result")
        if not isinstance(result, dict) or result.get("isError") is not True:
            return None
        text = _mcp_result_error_text(result)
    # ``text`` is untrusted server output. Slice it to a bounded prefix FIRST
    # (well above ``_TOOL_ERROR_LOG_MAX``) so the credential/exfiltration
    # ``redact`` pass — the same one the backend stderr pump applies — runs on
    # a few KiB rather than a frame bounded only by ``READ_BUFFER_LIMIT_BYTES``;
    # this routing happens on the shared stdout pump, so an O(payload) scan here
    # would add head-of-line latency to every co-pooled session's frames.
    # ``_collapse_nonprintable`` then maps every control character (newlines,
    # escapes, NUL included) to a space so the value cannot forge a second log
    # line or clear the operator's terminal, and the record stays one line.
    if len(text) > _TOOL_ERROR_PRESCAN_MAX:
        text = text[:_TOOL_ERROR_PRESCAN_MAX]
    text = _collapse_nonprintable(redact(text))
    if len(text) > _TOOL_ERROR_LOG_MAX:
        text = text[: _TOOL_ERROR_LOG_MAX - 1] + "\u2026"
    return text


def _mcp_result_error_text(result: dict[str, Any]) -> str:
    """Best-effort human string from an ``isError: true`` CallToolResult.

    An MCP tool error is reported in the result's ``content`` list (text parts
    carry the message); fall back to a compact JSON dump when no text part is
    present so there is always something to log."""
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if parts:
            return " ".join(parts)
    return json.dumps(result, separators=(",", ":"))


# A caller/server-supplied identifier (tool name, session key, server name)
# interpolated into the failed-tool-call WARNING. Cap it so a long name cannot
# push the structured fields off a wrapped log line.
_MCP_IDENT_LOG_MAX = 120


def _collapse_nonprintable(value: str) -> str:
    """Replace every non-printable character (and whitespace run) with a single
    space.

    ``str.split()`` only recognises Python's whitespace set, so an escape
    (``\\x1b``), NUL, BEL or DEL would otherwise survive into the log line and
    could clear the terminal or overwrite preceding records. Map every
    character ``str.isprintable()`` rejects to a space, then collapse runs.
    """
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())


def _log_safe_identifier(value: str, fallback: str = "?") -> str:
    """Make a caller/server-supplied identifier safe to interpolate into a log
    line.

    A tool name or session key is caller input (``params.name`` on a
    tools/call) and a server name is operator/registry input; any of them could
    carry a newline (forging a second gateway/dashboard log entry), a terminal
    escape, or a credential/exfiltration URL. Run the module's ``redact`` pass,
    map every non-printable character to a space, cap the length, and fall back
    to ``fallback`` when nothing printable remains.
    """
    cleaned = _collapse_nonprintable(redact(value))
    if len(cleaned) > _MCP_IDENT_LOG_MAX:
        cleaned = cleaned[: _MCP_IDENT_LOG_MAX - 1] + "\u2026"
    return cleaned or fallback


def _inject_caller_meta(msg: dict[str, Any], caller: CallerContext) -> dict[str, Any]:
    """Return a shallow copy of ``msg`` with ``params._meta.kirocrew.caller``
    unconditionally set from ``caller``.

    Assumes any pre-existing stub-supplied caller block has already been
    stripped by :func:`_strip_caller_meta`. Other ``_meta`` fields
    (``progressToken`` etc.) pass through unchanged.
    """
    out = dict(msg)
    params = out.get("params")
    if isinstance(params, dict):
        params = dict(params)
    else:
        params = {}
    meta_raw = params.get("_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    # Inject the authoritative caller block from the gateway.
    meta.update(build_caller_meta(caller))
    params["_meta"] = meta
    out["params"] = params
    return out


def _inject_tenant_meta(msg: dict[str, Any], nonce: str) -> dict[str, Any]:
    """Return a shallow copy of ``msg`` with ``params._meta.kirocrew.tenant``
    set to this connection's *nonce*.

    Injected on every request FORWARDED FROM A STUB by an advertising backend,
    including the ones with no caller — that is the case it exists for. A backend
    serving a caller the gateway cannot name falls back to a per-PROCESS
    namespace, which on a pooled backend is one namespace for every unnamed
    co-tenant; the nonce splits it per connection.

    Deliberately NOT on the gateway's own synthesized lease frames
    (``resources/subscribe`` / ``resources/unsubscribe`` replays): those carry the
    caller because a subscription is held per caller, and no backend keys
    subscription state by tenant. A backend that ever needs to would have to have
    the nonce threaded into the lease bookkeeping, which is why this says
    forwarded-from-a-stub rather than "always".

    It is NOT an identity and must not be read as one: the block carries no
    session key, so :meth:`CallerContext.from_meta` still finds nothing to parse
    and every identity resolver keeps returning empty for an unnamed caller.

    Copy discipline mirrors :func:`_inject_caller_meta`; assumes any stub-supplied
    block was already removed by :func:`_strip_caller_meta`.
    """
    out = dict(msg)
    params = out.get("params")
    if isinstance(params, dict):
        params = dict(params)
    else:
        params = {}
    meta_raw = params.get("_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    meta.update(build_tenant_meta(nonce))
    params["_meta"] = meta
    out["params"] = params
    return out


# --- Helpers ----------------------------------------------------------------


async def _write_json_line(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Serialize ``obj`` as one JSON-RPC line and drain the writer.

    Backpressure matters: without ``drain()`` a slow backend can let the
    OS pipe buffer fill and silently stall the gateway loop (Phase-0
    item #2). Every write goes through this helper.
    """
    await _write_json_lines(writer, [obj])


async def _write_json_lines(writer: asyncio.StreamWriter, objs: list[Any]) -> None:
    """:func:`_write_json_line` for several frames in ONE write and one drain,
    so no other writer's frame can land between them."""
    payload = b"".join(
        json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n" for obj in objs
    )
    lock = getattr(writer, "_mc_write_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(payload)
        # Bounded: a backend that stopped reading its stdin must not hang the
        # forwarding coroutine (and the heartbeat sweeper) forever. On timeout
        # raise a pipe error so the caller recycles the wedged backend (callers
        # treat BrokenPipeError/ConnectionResetError as BackendGone).
        try:
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_DRAIN_TIMEOUT_SECS)
        except asyncio.TimeoutError as exc:
            raise BrokenPipeError("backend stdin drain timed out") from exc
