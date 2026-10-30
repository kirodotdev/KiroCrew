"""Script hooks CRUD and webhook agent execution handlers."""

# This module is the hooks handler's facade: the handlers package, routes and tests import
# and patch every name through it, as ``kiro_crew.dashboard.handlers.hooks.<name>``. A read
# of a name an owner holds is served from that owner, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every module holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. It keeps the inbound webhook route and the agent turn it
# runs. New work goes to the owner of its responsibility:
#
#   ``hooks_webhook_intake`` -- the webhook's limits, registry reads and caller checks
#   ``hooks_scripts`` -- the script-hook routes, the SEL accessor and the store guard
#   ``hooks_webhooks`` -- the dashboard's webhook management routes

from __future__ import annotations

import asyncio
import functools
import importlib as _importlib
import json
import logging
import secrets
import sys as _sys
import time
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew import _facade_mirror, webhooks
from kiro_crew.agent import (  # noqa: F401
    _VALID_HOOK_EVENTS,
    _shipped_defaults,
    agents_spec_lock,
    kiro_agents_dir_path,
    unsent_kiro_hook_rows,
)
from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.agent_sdk.mcp_problems import (
    UNATTENDED_MCP_WAIT_SECS,
    declared_mcp_missing,
    missing_mcp_notice,
    session_mcp_problems,
    wait_for_declared_mcp,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import (
    DENY_CAUSE_AUDIT_UNAVAILABLE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
)
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.handlers.hooks_scripts import _sel
from kiro_crew.dashboard.handlers.hooks_webhook_intake import (
    _HOOK_BODY_MAX_BYTES,
    _HOOK_MAX_CONCURRENT,
    _HOOK_MESSAGE_MAX_LEN,
    _HOOK_SESSION_PREFIX,
    _HOOK_TIMEOUT_DEFAULT,
    _HOOK_TIMEOUT_MAX,
    _hook_inflight_sessions,
    _hook_semaphore,
    _HookBodyTooLarge,
    _installed_agent_names,
    _load_hook_context,
    _load_hook_execution,
    _read_hook_body,
    _verify_hook_signature,
    _verify_hook_token,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.execution_context import ExecutionContext, clear_session_execution
from kiro_crew.executors import run_in_embed_pool, run_in_tool_gate_pool
from kiro_crew.memory_startup import MemoryStartupUnavailable, wait_for_memory_preparation
from kiro_crew.permission_floor import (
    OUTCOME_PENDING_APPROVAL,
    OUTCOME_REJECTED_TRANSPORT_FLOOR,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_capabilities import CapabilityStartupError
from kiro_crew.tool_gate_busy import hook_gate_busy

logger = logging.getLogger(__name__)


# Deliberately NOT wrapped in @_store_failure_guard. This route is externally
# reachable, and it answers store failure with the neutral `webhooks_unavailable`
# 503 below rather than the dashboard's diagnostic `store_unavailable`: an outside
# caller should not learn that the operator's store needs repair. Its store touches
# are individually guarded (the kill-switch read, the token verification, the
# last-used stamp, the run record), so nothing here escapes as a 500.
async def api_hooks_agent(request: web.Request) -> web.Response:
    """POST /api/hooks/agent — run an agent turn from an external webhook.

    Equivalent to OpenClaw's POST /hooks/agent. Runs in an isolated session
    keyed by ``sessionKey``. Reuses live sessions, resumes expired ones via
    session/load, or creates fresh sessions as fallback.

    Payload:
        message (str, required): prompt for the agent
        sessionKey (str): session routing key (must start with "hook:")
        name (str): human-readable label for notifications
        agent (str): agent name for routing (default: kirocrew)
        deliver (bool): send result to Slack DM + dashboard notification
        timeoutSeconds (int): max agent run duration

    Headers:
        Authorization: Bearer <token>  (or X-KiroCrew-Token)
        X-KiroCrew-Timestamp: unix seconds — required when the token requires
            signatures
        X-KiroCrew-Signature: sha256=<hex hmac of "<timestamp>.<raw body>">
    """

    # Kill switch first: when an operator turns webhooks off, nothing about the
    # request should matter — not a valid token, not capacity. Checked before auth
    # so a disabled endpoint cannot be probed for token validity.
    # Every webhook store touch below goes through asyncio.to_thread: these are
    # flock + read-modify-write + fsync operations, and this route is externally
    # callable, so running them inline would let one caller (or a concurrent
    # dashboard token edit holding the same lock) stall the whole gateway loop.
    try:
        switch_on = await asyncio.to_thread(webhooks.token_store().is_switch_on)
    except webhooks.WebhookStoreUnreadable:
        # The store exists but cannot be parsed. Reads fail closed rather
        # than reporting an empty store, so answer with the same 503 shape an
        # operator-disabled endpoint uses instead of letting the exception
        # become an unhandled 500. Deliberately not recorded to the run store:
        # that store may be the unreadable one, and recording would attempt the
        # write this refusal exists to prevent.
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error="webhook store unreadable",
        )
        # Counted here rather than through the run store: the store a record
        # would write to is the one that cannot be read.
        webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
        return web.json_response(
            {"error": "inbound webhooks are unavailable", "code": "webhooks_unavailable"},
            status=503,
        )
    if not switch_on:
        from kiro_crew.webhook_sources import SOURCE_AGENT, source_refusal

        # A policy refusal reads as a switched-off store; say which one refused.
        by_policy = await asyncio.to_thread(source_refusal, SOURCE_AGENT)
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error=by_policy or "webhooks disabled by operator",
        )
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_DISABLED,
            name="Rejected",
            detail=(
                f"Inbound webhooks: {by_policy}"
                if by_policy
                else "Inbound webhooks are switched off in the dashboard"
            ),
        )
        return web.json_response(
            {"error": "inbound webhooks are disabled", "code": "webhooks_disabled"}, status=503
        )

    # This endpoint is on the token_auth bypass list (it authenticates itself),
    # so unauthenticated attempts reach here from any source that can route to
    # the gateway. Throttle repeated failures per source before doing any work.
    source = request.remote or "unknown"
    if webhooks.auth_throttle_blocked(source):
        _sel().log_api_access(
            caller=source,
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error="auth failures throttled",
        )
        # The branch the series exists for. The throttle deliberately records
        # nothing -- a disk write per rejected request is what it is avoiding --
        # so without this increment a rotated credential goes silent here
        # exactly while it is still broken.
        webhooks.count_request(webhooks.METRIC_OUTCOME_THROTTLED)
        return web.json_response(
            {"error": "too many failed attempts", "code": "auth_throttled"}, status=429
        )

    # Identify the bearer before reading a body. This route bypasses dashboard
    # auth, so an unknown caller must not be able to allocate even the bounded
    # webhook payload buffer. Successful-use metadata is deliberately deferred
    # until HMAC verification passes below.
    try:
        token_id = await asyncio.to_thread(_verify_hook_token, request)
    except webhooks.WebhookStoreUnreadable:
        # Verification reads the token store, so a malformed store raises here
        # too. Answer with the same neutral 503 the kill-switch read uses: an
        # outside caller must not learn the operator's store needs repair, and
        # the alternative is an unhandled 500. Not recorded to the run store —
        # that may be the unreadable file.
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error="webhook store unreadable",
        )
        webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
        return web.json_response(
            {"error": "inbound webhooks are unavailable", "code": "webhooks_unavailable"},
            status=503,
        )
    if not token_id:
        throttled = webhooks.record_auth_failure(source)
        _sel().log_api_access(
            caller=source,
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error="invalid token" + (" (source now throttled)" if throttled else ""),
        )
        # hook_id/token_id stay null: the caller is unidentified by definition.
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_UNAUTHORIZED,
            name="Unknown caller",
            detail="Rejected: missing or invalid bearer token",
        )
        return web.json_response({"error": "unauthorized", "code": "unauthorized"}, status=401)

    # A source pause is an operator-owned admission decision, so enforce it
    # after the caller has authenticated but before allocating or parsing the
    # body. This is deliberately a separate store read from HMAC verification:
    # a revocation between the two checks must still fail closed there.
    source_entry: dict[str, object] | None = None
    if token_id != webhooks.LEGACY_TOKEN_ID:
        try:
            source_entry = await asyncio.to_thread(webhooks.token_store().entry_for, token_id)
        except webhooks.WebhookStoreUnreadable:
            webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
            return web.json_response(
                {"error": "inbound webhooks are unavailable", "code": "webhooks_unavailable"},
                status=503,
            )
        if source_entry is None:
            # Revoked after bearer lookup: do not read the caller's body and do
            # not let the vanished row silently become a legacy-style source.
            _sel().log_api_access(
                caller=source,
                operation="hooks.agent",
                outcome="denied",
                source="webhook",
                resources=f"token:{token_id}",
                error="webhook source revoked during admission",
            )
            # A credential this build judged and refused, so it shares the
            # recorded member rather than minting a second auth label.
            webhooks.count_request(webhooks.OUTCOME_UNAUTHORIZED)
            return web.json_response({"error": "unauthorized", "code": "unauthorized"}, status=401)
        if source_entry.get("enabled", True) is False:
            _sel().log_api_access(
                caller=source,
                operation="hooks.agent",
                outcome="denied",
                source="webhook",
                resources=f"token:{token_id}",
                error="webhook source paused by operator",
            )
            await asyncio.to_thread(
                webhooks.run_store().record,
                outcome=webhooks.OUTCOME_DISABLED,
                name=str(source_entry.get("label") or "Paused source"),
                token_id=token_id,
                detail="Webhook source is paused in the dashboard",
            )
            return web.json_response(
                {"error": "webhook source is paused", "code": "source_disabled"},
                status=503,
            )

    # Read the RAW body before anything parses it: the signature covers the exact
    # bytes the caller signed, and re-serialising a parsed dict can never
    # reproduce them (key order, separators and unicode escaping all differ).
    # The endpoint-local streaming reader caps both fixed-length and chunked
    # bodies far below the app-wide 60 MiB artifact-upload allowance.
    try:
        raw_body = await _read_hook_body(request)
    except _HookBodyTooLarge:
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {
                "error": f"request body exceeds {_HOOK_BODY_MAX_BYTES} bytes",
                "code": "body_too_large",
            },
            status=413,
        )
    except Exception:
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "could not read request body", "code": "body_unreadable"}, status=400
        )

    # A valid bearer proves who; the signature proves the body and defeats
    # replay. Failures feed the SAME per-source throttle as a bad bearer — a
    # signature-guessing flood is the same abuse shape as a token-guessing one.
    sig_error = await asyncio.to_thread(_verify_hook_signature, request, token_id, raw_body)
    if sig_error:
        throttled = webhooks.record_auth_failure(source)
        _sel().log_api_access(
            caller=source,
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error=sig_error + (" (source now throttled)" if throttled else ""),
        )
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_UNAUTHORIZED,
            name="Unsigned caller",
            token_id=token_id,
            detail=f"Rejected: {sig_error}",
        )
        return web.json_response({"error": sig_error, "code": "signature_rejected"}, status=401)

    # Replay insertion occurs inside verify_signature, so only a fully valid
    # request reaches this successful-use stamp. Failed/missing signatures must
    # never make a credential look recently used in the dashboard.
    if token_id != webhooks.LEGACY_TOKEN_ID:
        try:
            await asyncio.to_thread(webhooks.token_store().stamp_used, token_id)
        except (OSError, webhooks.WebhookStoreUnreadable):
            # The stamp is bookkeeping; a caller already past HMAC must not be
            # rejected because it could not be written. The unreadable case is
            # narrow (the kill-switch read above reads the same file and would
            # have answered 503), but it is reachable if the file is replaced
            # between the two reads.
            logger.warning("webhook credential last_used_at stamp failed", exc_info=True)
    webhooks.record_auth_success(source)

    state: DashboardState = request.app["state"]
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Type-check before touching the values: a signed caller sending
    # {"message": 1} would otherwise reach .strip() on an int and turn a client
    # mistake into a 500. Same for sessionKey and its .startswith() below.
    raw_message = body.get("message", "")
    if not isinstance(raw_message, str):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "message must be a string", "code": "message_not_a_string"}, status=400
        )
    message = raw_message.strip()
    if not message:
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "message required", "code": "message_required"}, status=400
        )
    if len(message) > _HOOK_MESSAGE_MAX_LEN:
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": f"message exceeds {_HOOK_MESSAGE_MAX_LEN} chars", "code": "message_too_long"},
            status=400,
        )

    session_key = body.get("sessionKey", "")
    if not isinstance(session_key, str):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "sessionKey must be a string", "code": "session_key_not_a_string"}, status=400
        )
    if not session_key:
        # Per call, not per second: one turn runs per key, so two deliveries in
        # the same second that shared a key would have the second refused.
        session_key = f"hook:default:{int(time.time())}-{secrets.token_hex(6)}"
    if not session_key.startswith(_HOOK_SESSION_PREFIX):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {
                "error": f"sessionKey must start with '{_HOOK_SESSION_PREFIX}'",
                "code": "session_key_prefix_invalid",
            },
            status=400,
        )

    name = body.get("name", "Webhook")
    if not isinstance(name, str):
        # The last body field that was not type-checked, and the only one whose
        # failure lands AFTER the run is recorded: delivery redacts `name`, so a
        # non-string raises there, the notification and Slack DM never run, and
        # the ephemeral session is already reset — the turn's output is gone
        # while the run history says it was delivered.
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "name must be a string", "code": "name_not_a_string"}, status=400
        )
    requested_agent = body.get("agent", "") or None
    if requested_agent is not None and not isinstance(requested_agent, str):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "agent must be a string", "code": "agent_not_a_string"}, status=400
        )
    deliver = body.get("deliver", True)
    try:
        timeout_secs = max(
            60,
            min(body_int(body.get("timeoutSeconds", _HOOK_TIMEOUT_DEFAULT)), _HOOK_TIMEOUT_MAX),
        )
    except (ValueError, TypeError):
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {"error": "timeoutSeconds must be an integer", "code": "timeout_not_an_integer"},
            status=400,
        )

    mapped_agent = str(source_entry.get("agent") or "") if source_entry else ""
    if mapped_agent and requested_agent and requested_agent != mapped_agent:
        _sel().log_api_access(
            caller=source,
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            resources=f"token:{token_id};agent:{mapped_agent}",
            error="request agent conflicts with source destination",
        )
        webhooks.count_request(webhooks.METRIC_OUTCOME_REJECTED_BODY)
        return web.json_response(
            {
                "error": "request agent conflicts with the source destination",
                "code": "agent_conflict",
                "destination_agent": mapped_agent,
            },
            status=409,
        )
    agent = mapped_agent or requested_agent
    if mapped_agent:
        try:
            installed_agents = await asyncio.to_thread(_installed_agent_names)
        except Exception:
            logger.warning("webhook destination-agent discovery failed", exc_info=True)
            _sel().log_api_access(
                caller=source,
                operation="hooks.agent",
                outcome="denied",
                source="webhook",
                resources=f"token:{token_id};agent:{mapped_agent}",
                error="destination-agent discovery unavailable",
            )
            webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
            return web.json_response(
                {
                    "error": "destination agent could not be verified",
                    "code": "agent_discovery_unavailable",
                },
                status=503,
            )
        if mapped_agent not in installed_agents:
            _sel().log_api_access(
                caller=source,
                operation="hooks.agent",
                outcome="denied",
                source="webhook",
                resources=f"token:{token_id};agent:{mapped_agent}",
                error="source destination agent is not installed",
            )
            webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
            return web.json_response(
                {
                    "error": "the webhook source destination agent is not installed",
                    "code": "destination_agent_unavailable",
                    "destination_agent": mapped_agent,
                },
                status=409,
            )

    # One turn per sessionKey. Checked BEFORE the capacity gate so an overlapping
    # call is refused for the accurate reason rather than reported as "capacity".
    #
    # Check-and-claim is a single synchronous critical section: the membership
    # test and the `.add()` run back-to-back with NO `await` between them, so on
    # the single-threaded event loop no second same-key request can interleave
    # and pass the test before this one has claimed. An `await` here (e.g. the
    # capacity semaphore acquire, which yields) would reopen that TOCTOU window
    # and let both callers proceed to a task — the very race this guards. The
    # claim is unwound on every path below that does not spawn the runner; a
    # spawned runner releases it in its own finally.
    if session_key in _hook_inflight_sessions:
        _sel().log_api_access(
            caller="webhook",
            operation="hooks.agent",
            outcome="rejected",
            source="webhook",
            resources=session_key,
            error="session already running",
        )
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_REJECTED_CAPACITY,
            hook_id=session_key.removeprefix(_HOOK_SESSION_PREFIX),
            session_key=session_key,
            name=str(name),
            token_id=token_id,
            detail="Rejected: a turn for this session key is still running",
        )
        return web.json_response(
            {
                "error": "a turn for this session key is already running",
                "code": "session_busy",
            },
            status=409,
        )
    _hook_inflight_sessions.add(session_key)  # claim — no await since the check above

    # From here the key is claimed; every non-spawning exit MUST release it.
    if _hook_semaphore.locked():
        _hook_inflight_sessions.discard(session_key)
        _sel().log_api_access(
            caller="webhook",
            operation="hooks.agent",
            outcome="rejected",
            source="webhook",
            resources=session_key,
            error="capacity reached",
        )
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_REJECTED_CAPACITY,
            hook_id=session_key.removeprefix(_HOOK_SESSION_PREFIX),
            session_key=session_key,
            name=str(name),
            token_id=token_id,
            detail=f"Rejected: {_HOOK_MAX_CONCURRENT} concurrent runs already in flight",
        )
        return web.json_response(
            {
                "error": f"hook capacity reached ({_HOOK_MAX_CONCURRENT})",
                "code": "capacity_reached",
            },
            status=429,
        )

    permit_acquired = False
    try:
        try:
            execution = await asyncio.to_thread(_load_hook_execution, session_key)
        except (OSError, ValueError):
            _hook_inflight_sessions.discard(session_key)
            webhooks.count_request(webhooks.METRIC_OUTCOME_UNAVAILABLE)
            return web.json_response(
                {"error": "hook execution identity is unavailable", "code": "memory_unavailable"},
                status=503,
            )
        # With a positive count acquire completes synchronously; the key was
        # already claimed above even if a test double or future implementation
        # makes this await yield.
        await _hook_semaphore.acquire()
        permit_acquired = True
        _sel().log_api_access(
            caller="webhook",
            operation="hooks.agent",
            outcome="accepted",
            source="webhook",
            resources=session_key,
        )
        task = asyncio.create_task(
            _run_hook_agent(
                state,
                session_key,
                message,
                name,
                agent,
                deliver,
                timeout_secs,
                token_id=token_id,
                execution_context=execution,
            )
        )
    except BaseException:
        # No runner exists to release the claim or permit. This also covers
        # audit failures between acquire and create_task.
        _hook_inflight_sessions.discard(session_key)
        if permit_acquired:
            _hook_semaphore.release()
        raise
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return web.json_response({"status": "accepted", "sessionKey": session_key})


