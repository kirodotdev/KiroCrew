"""The local token bootstrap: a dashboard session for a process running as this user.

``/api/token/local`` mints a session token only for a request that carries the local
secret, comes from a process the gateway verifies as the local owner, and arrives over
loopback TCP or over the gateway's ``AF_UNIX`` socket from a peer the kernel confirms
is this process's own principal (``_unix_peer_is_self``); the logout and shutdown
routes admit the same two transports.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _ct_eq,
    _unix_request_socket,
    generate_token,
    parse_duration,
)
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

# ── Local token bootstrap (Electron / local apps) ─────────────────────


def _unix_peer_is_self(request: web.Request) -> bool:
    """True iff *request* arrived on an ``AF_UNIX`` socket AND the kernel
    positively confirms the peer runs as this process's own principal.

    Transport-admission twin of ``is_loopback`` for the local-secret endpoints:
    an ``AF_UNIX`` request has an EMPTY ``request.remote``, so the
    loopback test alone 403s the transport that is strictly HARDER to reach
    than loopback TCP — the dashboard's socket sits ``0600`` inside a ``0700``
    owner-only directory, and the kernel reports who connected, which loopback
    TCP cannot. Because ``/api/token/local`` is ``token_auth``-bypassed, this
    admission is deny-by-default via ``check_peer_is_self``: ``MISMATCH``
    (another principal reached our socket — exactly when the directory gate
    has failed and refusing matters most) and ``UNVERIFIABLE`` (no mechanism,
    failed syscall) are BOTH refused, so a platform without peer credentials
    never silently widens the gate. This admits a TRANSPORT, never a caller —
    the ``X-Local-Secret`` check downstream is unchanged.

    Transport discrimination is delegated to ``token_auth._unix_request_socket``,
    the one shared definition of "arrived on the dashboard's unix socket" for
    the CSRF and token-auth layers.
    """
    sock = _unix_request_socket(request)
    return sock is not None and check_peer_is_self(sock) is PeerCredResult.MATCH


async def api_token_local(request: web.Request) -> web.Response:
    """GET /api/token/local — issue a token for local apps.

    Requires a per-session secret written to ~/.kiro/crew/.local_secret at
    gateway startup. Only processes on the same machine can read the file.
    Secret passed via ``X-Local-Secret`` header (not query string, to avoid
    leaking in logs).

    Reachable over loopback TCP or the dashboard's ``AF_UNIX`` socket; unix
    peers are admitted only on a positive kernel same-principal check
    (``_unix_peer_is_self``), which is stronger locality evidence than a
    loopback address. The secret is required on both transports.

    Each of the three refusals carries a machine-readable ``code`` beside its
    ``error`` — ``loopback_only``, ``invalid_secret``, ``member_owner_token_refused``
    — mirroring the distinction already in the SEL record, so a caller reports the
    gate that refused rather than listing the ones that might have. The codes
    restate what the ``error`` text already says and widen no gate.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    if not _h.is_loopback(request.remote or "") and not _unix_peer_is_self(request):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="token.local",
            outcome="denied",
            source="local-bootstrap",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)

    expected = request.app.get("local_secret", "")
    if not expected:
        return web.json_response({"error": "not available"}, status=503)
    provided = request.headers.get("X-Local-Secret", "")
    if not provided or not _ct_eq(expected, provided):
        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="token.local",
            outcome="denied",
            source="local-bootstrap",
            resources="invalid-secret",
        )
        return web.json_response({"error": "invalid secret", "code": "invalid_secret"}, status=403)
    from kiro_crew.member_memory_auth import local_owner_bootstrap_provenance

    provenance = await asyncio.to_thread(local_owner_bootstrap_provenance, request)
    if provenance is None:
        _sel().log_api_access(
            caller="local-process",
            operation="token.local",
            outcome="denied",
            source="local-bootstrap",
            resources="unverified-owner-process",
        )
        return web.json_response(
            {
                "error": "The gateway could not verify this process as the local owner. "
                "Open the dashboard using its CLI login link on the gateway host.",
                "code": "member_owner_token_refused",
            },
            status=403,
        )
    ttl = MAX_SESSION_TTL_SECS
    ttl_param = request.query.get("ttl", "")
    if ttl_param:
        parsed = parse_duration(ttl_param)
        if parsed:
            ttl = parsed
    state = request.app.get("state")
    owner_id = str(getattr(state, "owner_id", "") or "")
    # Optional multi-instance embed claim: the parent (embedding) dashboard's
    # port, so the embedded remote can authorize exactly that loopback parent
    # origin in CSP frame-ancestors (see server._extra_frame_ancestors). Minted
    # only via this local-secret-gated endpoint; validated as a loopback port.
    extra: dict[str, str] = {}
    epp = request.query.get("embed_parent_port", "")
    if epp.isdigit() and 1 <= int(epp) <= 65535:
        extra["embed_parent_port"] = str(int(epp))
    token = generate_token(owner_id or "local-app", ttl_seconds=ttl, extra=extra or None)
    # The provenance gate knows WHICH app's backend owns the calling process, so
    # the audit row names it: a token minted by an app is attributed to that app,
    # and any later per-app scoping of this grant is a data change here rather
    # than a protocol change. The name goes into the record only, not into the
    # token: an ``app``-scoped token is refused by ``is_owner_dashboard_request``,
    # so a claim would withdraw the capability. The registry key is kebab-case
    # (``apps.manifest.app_name_error``), so it cannot carry a space or ``=`` and
    # cannot forge a second field in the row.
    app_name = provenance.app
    _sel().log_api_access(
        caller=request.remote or "unknown",
        operation="token.local",
        outcome="success",
        source="local-bootstrap",
        resources=f"token-issued app={app_name}" if app_name else "token-issued",
    )
    return web.json_response({"token": token, "expires_in": ttl})
