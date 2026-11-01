"""Low-level MCP transports for Kiro BYO.

This module owns wire-era negotiation and process/HTTP lifecycle only.  Tool
catalog caching, deferred discovery and Crew-facing binding policy live in
``mcp_client.py``.  Keeping the transport boundary separate prevents the MCP
manager from becoming another monolithic agent runtime.
"""

from __future__ import annotations

from . import __version__

import base64
import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


_LOG = logging.getLogger(__name__)
_JS_SAFE_INTEGER_MAX = (1 << 53) - 1


class McpError(RuntimeError):
    pass


class McpProtocolError(McpError):
    """The remote endpoint answered, but not with a usable MCP response."""


class McpInputRequired(McpProtocolError):
    """A dispatched MCP call requires client interaction that Crew cannot project.

    The remote tool may already have acted.  This is NOT a pre-dispatch
    rejection or an invitation to automatically replay the tool call.
    """


class McpJsonRpcError(McpProtocolError):
    """A peer returned a structured JSON-RPC error response."""

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(f"MCP JSON-RPC error {code}: {message}")


class McpTransportError(McpError):
    """The MCP endpoint could not be reached or returned a non-negotiation HTTP failure."""


class McpRequestTimeout(McpProtocolError):
    """A request received no protocol response before its local deadline."""


class McpProbeUnsupported(McpProtocolError):
    """Affirmative evidence that the endpoint does not speak the modern probe."""


class McpNoResponse(McpProtocolError):
    """The transport completed a request without returning its JSON-RPC response."""


MCP_LATEST_HANDSHAKE_VERSION = "2025-11-25"
MCP_MODERN_VERSION = "2026-07-28"
_MAX_LIST_PAGES = 256


def _is_legacy_protocol_version(value: Any) -> bool:
    """Return whether an MCP date-version belongs to the initialize/session era."""
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    return value <= MCP_LATEST_HANDSHAKE_VERSION


def _legacy_probe_rejection(exc: BaseException) -> bool:
    """Return whether a failed ``server/discover`` is affirmative legacy evidence.

    ``UnsupportedProtocolVersion`` (-32022) is not itself evidence of a legacy
    peer: a modern server can use it to advertise a different modern revision.
    Fall back to initialize only when its required ``supported`` list actually
    contains an initialize-era version.
    """

    if isinstance(exc, (McpProbeUnsupported, McpNoResponse)):
        return True
    if isinstance(exc, McpJsonRpcError):
        if exc.code == -32601:
            return True
        if exc.code == -32022 and isinstance(exc.data, dict):
            supported = exc.data.get("supported")
            return isinstance(supported, list) and any(
                _is_legacy_protocol_version(version) for version in supported
            )
    return False


def _validate_cacheable_result(result: dict[str, Any], *, method: str) -> dict[str, Any]:
    """Validate cache hints required by modern MCP cacheable operations."""
    ttl_ms = result.get("ttlMs")
    if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or ttl_ms < 0:
        raise McpProtocolError(f"MCP {method} returned invalid ttlMs")
    cache_scope = result.get("cacheScope")
    if cache_scope not in {"public", "private"}:
        raise McpProtocolError(f"MCP {method} returned invalid cacheScope")
    return result


