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
            raise McpProtocolError(
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

    def _reader(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
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
            # Kiro BYO does not currently expose legacy MCP sampling,
            # elicitation or roots callbacks. Never leave a server-initiated
            # JSON-RPC request hanging: reject it explicitly so the server can
            # fail/choose a fallback instead of deadlocking this tool call.
            if "id" in msg and isinstance(msg.get("method"), str) and "result" not in msg and "error" not in msg:
                try:
                    self._send({
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32601, "message": f"unsupported MCP client method: {msg.get('method')}"},
                    })
                except McpError:
                    pass
                continue
            self._responses.put(msg)

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self.process
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError(f"MCP server {self.name!r} is not running")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            proc.stdin.write(encoded)
            proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        # Serialize the whole exchange. _ids is a plain int and _responses is a
        # single shared queue, so two concurrent calls on the same stdio server
        # could allocate colliding ids and drain/re-queue each other's replies,
        # delivering a response to the wrong caller or timing out spuriously.
        with self._request_lock:
            self._ids += 1
            req_id = self._ids
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            deferred: list[dict[str, Any]] = []
            try:
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        raise McpRequestTimeout(f"MCP server {self.name!r} timed out during {method}")
                    try:
                        msg = self._responses.get(timeout=remain)
                    except queue.Empty as exc:
                        raise McpRequestTimeout(f"MCP server {self.name!r} timed out during {method}") from exc
                    if msg.get("id") != req_id:
                        if "id" in msg:
                            deferred.append(msg)
                        continue
                    if isinstance(msg.get("error"), dict):
                        err = msg["error"]
                        raise McpJsonRpcError(
                            err.get("code"),
                            str(err.get("message") or err),
                            err.get("data"),
                        )
                    result = msg.get("result")
                    return result if isinstance(result, dict) else {}
            finally:
                for msg in deferred:
                    self._responses.put(msg)

    def _list_tools(self, *, modern: bool) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self._request(
                "tools/list",
                self._modern_params(params) if modern else params,
                timeout=20.0,
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
        self._ids = 0
        child_env = os.environ.copy()
        child_env.update(self.env)
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
            )
        except OSError as exc:
            self.process = None
            raise McpTransportError(f"could not start MCP server {self.name!r}: {exc}") from exc
        threading.Thread(target=self._reader, name=f"kiro-byo-mcp-{self.name}", daemon=True).start()

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
        # Do not take _start_lock here: start() invokes close() on failure while
        # already holding it.
        proc = self.process
        self.process = None
        self._initialized = False
        self._era = "unknown"
        self.server_capabilities = {}
        self.instructions = ""
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
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
                    if session_id:
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
        self._ids += 1
        return self._post(
            {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params},
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
        self._ids += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._ids,
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
            raise McpProtocolError(
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
        with self._start_lock:
            if self._initialized:
                return
            try:
                if not self._start_modern_or_fallback():
                    self._start_legacy()
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
