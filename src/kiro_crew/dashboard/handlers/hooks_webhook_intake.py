"""What the inbound webhook route checks and reads before it runs a turn.

The webhook limits (session prefix, timeouts, message and body caps, the capacity
semaphore), the in-flight session registry, the bounded body read, the hook registry on disk
and each run's saved context and execution, token and signature verification, the
installed-agent names, and the bounded JSON-object body read the dashboard's hook and
webhook routes share.

:mod:`kiro_crew.dashboard.handlers.hooks` is this module's facade: callers and tests import
and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.agent_discovery import list_agents
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.execution_context import ExecutionContext

# ── Webhook Hooks (OpenClaw-style /hooks/agent) ──

_HOOK_SESSION_PREFIX = "hook:"


_HOOK_TIMEOUT_DEFAULT = 599  # ~10 min — prime to avoid thundering herd with cron intervals


_HOOK_TIMEOUT_MAX = 3593  # ~1 hour — prime for same reason


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_HOOK_STORE_PATH: Path | None = None


def _hook_store_path() -> Path:
    """hooks.json path, resolved against the live data home.

    Resolved by :func:`kiro_crew.webhooks.context_registrations_path`, which is
    the single owner of that location: ``kirocrew.inventory.webhooks.registered``
    counts the same file from the metrics reader thread, where this module's
    test-only override is not in play and a second spelling of the path would
    read a different install's home.
    """
    if _HOOK_STORE_PATH is not None:
        return _HOOK_STORE_PATH
    return webhooks.context_registrations_path()


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
# without a lock — PROVIDED the accept path tests membership and `.add()`s the
# key in one synchronous critical section with no `await` between them. An await
# there yields to the loop and lets a second same-key request pass the test
# before the first claims, admitting both (TOCTOU). Entries are removed in the
# runner's finally so a failed turn cannot wedge a key permanently.
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


# Named by the path callers import it from: an error chain renders a raised class as
# ``module.qualname``, so the owner module stays out of a traceback or a log line.
_HookBodyTooLarge.__module__ = "kiro_crew.dashboard.handlers.hooks"


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
    webhook registration and is skipped rather than crashing the read. The shape
    test is :func:`kiro_crew.webhooks.is_context_registration`, shared with the
    id-addressed delete and with the registered-hook gauge.
    """
    raw = _read_json_file(_hook_store_path())
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if webhooks.is_context_registration(v)}


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


def _load_hook_execution(session_key: str) -> ExecutionContext:
    """Capture a registration's identity without consulting current member labels."""
    from kiro_crew.execution_context import capture_session_execution, execution_from_record

    path = _hook_store_path()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Hook registration is malformed")
        row = raw.get(session_key.removeprefix(_HOOK_SESSION_PREFIX))
        if isinstance(row, dict) and "execution_context" in row:
            execution = execution_from_record(row)
        else:
            execution = capture_session_execution(session_key)
    else:
        execution = capture_session_execution(session_key)
    if execution.memory_mode != "persistent":
        raise ValueError("Hook registration is disabled for this session mode")
    return execution


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


def _verify_hook_signature(request: web.Request, token_id: str, raw_body: bytes) -> str | None:
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


def _installed_agent_names() -> set[str]:
    """Return currently dispatchable global agent names (blocking filesystem read)."""
    return {agent.name for agent in list_agents()}


async def _json_object(
    request: web.Request, *, default_empty: bool = False
) -> tuple[dict | None, web.Response | None]:
    """Parse a JSON **object** body for the dashboard's hook/webhook routes.

    Returns ``(body, None)``, or ``(None, error_response)`` to return as-is.

    Reads through :func:`read_bounded_json`, so a body that is present but not
    declared JSON is refused 415 before it is read, and the read is capped at
    ``_HOOK_BODY_MAX_BYTES`` -- the same ceiling the external
    ``POST /api/hooks/agent`` webhook applies. These routes carry a handful of
    control fields (a flag, a label, an agent name, a test message), so no
    dashboard caller needs more than the webhook itself accepts. That route
    reads through ``_read_hook_body`` instead and is not a caller.

    A list, string or number body is refused with this module's existing
    ``invalid_json`` 400: every caller calls ``.get()`` on the result, which
    would turn a client mistake into a 500.

    *default_empty* keeps the handlers that treat an absent or unparseable body
    as "use the defaults" doing exactly that, while still rejecting a body the
    caller clearly meant as data but shaped wrongly. It defaults only the two
    "no usable document" answers (absent, ``invalid_json``): the 415 and 413 are
    returned whatever the flag says, so the gate and the cap hold on every route.
    """
    # Body-scope import, like ``require_owner_dashboard_request`` in the hooks routes.
    from kiro_crew.dashboard.handlers._shared import bounded_json_error_code, read_bounded_json

    body, error = await read_bounded_json(
        request, max_bytes=_HOOK_BODY_MAX_BYTES, allow_absent=default_empty
    )
    if error is None:
        return body, None
    code = bounded_json_error_code(error)
    if default_empty and code == "invalid_json":
        return {}, None
    if code == "body_not_object":
        # The hooks routes have always answered a non-object body with their
        # ``invalid_json`` 400; keep that wire answer for the dashboard client.
        return None, web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400
        )
    return None, error