# The permission-request event kind, spelled locally: the agent-sdk-boundary
# gate forbids ADDING an ACP/providers import edge in application code, and
# kiro_crew.agent_sdk does not re-export the event vocabulary yet, so naming
# the wire constant here would grow exactly the edge the gate ratchets down.
# The spelling is pinned against the source of truth by the regression tests,
# which compare it to the real event object's kind.
_EVENT_PERMISSION_REQUEST_KIND = "permission_request"

#: What the MODEL is told when the webhook turn refuses a tool call, keyed by
#: the SEL ``error`` code the deny branch records: (deny cause, reason). A
#: webhook turn has no person to ask, so every entry is a HOST deny, and each
#: owes the in-band notice that corrects kiro-cli's "User denied tool
#: execution". ``hook_deny`` is not here: its reason is the gate's own.
_WEBHOOK_DENY_NOTICE: dict[str, tuple[str, str]] = {
    "no_hook_store": (
        DENY_CAUSE_SURFACE_POLICY,
        "this webhook turn has no hook gate to authorize a tool call, so every call is refused",
    ),
    "gate_error": (
        DENY_CAUSE_HOOK_ERROR,
        "the webhook's hook gate raised while deciding this call",
    ),
    "no_interactive_approver": (
        DENY_CAUSE_SURFACE_POLICY,
        "a webhook turn has no person to approve a tool call, so only a call a hook "
        "rule auto-approves can run, and no rule auto-approved this one",
    ),
    "child_low_fidelity": (
        DENY_CAUSE_SURFACE_POLICY,
        "the call came from a subagent without verifiable security context, and a "
        "webhook turn has no person to approve it instead",
    ),
    "name_grant_headless_reject": (
        DENY_CAUSE_SURFACE_POLICY,
        "the hook's name-based grant was withheld, and a webhook turn has no person "
        "to approve the call instead",
    ),
}


