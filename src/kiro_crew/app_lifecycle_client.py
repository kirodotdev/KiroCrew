"""Client for live app lifecycle changes using ordinary dashboard authentication.

The local credential mint and the authenticated app action travel exclusively over
the dashboard's owner-only Unix socket, and each connection is checked, before any
byte is sent, to be answered by the gateway process recorded for the port. Neither
credential can reach a foreign process bound to the resolved TCP port or to a
rebound socket path.
There is deliberately no TCP fallback and no MCP/internal-secret authorization path.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.loopback_http import deadline_expired, unix_socket_urlopen
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.terminal_safe import safe_terminal_line

# A legal enable can spend 120 s provisioning npm dependencies, about 105 s in
# backend health checks, and another 30 s in the default onEnable hook. Leave
# enough headroom for registration and capability dependency resolution.
_ACTION_TIMEOUT_SECS = 300


class AppGatewayError(RuntimeError):
    """A running gateway refused or could not complete an app lifecycle request."""

    def __init__(self, message: str, *, code: str = "") -> None:
        # This exception is rendered by cli_commands_app on ONE prefixed line, so the
        # terminal boundary is here even when the message came from an HTTP error
        # or response payload, and a line break in it must not start a new line.
        super().__init__(safe_terminal_line(message))
        # The gateway's machine-readable ``code`` when it sent one, so the CLI can
        # act on a refusal (``session_approval_consent_required``) without parsing prose.
        self.code = code


class AppGatewayTimeout(AppGatewayError):
    """The gateway accepted the action but did not answer within the deadline.

    Distinct from a refusal because the outcome is UNKNOWN, not negative: the
    gateway may still be applying the action. The CLI must say so rather than
    report a refusal, and must not call a retry safe — the lifecycle routes
    serialize on ``app_lifecycle_lock`` but rerun their hooks for an app already
    in the target state, so a blind retry can double-apply.
    """


def _read_json(response: object) -> object:
    """Read one complete JSON response or translate truncation/malformed data."""
    try:
        return json.loads(response.read())  # type: ignore[attr-defined]
    except (http.client.IncompleteRead, ValueError) as exc:
        raise AppGatewayError("gateway returned a malformed response") from exc


def _gateway_error(exc: urllib.error.HTTPError) -> AppGatewayError:
    """Return the gateway's structured error text and code, falling back to its status."""
    try:
        body = json.loads(exc.read())
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            code = body.get("code")
            return AppGatewayError(body["error"], code=code if isinstance(code, str) else "")
    except Exception:
        pass
    return AppGatewayError(f"{exc.code} {exc.reason}")


