"""The memory mode a fork's child is born with: its parent's, or a stricter one.

A transcript belongs to the memory boundary it was written in, so a fork may keep
that boundary or narrow it, never widen it. Narrowing serves a side thread: a fork
of a persistent chat made incognito or temporary keeps the context it was forked
with, and nothing it then does feeds memory, lessons or consolidation.

Strictness is the order of ``execution_context.MEMORY_MODES`` (persistent, then
incognito, then temporary), read through ``stricter_memory_mode``, so this rule and
the execution record's own ratchet (``ExecutionContext.with_mode``) cannot disagree.
"Keep this chat" (``chat_fork.api_chat_slot_promote``) is the one route that loosens
a mode; it sends no ``memory_mode`` and never reaches this rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.execution_context import MEMORY_MODES, stricter_memory_mode
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_fork import ForkSource


def fork_child_memory_mode(
    source: "ForkSource", requested: object, audit_caller: str, audit_operation: str
) -> "str | web.Response":
    """The child's mode, or the finished refusal for a mode it may not have.

    ``requested`` is the caller's ``memory_mode`` as it arrived; ``None`` keeps the
    parent's. The parent's mode is the one its frozen execution carries
    (``source.execution``), which is what an ordinary fork binds the child to. It is
    applied inside ``chat_fork.fork_slot``, before the transcript is read, so a
    refused mode costs no snapshot and leaves no child. Only the dashboard fork route
    (``POST /api/chat/slots/{slot}/fork``) sends the field; the agent ``session_fork``
    route sends none, so its child keeps the persistent source's mode.
    """
    inherited = source.execution.memory_mode
    if requested is None:
        return inherited
    if not isinstance(requested, str) or requested not in MEMORY_MODES:
        return web.json_response(
            {
                "error": f"memory_mode must be one of {list(MEMORY_MODES)}",
                "code": "invalid_memory_mode",
            },
            status=400,
        )
    if stricter_memory_mode(inherited, requested) != requested:
        sel().log_api_access(
            caller=audit_caller,
            operation=audit_operation,
            outcome="denied",
            source="dashboard",
            resources=f"slot={source.slot.key},memory_mode={inherited},requested={requested}",
            error="a fork may not be less private than its source",
        )
        return web.json_response(
            {
                "error": (
                    f"a fork of a {inherited} session cannot be {requested}: it keeps "
                    "the source's memory mode or a more private one"
                ),
                "code": "fork_memory_mode_looser",
            },
            status=409,
        )
    return requested
