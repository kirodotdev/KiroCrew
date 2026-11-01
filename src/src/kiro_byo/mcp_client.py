"""Session-scoped MCP client used by the Kiro BYO harness.

0.1.3 keeps Crew MCP's broker security boundary but removes MCP discovery
from the synchronous ``session/new``/``session/load`` critical path.

The design intentionally follows the patterns used by Kiro Crew's pooled MCP
projection and modern agent runtimes:

* session creation schedules MCP warm-up and returns immediately;
* the first prompt waits only when a tool catalogue is genuinely still unknown;
* stable Crew-owned catalogues are remembered process-wide as "last known" tool
  schemas, while each session still owns its own authenticated stdio processes;
* actual calls never reuse another session's process or HTTP session identity;
* stdio and Streamable HTTP auto-negotiate modern/legacy MCP; deprecated SSE fails closed;
* failed/stale connections may retry on the call path instead of silently
  turning a cached schema into ambient host access.

Kiro BYO exposes native read/glob/grep separately; Crew MCP still does not grant native shell/write authority.
"""

from __future__ import annotations

import hashlib
import difflib
import importlib.metadata
import importlib.util
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection

from . import __version__
from .config import home_dir
from .mcp_transports import (
    HttpMcpServer,
    MCP_LATEST_HANDSHAKE_VERSION,
    MCP_MODERN_VERSION,
    McpError,
    McpInputRequired,
    McpJsonRpcError,
    McpProtocolError,
    McpTransportError,
    StdioMcpServer,
)


DEFERRED_TOOL_SEARCH_NAME = "mcp__kiro_byo__tool_search"
DEFERRED_TOOL_ACTIVE_LIMIT = 12

# Wire revision constants are owned by mcp_transports.  Re-exporting the imported
# names here preserves the historical public surface without creating a second
# source of truth.


