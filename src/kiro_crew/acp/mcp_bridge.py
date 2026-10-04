"""Shared MCP bridge for KiroCrew's autonomous ACP harness adapters.

The gateway hands each ACP session the agent spec's MCP servers (the crew
tools live in kirocrew-core: spawn_sub_agents, spawn_run, ...). Adapters in
ACP_BACKENDS_SESSION_MCP_ARRAY are expected to bridge that array (H6: any
adapter that reads no agent spec belongs there). Without this, the model on
a harness path never sees spawn_sub_agents → sub-agents can never spawn →
the dashboard's orange sub-agent progress strip never renders.

Scope: minimal MCP client — stdio (newline-delimited JSON-RPC subprocess)
and http/sse (streamable-HTTP POSTs). Lazy-connect on first prompt; a
server that fails to come up only shrinks the tool list, it never fails the
turn. Sessions whose mcpServers array is empty behave exactly as before.

Consumers (every direct adapter) keep per-session dicts ``mcp_servers`` /
``mcp_handles`` / ``mcp_tools`` with identical shapes, so ``iter_mcp_tool`` and
the spec builder work unchanged against any of them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Mapping

from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv

# The per-call kill budget. KIROCREW_MCP_TIMEOUT_SECS is the shared name;
# OPENROUTER_MCP_TIMEOUT_SECS is kept as a legacy fallback so existing
# operator environments keep working after the extraction.
MCP_TIMEOUT_SECS = float(
    os.environ.get("KIROCREW_MCP_TIMEOUT_SECS")
    or os.environ.get("OPENROUTER_MCP_TIMEOUT_SECS", "3600")
    or "3600"
)
MCP_PROTOCOL_VERSION = "2024-11-05"
# Built-in tool names each harness exposes natively; MCP tools must not
# shadow them (collisions get a server-qualified exposed name instead).
MCP_BUILTIN_TOOLS = ("bash", "read_file", "write_file")
_MODEL_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MODEL_TOOL_NAME_MAX = 64
_PROVIDER_SECRET_ENV = {
    "DEEPSEEK_API_KEY",
    "LMSTUDIO_API_KEY",
    "LM_STUDIO_API_KEY",
    "OPENROUTER_API_KEY",
}


class McpToolPolicyError(ValueError):
    """A malformed Kiro Crew per-session MCP tool policy."""


def parse_session_tool_policy(
    params: object,
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Read the adapter-private, namespaced per-tool projection from ACP ``_meta``.

    Legacy callers may omit the extension and retain the historical all-mounted-tools
    behavior. Once the extension is present, malformed or unknown versions fail closed
    instead of silently advertising a wider MCP schema set.
    """
    if not isinstance(params, Mapping):
        raise McpToolPolicyError("session parameters must be an object")
    if "_meta" not in params:
        return {}, {}
    meta = params["_meta"]
    if not isinstance(meta, Mapping):
        raise McpToolPolicyError("_meta must be an object")
    if "kirocrew" not in meta:
        return {}, {}
    crew = meta["kirocrew"]
    if not isinstance(crew, Mapping):
        raise McpToolPolicyError("_meta.kirocrew must be an object")
    if "mcpToolPolicy" not in crew:
        return {}, {}
    raw = crew["mcpToolPolicy"]
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "allowedToolsByServer",
        "deniedToolsByServer",
    }:
        raise McpToolPolicyError("mcpToolPolicy has an unsupported shape")
    version = raw.get("version")
    if isinstance(version, bool) or version != 1:
        raise McpToolPolicyError("mcpToolPolicy version is unsupported")

    def read_map(key: str) -> dict[str, frozenset[str]]:
        value = raw.get(key)
        if not isinstance(value, Mapping):
            raise McpToolPolicyError(f"mcpToolPolicy.{key} must be an object")
        result: dict[str, frozenset[str]] = {}
        for server, tools in value.items():
            if not isinstance(server, str) or not server or not isinstance(tools, list):
                raise McpToolPolicyError(f"mcpToolPolicy.{key} contains an invalid entry")
            if any(not isinstance(tool, str) or not tool for tool in tools):
                raise McpToolPolicyError(f"mcpToolPolicy.{key} contains an invalid tool")
            if len(set(tools)) != len(tools):
                raise McpToolPolicyError(f"mcpToolPolicy.{key} contains duplicate tools")
            result[server] = frozenset(tools)
        return result

    return read_map("allowedToolsByServer"), read_map("deniedToolsByServer")


