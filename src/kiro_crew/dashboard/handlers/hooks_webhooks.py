"""The dashboard's webhook management routes, under ``/api/webhooks``.

The kill switch, the listing with its run contexts and recent runs, token create, update and
delete (a destination agent is pinned under the agent-spec lock), a run context's deletion,
and the test send.

:mod:`kiro_crew.dashboard.handlers.hooks` is this module's facade: callers and tests import
and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, TypeVar

import aiohttp
from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.agent import agents_spec_lock, kiro_agents_dir_path
from kiro_crew.dashboard.handlers.hooks_scripts import _sel, _store_failure_guard
from kiro_crew.dashboard.handlers.hooks_webhook_intake import (
    _HOOK_BODY_MAX_BYTES,
    _HOOK_MAX_CONCURRENT,
    _HOOK_MESSAGE_MAX_LEN,
    _HOOK_SESSION_PREFIX,
    _HOOK_TIMEOUT_DEFAULT,
    _HOOK_TIMEOUT_MAX,
    _hook_semaphore,
    _hook_store_path,
    _installed_agent_names,
    _json_object,
    _legacy_hook_token,
    _read_hook_registrations,
    _read_json_file,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import sanitize_string

logger = logging.getLogger("kiro_crew.dashboard.handlers.hooks")


_T = TypeVar("_T")


class _DestinationAgentGone(Exception):
    """The pinned agent was installed at the pre-check but not at commit time."""


# Named by the path callers import it from: an error chain renders a raised class as
# ``module.qualname``, so the owner module stays out of a traceback or a log line.
_DestinationAgentGone.__module__ = "kiro_crew.dashboard.handlers.hooks"


def _commit_pinned_token(agent: str, commit: Callable[[], _T]) -> _T:
    """Thread-side: re-verify *agent* and run *commit* under the agents spec lock.

    The template delete guard counts webhook pins and unlinks the spec file
    while holding ``agents_spec_lock``; a token that pins an agent must therefore
    be COMMITTED under that same lock, or it can validate against a file the
    delete is about to remove and land pointing at nothing. Lock order matches
    the delete's (spec lock, then the token store's own file lock), so the two
    writers serialize instead of deadlocking. The loop-side pre-check stays for
    the fast, friendly 400; this is the one that decides.
    """
    agents_dir = kiro_agents_dir_path()
    # The lockfile lives beside the specs; the pinned agent's own file is in
    # this directory, so creating it is never a surprise.
    agents_dir.mkdir(parents=True, exist_ok=True)
    with agents_spec_lock(agents_dir):
        if agent not in _installed_agent_names():
            raise _DestinationAgentGone(agent)
        return commit()


# ── Webhook management API (dashboard-authed) ──


def _hook_slots_in_use() -> int:
    """Best-effort count of webhook runs currently holding a slot.

    Derived from the semaphore's remaining permits; purely diagnostic, so a
    missing attribute degrades to 0 rather than failing the read endpoint.
    """
    remaining = getattr(_hook_semaphore, "_value", _HOOK_MAX_CONCURRENT)
    try:
        return max(0, _HOOK_MAX_CONCURRENT - int(remaining))
    except (TypeError, ValueError):
        return 0


def _webhook_endpoint_url(request: web.Request) -> str:
    """Public URL an external system should POST to, as this client sees us."""
    return f"{request.url.origin()}/api/hooks/agent"


def _list_hook_contexts(now: float | None = None) -> list[dict]:
    """Registered webhook contexts from hooks.json, newest registration first."""
    stamp = now if now is not None else time.time()
    out: list[dict] = []
    for hook_id, entry in _read_hook_registrations().items():
        freshness, _ = webhooks.resolve_context(entry, stamp)
        summary = entry.get("context_summary", "") or entry.get("summary", "")
        summary = summary if isinstance(summary, str) else ""
        # The summary is agent-written free text (register_hook), so it can carry
        # whatever the session had in view — including a credential. Scrub before
        # it leaves on this read surface, the same way api_kiro_hooks scrubs hook
        # commands and _run_hook_agent scrubs the delivered result. Redact BEFORE
        # slicing so a secret cannot survive by straddling the transport cut.
        safe_summary, _ = redact_exfiltration_urls(summary)
        safe_summary, _ = redact_credentials(safe_summary)
        # The id and session key are equally agent-supplied — register_hook names
        # the hook — so they need the same scrub. Redacting only the summary
        # would leave a credential pasted into a hook id on this surface.
        safe_id = _redact_hook_identifier(hook_id)
        raw_session = entry.get("session_key") or f"{_HOOK_SESSION_PREFIX}{hook_id}"
        safe_session = _redact_hook_identifier(str(raw_session))
        try:
            registered = float(entry.get("registered_at", 0) or 0)
        except (TypeError, ValueError):
            registered = 0.0
        out.append(
            {
                "hook_id": safe_id,
                "session_key": safe_session,
                "registered_at": registered,
                "age_seconds": max(0, int(stamp - registered)) if registered else None,
                "freshness": freshness,
                "context_summary": safe_summary[: webhooks.CONTEXT_SUMMARY_TRANSPORT_MAX],
                # Length of the stored text, so the UI can show that the pane is
                # a preview of a longer summary rather than the whole thing.
                "context_chars": len(summary),
            }
        )
    out.sort(key=lambda c: c["registered_at"] or 0.0, reverse=True)
    return out


def _redact_hook_identifier(value: str) -> str:
    """Scrub an agent-supplied identifier for egress.

    ``register_hook`` takes ``hook_id`` as free-form text with no charset
    constraint, and ``mcp_core`` already redacts that same value before echoing
    it back, so this surface matches that precedent.
    """
    safe, _ = redact_exfiltration_urls(str(value))
    safe, _ = redact_credentials(safe)
    return safe


def _public_runs(runs: list[dict]) -> list[dict]:
    """Scrub caller-supplied text in run history before it leaves.

    Every string field a run carries originates outside the gateway: ``hook_id``
    and ``session_key`` come from whatever ``register_hook`` was called with,
    ``name`` is a free-text field on the inbound request body, and ``detail``
    quotes caller-derived reasons. A credential pasted into any of them — a hook
    named after an API key, say — would otherwise be stored verbatim and then
    rendered on the dashboard, turning the run list into a disclosure surface.

    The record-time pass over ``name`` is not sufficient on its own: it runs only
    the exfil-URL pass, covers just that one field, and cannot clean a row
    already on disk. Redacting on egress applies both
    passes to every field, on every read, which is the same discipline
    ``_list_hook_contexts`` already follows for the context list.
    """
    scrubbed: list[dict] = []
    for run in runs:
        row = dict(run)
        for field in ("hook_id", "session_key", "name", "detail"):
            value = row.get(field)
            if isinstance(value, str) and value:
                row[field] = _redact_hook_identifier(value)
        scrubbed.append(row)
    return scrubbed


def _delete_hook_context(hook_id: str) -> bool:
    """Drop *hook_id* from hooks.json. False when it is not registered.

    The read surface publishes REDACTED identifiers, so a client can only ever
    ask to delete the redacted form of a hook whose raw id contained something
    secret-shaped. Resolve that by comparing stored keys through the same
    redaction; an ambiguous match (two raw ids redacting alike) deletes nothing
    rather than guessing which one the user meant.
    """
    path = _hook_store_path()
    with webhooks.locked(path):
        raw = _read_json_file(path)
        if not isinstance(raw, dict):
            return False
        target = hook_id
        if target not in raw:
            matches = [
                k
                for k in raw
                if webhooks.is_context_registration(raw[k])
                and _redact_hook_identifier(k) == hook_id
            ]
            if len(matches) != 1:
                return False
            target = matches[0]
        # Only ever remove a context registration, never a sibling key that
        # happens to share the requested name.
        if not webhooks.is_context_registration(raw[target]):
            return False
        del raw[target]
        webhooks.write_json_atomic(path, raw)
    return True


@_store_failure_guard
async def api_webhooks_switch(request: web.Request) -> web.Response:
    """POST /api/webhooks/switch — turn inbound webhooks on or off.

    Non-destructive kill switch: tokens, contexts and run history all survive, so
    turning webhooks back on restores every integration without re-provisioning
    the callers. Dashboard-authed like the rest of the management surface.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.switch")
    if denied is not None:
        return denied
    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error
    assert body is not None  # (dict, None) on success
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "enabled_not_a_boolean"}, status=400
        )
    from kiro_crew.edition_locks import LOCKED_BY_POLICY, locked_off

    # Enterprise: webhooks stay off (RFC enterprise edition, Red "Webhooks by
    # source"), so an enable is refused rather than stored behind the lock.
    if enabled and locked_off():
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.switch",
            outcome="denied",
            source="dashboard",
            error="locked_by_policy",
        )
        return web.json_response(
            {"error": LOCKED_BY_POLICY, "code": "locked_by_policy", "switch_on": False},
            status=409,
        )

    try:
        stored = await asyncio.to_thread(webhooks.token_store().set_switch, enabled)
    except (webhooks.WebhookStoreUnreadable, OSError) as exc:
        # The store refuses rather than reporting an empty file, and the write
        # itself can fail on a full or read-only disk. Either way the switch
        # state on disk is unchanged, so report that instead of surfacing an
        # unhandled 500 that reads as a gateway fault.
        logger.warning("webhook kill switch could not be persisted: %s", exc)
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.switch",
            outcome="denied",
            source="dashboard",
            error="switch store unavailable",
        )
        return web.json_response(
            {
                "error": "the webhook store is unavailable, so the switch was not changed",
                "code": "store_unavailable",
            },
            status=503,
        )
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.switch",
        outcome="success",
        source="dashboard",
        resources=f"enabled={stored}",
    )
    return web.json_response({"ok": True, "switch_on": stored})


