"""A stdio MCP server exposing the guide as read-only agent tools.

Why stdio and not HTTP: a Kiro Crew builtin runs IN-PROCESS inside the gateway,
so it has no backend port of its own, and the app bridge skips a URL-based MCP
entry when there is no live port. A command (stdio) entry has no port to be
dead, so that is the shape a builtin must use.

It reads the SAME merged data the HTTP routes read, through the same
``search`` module, so the ranking an agent sees and the ranking the UI shows can
never diverge. Nothing here mutates anything — the guide is data — so both tools
are safe to auto-approve. The edition is naturally correct: the tools read the
merged base+overlay, so an internal machine's agent sees internal entries and a
public user physically cannot.

Run as: ``python -m kiro_crew.apps.builtins.guide.backend.mcp_server``
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import search

_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_PROTOCOL_VERSION = "2024-11-05"

#: Cap a single result so an agent cannot pull an unbounded blob into context.
_MAX_RESULT_CHARS = 60_000


def _redact(text: str) -> str:
    """Best-effort credential scrub before a result reaches the model.

    Guide data has already passed the publish-time exfil linter, so this is
    belt-and-suspenders; fail-closed to a fixed string if the scanner is absent.
    """
    try:
        from kiro_crew.security import redact

        return redact(text)
    except Exception:  # noqa: BLE001 - never emit unscanned text
        return '{"error": "result withheld: redaction unavailable"}'


def _tool_guide_search(args: dict[str, Any]) -> dict[str, Any]:
    """Ranked entry summaries for a natural-language query or error string."""
    query = str(args.get("query") or "")
    platform = args.get("platform")
    topic = args.get("topic")
    limit = args.get("limit")
    try:
        n = int(limit) if limit is not None else 5
    except (TypeError, ValueError):
        n = 5
    results = search.search(
        query,
        platform=str(platform) if platform else None,
        topic=str(topic) if topic else None,
        limit=n,
    )
    return {"results": results, "total": len(results)}


def _tool_guide_get(args: dict[str, Any]) -> dict[str, Any]:
    """One entry's full text: steps, if_stuck, crew_prompt, sources."""
    entry_id = str(args.get("id") or "").strip()
    if not entry_id:
        raise ValueError("id is required")
    entry = search.get_entry(entry_id)
    if entry is None:
        raise ValueError(f"no guide entry {entry_id}")
    return entry


TOOLS: dict[
    str, tuple[Callable[[dict[str, Any]], dict[str, Any]], str, dict[str, Any], list[str]]
] = {
    "guide_search": (
        _tool_guide_search,
        "Search the Kiro Crew guide by symptom, error text, or topic. Returns "
        "ranked summaries (id, title, symptom, trust, one-line fix).",
        {
            "query": {"type": "string", "description": "Natural-language query or error text."},
            "platform": {"type": "string", "description": "Filter to a platform (e.g. macos)."},
            "topic": {"type": "string", "description": "Filter to a topic."},
            "limit": {"type": "integer", "description": "Max results (default 5, cap 50)."},
        },
        ["query"],
    ),
    "guide_get": (
        _tool_guide_get,
        "Fetch one guide entry in full by id: steps, if_stuck, crew_prompt, sources.",
        {"id": {"type": "string", "description": "The guide entry id."}},
        ["id"],
    ),
}


def _input_schema(name: str) -> dict[str, Any]:
    _fn, _desc, props, required = TOOLS[name]
    return {"type": "object", "properties": props, "required": required}


def _schema() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": desc, "inputSchema": _input_schema(name)}
        for name, (_fn, desc, _props, _req) in TOOLS.items()
    ]


def _result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns None for a notification."""
    method = str(request.get("method") or "")
    req_id = request.get("id")
    raw_params = request.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "guide", "version": "0.1.0"},
            },
        )
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return _result(req_id, {"tools": _schema()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        entry = TOOLS.get(name)
        if entry is None:
            return _error(req_id, _METHOD_NOT_FOUND, f"unknown tool: {_redact(name)}")
        raw_args = params.get("arguments")
        if raw_args is not None and not isinstance(raw_args, dict):
            return _error(req_id, _INVALID_PARAMS, "invalid arguments: expected an object")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        fn = entry[0]
        try:
            payload = fn(args)
        except ValueError as exc:
            return _error(req_id, _INVALID_PARAMS, _redact(str(exc)))
        except Exception as exc:  # noqa: BLE001 - a tool error is a result, not a crash
            return _error(req_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {_redact(str(exc))}")
        text = _redact(json.dumps(payload, default=str))[:_MAX_RESULT_CHARS]
        return _result(req_id, {"content": [{"type": "text", "text": text}]})
    return _error(req_id, _METHOD_NOT_FOUND, f"unknown method: {method}")


def main() -> None:
    """Read line-delimited JSON-RPC on stdin, write replies on stdout.

    One malformed line must not end the session: dying here would surface as the
    whole MCP server disappearing mid-session.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        reply = handle(request)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