def mcp_tool_policy_allows(session: Any, exposed_name: str) -> bool:
    """Return whether this session's policy permits a bridged model tool."""
    tools = getattr(session, "mcp_tools", None)
    meta = tools.get(exposed_name) if isinstance(tools, dict) else None
    if not isinstance(meta, dict):
        return False
    server = meta.get("server")
    tool = meta.get("tool")
    if not isinstance(server, str) or not isinstance(tool, str):
        return False
    allowed_by_server = getattr(session, "mcp_tool_allowlist", {})
    denied_by_server = getattr(session, "mcp_tool_denylist", {})
    if server in allowed_by_server and tool not in allowed_by_server[server]:
        return False
    return tool not in denied_by_server.get(server, ())


def mcp_identity_raw_input(session: Any, name: str, raw_input: dict) -> dict:
    """Stamp adapter-resolved identity onto model-originated MCP arguments.

    The model's arguments are untrusted. Resolve identity from its exposed name
    and live MCP handle so Kiro Crew's permission checks can identify the actual
    server and tool, even if the model supplied forged identity fields.
    """
    tools = getattr(session, "mcp_tools", None)
    meta = tools.get(name) if isinstance(tools, dict) else None
    if not isinstance(meta, dict):
        return raw_input
    server = meta.get("server")
    tool = meta.get("tool")
    handles = getattr(session, "mcp_handles", None)
    if not isinstance(handles, dict) or not isinstance(server, str) or not server:
        return raw_input
    handle = handles.get(server)
    if handle is None or getattr(handle, "name", server) != server:
        return raw_input
    if not isinstance(tool, str) or not tool:
        return raw_input
    return {**raw_input, "server": server, "tool": tool}


def _model_tool_name(raw_name: str, server_name: str, taken: set[str]) -> str:
    """Return a legal, deterministic model-facing name for an MCP tool."""
    if (
        len(raw_name) <= _MODEL_TOOL_NAME_MAX
        and _MODEL_TOOL_NAME_RE.fullmatch(raw_name)
        and raw_name not in taken
    ):
        return raw_name

    source = f"{server_name}__{raw_name}"
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", source).strip("_") or "mcp_tool"
    base = base[:_MODEL_TOOL_NAME_MAX]
    if base not in taken:
        return base

    digest = hashlib.sha256(f"{server_name}\0{raw_name}".encode("utf-8")).hexdigest()[:10]
    suffix = f"_{digest}"
    candidate = f"{base[:_MODEL_TOOL_NAME_MAX - len(suffix)]}{suffix}"
    if candidate not in taken:
        return candidate

    counter = 2
    while True:
        suffix = f"_{digest}_{counter}"
        candidate = f"{base[:_MODEL_TOOL_NAME_MAX - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1


def _stdio_environment(spec: dict) -> dict[str, str]:
    """Build an MCP child environment without provider adapter credentials.

    The manifest overlay is intentionally filtered too: a session-provided MCP
    declaration must not be able to restore credentials removed from the
    ambient process environment.
    """

    env = {key: value for key, value in os.environ.items() if key not in _PROVIDER_SECRET_ENV}
    for entry in spec.get("env") or []:
        name = str(entry.get("name"))
        if name in _PROVIDER_SECRET_ENV:
            continue
        env[name] = str(entry.get("value"))
    return env


class McpToolError(RuntimeError):
    pass


