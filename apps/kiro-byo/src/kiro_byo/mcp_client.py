"""Session-scoped MCP client used by the Kiro BYO harness.

0.1.0 keeps Crew MCP's broker security boundary but removes MCP discovery
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

from .config import home_dir
from .mcp_transports import (
    HttpMcpServer,
    MCP_LATEST_HANDSHAKE_VERSION,
    MCP_MODERN_VERSION,
    McpError,
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
                "Kiro BYO 0.1.0 supports stdio and Streamable HTTP. Deprecated SSE fails closed."
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
                        self._remember_tools(server)
                    except Exception as exc:
                        self.startup_failures[name] = str(exc)
                        # If a last-known catalogue exists it remains visible, as
                        # in long-lived MCP runtimes; calls will retry connection.
                        cached = self._cached_tools(server)
                        if cached is not None:
                            server.tools = cached
                        else:
                            server.tools = []
                    self._rebuild_bindings()
        finally:
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

    @staticmethod
    def deferred_tool_search_schema() -> dict[str, Any]:
        # Return a detached copy because provider adapters may normalize schemas.
        return json.loads(json.dumps(_DEFERRED_TOOL_SEARCH_SCHEMA))

    def openai_tools(
        self, names: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        selected = set(names) if names is not None else None
        with self._binding_lock:
            ordered = sorted(
                (
                    binding
                    for binding in self.bindings.values()
                    if selected is None or binding.public_name in selected
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

    def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        with self._binding_lock:
            binding = self.bindings.get(public_name)
        if binding is None:
            # A background discovery may still be completing. Join it once before
            # declaring the tool nonexistent.
            self.wait_for_catalog(timeout=20.0)
            with self._binding_lock:
                binding = self.bindings.get(public_name)
        if binding is None:
            return f"Error: unknown tool {public_name}. Do not retry nonexistent tools; use only functions present in the supplied tool list."
        server = self.servers.get(binding.server_name)
        if server is None:
            return f"Error: MCP server {binding.server_name} is unavailable"
        try:
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
            return f"Error: {exc}"
        return self._render_tool_result(result)

    def close(self) -> None:
        self._closed = True
        self._ready_event.set()
        for server in self.servers.values():
            server.close()
        self.servers.clear()
        with self._binding_lock:
            self.bindings.clear()
