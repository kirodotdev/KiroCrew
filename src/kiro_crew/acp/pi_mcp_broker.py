"""Host-side MCP broker for Pi sessions (clears GPT F1).

Pi's sandbox must never see secret-bearing server configuration. The sealed
bridge extension therefore does **not** spawn Crew MCP children itself and does
**not** read a servers JSON file inside the sandbox. Instead this module — which
runs in the unsandboxed ACP client / gateway process — holds the real
``{name, command, args, env}`` specs, spawns the stdio MCP children, and serves
a thin NDJSON IPC protocol over an owner-only local endpoint (unix socket /
named pipe via :mod:`kiro_crew.mcp_gateway.transport`).

The Pi bridge connects with only the endpoint address
(``KIROCREW_PI_MCP_BROKER_SOCK``); that address carries no credentials. Peer
admission reuses :func:`kiro_crew.mcp_gateway.socketsec.check_peer_is_self`.
Every call additionally consumes one host approval bound to its call id, tool
name and exact arguments; knowing the endpoint is not permission to run tools.

Soft-fail: if the broker cannot bind or no servers are supplied, callers leave
the session chat-only and must not advertise Crew tools as mounted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import functools
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional, TypeGuard
from urllib.parse import urlsplit

from kiro_crew import platform_compat, process_identity
from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text
from kiro_crew.acp.transport_framing import (
    RequestWriteResult,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.env import sanitize_spec_env
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway import socketsec, transport
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES
from kiro_crew.runtime_ownership import authorize_runtime_kill
from kiro_crew.sandbox import (
    _pinned_spawn_path,
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Env var naming the broker endpoint address the Pi bridge connects to.
#: On POSIX this is the unix-socket path; on Windows it is the named-pipe
#: address :func:`transport.resolve_address` derives. Never a secret.
ENV_BROKER_SOCK = "KIROCREW_PI_MCP_BROKER_SOCK"

# IPC + MCP-child stdout share the gateway's read ceiling. Asyncio's stdlib
# default (64 KiB) truncates a real ``kirocrew-core`` tools/list (~100 KiB+ of
# schemas) mid-frame and surfaces as a false "exited" handshake failure while
# leaner siblings like ``kirocrew-cron`` still mount.
_DEFAULT_READ_LIMIT = READ_BUFFER_LIMIT_BYTES
_HANDSHAKE_TIMEOUT_SECS = 30.0
_CALL_TIMEOUT_SECS = 120.0
_STARTUP_TIMEOUT_SECS = 60.0
_CHILD_STARTUP_TIMEOUT_SECS = 30.0
_STARTUP_CONCURRENCY = 8
_DELIVERY_TIMEOUT_SECS = 5.0
_WRITE_PROGRESS_BOUND_SECS = 5.0
_TEARDOWN_RETRY_DELAY_SECS = 30.0
_FAILED_TEARDOWN_TASKS: set[asyncio.Task[None]] = set()
# Bound the roster before retaining specifications or creating child tasks.
# A 64 KiB spec accommodates ordinary command/env configuration while keeping
# the broker's retained configuration below 4 MiB.
_SERVER_SPEC_MAX_COUNT = 64
_SERVER_SPEC_MAX_BYTES = 64 * 1024
_SERVER_NAME_MAX_BYTES = 512
_MAX_ACTIVE_CLIENTS = 64
_BRIDGE_RESPONSE_MAX_BYTES = 7 * 1024 * 1024
_BRIDGE_ERROR_MAX_CHARS = 4096
_APPROVAL_MAX_OUTSTANDING = 32
_APPROVAL_REQUEST_ID_MAX_BYTES = 256
_APPROVAL_CALL_ID_MAX_BYTES = 256
_APPROVAL_TITLE_MAX_BYTES = 2048
_APPROVAL_ARGS_MAX_BYTES = 2 * 1024 * 1024
# Pi's bridge receive buffer is 8 MiB. Leave envelope headroom and reject a
# server whose schemas cannot fit, without losing already admitted siblings.
_TOOL_INDEX_MAX_BYTES = 7 * 1024 * 1024
_TOOL_DESCRIPTION_MAX_CHARS = 4096
_TOOL_DESCRIPTION_MAX_BYTES = 64 * 1024
_TOOL_LIST_MAX_COUNT = 4096
_TOOL_NAME_MAX_BYTES = 512
_TOOL_SCHEMA_MAX_BYTES = 256 * 1024
# Retain enough verified group members for ordinary child fan-out without
# allowing a long-lived server to accumulate every helper it ever spawned.
_DESCENDANT_IDENTITY_MAX_COUNT = 1024
_CHILD_B64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{8,}={0,2}(?![A-Za-z0-9+/_-])")
_NON_SECRET_ENV_KEYS = frozenset({"KIROCREW_BOUND_PORT", "KIROCREW_CHANNEL_ID"})
_SECRET_ENV_KEY_SEGMENTS = frozenset(
    {
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "AUTH",
        "KEY",
        "PASS",
        "PIN",
        "PASSWD",
        "COOKIE",
        "COOKIES",
    }
)
# Compound spellings one segment hides: APIKEY, CLIENTSECRET, GHTOKEN. Matched as
# a segment SUFFIX, and only for markers long enough that an ordinary word cannot
# end in one -- "PASS" would claim BYPASS and "PIN" would claim PING, so those two
# stay exact-segment only. "KEY" admits MONKEY; an env key spelled that way costs
# one over-redacted declared value, where missing APIKEY costs a leaked one.
_SECRET_ENV_KEY_SUFFIXES = (
    "TOKEN",
    "TOKENS",
    "SECRET",
    "SECRETS",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "CREDENTIAL",
    "CREDENTIALS",
    "KEY",
    "KEYS",
)
# A value under a key that names nothing is still a credential when it is SHAPED
# like one. Sized so ordinary declared metadata -- a hostname, a region, a log
# level, a version, a path, or a URL, is never mistaken for a token.
_OPAQUE_SECRET_MIN_CHARS = 20
_LONG_HEX_VALUE = re.compile(r"\A[0-9a-fA-F]{32,}\Z")
# Above this the shape test stops: no credential is this long, and running the
# matcher over an oversized declared blob would put that cost on the gateway
# loop. _redact_metadata's unconditional redact_credentials pass still covers a
# credential pattern inside it.
_CREDENTIAL_SHAPE_MAX_CHARS = 8192


class _ToolMetadataOverflow(ValueError):
    """A child's tool list cannot fit the bounded bridge metadata."""


class _BridgeResponseOverflow(ValueError):
    """A call result cannot fit Pi's bridge receive buffer."""


class _DescendantIdentityOverflow(RuntimeError):
    """A child has more verified descendants than teardown can retain."""


def _failure_name(spec: dict[str, Any], index: int) -> str:
    """Name a rejected spec without retaining an unbounded untrusted name."""
    name = spec.get("name")
    if isinstance(name, str) and name and len(name) <= _SERVER_NAME_MAX_BYTES:
        try:
            if len(name.encode("utf-8")) <= _SERVER_NAME_MAX_BYTES:
                return name
        except UnicodeError:
            pass
    return f"server[{index}]"


def _fits_raw_json_budget(value: Any, limit: int) -> bool:
    """Reject a giant string/container before JSON encoding copies its content."""
    remaining = limit
    pending: list[Any] = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            remaining -= len(item)
        elif isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                return False
            seen.add(id(item))
            remaining -= len(item)
            if remaining < 0:
                return False
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            else:
                pending.extend(item)
        else:
            remaining -= 1
        if remaining < 0:
            return False
    return True


