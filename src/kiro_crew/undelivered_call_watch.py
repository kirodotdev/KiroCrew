"""Hear a dispatched worker's work-tool call that the harness never delivered.

On KAS a model that calls a work tool by its bare name (``work_report``) gets
``Tool "work_report" is not available.`` and the call never reaches the server
(:mod:`kiro_crew.agent_sdk.kas_tool_names`), so the worker's report is lost and its
conductor sees only silence. Each turn consumer -- the dashboard turn and the
messaging ``TurnDriver`` -- holds one :class:`UndeliveredCallWatch` per turn, built
with the session key that consumer already authenticated and the provider serving
the turn. When the watch sees that unresolved call fail, it records it on the
worker's item through the store (:mod:`kiro_crew.work_ledger_undelivered`) and pulls
the conductor's work-ledger watch forward.

What it trusts, and why that is enough:

* KAS only, by the provider's own label (positive identity, harness-parity H5).
* Only a ``tool_call`` frame with no server and no tool identity, kind ``other``:
  how KAS frames a call it could not resolve, with the name the model called as
  its wire title. A shell call is kind ``execute``, a resolved MCP call names its
  server.
* Only a result that ``failed`` whose whole output IS KAS's could-not-resolve answer,
  naming the same worker tool as the frame.
* The record is keyed by the turn's own session key and lands only on an open item
  that session is bound to, so a session can at most flag its own item.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from kiro_crew.agent_sdk.kas_tool_names import (
    KAS_TOOL_UNAVAILABLE_RE,
    is_kas_provider_label,
    kas_mcp_tool_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WORK_SERVER",
    "UndeliveredCallWatch",
    "record_undelivered_call",
    "undelivered_work_tool",
]

#: The MCP server the worker tools live on.
WORK_SERVER = "kirocrew-work"

#: Mirrors :data:`kiro_crew.work_ledger_undelivered.WORKER_TOOLS` (pinned equal by a
#: test) so judging a frame does not load the store.
_WORKER_TOOLS: frozenset[str] = frozenset({"work_brief", "work_report"})

#: The tool kind KAS gives a call it could not resolve.
_UNRESOLVED_KIND = "other"

#: Unresolved worker-tool frames one watch remembers per turn. A turn makes far
#: fewer calls than this. Each entry is a bounded call id mapped to one of the
#: :data:`_WORKER_TOOLS` constants, so this cap bounds the map's bytes too.
_MAX_WATCHED_CALLS = 256

#: Longest ``tool_call_id`` the watch keeps. KAS ids are short; a longer one is
#: not a frame this watch judges.
_MAX_CALL_ID_LEN = 128

#: Longest wire title read as a worker-tool name. Every spelling
#: :data:`_QUALIFIED` accepts is far shorter.
_MAX_TITLE_LEN = 256

#: How a worker tool may be spelled in the name KAS echoes: bare, or one of the
#: qualified forms a model might have tried. Matched against the whole name, so a
#: tool on another server that merely ends in ``work_report`` is not a worker tool.
_QUALIFIED = re.compile(
    r"^(?:@kirocrew-work/|mcp__kirocrew-work__|kirocrew-work::|kirocrew-work_|"
    + re.escape(kas_mcp_tool_id(WORK_SERVER, ""))
    + r")?(?P<tool>[a-z_]+)$"
)


def _worker_tool(name: str | None) -> str | None:
    named = _QUALIFIED.match((name or "").strip().lower())
    if named and named.group("tool") in _WORKER_TOOLS:
        return named.group("tool")
    return None


def undelivered_work_tool(
    *, wire_title: str | None, kind: str | None, status: str | None, output: str | None
) -> str | None:
    """The worker tool an unresolved call named, or ``None``.

    *wire_title* and *kind* come from the call's own ``tool_call`` frame (one that
    named no server and no tool); *status* and *output* from its result. All must
    agree: the call ``failed``, its whole output IS KAS's could-not-resolve answer,
    and that answer and the frame name the same worker tool. A shell command printing
    the same sentence is an ``execute`` frame, and a call the server refused (a
    ``done`` with no evidence) reached the ledger and came back with its own answer.
    """
    if status != "failed" or kind != _UNRESOLVED_KIND or not output or len(output) > 1024:
        return None
    text = output.strip()
    if text.startswith('"'):
        # ``rawOutput`` can arrive as a JSON string rather than bare text.
        try:
            decoded = json.loads(text)
        except ValueError:
            return None
        text = decoded.strip() if isinstance(decoded, str) else ""
    match = KAS_TOOL_UNAVAILABLE_RE.fullmatch(text)
    if match is None:
        return None
    called = _worker_tool(match.group("name"))
    return called if called is not None and called == _worker_tool(wire_title) else None


async def record_undelivered_call(worker_session_key: str, tool: str) -> dict[str, Any] | None:
    """Record the call on the worker's item off the event loop, then pull the
    conductor forward.

    The push is the conductor's ordinary work-ledger pull-forward
    (:func:`kiro_crew.conductor_wake.wake_for_undelivered_report`): batched, budgeted,
    and read when its tick runs, so a worker that retries with the right name before
    that tick clears the record and the tick finds nothing to wake for.
    """
    from kiro_crew import conductor_wake, work_ledger

    # The failure's place in the order is taken here, on the event loop, in the same
    # step that judged the result frame: a clear that runs while the note is off the
    # loop then orders after it. The note settles it. A cancelled turn settles it
    # only when the note never started: one already running settles itself, and
    # settling it early would leave a clear in the meantime with nothing to fence.
    seen = work_ledger.undelivered_seen(worker_session_key)
    gate = threading.Lock()
    claimed: list[str] = []

    def _note() -> dict[str, Any] | None:
        with gate:
            if claimed:
                return None
            claimed.append("note")
        return work_ledger.note_undelivered(worker_session_key, tool, seen=seen)

    try:
        noted = await asyncio.to_thread(_note)
    except BaseException:
        with gate:
            abandoned = not claimed
            if abandoned:
                claimed.append("abandoned")
        if abandoned:
            work_ledger.settle_undelivered(worker_session_key, seen)
        raise
    if noted is not None:
        conductor_wake.wake_for_undelivered_report(noted["conductor_slot_key"], noted["item_id"])
    return noted


def _serves_kas(provider: object) -> bool:
    """Whether *provider* is a KAS session, by its own provider label."""
    from kiro_crew.agent_sdk import context_provider_of

    served = context_provider_of(provider)
    if served is None:
        return False
    try:
        return is_kas_provider_label(served.context_provider_type)
    except Exception:  # noqa: BLE001 - an unreadable label is not KAS
        return False


class UndeliveredCallWatch:
    """One turn's watch for a worker-tool call the harness never delivered.

    Built per turn by the consumer that streams it. Off KAS it records nothing.
    """

    def __init__(self, session_key: str, provider: object) -> None:
        self._session_key = session_key or ""
        self._active = bool(self._session_key) and _serves_kas(provider)
        # call id -> the worker tool its frame named (a :data:`_WORKER_TOOLS` constant).
        self._frames: dict[str, str] = {}

    def on_call(self, event: Any) -> None:
        """Remember an unresolved ``tool_call`` frame that named a worker tool.

        Keeps only what :meth:`_judge` needs: the call id and the worker tool, never
        the backend's own strings, so the map stays bounded in bytes as well as count.
        """
        if not self._active:
            return
        call_id = str(getattr(event, "tool_call_id", "") or "")
        server = str(getattr(event, "mcp_server_name", "") or "")
        tool = str(getattr(event, "tool_name", "") or "")
        if not call_id or len(call_id) > _MAX_CALL_ID_LEN or server or tool:
            return
        if getattr(event, "tool_kind", "") != _UNRESOLVED_KIND:
            return
        title = getattr(event, "wire_title", "") or ""
        if not isinstance(title, str) or len(title) > _MAX_TITLE_LEN:
            return
        named = _worker_tool(title)
        if named is None:
            return
        if len(self._frames) >= _MAX_WATCHED_CALLS and call_id not in self._frames:
            return
        self._frames[call_id] = named

    async def on_result(self, event: Any) -> dict[str, Any] | None:
        """Judge a result frame against its call; record it when undelivered.

        Never raises (cancellation aside): this runs inside a worker's turn.
        """
        if not self._active:
            return None
        try:
            return await self._judge(event)
        except Exception:  # noqa: BLE001 - bookkeeping must not cost the turn
            logger.warning("undelivered-call watch failed on a result frame", exc_info=True)
            return None

    async def _judge(self, event: Any) -> dict[str, Any] | None:
        call_id = getattr(event, "tool_call_id", "") or ""
        status = getattr(event, "tool_status", "") or ""
        named = self._frames.get(call_id)
        if named is None or status in ("", "pending", "in_progress"):
            return None
        del self._frames[call_id]
        tool = undelivered_work_tool(
            wire_title=named,
            kind=_UNRESOLVED_KIND,
            status=status,
            output=getattr(event, "tool_output", "") or "",
        )
        if tool is None:
            return None
        return await record_undelivered_call(self._session_key, tool)
