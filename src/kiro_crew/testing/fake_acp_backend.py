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
  for pre-approved tools. The emitted ``rawInput`` carries the reserved purpose
  argument under its camelCase spelling (see ``_TOOL_PURPOSE``), so the pill's
  concise-label path is covered by the harness.
* ``[[REVIEW_FIX_EDIT:<relative-path>|<base64-bytes>]]`` -> write the decoded
  bytes below the process cwd and emit a completed or failed edit tool event.
  This is a deterministic Review Fix-only test protocol; absolute paths and
  traversal are rejected.
* ``[[PERMISSION]]`` in the prompt -> the ``[[TOOL]]`` sequence PLUS a
  server->client ``session/request_permission`` (to surface the approval
  modal). This is **fire-and-forget**: the fake does not gate its own
  completion on the outcome, and the host's answer is ignored. Resolving that
  permission needs the dashboard/UI, so this path is for Playwright E2E and
  live demos -- NOT the headless backend suite (a headless host has nothing to
  resolve the modal and the turn would stall).
* ``[[GATED]]`` in the prompt -> like ``[[PERMISSION]]`` but the fake WAITS for
  the host's answer (bounded by ``PERMISSION_WAIT_SECS``) and reflects it: a
  reject/cancel outcome yields ``status: "failed"`` on the tool_call_update.
  On timeout it reports ``completed``, preserving the never-block guarantee.
* ``[[SLOW]]`` -> stream ``SLOW_CHUNKS`` chunks with a delay, checking for a
  ``session/cancel`` between each. A cancel ends the turn early with
  ``stopReason: "cancelled"`` (the ACP soft-stop ack the host waits for).
* ``[[SLOW_NOACK]]`` -> the same slow stream but deliberately DEAF to cancel,
  so the host's ``soft_stop_budget_secs`` expires. Models an agent wedged in a
  long tool call, which is the "Stop Failed, Session Reset" path.
* ``[[SLOW_LATEACK]]`` -> honours the cancel like ``[[SLOW]]`` but winds down
  over ``SLOW_LATEACK_CHUNKS`` more chunks first. ``[[SLOW]]`` acks within one
  chunk, so the host's ``soft_pending`` state lasts ~250ms on average and a
  loaded browser can miss it entirely; the wind-down makes that state
  observable while still acking well inside the budget.
* ``[[ERROR]]`` -> reply with a JSON-RPC error instead of a result.
* ``[[MAXTOKENS]]`` / ``[[REFUSAL]]`` -> alternate terminal ``stopReason``.

Observing a cancel mid-turn requires reading stdin while a prompt is streaming,
so ``main()`` reads on a background thread into a queue rather than looping
read->handle. Calling ``_handle`` directly (as the unit tests do) leaves that
queue empty, every wait times out, and the default behaviour is unchanged.

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

import base64
import binascii
import json
import queue
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

PROTOCOL_VERSION = "2025-08-22"

# Stable, searchable marker the send->assert test asserts on. Kept distinctive
# so a real backend's output could never masquerade as this fake's reply.
REPLY_TEXT = "pong from the fake ACP backend"
FAKE_VERSION = "kiro-cli fake-e2e"
FAKE_IDENTITY = "fake-e2e-user"

# Prompt sentinels. Absent by default so a plain prompt stays text-only.
TOOL_TRIGGER = "[[TOOL]]"
REVIEW_FIX_EDIT_PREFIX = "[[REVIEW_FIX_EDIT:"
REVIEW_FIX_EDIT_SUFFIX = "]]"
PERMISSION_TRIGGER = "[[PERMISSION]]"
# Permission that actually GATES: the fake waits for the host's answer and
# reflects it in the tool_call_update status. Distinct from PERMISSION_TRIGGER,
# whose fire-and-forget behaviour existing specs rely on.
GATED_PERMISSION_TRIGGER = "[[GATED]]"
# A turn long enough for a host to press Stop mid-flight. SLOW honours
# session/cancel; SLOW_NOACK deliberately ignores it so the host's soft-stop
# budget expires (the "Stop Failed, Session Reset" path). SLOW_LATEACK honours
# it but winds down over a few chunks first, so the host's `soft_pending` state
# is observable for a bounded interval instead of a race.
SLOW_TRIGGER = "[[SLOW]]"
SLOW_NOACK_TRIGGER = "[[SLOW_NOACK]]"
SLOW_LATEACK_TRIGGER = "[[SLOW_LATEACK]]"
# Terminal outcomes other than end_turn.
ERROR_TRIGGER = "[[ERROR]]"
MAX_TOKENS_TRIGGER = "[[MAXTOKENS]]"
REFUSAL_TRIGGER = "[[REFUSAL]]"