_DEFERRED_TOOL_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DEFERRED_TOOL_SEARCH_NAME,
        "description": (
            "Search Kiro Crew's deferred tool catalogue and load only the relevant "
            "tool definitions for the next model step. Use this before a Crew "
            "capability whose exact function is not already present. Search with "
            "short English capability keywords (for example: 'chat history', "
            "'memory recall', 'schedule reminder', 'desktop screenshot'). This "
            "search only loads function definitions and has no external side effect."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short English capability keywords describing the needed tool.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "Maximum number of tool definitions to load. Default 8.",
                },
                "server": {
                    "type": "string",
                    "description": (
                        "Optional MCP server name filter, such as kirocrew-core, "
                        "kirocrew-cron, or kirocrew-computer."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ToolBinding:
    public_name: str
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    # MCP annotations are untrusted behavior hints. They are forwarded for UI
    # context only and never used to auto-approve or lower policy.
    annotations: dict[str, bool] = field(default_factory=dict)



def _safe_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return value or "tool"


# These are Crew-owned broker surfaces. Their *process/session identity* must
# remain per-session, but their tool schemas are stable for a running Crew build.
# Ignoring session-token env values only for these names gives useful process-wide
# schema reuse without making a third-party server's env-dependent catalogue lie.
_CREW_STABLE_CATALOG_SERVERS = frozenset({
    "kirocrew-core",
    "kirocrew-cron",
    "kirocrew-computer",
})
# Crew's member DM control servers carry a different identity per session.
# Unlike an arbitrary third-party MCP they MUST complete a live handshake
# before their cached tool schemas can become executable or visible.
_CREW_MEMBER_MOUNT_SERVERS = frozenset({"kirocrew-dashboard", "kirocrew-panel"})
_CREW_LIVE_REQUIRED_SERVERS = _CREW_STABLE_CATALOG_SERVERS | _CREW_MEMBER_MOUNT_SERVERS


def _crew_build_fingerprint() -> str:
    """Best-effort build identity for safe persisted Crew schema caching.

    The cache stores no credentials and never reuses a live MCP process.  A Crew
    package update changes either distribution metadata or the module mtime, so
    stale built-in schemas naturally fall into a different key.
    """
    version = "unknown"
    for dist in ("kiro-crew", "kiro_crew"):
        try:
            version = importlib.metadata.version(dist)
            break
        except importlib.metadata.PackageNotFoundError:
            continue
    origin = ""
    mtime = 0
    try:
        spec = importlib.util.find_spec("kiro_crew")
        if spec is not None and spec.origin:
            origin = str(Path(spec.origin).resolve())
            mtime = int(Path(spec.origin).stat().st_mtime_ns)
    except (OSError, ValueError):
        pass
    raw = f"{version}|{origin}|{mtime}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass(frozen=True)
class McpCallOutcome:
    """Protocol-level outcome separate from model-readable tool text.

    ``dispatched`` records that the remote invocation was attempted. A failed
    dispatched call may have committed side effects, even if a server reports
    ``isError``. Text is never used as the source of failure status.
    """

    text: str
    is_error: bool = False
    dispatched: bool = False


class McpManager:
    """Own the MCP subprocesses for one ACP session.

    Processes are never shared between ACP sessions.  Only immutable tool schema
    catalogues may be reused process-wide, mirroring the "last known tools"
    behavior common in long-lived MCP clients while preserving Crew's per-session
    broker identity and permission boundary.
    """

    _catalog_cache_lock = threading.RLock()
    _catalog_cache: dict[str, list[dict[str, Any]]] = {}
    _durable_catalog_keys: set[str] = set()
    _persistent_cache_loaded = False

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._load_persistent_cache_once()
        self.servers: dict[str, StdioMcpServer | HttpMcpServer] = {}
        self.bindings: dict[str, ToolBinding] = {}
        self.startup_durations_ms: dict[str, float] = {}
        self.startup_failures: dict[str, str] = {}
        self.catalog_cache_hits: set[str] = set()
        # A persisted schema is only a display hint, never proof that this
        # session's Crew broker has granted the tool. Authenticated handshakes
        # establish live verification separately for every session.
        self._live_verified_servers: set[str] = set()
        self._last_host_refresh: float = 0.0
        self._host_refresh_lock = threading.Lock()
        self._binding_lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._start_scheduled = False
        self._closed = False
        self._raw_entries: list[dict[str, Any]] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "stdio").lower()
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if name in self.servers:
                raise McpError(f"duplicate MCP server name {name!r} in session projection")
            if kind in {"", "stdio"}:
                command = str(entry.get("command") or "").strip()
                args_raw = entry.get("args") or []
                if not command or not isinstance(args_raw, list):
                    raise McpError(f"invalid stdio MCP server {name!r}: command/args are required")
                env: dict[str, str] = {}
                raw_env = entry.get("env") or []
                if isinstance(raw_env, list):
                    for pair in raw_env:
                        if isinstance(pair, dict) and isinstance(pair.get("name"), str):
                            env[str(pair["name"])] = str(pair.get("value") or "")
                args = [str(v) for v in args_raw]
                self.servers[name] = StdioMcpServer(name, command, args, env)
                self._raw_entries.append({
                    "type": "stdio",
                    "name": name,
                    "command": command,
                    "args": args,
                    "env": dict(env),
                })
                continue
            if kind == "http":
                url = str(entry.get("url") or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    raise McpError(f"invalid HTTP MCP server {name!r}: absolute http(s) url is required")
                headers: dict[str, str] = {}
                raw_headers = entry.get("headers") or []
                if isinstance(raw_headers, list):
                    for pair in raw_headers:
                        if isinstance(pair, dict) and isinstance(pair.get("name"), str):
                            headers[str(pair["name"])] = str(pair.get("value") or "")
                self.servers[name] = HttpMcpServer(name, url, headers)
                self._raw_entries.append({"type": "http", "name": name, "url": url, "headers": dict(headers)})
                continue
            raise McpError(
                f"unsupported MCP transport {kind!r} for server {name!r}; "
                f"Kiro BYO {__version__} supports stdio and Streamable HTTP. Deprecated SSE fails closed."
            )

        self._seed_cached_catalogues()
        if not self.servers:
            self._ready_event.set()

    @classmethod
    def clear_process_catalog_cache(cls) -> None:
        """Test/debug hook. Does not affect any already-created manager."""
        with cls._catalog_cache_lock:
            cls._catalog_cache.clear()
            cls._durable_catalog_keys.clear()
            cls._persistent_cache_loaded = False

    @staticmethod
    def _persistent_cache_path() -> Path:
        return home_dir() / "data" / "mcp_catalog_cache_v1.json"

    @classmethod
    def _load_persistent_cache_once(cls) -> None:
        with cls._catalog_cache_lock:
            if cls._persistent_cache_loaded:
                return
            cls._persistent_cache_loaded = True
            try:
                payload = json.loads(cls._persistent_cache_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                return
            raw = payload.get("catalogues")
            if not isinstance(raw, dict):
                return
            for key, tools in raw.items():
                if not isinstance(key, str) or not isinstance(tools, list):
                    continue
                clean = [tool for tool in tools if isinstance(tool, dict)]
                cls._catalog_cache[key] = clean
                cls._durable_catalog_keys.add(key)

    @classmethod
    def _persist_catalog_cache(cls) -> None:
        # Persist only opaque-keyed public schemas; never command lines, env or
        # broker/session tokens. Third-party entries can live in memory but are
        # intentionally excluded from the durable cache.
        try:
            path = cls._persistent_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with cls._catalog_cache_lock:
                payload = {
                    "schema_version": 1,
                    "updated_at": time.time(),
                    "catalogues": {
                        key: cls._catalog_cache[key]
                        for key in cls._durable_catalog_keys
                        if key in cls._catalog_cache
                    },
                }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _catalog_key(server: StdioMcpServer | HttpMcpServer) -> str:
        if isinstance(server, StdioMcpServer):
            env: list[tuple[str, str]]
            if server.name in _CREW_STABLE_CATALOG_SERVERS:
                # Crew's broker/session token changes per session, but does not change
                # the public schema of these three built-in servers. Include the Crew
                # build fingerprint so an app update cannot inherit stale schemas.
                env = []
                build = _crew_build_fingerprint()
            else:
                env = sorted(server.env.items())
                build = "session-sensitive"
            identity: Any = ["stdio", server.name, server.command, server.args, env, build]
        else:
            # Header values can contain OAuth/API secrets. Hashing the complete
            # identity is useful for in-memory cache invalidation, but HTTP
            # catalogues are never added to the durable cache below.
            identity = ["http", server.name, server.url, sorted(server.headers.items())]
        payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def is_crew_member_mount(self, server_name: str) -> bool:
        """Identify Crew's per-session member control mounts (never cached).

        A same-named arbitrary MCP server is not sufficient. Crew itself
        supplies a session-scoped stdio invocation and member identity in
        session/new; the Crew dashboard MCP still validates that identity on
        each request. This is a VISIBILITY hint, not a permission grant.
        """
        invocation = {"kirocrew-dashboard": "mcp-dashboard", "kirocrew-panel": "mcp-panel"}.get(server_name)
        if invocation is None:
            return False
        for entry in self._raw_entries:
            if entry.get("name") != server_name or entry.get("type") != "stdio":
                continue
            args = entry.get("args") or []
            env = entry.get("env") or {}
            key = str(env.get("KIROCREW_SESSION_KEY") or "")
            for prefix in ("dashboard_", "dashboard:"):
                if key.startswith(prefix):
                    key = key[len(prefix):]
                    break
            if (invocation in args and key.startswith("member-")
                    and len(key) > len("member-")):
                return True
        return False

    def has_crew_member_mount(self) -> bool:
        return any(self.is_crew_member_mount(name) for name in self.servers)

    def signature(self) -> str:
        """Exact connection signature for diagnostics/reuse decisions."""
        payload = json.dumps(self._raw_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _cached_tools(cls, server: StdioMcpServer | HttpMcpServer) -> list[dict[str, Any]] | None:
        key = cls._catalog_key(server)
        with cls._catalog_cache_lock:
            value = cls._catalog_cache.get(key)
            return [dict(tool) for tool in value] if value is not None else None

    @classmethod
    def _remember_tools(cls, server: StdioMcpServer | HttpMcpServer) -> None:
        key = cls._catalog_key(server)
        fresh = [dict(tool) for tool in server.tools]
        changed = False
        with cls._catalog_cache_lock:
            if cls._catalog_cache.get(key) != fresh:
                cls._catalog_cache[key] = fresh
                changed = True
            if isinstance(server, StdioMcpServer) and server.name in _CREW_STABLE_CATALOG_SERVERS:
                cls._durable_catalog_keys.add(key)
        if changed and isinstance(server, StdioMcpServer) and server.name in _CREW_STABLE_CATALOG_SERVERS:
            cls._persist_catalog_cache()

    def _seed_cached_catalogues(self) -> None:
        for name, server in self.servers.items():
            cached = self._cached_tools(server)
            if cached is None:
                continue
            server.tools = cached
            self.catalog_cache_hits.add(name)
        self._rebuild_bindings()

    def _rebuild_bindings(self) -> None:
        bindings: dict[str, ToolBinding] = {}
        # The original mcpServers order is preserved by dict insertion order.
        # Sorting only the exported public name later makes provider request
        # prefixes deterministic across parallel startup completion order.
        for name, server in self.servers.items():
            for tool in server.tools:
                tool_name = tool.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                public = f"mcp__{_safe_component(name)}__{_safe_component(tool_name)}"
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                desc = tool.get("description")
                raw_annotations = tool.get("annotations")
                annotations: dict[str, bool] = {}
                if isinstance(raw_annotations, dict):
                    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                        value = raw_annotations.get(key)
                        if isinstance(value, bool):
                            annotations[key] = value
                bindings[public] = ToolBinding(
                    public_name=public,
                    server_name=name,
                    tool_name=tool_name,
                    description=str(desc or f"{name}: {tool_name}"),
                    input_schema=schema,
                    annotations=annotations,
                )
        with self._binding_lock:
            self.bindings = bindings

    def _start_impl(self) -> None:
        try:
            items = list(self.servers.items())
            if not items or self._closed:
                return

            max_workers = min(4, len(items))

            def _start_one(server: StdioMcpServer | HttpMcpServer) -> float:
                t0 = time.perf_counter()
                server.start()
                return (time.perf_counter() - t0) * 1000.0

            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kiro-byo-mcp-start") as pool:
                future_map = {pool.submit(_start_one, server): (name, server) for name, server in items}
                for future in as_completed(future_map):
                    name, server = future_map[future]
                    if self._closed:
                        server.close()
                        continue
                    try:
                        self.startup_durations_ms[name] = float(future.result())
                        self.startup_failures.pop(name, None)
                        with self._binding_lock:
                            self._live_verified_servers.add(name)
                        self._remember_tools(server)
                    except Exception as exc:
                        self.startup_failures[name] = str(exc)
                        with self._binding_lock:
                            self._live_verified_servers.discard(name)
                        # If a last-known catalogue exists it remains visible, as
                        # in long-lived MCP runtimes; calls will retry connection.
                        cached = self._cached_tools(server)
                        if cached is not None and name not in _CREW_LIVE_REQUIRED_SERVERS:
                            server.tools = cached
                        else:
                            # A failed Crew broker cannot authorize a stale
                            # cached capability, even if its schema is known.
                            server.tools = []
                    self._rebuild_bindings()
        finally:
            # Initial live discovery is also a successful capabilities check;
            # don't immediately re-query all brokers on the first prompt.
            self._last_host_refresh = time.monotonic()
            self._ready_event.set()

    def start_background(self) -> None:
        """Schedule warm-up without blocking ACP session creation."""
        with self._start_lock:
            if self._start_scheduled or self._closed:
                return
            self._start_scheduled = True
            threading.Thread(
                target=self._start_impl,
                name="kiro-byo-mcp-warmup",
                daemon=True,
            ).start()

    def start(self, timeout: float | None = None) -> None:
        """Compatibility synchronous start; joins the background warm-up."""
        self.start_background()
        if not self._ready_event.wait(timeout=timeout):
            raise McpError("MCP startup timed out")

    def catalog_ready(self) -> bool:
        # A full cached catalogue lets a prompt proceed while this session's
        # authenticated stdio processes continue warming in the background.
        with self._binding_lock:
            cached_names = set(self.catalog_cache_hits)
        return self._ready_event.is_set() or cached_names.issuperset(self.servers.keys())

    def wait_for_catalog(self, timeout: float = 20.0) -> bool:
        if self.catalog_ready():
            return True
        self.start_background()
        return self._ready_event.wait(timeout=timeout)

    def wait_for_live_crew_catalog(self, server_name: str, timeout: float = 8.0) -> bool:
        """Join the actual per-session broker handshake, not cached schemas.

        catalog_ready() intentionally accepts last-known schema hints; this
        stronger barrier is only for explicit browser actions, where a stale
        cache must not make the first turn miss a newly enabled Crew browser.
        """
        if server_name not in self.servers or self._closed:
            return False
        with self._binding_lock:
            if server_name in self._live_verified_servers:
                return True
        self.start_background()
        self._ready_event.wait(timeout=timeout)
        with self._binding_lock:
            return server_name in self._live_verified_servers

    @staticmethod
    def deferred_tool_search_schema() -> dict[str, Any]:
        # Return a detached copy because provider adapters may normalize schemas.
        return json.loads(json.dumps(_DEFERRED_TOOL_SEARCH_SCHEMA))

    def is_live_binding(self, name: str) -> bool:
        """True only if a Crew built-in tool has a live session handshake.

        Non-Crew servers keep their historical deferred discovery behavior;
        this condition does not approve tool execution or bypass the host gate.
        """
        with self._binding_lock:
            binding = self.bindings.get(name)
            return bool(binding is not None and (
                binding.server_name not in _CREW_LIVE_REQUIRED_SERVERS
                or binding.server_name in self._live_verified_servers
            ))

    def revalidate_requested_crew_binding(self, public_name: str) -> bool:
        """Try a fresh *host-issued* tools/list for an unavailable Crew tool.

        This does not manufacture a grant: only a broker already projected in
        session/new or session/load can be contacted, and the returned binding
        must come from that authenticated server. In particular Crew chat
        'trust' is not permission to add MCP servers or reuse stale catalogues.
        """
        if self._closed or not isinstance(public_name, str):
            return False
        with self._binding_lock:
            binding = self.bindings.get(public_name)
            if binding is not None and self.is_live_binding(public_name):
                return True
        parts = public_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp" or not parts[2]:
            return False
        name = parts[1]
        if name not in _CREW_LIVE_REQUIRED_SERVERS:
            return False
        server = self.servers.get(name)
        if not isinstance(server, StdioMcpServer):
            return False
        # A cold startup owns the same server connection. Wait for its
        # per-session handshake before attempting any explicit revalidation.
        if not self._ready_event.is_set():
            self.start_background()
            self._ready_event.wait(timeout=8.0)
        if self._closed:
            return False
        with self._host_refresh_lock:
            if self._closed:
                return False
            with self._binding_lock:
                binding = self.bindings.get(public_name)
                if binding is not None and self.is_live_binding(public_name):
                    return True
            try:
                server.refresh_tools()
                with self._binding_lock:
                    self._live_verified_servers.add(name)
                self.startup_failures.pop(name, None)
                self._remember_tools(server)
            except Exception as exc:
                self.startup_failures[name] = str(exc)
                with self._binding_lock:
                    self._live_verified_servers.discard(name)
                server.tools = []
            self._rebuild_bindings()
            return self.is_live_binding(public_name)

    def refresh_host_capabilities(self, *, interval: float = 60.0) -> bool:
        """Refresh live Crew broker catalogues at a turn boundary, at most once
        per interval. An unavailable broker fails closed; never add servers that
        Crew did not project via session/new or session/load.
        """
        if self._closed or not self._ready_event.is_set():
            return False
        now = time.monotonic()
        if now - self._last_host_refresh < interval:
            return False
        if not self._host_refresh_lock.acquire(blocking=False):
            return False
        changed = False
        try:
            if self._closed or time.monotonic() - self._last_host_refresh < interval:
                return False
            self._last_host_refresh = time.monotonic()
            for name, server in list(self.servers.items()):
                if name not in _CREW_LIVE_REQUIRED_SERVERS:
                    continue
                if not isinstance(server, StdioMcpServer):
                    continue
                # Retry the current broker after a transient refresh failure.
                # Failing closed must not permanently brick this session until
                # Crew restarts or the user opens a new conversation.
                before = [dict(tool) for tool in server.tools]
                try:
                    fresh = server.refresh_tools()
                    with self._binding_lock:
                        self._live_verified_servers.add(name)
                    self.startup_failures.pop(name, None)
                    self._remember_tools(server)
                except Exception as exc:
                    self.startup_failures[name] = str(exc)
                    with self._binding_lock:
                        self._live_verified_servers.discard(name)
                    server.tools = []
                    fresh = []
                if before != fresh:
                    changed = True
                self._rebuild_bindings()
            return changed
        finally:
            self._host_refresh_lock.release()

    def host_capability_snapshot(
        self, *, allowed: Callable[[ToolBinding], bool] | None = None
    ) -> dict[str, Any]:
        """Non-secret, per-session snapshot of host-projected tools and health.

        Session broker tokens, headers, arguments and any user prompts are never
        returned. This is an observation, not an independent grant authority.
        """
        with self._binding_lock:
            confirmed = set(self._live_verified_servers)
            available = sorted(
                item.public_name for item in self.bindings.values()
                if (allowed is None or allowed(item))
                and (item.server_name not in _CREW_LIVE_REQUIRED_SERVERS
                     or item.server_name in confirmed)
            )
        return {
            "servers": sorted(self.servers),
            "verified_crew_servers": sorted(confirmed & _CREW_LIVE_REQUIRED_SERVERS),
            "pending_crew_servers": sorted((set(self.servers) & _CREW_LIVE_REQUIRED_SERVERS) - confirmed),
            "failed_servers": sorted(self.startup_failures),
            "available_tools": available,
            "crew_browser": "mcp__kirocrew-core__browser" in available,
        }

    def crew_browser_tool_names(
        self, *, allowed: Callable[[ToolBinding], bool] | None = None
    ) -> tuple[str, ...]:
        """Recognize only the browser capability granted by Crew's core MCP.

        Never synthesize a tool or infer permission from the dashboard setting:
        KiroCrew owns its browser panel / playwright fallback, and the MCP
        server rechecks its live governance and panel availability on each call.
        Third-party tools also named ``browser`` are not elevated here.
        """
        with self._binding_lock:
            return tuple(sorted(
                item.public_name for item in self.bindings.values()
                if item.server_name == "kirocrew-core"
                and item.server_name in self._live_verified_servers
                and item.tool_name == "browser"
                and (allowed is None or allowed(item))
            ))

    def crew_skill_tool_names(
        self, *, allowed: Callable[[ToolBinding], bool] | None = None
    ) -> tuple[str, ...]:
        """Only surface *live*, Crew-owned skill discovery as a default tool.

        A Skill is a Crew resource, not an independent BYO permission grant.
        The host still owns agent skill activation and tool approvals. Other
        similarly named third-party tools are never privileged or exposed.
        """
        with self._binding_lock:
            return tuple(sorted(
                item.public_name for item in self.bindings.values()
                if item.server_name == "kirocrew-core"
                and item.server_name in self._live_verified_servers
                and item.tool_name == "skill_search"
                and (allowed is None or allowed(item))
            ))

    def crew_artifact_tool_names(
        self, *, allowed: Callable[[ToolBinding], bool] | None = None
    ) -> tuple[str, ...]:
        """Load only verified, host-owned non-destructive artifact tools.

        Delete/revert are deliberately *not* preloaded: they remain available
        only through explicit deferred search and the usual permission checks.
        """
        safe_tools = {"artifact_save", "artifact_get", "artifact_update", "artifact_list", "artifact_versions"}
        with self._binding_lock:
            return tuple(sorted(
                binding.public_name for binding in self.bindings.values()
                if binding.server_name == "kirocrew-core"
                and binding.server_name in self._live_verified_servers
                and binding.tool_name in safe_tools
                and (allowed is None or allowed(binding))
            ))

    def tool_specs(
        self, names: Collection[str] | None = None, *, verified_only: bool = False
    ) -> list[dict[str, Any]]:
        selected = set(names) if names is not None else None
        with self._binding_lock:
            ordered = sorted(
                (
                    binding
                    for binding in self.bindings.values()
                    if (selected is None or binding.public_name in selected)
                    and (not verified_only
                         or binding.server_name not in _CREW_LIVE_REQUIRED_SERVERS
                         or binding.server_name in self._live_verified_servers)
                ),
                key=lambda item: item.public_name,
            )
        return [
            {
                "type": "function",
                "function": {
                    "name": binding.public_name,
                    "description": binding.description,
                    "parameters": binding.input_schema,
                },
            }
            for binding in ordered
        ]

    @staticmethod
    def _search_words(value: str) -> list[str]:
        # Public tool names/descriptions are predominantly English. The runtime
        # prompt tells the model to issue short English capability queries, which
        # keeps this lookup deterministic and provider-neutral.
        return re.findall(r"[a-z0-9]+", str(value or "").lower().replace("_", " "))

    def search_tools(
        self,
        query: str,
        *,
        limit: int = 8,
        server: str = "",
        timeout: float = 20.0,
        allowed: Callable[[ToolBinding], bool] | None = None,
        verified_only: bool = False,
    ) -> tuple[list[ToolBinding], float]:
        """Search the catalogue, joining discovery only when search is requested.

        Ordinary text turns never wait for all MCP schemas. This is the
        provider-neutral equivalent of deferred/tool-search loading: a model first
        sees one tiny search function, and only the selected real tool definitions
        are sent on the next inference step.
        """
        started = time.perf_counter()
        if not self.catalog_ready():
            self.wait_for_catalog(timeout=timeout)
        # A cached Crew schema is only a discovery hint. For an explicitly
        # verified tool search, join this session's own authenticated broker
        # startup so the first search does not incorrectly return "no tools".
        # Only bounded session-projected servers are involved; no extra server
        # is created and no saved permission is inferred from the cache.
        if verified_only and (set(self.servers) & _CREW_LIVE_REQUIRED_SERVERS):
            if not self._ready_event.is_set():
                self.start_background()
                self._ready_event.wait(timeout=min(8.0, max(0.0, timeout)))
        waited_ms = (time.perf_counter() - started) * 1000.0

        query = str(query or "").strip()
        if not query:
            return [], waited_ms
        q_words = self._search_words(query)
        q_norm = " ".join(q_words) or query.lower()
        server_filter = str(server or "").strip().lower()
        try:
            cap = max(1, min(12, int(limit)))
        except (TypeError, ValueError):
            cap = 8

        with self._binding_lock:
            candidates = list(self.bindings.values())

        scored: list[tuple[float, ToolBinding]] = []
        for binding in candidates:
            if allowed is not None and not allowed(binding):
                continue
            if verified_only and not self.is_live_binding(binding.public_name):
                continue
            if server_filter and server_filter not in binding.server_name.lower():
                continue
            name_text = (
                binding.public_name.replace("mcp__", " ")
                .replace("__", " ")
                .replace("_", " ")
            ).lower()
            desc_text = binding.description.lower()
            # Anthropic's search surface explicitly includes argument names and
            # descriptions; doing the same materially helps ambiguous Crew tools.
            schema_text = json.dumps(
                binding.input_schema, ensure_ascii=False, separators=(",", ":")
            ).lower()
            candidate_text = (
                f"{binding.server_name.lower()} {name_text} {desc_text} {schema_text}"
            )
            candidate_words = set(self._search_words(candidate_text))
            score = 0.0
            if q_norm and q_norm in candidate_text:
                score += 40.0
            for word in q_words:
                if word in candidate_words:
                    score += 12.0
                elif word in candidate_text:
                    score += 5.0
                if word and word in binding.public_name.lower():
                    score += 5.0
            score += 6.0 * difflib.SequenceMatcher(
                None, q_norm, name_text[:240]
            ).ratio()
            # A fuzzy name resemblance alone should not expose arbitrary tools.
            # Real matches normally score >= 12 from one exact capability word;
            # a small floor keeps an unhelpful search empty so the model can
            # retry with better keywords instead of calling an unrelated tool.
            if score >= 5.0:
                scored.append((score, binding))

        scored.sort(key=lambda item: (-item[0], item[1].public_name))
        return [binding for _score, binding in scored[:cap]], waited_ms

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        """Render an MCP CallToolResult for the model without hiding tool errors.

        MCP deliberately reports *tool* failures as successful JSON-RPC results
        with ``isError: true``.  Treating those as ordinary text makes a failed
        mutation indistinguishable from a successful one to the model.  Content
        is the model-facing representation and therefore wins when present;
        ``structuredContent`` is an application-facing fallback when the server
        supplied no model-readable content.
        """
        if not isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)

        texts: list[str] = []
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block_type in {"image", "image_url"}:
                    texts.append("[tool returned an image]")
                elif block_type == "audio":
                    texts.append("[tool returned audio]")
                elif block_type in {"resource", "resource_link"}:
                    # Preserve useful resource identity without dumping an opaque
                    # binary/resource payload into the model transcript.
                    resource = block.get("resource") if block_type == "resource" else block
                    if isinstance(resource, dict):
                        uri = resource.get("uri")
                        name = resource.get("name")
                        label = uri if isinstance(uri, str) and uri else name
                        texts.append(
                            f"[tool returned resource: {label}]"
                            if isinstance(label, str) and label
                            else "[tool returned a resource]"
                        )
                    else:
                        texts.append("[tool returned a resource]")

        if texts:
            rendered = "\n".join(texts)
        elif "structuredContent" in result:
            rendered = json.dumps(result.get("structuredContent"), ensure_ascii=False)
        else:
            # Keep unknown extension members observable instead of silently
            # converting an empty/novel result into success text.
            rendered = json.dumps(result, ensure_ascii=False)

        if result.get("isError") is True:
            return f"Error: MCP tool reported failure\n{rendered}"
        return rendered

    def call_with_status(self, public_name: str, arguments: dict[str, Any]) -> McpCallOutcome:
        """Call one MCP tool, preserving protocol status independently of text.

        The historical ``call() -> str`` API remains available for other callers.
        Pre-dispatch rejection is not a remote side-effect uncertainty; a call
        error *after* invoking the server is. Unexpected exceptions propagate
        to the turn dispatcher where they are terminally recorded and sanitized.
        """
        if self._closed:
            return McpCallOutcome("Error: MCP session is closed; tool was not dispatched.", True)
        with self._binding_lock:
            binding = self.bindings.get(public_name)
        if binding is None:
            # A background discovery may still be completing. Join it once before
            # declaring the tool nonexistent.
            self.wait_for_catalog(timeout=20.0)
            with self._binding_lock:
                binding = self.bindings.get(public_name)
        if binding is None:
            return McpCallOutcome(
                f"Error: unknown tool {public_name}. Do not retry nonexistent tools; use only functions present in the supplied tool list.",
                True,
            )
        if self._closed:
            return McpCallOutcome("Error: MCP session is closed; tool was not dispatched.", True)
        if not self.is_live_binding(public_name):
            return McpCallOutcome(
                f"Error: tool {public_name} is not verified in the current Crew session.", True,
            )
        server = self.servers.get(binding.server_name)
        if server is None:
            return McpCallOutcome(f"Error: MCP server {binding.server_name} is unavailable", True)
        try:
            if self._closed:
                return McpCallOutcome("Error: MCP session is closed; tool was not dispatched.", True)
            was_running = server.is_running()
            t0 = time.perf_counter()
            result = server.call(binding.tool_name, arguments)
            # A call-path restart may have refreshed the catalogue. Avoid a disk
            # cache write on ordinary tool calls where the live process and tool
            # schema did not change.
            if not was_running:
                self.startup_durations_ms.setdefault(
                    binding.server_name, (time.perf_counter() - t0) * 1000.0
                )
                self._remember_tools(server)
                self._rebuild_bindings()
        except McpError as exc:
            # This is a model-facing tool result and also enters the durable
            # transcript/trace. Never forward arbitrary peer/transport error
            # strings: they can contain authorization URLs, tokens, headers or
            # server-supplied JSON-RPC data.  Preserve the *fact* of a failed
            # dispatched call (unknown side effect) without leaking its payload.
            if isinstance(exc, McpInputRequired):
                detail = (
                    "MCP tool requires interactive client input, which this "
                    "KiroCrew bridge does not support. Do not automatically "
                    "repeat the tool call."
                )
            elif isinstance(exc, McpJsonRpcError):
                detail = "MCP tool returned a JSON-RPC error."
            elif isinstance(exc, McpProtocolError):
                detail = "MCP tool returned an unusable protocol response."
            else:
                detail = "MCP tool transport failed."
            return McpCallOutcome(f"Error: {detail}", True, True)
        # The server's isError flag, *not* a string prefix, is authoritative.
        is_error = isinstance(result, dict) and result.get("isError") is True
        return McpCallOutcome(self._render_tool_result(result), is_error, True)

    def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Legacy model-facing text API (kept for older call sites)."""
        return self.call_with_status(public_name, arguments).text

    def close(self) -> None:
        self._closed = True
        self._ready_event.set()
        with self._binding_lock:
            self._live_verified_servers.clear()
        for server in self.servers.values():
            server.close()
        self.servers.clear()
        with self._binding_lock:
            self.bindings.clear()
