"""A KAS native sub-agent's FINISHED tool call, named on its activity event.

A native KAS sub-agent's nested ``tool_call`` / ``tool_call_update`` frames run
through the shared parser for its cache side effects only, and the session
handle returns just one ``EVENT_SUBAGENT_ACTIVITY`` event per frame (the child's
card is fed by the roster and that activity, never by a top-level tool row). The
turn loops fire the session agent's spec ``postToolUse`` hooks on a terminal tool
result, which a child's call does not produce.

So on the FIRST terminal frame of a child call :func:`mark_child_call_finished`
copies, onto that frame's activity event, the existing
``AcpEvent`` fields a turn loop fires ``PostToolUse`` from: ``tool_final``,
``tool_status``, ``tool_output``, ``is_shell``, ``tool_name``,
``mcp_server_name`` and ``harness_tool_id``. Only on the first: a call reports
one outcome, so a repeated terminal frame of the same call is left unmarked.
No other frame of a child carries them, so a consumer that does not fire hooks
sees nothing new.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch_parts.tool_call_provenance import (
    TOOL_CALL_CACHE_MAX_ENTRIES,
    TOOL_CALL_CACHE_MAX_KEY_CHARS,
)
from kiro_crew.acp.types import EVENT_TOOL_RESULT, AcpEvent

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

logger = logging.getLogger(__name__)

#: How many child calls one session remembers per turn (a title each, and each
#: finished one, to mark it once): the bound every other per-call cache of the
#: handle keeps. Past it a new title is not kept, and the finished set is cleared
#: with a log line, which can let one repeated terminal frame of a forgotten call
#: be marked a second time.
_FINISHED_CAP = TOOL_CALL_CACHE_MAX_ENTRIES
#: The longest scoped key (session id, ``|``, tool-call id) a child call is
#: remembered by, as for the handle's other per-call caches; a longer one is not
#: marked at all.
_KEY_MAX_CHARS = TOOL_CALL_CACHE_MAX_KEY_CHARS
#: The longest title kept for a child call; the rest is cut.
_TITLE_MAX_CHARS = 512


def is_finished_child_call(event: Any) -> bool:
    """Whether *event* is a child's activity event :func:`mark_child_call_finished` marked."""
    from kiro_crew.agent_sdk.drivers.acp_vocab import TERMINAL_TOOL_STATUSES

    return (
        bool(getattr(event, "sub_session_id", ""))
        and bool(getattr(event, "tool_call_id", ""))
        and (
            getattr(event, "tool_final", False) is True
            or getattr(event, "tool_status", "") in TERMINAL_TOOL_STATUSES
        )
    )


def mark_child_call_finished(
    handle: AcpSessionHandle, activity: AcpEvent, parsed: list[AcpEvent]
) -> None:
    """Copy the finished call's fields from *parsed* onto *activity*, once per call.

    *parsed* is what the shared parser returned for the child's frame. Read
    before the handle releases the call's caches.
    """
    from kiro_crew.acp._dispatch import scoped_tool_cache_key
    from kiro_crew.agent_sdk.drivers.acp_vocab import TERMINAL_TOOL_STATUSES

    call_id = activity.tool_call_id
    if not call_id:
        return
    key = scoped_tool_cache_key(handle._session_id, call_id)
    if len(key) > _KEY_MAX_CHARS:
        return
    titles: dict[str, str] = handle._native_child_titles
    if activity.title:
        if key in titles or len(titles) < _FINISHED_CAP:
            titles[key] = activity.title[:_TITLE_MAX_CHARS]
    result = next(
        (
            ev
            for ev in parsed
            if ev.kind == EVENT_TOOL_RESULT
            and ev.tool_call_id == call_id
            and (ev.tool_final or ev.tool_status in TERMINAL_TOOL_STATUSES)
        ),
        None,
    )
    if result is None:
        return
    seen: set[str] = handle._native_child_finished
    if key in seen:
        return
    if len(seen) >= _FINISHED_CAP:
        logger.info("native-child finished set full (%d); cleared", len(seen))
        seen.clear()
    seen.add(key)
    activity.title = titles.pop(key, "") or activity.title[:_TITLE_MAX_CHARS]
    activity.tool_final = bool(result.tool_final)
    activity.tool_status = result.tool_status or ("completed" if result.tool_final else "")
    activity.tool_output = result.tool_output
    activity.is_shell = bool(result.is_shell or handle._tool_call_is_shell.get(key))
    activity.tool_name = result.tool_name or handle._tool_call_tool_name.get(key, "")
    activity.mcp_server_name = result.mcp_server_name or handle._tool_call_mcp_server.get(key, "")
    activity.harness_tool_id = result.harness_tool_id or handle._tool_call_harness_tool_name.get(
        key, ""
    )


__all__ = ["is_finished_child_call", "mark_child_call_finished"]