# Slow-stream shape. Module-level so unit tests can shrink them to run fast:
# 30 x 0.5s = ~15s, comfortably longer than the 0.5s-60s soft_stop_budget_secs
# range the dashboard allows, so a NOACK turn always outlives the budget.
SLOW_CHUNKS = 30
SLOW_CHUNK_DELAY_SECS = 0.5
SLOW_CHUNK_TEXT = "fake slow chunk "
# How many MORE chunks a SLOW_LATEACK turn emits after it notices the cancel,
# before acking. Counted in chunks rather than seconds so the unit test is
# deterministic and does not depend on wall clock. At the default
# SLOW_CHUNK_DELAY_SECS that is ~3s, which is well inside the 0.5s-60s
# soft_stop_budget_secs range (so the ack always beats the budget and the turn
# ends cooperatively) and roughly 12x the ~250ms window a plain SLOW cancel
# leaves, which is too short for a loaded browser to paint.
SLOW_LATEACK_CHUNKS = 6
# How long a gated permission waits for the host's answer before giving up.
# Bounded on purpose: the headless backend suite has nothing to resolve a modal,
# and a hang there would stall the whole turn.
PERMISSION_WAIT_SECS = 15.0
_POLL_INTERVAL_SECS = 0.02

ERROR_CODE = -32603
ERROR_MESSAGE = "fake ACP backend: injected failure"

_SESSION_ID = "fake-1"
_TOOL_CALL_ID = "fake-tool-1"
_REVIEW_FIX_EDIT_TOOL_CALL_ID = "fake-review-fix-edit-1"
# The agent-authored purpose line, carried as a reserved tool argument. kiro-cli
# echoes it back in ``rawInput`` under EITHER spelling; the fake emits the
# camelCase one so the harness exercises the shape the dashboard's concise tool
# pill can otherwise drop (falling back to the literal command line).
_TOOL_PURPOSE = "Say hello from the fake backend"
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


def _error(req_id: Any, code: int = ERROR_CODE, message: str = ERROR_MESSAGE) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# --------------------------------------------------------------------------- #
# Mid-turn inbox
#
# main() reads stdin on a background thread and pushes into this queue, so a
# handler can poll for cancels / permission answers while a session/prompt is
# still streaming. A strict read -> handle loop cannot observe a session/cancel
# arriving DURING a session/prompt until the turn has already finished.
#
# Direct _handle() callers (the unit tests) leave the queue empty: every wait
# below then simply times out and the default behaviour is unchanged.
# --------------------------------------------------------------------------- #
_INBOX: queue.Queue[dict[str, Any] | None] = queue.Queue()


def _poll_inbox(match: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """Scan everything queued right now for a match, without blocking.

    Non-matching messages (and the EOF sentinel) are put back in order so the
    main loop still handles them. Returns the first match, else None.
    """
    deferred: list[dict[str, Any] | None] = []
    found: dict[str, Any] | None = None
    while True:
        try:
            msg = _INBOX.get_nowait()
        except queue.Empty:
            break
        if found is None and msg is not None and match(msg):
            found = msg
            continue
        deferred.append(msg)
    for m in deferred:
        _INBOX.put(m)
    return found


def _await_inbox(
    match: Callable[[dict[str, Any]], bool], timeout: float
) -> dict[str, Any] | None:
    """Poll for a matching message until `timeout` elapses. Never blocks forever."""
    deadline = time.monotonic() + timeout
    while True:
        hit = _poll_inbox(match)
        if hit is not None:
            return hit
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL_SECS)


def _is_cancel_for(session_id: str) -> Callable[[dict[str, Any]], bool]:
    def _match(msg: dict[str, Any]) -> bool:
        if msg.get("method") != "session/cancel":
            return False
        params = msg.get("params") or {}
        # A cancel without a sessionId is treated as "cancel the current turn".
        return str(params.get("sessionId", session_id)) == session_id

    return _match


