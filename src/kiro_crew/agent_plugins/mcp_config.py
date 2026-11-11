"""Validate a plugin's ``mcp.json`` (Agent Plugins 1.0.0, §7.2).

Failure boundaries follow §7.2.2. A document-level problem (bad JSON, wrong or
mismatched ``$schema``, an extra top-level field) disables MCP for the plugin;
a bad server entry skips that entry only. Either way the plugin's other
components still load. Nothing here expands placeholders or starts a process.
"""

from __future__ import annotations

import ipaddress
import os
import string
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.agent_plugins.model import Issue, McpServer, RemoteServer, StdioServer
from kiro_crew.agent_plugins.paths import resolves_within
from kiro_crew.user_json import loads_user_json

MCP_FILE = "mcp.json"
#: Same bound as the manifest's; a real ``mcp.json`` is a few KB.
MAX_MCP_BYTES = 256 * 1024

ROOT_PLACEHOLDER = "${PLUGIN_ROOT}"
DATA_PLACEHOLDER = "${PLUGIN_DATA}"
RESERVED_ENV = ("PLUGIN_ROOT", "PLUGIN_DATA")

_STDIO_FIELDS = frozenset({"type", "command", "args", "env", "cwd"})
_REMOTE_FIELDS = frozenset({"type", "url", "headers"})
_REMOTE_TYPES = ("streamable-http", "sse")
# RFC 9110 token characters.
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~")
# Field values may hold visible characters, spaces, tabs and obs-text; any
# other control character (CR and LF included) makes the value invalid.
_HEADER_VALUE_BAD = frozenset(chr(code) for code in (*range(0x00, 0x09), *range(0x0A, 0x20), 0x7F))
# A hostname label: 1-63 letters, digits and hyphens, a letter or digit at both ends.
_LABEL_ENDS = frozenset(string.ascii_letters + string.digits)
_LABEL_CHARS = _LABEL_ENDS | frozenset("-")
_MAX_LABEL_CHARS = 63


class _EntryError(Exception):
    """One server entry is invalid; the message says why."""