def _webhook_deny_notice(deny_error: str, decision: Any) -> tuple[str, str]:
    """The (cause, reason) the model reads for a webhook deny recorded as *deny_error*.

    An unknown code still yields a true host notice rather than none: the
    alternative is kiro-cli's "user denied", which is false on this surface.
    """
    if deny_error == "hook_deny":
        reason = str(getattr(decision, "reason", "") or "")
        return DENY_CAUSE_POLICY, reason or "a PreToolUse hook denied the call"
    return _WEBHOOK_DENY_NOTICE.get(
        deny_error, (DENY_CAUSE_SURFACE_POLICY, "this webhook turn refused the call")
    )


def _capability_refusal_text(
    exc: CapabilityError | CapabilityStartupError, agent: str | None
) -> str:
    """The run result for a member start the capability check stopped.

    Both exception types carry only a bounded machine code, never file bytes,
    so the code is safe to show. The member comes from the refusal when the
    check recorded it, otherwise from the hook's own destination agent.

    A ``CapabilityError`` refuses the member's saved spec, which the user fixes
    in Capabilities. A ``CapabilityStartupError`` can also be a start that raced
    a save or could not be verified after the process ran, which a resend may
    clear, so its text asks for a resend first.
    """
    member = (getattr(exc, "member", "") or agent or "").strip() or "this crew member"
    if isinstance(exc, CapabilityStartupError):
        return (
            f"Hook agent did not run: the start of {member} could not be verified "
            f"({exc}). Send the webhook again; if it repeats, check {member} > Capabilities."
        )
    if exc.code == "materialization_changed":
        filename = exc.file
        where = f" ({filename})" if filename else ""
        return (
            f"Hook agent did not start: the agent file{where} for {member} was edited "
            f"outside the Capabilities page. Open {member} > Capabilities, review the "
            "change and save it, then send the webhook again."
        )
    return (
        f"Hook agent did not start: the capability check for {member} refused the "
        f"start ({exc.code}). Open {member} > Capabilities to see what needs fixing, "
        "then send the webhook again."
    )


