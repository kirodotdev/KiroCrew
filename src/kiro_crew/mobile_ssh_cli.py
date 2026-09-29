"""CLI transport for the host-side mobile SSH contract."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from kiro_crew.config.loader import read_local_secret
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mobile_ssh import MOBILE_ERROR_SCHEMA, MOBILE_ORIGINAL_COMMAND
from kiro_crew.port_resolution import port_is_gateway_owned, resolve_client_port

_REQUEST_TIMEOUT_SECS = 10
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_PUBLIC_KEY_BYTES = 2048


class _GatewayCallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _emit(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def _error(code: str, message: str) -> int:
    _emit({"schema": MOBILE_ERROR_SCHEMA, "ok": False, "code": code, "error": message})
    return 1


def _read_public_key(path: str) -> str:
    if path == "-":
        text = sys.stdin.read(_MAX_PUBLIC_KEY_BYTES)
    else:
        from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            raise _GatewayCallError("public_key_unreadable", "public key file could not be read")
        try:
            data = safe_read_file_bytes_nolink(expanded, max_bytes=_MAX_PUBLIC_KEY_BYTES)
        except FileTooLargeError as exc:
            raise _GatewayCallError("public_key_too_large", "public key file is too large") from exc
        except OSError as exc:
            raise _GatewayCallError(
                "public_key_unreadable", "public key file could not be read"
            ) from exc
        if data is None:
            raise _GatewayCallError(
                "public_key_path_refused", "public key file is on a protected path"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _GatewayCallError(
                "public_key_unreadable", "public key file could not be read"
            ) from exc
    if "PRIVATE KEY" in text:
        raise _GatewayCallError(
            "public_key_is_private", "that is a private key; pass the .pub public key"
        )
    return text


def _call_gateway(
    port: int, path: str, *, method: str, payload: dict[str, object] | None = None
) -> dict[str, Any]:
    if not port_is_gateway_owned(port):
        raise _GatewayCallError(
            "gateway_unverified", "the listener on this port is not this user's gateway"
        )
    secret = read_local_secret(port, dial_host="127.0.0.1")
    if not secret:
        raise _GatewayCallError(
            "local_auth_unavailable", "gateway local authentication is unavailable"
        )
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"X-Local-Secret": secret}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method
    )
    try:
        with loopback_urlopen(request, timeout=_REQUEST_TIMEOUT_SECS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _GatewayCallError("gateway_error", "gateway rejected the request") from exc
        if not isinstance(document, dict):
            raise _GatewayCallError("gateway_error", "gateway rejected the request") from exc
        raise _GatewayCallError(
            str(document.get("code") or "gateway_error"),
            str(document.get("error") or "gateway rejected the request"),
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _GatewayCallError("gateway_unavailable", "gateway is not reachable") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _GatewayCallError("gateway_response_too_large", "gateway response was too large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _GatewayCallError(
            "gateway_response_invalid", "gateway returned invalid JSON"
        ) from exc
    if not isinstance(document, dict):
        raise _GatewayCallError("gateway_response_invalid", "gateway returned invalid JSON")
    return document


def _forced_command_context_valid() -> bool:
    """Reject direct or PTY invocations; the enrollment binding is the real proof."""
    if os.environ.get("SSH_ORIGINAL_COMMAND", "") != MOBILE_ORIGINAL_COMMAND:
        return False
    if os.environ.get("SSH_TTY"):
        return False
    fields = os.environ.get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        return False
    try:
        ipaddress.ip_address(fields[0])
        ipaddress.ip_address(fields[2])
        client_port = int(fields[1])
        server_port = int(fields[3])
    except (ValueError, TypeError):
        return False
    return 1 <= client_port <= 65535 and 1 <= server_port <= 65535


MOBILE_PAIRING_PREFIX = "kiro-crew://c/"


def pairing_uri(enrollment: dict[str, Any]) -> str:
    """The Kiro Mobile pairing code for an enrollment; it carries no credential."""
    payload = {
        "version": 1,
        "host": enrollment["ssh_host"],
        "sshPort": enrollment["ssh_port"],
        "username": enrollment["ssh_username"],
        "remoteGatewayPort": enrollment["gateway_port"],
        "hostKeyFingerprint": enrollment["host_key_fingerprint"],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return MOBILE_PAIRING_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _print_pairing_qr(uri: str) -> None:
    import qrcode  # noqa: PLC0415 - pulls Pillow; only the --qr path needs it

    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    # stdout stays one JSON document; the QR is for the person at the terminal.
    qr.print_ascii(out=sys.stderr, invert=True)
    sys.stderr.write("Scan in Kiro Mobile: Add a crew instance > Scan QR\n")
    sys.stderr.flush()


def _use_enrolled_home(home: str | None) -> None:
    if not home:
        return
    if not os.path.isabs(home) or not os.path.isdir(home):
        raise _GatewayCallError("invalid_home", "enrolled Kiro Crew data home is unavailable")
    os.environ["KIROCREW_HOME"] = home


def pin_enrolled_home(args: argparse.Namespace) -> int:
    """Apply a forced command's ``--home`` before the CLI prelude resolves the default home."""
    if getattr(args, "mobile_ssh_action", None) != "token":
        return 0
    try:
        _use_enrolled_home(getattr(args, "home", None))
    except _GatewayCallError as exc:
        return _error(exc.code, exc.message)
    return 0


def run_mobile_ssh(args: argparse.Namespace) -> int:
    action = getattr(args, "mobile_ssh_action", None)
    if action not in {"enroll", "list", "revoke", "token"}:
        return _error("missing_action", "choose enroll, list, revoke, or token")
    try:
        port = int(args.port) if action == "token" else resolve_client_port(args.port)
        if not 1 <= port <= 65535:
            raise _GatewayCallError("invalid_port", "gateway port must be between 1 and 65535")
        if action == "enroll":
            result = _call_gateway(
                port,
                "/api/mobile/ssh/enroll",
                method="POST",
                payload={
                    "device_id": args.device_id,
                    "label": args.label,
                    "ssh_host": args.ssh_host,
                    "public_key": _read_public_key(args.public_key_file),
                },
            )
        elif action == "list":
            result = _call_gateway(port, "/api/mobile/ssh/devices", method="GET")
        elif action == "revoke":
            result = _call_gateway(
                port,
                "/api/mobile/ssh/revoke",
                method="POST",
                payload={"device_id": args.device_id},
            )
        else:
            if not _forced_command_context_valid():
                return _error(
                    "forced_command_required",
                    "token minting requires the enrolled OpenSSH forced command",
                )
            _use_enrolled_home(getattr(args, "home", None))
            result = _call_gateway(
                port,
                "/api/mobile/ssh/token",
                method="POST",
                payload={"device_id": args.device_id, "binding": args.binding},
            )
    except _GatewayCallError as exc:
        return _error(exc.code, exc.message)
    _emit(result)
    if action == "enroll" and getattr(args, "qr", False) and result.get("ok") is True:
        _print_pairing_qr(pairing_uri(result))
    return 0