def _webhooks_snapshot() -> tuple[list[dict], bool, list[dict], list[dict]]:
    """Read every webhook store the dashboard view needs, in one call.

    Sync on purpose: the caller runs it via ``asyncio.to_thread`` so the file
    reads stay off the event loop, and grouping them means the returned tokens,
    switch state, contexts and runs all come from one point in time.
    """
    store = webhooks.token_store()
    return (
        store.public_entries(legacy_token=_legacy_hook_token()),
        store.is_switch_on(),
        _list_hook_contexts(),
        _public_runs(webhooks.run_store().list_runs()),
    )


@_store_failure_guard
async def api_webhooks(request: web.Request) -> web.Response:
    """GET /api/webhooks — endpoint config, tokens, contexts and run history."""
    # One offloaded hop for the whole read: four separate to_thread round trips
    # would each be a context switch, and a snapshot taken under one call is also
    # internally consistent rather than interleaved with a concurrent token edit.
    try:
        tokens, switch_on, contexts, runs = await asyncio.to_thread(_webhooks_snapshot)
    except webhooks.WebhookStoreUnreadable as exc:
        # Reads refuse rather than reporting an empty store, so the page gets a
        # named error it can show instead of an unhandled 500 that looks like a
        # gateway fault. Reported as 503 because the data exists but cannot be
        # served until an operator fixes the file.
        logger.warning("webhook dashboard read failed: %s", exc)
        return web.json_response({"error": str(exc), "code": "store_unreadable"}, status=503)
    payload = {
        # `enabled` is the effective state the endpoint actually enforces: a token
        # must exist AND the operator switch must be on. `switch_on` is reported
        # separately so the UI can say WHY it is off rather than just that it is.
        "enabled": bool(tokens) and switch_on,
        "switch_on": switch_on,
        "has_tokens": bool(tokens),
        "url": _webhook_endpoint_url(request),
        "slots": {"in_use": _hook_slots_in_use(), "max": _HOOK_MAX_CONCURRENT},
        "limits": {
            "session_key_prefix": _HOOK_SESSION_PREFIX,
            "message_max": _HOOK_MESSAGE_MAX_LEN,
            "body_max_bytes": _HOOK_BODY_MAX_BYTES,
            "timeout_default": _HOOK_TIMEOUT_DEFAULT,
            "timeout_max": _HOOK_TIMEOUT_MAX,
            "max_concurrent": _HOOK_MAX_CONCURRENT,
            "signature_window_seconds": webhooks.SIGNATURE_WINDOW_SECONDS,
        },
        "tokens": tokens,
        "contexts": contexts,
        "runs": runs,
    }
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.read",
        outcome="success",
        source="dashboard",
        resources=f"tokens={len(tokens)}",
    )
    return web.json_response(payload)