def _encode_bridge_frame(obj: dict[str, Any], max_bytes: int | None) -> bytes:
    """Encode a response off the gateway loop, enforcing Pi's receive budget."""
    if max_bytes is None:
        return (json.dumps(obj, separators=(",", ":")) + "\n").encode()
    if not _fits_raw_json_budget(obj, max_bytes):
        raise _BridgeResponseOverflow("MCP tool result exceeds bridge size limit")
    chunks: list[str] = []
    size = 1  # Newline.
    for chunk in json.JSONEncoder(separators=(",", ":")).iterencode(obj):
        size += len(chunk)  # ensure_ascii=True, so characters are bytes.
        if size > max_bytes:
            raise _BridgeResponseOverflow("MCP tool result exceeds bridge size limit")
        chunks.append(chunk)
    return ("".join(chunks) + "\n").encode()


def _bounded_server_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Snapshot a JSON server spec only while its serialized size fits."""
    if not _fits_raw_json_budget(spec, _SERVER_SPEC_MAX_BYTES):
        return None
    chunks: list[str] = []
    size = 0
    try:
        # ensure_ascii=True makes each emitted character one serialized byte.
        # Stop before joining or retaining an oversized specification.
        for chunk in json.JSONEncoder(separators=(",", ":")).iterencode(spec):
            size += len(chunk)
            if size > _SERVER_SPEC_MAX_BYTES:
                return None
            chunks.append(chunk)
        snapshot = json.loads("".join(chunks))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _bounded_disabled_tools_for_server(
    disabled: Iterable[tuple[str, str]], server_name: str
) -> list[str] | None:
    """Collect a server's restrictions without building an unbounded entry."""
    names: list[str] = []
    remaining = _SERVER_SPEC_MAX_BYTES
    for server, tool in disabled:
        if server != server_name:
            continue
        remaining -= len(tool)
        if remaining < 0 or len(names) >= _SERVER_SPEC_MAX_BYTES:
            return None
        names.append(tool)
    return sorted(names)


@dataclass(frozen=True)
class AdmittedServerRoster:
    """The broker's bounded snapshot, including names for unavailable entries."""

    specs: tuple[dict[str, Any], ...]
    failures: dict[str, str]
    expected_names: tuple[str, ...]