async def _run_hook_inner(
    state: DashboardState,
    session_key: str,
    message: str,
    agent: str | None,
    *,
    execution_context: ExecutionContext | None = None,
    on_client: Callable[[Any], None] | None = None,
) -> str:
    """Inner agent turn — called within timeout wrapper.

    ``on_client`` receives the session's provider as soon as it exists, so the
    caller can still read that session's MCP report when this coroutine is
    cancelled by the timeout or raises.
    """
    from dataclasses import replace

    from kiro_crew import name_grant
    from kiro_crew.context import _neutralize_structural_markers, session_store_for_turn
    from kiro_crew.execution_context import bind_session_execution, read_session_execution
    from kiro_crew.hooks import (  # noqa: F811  # circular import
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        hook_gate_kwargs,
        identity_grant_covers_child,
    )
    from kiro_crew.llm_helpers import _steer_host_deny
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK  # noqa: F811

    execution: ExecutionContext = (
        execution_context
        if execution_context is not None
        else await asyncio.to_thread(_load_hook_execution, session_key)
    )
    if execution.memory_mode != "persistent":
        raise ValueError("Hook execution is disabled for this session mode")

    def bind_captured() -> None:
        prior = read_session_execution(session_key)
        if prior is not None and (
            prior.member_id != execution.member_id
            or prior.store != execution.store
            or prior.memory_mode != "persistent"
        ):
            raise ValueError("Hook session no longer matches its registered execution")
        # Publish the captured identity WITHOUT vouching. A vouched entry is only
        # ever read for the CALLER slot of `create_session`'s own-store admission,
        # and a hook session can never be that caller: its key is a `hook:` synthetic
        # (see `_HOOK_SESSION_PREFIX`) belonging to an ephemeral session that
        # `_run_hook_agent` destroys after the turn, so it is never a dashboard slot
        # and `caller_slot_key` cannot resolve it. Vouching it would only occupy a
        # slot in the capped vouched map for an entry nothing can read. Publishing the
        # record still lets the hook turn run.
        bind_session_execution(
            session_key, execution, replace_existing=True, expected=prior, vouch=False
        )

    await asyncio.to_thread(bind_captured)
    if agent:
        execution = replace(execution, template_id=agent)
    else:
        agent = execution.template_id or None
    memory_store = await session_store_for_turn(state.context_builder, session_key)
    client, is_new, resumed = await state.sessions.get_or_create(session_key, agent=agent)
    if on_client is not None:
        on_client(client)
    if is_new:
        # Nobody can reconnect a slow MCP server by hand in this run, so the
        # first prompt waits (bounded) for the agent spec's servers to report.
        await wait_for_declared_mcp(client, UNATTENDED_MCP_WAIT_SECS)
    full_message = message
    # The ContextBuilder prompt is the only text on this path that legitimately
    # MINTS structural boundary markers, so it is the one text the scrub below
    # must leave byte-exact. Identity, not equality: a prompt rebuilt by any
    # future step is a different object and is treated as untrusted.
    trusted_prompt: str | None = None
    if is_new and state.context_builder:
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        full_message, _ = await run_in_embed_pool(
            state.context_builder.build_message,
            message,
            is_new,
            session_key,
            agent=agent,
            resumed=resumed,
            # Fallback label only: with the session's provider passed below,
            # build_message builds on the SERVED harness label (`kas` on KAS),
            # the same rule chat, cron, task and subagent turns follow, so the
            # skill directory matches what skill_search can load.
            provider_type=KiroCrewConfig.load().agent.provider,
            memory_store=memory_store,
            execution_context=execution,
            context_provider=client,
        )
        trusted_prompt = full_message
    if full_message is not trusted_prompt:
        # ``message`` is supplied by an external caller of /api/hooks/agent, and
        # ContextBuilder.build_message is the only code that neutralizes forgeable
        # boundary markers in it. It runs on the new-session branch alone, so a
        # reused session would hand a forged ``[END OF SESSION CONTEXT]`` /
        # ``[CURRENT USER REQUEST ...]`` pair to the model verbatim. The scrub sits
        # at the last statement before the stream, and covers everything the
        # builder did not produce, so a branch added above cannot route around it.
        # Off-loop like the dashboard's sibling seam: the restored-context prefix
        # ``_run_hook_agent`` prepends is not bounded by _HOOK_MESSAGE_MAX_LEN.
        full_message = await asyncio.to_thread(_neutralize_structural_markers, full_message)
    result_text = ""
    _complete_event: object | None = None
    # Wall clock for the webhook agent turn: acp leaves TurnUsage.duration_ms
    # at 0, so without this the row records a literal 0. Started after the
    # context build so prompt assembly is not charged to the turn.
    _turn_t0 = time.monotonic()
    async for event in client.stream(full_message):
        if event.kind == EVENT_TEXT_CHUNK:
            result_text += event.text
        elif event.kind == _EVENT_PERMISSION_REQUEST_KIND:
            # Webhook turns are headless and their payload is untrusted
            # external input, so the default is DENY. An unanswered request
            # stalls the provider until the _run_hook_agent timeout fires and
            # the watchdog reports the turn as cancelled by the user. Route
            # the request through the same hook gate every other headless
            # runner uses (task_planner, llm_helpers, subagent_manager) and
            # approve ONLY on the gate's affirmative TOOL_AUTO_APPROVE:
            # TOOL_ALLOW means "ask the user" on interactive surfaces, and
            # with no approver here it fails closed.
            decision = None
            deny_error = "no_hook_store"
            hooks_gate = getattr(state.context_builder, "hooks", None)
            if hooks_gate is not None:
                try:
                    decision = await run_in_tool_gate_pool(
                        hooks_gate.on_tool_call,
                        event.title,
                        on_queue_timeout=hook_gate_busy,
                        session_key=session_key,
                        agent=agent or "",
                        **hook_gate_kwargs(event),
                    )
                except Exception:
                    # A raising gate must not leave the request unanswered --
                    # an unanswered request is the exact stall this branch
                    # exists to fix. No verdict is no positive authorization.
                    logger.exception("webhook hook gate failed for %s", session_key)
                    decision = None
                    deny_error = "gate_error"
                else:
                    deny_error = (
                        "hook_deny" if decision.action == TOOL_DENY else "no_interactive_approver"
                    )
            approve = False
            if decision is not None and decision.action == TOOL_AUTO_APPROVE:
                approve = True
                if event.child_low_fidelity and not identity_grant_covers_child(decision, event):
                    # A backend-child request whose security context is
                    # unverified: every hook auto-approve except the
                    # identity-keyed grant read the forgeable, agent-authored
                    # title, so the dashboard runner and the subagent manager
                    # both downgrade it. Headless there is no approval card to
                    # downgrade to, so the downgrade is deny.
                    approve = False
                    deny_error = "child_low_fidelity"
            if approve:
                # An auto-approve tier is a statement about a PROGRAM name,
                # and the shell re-resolves that name through a PATH that can
                # lead with agent-writable directories. Verify it
                # unconditionally, as every name-grant surface does at the
                # point of honour; with no approver to downgrade to, a
                # withheld grant denies (same as llm_helpers headless).
                _ng_refusal = await name_grant.refusal_for_event(event)
                if _ng_refusal is not None:
                    if name_grant.should_log_decline(session_key, _ng_refusal):
                        logger.warning(
                            "declining a webhook hook auto-approve: %s",
                            _ng_refusal.log_text,
                        )
                    name_grant.log_decline(
                        source="webhook",
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        event=event,
                        refusal=_ng_refusal,
                        tier="hook_auto_approve",
                        sel_factory=_sel,
                    )
                    approve = False
                    deny_error = "name_grant_headless_reject"
            if approve:
                # Audit-or-deny: this surface runs unattended, so an
                # auto-approve that cannot be audited must not run.
                # critical=True writes synchronously and re-raises on a
                # filesystem failure; audit BEFORE the wire call so a
                # transport failure cannot skip the audit either (the stated
                # invariant in llm_helpers / backend-security-controls).
                try:
                    # critical=True writes synchronously (open/write/flush)
                    # and this is the branch's common path: off-loop it so an
                    # SEL disk stall cannot pause every gateway task.
                    await asyncio.to_thread(
                        functools.partial(
                            _sel().log_tool_invocation,
                            session_key=session_key,
                            agent=agent or "kirocrew",
                            tool_name=event.title or "unknown",
                            tool_kind=event.tool_kind,
                            outcome=OUTCOME_PENDING_APPROVAL,
                            source="webhook",
                            request_id=str(event.request_id),
                            critical=True,
                        )
                    )
                except Exception:
                    logger.exception("webhook auto-approve audit failed; denying %s", session_key)
                    # The decision itself must not vanish from SEL: hand the
                    # denial to the ordinary (batched) writer best-effort,
                    # naming the audit failure as the reason.
                    try:
                        _sel().log_tool_invocation(
                            session_key=session_key,
                            agent=agent or "kirocrew",
                            tool_name=event.title or "unknown",
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            source="webhook",
                            request_id=str(event.request_id),
                            error="audit_write_failed",
                        )
                    except Exception:
                        logger.debug("denial record after audit failure also failed", exc_info=True)
                    await _steer_host_deny(
                        client,
                        event,
                        "a hook rule auto-approved it, but the security event log "
                        "could not record the approval",
                        cause=DENY_CAUSE_AUDIT_UNAVAILABLE,
                    )
                    await client.reject_tool(event.request_id)
                else:
                    approval_sent = await client.approve_tool(event.request_id)
                    outcome = (
                        OUTCOME_REJECTED_TRANSPORT_FLOOR
                        if approval_sent is False
                        else "auto_approved"
                    )
                    _sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        tool_name=event.title or "unknown",
                        tool_kind=event.tool_kind,
                        outcome=outcome,
                        source="webhook",
                        request_id=str(event.request_id),
                    )
            else:
                # Audit the denial BEFORE rejecting, for the same reason.
                _sel().log_tool_invocation(
                    session_key=session_key,
                    agent=agent or "kirocrew",
                    tool_name=event.title or "unknown",
                    tool_kind=event.tool_kind,
                    outcome="denied",
                    source="webhook",
                    request_id=str(event.request_id),
                    error=deny_error,
                )
                # No person is attached to a webhook turn, so every deny here
                # is the host's: tell the model which one before the wire.
                _deny_cause, _deny_reason = _webhook_deny_notice(deny_error, decision)
                await _steer_host_deny(client, event, _deny_reason, cause=_deny_cause)
                await client.reject_tool(event.request_id)
        elif event.kind == EVENT_COMPLETE:
            _complete_event = event
            break
    state.sessions.record_success(session_key)  # sync; record_failure is async

    # ── Per-turn usage row: attribute webhook spend. ──
    try:
        # circular import: reached while kiro_crew.slack.handler is still
        # initialising (dashboard/file_api/channel_upload.py imports is_tracked_channel
        # from it), so a module-scope import raises ImportError under the
        # suite's import order.
        from kiro_crew.dashboard.handlers.usage import (
            persist_token_record_async,
            read_context_tokens,
            read_effective_agent,
        )

        _used, _window = read_context_tokens(client)
        await persist_token_record_async(
            session_key,
            "",
            _complete_event,
            provider=KiroCrewConfig.load().agent.provider,
            surface="webhook",
            agent=read_effective_agent(client) or agent or "",
            context_used=_used,
            context_window=_window,
            elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
            model_source=client,
        )
    except Exception:
        logger.debug("usage row (webhook) persist failed", exc_info=True)

    return result_text


