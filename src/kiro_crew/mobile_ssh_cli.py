"""CLI for mobile SSH enrollment and the forced-command stdio bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from kiro_crew.mobile_ssh import MOBILE_BRIDGE_COMMAND, MOBILE_ERROR_SCHEMA

_REQUEST_TIMEOUT_SECS = 10
_CONNECT_TIMEOUT_SECS = 5
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_PUBLIC_KEY_BYTES = 2048
_CHUNK_BYTES = 64 * 1024
BRIDGE_HOST = "127.0.0.1"


class _CliError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _document(code: str, message: str) -> str:
    return json.dumps(
        {"schema": MOBILE_ERROR_SCHEMA, "ok": False, "code": code, "error": message},
        sort_keys=True,
        separators=(",", ":"),
    )


def _emit(document: dict[str, object]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def _error(code: str, message: str) -> int:
    print(_document(code, message))
    return 1


def _read_public_key(path: str) -> str:
    if path == "-":
        text = sys.stdin.read(_MAX_PUBLIC_KEY_BYTES)
    else:
        from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            raise _CliError("public_key_unreadable", "public key file could not be read")
        try:
            data = safe_read_file_bytes_nolink(expanded, max_bytes=_MAX_PUBLIC_KEY_BYTES)
        except FileTooLargeError as exc:
            raise _CliError("public_key_too_large", "public key file is too large") from exc
        except OSError as exc:
            raise _CliError("public_key_unreadable", "public key file could not be read") from exc
        if data is None:
            raise _CliError("public_key_path_refused", "public key file is on a protected path")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _CliError("public_key_unreadable", "public key file could not be read") from exc
    if "PRIVATE KEY" in text:
        raise _CliError("public_key_is_private", "that is a private key; pass the .pub public key")
    return text


def _call_gateway(
    port: int, path: str, *, method: str, payload: dict[str, object] | None = None
) -> dict[str, Any]:
    from kiro_crew.config.loader import read_local_secret
    from kiro_crew.loopback_http import loopback_urlopen
    from kiro_crew.port_resolution import port_is_gateway_owned

    if not port_is_gateway_owned(port):
        raise _CliError(
            "gateway_unverified", "the listener on this port is not this user's gateway"
        )
    secret = read_local_secret(port, dial_host=BRIDGE_HOST)
    if not secret:
        raise _CliError("local_auth_unavailable", "gateway local authentication is unavailable")
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"X-Local-Secret": secret}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{BRIDGE_HOST}:{port}{path}", data=body, headers=headers, method=method
    )
    try:
        with loopback_urlopen(request, timeout=_REQUEST_TIMEOUT_SECS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _CliError("gateway_error", "gateway rejected the request") from exc
        if not isinstance(document, dict):
            raise _CliError("gateway_error", "gateway rejected the request") from exc
        raise _CliError(
            str(document.get("code") or "gateway_error"),
            str(document.get("error") or "gateway rejected the request"),
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _CliError("gateway_unavailable", "gateway is not reachable") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _CliError("gateway_response_too_large", "gateway response was too large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _CliError("gateway_response_invalid", "gateway returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise _CliError("gateway_response_invalid", "gateway returned invalid JSON")
    return document


def _require_ssh_context() -> None:
    """Refuse anything but the client's bridge exec request under sshd."""
    if os.environ.get("SSH_ORIGINAL_COMMAND", "") != MOBILE_BRIDGE_COMMAND:
        raise _CliError("bridge_command_required", f"request the {MOBILE_BRIDGE_COMMAND} command")
    if os.environ.get("SSH_TTY"):
        raise _CliError("bridge_pty_refused", "the bridge does not run on a terminal")
    fields = os.environ.get("SSH_CONNECTION", "").split()
    try:
        if len(fields) != 4:
            raise ValueError("SSH_CONNECTION")
        ipaddress.ip_address(fields[0])
        ipaddress.ip_address(fields[2])
        if not (1 <= int(fields[1]) <= 65535 and 1 <= int(fields[3]) <= 65535):
            raise ValueError("SSH_CONNECTION")
    except ValueError as exc:
        raise _CliError(
            "forced_command_required", "the bridge runs only as an sshd command"
        ) from exc


