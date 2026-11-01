from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import textwrap
import time
import unittest
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path

from kiro_byo.mcp_client import HttpMcpServer, McpError, McpJsonRpcError, McpManager, McpProtocolError, StdioMcpServer, ToolBinding


ENTRY = {
    "type": "stdio",
    "name": "kirocrew-core",
    "command": "python",
    "args": ["-m", "fake_core"],
    "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "session-a"}],
}


class McpWarmupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("KIRO_BYO_HOME")
        os.environ["KIRO_BYO_HOME"] = self.tmp.name
        McpManager.clear_process_catalog_cache()

    def tearDown(self) -> None:
        McpManager.clear_process_catalog_cache()
        if self.old_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = self.old_home
        self.tmp.cleanup()


    def test_deprecated_sse_transport_fails_closed_instead_of_dropping_tools(self) -> None:
        with self.assertRaisesRegex(McpError, "unsupported MCP transport"):
            McpManager([{
                "type": "sse",
                "name": "remote",
                "url": "https://example.invalid/mcp",
                "headers": [],
            }])

    def test_streamable_http_discovers_and_calls_tool_with_negotiated_header(self) -> None:
        seen: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                return
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                seen.append({
                    "method": body.get("method"),
                    "protocol": self.headers.get("MCP-Protocol-Version"),
                    "session": self.headers.get("Mcp-Session-Id"),
                })
                method = body.get("method")
                req_id = body.get("id")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    }
                    session = "http-session-1"
                elif method == "tools/list":
                    result = {
                        "tools": [{
                            "name": "ping",
                            "description": "Ping",
                            "inputSchema": {"type": "object", "properties": {}},
                        }]
                    }
                    session = None
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "pong"}]}
                    session = None
                else:
                    # initialized notification: 202 with no response body.
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_DELETE(self):
                self.send_response(204)
                self.end_headers()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            manager = McpManager([{
                "type": "http",
                "name": "remote",
                "url": f"http://127.0.0.1:{httpd.server_address[1]}/mcp",
                "headers": [],
            }])
            manager.start(timeout=2.0)
            self.assertIn("mcp__remote__ping", manager.bindings)
            self.assertEqual(manager.call("mcp__remote__ping", {}), "pong")
            # initialize has no negotiated-version header; all subsequent
            # handshake-era requests do. Session identity is replayed too.
            by_method = {str(item["method"]): item for item in seen if item["method"]}
            self.assertEqual(seen[0]["method"], "server/discover")
            self.assertIsNone(by_method["initialize"]["protocol"])
            self.assertEqual(by_method["tools/list"]["protocol"], "2025-11-25")
            self.assertEqual(by_method["tools/list"]["session"], "http-session-1")
            self.assertEqual(by_method["tools/call"]["protocol"], "2025-11-25")
            manager.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)


    def test_modern_http_uses_discover_per_request_envelope_and_routing_headers(self) -> None:
        seen: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                return
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                seen.append({
                    "method": body.get("method"),
                    "params": body.get("params"),
                    "protocol": self.headers.get("MCP-Protocol-Version"),
                    "mcp_method": self.headers.get("Mcp-Method"),
                    "name": self.headers.get("Mcp-Name"),
                    "region": self.headers.get("Mcp-Param-Region"),
                    "session": self.headers.get("Mcp-Session-Id"),
                })
                method = body.get("method")
                req_id = body.get("id")
                if method == "server/discover":
                    result = {
                        "resultType": "complete",
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {"tools": {}},
                        "ttlMs": 0,
                        "cacheScope": "public",
                    }
                elif method == "tools/list":
                    result = {
                        "resultType": "complete",
                        "tools": [{
                            "name": "check_stock",
                            "description": "Check stock",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "region": {"type": "string", "x-mcp-header": "Region"},
                                },
                                "required": ["title", "region"],
                            },
                        }],
                        "ttlMs": 0,
                        "cacheScope": "public",
                    }
                elif method == "tools/call":
                    result = {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": "3"}],
                    }
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # A modern server must not establish transport session identity.
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_DELETE(self):
                raise AssertionError("modern MCP must not send DELETE session close")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            manager = McpManager([{
                "type": "http",
                "name": "modern",
                "url": f"http://127.0.0.1:{httpd.server_address[1]}/mcp",
                "headers": [],
            }])
            manager.start(timeout=2.0)
            self.assertIn("mcp__modern__check_stock", manager.bindings)
            self.assertEqual(
                manager.call("mcp__modern__check_stock", {"title": "Book", "region": "東京"}),
                "3",
            )
            self.assertEqual([x["method"] for x in seen], ["server/discover", "tools/list", "tools/call"])
            for item in seen:
                self.assertEqual(item["protocol"], "2026-07-28")
                self.assertEqual(item["mcp_method"], item["method"])
                self.assertIsNone(item["session"])
                meta = item["params"]["_meta"]
                self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28")
                self.assertEqual(meta["io.modelcontextprotocol/clientInfo"]["name"], "kiro-byo")
                self.assertEqual(meta["io.modelcontextprotocol/clientCapabilities"], {})
            self.assertEqual(seen[-1]["name"], "check_stock")
            # Non-ASCII values use the official =?base64?...?= sentinel.
            self.assertTrue(str(seen[-1]["region"]).startswith("=?base64?"))
            manager.close()
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(2)

    def test_modern_http_filters_only_tools_with_invalid_header_annotations(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server._modern_request = lambda method, params, **_kwargs: (
            {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 0, "cacheScope": "public"}
            if method == "server/discover"
            else {
                "tools": [
                    {
                        "name": "valid",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "region": {"type": "string", "x-mcp-header": "Region"}
                            },
                        },
                    },
                    {
                        "name": "array-hidden",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "region": {
                                                "type": "string",
                                                "x-mcp-header": "Region",
                                            }
                                        },
                                    },
                                }
                            },
                        },
                    },
                    {
                        "name": "number",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "ratio": {"type": "number", "x-mcp-header": "Ratio"}
                            },
                        },
                    },
                ],
                "ttlMs": 0,
                "cacheScope": "public",
            }
        )
        self.assertTrue(server._start_modern_or_fallback())
        self.assertEqual([tool["name"] for tool in server.tools], ["valid"])

    def test_modern_http_filters_duplicate_header_names_case_insensitively(self) -> None:
        tools = [{
            "name": "duplicate",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-mcp-header": "Region"},
                    "b": {"type": "string", "x-mcp-header": "region"},
                },
            },
        }]
        self.assertEqual(HttpMcpServer._filter_modern_tools(tools, server_name="x"), [])

    def test_modern_http_rejects_unsafe_integer_header_value(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server.tools = [{
            "name": "lookup",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "x-mcp-header": "Id"},
                },
            },
        }]
        with self.assertRaisesRegex(McpProtocolError, "safe-integer"):
            server._tool_param_headers("lookup", {"id": 2**53})
        self.assertEqual(
            server._tool_param_headers("lookup", {"id": 2**53 - 1})["Mcp-Param-Id"],
            str(2**53 - 1),
        )

    def test_modern_http_strips_user_transport_headers_case_insensitively(self) -> None:
        server = HttpMcpServer(
            "modern",
            "http://127.0.0.1:1/mcp",
            {
                "mcp-session-id": "stale",
                "MCP-METHOD": "spoofed",
                "mcp-name": "spoofed",
                "mcp-protocol-version": "1900-01-01",
                "MCP-PARAM-REGION": "spoofed",
                "Authorization": "Bearer ok",
            },
        )
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "check_stock", "arguments": {}},
        }
        headers = server._request_headers(
            payload,
            modern=True,
            extra_headers={"Mcp-Param-Region": "generated"},
        )
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertNotIn("mcp-session-id", lowered)
        self.assertEqual(lowered["mcp-protocol-version"], "2026-07-28")
        self.assertEqual(lowered["mcp-method"], "tools/call")
        self.assertEqual(lowered["mcp-name"], "check_stock")
        self.assertEqual(lowered["mcp-param-region"], "generated")
        self.assertEqual(lowered["authorization"], "Bearer ok")

    def test_modern_http_requires_result_type(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        with patch.object(server, "_post", return_value={"content": []}):
            with self.assertRaisesRegex(McpProtocolError, "missing required resultType"):
                server._modern_request("tools/call", {"name": "x", "arguments": {}})

    def test_modern_http_header_mismatch_refreshes_catalogue_and_retries_once(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server._initialized = True
        server._era = "modern"
        server.server_capabilities = {"tools": {}}
        server.tools = [{
            "name": "lookup",
            "inputSchema": {"type": "object", "properties": {}},
        }]
        seen: list[tuple[str, dict[str, str] | None]] = []

        def fake_modern(method, params, **kwargs):
            seen.append((method, kwargs.get("extra_headers")))
            if method == "tools/list":
                return {
                    "tools": [{
                        "name": "lookup",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "region": {"type": "string", "x-mcp-header": "Region"}
                            },
                        },
                    }],
                    "ttlMs": 0,
                    "cacheScope": "private",
                }
            calls = sum(1 for name, _headers in seen if name == "tools/call")
            if calls == 1:
                raise McpJsonRpcError(-32020, "HeaderMismatch")
            return {"content": [{"type": "text", "text": "ok"}]}

        server._modern_request = fake_modern
        result = server.call("lookup", {"region": "us-east-1"})
        self.assertEqual(result["content"][0]["text"], "ok")
        self.assertEqual([name for name, _headers in seen], ["tools/call", "tools/list", "tools/call"])
        self.assertEqual(seen[0][1], {})
        self.assertEqual(seen[-1][1], {"Mcp-Param-Region": "us-east-1"})

    def test_modern_http_header_mismatch_retries_only_once(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server._initialized = True
        server._era = "modern"
        server.server_capabilities = {"tools": {}}
        server.tools = [{"name": "lookup", "inputSchema": {"type": "object", "properties": {}}}]
        call_count = 0

        def fake_modern(method, params, **kwargs):
            nonlocal call_count
            if method == "tools/list":
                return {"tools": list(server.tools), "ttlMs": 0, "cacheScope": "private"}
            call_count += 1
            raise McpJsonRpcError(-32020, "HeaderMismatch")

        server._modern_request = fake_modern
        with self.assertRaisesRegex(McpJsonRpcError, "HeaderMismatch"):
            server.call("lookup", {})
        self.assertEqual(call_count, 2)

    def test_modern_http_mrtr_input_required_fails_explicitly(self) -> None:
        server = HttpMcpServer("remote", "https://example.invalid/mcp", {})
        server._initialized = True
        server._era = "modern"
        with patch.object(server, "_post", return_value={
            "resultType": "input_required",
            "inputRequests": {"confirm": {"method": "elicitation/create"}},
            "requestState": "opaque",
        }):
            with self.assertRaisesRegex(McpError, "multi-round-trip client input"):
                server._modern_request("tools/call", {"name": "x", "arguments": {}})

    def test_stdio_auto_negotiates_modern_on_single_process(self) -> None:
        root = Path(self.tmp.name)
        script = root / "modern_mcp.py"
        log = root / "starts.log"
        script.write_text(textwrap.dedent(r"""
            import json, sys
            import os
            log = sys.argv[1]
            with open(log, "a", encoding="utf-8") as f:
                f.write("START " + str(os.getpid()) + "\n")
            for raw in sys.stdin:
                msg = json.loads(raw)
                method = msg.get("method")
                with open(log, "a", encoding="utf-8") as f:
                    f.write(str(method) + "\n")
                rid = msg.get("id")
                if rid is None:
                    continue
                if method == "server/discover":
                    result = {"resultType":"complete", "supportedVersions":["2026-07-28"], "capabilities":{"tools":{}}, "ttlMs":0, "cacheScope":"public"}
                elif method == "tools/list":
                    result = {"resultType":"complete", "tools":[{"name":"ping", "description":"Ping", "inputSchema":{"type":"object", "properties":{}}}], "ttlMs":0, "cacheScope":"public"}
                elif method == "tools/call":
                    result = {"resultType":"complete", "content":[{"type":"text", "text":"pong"}]}
                else:
                    print(json.dumps({"jsonrpc":"2.0", "id":rid, "error":{"code":-32601, "message":"no"}}), flush=True)
                    continue
                print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":result}), flush=True)
        """), encoding="utf-8")
        server = StdioMcpServer("modern", sys.executable, [str(script), str(log)], {})
        server.start()
        self.assertEqual(server._era, "modern")
        self.assertEqual([tool["name"] for tool in server.tools], ["ping"])
        result = server.call("ping", {})
        self.assertEqual(result["content"][0]["text"], "pong")
        server.close()
        self.assertEqual(log.read_text(encoding="utf-8").splitlines()[1:], ["server/discover", "tools/list", "tools/call"])
        self.assertEqual(sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("START ")), 1)

    def test_stdio_auto_falls_back_to_legacy_after_discover_rejection(self) -> None:
        root = Path(self.tmp.name)
        script = root / "legacy_mcp.py"
        log = root / "legacy_starts.log"
        script.write_text(textwrap.dedent(r"""
            import json, sys
            import os
            log = sys.argv[1]
            with open(log, "a", encoding="utf-8") as f:
                f.write("START " + str(os.getpid()) + "\n")
            for raw in sys.stdin:
                msg = json.loads(raw)
                method = msg.get("method")
                rid = msg.get("id")
                with open(log, "a", encoding="utf-8") as f:
                    f.write(str(method) + "\n")
                if rid is None:
                    continue
                if method == "server/discover":
                    print(json.dumps({"jsonrpc":"2.0", "id":rid, "error":{"code":-32601, "message":"method not found"}}), flush=True)
                    continue
                if method == "initialize":
                    result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{}}, "serverInfo":{"name":"old", "version":"1"}}
                elif method == "tools/list":
                    result = {"tools":[{"name":"ping", "description":"Ping", "inputSchema":{"type":"object", "properties":{}}}]}
                elif method == "tools/call":
                    result = {"content":[{"type":"text", "text":"pong"}]}
                else:
                    print(json.dumps({"jsonrpc":"2.0", "id":rid, "error":{"code":-32601, "message":"no"}}), flush=True)
                    continue
                print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":result}), flush=True)
        """), encoding="utf-8")
        server = StdioMcpServer("legacy", sys.executable, [str(script), str(log)], {})
        server.start()
        self.assertEqual(server._era, "legacy")
        self.assertEqual(server.call("ping", {})["content"][0]["text"], "pong")
        server.close()
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(1 for line in lines if line.startswith("START ")), 1)
        self.assertEqual(lines[1:], ["server/discover", "initialize", "notifications/initialized", "tools/list", "tools/call"])

    def test_modern_http_encodes_non_ascii_mcp_name(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "東京検索", "arguments": {}},
        }
        headers = server._request_headers(payload, modern=True)
        self.assertTrue(headers["Mcp-Name"].startswith("=?base64?"))
        self.assertTrue(headers["Mcp-Name"].endswith("?="))

    def test_http_tools_list_paginates_all_pages(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server.server_capabilities = {"tools": {}}
        pages = []
        def fake_modern(method, params, **_kwargs):
            self.assertEqual(method, "tools/list")
            pages.append(dict(params))
            if "cursor" not in params:
                return {"tools": [{"name": "a", "inputSchema": {"type": "object"}}], "nextCursor": "page-2", "ttlMs": 0, "cacheScope": "public"}
            return {"tools": [{"name": "b", "inputSchema": {"type": "object"}}], "ttlMs": 0, "cacheScope": "public"}
        server._modern_request = fake_modern
        tools = server._list_tools(modern=True)
        self.assertEqual([tool["name"] for tool in tools], ["a", "b"])
        self.assertEqual(pages, [{}, {"cursor": "page-2"}])

    def test_stdio_tools_list_paginates_all_pages_on_same_process(self) -> None:
        root = Path(self.tmp.name)
        script = root / "paged_mcp.py"
        script.write_text(textwrap.dedent(r"""
            import json, sys
            for raw in sys.stdin:
                msg = json.loads(raw); rid = msg.get("id"); method = msg.get("method"); params = msg.get("params") or {}
                if rid is None: continue
                if method == "server/discover":
                    result = {"resultType":"complete", "supportedVersions":["2026-07-28"], "capabilities":{"tools":{}}, "ttlMs":0, "cacheScope":"public"}
                elif method == "tools/list":
                    if params.get("cursor") == "next":
                        result = {"resultType":"complete", "tools":[{"name":"b", "inputSchema":{"type":"object"}}], "ttlMs":0, "cacheScope":"public"}
                    else:
                        result = {"resultType":"complete", "tools":[{"name":"a", "inputSchema":{"type":"object"}}], "nextCursor":"next", "ttlMs":0, "cacheScope":"public"}
                else:
                    result = {"resultType":"complete", "content":[]}
                print(json.dumps({"jsonrpc":"2.0", "id":rid, "result":result}), flush=True)
        """), encoding="utf-8")
        server = StdioMcpServer("paged", sys.executable, [str(script)], {})
        server.start()
        self.assertEqual([tool["name"] for tool in server.tools], ["a", "b"])
        server.close()

    def test_modern_discover_missing_required_cache_fields_fails_closed(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        calls = []
        def fake_modern(method, params, **_kwargs):
            calls.append(method)
            return {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                # Deliberately missing ttlMs/cacheScope: invalid modern result.
            }
        server._modern_request = fake_modern
        with self.assertRaisesRegex(McpProtocolError, "ttlMs"):
            server._start_modern_or_fallback()
        self.assertEqual(calls, ["server/discover"])

    def test_modern_discover_missing_client_capability_does_not_fallback(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        calls = []
        def fake_modern(method, params, **_kwargs):
            calls.append(method)
            raise McpJsonRpcError(-32021, "MissingRequiredClientCapability")
        server._modern_request = fake_modern
        with self.assertRaisesRegex(McpJsonRpcError, "MissingRequiredClientCapability"):
            server._start_modern_or_fallback()
        self.assertEqual(calls, ["server/discover"])

    def test_modern_discover_method_not_found_is_legacy_evidence(self) -> None:
        server = HttpMcpServer("legacy", "https://example.invalid/mcp", {})
        def fake_modern(method, params, **_kwargs):
            raise McpJsonRpcError(-32601, "Method not found")
        server._modern_request = fake_modern
        self.assertFalse(server._start_modern_or_fallback())

    def test_modern_discover_unsupported_version_only_falls_back_for_legacy_supported(self) -> None:
        legacy = HttpMcpServer("legacy", "https://example.invalid/mcp", {})
        legacy._modern_request = lambda method, params, **_kwargs: (_ for _ in ()).throw(
            McpJsonRpcError(
                -32022,
                "Unsupported protocol version",
                {"supported": ["2025-11-25"], "requested": "2026-07-28"},
            )
        )
        self.assertFalse(legacy._start_modern_or_fallback())

        future = HttpMcpServer("future", "https://example.invalid/mcp", {})
        future._modern_request = lambda method, params, **_kwargs: (_ for _ in ()).throw(
            McpJsonRpcError(
                -32022,
                "Unsupported protocol version",
                {"supported": ["2027-01-01"], "requested": "2026-07-28"},
            )
        )
        with self.assertRaisesRegex(McpJsonRpcError, "Unsupported protocol version"):
            future._start_modern_or_fallback()

    def test_modern_discover_result_can_select_legacy_but_not_unknown_future(self) -> None:
        legacy = HttpMcpServer("legacy", "https://example.invalid/mcp", {})
        legacy._modern_request = lambda method, params, **_kwargs: {
            "supportedVersions": ["2025-11-25"],
            "capabilities": {},
            "ttlMs": 0,
            "cacheScope": "private",
        }
        self.assertFalse(legacy._start_modern_or_fallback())

        future = HttpMcpServer("future", "https://example.invalid/mcp", {})
        future._modern_request = lambda method, params, **_kwargs: {
            "supportedVersions": ["2027-01-01"],
            "capabilities": {},
            "ttlMs": 0,
            "cacheScope": "private",
        }
        with self.assertRaisesRegex(McpProtocolError, "no mutually supported"):
            future._start_modern_or_fallback()

    def test_modern_tools_list_requires_cache_hints(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        server.server_capabilities = {"tools": {}}
        server._modern_request = lambda method, params, **_kwargs: {"tools": []}
        with self.assertRaisesRegex(McpProtocolError, "ttlMs"):
            server._list_tools(modern=True)

    def test_mcp_server_without_tools_capability_does_not_call_tools_list(self) -> None:
        server = HttpMcpServer("modern", "https://example.invalid/mcp", {})
        calls = []
        def fake_modern(method, params, **_kwargs):
            calls.append(method)
            if method == "server/discover":
                return {"supportedVersions": ["2026-07-28"], "capabilities": {"resources": {}}, "ttlMs": 0, "cacheScope": "public"}
            raise AssertionError("tools/list must not be called without capabilities.tools")
        server._modern_request = fake_modern
        self.assertTrue(server._start_modern_or_fallback())
        self.assertEqual(server.tools, [])
        self.assertEqual(calls, ["server/discover"])

    def test_background_warmup_does_not_block_session_path(self) -> None:
        def fake_start(server: StdioMcpServer) -> None:
            time.sleep(0.20)
            server.tools = [
                {
                    "name": "ping",
                    "description": "Ping the Crew core broker",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        manager = McpManager([ENTRY])
        with patch.object(StdioMcpServer, "start", fake_start):
            t0 = time.perf_counter()
            manager.start_background()
            scheduled_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(scheduled_ms, 80.0)
            self.assertEqual(manager.tool_specs(), [])
            self.assertTrue(manager.wait_for_catalog(timeout=2.0))
            self.assertEqual(len(manager.tool_specs()), 1)
        manager.close()

    def test_crew_catalogue_cache_ignores_per_session_token_but_not_process_identity(self) -> None:
        def fake_start(server: StdioMcpServer) -> None:
            server.tools = [
                {
                    "name": "ping",
                    "description": "Ping",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        first = McpManager([ENTRY])
        with patch.object(StdioMcpServer, "start", fake_start):
            first.start(timeout=2.0)
        self.assertEqual(len(first.tool_specs()), 1)

        second_entry = dict(ENTRY)
        second_entry["env"] = [
            {"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "session-b"}
        ]
        second = McpManager([second_entry])
        # The second session has a different broker identity, but the immutable
        # Crew-owned schema is already available before its process warms.
        self.assertIn("kirocrew-core", second.catalog_cache_hits)
        self.assertEqual(len(second.tool_specs()), 1)
        self.assertTrue(second.catalog_ready())

        first.close()
        second.close()

    def test_persisted_crew_catalogue_contains_no_session_token_or_command(self) -> None:
        def fake_start(server: StdioMcpServer) -> None:
            server.tools = [{
                "name": "ping",
                "description": "Ping",
                "inputSchema": {"type": "object", "properties": {}},
            }]

        manager = McpManager([ENTRY])
        with patch.object(StdioMcpServer, "start", fake_start):
            manager.start(timeout=2.0)
        cache = Path(self.tmp.name) / "data" / "mcp_catalog_cache_v1.json"
        text = cache.read_text(encoding="utf-8")
        self.assertNotIn("session-a", text)
        self.assertNotIn("fake_core", text)
        self.assertNotIn("KIROCREW_STUB_SESSION_TOKEN", text)
        self.assertIn("ping", text)
        manager.close()

    def test_deferred_search_ranks_tool_name_description_and_arguments(self) -> None:
        manager = McpManager([])
        manager.bindings = {
            "mcp__kirocrew-core__search_chat_history": ToolBinding(
                "mcp__kirocrew-core__search_chat_history",
                "kirocrew-core",
                "search_chat_history",
                "Search prior chat sessions and message history",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "history search text"}},
                },
            ),
            "mcp__kirocrew-cron__create_task": ToolBinding(
                "mcp__kirocrew-cron__create_task",
                "kirocrew-cron",
                "create_task",
                "Create a scheduled reminder",
                {"type": "object", "properties": {"schedule": {"type": "string"}}},
            ),
        }
        matches, waited = manager.search_tools("chat history", limit=1)
        self.assertLess(waited, 20.0)
        self.assertEqual([m.public_name for m in matches], ["mcp__kirocrew-core__search_chat_history"])

        # A nonsense query must not expose arbitrary low-ranked tools merely
        # because fuzzy matching gives every pair a small non-zero score.
        no_matches, _ = manager.search_tools("zzzxxyyq unrelated", limit=8)
        self.assertEqual(no_matches, [])
        manager.close()

    def test_call_tool_result_marks_is_error_for_model(self) -> None:
        manager = McpManager([])
        manager.bindings = {
            "mcp__fake__mutate": ToolBinding(
                "mcp__fake__mutate", "fake", "mutate", "Mutate", {"type": "object"}
            )
        }

        class FakeServer:
            def is_running(self) -> bool:
                return True

            def call(self, _name: str, _args: dict[str, Any]) -> dict[str, Any]:
                return {
                    "content": [{"type": "text", "text": "permission denied"}],
                    "isError": True,
                }

            def close(self) -> None:
                return None

        manager.servers = {"fake": FakeServer()}  # type: ignore[assignment]
        rendered = manager.call("mcp__fake__mutate", {})
        self.assertTrue(rendered.startswith("Error: MCP tool reported failure"))
        self.assertIn("permission denied", rendered)
        manager.close()

    def test_call_tool_result_prefers_model_content_over_structured_content(self) -> None:
        result = {
            "content": [{"type": "text", "text": "three matches"}],
            "structuredContent": {"matches": 3},
            "isError": False,
        }
        self.assertEqual(McpManager._render_tool_result(result), "three matches")

    def test_call_tool_result_uses_structured_content_when_text_absent(self) -> None:
        result = {"content": [], "structuredContent": {"matches": 3}, "isError": False}
        self.assertEqual(McpManager._render_tool_result(result), '{"matches": 3}')

    def test_export_order_is_deterministic_for_provider_prefix_caching(self) -> None:
        manager = McpManager([])
        # No process needed: seed bindings in deliberately reversed order.
        from kiro_byo.mcp_client import ToolBinding
        manager.bindings = {
            "mcp__z__b": ToolBinding("mcp__z__b", "z", "b", "b", {"type": "object"}),
            "mcp__a__a": ToolBinding("mcp__a__a", "a", "a", "a", {"type": "object"}),
        }
        names = [x["function"]["name"] for x in manager.tool_specs()]
        self.assertEqual(names, ["mcp__a__a", "mcp__z__b"])
        manager.close()


if __name__ == "__main__":
    unittest.main()