def load_mcp_config(
    root_real: str, data_dir: str, expected_schema: str
) -> tuple[tuple[McpServer, ...], list[Issue]]:
    """Read ``mcp.json`` under the resolved plugin root.

    *data_dir* is the plugin's ``PLUGIN_DATA`` path, needed to check a
    ``${PLUGIN_DATA}``-rooted ``cwd``. *expected_schema* is the ``mcp.json``
    identifier matching the manifest's version. A missing file is not an issue;
    one that could not be read is an issue marked ``failed``.
    """
    path = os.path.join(root_real, MCP_FILE)
    if not os.path.lexists(path):
        return (), []
    resolved = resolves_within(path, root_real)
    if resolved is None:
        return (), [Issue("mcp", "mcp.json points outside the plugin folder; MCP is off")]
    if not os.path.isfile(resolved):
        return (), [Issue("mcp", "mcp.json is not a regular file; MCP is off")]
    try:
        if os.path.getsize(resolved) > MAX_MCP_BYTES:
            return (), [Issue("mcp", "mcp.json is too large; MCP is off")]
        with open(resolved, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return (), [Issue("mcp", "mcp.json could not be read; MCP is off", failed=True)]
    except UnicodeDecodeError:
        return (), [Issue("mcp", "mcp.json is not UTF-8 text; MCP is off")]
    try:
        data = loads_user_json(text)
    except (ValueError, RecursionError):
        # RecursionError: nesting deeper than the parser's stack, well under the size cap.
        return (), [Issue("mcp", "mcp.json is not valid JSON; MCP is off")]
    return parse_mcp_config(data, root_real, data_dir, expected_schema)


def parse_mcp_config(
    data: Any, root_real: str, data_dir: str, expected_schema: str
) -> tuple[tuple[McpServer, ...], list[Issue]]:
    """Validate an already-parsed ``mcp.json`` object."""
    if not isinstance(data, dict):
        return (), [Issue("mcp", "mcp.json must hold a JSON object; MCP is off")]
    extra = sorted(set(data) - {"$schema", "mcpServers"})
    if extra:
        return (), [Issue("mcp", f"mcp.json has an unknown field ({extra[0]}); MCP is off")]
    schema = data.get("$schema")
    if not isinstance(schema, str) or not schema:
        return (), [Issue("mcp", "mcp.json is missing $schema; MCP is off")]
    if schema != expected_schema:
        return (), [
            Issue("mcp", "mcp.json targets a different version than plugin.json; MCP is off")
        ]
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return (), [Issue("mcp", "mcp.json is missing the mcpServers object; MCP is off")]

    out: list[McpServer] = []
    issues: list[Issue] = []
    for name, entry in servers.items():
        try:
            out.append(_parse_entry(name, entry, root_real, data_dir))
        except _EntryError as exc:
            issues.append(Issue("mcp", f"server skipped: {exc}", entry=name))
    return tuple(out), issues


def _parse_entry(name: str, entry: Any, root_real: str, data_dir: str) -> McpServer:
    if not name:
        raise _EntryError("the server name is empty")
    if not isinstance(entry, dict):
        raise _EntryError("the entry is not an object")
    kind = entry.get("type")
    if kind is None:
        raise _EntryError("type is missing")
    if kind == "stdio":
        return _parse_stdio(name, entry, root_real, data_dir)
    if kind in _REMOTE_TYPES:
        return _parse_remote(name, kind, entry)
    raise _EntryError(f"unknown type {kind!r}")


def _parse_stdio(name: str, entry: dict, root_real: str, data_dir: str) -> StdioServer:
    extra = sorted(set(entry) - _STDIO_FIELDS)
    if extra:
        raise _EntryError(f"unknown field {extra[0]!r} for a stdio server")
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise _EntryError("command is missing")
    _check_command(command, root_real)

    if "args" in entry:
        args = entry["args"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _EntryError("args must be a list of strings")
    else:
        args = []

    if "env" in entry:
        env = entry["env"]
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise _EntryError("env must map names to strings")
    else:
        env = {}
    for key in env:
        if _is_reserved_env(key):
            raise _EntryError(f"env must not set {key}")

    cwd = None
    if "cwd" in entry:
        cwd = entry["cwd"]
        if not isinstance(cwd, str):
            raise _EntryError("cwd must be a string")
        _check_cwd(cwd, root_real, data_dir)

    return StdioServer(name=name, command=command, args=tuple(args), env=dict(env), cwd=cwd)


def _is_reserved_env(key: str) -> bool:
    # Environment names are case-insensitive on Windows (§9.1 "platform
    # environment-name semantics").
    if os.name == "nt":
        return key.upper() in RESERVED_ENV
    return key in RESERVED_ENV


def _check_command(command: str, root_real: str) -> None:
    # §7.2.1: one executable token, either a bare name or a ./ path. A command
    # is never placeholder-expanded, so ${...} in it is literal text.
    if command.startswith("./"):
        resolved = resolves_within(os.path.join(root_real, command[2:]), root_real)
        if resolved is None:
            raise _EntryError("command points outside the plugin folder")
        # Checked so the verdict is the same on every platform: a link whose
        # target is spelled for another OS (``C:/...`` read on POSIX) resolves to
        # a missing path inside the root, and could never launch anyway.
        if not os.path.isfile(resolved):
            raise _EntryError("command is not a file in the plugin folder")
        return
    # A colon makes ``C:tool.exe`` a drive-relative path on Windows, not a bare name.
    if any(char in command for char in ("/", "\\", ":")) or command in (".", ".."):
        raise _EntryError("command must be a bare name or start with ./")


def _check_cwd(cwd: str, root_real: str, data_dir: str) -> None:
    if cwd.startswith("./"):
        base, rest = root_real, cwd[2:]
    elif cwd == ROOT_PLACEHOLDER or cwd.startswith(ROOT_PLACEHOLDER + "/"):
        base, rest = root_real, cwd[len(ROOT_PLACEHOLDER) :].lstrip("/")
    elif cwd == DATA_PLACEHOLDER or cwd.startswith(DATA_PLACEHOLDER + "/"):
        base, rest = data_dir, cwd[len(DATA_PLACEHOLDER) :].lstrip("/")
    else:
        raise _EntryError("cwd must start with ./, ${PLUGIN_ROOT} or ${PLUGIN_DATA}")
    base_real = os.path.realpath(base)
    target = os.path.join(base_real, rest) if rest else base_real
    if resolves_within(target, base_real) is None:
        where = "its data folder" if base is data_dir else "the plugin folder"
        raise _EntryError(f"cwd points outside {where}")


def _parse_remote(name: str, kind: str, entry: dict) -> RemoteServer:
    extra = sorted(set(entry) - _REMOTE_FIELDS)
    if extra:
        raise _EntryError(f"unknown field {extra[0]!r} for a {kind} server")
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise _EntryError("url is missing")
    _check_url(url)

    if "headers" in entry:
        headers = entry["headers"]
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise _EntryError("headers must map names to strings")
    else:
        headers = {}
    seen: set[str] = set()
    for key, value in headers.items():
        if not key or not all(char in _TOKEN_CHARS for char in key):
            raise _EntryError(f"header name {key!r} is not valid")
        if any(char in _HEADER_VALUE_BAD for char in value):
            raise _EntryError(f"header {key!r} has a control character in its value")
        folded = key.lower()
        if folded in seen:
            raise _EntryError(f"header {key!r} appears twice")
        seen.add(folded)

    return RemoteServer(name=name, type=kind, url=url, headers=dict(headers))  # type: ignore[arg-type]


def _check_url(url: str) -> None:
    if any(char.isspace() or unicodedata.category(char) == "Cc" for char in url):
        raise _EntryError("url must not contain whitespace or control characters")
    if "#" in url:
        raise _EntryError("url must not contain a fragment")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise _EntryError("url is not a valid URL") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc or not host:
        raise _EntryError("url must be an absolute http or https URL")
    if "@" in parts.netloc:
        raise _EntryError("url must not contain user information")
    if _has_empty_port(parts.netloc):
        raise _EntryError("url is not a valid URL")
    if not _is_valid_hostname(host):
        raise _EntryError("url host is not valid")
    if parts.scheme == "http" and not _is_loopback(host):
        raise _EntryError("url must use https unless the host is loopback")


def _has_empty_port(netloc: str) -> bool:
    authority = netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        close = authority.rfind("]")
        return close >= 0 and authority[close + 1 :] == ":"
    return authority.endswith(":")


def _is_valid_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if ascii_host.endswith("."):
        ascii_host = ascii_host[:-1]
    return (
        bool(ascii_host)
        and len(ascii_host) <= 253
        and all(_is_host_label(label) for label in ascii_host.split("."))
    )


def _is_host_label(label: str) -> bool:
    return (
        0 < len(label) <= _MAX_LABEL_CHARS
        and label[0] in _LABEL_ENDS
        and label[-1] in _LABEL_ENDS
        and all(char in _LABEL_CHARS for char in label)
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