def _use_enrolled_home(home: str | None) -> None:
    if not home or not os.path.isabs(home) or not os.path.isdir(home):
        raise _CliError("invalid_home", "enrolled Kiro Crew data home is unavailable")
    os.environ["KIROCREW_HOME"] = home


def pin_enrolled_home(args: argparse.Namespace) -> int:
    """Apply the bridge's ``--home`` before the CLI prelude resolves the default home."""
    if getattr(args, "mobile_ssh_action", None) != "bridge":
        return 0
    try:
        _use_enrolled_home(getattr(args, "home", None))
    except _CliError as exc:
        sys.stderr.write(_document(exc.code, exc.message) + "\n")
        return 1
    return 0


def _bridge_target(device_id: str, port: int, home: str) -> tuple[str, int]:
    """Resolve the one address the bridge may dial, or refuse."""
    from kiro_crew.mobile_ssh import MobileSshDeviceStore, MobileSshError
    from kiro_crew.port_resolution import port_is_gateway_owned

    try:
        device = MobileSshDeviceStore(Path(home)).device(device_id)
    except MobileSshError as exc:
        raise _CliError(exc.code, exc.message) from exc
    if device is None:
        raise _CliError("device_not_found", "device is not enrolled")
    if not device.active:
        raise _CliError("device_revoked", "device enrollment is revoked")
    if int(device.gateway_port) != port:
        raise _CliError("port_mismatch", "the bridge port does not match the enrollment")
    if not port_is_gateway_owned(port):
        raise _CliError(
            "gateway_unverified", "the listener on this port is not this user's gateway"
        )
    return BRIDGE_HOST, port


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def relay(sock: socket.socket, stdin_fd: int, stdout_fd: int) -> None:
    """Copy bytes both ways until the gateway side closes."""

    def upstream() -> None:
        try:
            while True:
                data = os.read(stdin_fd, _CHUNK_BYTES)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, name="mobile-ssh-bridge", daemon=True).start()
    try:
        while True:
            data = sock.recv(_CHUNK_BYTES)
            if not data:
                break
            _write_all(stdout_fd, data)
    except OSError:
        pass
    finally:
        sock.close()


def run_bridge(args: argparse.Namespace) -> int:
    try:
        _require_ssh_context()
        port = int(args.port)
        if not 1 <= port <= 65535:
            raise _CliError("invalid_port", "gateway port must be between 1 and 65535")
        host, port = _bridge_target(args.device_id, port, os.environ["KIROCREW_HOME"])
        try:
            sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECS)
        except OSError as exc:
            raise _CliError("gateway_unavailable", "gateway is not reachable") from exc
        sock.settimeout(None)
    except _CliError as exc:
        # stdout is the HTTP stream; a refusal goes to stderr and leaves it empty.
        sys.stderr.write(_document(exc.code, exc.message) + "\n")
        sys.stderr.flush()
        return 1
    relay(sock, sys.stdin.fileno(), sys.stdout.fileno())
    return 0


def run_mobile_ssh(args: argparse.Namespace) -> int:
    action = getattr(args, "mobile_ssh_action", None)
    if action == "bridge":
        return run_bridge(args)
    if action not in {"enroll", "list", "revoke"}:
        return _error("missing_action", "choose enroll, list, or revoke")
    from kiro_crew.port_resolution import resolve_client_port

    try:
        port = resolve_client_port(args.port)
        if action == "enroll":
            result = _call_gateway(
                port,
                "/api/mobile/ssh/enroll",
                method="POST",
                payload={
                    "device_id": args.device_id,
                    "label": args.label,
                    "public_key": _read_public_key(args.public_key_file),
                },
            )
        elif action == "list":
            result = _call_gateway(port, "/api/mobile/ssh/devices", method="GET")
        else:
            result = _call_gateway(
                port, "/api/mobile/ssh/revoke", method="POST", payload={"device_id": args.device_id}
            )
    except _CliError as exc:
        return _error(exc.code, exc.message)
    _emit(result)
    return 0
