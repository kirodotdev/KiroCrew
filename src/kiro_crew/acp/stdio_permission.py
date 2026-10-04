"""Fail-closed ACP permission relay for owned stdio adapters.

The direct OpenAI-compatible adapters are ACP servers while KiroCrew is their
ACP client.  Before an adapter invokes any built-in or bridged MCP tool it must
send ``session/request_permission`` back to that client and wait for the exact
decision.  A missing reader, EOF, malformed response, or cancellation denies
the call.
"""

from __future__ import annotations

import json
import queue
import uuid
from collections.abc import Callable
from typing import Any


def make_permission_decider(
    inbox: "queue.Queue[dict[str, Any] | None]",
    deferred: list[dict[str, Any]],
    *,
    on_cancel: Callable[[str], None] | None = None,
    on_eof: Callable[[], None] | None = None,
) -> Callable[[str, str, str, str, dict[str, Any]], bool]:
    """Return a synchronous, fail-closed ACP permission callback.

    A dedicated reader thread must own stdin and feed *inbox*.  Unrelated
    messages are preserved in *deferred* for the adapter's normal dispatcher.
    """

    def decide(
        session_id: str,
        tool_call_id: str,
        title: str,
        kind: str,
        raw_input: dict[str, Any],
    ) -> bool:
        request_id = f"kirocrew-permission-{uuid.uuid4()}"
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": title,
                    "kind": kind,
                    "rawInput": raw_input,
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {
                        "optionId": "allow_always",
                        "name": "Always allow",
                        "kind": "allow_always",
                    },
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
        print(json.dumps(request, separators=(",", ":")), flush=True)

        while True:
            message = inbox.get()
            if message is None:
                if on_eof is not None:
                    on_eof()
                return False
            if message.get("id") == request_id and not message.get("method"):
                outcome = (message.get("result") or {}).get("outcome") or {}
                return bool(
                    outcome.get("outcome") == "selected"
                    and outcome.get("optionId") in {"allow_once", "allow_always"}
                )
            if message.get("method") == "session/cancel":
                params = message.get("params") or {}
                cancelled_session = str(params.get("sessionId") or "")
                if on_cancel is not None:
                    on_cancel(cancelled_session)
                if cancelled_session == session_id:
                    return False
                continue
            deferred.append(message)

    return decide
