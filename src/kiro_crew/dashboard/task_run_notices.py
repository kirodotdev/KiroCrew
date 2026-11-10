"""Task Runner notices that also reach the conversation a run was started from.

Every run notice goes to the bell (the gateway's ``_task_notify`` sink). A few of them
describe something the person who started the run from a chat cannot otherwise see in
that chat: a step's agent runtime died, or a gateway restart interrupted the run and
it was resumed from its checkpoint or left paused. Those are also appended to the
dashboard tab showing that conversation, as an assistant-role system notice row
(:data:`TASKRUNNER_NOTICE_KIND`), so they are not read as a turn. The run's origin key
is resolved here, at delivery time: when no open tab shows the conversation, nothing
is appended and the bell keeps the notice.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard.system_notices import TASKRUNNER_NOTICE_KIND

#: How the titles (lower case) of the run notices the originating conversation also
#: gets end: the executor's and the watchdog's runtime death notices, and the
#: adoption sweep's notices after a gateway restart.
_ORIGIN_TITLE_ENDINGS = (
    "process died",
    "not auto-resumed",
    "resumed after a gateway restart",
)


def reaches_origin(title: str) -> bool:
    """Whether a run notice titled *title* also goes to the run's conversation.

    Matched on how the title ends, never anywhere in it: ``task_reporter.notify``
    puts the run's ``[label]`` in front of the heading, so a run named, say,
    ``Investigate why the process died`` must not send its ordinary notices here.
    """
    return title.rstrip().lower().endswith(_ORIGIN_TITLE_ENDINGS)


def notice_text(title: str, body: str) -> str:
    """The row's text: the notice as the bell shows it, title then body."""
    return f"{title}\n{body}" if body else title


async def post_to_origin_tab(state: Any, session_key: str, text: str, task_id: str = "") -> bool:
    """Append *text* to the open dashboard tab showing *session_key*; whether it landed.

    *text* is already redacted by the sink. A key no tab shows is left alone.
    """
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key
    from kiro_crew.dashboard.state import load_window_off_loop

    slot_key = dashboard_slot_key(session_key)
    slot = state.get_slot(slot_key) if slot_key else None
    if slot is None:
        return False
    # A tab the restart restored but nobody opened yet still has its window on disk.
    await load_window_off_loop(slot)
    meta: dict[str, Any] = {"kind": TASKRUNNER_NOTICE_KIND}
    if task_id:
        meta["task_id"] = task_id
    slot.append("assistant", text, "msg msg-a", meta=meta)
    return True