class ServerRosterAdmission:
    """Admit one bounded spec at a time before the Pi spawn retains it."""

    def __init__(self) -> None:
        self.specs: list[dict[str, Any]] = []
        self.failures: dict[str, str] = {}
        self.expected_names: list[str] = []
        self.count = 0
        self.overflowed = False

    @property
    def max_count(self) -> int:
        return _SERVER_SPEC_MAX_COUNT

    @property
    def full(self) -> bool:
        return self.count >= self.max_count

    def note_overflow(self) -> None:
        self.overflowed = True

    def reject(self, spec: dict[str, Any], reason: str) -> None:
        index = self.count
        self.count += 1
        if index >= self.max_count:
            self.note_overflow()
            return
        name = _failure_name(spec, index)
        self.failures[name] = reason
        self.expected_names.append(name)

    def offer(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Return the retained snapshot only if this slot is valid and in budget."""
        index = self.count
        self.count += 1
        if index >= self.max_count:
            self.note_overflow()
            return None
        if not isinstance(spec, dict):
            name = f"server[{index}]"
            self.failures[name] = "MCP server specification is invalid"
            self.expected_names.append(name)
            return None
        snapshot = _bounded_server_spec(spec)
        if snapshot is None:
            name = _failure_name(spec, index)
            self.failures[name] = "MCP server specification exceeds size limit or is invalid"
            self.expected_names.append(name)
            return None
        raw_name = snapshot.get("name")
        if (
            not isinstance(raw_name, str)
            or not _bounded_text(raw_name, _SERVER_NAME_MAX_BYTES)
            or raw_name != raw_name.strip()
            or "__" in raw_name
        ):
            name = _failure_name(spec, index)
            self.failures[name] = "MCP server name exceeds size limit or is invalid"
            self.expected_names.append(name)
            return None
        self.specs.append(snapshot)
        self.expected_names.append(raw_name)
        return snapshot

    def finish(self) -> AdmittedServerRoster:
        """Freeze the bounded broker roster and its bounded report labels."""
        expected_names = self.expected_names
        failures = self.failures
        if self.overflowed:
            # Reports normalize names before recording them, so the summary must
            # differ from every admitted name after that normalization too.
            reported_names = {sanitize_sink_text(name, NAME_CAP) for name in expected_names}
            overflow_name = "<additional servers>"
            suffix = 1
            while overflow_name in reported_names:
                overflow_name = f"<additional servers {suffix}>"
                suffix += 1
            failures[overflow_name] = "MCP server count limit exceeded"
            expected_names.append(overflow_name)
        return AdmittedServerRoster(
            tuple(self.specs), dict(failures), tuple(dict.fromkeys(expected_names))
        )


def admit_server_roster(servers: list[dict[str, Any]]) -> AdmittedServerRoster:
    """Bound and validate the roster once for broker launch and session reports."""
    admission = ServerRosterAdmission()
    for spec in servers[: admission.max_count + 1]:
        admission.offer(spec)
    return admission.finish()


def _bounded_text(value: Any, max_bytes: int) -> TypeGuard[str]:
    if not isinstance(value, str) or not value or len(value) > max_bytes:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def _bounded_approval_args(arguments: dict[str, Any]) -> str | None:
    if not _fits_raw_json_budget(arguments, _APPROVAL_ARGS_MAX_BYTES):
        return None
    try:
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return encoded if len(encoded) <= _APPROVAL_ARGS_MAX_BYTES else None


# Env keys a host-spawned MCP child may inherit from the broker process —
# never the full process.env (sibling secrets / gateway vars).
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "NODE_PATH",
    "NODE_OPTIONS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)

_PROXY_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY"})
_CONTROL_PLANE_LOADER_ENV = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX", "NODE_PATH", "NODE_OPTIONS", "VIRTUAL_ENV"}
)


def _proxy_has_userinfo(value: str) -> bool:
    """Keep host proxy credentials out of third-party MCP child environments."""
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
    except ValueError:
        return True
    return "@" in parsed.netloc


def _normalize_server_env(
    server_env: Any, *, metadata_secrets: set[str] | None = None
) -> dict[str, str]:
    """Allowlisted process env + sanitized spec overlay."""
    out: dict[str, str] = {}
    for key in _CHILD_ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val:
            if key.upper() in _PROXY_ENV_KEYS and _proxy_has_userinfo(val):
                logger.warning("pi-mcp-broker: omitting inherited proxy %s", key)
                continue
            out[key] = val
    if not server_env:
        return out
    declared: list[tuple[str, str]] = []
    if isinstance(server_env, list):
        for row in server_env:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                declared.append(
                    (row["name"], "" if row.get("value") is None else str(row["value"]))
                )
    elif isinstance(server_env, dict):
        for key, val in server_env.items():
            if val is not None:
                declared.append((str(key), str(val)))
    declared_env = sanitize_spec_env(declared)
    out.update(declared_env)
    if metadata_secrets is not None:
        metadata_secrets.update(_metadata_secret_values(declared_env))
    return out


def _secret_env_key(key: str) -> bool:
    """Whether the KEY NAME declares its value a credential."""
    return any(
        segment in _SECRET_ENV_KEY_SEGMENTS or segment.endswith(_SECRET_ENV_KEY_SUFFIXES)
        for segment in key.upper().split("_")
    )


def _value_is_credential_shaped(value: str) -> bool:
    """Whether the VALUE is a credential its key name does not announce."""
    if len(value) > _CREDENTIAL_SHAPE_MAX_CHARS:
        return False
    if redact_credentials(value)[0] != value:
        # A pattern the package already recognises. Naming it here costs no new
        # false positive -- _redact_metadata redacts it from every metadata
        # string anyway -- and adds the encoded-form and callable-name checks.
        return True
    if len(value) < _OPAQUE_SECRET_MIN_CHARS or any(char.isspace() for char in value):
        return False
    if _LONG_HEX_VALUE.match(value):
        return True
    if "://" in value or value[0] in "/.~":
        # A URL or a filesystem path is location metadata, not a credential.
        return False
    return (
        any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def _metadata_secret_values(values: dict[str, str]) -> set[str]:
    """Select values that can be credentials without masking ordinary metadata.

    A value is a secret when its KEY names one (``GITHUB_TOKEN``) or when the
    VALUE is shaped like one (an opaque token under an innocuous key). Length is
    NOT evidence: ``GITHUB_HOST=github.com``, ``NODE_ENV=production`` and
    ``AWS_REGION=eu-west-1`` are ordinary metadata, and selecting them redacted
    them out of every description, schema and result and WITHHELD any callable
    tool whose name contained one -- a working tool vanished from Pi's index
    because its server declared its own hostname. The fail-safe direction is
    unchanged: a credential-named key is selected whatever its value looks like
    (so ``KIROCREW_SESSION_KEY`` and ``KIROCREW_STUB_SESSION_TOKEN`` stay
    covered), and :func:`_redact_metadata` scrubs credential patterns and
    exfiltration URLs from every metadata string whether or not a declared value
    named them.
    """
    return {
        value
        for key, value in values.items()
        if value
        and key.upper() not in _NON_SECRET_ENV_KEYS
        and (_secret_env_key(key) or _value_is_credential_shaped(value))
    }


def _redact_metadata(value: Any, secrets: tuple[str, ...]) -> Any:
    """Scrub child-authored values before they can reach Pi or a log."""
    if isinstance(value, str):
        if secrets:
            secret_bytes = tuple(secret.encode("utf-8") for secret in secrets)

            def scrub_encoded_blob(match: re.Match[str]) -> str:
                token = match.group()
                try:
                    raw = base64.b64decode(
                        token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
                    )
                except (ValueError, binascii.Error):
                    return token
                return (
                    "[REDACTED: server env]"
                    if any(secret in raw for secret in secret_bytes)
                    else token
                )

            value = _CHILD_B64_TOKEN.sub(scrub_encoded_blob, value)
            for secret in secrets:
                value = value.replace(secret, "[REDACTED: server env]")
                encoded = secret.encode("utf-8")
                for representation in {
                    base64.b64encode(encoded).decode("ascii"),
                    base64.urlsafe_b64encode(encoded).decode("ascii"),
                }:
                    value = value.replace(representation, "[REDACTED: server env]")
                    value = value.replace(representation.rstrip("="), "[REDACTED: server env]")
        value, _ = redact_exfiltration_urls(value)
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact_metadata(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _redact_metadata(key, secrets): _redact_metadata(item, secrets)
            for key, item in value.items()
        }
    return value


def _sanitize_tool_list(
    tools: list[Any], secrets: tuple[str, ...], server_name: str
) -> tuple[list[dict[str, Any]], int]:
    """Bound and scrub child metadata away from the gateway event loop."""
    if len(tools) > _TOOL_LIST_MAX_COUNT:
        raise _ToolMetadataOverflow("MCP tool metadata size limit exceeded: tool count")
    retained: list[dict[str, Any]] = []
    retained_bytes = 2  # JSON array brackets.
    withheld = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        if _redact_metadata(name, secrets) != name:
            # The wire name is needed for tools/call, so it cannot be rewritten.
            withheld += 1
            continue
        if len(name.encode("utf-8")) > _TOOL_NAME_MAX_BYTES:
            raise _ToolMetadataOverflow("MCP tool metadata size limit exceeded: tool name")
        description = tool.get("description")
        clipped_description = (
            _redact_metadata(description, secrets)[:_TOOL_DESCRIPTION_MAX_CHARS]
            if isinstance(description, str)
            else ""
        )
        if (
            len(json.dumps(clipped_description, separators=(",", ":")).encode())
            > _TOOL_DESCRIPTION_MAX_BYTES
        ):
            raise _ToolMetadataOverflow("MCP tool metadata size limit exceeded: description")
        schema = _redact_metadata(tool.get("inputSchema"), secrets)
        if len(json.dumps(schema, separators=(",", ":")).encode()) > _TOOL_SCHEMA_MAX_BYTES:
            raise _ToolMetadataOverflow("MCP tool metadata size limit exceeded: input schema")
        entry = {"name": name, "description": clipped_description, "inputSchema": schema}
        index_entry = {"server": server_name, **entry}
        retained_bytes += len(json.dumps(index_entry, separators=(",", ":")).encode()) + 1
        if retained_bytes > _TOOL_INDEX_MAX_BYTES:
            raise _ToolMetadataOverflow("MCP tool metadata size limit exceeded: tool list")
        retained.append(entry)
    return retained, withheld


async def _discard_overlong_frame(
    stream: asyncio.StreamReader, overrun: asyncio.LimitOverrunError
) -> bool:
    """Drain one oversized frame through its newline without eating the next."""
    while True:
        try:
            if overrun.consumed:
                await stream.readexactly(overrun.consumed)
            await stream.readuntil(b"\n")
            return True
        except asyncio.LimitOverrunError as exc:
            overrun = exc
        except asyncio.IncompleteReadError:
            return False


def _windows_spawn_env(env: dict[str, str], work_dir: str) -> dict[str, str]:
    """Exclude workspace PATH entries before a Windows child can run a wrapper."""
    path_keys = [key for key in env if key.casefold() == "path"]
    path_value = env[path_keys[-1]] if path_keys else os.defpath
    normalized = {key: value for key, value in env.items() if key.casefold() != "path"}
    normalized["PATH"] = path_value
    screened = _pinned_spawn_path(normalized)
    workspace = os.path.normcase(os.path.realpath(work_dir))
    entries = []
    for entry in screened["PATH"].split(os.pathsep):
        if not entry:
            continue
        resolved = os.path.realpath(entry)
        try:
            inside = os.path.commonpath((workspace, os.path.normcase(resolved))) == workspace
        except ValueError:
            inside = False
        if not inside:
            entries.append(resolved)
    screened["PATH"] = os.pathsep.join(entries)
    return screened


async def _settle_task(task: asyncio.Task[Any]) -> Any:
    """Finish an owned task before propagating caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def _close_work_dir_fd(fd: int) -> None:
    await _settle_task(asyncio.create_task(asyncio.to_thread(os.close, fd)))


async def _open_work_dir_fd(work_dir: str) -> int:
    """Open the session directory off loop without leaking its fd on cancellation."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    opening = asyncio.create_task(asyncio.to_thread(os.open, work_dir, flags))
    cancellation: asyncio.CancelledError | None = None
    while not opening.done():
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            fd = opening.result()
        except BaseException:
            raise cancellation
        await _close_work_dir_fd(fd)
        raise cancellation
    return opening.result()


@dataclass
class _Pending:
    future: asyncio.Future[dict[str, Any]]


@dataclass(frozen=True)
class _Approval:
    call_id: str
    title: str
    args_json: str


@dataclass
class _McpChild:
    name: str
    process: asyncio.subprocess.Process
    start_id: str | None = None
    pgid: int | None = None
    descendant_start_ids: dict[int, str] = field(default_factory=dict)
    descendant_overflowed: bool = False
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    stdout_frame_discarding: bool = False
    sandbox_cleanup: str | None = None
    buffer: str = ""
    next_id: int = 1
    pending: dict[int, _Pending] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    tools: list[dict[str, Any]] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)
    withheld_secret_tool_names: int = 0
    metadata_secrets: tuple[str, ...] = field(default=(), repr=False)

    async def request(self, method: str, params: Any = None, *, timeout: float) -> Any:
        if self.reader_task is not None and self.reader_task.done():
            raise RuntimeError(f"{self.name} stdout reader exited")
        if self.stdout_frame_discarding:
            raise RuntimeError(f"{self.name} stdout frame exceeds read limit")
        if self.descendant_overflowed:
            raise _DescendantIdentityOverflow("MCP descendant identity limit exceeded")
        req_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[req_id] = _Pending(future=fut)
        try:
            assert self.process.stdin is not None
            frame = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            result = await write_request_frame_bounded(
                self.process.stdin,
                self.write_lock,
                frame,
                bound_secs=_WRITE_PROGRESS_BOUND_SECS,
            )
            if result is not RequestWriteResult.OK:
                raise RuntimeError(f"{self.name} stdin write stalled")
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(req_id, None)
        if msg.get("error"):
            err = msg["error"]
            if isinstance(err, dict):
                message = err.get("message") or f"MCP error {err.get('code')}"
            else:
                message = str(err)
            safe_message = await asyncio.to_thread(
                _redact_metadata, str(message), self.metadata_secrets
            )
            raise RuntimeError(safe_message)
        return msg.get("result")

    async def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if self.process.stdin is None:
            return
        try:
            frame = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            result = await write_request_frame_bounded(
                self.process.stdin,
                self.write_lock,
                frame,
                bound_secs=_WRITE_PROGRESS_BOUND_SECS,
            )
            if result is not RequestWriteResult.OK:
                raise RuntimeError("stdin write stalled")
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.debug("pi-mcp-broker: %s notify failed: %s", self.name, exc)

    def _on_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            if len(line) > 200:
                # Omitting the whole line avoids leaking a clipped secret prefix.
                logger.debug(
                    "pi-mcp-broker: %s non-JSON line exceeded diagnostic limit",
                    sanitize_sink_text(self.name, 128),
                )
            else:
                logger.debug(
                    "pi-mcp-broker: %s non-JSON line: %s",
                    sanitize_sink_text(self.name, 128),
                    sanitize_sink_text(_redact_metadata(line, self.metadata_secrets), 200),
                )
            return
        if not isinstance(msg, dict) or msg.get("id") is None:
            return
        try:
            req_id = int(msg["id"])
        except (TypeError, ValueError):
            return
        pending = self.pending.pop(req_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(msg)

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                try:
                    chunk = await self.process.stdout.readuntil(b"\n")
                except asyncio.LimitOverrunError as overrun:
                    # Fail current calls and discard the whole oversized frame.
                    err = RuntimeError(
                        f"{self.name} stdout frame exceeded read limit "
                        f"({_DEFAULT_READ_LIMIT} bytes)"
                    )
                    logger.warning("pi-mcp-broker: %s", err)
                    for pending in list(self.pending.values()):
                        if not pending.future.done():
                            pending.future.set_exception(err)
                    self.pending.clear()
                    self.stdout_frame_discarding = True
                    try:
                        if not await _discard_overlong_frame(self.process.stdout, overrun):
                            break
                    finally:
                        self.stdout_frame_discarding = False
                    continue
                except asyncio.IncompleteReadError as exc:
                    chunk = exc.partial
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if line:
                    self._on_line(line)
        finally:
            err = RuntimeError(f"{self.name} exited")
            for pending in list(self.pending.values()):
                if not pending.future.done():
                    pending.future.set_exception(err)
            self.pending.clear()

    async def handshake(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kiro-crew-pi-mcp-broker", "version": "0.1.0"},
            },
            timeout=_HANDSHAKE_TIMEOUT_SECS,
        )
        await self.remember_descendants()
        await self.notify("notifications/initialized")
        listed = await self.request("tools/list", {}, timeout=_HANDSHAKE_TIMEOUT_SECS)
        await self.remember_descendants()
        tools = listed.get("tools") if isinstance(listed, dict) else None
        self.tools = []
        if not isinstance(tools, list):
            return
        self.tools, self.withheld_secret_tool_names = await asyncio.to_thread(
            _sanitize_tool_list, tools, self.metadata_secrets, self.name
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=_CALL_TIMEOUT_SECS,
        )
        await self.remember_descendants()
        return await asyncio.to_thread(_redact_metadata, result, self.metadata_secrets)

    async def remember_descendants(self) -> None:
        """Record descendants before the leader exits or a helper detaches."""
        if self.descendant_overflowed:
            raise _DescendantIdentityOverflow("MCP descendant identity limit exceeded")
        if self.pgid is None or self.process.returncode is not None:
            return
        loop = asyncio.get_running_loop()
        descendants = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(platform_compat.process_descendant_identities, self.process.pid),
        )
        atomic = {
            descendant.pid: descendant.start_time
            for descendant in descendants or []
            if descendant.source is platform_compat.ProcessIdentitySource.ATOMIC
        }
        if len(self.descendant_start_ids.keys() | atomic.keys()) > _DESCENDANT_IDENTITY_MAX_COUNT:
            retained = tuple(self.descendant_start_ids.items())
            retired = await loop.run_in_executor(
                subprocess_executor(), functools.partial(self._retired_descendant_ids, retained)
            )
            for pid, start_id in retired:
                if self.descendant_start_ids.get(pid) == start_id:
                    self.descendant_start_ids.pop(pid, None)
        overflow = False
        for pid, start_id in atomic.items():
            if pid not in self.descendant_start_ids and (
                len(self.descendant_start_ids) >= _DESCENDANT_IDENTITY_MAX_COUNT
            ):
                overflow = True
                break
            self.descendant_start_ids[pid] = start_id
        if overflow:
            self.descendant_overflowed = True
            try:
                await self.kill()
            except Exception:
                logger.warning(
                    "pi-mcp-broker: descendant limit teardown failed for %s",
                    sanitize_sink_text(self.name, 128),
                )
            raise _DescendantIdentityOverflow("MCP descendant identity limit exceeded")

    def _retired_descendant_ids(
        self, retained: tuple[tuple[int, str], ...]
    ) -> list[tuple[int, str]]:
        """Forget only processes whose recorded incarnation is proven gone."""
        retired: list[tuple[int, str]] = []
        for pid, start_id in retained:
            current = platform_compat.get_process_start_id(pid)
            if (
                (current is not None and current != start_id)
                or (
                    current is None
                    and platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD
                )
                or (current == start_id and platform_compat.pid_is_zombie(pid) is True)
            ):
                retired.append((pid, start_id))
        return retired

    def _kill_verified_group(self) -> None:
        """Signal a retained group only while an owned member still vouches for it."""
        assert self.pgid is not None
        handle = process_identity.ProcessHandle(
            pid=self.process.pid,
            start_id=self.start_id,
            pgid=self.pgid,
            child_pids=self.descendant_start_ids,
        )
        if not process_identity._group_member_vouches(handle):
            if platform_compat.pgroup_exists(self.pgid):
                raise RuntimeError(
                    f"Pi MCP process group {self.pgid} has no verified member; not signalled"
                )
            return
        try:
            platform_compat.kill_process_group(self.pgid, platform_compat.SIGKILL)
        except ProcessLookupError:
            pass

    def _kill_verified_detached_descendants(self) -> None:
        """Signal recorded survivors outside the group by verified process identity."""
        assert self.pgid is not None
        first_error: Exception | None = None
        for pid, start_id in reversed(list(self.descendant_start_ids.items())):
            if (
                pid <= 1
                or platform_compat.get_process_start_id(pid) != start_id
                or platform_compat.pid_is_zombie(pid) is True
            ):
                continue
            # A process that changed groups still has the same start identity.
            # An unreadable group is handled as a single pid, never broadcast.
            if platform_compat.pgroup_of(pid) == self.pgid:
                continue
            if platform_compat.get_process_start_id(pid) != start_id:
                continue
            if not authorize_runtime_kill(
                pid,
                reason="Pi MCP detached helper teardown",
                caller="pi_mcp_broker._McpChild.kill",
            ):
                if first_error is None:
                    first_error = RuntimeError(f"Pi MCP detached helper {pid} kill was refused")
                continue
            try:
                platform_compat.kill_pid_pinned_outside_group(
                    pid, start_id, self.pgid, platform_compat.SIGKILL
                )
            except ProcessLookupError:
                pass
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _kill_verified_tree(self) -> None:
        """Attempt both group and detached cleanup even if one path fails."""
        first_error: Exception | None = None
        try:
            self._kill_verified_group()
        except Exception as exc:
            first_error = exc
        try:
            self._kill_verified_detached_descendants()
        except Exception as exc:
            if first_error is None:
                first_error = exc
            else:
                logger.warning("pi-mcp-broker: detached child cleanup also failed: %s", exc)
        if first_error is not None:
            raise first_error

    async def kill(self) -> None:
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stderr_task
            self.stderr_task = None
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader_task
            self.reader_task = None
        try:
            if platform_compat.IS_WINDOWS:
                await platform_compat.terminate_windows_asyncio_tree(self.process)
            else:
                if self.pgid is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(), self._kill_verified_tree
                    )
                elif self.process.returncode is None:
                    with contextlib.suppress(Exception):
                        await platform_compat.kill_process_tree_async(self.process.pid)
                if self.process.returncode is None:
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        if self.pgid is None:
                            with contextlib.suppress(Exception):
                                await platform_compat.kill_process_tree_async(
                                    self.process.pid, platform_compat.SIGKILL
                                )
                            await asyncio.wait_for(self.process.wait(), timeout=5.0)
                        else:
                            raise
        finally:
            if self.sandbox_cleanup:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.unlink, self.sandbox_cleanup)
                self.sandbox_cleanup = None


def broker_socket_path(*, artifact_dir: str, pid: int, nonce: str) -> str:
    """Filesystem path (POSIX) / lock-file anchor (Windows) for one session broker."""
    if not nonce or not str(nonce).strip():
        raise ValueError("pi MCP broker requires a non-empty session nonce")
    # Keep the leaf short: macOS AF_UNIX sockaddr is ~104 bytes; a deep
    # config_dir plus a long nonce would otherwise fail bind at session start.
    return os.path.join(artifact_dir, f"pmb_{pid}_{nonce[:12]}.sock")


class PiMcpBroker:
    """Host-side broker: secrets stay here; Pi talks over :data:`ENV_BROKER_SOCK`."""

    def __init__(
        self,
        *,
        roster: AdmittedServerRoster,
        socket_path: str,
        sandbox_mode: str = "standard",
        hidden_dirs: tuple[str, ...] = (),
        host_control_plane_servers: frozenset[str] = frozenset(),
        trusted_server_env: dict[str, dict[str, str]] | None = None,
        work_dir: str | None = None,
        session_key: str = "",
    ) -> None:
        self._sandbox_mode = sandbox_mode
        self._hidden_dirs = hidden_dirs
        self._host_control_plane_servers = host_control_plane_servers
        self._trusted_server_env = {
            name: dict(values) for name, values in (trusted_server_env or {}).items()
        }
        admitted = roster
        self.server_failures = dict(admitted.failures)
        self._specs = list(admitted.specs)
        self._work_dir = work_dir
        self._session_key = session_key
        self._socket_path = socket_path
        self._endpoint = transport.resolve_address(socket_path)
        self._server: Optional[transport.TransportServer] = None
        self._children: dict[str, _McpChild] = {}
        self._retiring_children: dict[str, _McpChild] = {}
        self._tool_index: list[dict[str, Any]] = []
        self._started = False
        self._clients: set[asyncio.Task[None]] = set()
        self._capacity_audits: set[asyncio.Task[None]] = set()
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._stop_lock = asyncio.Lock()
        self._teardown_retry_task: asyncio.Task[None] | None = None
        self._pending_approvals: dict[str, _Approval] = {}
        self._approved_calls: dict[str, tuple[str, str]] = {}
        self._delivering: dict[str, asyncio.Event] = {}
        self._approval_generations: dict[str, object] = {}
        self._delivery_generations: dict[str, object] = {}
        self._grant_generations: dict[str, object] = {}

    def _child_hidden_dirs(self, name: str, raw_name: str) -> tuple[str, ...]:
        """Use Pi's credential mask except for a verified host control-plane child."""
        return (
            ()
            if raw_name == name and name in self._host_control_plane_servers
            else self._hidden_dirs
        )

    def rekey(self, session_key: str) -> None:
        """Attribute later broker calls to the current warm-pool session."""
        self._session_key = session_key

    def note_permission(self, request_id: str, envelope: dict[str, Any]) -> None:
        """Remember the exact call the existing host permission path evaluates."""
        if not _bounded_text(request_id, _APPROVAL_REQUEST_ID_MAX_BYTES):
            return
        self.reject_permission(request_id)
        call_id = envelope.get("toolCallId")
        if isinstance(call_id, str):
            self.finish_call(call_id)
        title = envelope.get("title")
        arguments = envelope.get("input")
        if (
            envelope.get("truncated")
            or not isinstance(call_id, str)
            or not _bounded_text(call_id, _APPROVAL_CALL_ID_MAX_BYTES)
            or not isinstance(title, str)
            or not _bounded_text(title, _APPROVAL_TITLE_MAX_BYTES)
            or not title.startswith("mcp__")
            or not isinstance(arguments, dict)
            or len(self._pending_approvals) + len(self._approved_calls) >= _APPROVAL_MAX_OUTSTANDING
        ):
            return
        args_json = _bounded_approval_args(arguments)
        if args_json is None:
            return
        self._pending_approvals[request_id] = _Approval(call_id, title, args_json)
        self._approval_generations[request_id] = object()

    def stage_permission(self, request_id: str) -> object | None:
        approval = self._pending_approvals.get(request_id)
        if approval is not None:
            self._delivering[approval.call_id] = asyncio.Event()
            self._delivery_generations[approval.call_id] = self._approval_generations[request_id]
            return self._approval_generations[request_id]
        return None

    def approve_permission(self, request_id: str, generation: object | None) -> None:
        if generation is None or self._approval_generations.get(request_id) is not generation:
            return
        self._approval_generations.pop(request_id, None)
        approval = self._pending_approvals.pop(request_id, None)
        if approval is not None:
            self._grant_generations[approval.call_id] = generation
            self._delivery_generations.pop(approval.call_id, None)
            self._approved_calls[approval.call_id] = (
                approval.title,
                approval.args_json,
            )
            event = self._delivering.pop(approval.call_id, None)
            if event is not None:
                event.set()

    def reject_permission(self, request_id: str, generation: object | None = None) -> None:
        if generation is not None and self._approval_generations.get(request_id) is not generation:
            return
        self._approval_generations.pop(request_id, None)
        approval = self._pending_approvals.pop(request_id, None)
        if approval is not None:
            self._approved_calls.pop(approval.call_id, None)
            self._grant_generations.pop(approval.call_id, None)
            self._delivery_generations.pop(approval.call_id, None)
            event = self._delivering.pop(approval.call_id, None)
            if event is not None:
                event.set()

    async def wait_for_delivery(self, call_id: Any) -> object | None:
        if not isinstance(call_id, str):
            return None
        event = self._delivering.get(call_id)
        if event is None:
            return self._grant_generations.get(call_id)
        generation = self._delivery_generations.get(call_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=_DELIVERY_TIMEOUT_SECS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._revoke_generation(call_id, generation)
            raise
        return generation

    def _revoke_generation(self, call_id: str, generation: object | None) -> None:
        """Retire a failed delivery even if its grant was published during the wait."""
        if generation is None:
            return
        for request_id, approval in list(self._pending_approvals.items()):
            if (
                approval.call_id == call_id
                and self._approval_generations.get(request_id) is generation
            ):
                self.reject_permission(request_id, generation)
        if self._grant_generations.get(call_id) is generation:
            self._grant_generations.pop(call_id, None)
            self._approved_calls.pop(call_id, None)
        if self._delivery_generations.get(call_id) is generation:
            self._delivery_generations.pop(call_id, None)
            event = self._delivering.pop(call_id, None)
            if event is not None:
                event.set()

    def finish_call(self, call_id: str) -> None:
        self._approved_calls.pop(call_id, None)
        self._grant_generations.pop(call_id, None)
        self._delivery_generations.pop(call_id, None)
        for request_id, approval in list(self._pending_approvals.items()):
            if approval.call_id == call_id:
                self.reject_permission(request_id)
        event = self._delivering.pop(call_id, None)
        if event is not None:
            event.set()

    def _consume_approval(
        self, call_id: Any, tool: str, arguments: dict[str, Any], *, generation: object | None
    ) -> None:
        expected = self._approved_calls.get(call_id) if isinstance(call_id, str) else None
        if expected is None or self._grant_generations.get(call_id) is not generation:
            raise PermissionError("Pi MCP call has no matching host-approved permission")
        if expected != (tool, _bounded_approval_args(arguments)):
            raise PermissionError("Pi MCP call has no matching host-approved permission")
        self._approved_calls.pop(call_id, None)
        self._grant_generations.pop(call_id, None)

    @property
    def endpoint(self) -> str:
        """Address to place in :data:`ENV_BROKER_SOCK` for the Pi child."""
        return self._endpoint

    @property
    def initialized_servers(self) -> frozenset[str]:
        return frozenset(self._children)

    async def start(self) -> None:
        """Spawn MCP children and bind the IPC endpoint."""
        if self._started:
            return
        if self._children or self._retiring_children:
            raise RuntimeError("Pi MCP broker has children awaiting teardown")
        await asyncio.to_thread(transport.prepare_dir, self._socket_path)
        # Drop a stale socket file from a previous soft-fail race (POSIX only).
        if not platform_compat.IS_WINDOWS:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(os.unlink, self._socket_path)
        try:
            await self._spawn_children()
            self._server = await transport.serve(
                self._socket_path, self._accept_client, limit=_DEFAULT_READ_LIMIT
            )
            if not platform_compat.IS_WINDOWS:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(platform_compat.restrict_to_owner, self._socket_path)
        except BaseException:
            try:
                await self.stop()
            except BaseException:
                logger.warning("pi-mcp-broker: cleanup failed after startup failure")
            raise
        self._started = True
        for name, child in tuple(self._children.items()):
            if child.reader_task is not None:
                child.reader_task.add_done_callback(
                    functools.partial(self._on_reader_finished, name, child)
                )
        logger.info(
            "pi-mcp-broker: listening on %s with %d tool(s) from %d server(s)",
            self._endpoint,
            len(self._tool_index),
            len(self._children),
        )

    async def stop(self) -> None:
        """Tear down the endpoint and every MCP child."""
        try:
            async with self._stop_lock:
                await self._stop_once()
        except BaseException:
            if self._children or self._retiring_children:
                self._ensure_teardown_retry()
            raise
        else:
            retry = self._teardown_retry_task
            if retry is not None and retry is not asyncio.current_task():
                retry.cancel()
                await asyncio.gather(retry, return_exceptions=True)

    def _ensure_teardown_retry(self) -> None:
        retry = self._teardown_retry_task
        if retry is not None and not retry.done():
            return
        retry = asyncio.create_task(self._retry_failed_teardown())
        self._teardown_retry_task = retry
        _FAILED_TEARDOWN_TASKS.add(retry)
        retry.add_done_callback(_FAILED_TEARDOWN_TASKS.discard)

    async def _retry_failed_teardown(self) -> None:
        try:
            while self._children or self._retiring_children:
                await asyncio.sleep(_TEARDOWN_RETRY_DELAY_SECS)
                try:
                    await self.stop()
                except Exception:
                    continue
        finally:
            if self._teardown_retry_task is asyncio.current_task():
                self._teardown_retry_task = None

    async def _stop_once(self) -> None:
        server = self._server
        self._server = None
        self._started = False
        if server is not None:
            with contextlib.suppress(Exception):
                server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        self._clients.clear()
        if self._capacity_audits:
            await asyncio.gather(*self._capacity_audits, return_exceptions=True)
        self._capacity_audits.clear()
        if self._retirement_tasks:
            await asyncio.gather(*self._retirement_tasks, return_exceptions=True)
        self._retirement_tasks.clear()
        cleanup_errors: list[BaseException] = []
        try:
            for children in (self._children, self._retiring_children):
                for name, child in list(children.items()):
                    try:
                        await child.kill()
                    except BaseException as exc:
                        cleanup_errors.append(exc)
                    else:
                        if children.get(name) is child:
                            children.pop(name, None)
        finally:
            self._tool_index.clear()
            for event in self._delivering.values():
                event.set()
            self._delivering.clear()
            self._delivery_generations.clear()
            self._grant_generations.clear()
            self._approval_generations.clear()
            self._pending_approvals.clear()
            self._approved_calls.clear()
            if not platform_compat.IS_WINDOWS:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.unlink, self._socket_path)
        if cleanup_errors:
            raise cleanup_errors[0]

    def _on_reader_finished(self, name: str, child: _McpChild, reader: asyncio.Task[None]) -> None:
        """Unpublish a dead child before another bridge call can select it."""
        if not reader.cancelled():
            try:
                reader.result()
            except Exception:
                logger.warning(
                    "pi-mcp-broker: %s stdout reader failed", sanitize_sink_text(name, 128)
                )
        if not self._started or self._children.get(name) is not child:
            return
        self._children.pop(name, None)
        self._tool_index = [tool for tool in self._tool_index if tool["server"] != name]
        self.server_failures[name] = "MCP server stdout reader exited"
        self._retiring_children[name] = child
        task = asyncio.create_task(child.kill())
        self._retirement_tasks.add(task)
        task.add_done_callback(functools.partial(self._on_retirement_finished, name, child))

    def _on_retirement_finished(
        self, name: str, child: _McpChild, task: asyncio.Task[None]
    ) -> None:
        self._retirement_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning("pi-mcp-broker: retired child cleanup failed")
            return
        if self._retiring_children.get(name) is child:
            self._retiring_children.pop(name, None)

    async def _spawn_children(self) -> None:
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for spec in self._specs:
            raw_name = str(spec.get("name") or "")
            name = raw_name.strip()
            command = spec.get("command")
            if raw_name != name:
                logger.warning("pi-mcp-broker: refusing server with padded name: %r", raw_name)
                continue
            if not name or not command or not isinstance(command, str):
                continue
            if "__" in name:
                logger.warning("pi-mcp-broker: refusing server whose name contains '__': %r", name)
                continue
            if spec.get("disabled"):
                continue
            if spec.get("type", "stdio") not in ("stdio", None, ""):
                continue
            if not self._work_dir or not os.path.isabs(self._work_dir):
                raise ValueError("Pi MCP child requires an absolute session work directory")
            if not os.path.isabs(command) and os.path.basename(command) != command:
                self.server_failures[name] = (
                    "Pi MCP child command must be absolute or a bare executable name"
                )
                logger.warning(
                    "pi-mcp-broker: refusing relative command for %s",
                    sanitize_sink_text(name, 128),
                )
                continue
            if name in seen:
                # Neither copy has an unambiguous identity for approvals.
                # Withhold both while preserving unrelated servers.
                self.server_failures[name] = "Pi MCP child name is duplicated"
                candidates = [candidate for candidate in candidates if candidate[0] != name]
                continue
            seen.add(name)
            candidates.append((name, raw_name, spec))

        semaphore = asyncio.Semaphore(_STARTUP_CONCURRENCY)

        async def launch(name: str, raw_name: str, spec: dict[str, Any]) -> _McpChild | None:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self._spawn_child(name, raw_name, spec),
                        timeout=_CHILD_STARTUP_TIMEOUT_SECS,
                    )
                except asyncio.TimeoutError:
                    self.server_failures[name] = "MCP server initialization timed out"
                    logger.warning(
                        "pi-mcp-broker: startup timed out for %s", sanitize_sink_text(name, 128)
                    )
                    return None

        tasks = [
            asyncio.create_task(launch(name, raw_name, spec), name=f"pi-mcp-broker-start-{name}")
            for name, raw_name, spec in candidates
        ]
        try:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=_STARTUP_TIMEOUT_SECS)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            for (name, _raw_name, _spec), task in zip(candidates, tasks):
                if task.cancelled():
                    self.server_failures.setdefault(name, "MCP server initialization timed out")
                    continue
                child = task.result()
                if child is None:
                    continue
                if child.withheld_secret_tool_names:
                    self.server_failures[name] = (
                        f"{child.withheld_secret_tool_names} MCP tool name(s) contained "
                        "protected server env and were withheld"
                    )
                server_tools: list[dict[str, Any]] = []
                for tool in child.tools:
                    tool_name = tool.get("name")
                    if not isinstance(tool_name, str) or not tool_name:
                        continue
                    if tool_name in child.disabled_tools or "__" in tool_name:
                        continue
                    server_tools.append(
                        {
                            "server": name,
                            "name": tool_name,
                            "description": tool.get("description") or "",
                            "inputSchema": tool.get("inputSchema"),
                        }
                    )
                candidate = self._tool_index + server_tools
                if (
                    len(json.dumps(candidate, separators=(",", ":")).encode())
                    > _TOOL_INDEX_MAX_BYTES
                ):
                    self.server_failures[name] = "MCP tool metadata exceeds bridge size limit"
                    await child.kill()
                    self._children.pop(name, None)
                    continue
                self._tool_index = candidate
                logger.info("pi-mcp-broker: %s listed %d tool(s)", name, len(child.tools))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _spawn_child(
        self, name: str, raw_name: str, spec: dict[str, Any]
    ) -> _McpChild | None:
        cleanup: str | None = None
        child: _McpChild | None = None
        try:
            work_dir = self._work_dir
            assert work_dir is not None
            command = spec["command"]
            args = [str(a) for a in (spec.get("args") or [])]
            metadata_secrets: set[str] = set()
            env = _normalize_server_env(spec.get("env"), metadata_secrets=metadata_secrets)
            trusted_env = self._trusted_server_env.get(name, {})
            env.update(trusted_env)
            metadata_secrets.update(_metadata_secret_values(trusted_env))
            mask_exempt = raw_name == name and name in self._host_control_plane_servers
            if mask_exempt:
                # The verified managed invocation gets the host session identity
                # and no credential-directory mask. Inherited interpreter hooks
                # must not execute agent-controlled code with that authority.
                # Strip the explicit spawn env as well as the sandbox launcher:
                # a no-backend launch has no launcher to do the latter.
                env = {
                    key: value
                    for key, value in env.items()
                    if key.upper() not in _CONTROL_PLANE_LOADER_ENV
                }
                # Python's user site can still load agent-written .pth and
                # sitecustomize files even after PYTHONPATH is removed.
                env["PYTHONNOUSERSITE"] = "1"
                env["PYTHONSAFEPATH"] = "1"
            if platform_compat.IS_WINDOWS:
                env = await asyncio.to_thread(_windows_spawn_env, env, work_dir)
            disabled = {t for t in (spec.get("disabledTools") or []) if isinstance(t, str) and t}
            argv, cleanup = await wrap_argv_async(
                [command, *args],
                mode=self._sandbox_mode,
                strip_python_env=mask_exempt,
                # The Pi adapter needs the credential mask. The host's own
                # verified control-plane child needs its protected binding.
                extra_hidden_dirs=self._child_hidden_dirs(name, raw_name),
                _prepare=wrap_argv,
            )
            work_dir_fd: int | None = None
            try:
                if platform_compat.IS_POSIX:
                    work_dir_fd = await _open_work_dir_fd(work_dir)
                argv = await asyncio.to_thread(cgroup_scope_argv, argv)
                process = await platform_compat.create_windows_cleanup_owned_process(
                    functools.partial(
                        create_subprocess_limited,
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=work_dir if platform_compat.IS_WINDOWS else None,
                        chdir_fd=work_dir_fd,
                        limit=_DEFAULT_READ_LIMIT,
                        start_new_session=platform_compat.IS_POSIX,
                        creationflags=(
                            platform_compat.CREATE_NEW_PROCESS_GROUP
                            | platform_compat._SUBPROCESS_NO_WINDOW
                            | platform_compat.CREATE_SUSPENDED
                        ),
                    )
                )
                child = _McpChild(
                    name=name,
                    process=process,
                    disabled_tools=disabled,
                    sandbox_cleanup=cleanup,
                    metadata_secrets=tuple(sorted(metadata_secrets, key=len, reverse=True)),
                )
                self._children[name] = child
            finally:
                if work_dir_fd is not None:
                    await _close_work_dir_fd(work_dir_fd)
            if platform_compat.IS_WINDOWS:
                # Imported here because the client imports this broker at module load.
                from kiro_crew.acp.client import finish_suspended_spawn

                await platform_compat.finish_windows_cleanup_owned_spawn(
                    lambda: asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(),
                        functools.partial(
                            finish_suspended_spawn,
                            process,
                            process.pid,
                            label=f"pi MCP server {name}",
                        ),
                    )
                )
            else:
                start_id = platform_compat.get_process_start_id(process.pid)
                child.start_id = start_id
                child.pgid = process_identity.isolated_group_of(process.pid, start_id)
            child.reader_task = asyncio.create_task(
                child._read_stdout(), name=f"pi-mcp-broker-{name}-stdout"
            )
            # Drain stderr so a chatty server cannot fill the pipe.
            child.stderr_task = asyncio.create_task(
                self._drain_stderr(name, process, child.metadata_secrets),
                name=f"pi-mcp-broker-{name}-stderr",
            )
            await child.handshake()
            return child
        except Exception as exc:
            self.server_failures[name] = (
                str(exc)
                if isinstance(exc, (_ToolMetadataOverflow, _DescendantIdentityOverflow))
                else (
                    "MCP server initialization timed out"
                    if isinstance(exc, asyncio.TimeoutError)
                    else "MCP server initialization failed"
                )
            )
            logger.warning(
                "pi-mcp-broker: startup failed for %s: %s",
                sanitize_sink_text(name, 128),
                sanitize_sink_text(
                    _redact_metadata(str(exc), child.metadata_secrets if child is not None else ()),
                    2000,
                ),
            )
            if child is not None:
                await child.kill()
                self._children.pop(name, None)
            elif cleanup:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.unlink, cleanup)
            return None
        except BaseException:
            if child is not None:
                await child.kill()
                self._children.pop(name, None)
            elif cleanup:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.unlink, cleanup)
            raise

    async def _drain_stderr(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        secrets: tuple[str, ...] = (),
    ) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                try:
                    line = await process.stderr.readuntil(b"\n")
                except asyncio.LimitOverrunError as overrun:
                    # Keep draining after an overlong diagnostic so the child
                    # cannot stall on a full stderr pipe.
                    logger.warning(
                        "pi-mcp-broker: %s stderr frame exceeded read limit",
                        sanitize_sink_text(name, 128),
                    )
                    if not await _discard_overlong_frame(process.stderr, overrun):
                        break
                    continue
                except asyncio.IncompleteReadError as exc:
                    line = exc.partial
                if not line:
                    break
                if len(line) > 2000:
                    # A prefix could reveal part of a longer credential.
                    logger.debug(
                        "pi-mcp-broker: %s stderr diagnostic exceeded log limit",
                        sanitize_sink_text(name, 128),
                    )
                    continue
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug(
                        "pi-mcp-broker: %s stderr: %s",
                        sanitize_sink_text(name, 128),
                        sanitize_sink_text(_redact_metadata(text, secrets), 2000),
                    )
        except Exception:
            pass

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._clients) >= _MAX_ACTIVE_CLIENTS:
            # One in-flight record represents a burst of capacity refusals.
            # A reconnect loop must not enqueue unbounded SEL work.
            if not self._capacity_audits:
                audit = asyncio.create_task(
                    self._audit_peer("unverified-peer", "denied", "capacity")
                )
                self._capacity_audits.add(audit)
                audit.add_done_callback(self._capacity_audits.discard)
            writer.close()
            return
        task = asyncio.create_task(self._on_client(reader, writer))
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        verdict = socketsec.check_peer_is_self(writer)
        if verdict is not socketsec.PeerCredResult.MATCH:
            logger.warning("pi-mcp-broker: refusing peer (principal %s)", verdict.value)
            await self._audit_peer("unverified-peer", "denied", verdict.value)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        await self._audit_peer(self._session_key or "pi-session", "allowed")
        write_lock = asyncio.Lock()
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    await self._write(
                        writer,
                        write_lock,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "parse error"},
                        },
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch(writer, write_lock, msg)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(
        self, writer: asyncio.StreamWriter, write_lock: asyncio.Lock, msg: dict[str, Any]
    ) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        raw_params = msg.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        is_call = method == "bridge/call"
        server = ""
        tool = ""
        routed = False
        audit_outcome: str | None = None
        try:
            if method == "bridge/list":
                result: Any = {"tools": list(self._tool_index)}
            elif method == "bridge/call":
                raw_server = params.get("server")
                raw_tool = params.get("tool")
                if not _bounded_text(raw_server, _SERVER_NAME_MAX_BYTES) or not _bounded_text(
                    raw_tool, _TOOL_NAME_MAX_BYTES
                ):
                    raise RuntimeError("bridge/call names exceed size limit")
                server = raw_server
                tool = raw_tool
                arguments = params.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                generation = await self.wait_for_delivery(params.get("toolCallId"))
                self._consume_approval(
                    params.get("toolCallId"),
                    f"mcp__{server}__{tool}",
                    arguments,
                    generation=generation,
                )
                child = self._children.get(server)
                if child is None:
                    raise RuntimeError(f"unknown server {server!r}")
                if tool in child.disabled_tools:
                    raise RuntimeError(f"tool {tool!r} is disabled on {server}")
                routed = True
                result = await child.call_tool(tool, arguments)
                audit_outcome = (
                    "failed"
                    if isinstance(result, dict) and result.get("isError") is True
                    else "completed"
                )
            else:
                await self._write(
                    writer,
                    write_lock,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    },
                )
                return
            await self._write(
                writer,
                write_lock,
                {"jsonrpc": "2.0", "id": req_id, "result": result},
                max_bytes=_BRIDGE_RESPONSE_MAX_BYTES if is_call else None,
            )
        except asyncio.CancelledError:
            if is_call and audit_outcome is None:
                audit_outcome = "cancelled" if routed else "denied"
            raise
        except Exception as exc:
            if is_call and (audit_outcome is None or isinstance(exc, _BridgeResponseOverflow)):
                audit_outcome = "failed" if routed else "denied"
            await self._write(
                writer,
                write_lock,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32000,
                        "message": sanitize_sink_text(str(exc), _BRIDGE_ERROR_MAX_CHARS),
                    },
                },
            )
        finally:
            if audit_outcome is not None:
                await _settle_task(
                    asyncio.create_task(self._audit_tool_call(server, tool, audit_outcome))
                )

    async def _audit_peer(self, caller: str, outcome: str, error: str = "") -> None:
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: sel().log_api_access(
                        caller=caller,
                        operation="pi-mcp-broker.connect",
                        outcome=outcome,
                        source="acp",
                        error=error,
                    ),
                ),
                timeout=5.0,
            )
        except Exception:
            logger.warning("pi-mcp-broker: peer SEL audit failed", exc_info=True)

    async def _audit_tool_call(self, server: str, tool: str, outcome: str) -> None:
        session_key = self._session_key
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: sel().log_tool_invocation(
                        session_key=session_key,
                        source="acp",
                        tool_name="pi-mcp-broker.call",
                        tool_kind="mcp",
                        outcome=outcome,
                        resources=f"server={server} tool={tool}",
                    ),
                ),
                timeout=5.0,
            )
        except Exception:
            logger.warning("pi-mcp-broker: tool SEL audit failed", exc_info=True)

    async def _write(
        self,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
        obj: dict[str, Any],
        *,
        max_bytes: int | None = None,
    ) -> None:
        frame = await asyncio.to_thread(_encode_bridge_frame, obj, max_bytes)
        drained = await write_response_frame_bounded(
            writer, write_lock, frame, bound_secs=_WRITE_PROGRESS_BOUND_SECS
        )
        if not drained:
            raise ConnectionError("bridge response write stalled")
