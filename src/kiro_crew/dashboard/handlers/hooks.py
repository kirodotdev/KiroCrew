"""Script hooks CRUD and webhook agent execution handlers."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.config.loader import KiroCrewConfig, data_home
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import sanitize_string

logger = logging.getLogger(__name__)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


# ── Script Hooks ──


def _get_hook_store(state: DashboardState):
    """Lazy-init ScriptHookStore on DashboardState."""
    if state._hook_store is None:
        from kiro_crew.hooks import (  # noqa: F811  # circular import
            ScriptHookStore,
            set_global_hook_store,
        )

        state._hook_store = ScriptHookStore()
        set_global_hook_store(state._hook_store)
    return state._hook_store


def _store_failure_guard(handler):
    """Map a webhook/script-hook store failure to 503 instead of a 500.

    Every store this module touches can now REFUSE rather than silently report an
    empty file: reads raise ``WebhookStoreUnreadable`` when the file exists but
    cannot be parsed, and the shared ``hooks.json`` write refuses rather than
    erasing the webhook contexts stored alongside the script hooks. Writes can also
    fail outright on a full or read-only disk (``OSError``).

    Those refusals were being caught one handler at a time, a round of review each.
    Applying one wrapper to every store-touching handler closes the class: a
    handler that already returns a more specific 503 still does (its own guard runs
    first), and anything that would otherwise escape as an unhandled 500 — which
    reads to the operator as a gateway fault rather than "your store needs
    repair" — becomes the shared, machine-readable response.
    """
    @functools.wraps(handler)
    async def _guarded(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except (webhooks.WebhookStoreUnreadable, OSError) as exc:
            logger.warning(
                "webhook store unavailable in %s: %s", getattr(handler, "__name__", "?"), exc
            )
            return _store_unavailable_response()

    return _guarded


def _store_unavailable_response() -> web.Response:
    """The shared 503 for a script-hook mutation that could not be persisted."""
    return web.json_response(
        {
            "error": "the hook store is unavailable, so nothing was changed",
            "code": "store_unavailable",
        },
        status=503,
    )


@_store_failure_guard
async def api_hooks(request: web.Request) -> web.Response:
    """GET /api/hooks — list all script hooks."""
    store = _get_hook_store(request.app["state"])
    return web.json_response({"hooks": [h.to_dict() for h in store.list_all()]})


@_store_failure_guard
async def api_kiro_hooks(request: web.Request) -> web.Response:
    """GET /api/kiro-hooks — read-only view of kiro-cli agent hooks from kirocrew.json."""
    from kiro_crew.agent import _VALID_HOOK_EVENTS, _shipped_defaults, kiro_agents_dir_path
    from kiro_crew.platform import redact_via_context as redact

    agent_cfg = kiro_agents_dir_path() / "kirocrew.json"
    try:
        raw = json.loads(agent_cfg.read_text())
        hooks = raw.get("hooks", {}) if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        hooks = {}
    # Load bundled defaults to tag source
    try:
        raw = json.loads(_shipped_defaults().read_text())
        bundled = raw.get("hooks", {}) if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        bundled = {}
    bundled_keys: set[tuple[str, str, str]] = set()
    for event, entries in bundled.items():
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                bundled_keys.add((event, e.get("command") or "", e.get("matcher") or ""))
    result: dict[str, list[dict]] = {}
    for event, entries in hooks.items():
        if event not in _VALID_HOOK_EVENTS:
            continue  # drop unknown/injected event keys
        tagged = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                key = (event, e.get("command") or "", e.get("matcher") or "")
                # Context-aware redact(): runs the exfil-URL + credential passes
                # and applies a loaded companion's extra regexes (so an internal
                # token in a hook command is scrubbed on this egress surface too).
                tagged.append({
                    "command": redact(e.get("command") or ""),
                    "matcher": redact(e.get("matcher") or ""),
                    "source": "bundled" if key in bundled_keys else "user",
                })
        if tagged:
            result[event] = tagged
    return web.json_response({"hooks": result})


class _StoreUnavailable(Exception):
    """Raised internally when a script-hook mutation could not be persisted."""


async def _mutate_hook_store(operation, *args):
    """Run a script-hook store mutation off the event loop, failing loudly.

    Persistence takes the shared ``hooks.json`` lock and fsyncs, so it cannot run
    inline: a concurrent ``register_hook`` holding that lock would stall the whole
    gateway loop.

    ``ScriptHookStore._save`` refuses to overwrite an unreadable ``hooks.json``
    (overwriting would erase the webhook contexts kept in the same file), and the
    write itself can fail on a full or read-only disk. Both arrive here as
    :class:`_StoreUnavailable` so each caller answers 503 instead of leaking an
    unhandled 500 that reads as a gateway fault.
    """
    try:
        return await asyncio.to_thread(operation, *args)
    except (webhooks.WebhookStoreUnreadable, OSError) as exc:
        logger.warning("script-hook store mutation failed: %s", exc)
        raise _StoreUnavailable(str(exc)) from exc


@_store_failure_guard
async def api_hooks_create(request: web.Request) -> web.Response:
    """POST /api/hooks — create a new script hook."""
    from kiro_crew.validation import (  # noqa: F811
        HOOK_CREATE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    store = _get_hook_store(request.app["state"])
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Validate via schema (rejects wrong types, enforces length limits, sanitizes)
    try:
        validated = validate_tool_args(body, HOOK_CREATE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)

    try:
        hook = await _mutate_hook_store(store.create, validated)
    except _StoreUnavailable:
        return _store_unavailable_response()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.create",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook.id}:{hook.name}:{hook.event}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_detail(request: web.Request) -> web.Response:
    """PUT/DELETE /api/hooks/{hook_id}."""
    from kiro_crew.validation import (  # noqa: F811
        HOOK_UPDATE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    if request.method == "DELETE":
        hook = store.get(hook_id)
        try:
            deleted = await _mutate_hook_store(store.delete, hook_id)
        except _StoreUnavailable:
            return _store_unavailable_response()
        if deleted:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="hook.delete",
                outcome="success",
                source="dashboard",
                resources=f"hook:{hook_id}:{hook.name if hook else 'unknown'}",
            )
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)

    # PUT — update
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Validate via schema (rejects wrong types, enforces length limits, sanitizes)
    try:
        validated = validate_tool_args(body, HOOK_UPDATE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)

    try:
        hook = await _mutate_hook_store(store.update, hook_id, validated)
    except _StoreUnavailable:
        return _store_unavailable_response()
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.update",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook_id}:{hook.name}:{hook.event}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_toggle(request: web.Request) -> web.Response:
    """POST /api/hooks/{hook_id}/toggle — enable/disable."""

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    try:
        hook = await _mutate_hook_store(store.toggle, hook_id)
    except _StoreUnavailable:
        return _store_unavailable_response()
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.toggle",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook_id}:{hook.name}:enabled={hook.enabled}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_test(request: web.Request) -> web.Response:
    """POST /api/hooks/{hook_id}/test — execute hook and return output."""
    # circular import: kiro_crew.hooks pulls dashboard state at module load, so
    # this handler defers the import to call time (matches _get_hook_store above).
    from kiro_crew.hooks import HOOK_EVENT_STOP, run_script_hook  # noqa: F811

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    hook = store.get(hook_id)
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    body = await _json_object(request, default_empty=True)
    if body is None:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    raw_context = body.get("context", "test")
    context = sanitize_string(raw_context)
    if len(context) > 10000:  # Max context length for hook test
        context = context[:10000]
    # Mirror ScriptHookStore.fire()'s Stop payload so a Stop hook reading the
    # stdin ``assistant_text`` key (the full segment; the env var is capped at
    # 500 in run_script_hook) is testable through this endpoint too. Other
    # events keep the default payload (run_script_hook builds it when None).
    hook_event = None
    if hook.event == HOOK_EVENT_STOP:
        hook_event = {
            "hook_event_name": hook.event,
            "cwd": os.getcwd(),
            "assistant_text": context,
        }
    result = await run_script_hook(hook, context, hook_event)
    _sel().log_tool_invocation(
        session_key="dashboard:hook_test",
        agent="kirocrew",
        source="dashboard",
        tool_name=f"hook:{hook.name}",
        tool_kind="script_hook",
        outcome="tested",
        metadata={
            "hook_id": hook.id,
            "hook_event": hook.event,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "context": context,
        },
    )
    return web.json_response(
        {
            "ok": True,
            "result": {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "error": result.error,
                "duration_ms": result.duration_ms,
            },
        }
    )


# ── Webhook Hooks (OpenClaw-style /hooks/agent) ──

_HOOK_SESSION_PREFIX = "hook:"
_HOOK_TIMEOUT_DEFAULT = 599  # ~10 min — prime to avoid thundering herd with cron intervals
_HOOK_TIMEOUT_MAX = 3593  # ~1 hour — prime for same reason
# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_HOOK_STORE_PATH: Path | None = None


def _hook_store_path() -> Path:
    """hooks.json path, resolved against the live data home."""
    return _HOOK_STORE_PATH if _HOOK_STORE_PATH is not None else data_home() / "hooks.json"


_HOOK_MESSAGE_MAX_LEN = 49_999  # ~50K chars — leave 1 char headroom
# The app accepts 60 MiB for artifact uploads, but this externally callable route
# only needs a compact JSON envelope. Bound decompressed/chunked bytes locally so
# a caller cannot make request.read() allocate the app-wide maximum before auth.
_HOOK_BODY_MAX_BYTES = 256 * 1024
_HOOK_READ_CHUNK_BYTES = 64 * 1024
_HOOK_MAX_CONCURRENT = 6
_hook_semaphore = asyncio.Semaphore(_HOOK_MAX_CONCURRENT)

# Session keys with a webhook turn currently in flight.
#
# `sessionKey` is caller-chosen and `register_hook` hands the same id to whatever
# calls back, so two valid calls can share one. Both would resolve to a SINGLE
# session via `get_or_create`, and `_run_hook_agent`'s cleanup releases and resets
# by session key rather than by ownership — so whichever turn finishes or times
# out first destroys the other's live session, losing a turn whose caller already
# got a 200. The capacity semaphore does not cover this: it counts turns globally
# and admits both.
#
# Mutated only from the event loop (single-threaded), so a plain set is safe
# without a lock. Entries are removed in the runner's finally so a failed turn
# cannot wedge a key permanently.
_hook_inflight_sessions: set[str] = set()


def _reset_hook_inflight() -> None:
    """Clear the in-flight session registry. Test-only.

    Mirrors ``webhooks._reset_auth_throttle`` / ``_reset_signature_replay``: the
    registry is process-global and the accept path claims a key that only the
    background runner's finally releases, so a test that drives the endpoint to a
    200 without awaiting that task would otherwise leave the key claimed and make
    a later test on the same session key see a 409.
    """
    _hook_inflight_sessions.clear()


class _HookBodyTooLarge(ValueError):
    """Raised after reading at most one byte beyond the webhook body limit."""


async def _read_hook_body(request: web.Request) -> bytes:
    """Read the exact request bytes without exceeding the endpoint-local cap."""
    declared = request.content_length
    if declared is not None and declared > _HOOK_BODY_MAX_BYTES:
        raise _HookBodyTooLarge

    # Unit-test requests commonly replace request.read() without attaching a
    # payload stream. Real network requests take the streaming branch, which is
    # what protects chunked bodies and decompressed payloads with no useful
    # Content-Length header.
    if not request.can_read_body:
        body = await request.read()
        if len(body) > _HOOK_BODY_MAX_BYTES:
            raise _HookBodyTooLarge
        return body

    body = bytearray()
    while True:
        remaining = _HOOK_BODY_MAX_BYTES + 1 - len(body)
        chunk = await request.content.read(min(_HOOK_READ_CHUNK_BYTES, remaining))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > _HOOK_BODY_MAX_BYTES:
            raise _HookBodyTooLarge


def _read_hook_registrations() -> dict[str, dict]:
    """Read hooks.json, keeping only webhook context registrations.

    ``hooks.json`` is also (mis)used by ``ScriptHookStore``, which writes a
    ``{"hooks": [...]}`` shape; anything whose value is not a dict is not a
    webhook registration and is skipped rather than crashing the read.
    """
    raw = _read_json_file(_hook_store_path())
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): v
        for k, v in raw.items()
        if isinstance(v, dict) and ("context_summary" in v or "summary" in v)
    }


def _read_json_file(path) -> object:
    """Read JSON from *path*, returning ``None`` when absent or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _load_hook_context(hook_id: str) -> str:
    """Load context_summary from hooks.json for a registered hook.

    The three-horizon decay (verbatim < 1h, banner < 24h, dropped beyond)
    lives in :func:`kiro_crew.webhooks.resolve_context` so the dashboard's
    freshness badge is derived from the same code that decides what the
    agent actually receives.
    """
    raw = _read_json_file(_hook_store_path())
    if not isinstance(raw, dict):
        return ""
    _, injectable = webhooks.resolve_context(raw.get(hook_id))
    return injectable


