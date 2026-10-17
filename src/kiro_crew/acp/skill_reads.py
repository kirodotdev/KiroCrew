"""Per-session skill-read credit, shared by both ACP transports.

A skill body the model reads directly -- a file-read tool, or ``cat`` in a
shell -- bypasses the skills loader, so the usage ledger learns of it only from
the tool call itself. The ACP layer is the one place that sees every surface's
tool calls (dashboard, Slack, subagents, task runner), but a session reaches
that layer by one of two transports: ``AcpClient`` (one process per session)
or ``AcpSessionHandle`` (one session on the shared ``AcpRuntime`` process).
The ledger must not know which, so both own one :class:`SkillReadTracker` and
feed it the same events; the credit a backend earns cannot then depend on the
transport that serves it.

Two phases on purpose. :meth:`SkillReadTracker.note` resolves which served
skills a tool call is about to deliver and holds the keys; it records nothing.
:meth:`SkillReadTracker.credit` records them once the call's completed result
arrives, so a read that was denied, errored, or never ran leaves no delivery.

The per-surface permission gate (``HookManager.on_tool_call``) cannot do this:
file reads are auto-approved, so they never reach it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING

from kiro_crew.skill_usage import get_global_skill_read_observer, names_skill_file

if TYPE_CHECKING:
    from kiro_crew.acp.types import AcpEvent

logger = logging.getLogger(__name__)

#: Distinct tool calls one tracker remembers before dropping its bookkeeping
#: wholesale. A single turn cannot legitimately hold this many skill reads, so
#: reaching the cap means completions never arrived (a killed process, an
#: abandoned turn); the worst case after a reset is one duplicate credit, never
#: a set that grows for the life of the session.
_MAX_NOTED_SKILL_READS = 512


def _call_key(tool_call_id: str) -> str:
    """The fixed-size key a call is tracked under.

    ``toolCallId`` is a backend-authored string with no length bound, and the
    tracker retains it for the life of the call, so storing the text would
    leave each row unbounded under a bounded count. A SHA-256 digest is 64
    characters whatever the id was; ``note`` and ``credit`` both key by it, so
    the completion frame finds the row its call wrote.
    """
    return hashlib.sha256(tool_call_id.encode("utf-8", "surrogatepass")).hexdigest()


def _tracked_id(tool_call_id: object) -> str:
    """The call id an event carries, or ``""`` when it is not a string.

    The frame's ``toolCallId`` is backend-authored and reaches the result event
    unconverted, so a non-string value is treated as no id -- the same reading
    the shared parser gives a ``tool_call`` frame -- rather than raised on, since
    this runs inline in the dispatch loop and must never abort a turn.
    """
    return tool_call_id if isinstance(tool_call_id, str) else ""


class SkillReadTracker:
    """Note a tool call's skill reads, credit them when the call completes.

    One per session. ``noted`` holds the digests of the ``tool_call_id``s
    already resolved: the arguments arrive on the initial ``tool_call`` for
    some providers and only on its ``tool_call_update`` refinement for others
    (claude-agent-acp leaves ``rawInput`` empty on the initial notification),
    so both are observed and the set keeps one read from being credited
    twice. ``pending`` maps a noted call's digest to the served skill keys its
    completion will credit -- names the loader resolved from the skills on
    disk, not backend text. Both are bounded in count by
    ``_MAX_NOTED_SKILL_READS`` and in key size by the digest.
    """

    def __init__(self) -> None:
        self.noted: set[str] = set()
        self.pending: dict[str, list[str]] = {}

    def clear(self) -> None:
        """Forget every call: the owning session starts a new turn."""
        self.noted.clear()
        self.pending.clear()

    async def note(self, tool_event: AcpEvent) -> None:
        """Resolve which skills *tool_event* is about to read, crediting later.

        Resolution is filesystem-bound (a skills-tree walk after cache expiry,
        plus a ``resolve()`` per served skill), so it runs on a thread; on the
        event loop it would stall every session in the gateway. Gated on the
        skill basename appearing in the arguments BEFORE the offload, so a tool
        call unrelated to skills costs one substring scan. Whether the call is a
        content-delivering READ (rather than a delete, move, or grep that merely
        names the path) is the observer's decision. Failures are swallowed:
        telemetry must not disturb the tool call.
        """
        observer = get_global_skill_read_observer()
        if observer is None:
            return
        raw_id = _tracked_id(tool_event.tool_call_id)
        tool_id = _call_key(raw_id) if raw_id else ""
        if tool_id and tool_id in self.noted:
            return
        raw_params = tool_event.raw_tool_params
        command = tool_event.shell_command
        if not names_skill_file(raw_params, command):
            return
        if tool_id:
            if len(self.noted) >= _MAX_NOTED_SKILL_READS:
                self.clear()
            self.noted.add(tool_id)
        try:
            keys = await asyncio.to_thread(
                observer.resolve_tool_read_keys,
                tool_event.tool_name or "",
                raw_params,
                command,
            )
        except Exception:
            logger.warning("skill-read resolution failed", exc_info=True)
            return
        if keys and tool_id:
            self.pending[tool_id] = keys

    def credit(self, tool_result_event: AcpEvent) -> None:
        """Credit the reads noted for a call whose result has now arrived.

        Only a ``status == "completed"`` result (``tool_final``) credits, so a
        read that was denied, errored, or never ran contributes no delivery.
        In-memory only -- the ledger debounces its own disk write -- so this is
        safe to run inline on the event loop.
        """
        if not tool_result_event.tool_final:
            return
        tool_id = _tracked_id(tool_result_event.tool_call_id)
        keys = self.pending.pop(_call_key(tool_id), None) if tool_id else None
        if not keys:
            return
        observer = get_global_skill_read_observer()
        if observer is None:
            return
        try:
            observer.credit_skill_reads(keys)
        except Exception:
            logger.warning("skill-read credit failed", exc_info=True)
