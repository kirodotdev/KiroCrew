"""A minimal stdio MCP server that behaves like an MCP App (SEP-1865).

Used by ``test_mcp_apps_e2e.py`` as a REAL child process on the other side of
the gateway ``Backend``: it speaks line-delimited JSON-RPC on stdin/stdout,
exposes one model-visible tool (``draw``) whose result carries a
``_meta.ui.resourceUri`` reference, one app-only tool (``save_state``), and
serves the referenced ``ui://fake/app.html`` resource via ``resources/read``.

To make the client-capability injection observable from the OUTSIDE, the
server records whether the ``initialize`` request advertised the
``io.modelcontextprotocol/ui`` extension and embeds ``ui-ext-seen=true|false``
in every ``draw`` result text.

``--break-resources`` makes ``resources/read`` return a JSON-RPC error, so the
failure-safe delivery path (original result, no marker) can be exercised.

Stdlib only — no third-party imports — so the test needs no extra deps.
"""

from __future__ import annotations

import json
import sys

UI_EXTENSION_KEY = "io.modelcontextprotocol/ui"
UI_RESOURCE_URI = "ui://fake/app.html"
UI_MIME = "text/html;profile=mcp-app"
UI_HTML = "<!DOCTYPE html><html><body>fake app</body></html>"
UI_CSP = {"resourceDomains": ["https://esm.sh"]}


def _reply(msg_id, result=None, error=None):
    frame = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        frame["error"] = error
    else:
        frame["result"] = result
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def main() -> int:
    break_resources = "--break-resources" in sys.argv[1:]
    # Mimic the real pdf-server / Excalidraw shape: the ui:// association is
    # declared ONLY on the tool definition in tools/list; the tools/call
    # result carries no _meta.ui. Exercises the gateway's declared-uri
    # fallback path.
    declared_only = "--declared-only" in sys.argv[1:]
    saw_ui_extension = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications (no id) are consumed silently.
        if msg_id is None:
            continue

        if method == "initialize":
            params = msg.get("params") or {}
            caps = params.get("capabilities") or {}
            ext = caps.get("extensions") or {}
            saw_ui_extension = UI_EXTENSION_KEY in ext
            _reply(msg_id, result={
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "fake-mcp-app", "version": "1.0.0"},
            })
        elif method == "tools/list":
            _reply(msg_id, result={"tools": [
                {
                    "name": "draw",
                    "description": "Draw something and open the fake app UI.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "_meta": {"ui": {
                        "resourceUri": UI_RESOURCE_URI,
                        "visibility": ["model", "app"],
                    }},
                },
                {
                    "name": "save_state",
                    "description": "App-only callback tool.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "_meta": {"ui": {"visibility": ["app"]}},
                },
            ]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tool = params.get("name")
            if tool == "draw":
                result = {
                    "content": [{
                        "type": "text",
                        "text": f"drew a thing ui-ext-seen={str(saw_ui_extension).lower()}",
                    }],
                    "structuredContent": {"shapes": 1},
                }
                if not declared_only:
                    result["_meta"] = {"ui": {"resourceUri": UI_RESOURCE_URI}}
                _reply(msg_id, result=result)
            elif tool == "save_state":
                _reply(msg_id, result={
                    "content": [{"type": "text", "text": "state saved"}],
                })
            else:
                _reply(msg_id, error={"code": -32602, "message": f"unknown tool {tool!r}"})
        elif method == "resources/read":
            if break_resources:
                _reply(msg_id, error={"code": -32603, "message": "resources are broken today"})
                continue
            params = msg.get("params") or {}
            uri = params.get("uri")
            if uri != UI_RESOURCE_URI:
                _reply(msg_id, error={"code": -32002, "message": f"unknown resource {uri!r}"})
                continue
            _reply(msg_id, result={"contents": [{
                "uri": UI_RESOURCE_URI,
                "mimeType": UI_MIME,
                "text": UI_HTML,
                "_meta": {"ui": {"csp": UI_CSP, "prefersBorder": True}},
            }]})
        elif method == "ping":
            _reply(msg_id, result={})
        else:
            _reply(msg_id, error={"code": -32601, "message": f"method {method!r} not found"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
