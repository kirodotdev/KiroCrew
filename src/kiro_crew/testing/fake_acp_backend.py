#!/usr/bin/env python3
"""Fake ACP model backend for offline E2E testing and demos.

Speaks the minimal subset of the Agent Client Protocol (JSON-RPC 2.0 over
newline-delimited stdio) that ``kiro_crew.acp.client.AcpClient`` drives:

    initialize        -> {protocolVersion, agentCapabilities: {loadSession: false}}
    session/new       -> {sessionId}
    session/set_mode  -> {}   (host awaits it; reply so the handshake never blocks)
    session/set_model -> {}
    session/prompt    -> stream update(s), then {stopReason: "end_turn"}

Prompt-driven behaviour on ``session/prompt`` (the reply is always sent last):

* Default text -> stream one ``agent_message_chunk`` (the canned reply).
* ``[[TOOL]]`` in the prompt -> also emit a ``tool_call`` + ``tool_call_update``
  (no permission). Exercises the tool-card UI and is deterministic headless:
  kiro-cli likewise emits tool calls that never raise ``session/request_permission``
  for pre-approved tools.
* ``[[PERMISSION]]`` in the prompt -> the ``[[TOOL]]`` sequence PLUS a
  server->client ``session/request_permission`` (to surface the approval
  modal). This is **fire-and-forget**: the fake does not gate its own
  completion on the outcome, and the host's answer is ignored. Resolving that
  permission needs the dashboard/UI, so this path is for Playwright E2E and
  live demos -- NOT the headless backend suite (a headless host has nothing to
  resolve the modal and the turn would stall).

Deterministic, offline, no network, no auth, stdlib-only. Reachable ONLY via
the ``KIROCREW_KIRO_BIN`` override (the provider stays ``acp``); it is never
selectable from a real gateway. Run standalone as
``python -m kiro_crew.testing.fake_acp_backend`` (the pytest harness and the
live-test harness both point ``KIROCREW_KIRO_BIN`` at a launcher that runs it).
``AcpClient`` invokes it as ``<launcher> acp [--agent NAME ...]`` -- argv is
ignored on that path and the protocol is driven entirely over stdio. The
``--version`` and ``whoami`` commands return deterministic success so the
offline gateway exercises the same first-run readiness gate as production.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2025-08-22"

# Stable, searchable marker the send->assert test asserts on. Kept distinctive
# so a real backend's output could never masquerade as this fake's reply.
REPLY_TEXT = "pong from the fake ACP backend"
FAKE_VERSION = "kiro-cli fake-e2e"
FAKE_IDENTITY = "fake-e2e-user"

# Prompt sentinels. Absent by default so a plain prompt stays text-only.
TOOL_TRIGGER = "[[TOOL]]"
PERMISSION_TRIGGER = "[[PERMISSION]]"

_SESSION_ID = "fake-1"
_TOOL_CALL_ID = "fake-tool-1"
_PERMISSION_REQ_ID = 9001


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _notify(method: str, params: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def _update(session_id: str, update: dict[str, Any]) -> None:
    _notify("session/update", {"sessionId": session_id, "update": update})


def _read_message() -> dict[str, Any] | None:
    """Read one newline-delimited JSON-RPC message, or None at EOF."""
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF: host closed stdin.
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue


def _prompt_text(params: dict[str, Any]) -> str:
    """Concatenate the text blocks of a session/prompt request."""
    blocks = params.get("prompt")
    if not isinstance(blocks, list):
        return ""
    parts = [
        str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts)


def _emit_tool_call(session_id: str, *, with_permission: bool) -> None:
    """Emit a tool_call (+ optional approval modal) then a completed update."""
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": _TOOL_CALL_ID,
            "title": "fake demo tool",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "echo hello-from-fake"},
        },
    )
    if with_permission:
        # Surface an approval modal for UI E2E / demos. Fire-and-forget: the
        # fake does not wait for the outcome (a real backend would). The host
        # answers on its own channel; that response is ignored by the loop.
        _send(
            {
                "jsonrpc": "2.0",
                "id": _PERMISSION_REQ_ID,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": _TOOL_CALL_ID,
                        "title": "fake demo tool",
                        "kind": "execute",
                    },
                    "options": [
                        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            }
        )
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": _TOOL_CALL_ID,
            "status": "completed",
        },
    )


def _handle(msg: dict[str, Any]) -> None:
    method = msg.get("method")
    if method is None:
        # A response/error to one of our requests (e.g. the permission answer).
        # Nothing to do -- the fake never gates on it.
        return
    req_id = msg.get("id")
    if req_id is None:
        # Notification (e.g. session/cancel): nothing to answer.
        return

    if method == "initialize":
        _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False},
            },
        )
    elif method == "session/new":
        _result(req_id, {"sessionId": _SESSION_ID})
    elif method == "session/prompt":
        params = msg.get("params") or {}
        session_id = str(params.get("sessionId", _SESSION_ID))
        text = _prompt_text(params)
        if PERMISSION_TRIGGER in text:
            _emit_tool_call(session_id, with_permission=True)
        elif TOOL_TRIGGER in text:
            _emit_tool_call(session_id, with_permission=False)
        _update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": REPLY_TEXT},
            },
        )
        _result(req_id, {"stopReason": "end_turn"})
    else:
        # session/set_mode, session/set_model, or any other awaited request:
        # reply empty so the turn never blocks.
        _result(req_id, {})


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(FAKE_VERSION)
        return
    if args == ["whoami"]:
        print(FAKE_IDENTITY)
        return
    while True:
        msg = _read_message()
        if msg is None:
            break
        _handle(msg)


if __name__ == "__main__":
    main()