def mcp_result_text(result) -> str:
    parts = []
    for block in (result or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "\n".join(p for p in parts if p)
    if (result or {}).get("isError"):
        text = f"ERROR: {text or 'tool reported failure'}"
    return text or "(empty result)"


class McpConnection:
    """One MCP server (stdio subprocess or streamable-HTTP endpoint)."""

    def __init__(self, spec: dict, client_name: str = "kirocrew-acp"):
        self.name = str(spec.get("name") or "mcp")
        self.spec = spec
        self.client_name = client_name
        self.tools: list[dict[str, Any]] = []
        self._next_id = 0
        self._proc: subprocess.Popen | None = None
        self._session_hdr: str | None = None
        self._timer: threading.Timer | None = None
        self._cleanup_path: str | None = None

    # -- transports --------------------------------------------------------
    def _stdio_send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpToolError(f"MCP server {self.name!r} has no writable stdin")
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _stdio_rpc(self, method, params, timeout):
        self._next_id += 1
        rid = self._next_id
        self._stdio_send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise McpToolError(f"MCP server {self.name!r} timed out during {method}")
            proc = self._proc
            if proc is None or proc.stdout is None:
                raise McpToolError(f"MCP server {self.name!r} has no readable stdout")
            line = proc.stdout.readline()
            if not line:
                raise McpToolError(f"MCP server {self.name!r} closed its output during {method}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # non-JSON noise on the channel
            if msg.get("id") != rid:
                continue  # notification / unrelated response
            if "error" in msg:
                raise McpToolError(f"MCP server {self.name!r}: {msg['error']}")
            return msg.get("result") or {}

    def _http_headers(self):
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        for entry in self.spec.get("headers") or []:
            headers[str(entry.get("name"))] = str(entry.get("value"))
        if self._session_hdr:
            headers["Mcp-Session-Id"] = self._session_hdr
        return headers

    def _http_rpc(self, method, params, timeout):
        self._next_id += 1
        rid = self._next_id
        body = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        ).encode()
        request = urllib.request.Request(
            str(self.spec.get("url")), data=body, method="POST", headers=self._http_headers()
        )
        with urllib.request.urlopen(request, timeout=timeout) as raw:
            if raw.headers.get("Mcp-Session-Id"):
                self._session_hdr = raw.headers["Mcp-Session-Id"]
            ctype = raw.headers.get("Content-Type", "")
            payload = raw.read().decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            for line in payload.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[len("data:") :].strip())
                except Exception:
                    continue
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise McpToolError(f"MCP server {self.name!r}: {msg['error']}")
                    return msg.get("result") or {}
            raise McpToolError(f"MCP server {self.name!r}: no response in SSE stream")
        try:
            msg = json.loads(payload)
        except Exception as e:
            raise McpToolError(f"MCP server {self.name!r}: bad JSON response ({e})") from e
        if msg.get("id") == rid:
            if "error" in msg:
                raise McpToolError(f"MCP server {self.name!r}: {msg['error']}")
            return msg.get("result") or {}
        raise McpToolError(f"MCP server {self.name!r}: mismatched response id")

    def _rpc(self, method, params, timeout):
        if self._proc is not None:
            return self._stdio_rpc(method, params, timeout)
        return self._http_rpc(method, params, timeout)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        transport = str(self.spec.get("type") or "stdio")
        if transport in ("http", "sse", "streamable-http"):
            self._http_rpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": "0.5.0"},
                },
                MCP_TIMEOUT_SECS,
            )
        else:
            # Provider credentials authenticate the adapter only. They are not
            # ambient authority for MCP subprocesses mounted into the session.
            argv = [str(self.spec.get("command"))] + [str(a) for a in self.spec.get("args") or []]
            wrapped, env, self._cleanup_path = sandboxed_spawn_argv(
                argv,
                mode="standard",
                env=_stdio_environment(self.spec),
                strip_python_env=True,
            )
            self._proc = popen_limited(
                wrapped,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                start_new_session=True,
                env=env,
            )
            self._stdio_rpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": "0.5.0"},
                },
                MCP_TIMEOUT_SECS,
            )
            try:
                self._stdio_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except Exception:
                pass
        self._load_tools()

    def _load_tools(self) -> None:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params, MCP_TIMEOUT_SECS)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def close(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._proc is not None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._cleanup_path:
            try:
                os.unlink(self._cleanup_path)
            except OSError:
                pass
            self._cleanup_path = None

    def call_tool(self, tool_name, arguments, should_cancel=None):
        """Generator mirroring the harness iter_tool contract: yields
        ("progress", text) roughly every 2s (keeps the client's tool-stall
        watchdog fed and the dashboard live), then a final ("final", text).
        The actual tools/call runs in a worker thread so the waiting loop can
        poll the cancel flag. A hard timer kills the whole stdio process
        group at the MCP budget."""
        started = time.monotonic()
        timed_out = {"flag": False}

        def _kill():
            timed_out["flag"] = True
            self.close()

        timer = threading.Timer(MCP_TIMEOUT_SECS, _kill)
        self._timer = timer
        timer.daemon = True
        timer.start()

        box = {}

        def _work():
            try:
                box["result"] = self._rpc(
                    "tools/call", {"name": tool_name, "arguments": arguments}, MCP_TIMEOUT_SECS
                )
            except Exception as e:  # noqa: BLE001 — surfaced as the tool result
                box["error"] = e

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        last_emit = time.monotonic()
        try:
            while worker.is_alive():
                if should_cancel is not None and should_cancel():
                    self.close()
                    yield "final", "ERROR: cancelled by user; tool not completed."
                    return
                now = time.monotonic()
                if now - last_emit >= 2.0:
                    yield "progress", (
                        f"mcp {self.name}/{tool_name} running… {int(now - started)}s elapsed"
                    )
                    last_emit = now
                worker.join(0.25)
            if timed_out["flag"]:
                yield "final", (
                    f"ERROR: MCP tool exceeded the {MCP_TIMEOUT_SECS:.0f}s budget "
                    "and was killed."
                )
                return
            if "error" in box:
                yield "final", f"ERROR: {box['error']}"
                return
            yield "final", mcp_result_text(box.get("result"))
        finally:
            if self._timer:
                self._timer.cancel()
                self._timer = None


def iter_mcp_tool(session, name, args_json, should_cancel=None):
    """Adapter-level tool iterator for MCP tools — same ("kind", text) stream
    contract as the harness's iter_tool, so the prompt loop treats both
    identically."""
    meta = session.mcp_tools.get(name) or {}
    conn = session.mcp_handles.get(meta.get("server"))
    if conn is None:
        yield "final", f"ERROR: MCP server for tool {name!r} is not connected"
        return
    try:
        arguments = json.loads(args_json or "{}")
    except Exception:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {"input": arguments}
    yield from conn.call_tool(meta.get("tool", name), arguments, should_cancel=should_cancel)


def bridge_session_servers(session, client_name: str, log_tag: str):
    """Lazy-connect ``session.mcp_servers`` on first prompt and merge their
    tools into ``session.mcp_tools``.

    Built-in harness tools win their names; every bridged MCP tool receives a
    legal model-facing name. Unique legal names are preserved; otherwise the
    name is a deterministic server-qualified alias. A server that fails to
    start is logged and skipped — it only shrinks the tool list, never the
    turn. Idempotent: a session with tools already mounted is left untouched.
    """
    if not getattr(session, "mcp_servers", None) or session.mcp_tools:
        return
    taken = set(MCP_BUILTIN_TOOLS)
    for spec in session.mcp_servers:
        if not (spec.get("command") or spec.get("url")):
            continue
        conn = McpConnection(spec, client_name=client_name)
        try:
            conn.start()
        except Exception as e:  # noqa: BLE001 — degradation, not failure
            conn.close()
            print(
                f"[{log_tag}] MCP server {spec.get('name')!r} " f"unavailable: {e}",
                file=sys.stderr,
                flush=True,
            )
            continue
        session.mcp_handles[conn.name] = conn
        for tool in conn.tools:
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            exposed = _model_tool_name(tool_name, conn.name, taken)
            taken.add(exposed)
            session.mcp_tools[exposed] = {
                "server": conn.name,
                "tool": tool_name,
                "schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                "description": str(tool.get("description") or ""),
            }
    if session.mcp_tools:
        print(
            f"[{log_tag}] MCP bridge: {len(session.mcp_tools)} tool(s) "
            f"from {len(session.mcp_handles)} server(s): " + ", ".join(sorted(session.mcp_tools)),
            file=sys.stderr,
            flush=True,
        )


def mcp_tools_spec(session) -> list[dict]:
    """OpenAI-style specs for this session's allowed bridged MCP tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": exposed,
                "description": meta["description"] or f"MCP tool {meta['tool']}",
                "parameters": meta["schema"],
            },
        }
        for exposed, meta in session.mcp_tools.items()
        if mcp_tool_policy_allows(session, exposed)
    ]


def close_session_handles(session) -> None:
    """Kill every MCP subprocess/handle owned by a session (close/delete)."""
    for conn in getattr(session, "mcp_handles", {}).values():
        try:
            conn.close()
        except Exception:
            pass
    session.mcp_handles.clear()