def _verify_hook_token(request: web.Request) -> str | None:
    """Return the id of the webhook token authenticating *request*, else None.

    Checks the multi-token store (sha256 hashes, constant-time compare)
    first, then the legacy scalar ``hooks.webhook_token`` from config as a
    synthetic ``legacy`` entry so pre-existing setups keep working.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        candidate = auth[7:]
    else:
        candidate = request.headers.get("x-kirocrew-token", "")
    if not candidate:
        return None
    return webhooks.token_store().verify(
        candidate, legacy_token=_legacy_hook_token(), stamp_used=False
    )


def _verify_hook_signature(
    request: web.Request, token_id: str, raw_body: bytes
) -> str | None:
    """Verify the request signature for *token_id*. ``None`` means accepted.

    Any other return value is the ``SIG_ERR_*`` string naming the cause, used
    verbatim as the 401 ``error`` and in the recorded run detail. Tokens without
    ``require_signature`` (including the legacy config scalar, which has no
    secret to verify against) are accepted on the bearer alone.

    Fails closed when a token is marked as requiring signatures but has no
    stored secret: that combination only arises from a hand-edited or truncated
    store file, and accepting it would silently downgrade the token to
    bearer-only.
    """
    if token_id == webhooks.LEGACY_TOKEN_ID:
        return None
    entry = webhooks.token_store().entry_for(token_id)
    if entry is None:
        # A concurrent revocation between bearer lookup and this read must fail
        # closed rather than silently downgrading the vanished token to bearer-only.
        return webhooks.SIG_ERR_NO_SECRET
    if not entry.get("require_signature"):
        return None
    return webhooks.verify_signature(
        secret=str(entry.get("signing_secret") or ""),
        timestamp=request.headers.get(webhooks.TIMESTAMP_HEADER),
        signature=request.headers.get(webhooks.SIGNATURE_HEADER),
        body=raw_body,
    )


def _legacy_hook_token() -> str:
    """Return the legacy scalar webhook token from config, or ``""``."""
    cfg = KiroCrewConfig.load()
    hooks_cfg = cfg.hooks if isinstance(cfg.hooks, dict) else {}
    legacy = hooks_cfg.get("webhook_token", "")
    return legacy if isinstance(legacy, str) else ""


async def _json_object(
    request: web.Request, *, default_empty: bool = False
) -> dict | None:
    """Parse a JSON **object** body. ``None`` means answer 400.

    ``await request.json()`` happily returns a list, string or number for a body
    that is valid JSON but not an object, and every caller here then calls
    ``.get()`` on it — which raises and turns a client mistake into a 500.

    *default_empty* keeps the handlers that treat an absent or unparseable body
    as "use the defaults" doing exactly that, while still rejecting a body the
    caller clearly meant as data but shaped wrongly.
    """
    try:
        parsed = await request.json()
    except Exception:
        return {} if default_empty else None
    return parsed if isinstance(parsed, dict) else None


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
        # The store exists but cannot be parsed. Reads now fail closed rather
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
        return web.json_response(
            {"error": "inbound webhooks are unavailable", "code": "webhooks_unavailable"}, status=503
        )
    if not switch_on:
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="hooks.agent",
            outcome="denied",
            source="webhook",
            error="webhooks disabled by operator",
        )
        await asyncio.to_thread(
            webhooks.run_store().record,
            outcome=webhooks.OUTCOME_DISABLED,
            name="Rejected",
            detail="Inbound webhooks are switched off in the dashboard",
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
        return web.json_response({"error": "too many failed attempts", "code": "auth_throttled"}, status=429)

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
        return web.json_response(
            {"error": "inbound webhooks are unavailable", "code": "webhooks_unavailable"}, status=503
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

    # Read the RAW body before anything parses it: the signature covers the exact
    # bytes the caller signed, and re-serialising a parsed dict can never
    # reproduce them (key order, separators and unicode escaping all differ).
    # The endpoint-local streaming reader caps both fixed-length and chunked
    # bodies far below the app-wide 60 MiB artifact-upload allowance.
    try:
        raw_body = await _read_hook_body(request)
    except _HookBodyTooLarge:
        return web.json_response(
            {"error": f"request body exceeds {_HOOK_BODY_MAX_BYTES} bytes", "code": "body_too_large"}, status=413
        )
    except Exception:
        return web.json_response({"error": "could not read request body", "code": "body_unreadable"}, status=400)

    # A valid bearer proves who; the signature proves the body and defeats
    # replay. Failures feed the SAME per-source throttle as a bad bearer — a
    # signature-guessing flood is the same abuse shape as a token-guessing one.
    sig_error = await asyncio.to_thread(
        _verify_hook_signature, request, token_id, raw_body
    )
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
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Type-check before touching the values: a signed caller sending
    # {"message": 1} would otherwise reach .strip() on an int and turn a client
    # mistake into a 500. Same for sessionKey and its .startswith() below.
    raw_message = body.get("message", "")
    if not isinstance(raw_message, str):
        return web.json_response({"error": "message must be a string", "code": "message_not_a_string"}, status=400)
    message = raw_message.strip()
    if not message:
        return web.json_response({"error": "message required", "code": "message_required"}, status=400)
    if len(message) > _HOOK_MESSAGE_MAX_LEN:
        return web.json_response(
            {"error": f"message exceeds {_HOOK_MESSAGE_MAX_LEN} chars", "code": "message_too_long"}, status=400
        )

    session_key = body.get("sessionKey", "")
    if not isinstance(session_key, str):
        return web.json_response({"error": "sessionKey must be a string", "code": "session_key_not_a_string"}, status=400)
    if not session_key:
        session_key = f"hook:default:{int(time.time())}"
    if not session_key.startswith(_HOOK_SESSION_PREFIX):
        return web.json_response(
            {"error": f"sessionKey must start with '{_HOOK_SESSION_PREFIX}'", "code": "session_key_prefix_invalid"}, status=400
        )

    name = body.get("name", "Webhook")
    if not isinstance(name, str):
        # The last body field that was not type-checked, and the only one whose
        # failure lands AFTER the run is recorded: delivery redacts `name`, so a
        # non-string raises there, the notification and Slack DM never run, and
        # the ephemeral session is already reset — the turn's output is gone
        # while the run history says it was delivered.
        return web.json_response({"error": "name must be a string", "code": "name_not_a_string"}, status=400)
    agent = body.get("agent", "") or None
    if agent is not None and not isinstance(agent, str):
        return web.json_response({"error": "agent must be a string", "code": "agent_not_a_string"}, status=400)
    deliver = body.get("deliver", True)
    try:
        timeout_secs = max(
            60,
            min(int(body.get("timeoutSeconds", _HOOK_TIMEOUT_DEFAULT)), _HOOK_TIMEOUT_MAX),
        )
    except (ValueError, TypeError):
        return web.json_response({"error": "timeoutSeconds must be an integer", "code": "timeout_not_an_integer"}, status=400)

    # One turn per sessionKey. Checked BEFORE the capacity gate so an overlapping
    # call is refused for the accurate reason rather than reported as "capacity".
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

    # Fire-and-forget: run agent in background, return immediately
    if _hook_semaphore.locked():
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
            {"error": f"hook capacity reached ({_HOOK_MAX_CONCURRENT})", "code": "capacity_reached"}, status=429
        )
    await _hook_semaphore.acquire()  # immediate — no race in single-threaded asyncio
    _sel().log_api_access(
        caller="webhook",
        operation="hooks.agent",
        outcome="accepted",
        source="webhook",
        resources=session_key,
    )
    _hook_inflight_sessions.add(session_key)
    try:
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
            )
        )
    except BaseException:
        _hook_inflight_sessions.discard(session_key)
        _hook_semaphore.release()
        raise
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return web.json_response({"status": "accepted", "sessionKey": session_key})


async def _run_hook_inner(
    state: DashboardState, session_key: str, message: str, agent: str | None
) -> str:
    """Inner agent turn — called within timeout wrapper."""
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK  # noqa: F811

    client, is_new, resumed = await state.sessions.get_or_create(session_key, agent=agent)
    full_message = message
    if is_new and state.context_builder:
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        full_message, _ = await run_in_embed_pool(
            state.context_builder.build_message,
            message, is_new, session_key, agent=agent, resumed=resumed,
            provider_type=KiroCrewConfig.load().agent.provider,
        )
    result_text = ""
    _complete_event: object | None = None
    # Wall clock for the webhook agent turn: acp leaves TurnUsage.duration_ms
    # at 0, so without this the row records a literal 0. Started after the
    # context build so prompt assembly is not charged to the turn.
    _turn_t0 = time.monotonic()
    async for event in client.stream(full_message):
        if event.kind == EVENT_TEXT_CHUNK:
            result_text += event.text
        elif event.kind == EVENT_COMPLETE:
            _complete_event = event
            break
    state.sessions.record_success(session_key)  # sync; record_failure is async

    # ── Per-turn usage row: attribute webhook spend. ──
    try:
        # circular import: reached while kiro_crew.slack.handler is still
        # initialising (dashboard/handlers/files.py imports is_tracked_channel
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
) -> None:
    """Execute a webhook-triggered agent turn in an ephemeral session.

    Sessions are always destroyed after the turn completes (like subagents).
    Context continuity across webhook calls is provided by hooks.json —
    the agent calls ``register_hook`` to persist context_summary, and this
    handler injects it into the next fresh session.
    """
    # Load persisted context from hooks.json (written by register_hook MCP tool)
    hook_id = session_key.removeprefix(_HOOK_SESSION_PREFIX)
    saved_context = await asyncio.to_thread(_load_hook_context, hook_id)
    if saved_context:
        message = (
            f"=== Restored Context (from prior session) ===\n"
            f"{saved_context}\n"
            f"=== End Restored Context ===\n\n"
            f"{message}"
        )

    started_at = time.time()
    result_text = ""
    outcome = "completed"
    detail = ""
    try:
        result_text = await asyncio.wait_for(
            _run_hook_inner(state, session_key, message, agent), timeout=timeout_secs
        )
    except asyncio.TimeoutError:
        outcome = "timeout"
        result_text = f"Hook agent timed out after {timeout_secs}s"
        detail = result_text
        logger.warning("Hook agent timeout: %s", session_key)
        await state.sessions.record_failure(session_key)
    except Exception:
        outcome = "error"
        result_text = f"Hook agent error: internal failure (session {session_key})"
        detail = result_text
        logger.exception("Hook agent failed for %s", session_key)
        await state.sessions.record_failure(session_key)
    finally:
        try:
            state.sessions.release(session_key)
        except Exception:
            logger.exception("Hook session release failed: %s", session_key)
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
                state.notify(
                    "hook", title, result_text[:2000], meta={"session_key": session_key}
                )
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
    if outcome == "completed" and not detail:
        if not deliver or not result_text:
            detail = "No result to deliver"
        elif destinations:
            detail = "Delivered to " + " + ".join(destinations)
        else:
            detail = "Delivery failed for every destination"
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
    the exfil-URL pass, covers just that one field, and cannot retroactively
    clean rows written before this existed. Redacting on egress applies both
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


def _is_context_registration(value: object) -> bool:
    """True when a hooks.json value is a webhook context registration.

    Mirrors the filter in :func:`_read_hook_registrations`. ``hooks.json`` is
    shared with ``ScriptHookStore``, whose ``hooks`` key holds a LIST of script
    hooks, so an id-addressed delete must confirm the shape it is removing —
    otherwise ``DELETE /api/webhooks/contexts/hooks`` would drop every script
    hook the user has.
    """
    return isinstance(value, dict) and ("context_summary" in value or "summary" in value)


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
                k for k in raw
                if _is_context_registration(raw[k])
                and _redact_hook_identifier(k) == hook_id
            ]
            if len(matches) != 1:
                return False
            target = matches[0]
        # Only ever remove a context registration, never a sibling key that
        # happens to share the requested name.
        if not _is_context_registration(raw[target]):
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
    body = await _json_object(request)
    if body is None:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean", "code": "enabled_not_a_boolean"}, status=400)

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
        tokens, switch_on, contexts, runs = await asyncio.to_thread(
            _webhooks_snapshot
        )
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
    """POST /api/webhooks/tokens — mint a token; both secrets are shown once.

    ``require_signature`` defaults to true. Pass it as false to mint a
    bearer-only token for a caller that cannot compute an HMAC; the response
    then omits ``signing_secret`` because none was generated.
    """
    body = await _json_object(request)
    if body is None:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    require_signature = body.get("require_signature", True)
    if not isinstance(require_signature, bool):
        return web.json_response(
            {"error": "require_signature must be a boolean", "code": "require_signature_not_a_boolean"}, status=400
        )
    try:
        raw, signing_secret, entry = await asyncio.to_thread(
            webhooks.token_store().create,
            body.get("label", ""),
            require_signature=require_signature,
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
        resources=f"token:{entry['id']}:{entry['label']}:signed={require_signature}",
    )
    payload: dict[str, object] = {"ok": True, "token": raw, "entry": entry}
    if signing_secret:
        # The only time this is ever returned. It stays retrievable on disk for
        # the verifier, but no read endpoint echoes it back.
        payload["signing_secret"] = signing_secret
    return web.json_response(payload, status=201)


@_store_failure_guard
async def api_webhook_token_delete(request: web.Request) -> web.Response:
    """DELETE /api/webhooks/tokens/{token_id} — revoke one token."""
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
    body = await _json_object(request, default_empty=True)
    if body is None:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    message = sanitize_string(str(body.get("message") or "")).strip()
    if not message:
        message = (
            "This is a Kiro Crew webhook test request. Reply with a one-line "
            "confirmation that you received it; no other action is needed."
        )
    session_key = f"{_HOOK_SESSION_PREFIX}test:{int(time.time())}"

    store = webhooks.token_store()
    try:
        raw, signing_secret, entry = await asyncio.to_thread(
            store.create, "Test request (auto)"
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
                    f"Cannot mint a probe token ({exc}). Revoke an unused "
                    "token and try again."
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
        webhooks.SIGNATURE_HEADER: webhooks.sign_payload(
            signing_secret, timestamp, body_bytes
        ),
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
