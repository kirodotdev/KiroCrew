"""Client for live app lifecycle changes using ordinary dashboard authentication.

The local credential mint and the authenticated app action travel exclusively over
the dashboard's owner-only Unix socket. The gateway kernel-verifies the socket peer,
so neither credential can reach a foreign process bound to the resolved TCP port.
There is deliberately no TCP fallback and no MCP/internal-secret authorization path.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.loopback_http import unix_socket_urlopen
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.terminal_safe import safe_terminal_line

# A legal enable can spend 120 s provisioning npm dependencies, about 105 s in
# backend health checks, and another 30 s in the default onEnable hook. Leave
# enough headroom for registration and capability dependency resolution.
_ACTION_TIMEOUT_SECS = 300


class AppGatewayError(RuntimeError):
    """A running gateway refused or could not complete an app lifecycle request."""

    def __init__(self, message: str) -> None:
        # This exception is rendered by cli_commands on ONE prefixed line, so the
        # terminal boundary is here even when the message came from an HTTP error
        # or response payload, and a line break in it must not start a new line.
        super().__init__(safe_terminal_line(message))


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


def _gateway_error_detail(exc: urllib.error.HTTPError) -> str:
    """Return the gateway's structured error text, falling back to its status."""
    try:
        body = json.loads(exc.read())
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body["error"]
    except Exception:
        pass
    return f"{exc.code} {exc.reason}"


def _socket_unavailable(exc: OSError) -> bool:
    """Whether strict Unix transport proved that no gateway received the request."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (FileNotFoundError, ConnectionRefusedError)):
        return True
    return (
        isinstance(reason, OSError) and "AF_UNIX" in str(reason) and "not available" in str(reason)
    )


def _deadline_expired(exc: BaseException) -> bool:
    """Whether the action request ended because OUR deadline passed, not the peer's.

    ``urllib`` surfaces a read timeout as ``TimeoutError`` (``socket.timeout``) and a
    connect timeout wrapped in ``URLError.reason``; either means the gateway may
    still be working, which is the one outcome the CLI must not report as a refusal.
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, TimeoutError)


def toggle_app(app_name: str, action: str) -> dict[str, object] | None:
    """Apply an app lifecycle action through the owner-only dashboard socket.

    The resolver's port names the per-instance socket; its evidence source does
    not gate the attempt because the socket's location and peer check provide the
    ownership proof. ``None`` means no socket endpoint accepted the request (or no
    local secret exists), so the CLI may safely use its file-only path. Any failure
    after a gateway answers is raised so the CLI never silently edits only files.
    """
    port, _evidence_backed = resolve_client_port_ex(None)
    secret = read_local_secret(port)
    if not secret:
        return None

    socket_path = dashboard_socket_path(port)
    base = "http://127.0.0.1:%d" % port
    mint = urllib.request.Request(
        f"{base}/api/token/local?ttl=2m", headers={"X-Local-Secret": secret}
    )
    try:
        with unix_socket_urlopen(mint, timeout=5, socket_path=socket_path) as response:
            minted = _read_json(response)
    except AppGatewayError:
        raise
    except urllib.error.HTTPError as exc:
        raise AppGatewayError(_gateway_error_detail(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — peer bytes must not crash the CLI
        if isinstance(exc, OSError) and _socket_unavailable(exc):
            return None
        detail = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise AppGatewayError(f"could not mint local dashboard credential: {detail}") from exc

    credential = minted.get("token") if isinstance(minted, dict) else None
    if not isinstance(credential, str) or not credential:
        raise AppGatewayError("gateway returned an empty local dashboard credential")

    encoded_name = urllib.parse.quote(app_name, safe="")
    encoded_credential = urllib.parse.quote(credential, safe="")
    request = urllib.request.Request(
        f"{base}/api/apps/{encoded_name}/{action}?token={encoded_credential}",
        method="POST",
    )
    print(
        f"… applying {action} for {app_name} through the running gateway "
        f"(may take up to {_ACTION_TIMEOUT_SECS // 60} minutes)",
        file=sys.stderr,
    )
    try:
        with unix_socket_urlopen(
            request, timeout=_ACTION_TIMEOUT_SECS, socket_path=socket_path
        ) as response:
            result = _read_json(response)
    except AppGatewayError:
        raise
    except urllib.error.HTTPError as exc:
        raise AppGatewayError(_gateway_error_detail(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — peer bytes must not crash the CLI
        if _deadline_expired(exc):
            raise AppGatewayTimeout(
                f"the gateway did not finish {action} for {app_name} within "
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
        raise AppGatewayError(
            str(response_detail)
            if response_detail
            else "gateway rejected the app lifecycle request"
        )
    return result


def print_result(action: str, app_name: str, result: dict[str, object]) -> None:
    """Render a live app lifecycle response through one terminal sanitizer."""
    message = result.get("message")
    rendered_message = message if isinstance(message, str) else f"{action.title()}d {app_name}"
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