async def _run_hook_agent(
    state: DashboardState,
    session_key: str,
    message: str,
    name: str,
    agent: str | None,
    deliver: bool,
    timeout_secs: int,
    token_id: str | None = None,
    execution_context: ExecutionContext | None = None,
) -> None:
    """Execute a webhook-triggered agent turn in an ephemeral session.

    Sessions are always destroyed after the turn completes (like subagents).
    Context continuity across webhook calls is provided by hooks.json —
    the agent calls ``register_hook`` to persist context_summary, and this
    handler injects it into the next fresh session.
    """
    # Pure, cannot raise, so it may sit outside the try below.
    hook_id = session_key.removeprefix(_HOOK_SESSION_PREFIX)

    started_at = time.time()
    result_text = ""
    outcome = "completed"
    detail = ""
    clients: list[Any] = []
    mcp_problems = ""
    mcp_missing: tuple[str, ...] = ()
    mcp_never_reported = ""
    try:
        # Loading the persisted context (written by the register_hook MCP tool)
        # is INSIDE the try because the caller's permit and the in-flight claim
        # are released only by this function's finally, so anything that escapes
        # a try entered any later gives back neither. Six of those wedge the
        # endpoint at 429 `capacity_reached` permanently and one wedges that
        # session key at 409 `session_busy` until the gateway restarts, which
        # contradicts the invariants stated above at the semaphore and at
        # `_hook_inflight_sessions`.
        #
        # Two distinct escapes, and they leave by different doors. A read error
        # is NOT one of them -- `_read_json_file` answers `None` for an absent or
        # corrupt store -- but `resolve_context` parses what that store holds, so
        # malformed stored data raises, and that lands in the `except Exception`
        # leg and is recorded as an `error` run instead of vanishing. Cancellation
        # while this await is pending does not: `CancelledError` is a
        # `BaseException` and passes straight through that leg. The `finally` is
        # what covers it, and only from inside the try.
        saved_context = await asyncio.to_thread(_load_hook_context, hook_id)
        if saved_context:
            message = (
                f"=== Restored Context (from prior session) ===\n"
                f"{saved_context}\n"
                f"=== End Restored Context ===\n\n"
                f"{message}"
            )

        # The inner turn's first step opens the session's memory, which the
        # gateway keeps closed from boot until its startup preparation task has
        # restored and initialized every store. A dashboard chat turn waits for
        # that task before it reads memory; this turn takes the same bounded
        # wait, so a webhook accepted during startup runs once memory is ready
        # instead of failing on its first read.
        await wait_for_memory_preparation(getattr(state, "memory_startup_task", None))

        result_text = await asyncio.wait_for(
            _run_hook_inner(
                state,
                session_key,
                message,
                agent,
                execution_context=execution_context,
                on_client=clients.append,
            ),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        outcome = "timeout"
        result_text = f"Hook agent timed out after {timeout_secs}s"
        detail = result_text
        logger.warning("Hook agent timeout: %s", session_key)
        await state.sessions.record_failure(session_key)
    except (CapabilityError, CapabilityStartupError) as exc:
        # The member's capability check stopped the start. The run record and
        # the delivered result name the refusal and what to do about it, which
        # the generic arm below cannot.
        outcome = "error"
        result_text = _capability_refusal_text(exc, agent)
        detail = result_text
        logger.warning("Hook agent %s did not start: %s", session_key, exc)
        await state.sessions.record_failure(session_key)
    except MemoryStartupUnavailable as exc:
        # Memory preparation outlasted the wait, or recovery failed for this
        # store. The run record and the delivered result name that, so the
        # caller knows to retry or to inspect memory recovery.
        outcome = "error"
        result_text = f"Hook agent did not start: {exc}"
        detail = result_text
        logger.warning("Hook agent %s did not start: %s", session_key, exc)
        await state.sessions.record_failure(session_key)
    except Exception:
        outcome = "error"
        result_text = f"Hook agent error: internal failure (session {session_key})"
        detail = result_text
        logger.exception("Hook agent failed for %s", session_key)
        await state.sessions.record_failure(session_key)
    finally:
        # Read before release/reset: the report lives on the session being torn
        # down. Here rather than in the inner turn so a timeout or crash still
        # says which servers never started.
        mcp_problems = session_mcp_problems(clients[0] if clients else None)
        mcp_missing = declared_mcp_missing(clients[0] if clients else None)
        mcp_never_reported = missing_mcp_notice(clients[0] if clients else None)
        if mcp_problems:
            logger.warning(
                "Hook agent %s: MCP servers unusable in this run — %s",
                session_key,
                mcp_problems,
            )
        try:
            state.sessions.release(session_key)
        except Exception:
            logger.exception("Hook session release failed: %s", session_key)
        try:
            # Withdraw this process's word on the hook's identity. Neither call
            # below reaches it: `release` returns the slot and `SessionManager.reset`
            # recycles the session without going near the execution maps. And the
            # entry is always there to withdraw, because the hook path binds
            # PERSISTENT only -- `_run_hook_inner` raises for any other mode -- so
            # the bind leaves a vouched entry rather than a live carrier.
            #
            # Hook session keys are per-request by default (`hook:default:{ts}-{hex}`),
            # and per-event keys are the ordinary webhook pattern, so without this
            # the map would gain one permanent entry per authenticated request and
            # grow until the process restarted.
            clear_session_execution(session_key)
        except Exception:
            logger.exception("Hook execution withdrawal failed: %s", session_key)
        try:
            await state.sessions.reset(session_key)
        except Exception:
            logger.exception("Hook session reset failed: %s", session_key)
        finally:
            _hook_inflight_sessions.discard(session_key)
            _hook_semaphore.release()

    # Deliver BEFORE recording, and record only the destinations that actually
    # accepted the result. Deriving `delivered` from intent instead of outcome
    # meant a failed Slack DM was still stored as delivered to Slack, so the run
    # history — the only place an operator can check — named a destination that
    # received nothing.
    destinations: list[str] = []
    mcp_notice = f"MCP servers unusable in this run: {mcp_problems}" if mcp_problems else ""
    if mcp_missing and outcome == webhooks.OUTCOME_COMPLETED:
        # A turn that ran without a declared server is not a clean completion:
        # a distinct outcome lets the operator retry it instead of trusting it.
        outcome = webhooks.OUTCOME_DEGRADED
    if mcp_never_reported:
        mcp_notice = f"{mcp_never_reported}. {mcp_notice}" if mcp_notice else mcp_never_reported
    if mcp_notice:
        result_text = f"{result_text}\n\n{mcp_notice}" if result_text else mcp_notice
    if result_text:
        # Sanitize before delivery.
        result_text, _ = redact_exfiltration_urls(result_text)
        result_text, _ = redact_credentials(result_text)

        if deliver:
            name_safe, _ = redact_exfiltration_urls(name)
            name_safe, _ = redact_credentials(name_safe)
            title = f"Webhook: {name_safe}"
            # Guarded like the Slack path below: this was the one delivery call
            # with no handler, so a raising notifier lost the result AND skipped
            # the Slack attempt that might still have succeeded.
            try:
                state.notify("hook", title, result_text[:2000], meta={"session_key": session_key})
                destinations.append("notifications")
            except Exception:
                logger.exception("Hook agent: notification delivery failed")
            if state.slack_client and state.owner_id:
                try:
                    channel = await state.slack_client.open_dm(state.owner_id)
                    if channel:
                        await state.slack_client.post_message(
                            channel, f"*{title}*\n{result_text[:3000]}"
                        )
                        destinations.append("Slack DM")
                    else:
                        logger.warning("Hook agent: no Slack DM channel for owner")
                except Exception:
                    logger.exception("Hook agent: Slack delivery failed")

    delivered = bool(destinations)
    if outcome in (webhooks.OUTCOME_COMPLETED, webhooks.OUTCOME_DEGRADED) and not detail:
        if not deliver or not result_text:
            detail = "No result to deliver"
        elif destinations:
            detail = "Delivered to " + " + ".join(destinations)
        else:
            detail = "Delivery failed for every destination"
    if mcp_notice:
        detail = f"{mcp_notice} | {detail}" if detail else mcp_notice
    await asyncio.to_thread(
        webhooks.run_store().record,
        outcome=outcome,
        hook_id=hook_id,
        session_key=session_key,
        name=str(name),
        started_at=started_at,
        duration_ms=int((time.time() - started_at) * 1000),
        result_chars=len(result_text),
        token_id=token_id,
        delivered=delivered,
        detail=detail,
    )

    _sel().log_tool_invocation(
        session_key=session_key,
        source="webhook",
        tool_name="hooks.agent",
        outcome=outcome,
        downstream_service="slack" if "Slack DM" in destinations else "internal",
    )
    logger.info("Hook agent %s: %s (%d chars)", outcome, session_key, len(result_text))


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.hooks_webhook_intake",
    "kiro_crew.dashboard.handlers.hooks_scripts",
    "kiro_crew.dashboard.handlers.hooks_webhooks",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.hooks_scripts import (  # noqa: F401
        _UNSENT_AUTHORED,
        _get_hook_store,
        _mutate_hook_store,
        _store_failure_guard,
        _store_unavailable_response,
        _StoreUnavailable,
        api_hook_detail,
        api_hook_test,
        api_hook_toggle,
        api_hooks,
        api_hooks_create,
        api_kiro_hooks,
    )
    from kiro_crew.dashboard.handlers.hooks_webhook_intake import (  # noqa: F401
        _HOOK_READ_CHUNK_BYTES,
        _HOOK_STORE_PATH,
        _hook_store_path,
        _json_object,
        _legacy_hook_token,
        _read_hook_registrations,
        _read_json_file,
        _reset_hook_inflight,
    )
    from kiro_crew.dashboard.handlers.hooks_webhooks import (  # noqa: F401
        _T,
        _commit_pinned_token,
        _delete_hook_context,
        _DestinationAgentGone,
        _hook_slots_in_use,
        _list_hook_contexts,
        _public_runs,
        _redact_hook_identifier,
        _webhook_endpoint_url,
        _webhooks_snapshot,
        api_webhook_context_delete,
        api_webhook_test,
        api_webhook_token_create,
        api_webhook_token_delete,
        api_webhook_token_update,
        api_webhooks,
        api_webhooks_switch,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.agent_discovery import _read_agent_spec, list_agents  # noqa: F401
    from kiro_crew.validation import sanitize_string  # noqa: F401
