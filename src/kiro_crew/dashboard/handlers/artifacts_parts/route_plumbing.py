"""The request plumbing every artifact route shares.

JSON and error responses, the owner gate on write routes (``_non_owner_refusal``), the
capped JSON body read, the saving session's key and source, the SEL audit record, the
live-refresh broadcast, the off-loop store runner, folder-reference resolution and the
publish governance refusal.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew import sel as _sel_mod
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactError,
    ArtifactValidationError,
    get_default_folder_store,
    get_default_store,
)
from kiro_crew.dashboard.handlers._shared import (
    bounded_json_error_as_exception,
    bounded_json_error_code,
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (
    _redact_audit_metadata,
    _redact_text,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.publish_governance import bind_egress_session, publish_denied_reason
from kiro_crew.validation import infer_use_case


def sel():
    """Late-resolved sel() — calls the module function so test patching of
    ``kiro_crew.sel.sel`` (the canonical patch target) continues to work."""
    return _sel_mod.sel()


# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.artifacts")


# Maximum size of an artifact create/update request body (bytes). Sized to the
# store's content cap (MAX_CONTENT_BYTES = 25 MiB) PLUS headroom for JSON
# envelope overhead (base64/escaping + the other body fields), so content the
# store + validation accept (up to 25 MiB) is never rejected earlier at this
# HTTP boundary. A fixed cap smaller than the content cap would silently become
# the effective ceiling for dashboard/MCP artifact_save/update whenever the
# content cap is raised (the "store enforces a stricter cap" assumption inverts).
_MAX_BODY_BYTES = MAX_CONTENT_BYTES + 8 * 1024 * 1024  # 25 MiB content + 8 MiB envelope headroom


# Upper bound (seconds) on any single awaited remote-publish-provider network
# call. Without it a slow/hung provider would block the awaiting request (and
# its event-loop slot) indefinitely (CWE-400: uncontrolled resource
# consumption). Every ``await provider.*`` on a publish provider is wrapped in
# ``asyncio.wait_for(..., timeout=_REMOTE_PROVIDER_TIMEOUT_S)``. On timeout the
# primary-read path (remote_artifact_fetch) maps to a 504; the best-effort
# comment-sync paths degrade the same way any other provider failure does
# (local write still succeeds, sync_state=push_failed).
_REMOTE_PROVIDER_TIMEOUT_S = 15.0


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _non_owner_refusal(request: web.Request, operation: str) -> web.Response | None:
    """Refuse a signed-in dashboard caller who is not the owner; ``None`` admits.

    Every state-changing artifact route calls this first, before it reads the
    body or the store. Two callers are not dashboard subjects and keep their
    own rules: an app token (``request["app"]`` set, bounded by its manifest
    grant) and the loopback internal-secret transport the agent ``artifact_*``
    tools, the CLI and app drivers use. That transport leaves ``app`` absent
    and marks ``internal_auth``, so it is admitted on that mark rather than
    refused by the owner check, which needs a present empty app claim.

    Being first on every write route, it is also where the request's session is
    bound for the enterprise check each publish send runs at dispatch
    (``publish_governance.dispatch_denied_reason``).
    """
    bind_egress_session(request)
    if request.get("app") or request.get("internal_auth") is True:
        return None
    return await require_owner_dashboard_request(request, operation)


def _notify_artifact_update(state: Any, slug: str, version: int, *, deleted: bool = False) -> None:
    """Best-effort WS broadcast of an artifact content change.

    Called from the mutation funnel (create / content update / revert /
    relocate / delete) — the same choke points as the SEL audit, so panel
    chat, other dashboard sessions, Slack, and CLI mutations all emit.
    Fire-and-forget: a dropped or missed broadcast is picked up the next time a
    client fetches the artifact.

    External edits to a file-backed artifact's source_path never pass through a
    handler, so this never fires for them. The dashboard covers that case from
    the other side: ``useArtifactLiveReload`` watches ``source_path`` over
    ``GET /api/file-watch`` and refetches the artifact on a change (see
    docs/system-specs/modules/artifacts.md, "Live refresh — file-backed
    artifacts").
    """
    try:
        if state is not None:
            state.push_artifact_update(slug, version, deleted=deleted)
    except Exception:  # pragma: no cover — fire-and-forget by design
        logger.debug("artifact_update broadcast failed for %s", slug, exc_info=True)


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Read a JSON object body, capped at ``_MAX_BODY_BYTES``; absent is ``{}``.

    Reads through :func:`read_bounded_json`, so the cap bounds what is
    ALLOCATED, not only what is accepted: a declared oversize is refused before
    a byte is read, and a chunked body stops streaming one chunk past the cap
    instead of being buffered whole first.

    A body that is present but does not declare a JSON content type is refused
    415 ``unsupported_media_type`` before it is read. That refusal is RAISED as
    an ``HTTPException``, not as ``ArtifactValidationError``, because every
    caller turns the latter into a 400 and a 415 must reach the client as a 415.
    The oversize and malformed answers keep this module's existing
    ``ArtifactValidationError`` path (a 400 with the message), unchanged.
    """
    body, error = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES, allow_absent=True)
    if error is None:
        assert body is not None
        return body
    if error.status == 415:
        raise bounded_json_error_as_exception(error)
    if error.status == 413:
        raise ArtifactValidationError(f"request body exceeds {_MAX_BODY_BYTES} bytes")
    if bounded_json_error_code(error) == "body_not_object":
        raise ArtifactValidationError("request body must be a JSON object")
    raise ArtifactValidationError("invalid JSON")