@_store_failure_guard
async def api_webhook_token_create(request: web.Request) -> web.Response:
    """POST /api/webhooks/tokens — mint a routed source credential. Owner-only.

    The bearer this route hands back authenticates on ``POST /api/hooks/agent``
    through :func:`_verify_hook_token`, and that route's own comment states what
    the credential buys: a real agent turn with full tool access. Minting one is
    therefore at least as privileged as the agent writes
    ``dashboard/agent_admin/crew_records.py::api_kirocrew_agents_create`` reserves for the
    owner, so this route applies the same predicate and returns the same 403 shape.

    The caller it stops is a real principal, not a hypothetical one: an
    allow-listed messaging user running ``!dashboard`` holds an ordinary
    dashboard session (``app == ""``, ``sub != owner_id``) that token auth
    admits, and an ungated mint lets that scoped session trade itself for a
    durable, session-independent credential.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.token_create")
    if denied is not None:
        return denied
    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error
    assert body is not None  # (dict, None) on success
    require_signature = body.get("require_signature", True)
    if not isinstance(require_signature, bool):
        return web.json_response(
            {
                "error": "require_signature must be a boolean",
                "code": "require_signature_not_a_boolean",
            },
            status=400,
        )
    agent = body.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return web.json_response(
            {"error": "agent is required", "code": "agent_required"}, status=400
        )
    agent = agent.strip()
    try:
        installed_agents = await asyncio.to_thread(_installed_agent_names)
    except Exception:
        logger.warning("webhook destination-agent discovery failed", exc_info=True)
        return web.json_response(
            {
                "error": "destination agent could not be verified",
                "code": "agent_discovery_unavailable",
            },
            status=503,
        )
    if agent not in installed_agents:
        return web.json_response(
            {
                "error": "destination agent is not installed",
                "code": "destination_agent_unavailable",
            },
            status=400,
        )
    try:
        raw, signing_secret, entry = await asyncio.to_thread(
            _commit_pinned_token,
            agent,
            lambda: webhooks.token_store().create(
                body.get("label", ""),
                require_signature=require_signature,
                agent=agent,
            ),
        )
    except _DestinationAgentGone:
        return web.json_response(
            {
                "error": "destination agent is not installed",
                "code": "destination_agent_unavailable",
            },
            status=400,
        )
    except webhooks.WebhookError as exc:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.token_create",
            outcome="denied",
            source="dashboard",
            error=str(exc),
        )
        return web.json_response({"error": str(exc), "code": "credential_rejected"}, status=400)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.token_create",
        outcome="success",
        source="dashboard",
        resources=f"token:{entry['id']}:{entry['label']}:agent={agent}:signed={require_signature}",
    )
    payload: dict[str, object] = {"ok": True, "token": raw, "entry": entry}
    if signing_secret:
        # The only time this is ever returned. It stays retrievable on disk for
        # the verifier, but no read endpoint echoes it back.
        payload["signing_secret"] = signing_secret
    return web.json_response(payload, status=201)


@_store_failure_guard
async def api_webhook_token_update(request: web.Request) -> web.Response:
    """PATCH /api/webhooks/tokens/{token_id} — update source-owned settings."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.token_update")
    if denied is not None:
        return denied
    token_id = request.match_info["token_id"]
    if token_id == webhooks.LEGACY_TOKEN_ID:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.token_update",
            outcome="denied",
            source="dashboard",
            resources=f"token:{token_id}",
            error="legacy credential is config-managed",
        )
        return web.json_response(
            {
                "error": "the legacy credential is config-managed",
                "code": "legacy_credential_in_config",
            },
            status=400,
        )
    body, body_error = await _json_object(request)
    if body_error is not None:
        return body_error
    assert body is not None  # (dict, None) on success
    allowed = {"agent", "enabled", "label"}
    unknown = sorted(set(body) - allowed)
    if unknown or not body:
        return web.json_response(
            {
                "error": "patch may only contain agent, enabled, or label",
                "code": "invalid_source_patch",
            },
            status=400,
        )
    if "agent" in body:
        agent = body["agent"]
        if not isinstance(agent, str) or not agent.strip():
            return web.json_response(
                {"error": "agent is required", "code": "agent_required"}, status=400
            )
        agent = agent.strip()
        try:
            installed_agents = await asyncio.to_thread(_installed_agent_names)
        except Exception:
            logger.warning("webhook destination-agent discovery failed", exc_info=True)
            return web.json_response(
                {
                    "error": "destination agent could not be verified",
                    "code": "agent_discovery_unavailable",
                },
                status=503,
            )
        if agent not in installed_agents:
            return web.json_response(
                {
                    "error": "destination agent is not installed",
                    "code": "destination_agent_unavailable",
                },
                status=400,
            )
    else:
        agent = None
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "enabled_not_a_boolean"}, status=400
        )
    if "label" in body and not isinstance(body["label"], str):
        return web.json_response(
            {"error": "label must be a string", "code": "label_not_a_string"}, status=400
        )

    def _update() -> dict[str, Any] | None:
        return webhooks.token_store().update(
            token_id,
            agent=agent,
            enabled=body.get("enabled") if "enabled" in body else None,
            label=body.get("label") if "label" in body else None,
        )

    try:
        # A re-pin commits under the agents spec lock, like a mint; a change
        # that leaves the pin alone has nothing to serialize with.
        if agent is not None:
            entry = await asyncio.to_thread(_commit_pinned_token, agent, _update)
        else:
            entry = await asyncio.to_thread(_update)
    except _DestinationAgentGone:
        return web.json_response(
            {
                "error": "destination agent is not installed",
                "code": "destination_agent_unavailable",
            },
            status=400,
        )
    except webhooks.WebhookStoreUnreadable:
        raise
    except webhooks.WebhookError as exc:
        return web.json_response({"error": str(exc), "code": "source_update_rejected"}, status=400)
    if entry is None:
        return web.json_response({"error": "not found", "code": "credential_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.token_update",
        outcome="success",
        source="dashboard",
        resources=f"token:{token_id}:fields={','.join(sorted(body))}",
    )
    return web.json_response({"ok": True, "entry": entry})


@_store_failure_guard
async def api_webhook_token_delete(request: web.Request) -> web.Response:
    """DELETE /api/webhooks/tokens/{token_id} — revoke one token."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.token_delete")
    if denied is not None:
        return denied
    token_id = request.match_info["token_id"]
    if token_id == webhooks.LEGACY_TOKEN_ID:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.token_delete",
            outcome="denied",
            source="dashboard",
            resources=f"token:{token_id}",
            error="legacy token is config-managed",
        )
        return web.json_response(
            {
                "error": (
                    "The legacy token lives in your config. Remove "
                    "hooks.webhook_token from config.yaml to revoke it."
                ),
                "code": "legacy_credential_in_config",
            },
            status=400,
        )
    if not await asyncio.to_thread(webhooks.token_store().delete, token_id):
        return web.json_response({"error": "not found", "code": "credential_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.token_delete",
        outcome="success",
        source="dashboard",
        resources=f"token:{token_id}",
    )
    return web.json_response({"ok": True})


@_store_failure_guard
async def api_webhook_context_delete(request: web.Request) -> web.Response:
    """DELETE /api/webhooks/contexts/{hook_id} — drop a stored context."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.context_delete")
    if denied is not None:
        return denied
    hook_id = request.match_info["hook_id"]
    if not await asyncio.to_thread(_delete_hook_context, hook_id):
        return web.json_response({"error": "not found", "code": "context_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.context_delete",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook_id}",
    )
    return web.json_response({"ok": True})


