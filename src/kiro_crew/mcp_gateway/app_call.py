"""Gateway-side handler for the MCP Apps UI→tool callback path (``app-call``).

An embedded MCP App iframe cannot talk to its MCP server directly — the
dashboard exposes ``POST /api/mcp-apps/call``, which relays the request to
gatewayd as a one-shot ``app-call`` control frame on the uid-gated unix
socket (same trust basis as ``claim``/``abort``). This module implements the
gateway side:

1. **Capability token** — the frame carries only an opaque ``spool_id``. The
   gateway re-reads its OWN spool record (written at interception time) to
   recover ``{server, session_key}``; a caller that cannot name a live spool
   id gets nothing. No server names or session keys are trusted from the
   HTTP layer.
2. **Visibility enforcement** — the target tool's ``_meta.ui.visibility`` in the
   backend's ``tools/list`` must admit the ``"app"`` audience (fetched FRESH per
   call — an authorization input is never cached, so a server-side visibility
   revocation takes effect immediately). Per SEP-1865 ``visibility`` defaults to
   ``["model", "app"]``, so a tool that declares nothing IS app-callable; only an
   explicit declaration excluding ``"app"``, or one this host cannot parse, is
   refused.
3. **Argument validation** — the iframe-controlled ``arguments`` are
   validated against the tool's declared ``inputSchema`` (fail-closed; see
   :func:`kiro_crew.validation.validate_mcp_tool_arguments`) before any
   payload can reach the backend.
4. **Governance** — the canonical ``@server/tool`` reference is resolved
   against the governance ceiling ∩ active profile (``mcp`` scope), the same
   decision Plane A applies to model calls. Evaluation errors DENY.
5. **Forward** — the ``tools/call`` is forwarded through the normal
   ``forward_from_stub`` seam on an ephemeral stub, with a
   :class:`CallerContext` of ``session_type="mcp-app"`` so backends (and the
   SEL audit trail) can distinguish app-originated calls from model calls.

Every decision (allow or deny) is SEL-audited — this is a brand-new
UI-originated tool-invocation path and deserves the same trail as ``claim``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from typing import Any, Optional

from kiro_crew.mcp_apps_render import load_spool
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway.apps import AUDIENCE_APP, visibility_allows
from kiro_crew.sel import SecurityEventLog
from kiro_crew.validation import ValidationError, validate_mcp_tool_arguments

logger = logging.getLogger(__name__)

#: Round-trip budgets. tools/list is cheap; tools/call may do real work but
#: must resolve inside the dashboard's own 30s HTTP budget.
_LIST_TIMEOUT_SECS = 10.0
_CALL_TIMEOUT_SECS = 20.0


def _audit(outcome: str, reason: str, *, spool_id: str = "", server: str = "",
           tool: str = "", session_key: str = "") -> None:
    try:
        SecurityEventLog().log_api_access(
            caller=session_key or "unknown",
            operation="mcp-gateway.app-call",
            outcome=outcome,
            source="gateway",
            resources=(
                f"spool_id={spool_id} server={server} tool={tool} reason={reason}"
            ),
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for app-call failed", exc_info=True)


def _rejected(reason: str, **audit_kw: str) -> dict[str, Any]:
    _audit("denied", reason, **audit_kw)
    return {"type": "app-call-rejected", "reason": reason}


async def _roundtrip(
    backend: Any,
    frame: dict[str, Any],
    *,
    caller: Optional[CallerContext],
    timeout: float,
) -> dict[str, Any]:
    """Forward one request through an ephemeral stub and await ITS response.

    Reuses the backend's id-rewrite + stdout-pump routing: the response for
    our stub-scoped id is delivered to our inbox with the original id
    restored. The stub detaches in all cases so refcounts never leak.
    """
    stub_uuid = f"__app_call__{uuid.uuid4().hex[:12]}"
    inbox = await backend.attach_stub(stub_uuid)
    try:
        await backend.forward_from_stub(stub_uuid, frame, caller=caller)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            data = await asyncio.wait_for(inbox.get(), timeout=remaining)
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get("id") == frame["id"] and (
                "result" in msg or "error" in msg
            ):
                return msg
    finally:
        # Cancel any still-in-flight backend request for this stub BEFORE
        # detaching. On a timeout/cancellation the caller gets a failure while
        # a mutating tools/call may still be executing backend-side; without an
        # explicit cancel the work completes invisibly and a caller retry can
        # DUPLICATE the mutation. detach_stub only tears down routing state — it
        # does not signal the backend.
        try:
            await backend.cancel_in_flight_for_stub(stub_uuid)
        except Exception:  # pragma: no cover — best-effort; detach still runs
            logger.debug(
                "cancel_in_flight_for_stub failed for %s", stub_uuid, exc_info=True
            )
        await backend.detach_stub(stub_uuid)


async def _tools_by_name(
    backend: Any, *, caller: Optional[CallerContext] = None
) -> dict[str, dict[str, Any]]:
    """Fresh ``tools/list`` snapshot as ``{name: tool}``.

    Carries the originating ``caller`` so a caller-aware backend returns the
    listing scoped to the producing session (identity-dependent visibility)
    rather than a default/global one — the visibility gate below authorizes
    against exactly what that session may see.

    Deliberately UNCACHED: the snapshot is the authorization input for both
    the visibility gate and the inputSchema validation, and a server can
    revoke a tool's ``"app"`` visibility at any time (``tools/list_changed``).
    A cached snapshot would keep honoring revoked authorization for its TTL.
    App calls are user-interaction-rate and tools/list is cheap (10s budget),
    so freshness costs one extra round-trip per call — the right trade for an
    authorization input.
    """
    frame = {
        "jsonrpc": "2.0",
        "id": f"apps-list-{uuid.uuid4().hex[:8]}",
        "method": "tools/list",
        "params": {},
    }
    reply = await _roundtrip(backend, frame, caller=caller, timeout=_LIST_TIMEOUT_SECS)
    raw = reply.get("result", {}).get("tools", []) if "result" in reply else []
    return {
        t["name"]: t
        for t in raw
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    }


def _tool_visible_to_app(tool: dict[str, Any]) -> bool:
    """True iff an app from this server may call ``tool``.

    SEP-1865: ``visibility`` defaults to ``["model", "app"]`` when omitted, so a
    tool that declares nothing IS app-callable. Only an explicit declaration
    that excludes ``"app"`` — or one this host cannot parse — denies.

    Delegates to :func:`visibility_allows`, the same parser the agent-listing
    filter uses, so the two audiences cannot drift apart.
    """
    return visibility_allows(tool, AUDIENCE_APP).allowed


def _governance_denial(
    server: str, tool_name: str, session_key: str, *, agent: str = ""
) -> Optional[str]:
    """Evaluate governance (``policy ∩ profile``) for an app-originated call.

    Returns a denial reason string, or ``None`` when permitted. Mirrors Plane
    A's ``mcp``-scope semantics for the canonical ``@server/tool`` reference
    (see ``docs/system-specs/modules/governance.md``) so the platform keeps a
    single un-disableable ceiling across BOTH tool-invocation authorities —
    an enterprise deny that binds the model now binds an embedded app too.

    ``agent`` is the producing backend's agent name: a tightly-scoped agent may
    carry its own task-bound deny profile, and resolving without it would let
    an app callback execute a tool the agent's own profile forbids (its ceiling
    would be silently widened to the surface profile).

    Freshness note: Plane A uses the boot-frozen ceiling on the dashboard
    process; gatewayd is a separate daemon, so the policy is loaded per call
    (small JSON, app calls are user-interaction-rate). That is strictly
    fresher, never weaker.

    Fail-closed: any evaluation error DENIES. This is the opposite polarity
    from Plane A's soft fail-open, which is backstopped by the always-on deny
    floor — a floor this app-originated path does not traverse.
    """
    try:
        # circular import: platform.governance imports gateway-adjacent modules;
        # loaded per-call (also keeps the policy read fresh). Keep lazy.
        from kiro_crew.platform.governance import (
            assert_policy_signature_satisfied,
            load_security_policy,
            resolve,
        )
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        ceiling = load_security_policy()
        # Enforce the signature mandate on the ceiling this LIVE re-read produced —
        # but only when it produced one. A tampered policy file must not widen an
        # app callback just because this path re-reads it after boot already
        # verified the original (the raise lands in the except clause below, which
        # denies — the correct polarity here).
        #
        # Gated on `is not None` deliberately. This daemon is not the composition
        # process: it never runs ``boot_platform``, so it calls
        # ``load_security_policy()`` with no ``bundled_loader`` and cannot see a
        # companion-bundled ceiling. ``None`` here therefore does NOT mean "the
        # policy is gone" — on a bundle-only enterprise host it is the normal
        # result, and refusing on it would deny every app callback. Absence stays
        # decidable only where composition happened (boot); closing that residual
        # gap means handing gatewayd the composed ceiling instead of re-reading the
        # file, which is pre-existing behavior and a separate change.
        if ceiling is not None:
            assert_policy_signature_satisfied(ceiling)
        profile = resolve_active_scope(session_key, agent=agent)
        if ceiling is None and profile is None:
            return None  # ungoverned standalone host — permits, as on Plane A
        decision = resolve(ceiling, profile, "mcp", f"@{server}/{tool_name}")
        if not decision.permitted:
            return f"blocked by governance policy: {decision.reason}"
        return None
    except Exception as exc:
        logger.warning("app-call governance evaluation failed; denying: %s", exc)
        return f"governance evaluation failed: {exc}"


async def handle_app_call(pool: Any, frame: dict[str, Any]) -> dict[str, Any]:
    """Handle one ``app-call`` control frame; always returns a reply frame.

    Reply shapes:

    * ``{"type": "app-result", "result": {...}}`` — backend result.
    * ``{"type": "app-error", "error": {...}}`` — backend JSON-RPC error,
      relayed verbatim (the app understands JSON-RPC errors).
    * ``{"type": "app-call-rejected", "reason": "..."}`` — policy or
      infrastructure rejection decided by the gateway.
    """
    spool_id = frame.get("spool_id")
    callback_secret = frame.get("callback_secret")
    tool_name = frame.get("tool")
    arguments = frame.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(spool_id, str) or not spool_id:
        return _rejected("missing spool_id")
    if not isinstance(tool_name, str) or not tool_name:
        return _rejected("missing tool", spool_id=spool_id)
    if not isinstance(arguments, dict):
        return _rejected("arguments must be an object", spool_id=spool_id, tool=tool_name)

    # Capability token: only a live spool record (gateway-written) grants a
    # route. load_spool validates the id shape and enforces spool-dir
    # containment; a swept/never-existed id is a plain deny. Loaded via
    # to_thread — spool records can be multi-MB (a real PDF viewer is 4.4MB)
    # and this handler shares the gatewayd event loop with every stub pump.
    record = await asyncio.to_thread(load_spool, spool_id)
    if record is None:
        return _rejected("unknown or expired app", spool_id=spool_id, tool=tool_name)
    server = record.get("server")
    session_key = record.get("session_key") or ""
    pool_digest = record.get("pool_digest")

    # Capability split: the model-visible marker carries only ``spool_id``,
    # which authorizes NOTHING. The record's ``callback_secret`` is delivered
    # ONLY over the owner-only WS render frame; the iframe replays it here. A
    # model that harvested the marker from its own context cannot produce this
    # secret, so it cannot invoke app-visible tools against the uid socket.
    # Constant-time compare; a record with no secret (shape drift/tamper) or a
    # mismatch is a hard deny.
    record_secret = record.get("callback_secret")
    if not isinstance(record_secret, str) or not record_secret:
        return _rejected(
            "spool record has no callback capability", spool_id=spool_id,
            server=server if isinstance(server, str) else "", tool=tool_name,
            session_key=session_key,
        )
    if not isinstance(callback_secret, str) or not hmac.compare_digest(
        callback_secret, record_secret
    ):
        return _rejected(
            "invalid app callback capability", spool_id=spool_id,
            server=server if isinstance(server, str) else "", tool=tool_name,
            session_key=session_key,
        )

    if not isinstance(server, str) or not server:
        return _rejected("spool record has no server", spool_id=spool_id, tool=tool_name)
    if not isinstance(pool_digest, str) or not pool_digest:
        # Records written before digest binding (or tampered ones) carry no
        # exact identity — deny rather than guess a backend by server name,
        # which could route the call into a co-pooled tenant's partition.
        return _rejected(
            "spool record has no backend binding", spool_id=spool_id,
            server=server, tool=tool_name, session_key=session_key,
        )

    backend = await pool.get_by_digest(pool_digest)
    if backend is None or backend.pool_key.server_name != server:
        return _rejected(
            "producing backend no longer available", spool_id=spool_id,
            server=server, tool=tool_name, session_key=session_key,
        )

    audit_kw = {
        "spool_id": spool_id, "server": server,
        "tool": tool_name, "session_key": session_key,
    }
    # Originating identity for BOTH gateway-generated requests (tools/list and
    # the forwarded tools/call): a caller-aware backend scopes visibility to the
    # producing session rather than returning a default/global listing.
    caller = CallerContext(
        session_key=session_key,
        session_type="mcp-app",
        from_gateway=True,
    )
    try:
        tools = await _tools_by_name(backend, caller=caller)
    except asyncio.TimeoutError:
        return _rejected("tools/list timed out", **audit_kw)
    except Exception as exc:
        logger.warning("app-call tools/list failed for %s: %s", server, exc)
        return _rejected(f"tools/list failed: {exc}", **audit_kw)

    tool = tools.get(tool_name)
    if tool is None or not _tool_visible_to_app(tool):
        return _rejected("tool not app-visible", **audit_kw)

    # Gateway-side argument validation (fail-closed). The iframe fully
    # controls `arguments`; nothing upstream has checked them against the
    # tool's declared contract. Validate against the backend's OWN
    # tools/list inputSchema before the payload can reach a filesystem/
    # subprocess/DB-backed tool — a tool that declares no schema accepts
    # only {} (see validate_mcp_tool_arguments).
    # Gateway-side argument validation (fail-closed). Offloaded: pattern
    # checks against app-controlled values run with a bounded worker inside,
    # and even that bound must not be spent ON the gatewayd event loop.
    try:
        await asyncio.to_thread(
            validate_mcp_tool_arguments, arguments, tool.get("inputSchema")
        )
    except ValidationError as exc:
        return _rejected(f"arguments failed schema validation: {exc}", **audit_kw)

    # Governance ceiling (policy ∩ profile) on the canonical @server/tool ref —
    # same decision Plane A applies to model-originated MCP calls. Offloaded:
    # the evaluation reads the policy file from disk. Fail-closed inside.
    denial = await asyncio.to_thread(
        _governance_denial, server, tool_name, session_key,
        agent=getattr(backend.pool_key, "agent_name", "") or "",
    )
    if denial is not None:
        return _rejected(denial, **audit_kw)

    call_frame = {
        "jsonrpc": "2.0",
        "id": f"apps-call-{uuid.uuid4().hex[:8]}",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        reply = await _roundtrip(
            backend, call_frame, caller=caller, timeout=_CALL_TIMEOUT_SECS
        )
    except asyncio.CancelledError:
        # The tool was already dispatched to the backend; a cancellation here
        # (e.g. SIGTERM during a callback that outlives the shutdown drain)
        # must not silently escape the SEL trail. Audit, then re-raise so the
        # cancellation still propagates (CancelledError is a BaseException, so
        # the `except Exception` below never sees it).
        _audit("cancelled", "handler cancelled mid-call", **audit_kw)
        raise
    except asyncio.TimeoutError:
        return _rejected("tools/call timed out", **audit_kw)
    except Exception as exc:
        logger.warning("app-call forward failed for %s/%s: %s", server, tool_name, exc)
        return _rejected(f"forward failed: {exc}", **audit_kw)

    if "error" in reply:
        _audit("allowed", "backend returned error", **audit_kw)
        return {"type": "app-error", "error": reply["error"]}
    _audit("allowed", "ok", **audit_kw)
    return {"type": "app-result", "result": reply.get("result")}