def _cancel_requested(session_id: str) -> bool:
    return _poll_inbox(_is_cancel_for(session_id)) is not None


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
        str(b.get("text", ""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts)


def _review_fix_edit_payload(text: str) -> tuple[str, bytes] | None:
    """Decode one Review Fix edit marker without accepting host paths.

    The fake process is already cwd'd into the candidate worktree. The marker
    carries only a relative path and bytes, never a command, so a fixture cannot
    turn the fake backend into an arbitrary subprocess launcher.
    """
    start = text.find(REVIEW_FIX_EDIT_PREFIX)
    if start < 0:
        return None
    payload_start = start + len(REVIEW_FIX_EDIT_PREFIX)
    end = text.find(REVIEW_FIX_EDIT_SUFFIX, payload_start)
    if end < 0:
        raise ValueError("Review Fix edit marker is not closed")

    payload = text[payload_start:end]
    target_text, separator, encoded = payload.partition("|")
    target = Path(target_text)
    windows_target = PureWindowsPath(target_text)
    if (
        not separator
        or not target_text
        or "\x00" in target_text
        or target.is_absolute()
        or windows_target.is_absolute()
        or bool(windows_target.drive)
        or ".." in target.parts
        or ".." in windows_target.parts
        or target == Path(".")
    ):
        raise ValueError("Review Fix edit path must be a non-empty relative path")

    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Review Fix edit content is not valid base64") from exc
    return target_text, content


def _write_review_fix_edit(target_text: str, content: bytes) -> None:
    """Write decoded content only when the resolved target stays under cwd."""
    root = Path.cwd().resolve()
    target = (root / Path(target_text)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Review Fix edit target escapes the candidate worktree") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _emit_review_fix_edit(session_id: str, text: str) -> None:
    """Apply a Review Fix marker and expose the operation as an agent tool event."""
    if REVIEW_FIX_EDIT_PREFIX not in text:
        return

    target_text = "invalid"
    payload: tuple[str, bytes] | None = None
    status = "completed"
    try:
        payload = _review_fix_edit_payload(text)
        if payload is None:
            raise ValueError("Review Fix edit marker is missing")
        target_text = payload[0]
    except ValueError:
        status = "failed"

    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": _REVIEW_FIX_EDIT_TOOL_CALL_ID,
            "title": "review fix edit",
            "kind": "edit",
            "status": "pending",
            "rawInput": {
                "path": target_text,
                "__toolUsePurpose": "Apply deterministic Review Fix edit",
            },
        },
    )
    if status == "completed" and payload is not None:
        try:
            _write_review_fix_edit(*payload)
        except (OSError, ValueError):
            status = "failed"
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": _REVIEW_FIX_EDIT_TOOL_CALL_ID,
            "status": status,
        },
    )


def _permission_status(session_id: str) -> str:
    """Wait for the host's permission answer and map it to a tool_call status.

    Bounded by PERMISSION_WAIT_SECS. On timeout we report "completed" so a
    headless host that cannot resolve a modal still sees a finished turn --
    the same never-block guarantee the fire-and-forget path gives.
    """
    answer = _await_inbox(
        lambda m: m.get("method") is None and m.get("id") == _PERMISSION_REQ_ID,
        PERMISSION_WAIT_SECS,
    )
    if answer is None:
        return "completed"
    outcome = ((answer.get("result") or {}).get("outcome") or {}).get("outcome")
    option = ((answer.get("result") or {}).get("outcome") or {}).get("optionId", "")
    if outcome == "cancelled" or "reject" in str(option):
        return "failed"
    return "completed"


def _emit_tool_call(
    session_id: str, *, with_permission: bool, gated: bool = False
) -> None:
    """Emit a tool_call (+ optional approval modal) then a completed update."""
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": _TOOL_CALL_ID,
            "title": "fake demo tool",
            "kind": "execute",
            "status": "pending",
            "rawInput": {
                "command": "echo hello-from-fake",
                "__toolUsePurpose": _TOOL_PURPOSE,
            },
        },
    )
    status = "completed"
    if with_permission:
        # Surface an approval modal for UI E2E / demos. Fire-and-forget by
        # default: the fake does not wait for the outcome (a real backend
        # would). The host answers on its own channel; that response is ignored
        # by the loop. With gated=True we DO wait and reflect the answer, which
        # is what a negative-path spec needs.
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
                        {
                            "optionId": "allow_once",
                            "name": "Allow once",
                            "kind": "allow_once",
                        },
                        {
                            "optionId": "reject_once",
                            "name": "Reject",
                            "kind": "reject_once",
                        },
                    ],
                },
            }
        )
        if gated:
            status = _permission_status(session_id)
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": _TOOL_CALL_ID,
            "status": status,
        },
    )


