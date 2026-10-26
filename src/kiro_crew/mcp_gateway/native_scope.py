"""Host-issued authority for one native Windows runtime's broker stubs.

The agent can read the envelope but cannot change its session, launch or agent.
The protected SEL root is only read outside MXC; no new trust file is created.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

SCOPE_ENV = "KIROCREW_NATIVE_MCP_SCOPE_TOKEN"
DESCRIPTOR_ENV = "KIROCREW_NATIVE_MCP_DESCRIPTOR"
REQUIRED_ENV = "KIROCREW_MCP_REQUIRE_GATEWAY"
DESCRIPTOR_NAME = "native-mcp-handles.json"
_DOMAIN = b"kirocrew-native-mcp-scope-v1\0"
_FIELDS = (
    "server_name",
    "agent_name",
    "command_args_hash",
    "effective_env_hash",
    "work_dir",
    "binary_version",
    "os_uid",
    "sandbox_mode",
    "autoapprove_set_hash",
    "approval_mode",
    "trust_all_tools",
    "poolable",
    "channel_id",
    "config_snapshot_hash",
    "stub_code_fingerprint",
)
_MAX_SCOPE_BYTES = 16_384
_REGISTER_FIELDS = frozenset(_FIELDS) | {
    "type",
    "stub_uuid",
    "stub_session_token",
    "caller",
    "ancestor_pids",
    "parent_pid",
    "session_key",
    "session_type",
    "principal_id",
    "user_identity",
}


def _key() -> bytes:
    from kiro_crew.session_pid_sig import _load_hmac_key

    root = _load_hmac_key()
    if root is None:
        raise PermissionError("Native MCP host signing authority is unavailable")
    return hmac.new(root, _DOMAIN, hashlib.sha256).digest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def issue(register: dict[str, Any], instance: str, *, now: float | None = None) -> str:
    """Sign only the host-derived final stub declaration, never an agent request."""
    token = register.get("stub_session_token")
    if not isinstance(token, str) or not token or not instance:
        raise PermissionError("Native MCP requires a session-bound launch")
    body = {
        "version": 1,
        "instance": instance,
        "expires": int(time.time() if now is None else now) + 86_400,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "fields": {key: register.get(key) for key in _FIELDS},
    }
    record = {"body": body, "mac": hmac.new(_key(), _canonical(body), hashlib.sha256).hexdigest()}
    raw = _canonical(record).decode("ascii")
    if len(raw) > _MAX_SCOPE_BYTES:
        raise ValueError("Native MCP launch scope exceeds its limit")
    return raw


def verify(raw: str, register: dict[str, Any], instance: str, *, now: float | None = None) -> bool:
    """Reject expired, changed, foreign-runtime and cross-session declarations."""
    try:
        if register.get("type") != "register" or set(register) - _REGISTER_FIELDS:
            return False
        if not isinstance(raw, str) or len(raw) > _MAX_SCOPE_BYTES:
            return False
        record = json.loads(raw)
        body, mac = record["body"], record["mac"]
        if not hmac.compare_digest(
            mac, hmac.new(_key(), _canonical(body), hashlib.sha256).hexdigest()
        ):
            return False
        token = register.get("stub_session_token")
        return (
            body["version"] == 1
            and body["instance"] == instance
            and type(body["expires"]) is int
            and body["expires"] > (time.time() if now is None else now)
            and isinstance(token, str)
            and bool(token)
            and hmac.compare_digest(body["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            and body["fields"] == {key: register.get(key) for key in _FIELDS}
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def canonical_register(register: dict[str, Any]) -> dict[str, Any]:
    """Never copy untrusted first-frame verbs or administrative extension fields."""
    return {
        **{key: register[key] for key in _FIELDS if key in register},
        "type": "register",
        "stub_uuid": register["stub_uuid"],
        "stub_session_token": register["stub_session_token"],
        "native_session": True,
        "caller": {},
        "ancestor_pids": [],
        "session_key": "",
        "session_type": "unknown",
        "principal_id": "",
        "user_identity": "unknown",
    }


def runtime_identity(
    raw: str, local_version: str, local_fingerprint: str | None
) -> tuple[str, str | None]:
    """Use host-declared code metadata when the confined stub cannot inspect Git.

    This parser grants no authority: the outside helper still verifies the MAC
    and every field. Preserve the independently read executable identity; only
    its host code-generation suffix can come from the signed declaration.
    """
    try:
        if len(raw) > _MAX_SCOPE_BYTES:
            raise ValueError("Scope exceeds its limit")
        fields = json.loads(raw)["body"]["fields"]
        version, fingerprint = fields["binary_version"], fields["stub_code_fingerprint"]
        if not isinstance(version, str) or not isinstance(fingerprint, (str, type(None))):
            raise ValueError("Missing runtime identity")
        base = (
            local_version.removesuffix("+" + local_fingerprint)
            if local_fingerprint
            else local_version
        )
        declared_base = version.removesuffix("+" + fingerprint) if fingerprint else version
        if base != declared_base:
            raise ValueError("Executable identity changed")
        return version, fingerprint
    except (KeyError, TypeError, ValueError):
        from kiro_crew.mcp_gateway.required import GatewayRequiredError

        raise GatewayRequiredError("identity_unattested") from None


def attach(
    entries: list[dict[str, Any]], scratch: Path, *, timeout: float = 15
) -> list[dict[str, Any]]:
    """Seal the mirror's FINAL broker entries; run off the host event loop."""
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
    from kiro_crew.mcp_gateway.stub import _parse_args
    from kiro_crew.mcp_gateway.stub_register import build_register_payload

    selected = [
        entry
        for entry in entries
        if entry.get("name") in KIROCREW_BIN_MCP_SERVERS
        and "kiro_crew.mcp_gateway.stub" in entry.get("args", [])
    ]
    if not selected:
        return entries
    descriptor = scratch / DESCRIPTOR_NAME
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = json.loads(descriptor.read_text("ascii"))
            if not all(key in info for key in ("pid", "start", "request", "response", "instance")):
                raise ValueError("Native MCP transport is not ready")
            break
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                raise ConnectionRefusedError("Native MCP handle transport did not become ready")
            time.sleep(0.02)
    result = []
    for entry in entries:
        if entry not in selected:
            result.append(entry)
            continue
        argv = list(entry["args"])
        args = _parse_args(argv[argv.index("kiro_crew.mcp_gateway.stub") + 1 :])
        env = {item["name"]: item["value"] for item in entry.get("env", [])}
        token = env.get(STUB_SESSION_TOKEN_ENV, "")
        # The final entry carries the channel in argv or env. The untrusted
        # in-sandbox caller walk does not construct authority.
        register = build_register_payload(
            args,
            session_token=token,
            caller={
                "session_key": "",
                "session_type": "unknown",
                "principal_id": "",
                "channel_id": "",
            },
        )
        # Codex creates stdio children from the entry's env, rather than the
        # host's ambient channel. Sign the same value the child will register.
        register["channel_id"] = args.channel_id or env.get("KIROCREW_CHANNEL_ID") or None
        env.update(
            {
                DESCRIPTOR_ENV: str(descriptor),
                SCOPE_ENV: issue(register, info["instance"]),
                REQUIRED_ENV: "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8:backslashreplace",
            }
        )
        result.append(
            {**entry, "env": [{"name": key, "value": value} for key, value in env.items()]}
        )
    return result
