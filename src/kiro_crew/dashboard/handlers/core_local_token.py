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
import re
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.dashboard.handlers.core_security import _sel
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _ct_eq,
    _unix_request_socket,
    generate_token,
    parse_duration,
)
from kiro_crew.dashboard.token_auth_tokens import owner_mint_extra
from kiro_crew.executors import local_auth_executor
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

if TYPE_CHECKING:
    from kiro_crew.member_memory_auth import LocalOwnerProvenance

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
    via_app = ""
    provenance: LocalOwnerProvenance | None
    if not provided or not _ct_eq(expected, provided):
        # Leg 2 is for a value shaped like a derived one (64 hex digits; the local secret
        # is 32). Anything else is the refusal it has always been.
        if not _DERIVED_SHAPE.fullmatch(provided):
            await _note_refused_mint(request)
            _sel().log_api_access(
                caller=request.remote or "unknown",
                operation="token.local",
                outcome="denied",
                source="local-bootstrap",
                resources="invalid-secret",
            )
            return web.json_response(
                {"error": "invalid secret", "code": "invalid_secret"}, status=403
            )
        delegated = await _delegated_leg(request, provided)
        if isinstance(delegated, web.Response):
            return delegated
        provenance, via_app = delegated, delegated.app or ""
    else:
        from kiro_crew.member_memory_auth import local_owner_bootstrap_provenance

        provenance = await asyncio.get_running_loop().run_in_executor(
            local_auth_executor(), local_owner_bootstrap_provenance, request
        )
        if provenance is not None and provenance.basis == "spawned-app":
            # An app backend holds no real secret where a sandbox masks it. One that
            # presents it anyway got it past the mask, and mints nothing with it; only
            # a host with no sandbox (no boundary to hold) keeps the earlier behaviour.
            from kiro_crew.apps import dashboard_proxy

            if await asyncio.to_thread(dashboard_proxy.sandboxed_backend):
                _sel().log_api_access(
                    caller=request.remote or "unknown",
                    operation="token.local",
                    outcome="denied",
                    source="local-bootstrap",
                    resources=(
                        f"denied real-secret-from-app app={provenance.app or 'none'} "
                        f"basis={provenance.basis}"
                    ),
                )
                return web.json_response(
                    {"error": "invalid secret", "code": "invalid_secret"}, status=403
                )
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
                "Make this request inside the gateway's own host or container, not "
                "from an agent's shell or a sandbox.",
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
        # The pane's own forward port on this machine, which binds the app web
        # origin record to the port the hub minted for (apps/pane_ports.py).
        # Meaningful only with a parent port, so only minted beside one.
        efp = request.query.get("embed_forward_port", "")
        if efp.isdigit() and 1 <= int(efp) <= 65535:
            extra["embed_forward_port"] = str(int(efp))
    if via_app:
        # The delegated leg: an owner token scoped by ``dashboard.delegated_scope``. The
        # app is named in ``via_app``, never in ``app``: an ``app`` claim would put the
        # token under app scope, which is not what this grant is.
        extra["via_app"] = via_app
    token = generate_token(owner_id or "local-app", ttl_seconds=ttl, extra=owner_mint_extra(extra))
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
        # The admitting leg, from a fixed vocabulary with no space or ``=`` in it, so
        # a mint no host check verified (``basis=win32-unverified``) is visible.
        resources=(f"token-issued app={app_name}" if app_name else "token-issued")
        + f" basis={provenance.basis or 'unknown'}"
        + (" via=delegated" if via_app else ""),
    )
    return web.json_response({"token": token, "expires_in": ttl})


#: What a derived value looks like: a hex HMAC-SHA256 digest.
_DERIVED_SHAPE = re.compile(r"[0-9a-f]{64}")


async def _note_refused_mint(request: web.Request) -> None:
    """Record that a spawned app backend asked for a token and was refused, for the UI.

    An app without the grant holds no usable value (its masks are empty), so the refusal
    arrives before leg 2; the peer, not the value, says which app asked. Never raises.
    """
    from kiro_crew.apps import dashboard_proxy
    from kiro_crew.member_memory_auth import delegated_app_provenance

    try:
        provenance, _reason = await asyncio.get_running_loop().run_in_executor(
            local_auth_executor(), delegated_app_provenance, request
        )
        if provenance is not None and provenance.app:
            await asyncio.to_thread(dashboard_proxy.note_refused_mint, provenance.app)
    except Exception:
        return


async def _delegated_leg(
    request: web.Request, provided: str
) -> LocalOwnerProvenance | web.Response:
    """Leg 2: the provenance when *provided* is the derived value of the calling app.

    Otherwise the 403 (``invalid_secret``, so the body says no more than the first leg
    does) with its SEL row, which names the app when the caller is a spawned backend.
    Neither the value nor a token is ever logged.
    """
    from kiro_crew.apps import dashboard_proxy
    from kiro_crew.member_memory_auth import delegated_app_provenance

    loop = asyncio.get_running_loop()
    provenance, reason = await loop.run_in_executor(
        local_auth_executor(), delegated_app_provenance, request
    )
    if provenance is not None and provenance.app:
        reason = await asyncio.to_thread(dashboard_proxy.verify_leg_two, provenance.app, provided)
        if not reason:
            return provenance
        if reason == "not-granted":
            await asyncio.to_thread(dashboard_proxy.note_refused_mint, provenance.app)
    _sel().log_api_access(
        caller=request.remote or "unknown",
        operation="token.local",
        outcome="denied",
        source="local-bootstrap",
        resources=(
            f"denied {reason} app={provenance.app if provenance else 'none'} "
            f"basis={provenance.basis if provenance else 'none'} via=delegated"
        ),
    )
    return web.json_response({"error": "invalid secret", "code": "invalid_secret"}, status=403)
