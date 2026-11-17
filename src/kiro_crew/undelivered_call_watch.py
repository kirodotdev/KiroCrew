"""Hear a dispatched worker's work-tool call that the harness never delivered.

A model that calls a work tool by a name its harness does not offer (a bare
``work_report``) gets the harness's own could-not-resolve answer -- on KAS
``Tool "work_report" is not available.`` -- and the call never reaches the server,
so the worker's report is lost and its conductor sees only silence. Each turn
consumer -- the dashboard turn and the messaging ``TurnDriver`` -- holds one
:class:`UndeliveredCallWatch` per turn, built with the session key that consumer
already authenticated and the provider serving the turn. When the watch sees that
unresolved call end with the harness's answer, it records it on the worker's item
through the store (:mod:`kiro_crew.work_ledger_undelivered`) and pulls the
conductor's work-ledger watch forward.

What it trusts, and why that is enough:

* Only a harness whose answer was captured live (KAS, pi, OpenCode), by the
  provider's own label (positive identity, harness-parity H5); the answers are in
  :mod:`kiro_crew.agent_sdk.unresolved_tool_answers`.
* Only a ``tool_call`` frame with no server and no tool identity, kind ``other``:
  how those harnesses frame a call they could not resolve, with the name the model
  called as its wire title. A shell call is kind ``execute``, a resolved MCP call
  names its server.
* Only a result with that harness's status whose output IS its could-not-resolve
  answer, naming the same worker tool as the frame. OpenCode's answer goes on to
  list every tool the session has, so for OpenCode the output's head must be it.
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

from kiro_crew.agent_sdk.kas_tool_names import kas_mcp_tool_id
from kiro_crew.agent_sdk.unresolved_tool_answers import (
    UnresolvedToolAnswer,
    unresolved_tool_answer,
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

#: The tool kind KAS, pi and OpenCode give a call they could not resolve.
_UNRESOLVED_KIND = "other"

#: The longest output read: a whole answer, or the head of OpenCode's. Every
#: answer's fixed text plus a :data:`_MAX_TITLE_LEN` name fits.
_MAX_ANSWER_LEN = 1024

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

#: How a worker tool may be spelled in the name the harness echoes: bare, or one of
#: the qualified forms a model might have tried (pi's gate extension spells MCP
#: tools ``mcp__<server with _>__<tool>``). Matched against the whole name, so a
#: tool on another server that merely ends in ``work_report`` is not a worker tool.
_QUALIFIED = re.compile(
    r"^(?:@kirocrew-work/|mcp__kirocrew-work__|mcp__kirocrew_work__|kirocrew-work::|"
    r"kirocrew-work_|" + re.escape(kas_mcp_tool_id(WORK_SERVER, "")) + r")?(?P<tool>[a-z_]+)$"
)


def _worker_tool(name: str | None) -> str | None:
    named = _QUALIFIED.match((name or "").strip().lower())
    if named and named.group("tool") in _WORKER_TOOLS:
        return named.group("tool")
    return None


def undelivered_work_tool(
    *,
    answer: UnresolvedToolAnswer,
    wire_title: str | None,
    kind: str | None,
    status: str | None,
    output: str | None,
) -> str | None:
    """The worker tool an unresolved call named, or ``None``.

    *answer* is how the serving harness answers a call it could not resolve.
    *wire_title* and *kind* come from the call's own ``tool_call`` frame (one that
    named no server and no tool); *status* and *output* from its result. All must
    agree: the call ended with the answer's status, its output IS the answer (or, for
    an answer that is only a head, starts with it), and that answer and the frame name
    the same worker tool. A shell command printing the same sentence is an
    ``execute`` frame, and a call the server refused (a ``done`` with no evidence)
    reached the ledger and came back with its own answer.
    """
    if status != answer.status or kind != _UNRESOLVED_KIND or not output:
        return None
    if answer.whole and len(output) > _MAX_ANSWER_LEN:
        return None
    text = output[:_MAX_ANSWER_LEN].strip()
    if text.startswith('"'):
        # ``rawOutput`` can arrive as a JSON string rather than bare text.
        try:
            decoded = json.loads(text)
        except ValueError:
            return None
        text = decoded.strip() if isinstance(decoded, str) else ""
    match = answer.pattern.fullmatch(text) if answer.whole else answer.pattern.match(text)
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


def _answer_of(provider: object) -> UnresolvedToolAnswer | None:
    """How *provider*'s harness answers an unresolved call, by its own provider
    label; ``None`` for a harness whose answer is not known."""
    from kiro_crew.agent_sdk import context_provider_of

    served = context_provider_of(provider)
    if served is None:
        return None
    try:
        return unresolved_tool_answer(served.context_provider_type)
    except Exception:  # noqa: BLE001 - an unreadable label is no known harness
        return None


class UndeliveredCallWatch:
    """One turn's watch for a worker-tool call the harness never delivered.

    Built per turn by the consumer that streams it. On a harness whose
    could-not-resolve answer is not known it records nothing.
    """

    def __init__(self, session_key: str, provider: object) -> None:
        self._session_key = session_key or ""
        self._answer = _answer_of(provider) if self._session_key else None
        self._active = self._answer is not None
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
        if named is None or status in ("", "pending", "in_progress") or self._answer is None:
            return None
        del self._frames[call_id]
        tool = undelivered_work_tool(
            answer=self._answer,
            wire_title=named,
            kind=_UNRESOLVED_KIND,
            status=status,
            output=getattr(event, "tool_output", "") or "",
        )
        if tool is None:
            return None
        return await record_undelivered_call(self._session_key, tool)