def _session_key(request: web.Request) -> str:
    return request.headers.get("X-Session-Key") or ""


# The artifact ``source`` should reflect WHERE the saving session came from.
# ``infer_use_case`` already classifies a session_key into its origin
# (dashboard / slack / cli / cron / subagent / task-runner / unknown), so we use
# that value directly rather than collapsing everything to a generic "manual".
def _artifact_source_for_request(request: web.Request) -> str:
    """Actual origin of the session saving the artifact (never 'manual')."""
    return infer_use_case(_session_key(request))


#: Safe grammar for a client-supplied originating session key. Session keys are
#: opaque handles like ``chat-2`` / ``dashboard:chat-2`` / ``cron:foo`` / a Slack
#: ``ts``; restrict to that charset so a malformed or hostile value (e.g. a JSON
#: list, or injected markup) can neither poison persisted metadata nor reach the
#: dashboard surface unsanitized. Anchored at ``\Z``, not ``$``: Python's ``$``
#: also matches before a final newline, so a ``$`` anchor under ``.match`` admits
#: ``"dashboard:abc\n"`` into the store, where keys compare exactly.
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}\Z")


def _clean_origin_session_key(raw: Any) -> str:
    """Validate a client-supplied ``origin_session_key``.

    Returns the value only when it is a string matching the permitted grammar;
    anything else (non-string, empty, too long, illegal chars) collapses to
    ``""`` so it's simply treated as "no originating session".
    """
    if isinstance(raw, str) and _SESSION_KEY_RE.match(raw):
        return raw
    return ""


def _event_session_id(request: web.Request) -> str | None:
    """Session key for activity-feed events, or None when not a real slot.

    The dashboard's browser client sets X-Session-Key to the literal
    ``dashboard:ui`` for every request — that is not a chat slot a user can
    navigate to, so drop it (same rule as ``api_artifact_update``).
    """
    sk = request.headers.get("X-Session-Key")
    if not sk or sk == "dashboard:ui":
        return None
    return sk


def _audit(
    *,
    tool: str,
    request: web.Request,
    outcome: str,
    extra: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Write a tool-invocation SEL event, redacting caller-supplied text.

    The SEL writer signs bytes as-written and does NOT redact (see
    ``sel.log_governance_decision``'s docstring), so both the ``error`` string
    and every string leaf of ``extra`` are redacted HERE before ``log`` — an
    upstream provider exception can carry a credential or signed URL, and
    ``external_id`` in ``extra`` is provider-controlled. Routing through
    ``redact_via_context`` (not the bare ``_redact_text``) means a loaded
    companion's extra credential/cookie regexes apply to the audit trail too.
    """
    from kiro_crew.platform.context import redact_via_context

    try:
        safe_error = redact_via_context(error) if error else ""
        safe_extra = _redact_audit_metadata(extra) if extra else {}
        sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome=outcome,
            error=safe_error,
            metadata=safe_extra,
        )
    except Exception:  # pragma: no cover — audit must never break a request
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


def _resolve_folder_ref(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Resolve a folder reference (id or ``/``-separated human path) to a folder id.

    Returns ``(folder_id, error_message)``. ``None`` / ``""`` / ``"root"`` →
    ``""`` (unfiled/root). When ``create_missing`` is True, missing path
    segments are created (``mkdir -p``); otherwise an unknown path errors.
    """
    if ref is None:
        return "", None
    if not isinstance(ref, str):
        return "", "folder must be a string"
    if len(ref) > 4096:
        return "", "folder reference too long"
    try:
        fid = get_default_folder_store().resolve_path(ref, create_missing=create_missing)
    except ArtifactError as exc:
        # str(exc) can echo the raw LLM-supplied ref (e.g. "folder path not
        # found: <ref>"); redact before it reaches the dashboard via _err().
        return "", _redact_text(str(exc))
    return fid, None


async def _resolve_folder_ref_off_loop(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Async wrapper for :func:`_resolve_folder_ref`.

    When ``create_missing`` is True the resolver may persist new folders
    (``_save()`` → ``os.fsync``/``os.replace``), which is blocking filesystem
    IO. ``create_missing=False`` walks the tree in memory and writes nothing,
    but it still takes ``ArtifactFolderStore._lock`` — the same lock every
    mutating call holds ACROSS its ``_save()``. Those mutations run in the
    executor, so a read left inline blocks the gateway loop for the length of
    somebody else's folder write. Both modes go to the executor.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        lambda: _resolve_folder_ref(ref, create_missing=create_missing),
    )


async def _run_off_loop(fn):  # type: ignore[no-untyped-def]
    """Run a blocking store call (small filesystem read/write) in the shared
    executor so its ``os.fsync``/``os.replace`` never blocks the event loop.
    Exceptions raised by ``fn`` propagate to the caller unchanged."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), fn)


def _set_folder_and_reload(slug: str, folder_id: str) -> Any:
    """Move an artifact into a folder and return the reloaded record (blocking)."""
    store = get_default_store()
    store.set_folder(slug, folder_id)
    return store.get(slug)


def _publish_governance_denied(request: web.Request, provider_name: str) -> str | None:
    """Plane-C governance chokepoint for artifact publishing.

    Thin alias for :func:`kiro_crew.publish_governance.publish_denied_reason`,
    which owns the decision so the public-web deploy path (``/api/deploy/deploy``
    and the ``deploy-web-aws`` provider row) enforces the SAME ceiling instead of
    growing a second, drifting copy. Kept as a module-level name because the
    artifact routes and their tests reference it directly.
    """
    return publish_denied_reason(request, provider_name)