def _stream_slowly(
    session_id: str, *, cancel_aware: bool, ack_after_chunks: int = 0
) -> bool:
    """Stream SLOW_CHUNKS chunks with a delay. True if cancelled mid-stream.

    cancel_aware=False models an agent stuck in a long tool call that cannot
    acknowledge a stop, so the host's soft-stop budget expires.

    ack_after_chunks>0 models an agent that notices the cancel but takes a
    bounded moment to wind down: it emits that many more chunks, then acks. The
    host stays in `soft_pending` for the whole wind-down, which is what makes
    that state observable to a UI assertion instead of a ~250ms race, while
    still ending the turn cooperatively so nothing leaks into the next turn.

    The cancel observation is LATCHED because `_cancel_requested` consumes the
    message from the inbox: a second call after the wind-down starts would
    return False and the ack would never fire.
    """
    cancelled = False
    winding_down = 0
    for i in range(SLOW_CHUNKS):
        if cancel_aware and not cancelled and _cancel_requested(session_id):
            cancelled = True
            winding_down = ack_after_chunks
        if cancelled:
            if winding_down <= 0:
                return True
            winding_down -= 1
        _update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"{SLOW_CHUNK_TEXT}{i} "},
            },
        )
        time.sleep(SLOW_CHUNK_DELAY_SECS)
    # A cancel arriving during the final sleep still counts.
    return bool(cancelled or (cancel_aware and _cancel_requested(session_id)))


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
        _emit_review_fix_edit(session_id, text)
        if ERROR_TRIGGER in text:
            # A JSON-RPC error instead of a result: the turn fails, not stops.
            _error(req_id)
            return
        if GATED_PERMISSION_TRIGGER in text:
            _emit_tool_call(session_id, with_permission=True, gated=True)
        elif PERMISSION_TRIGGER in text:
            _emit_tool_call(session_id, with_permission=True)
        elif TOOL_TRIGGER in text:
            _emit_tool_call(session_id, with_permission=False)

        if SLOW_NOACK_TRIGGER in text:
            _stream_slowly(session_id, cancel_aware=False)
            stop_reason = "end_turn"
        elif SLOW_LATEACK_TRIGGER in text:
            cancelled = _stream_slowly(
                session_id, cancel_aware=True, ack_after_chunks=SLOW_LATEACK_CHUNKS
            )
            stop_reason = "cancelled" if cancelled else "end_turn"
        elif SLOW_TRIGGER in text:
            cancelled = _stream_slowly(session_id, cancel_aware=True)
            stop_reason = "cancelled" if cancelled else "end_turn"
        else:
            _update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": REPLY_TEXT},
                },
            )
            if MAX_TOKENS_TRIGGER in text:
                stop_reason = "max_tokens"
            elif REFUSAL_TRIGGER in text:
                stop_reason = "refusal"
            else:
                stop_reason = "end_turn"
        _result(req_id, {"stopReason": stop_reason})
    else:
        # session/set_mode, session/set_model, or any other awaited request:
        # reply empty so the turn never blocks.
        _result(req_id, {})


def _pump_stdin() -> None:
    """Read stdin into _INBOX until EOF, then push the None sentinel."""
    while True:
        msg = _read_message()
        _INBOX.put(msg)
        if msg is None:
            return


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(FAKE_VERSION)
        return
    if args == ["whoami"]:
        print(FAKE_IDENTITY)
        return
    if args == ["acp", "--help"]:
        # The readiness probe runs this to confirm the `acp` subcommand exists
        # (kiro_prerequisite._probe_acp_support). Answer success so the offline
        # gateway clears the acp-support gate exactly as a real, current kiro-cli
        # would; the real ACP session still drives the protocol over stdio when
        # invoked as `acp` with no `--help`.
        print("Usage: kiro-cli acp [OPTIONS]")
        return
    # Read on a daemon thread so _handle can poll _INBOX for a session/cancel
    # that arrives WHILE a prompt is streaming. select() on stdin is not an
    # option: the backend suite also runs on Windows.
    reader = threading.Thread(target=_pump_stdin, name="fake-acp-stdin", daemon=True)
    reader.start()
    while True:
        msg = _INBOX.get()
        if msg is None:
            break
        _handle(msg)


if __name__ == "__main__":
    main()