def _socket_unavailable(exc: OSError) -> bool:
    """Whether strict Unix transport proved that no gateway received the request."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (FileNotFoundError, ConnectionRefusedError)):
        return True
    return (
        isinstance(reason, OSError) and "AF_UNIX" in str(reason) and "not available" in str(reason)
    )


def toggle_app(
    app_name: str, action: str, *, payload: dict[str, object] | None = None
) -> dict[str, object] | None:
    """Apply an app lifecycle action through the owner-only dashboard socket.

    The resolver's port names the per-instance socket; its evidence source does
    not gate the attempt because the socket's location and peer check provide the
    ownership proof. ``None`` means no socket endpoint accepted the request (or no
    local secret exists), so the CLI may safely use its file-only path. Any failure
    after a gateway answers is raised so the CLI never silently edits only files.

    *payload* is sent as the request's JSON body when given, and is how an action
    whose behavior depends on a flag carries it across. ``uninstall`` needs this:
    its handler reads ``purge_data`` from the body and treats an absent or
    malformed body as "preserve data", so a bodyless request would turn a CLI
    ``--purge-data`` into a silent data-preserving uninstall. Actions with no flag
    pass ``None`` and send no body at all.
    """
    encoded_name = urllib.parse.quote(app_name, safe="")
    return _post_lifecycle_action(
        f"/api/apps/{encoded_name}/{action}", action, app_name, payload=payload
    )


def install_app(source: str) -> dict[str, object] | None:
    """Install the app directory *source* through the owner-only dashboard socket.

    Same contract as :func:`toggle_app`: ``None`` means no gateway received the
    request, so the CLI may install from files alone; any failure after a gateway
    answers is raised. The gateway resolves ``source`` against its OWN working
    directory, which is not this shell's, so the path is made absolute here.
    """
    resolved = str(Path(source).expanduser().resolve())
    return _post_lifecycle_action(
        "/api/apps/install", "install", resolved, payload={"source": resolved}
    )


def _gateway_peer_check(port: int):
    """A connect-time check that the socket is answered by the gateway recorded for *port*.

    The socket file sits in a directory its owner can rewrite, so a same-UID
    process could rebind it. Before any byte is written, the kernel-reported peer
    pid must equal the gateway recorded for *port* with a matching start token
    (``cli_server_peer._connected_unix_peer_is_recorded_gateway``, the same
    recorded-identity check the unix arm of ``_owner_verified_secret_urlopen``
    applies). Raises :class:`AppGatewayError`, which is not
    an ``OSError``, so the refusal is never read as "no gateway" and never drops the
    CLI to its file-only path.
    """
    from kiro_crew.cli_server_peer import (
        _audit_peer_allowed,
        _audit_peer_denied,
        _connected_unix_peer_is_recorded_gateway,
    )

    def _verify(sock: object) -> None:
        # Both arms of this permission decision are audited, in the same SEL
        # shape the CLI's other connection-bound proofs emit.
        if not _connected_unix_peer_is_recorded_gateway(sock, port):
            _audit_peer_denied(
                "app_lifecycle", port, "connected socket peer is not this install's gateway"
            )
            raise AppGatewayError(
                "the dashboard socket is not answered by this install's recorded "
                "gateway; nothing was sent. If this is your own gateway, restart it "
                "to re-publish its identity record."
            )
        _audit_peer_allowed("app_lifecycle", port)

    return _verify


def _post_lifecycle_action(
    route: str, action: str, subject: str, *, payload: dict[str, object] | None
) -> dict[str, object] | None:
    """POST one app lifecycle request through the dashboard socket.

    *route* is the dashboard path, *action* names the verb in progress and error
    text, and *subject* names what it acts on (an app name, or a source path).
    """
    port, _evidence_backed = resolve_client_port_ex(None)
    # This request travels the owner-only Unix SOCKET (unix_socket_urlopen below),
    # not TCP loopback -- the http://127.0.0.1 base is only the nominal URL on the
    # request line. So the credential is NOT paired to a dialled TCP address: pass
    # no dial_host and read the port-keyed secret. Threading a dial_host here would
    # make read_local_secret demand a listener sidecar for a TCP family the gateway
    # need never publish (a bind to a specific interface or ``::`` has no
    # 127.0.0.1 entry), returning "" for a live socket and silently dropping
    # uninstall to its file-only path while the backend keeps running.
    secret = read_local_secret(port)
    if not secret:
        return None

    socket_path = dashboard_socket_path(port)
    verify_peer = _gateway_peer_check(port)
    base = "http://127.0.0.1:%d" % port
    mint = urllib.request.Request(
        f"{base}/api/token/local?ttl=2m", headers={"X-Local-Secret": secret}
    )
    try:
        with unix_socket_urlopen(
            mint, timeout=5, socket_path=socket_path, verify_peer=verify_peer
        ) as response:
            minted = _read_json(response)
    except AppGatewayError:
        raise
    except urllib.error.HTTPError as exc:
        raise _gateway_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 — peer bytes must not crash the CLI
        if isinstance(exc, OSError) and _socket_unavailable(exc):
            return None
        detail = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise AppGatewayError(f"could not mint local dashboard credential: {detail}") from exc

    credential = minted.get("token") if isinstance(minted, dict) else None
    if not isinstance(credential, str) or not credential:
        raise AppGatewayError("gateway returned an empty local dashboard credential")

    encoded_credential = urllib.parse.quote(credential, safe="")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{route}?token={encoded_credential}",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    print(
        f"… applying {action} for {safe_terminal_line(subject)} through the running gateway "
        f"(may take up to {_ACTION_TIMEOUT_SECS // 60} minutes)",
        file=sys.stderr,
    )
    try:
        with unix_socket_urlopen(
            request,
            timeout=_ACTION_TIMEOUT_SECS,
            socket_path=socket_path,
            verify_peer=verify_peer,
        ) as response:
            result = _read_json(response)
    except AppGatewayError:
        raise
    except urllib.error.HTTPError as exc:
        raise _gateway_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 — peer bytes must not crash the CLI
        if deadline_expired(exc):
            raise AppGatewayTimeout(
                f"the gateway did not finish {action} for {subject} within "
                f"{_ACTION_TIMEOUT_SECS} s; it may still be applying it. Check "
                f"`kirocrew app list` or the dashboard before re-running the command, "
                f"because re-running while it is still in progress applies it twice"
            ) from exc
        raise AppGatewayError(
            "gateway stopped answering before the action completed; check the dashboard"
        ) from exc

    if not isinstance(result, dict):
        raise AppGatewayError("gateway returned an invalid app lifecycle response")
    if result.get("ok") is False:
        response_detail = result.get("error")
        response_code = result.get("code")
        raise AppGatewayError(
            (
                str(response_detail)
                if response_detail
                else "gateway rejected the app lifecycle request"
            ),
            code=response_code if isinstance(response_code, str) else "",
        )
    return result


def print_result(action: str, app_name: str, result: dict[str, object]) -> None:
    """Render a live app lifecycle response through one terminal sanitizer."""
    message = result.get("message")
    rendered_message = (
        message
        if isinstance(message, str)
        else f"{action.title()}{'d' if action.endswith('e') else 'ed'} {app_name}"
    )
    print(f"✅ {safe_terminal_line(rendered_message)}")

    warnings = result.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, str):
                print(f"⚠️  {safe_terminal_line(warning)}", file=sys.stderr)

    registration = result.get("registration")
    if isinstance(registration, dict):
        errors = registration.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str):
                    print(f"⚠️  {safe_terminal_line(error)}", file=sys.stderr)

    if action == "install":
        # Same lines the file-only install prints, so both paths read alike.
        if isinstance(registration, dict):
            for key, label in (
                ("agents", "Agents: "),
                ("skills", "Skills: "),
                ("crons", "Crons:  "),
            ):
                names = registration.get(key)
                if isinstance(names, list) and names:
                    joined = ", ".join(str(name) for name in names)
                    print(f"   {label}{safe_terminal_line(joined)}")
        if result.get("notice") == "session_approval_reconsent":
            # The gateway left the app off until its session-approval request is
            # accepted, so nothing above is running yet.
            print("   This app asks for session approval, so it stays off until enabled.")
        # An install records the app as not enabled, wherever it ran.
        print(f"\n   Run: kirocrew app enable {safe_terminal_line(app_name)}")
        return

    if action != "enable":
        return

    if isinstance(registration, dict):
        agents = registration.get("agents")
        skills = registration.get("skills")
        if isinstance(agents, list):
            print(f"   Agents registered: {len(agents)}")
        if isinstance(skills, list):
            print(f"   Skills registered: {len(skills)}")
    backend = result.get("backend")
    if isinstance(backend, dict) and isinstance(backend.get("port"), int):
        status = "healthy" if backend.get("healthy") else "starting"
        print(f"   Backend: port {backend['port']} ({status})")