def _validate_discover_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate the required 2026-07-28 ``DiscoverResult`` fields.

    ``resultType`` is validated/removed by the caller's modern-result helper.
    The remaining required cache and negotiation fields must still be present;
    accepting a partial result would incorrectly classify a broken modern peer
    as usable.
    """

    versions = result.get("supportedVersions")
    if not isinstance(versions, list) or not versions or not all(isinstance(v, str) and v for v in versions):
        raise McpProtocolError("server/discover returned invalid supportedVersions")
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict):
        raise McpProtocolError("server/discover returned invalid capabilities")
    return _validate_cacheable_result(result, method="server/discover")


def _next_cursor(result: dict[str, Any], *, method: str) -> str | None:
    value = result.get("nextCursor")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise McpProtocolError(f"MCP {method} returned an invalid nextCursor")
    return value


@dataclass
class StdioMcpServer:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    process: subprocess.Popen[str] | None = None
    _responses: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    _ids: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    _era: str = "unknown"  # unknown | modern | legacy
    _initialized: bool = False
    _start_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _request_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _exit_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @staticmethod
    def _modern_params(params: dict[str, Any]) -> dict[str, Any]:
        modern = dict(params)
        existing_meta = modern.get("_meta")
        meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
        meta.update({
            "io.modelcontextprotocol/protocolVersion": MCP_MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "kiro-byo", "version": __version__},
            "io.modelcontextprotocol/clientCapabilities": {},
        })
        modern["_meta"] = meta
        return modern

    @staticmethod
    def _logical_modern_result(result: dict[str, Any]) -> dict[str, Any]:
        result_type = result.get("resultType")
        if result_type == "input_required":
            raise McpInputRequired(
                "MCP 2026-07-28 tool requested multi-round-trip client input; "
                "Kiro BYO does not yet expose MRTR elicitation/sampling/roots to Crew"
            )
        if result_type is None:
            raise McpProtocolError("MCP 2026-07-28 response is missing required resultType")
        if result_type != "complete":
            raise McpProtocolError(f"unsupported MCP 2026 resultType: {result_type}")
        if "resultType" in result:
            result = dict(result)
            result.pop("resultType", None)
        return result

    def _reader(
        self,
        proc: subprocess.Popen[str],
        responses: "queue.Queue[dict[str, Any]]",
        ended: threading.Event,
    ) -> None:
        # Capture this transport lifetime. A stale reader must never enqueue a
        # reply into a newer connection after an MCP process restart.
        try:
            if proc.stdout is None:
                return
            for raw in proc.stdout:
                if ended.is_set():
                    return
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    # Some launchers print a one-line banner before JSON-RPC.
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and isinstance(msg.get("method"), str) and "result" not in msg and "error" not in msg:
                    # Do not send a rejection from an old reader to a newer
                    # server if the session has closed/restarted meanwhile.
                    if ended.is_set() or self.process is not proc:
                        return
                    try:
                        self._send({
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "error": {"code": -32601, "message": f"unsupported MCP client method: {msg.get('method')}"},
                        })
                    except McpError:
                        pass
                    continue
                responses.put(msg)
        finally:
            ended.set()
            # Wake any request waiting for a reply if the pipe closes, even
            # when the MCP server exited without a JSON-RPC error response.
            responses.put({"_kiro_byo_transport_closed": True})

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self.process
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError(f"MCP server {self.name!r} is not running")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            if self._exit_event.is_set() or self.process is not proc:
                raise McpTransportError(f"MCP server {self.name!r} connection closed")
            try:
                proc.stdin.write(encoded)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise McpTransportError(f"MCP server {self.name!r} write failed: {exc}") from exc

    def _request(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        # Serialize the whole exchange. _ids is a plain int and _responses is a
        # single shared queue, so two concurrent calls on the same stdio server
        # could allocate colliding ids and drain/re-queue each other's replies,
        # delivering a response to the wrong caller or timing out spuriously.
        with self._request_lock:
            # The queue and cancellation event belong to one process lifetime.
            # close() must wake this wait without taking _request_lock.
            responses = self._responses
            ended = self._exit_event
            if ended.is_set():
                raise McpTransportError(f"MCP server {self.name!r} connection closed")
            self._ids += 1
            req_id = self._ids
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            deferred: list[dict[str, Any]] = []
            try:
                while True:
                    if ended.is_set():
                        raise McpTransportError(f"MCP server {self.name!r} connection closed during {method}")
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        raise McpRequestTimeout(f"MCP server {self.name!r} timed out during {method}")
                    try:
                        msg = responses.get(timeout=remain)
                    except queue.Empty as exc:
                        if ended.is_set():
                            raise McpTransportError(f"MCP server {self.name!r} connection closed during {method}") from exc
                        raise McpRequestTimeout(f"MCP server {self.name!r} timed out during {method}") from exc
                    if ended.is_set() or msg.get("_kiro_byo_transport_closed"):
                        raise McpTransportError(f"MCP server {self.name!r} connection closed during {method}")
                    if msg.get("id") != req_id:
                        if "id" in msg:
                            deferred.append(msg)
                        continue
                    if isinstance(msg.get("error"), dict):
                        err = msg["error"]
                        raise McpJsonRpcError(err.get("code"), str(err.get("message") or err), err.get("data"))
                    result = msg.get("result")
                    return result if isinstance(result, dict) else {}
            finally:
                if not ended.is_set():
                    for msg in deferred:
                        responses.put(msg)

    def _list_tools(self, *, modern: bool, timeout: float = 20.0) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        deadline = time.monotonic() + timeout
        for _page in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpRequestTimeout(f"MCP server {self.name!r} tools/list refresh timed out")
            result = self._request(
                "tools/list",
                self._modern_params(params) if modern else params,
                timeout=remaining,
            )
            if modern:
                result = _validate_cacheable_result(
                    self._logical_modern_result(result), method="tools/list"
                )
            raw_tools = result.get("tools")
            if isinstance(raw_tools, list):
                tools.extend(tool for tool in raw_tools if isinstance(tool, dict))
            cursor = _next_cursor(result, method="tools/list")
            if cursor is None:
                return tools
            if cursor in seen:
                raise McpProtocolError("MCP tools/list repeated nextCursor")
            seen.add(cursor)
        raise McpProtocolError(f"MCP tools/list exceeded {_MAX_LIST_PAGES} pages")

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _launch_process(self) -> None:
        self._responses = queue.Queue()
        self._exit_event = threading.Event()
        self._ids = 0
        child_env = os.environ.copy()
        child_env.update(self.env)
        group_kwargs: dict[str, Any] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt" else {"start_new_session": True}
        )
        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
                **group_kwargs,
            )
        except OSError as exc:
            self.process = None
            raise McpTransportError(f"could not start MCP server {self.name!r}: {exc}") from exc
        proc = self.process
        assert proc is not None
        threading.Thread(
            target=self._reader,
            args=(proc, self._responses, self._exit_event),
            name=f"kiro-byo-mcp-{self.name}", daemon=True,
        ).start()

    def _probe_modern_in_place(self) -> bool:
        """Probe MCP 2026-07-28 on the *current* stdio connection.

        The official MCP v2 auto-negotiation flow is transport-independent:
        send one ``server/discover`` probe, then fall back to the classic
        ``initialize`` handshake on the same stdin/stdout stream when an older
        peer rejects or ignores discovery.  Do not spawn a disposable sibling
        process: doing so starts stateful/expensive MCP servers twice and does
        not match the reference SDKs.
        """
        try:
            result = _validate_discover_result(self._logical_modern_result(
                self._request(
                    "server/discover",
                    self._modern_params({}),
                    timeout=5.0,
                )
            ))
        except McpRequestTimeout:
            # Some pre-2026 stdio servers simply ignore unknown methods. A
            # bounded silent probe is the one ambiguity we retain for backward
            # compatibility on a private child-process transport.
            return False
        except McpProtocolError as exc:
            if _legacy_probe_rejection(exc):
                return False
            raise
        versions = result["supportedVersions"]
        if MCP_MODERN_VERSION not in versions:
            if any(_is_legacy_protocol_version(version) for version in versions):
                return False
            raise McpProtocolError("server/discover advertised no mutually supported MCP version")
        caps = result["capabilities"]
        self.server_capabilities = dict(caps)
        instructions = result.get("instructions")
        self.instructions = str(instructions) if isinstance(instructions, str) else ""
        return True

    def start(self) -> None:
        """Start and discover tools exactly once at a time."""
        with self._start_lock:
            if self.is_running() and self._initialized:
                return
            # A stale/dead process can never be reused across an MCP lifetime.
            if self.process is not None:
                self.close()
            self._launch_process()
            try:
                if self._probe_modern_in_place():
                    self._era = "modern"
                    self._initialized = True
                    # _probe_modern_in_place stores discovery metadata. Respect
                    # capability negotiation: a non-tool server is a valid MCP
                    # server and must not be forced through tools/list.
                    self.tools = self._list_tools(modern=True) if "tools" in self.server_capabilities else []
                else:
                    # Legacy fallback deliberately reuses the same child process.
                    # A discover response that arrived late is harmless: _request
                    # defers unmatched ids while waiting for initialize's id.
                    self._era = "legacy"
                    init = self._request(
                        "initialize",
                        {
                            "protocolVersion": MCP_LATEST_HANDSHAKE_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "kiro-byo", "version": __version__},
                        },
                        timeout=20.0,
                    )
                    caps = init.get("capabilities")
                    self.server_capabilities = dict(caps) if isinstance(caps, dict) else {}
                    instructions = init.get("instructions")
                    self.instructions = str(instructions) if isinstance(instructions, str) else ""
                    self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self._initialized = True
                    self.tools = self._list_tools(modern=False) if "tools" in self.server_capabilities else []
            except Exception:
                self.close()
                raise

    def refresh_tools(self) -> list[dict[str, Any]]:
        """Re-list this authenticated session's tools, never a cached peer schema.

        tools/list shares the stdio request lock with tools/call, so refreshing
        cannot consume another call's response. A failure leaves the caller in
        control of invalidating previously advertised capabilities.
        """
        self.start()
        # Refresh must not indefinitely hold up the user's next prompt.
        fresh = self._list_tools(modern=self._era == "modern", timeout=4.0) if "tools" in self.server_capabilities else []
        self.tools = fresh
        return list(fresh)

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        # A cached catalogue can make a tool visible before this session's broker
        # has finished warming. start() joins an in-flight startup under the lock.
        self.start()
        if self._era == "modern":
            result = self._request(
                "tools/call",
                self._modern_params({"name": tool_name, "arguments": arguments}),
                timeout=120.0,
            )
            return self._logical_modern_result(result)
        return self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=120.0,
        )

    def close(self) -> None:
        # close() must NOT acquire _request_lock or _start_lock: it may run
        # while a tools/call or an initialize exchange is awaiting a reply.
        proc = self.process
        self._exit_event.set()
        self._responses.put({"_kiro_byo_transport_closed": True})
        self.process = None
        self._initialized = False
        self._era = "unknown"
        self.server_capabilities = {}
        self.instructions = ""
        if proc is None:
            return
        # MCP stdio servers are owned by one session. A tool server may spawn
        # children; ending only its parent would leave unauthorized descendants.
        if proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5.0, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            # A non-terminating OS process cannot be reported as stopped.
            _LOG.warning("MCP stdio server %s did not confirm process exit", self.name)
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


@dataclass
class HttpMcpServer:
    """MCP Streamable-HTTP client with 2026-era auto negotiation.

    MCP now has two wire eras.  A modern (2026-07-28) server is stateless and
    is discovered with ``server/discover``; a legacy server still uses the
    initialize/initialized handshake and may issue ``Mcp-Session-Id``.  Follow
    the official SDK's ``auto`` strategy: probe modern first and fall back only
    when the endpoint clearly does not understand that protocol shape.

    Deprecated standalone SSE remains unsupported.  SSE *responses* to a
    Streamable-HTTP POST are still accepted in both eras.
    """

    name: str
    url: str
    headers: dict[str, str]
    tools: list[dict[str, Any]] = field(default_factory=list)
    server_capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    _ids: int = 0
    _session_id: str = ""
    _protocol_version: str = MCP_LATEST_HANDSHAKE_VERSION
    _initialized: bool = False
    _era: str = "unknown"  # unknown | modern | legacy
    _start_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ids_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _closed: threading.Event = field(default_factory=threading.Event, repr=False)
    # A remote HTTP operation may continue after its local caller is cancelled.
    # Cap orphanable requests and fence their results instead of pretending the
    # upstream server has rolled back any side effects.
    _http_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(8), repr=False
    )

    _NAME_BEARING_METHODS = {
        "tools/call": "name",
        "prompts/get": "name",
        "resources/read": "uri",
        "tasks/get": "taskId",
        "tasks/update": "taskId",
        "tasks/cancel": "taskId",
    }
    _HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
    _B64_SENTINEL_RE = re.compile(r"^=\?base64\?[A-Za-z0-9+/]*={0,2}\?=$")

    @classmethod
    def _encode_header_value(cls, value: str) -> str:
        # Mirrors the 2026 SDK header codec: plain printable ASCII survives
        # verbatim unless edge whitespace/sentinel ambiguity would alter it.
        safe_ascii = bool(value) and all(0x20 <= ord(ch) <= 0x7E for ch in value)
        if safe_ascii and value == value.strip() and cls._B64_SENTINEL_RE.fullmatch(value) is None:
            return value
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?base64?{encoded}?="

    @classmethod
    def _reject_forbidden_header_locations(
        cls,
        node: Any,
        *,
        path: tuple[str, ...] = (),
        annotation_allowed: bool = False,
        properties_chain: bool = True,
    ) -> None:
        """Reject ``x-mcp-header`` outside a statically reachable property.

        MCP 2026-07-28 permits header mirroring only for properties reachable
        through consecutive JSON-Schema ``properties`` members.  Annotations
        hidden behind ``items``, composition keywords, ``$ref`` or any other
        dynamic schema branch cannot be mirrored deterministically by an HTTP
        client and make *that tool* invalid for modern Streamable HTTP.
        """
        if isinstance(node, list):
            for index, item in enumerate(node):
                cls._reject_forbidden_header_locations(
                    item,
                    path=(*path, f"[{index}]"),
                    annotation_allowed=False,
                    properties_chain=False,
                )
            return
        if not isinstance(node, dict):
            return

        if "x-mcp-header" in node and not annotation_allowed:
            where = ".".join(path) or "<root>"
            raise McpProtocolError(
                f"MCP tool schema has x-mcp-header outside a pure properties path at {where!r}"
            )

        for key, value in node.items():
            if key == "x-mcp-header":
                continue
            if key == "properties" and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    if not isinstance(prop_name, str):
                        continue
                    cls._reject_forbidden_header_locations(
                        prop_schema,
                        path=(*path, prop_name),
                        annotation_allowed=properties_chain,
                        properties_chain=properties_chain,
                    )
                continue
            cls._reject_forbidden_header_locations(
                value,
                path=(*path, str(key)),
                annotation_allowed=False,
                properties_chain=False,
            )

    @classmethod
    def _header_annotations(
        cls,
        schema: dict[str, Any] | None,
        *,
        path: tuple[str, ...] = (),
        seen: dict[str, str] | None = None,
    ) -> dict[tuple[str, ...], str]:
        """Return valid pure-``properties`` x-mcp-header annotations.

        The modern MCP HTTP binding permits only string/integer/boolean scalar
        properties and requires annotation tokens to be unique
        case-insensitively. Invalid advertised annotations invalidate the
        individual tool; the modern tool catalogue filters them before they can
        be exposed to a model.
        """
        if not isinstance(schema, dict):
            return {}
        if seen is None:
            # Validate placement exactly once at the root. Recursive calls only
            # follow ``properties`` and therefore remain statically reachable.
            cls._reject_forbidden_header_locations(schema)
            seen = {}
        out: dict[tuple[str, ...], str] = {}
        props = schema.get("properties")
        if not isinstance(props, dict):
            return out
        for key, raw_prop in props.items():
            if not isinstance(key, str) or not isinstance(raw_prop, dict):
                continue
            child_path = (*path, key)
            marker = raw_prop.get("x-mcp-header")
            if marker is not None:
                where = ".".join(child_path)
                if not isinstance(marker, str) or not cls._HEADER_TOKEN_RE.fullmatch(marker):
                    raise McpProtocolError(
                        f"MCP tool schema has invalid x-mcp-header on {where!r}"
                    )
                if raw_prop.get("type") not in {"string", "integer", "boolean"}:
                    raise McpProtocolError(
                        f"MCP tool schema x-mcp-header on {where!r} requires string/integer/boolean"
                    )
                lower = marker.lower()
                if lower in seen:
                    raise McpProtocolError(
                        f"MCP tool schema duplicates x-mcp-header {marker!r} on {where!r} and {seen[lower]!r}"
                    )
                seen[lower] = where
                out[child_path] = marker
            out.update(cls._header_annotations(raw_prop, path=child_path, seen=seen))
        return out

    @classmethod
    def _filter_modern_tools(cls, raw_tools: Any, *, server_name: str) -> list[dict[str, Any]]:
        """Return the modern catalogue after per-tool header validation.

        The MCP 2026-07-28 client contract is deliberately per-tool: a broken
        ``x-mcp-header`` annotation must remove that tool from ``tools/list``
        rather than poisoning the whole MCP server catalogue.
        """
        if not isinstance(raw_tools, list):
            return []
        accepted: list[dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            try:
                schema = tool.get("inputSchema")
                cls._header_annotations(schema if isinstance(schema, dict) else None)
            except McpProtocolError as exc:
                _LOG.warning(
                    "MCP server %s advertised invalid modern tool %r; excluding it: %s",
                    server_name,
                    tool.get("name"),
                    exc,
                )
                continue
            accepted.append(tool)
        return accepted

    @staticmethod
    def _value_at_path(arguments: dict[str, Any], path: tuple[str, ...]) -> Any:
        value: Any = arguments
        for component in path:
            if not isinstance(value, dict) or component not in value:
                return None
            value = value[component]
        return value

    def _tool_param_headers(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, str]:
        tool = next(
            (item for item in self.tools if isinstance(item, dict) and item.get("name") == tool_name),
            None,
        )
        if not isinstance(tool, dict):
            return {}
        schema = tool.get("inputSchema")
        annotations = self._header_annotations(schema if isinstance(schema, dict) else None)
        headers: dict[str, str] = {}
        for path, token in annotations.items():
            value = self._value_at_path(arguments, path)
            if value is None:
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int) and not isinstance(value, bool):
                if not -_JS_SAFE_INTEGER_MAX <= value <= _JS_SAFE_INTEGER_MAX:
                    raise McpProtocolError(
                        f"MCP argument {'.'.join(path)!r} is outside the JavaScript safe-integer range"
                    )
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                raise McpProtocolError(
                    f"MCP argument {'.'.join(path)!r} cannot be mirrored as Mcp-Param-{token}"
                )
            headers[f"Mcp-Param-{token}"] = self._encode_header_value(rendered)
        return headers

    @staticmethod
    def _drop_transport_owned_headers(headers: dict[str, str], *, modern: bool) -> None:
        """Remove user headers owned by the MCP transport, case-insensitively.

        urllib/http.client dictionaries are case-preserving, so a custom
        ``mcp-session-id`` could otherwise coexist with our canonical
        ``Mcp-Session-Id``. Modern request-routing and x-mcp-header fields are
        transport facts and must never be overridden by arbitrary App config.
        """
        reserved = {"mcp-protocol-version", "mcp-session-id"}
        if modern:
            reserved.update({"mcp-method", "mcp-name"})
        for key in list(headers):
            lowered = key.lower()
            if lowered in reserved or (modern and lowered.startswith("mcp-param-")):
                headers.pop(key, None)

    def _request_headers(
        self,
        payload: dict[str, Any] | None = None,
        *,
        modern: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"kiro-byo/{__version__}",
        }
        headers.update(self.headers)
        self._drop_transport_owned_headers(headers, modern=modern)
        if modern:
            method = str((payload or {}).get("method") or "")
            headers["MCP-Protocol-Version"] = MCP_MODERN_VERSION
            headers["Mcp-Method"] = method
            params = (payload or {}).get("params")
            name_key = self._NAME_BEARING_METHODS.get(method)
            if name_key and isinstance(params, dict):
                name_value = params.get(name_key)
                if isinstance(name_value, str) and name_value:
                    headers["Mcp-Name"] = self._encode_header_value(name_value)
            if extra_headers:
                headers.update(extra_headers)
            # Modern MCP is stateless. Never leak a legacy session id across
            # the era boundary even if a user-supplied header tried to set it.
            headers.pop("Mcp-Session-Id", None)
            headers.pop("mcp-session-id", None)
        else:
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            if self._initialized:
                headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    @staticmethod
    def _parse_payload(raw: bytes, content_type: str, req_id: int | None) -> dict[str, Any]:
        text = raw.decode("utf-8", errors="replace")
        candidates: list[dict[str, Any]] = []
        if "text/event-stream" in content_type.lower():
            data_lines: list[str] = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            for data in data_lines:
                if not data or data == "[DONE]":
                    continue
                try:
                    value = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
        else:
            try:
                value = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError as exc:
                raise McpProtocolError("HTTP MCP response was not valid JSON/SSE") from exc
            if isinstance(value, dict):
                candidates.append(value)

        if req_id is None:
            return {}
        for value in candidates:
            if value.get("id") == req_id:
                if isinstance(value.get("error"), dict):
                    err = value["error"]
                    raise McpJsonRpcError(
                        err.get("code"),
                        str(err.get("message") or err),
                        err.get("data"),
                    )
                result = value.get("result")
                return result if isinstance(result, dict) else {}
        raise McpNoResponse(f"HTTP MCP server returned no response for request id {req_id}")

    def _post(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        expect_response: bool,
        modern: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Wait interruptibly for HTTP, without asserting remote cancellation.

        urllib does not provide portable cancellation of an in-progress
        urlopen/read. The network worker stays daemonized and bounded; closing
        this per-session transport fences the eventual response and wakes the
        ACP turn promptly. Remote tool side effects remain *unknown*.
        """
        if self._closed.is_set():
            raise McpTransportError(f"HTTP MCP server {self.name!r} session is closed")
        if not self._http_slots.acquire(blocking=False):
            raise McpTransportError(f"HTTP MCP server {self.name!r} has too many pending requests")
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def request_worker() -> None:
            try:
                if not self._closed.is_set():
                    outcome["result"] = self._post_blocking(
                        payload, timeout=timeout, expect_response=expect_response,
                        modern=modern, extra_headers=extra_headers,
                    )
            except Exception as exc:
                outcome["error"] = exc
            finally:
                self._http_slots.release()
                done.set()

        try:
            thread = threading.Thread(
                target=request_worker, name=f"kiro-byo-mcp-http-{self.name}", daemon=True
            )
            thread.start()
        except BaseException:
            self._http_slots.release()
            raise
        while not done.wait(0.05):
            if self._closed.is_set():
                raise McpTransportError(f"HTTP MCP server {self.name!r} session closed during request")
        if self._closed.is_set():
            raise McpTransportError(f"HTTP MCP server {self.name!r} session closed during request")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result", {})

    def _post_blocking(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        expect_response: bool,
        modern: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        req_id = payload.get("id") if isinstance(payload.get("id"), int) else None
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=self._request_headers(payload, modern=modern, extra_headers=extra_headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                # Only the handshake era owns transport session identity.
                if not modern:
                    session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                    if session_id and not self._closed.is_set():
                        self._session_id = str(session_id)
                raw = response.read()
                if not expect_response:
                    return {}
                return self._parse_payload(raw, str(response.headers.get("Content-Type") or ""), req_id)
        except urllib.error.HTTPError as exc:
            try:
                detail_raw = exc.read()
                detail = detail_raw.decode("utf-8", errors="replace")[:800]
            except Exception:
                detail_raw = b""
                detail = ""
            # 2026 servers map protocol-shape errors to 4xx. Those are valid
            # evidence that the endpoint exists but does not speak the probe;
            # authentication and server failures are transport failures and must
            # not be hidden by silently trying another era.
            if modern and exc.code in {400, 404, 405, 406, 415, 422, 501}:
                if expect_response and detail_raw:
                    try:
                        return self._parse_payload(
                            detail_raw,
                            str(exc.headers.get("Content-Type") or "") if exc.headers else "application/json",
                            req_id,
                        )
                    except McpJsonRpcError:
                        raise
                    except McpProtocolError:
                        pass
                raise McpProbeUnsupported(
                    f"HTTP MCP server {self.name!r} does not accept the modern probe (HTTP {exc.code})"
                ) from exc
            suffix = f": {detail}" if detail else ""
            raise McpTransportError(
                f"HTTP MCP server {self.name!r} returned HTTP {exc.code}{suffix}"
            ) from exc
        except urllib.error.URLError as exc:
            raise McpTransportError(f"HTTP MCP server {self.name!r} connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise McpTransportError(f"HTTP MCP server {self.name!r} timed out") from exc

    def _request(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        with self._ids_lock:
            self._ids += 1
            request_id = self._ids
        return self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout=timeout,
            expect_response=True,
            modern=False,
        )

    @staticmethod
    def _modern_params(params: dict[str, Any]) -> dict[str, Any]:
        modern = dict(params)
        existing_meta = modern.get("_meta")
        meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
        # Reserved modern envelope keys are transport-owned. User/tool payloads
        # cannot override the negotiated protocol identity.
        meta.update({
            "io.modelcontextprotocol/protocolVersion": MCP_MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "kiro-byo", "version": __version__},
            "io.modelcontextprotocol/clientCapabilities": {},
        })
        modern["_meta"] = meta
        return modern

    def _modern_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 20.0,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._ids_lock:
            self._ids += 1
            request_id = self._ids
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": self._modern_params(params),
        }
        result = self._post(
            payload,
            timeout=timeout,
            expect_response=True,
            modern=True,
            extra_headers=extra_headers,
        )
        result_type = result.get("resultType")
        if result_type == "input_required":
            # 0.9.10 does not yet project MCP elicitation/sampling/roots requests
            # through Crew. Refuse explicitly rather than pretending the tool
            # completed or dropping the server's request for user/client input.
            raise McpInputRequired(
                "MCP 2026-07-28 tool requested multi-round-trip client input; "
                "Kiro BYO does not yet expose MRTR elicitation/sampling/roots to Crew"
            )
        if result_type is None:
            raise McpProtocolError("MCP 2026-07-28 response is missing required resultType")
        if result_type != "complete":
            raise McpProtocolError(f"unsupported MCP 2026 resultType: {result_type}")
        # resultType is wire bookkeeping in the 2026 SDKs; keep application code
        # era-neutral by removing it before returning the logical result.
        if "resultType" in result:
            result = dict(result)
            result.pop("resultType", None)
        return result

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, timeout=20.0, expect_response=False, modern=False)

    def is_running(self) -> bool:
        return self._initialized

    def _list_tools(self, *, modern: bool) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = (
                self._modern_request("tools/list", params, timeout=20.0)
                if modern
                else self._request("tools/list", params, timeout=20.0)
            )
            if modern:
                result = _validate_cacheable_result(result, method="tools/list")
            raw_tools = result.get("tools")
            if isinstance(raw_tools, list):
                tools.extend(tool for tool in raw_tools if isinstance(tool, dict))
            cursor = _next_cursor(result, method="tools/list")
            if cursor is None:
                return self._filter_modern_tools(tools, server_name=self.name) if modern else tools
            if cursor in seen:
                raise McpProtocolError("MCP tools/list repeated nextCursor")
            seen.add(cursor)
        raise McpProtocolError(f"MCP tools/list exceeded {_MAX_LIST_PAGES} pages")

    def _start_legacy(self) -> None:
        self._ids = 0
        self._session_id = ""
        self._protocol_version = MCP_LATEST_HANDSHAKE_VERSION
        self._era = "legacy"
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_LATEST_HANDSHAKE_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "kiro-byo", "version": __version__},
            },
            timeout=20.0,
        )
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated.strip():
            self._protocol_version = negotiated.strip()
        caps = result.get("capabilities")
        self.server_capabilities = dict(caps) if isinstance(caps, dict) else {}
        instructions = result.get("instructions")
        self.instructions = str(instructions) if isinstance(instructions, str) else ""
        self._initialized = True
        self._notify("notifications/initialized")
        self.tools = self._list_tools(modern=False) if "tools" in self.server_capabilities else []

    def _start_modern_or_fallback(self) -> bool:
        self._ids = 0
        self._session_id = ""
        self._protocol_version = MCP_MODERN_VERSION
        self._era = "unknown"
        try:
            discover = _validate_discover_result(
                self._modern_request("server/discover", {}, timeout=20.0)
            )
        except McpProtocolError as exc:
            if not _legacy_probe_rejection(exc):
                raise
            # Fall back only on affirmative legacy evidence. A malformed modern
            # DiscoverResult is a server/proxy bug and must fail closed.
            self._era = "unknown"
            self._protocol_version = MCP_LATEST_HANDSHAKE_VERSION
            self._session_id = ""
            self._ids = 0
            return False

        versions = discover["supportedVersions"]
        if MCP_MODERN_VERSION not in versions:
            if any(_is_legacy_protocol_version(version) for version in versions):
                self._era = "unknown"
                self._protocol_version = MCP_LATEST_HANDSHAKE_VERSION
                self._session_id = ""
                self._ids = 0
                return False
            raise McpProtocolError("server/discover advertised no mutually supported MCP version")
        caps = discover["capabilities"]
        self.server_capabilities = dict(caps)
        instructions = discover.get("instructions")
        self.instructions = str(instructions) if isinstance(instructions, str) else ""
        self._era = "modern"
        self._initialized = True
        self.tools = self._list_tools(modern=True) if "tools" in self.server_capabilities else []
        return True

    def start(self) -> None:
        if self._closed.is_set():
            raise McpTransportError(f"HTTP MCP server {self.name!r} session is closed")
        with self._start_lock:
            if self._closed.is_set():
                raise McpTransportError(f"HTTP MCP server {self.name!r} session is closed")
            if self._initialized:
                return
            try:
                if not self._start_modern_or_fallback():
                    self._start_legacy()
                if self._closed.is_set():
                    raise McpTransportError(f"HTTP MCP server {self.name!r} session is closed")
            except Exception:
                self.close()
                raise

    def _refresh_modern_tools(self) -> None:
        self.tools = self._list_tools(modern=True) if "tools" in self.server_capabilities else []

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self.start()
        if self._era == "modern":
            def invoke() -> Any:
                return self._modern_request(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                    timeout=120.0,
                    extra_headers=self._tool_param_headers(tool_name, arguments),
                )

            try:
                return invoke()
            except McpJsonRpcError as exc:
                if exc.code != -32020:
                    raise
                # SEP-2243 recovery: a stale/missing mirrored-header schema gets
                # one catalogue refresh and one fresh request id. Never loop.
                self._refresh_modern_tools()
                if not any(
                    isinstance(tool, dict) and tool.get("name") == tool_name
                    for tool in self.tools
                ):
                    raise McpProtocolError(
                        f"MCP tool {tool_name!r} disappeared after HeaderMismatch catalogue refresh"
                    ) from exc
                return invoke()
        return self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=120.0,
        )

    def close(self) -> None:
        # Fence first so waiters stop promptly and late replies cannot be used.
        # urllib network workers may still be in-flight; DELETE is best-effort.
        if self._closed.is_set():
            return
        self._closed.set()
        # Only legacy Streamable HTTP owns a server-side session to delete.
        if self._era == "legacy" and self._session_id:
            try:
                request = urllib.request.Request(
                    self.url,
                    headers=self._request_headers(),
                    method="DELETE",
                )
                urllib.request.urlopen(request, timeout=3.0).close()
            except Exception:
                pass
        self._initialized = False
        self._session_id = ""
        self._protocol_version = MCP_LATEST_HANDSHAKE_VERSION
        self._era = "unknown"
        self.server_capabilities = {}
        self.instructions = ""