@_store_failure_guard
async def api_webhook_test(request: web.Request) -> web.Response:
    """POST /api/webhooks/test — fire a real loopback call at /api/hooks/agent.

    Mints a throwaway token, signs the request with that token's own signing
    secret, then revokes the token, so the probe exercises the genuine bearer +
    signature auth path rather than a bypass.
    """
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "webhooks.test")
    if denied is not None:
        return denied
    body, body_error = await _json_object(request, default_empty=True)
    if body_error is not None:
        return body_error
    assert body is not None  # (dict, None) on success
    message = sanitize_string(str(body.get("message") or "")).strip()
    if not message:
        message = (
            "This is a Kiro Crew webhook test request. Reply with a one-line "
            "confirmation that you received it; no other action is needed."
        )
    agent = body.get("agent") or "kirocrew"
    if not isinstance(agent, str):
        return web.json_response(
            {"error": "agent must be a string", "code": "agent_not_a_string"},
            status=400,
        )
    try:
        installed_agents = await asyncio.to_thread(_installed_agent_names)
    except Exception:
        logger.warning("webhook test destination-agent discovery failed", exc_info=True)
        return web.json_response(
            {
                "error": "destination agent could not be verified",
                "code": "agent_discovery_unavailable",
            },
            status=503,
        )
    if agent not in installed_agents:
        return web.json_response(
            {
                "error": "the webhook source destination agent is not installed",
                "code": "destination_agent_unavailable",
                "destination_agent": agent,
            },
            status=409,
        )
    session_key = f"{_HOOK_SESSION_PREFIX}test:{int(time.time())}"

    store = webhooks.token_store()
    try:
        raw, signing_secret, entry = await asyncio.to_thread(
            store.create, "Test request (auto)", agent=agent
        )
    except webhooks.WebhookError as exc:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="webhooks.test",
            outcome="denied",
            source="dashboard",
            error=str(exc),
        )
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"Cannot mint a probe token ({exc}). Revoke an unused " "token and try again."
                ),
                "code": "probe_credential_mint_failed",
            },
            status=409,
        )

    try:
        port = int(request.app.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    port = port or request.url.port or 6776
    url = f"http://127.0.0.1:{port}/api/hooks/agent"
    payload = {
        "message": message,
        "sessionKey": session_key,
        "name": "Webhook test",
        "agent": agent,
        "deliver": False,
    }
    # Serialise once and send those exact bytes: the signature covers the raw
    # body, so letting aiohttp re-encode a dict would sign one byte sequence and
    # transmit another. This is also why the probe posts ``data=`` not ``json=``.
    body_bytes = json.dumps(payload).encode("utf-8")
    timestamp = int(time.time())
    # Bearer + signature, exactly as an external caller sends them. The probe
    # token requires a signature like any freshly minted token, so this exercises
    # the full auth path — no gateway IPC secret, no dashboard cookie, and no
    # signing bypass.
    headers = {
        "Authorization": f"Bearer {raw}",
        "Content-Type": "application/json",
        webhooks.TIMESTAMP_HEADER: str(timestamp),
        webhooks.SIGNATURE_HEADER: webhooks.sign_payload(signing_secret, timestamp, body_bytes),
    }
    status = 0
    error = ""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body_bytes, headers=headers) as resp:
                status = resp.status
                if resp.status not in (200, 202):
                    detail = (await resp.text())[:400]
                    error = f"{resp.status}: {detail}"
    except Exception as exc:
        error = f"loopback request failed: {type(exc).__name__}"
        logger.exception("Webhook test request failed")
    finally:
        # Always revoke the probe credential, and keep even this cleanup off the
        # loop: it takes the same store lock an inbound webhook may be holding.
        await asyncio.to_thread(store.delete, entry["id"])

    ok = not error
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="webhooks.test",
        outcome="success" if ok else "failure",
        source="dashboard",
        resources=session_key,
        error=error,
    )
    if not ok:
        return web.json_response({"ok": False, "status": status, "error": error})
    return web.json_response({"ok": True, "status": status, "session_key": session_key})
