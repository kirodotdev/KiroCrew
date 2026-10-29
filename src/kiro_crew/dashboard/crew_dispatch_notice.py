"""The one-per-turn notice a Crew Mode conductor's chat gets when it dispatches.

In Crew Mode the conductor starts a crew member with ``session_create``. A
member is not a row of the person's session list -- it is listed in the **Crew**
(Workers) tab of the right-side panel -- so after a folded tool line the person
has no way to know a member exists or where to look. This writes ONE notice row
into the conductor's own chat, naming that tab and carrying an "Open Crew tab"
button, so the dispatch is visible where it happened.

It is a PRODUCT notice, written by the gateway, not model text: it always
appears, independent of what the conductor chose to say. The row's English
``content`` is only a fallback for channel mirrors and SSE readers; the
dashboard keys its localized copy and the button on ``meta["kind"]``
(:data:`CREW_DISPATCH_NOTICE_KIND`), the same way the transient-5xx notices key
on ``meta["notice"]`` (``chat_utils.TRANSIENT_NOTICE_*`` /
``website/src/pages/chat/transientNotice.ts``). ``test/test_crew_dispatch_notice_parity.py``
pins the kind to the frontend's twin.

Who gets it, and exactly once:

* Only a **Crew Mode root** (the conductor, a root session with no creator and a
  conductor template) whose child lands in a turned-on Crew Mode tree. A crew
  member's OWN sub-dispatch is a worker, not a root, so it writes nothing -- the
  gate reads ``slot_is_crew_mode_root`` on the caller, which is False for a
  worker.
* Only a **successful** dispatch: the route calls this after the create has
  committed (a refused or dry-run create never reaches here), so a failed
  dispatch writes nothing.
* **Once per person turn**: the dispatch reaches the create route while the
  conductor's turn task is in flight, and the slot bumps ``_turn_generation``
  once per turn. The first dispatch of a turn stamps
  ``_crew_dispatch_notice_turn_gen`` and writes; the rest of that turn's
  dispatches see the generation already stamped and write nothing. N dispatches
  in one turn -> one notice.

A plain (non-Crew-Mode) chat is not in a turned-on tree, so ``in_crew_tree`` is
False and nothing is written there either.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew import crew_mode

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.crew_dispatch_notice")

#: The ``meta["kind"]`` wire token the frontend matches to render this row as a
#: localized card with an "Open Crew tab" button
#: (``website/src/pages/chat/CrewDispatchNoticeCard.tsx``). A WIRE VALUE, not
#: copy -- never translate it, and keep it byte-identical to the frontend twin
#: (pinned by ``test/test_crew_dispatch_notice_parity.py``). Deliberately NOT a
#: member of ``system_notices.SYSTEM_NOTICE_KINDS``: that set marks rows the
#: "last real message" scans must SKIP, and this notice is meaningful content
#: the person is meant to read.
CREW_DISPATCH_NOTICE_KIND = "crew_dispatch_notice"

#: Plain-English fallback shown only to readers that cannot localize (channel
#: mirrors, SSE). The dashboard replaces it from the i18n catalog keyed on the
#: kind, so this wording and the catalog wording may differ without either side
#: falling back to raw English. Kept free of internal vocabulary.
CREW_DISPATCH_NOTICE_TEXT = (
    "A crew member was started. Crew members are listed in the Crew tab of the "
    "side panel, not the session list."
)


def _caller_is_turned_on_crew_root(caller_slot: "_ChatSlot", result: dict[str, Any]) -> bool:
    """Whether *caller_slot* is the Crew Mode conductor that just dispatched.

    ``result["crew_mode"]`` is True iff the child landed in a turned-on Crew
    Mode tree (``create_session``); ``slot_is_crew_mode_root`` is True ONLY for
    the root conductor, never a worker -- so a crew member's own sub-dispatch,
    whose caller is a worker, is False and writes no notice.
    """
    return bool(result.get("crew_mode")) and crew_mode.slot_is_crew_mode_root(caller_slot)


def maybe_write_dispatch_notice(
    state: "DashboardState", caller_slot: "_ChatSlot", result: dict[str, Any]
) -> bool:
    """Write the one-per-turn Crew Mode dispatch notice, if this dispatch earns it.

    Called from the session-control create route after a successful, non-dry-run
    create. Returns True when a notice was written (the first qualifying
    dispatch of the conductor's current turn), False otherwise. Never raises:
    the create has already committed, and a notice failure must not fail the
    dispatch -- any error is logged and swallowed.
    """
    try:
        if not _caller_is_turned_on_crew_root(caller_slot, result):
            return False
        generation = int(getattr(caller_slot, "_turn_generation", 0) or 0)
        if getattr(caller_slot, "_crew_dispatch_notice_turn_gen", -1) == generation:
            # Another dispatch in the SAME person turn already wrote the notice.
            return False
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(
            state,
            caller_slot,
            "notice",
            CREW_DISPATCH_NOTICE_TEXT,
            "msg msg-info",
            meta={"kind": CREW_DISPATCH_NOTICE_KIND},
        )
        caller_slot._crew_dispatch_notice_turn_gen = generation
        return True
    except Exception:
        logger.debug("crew-dispatch notice write failed", exc_info=True)
        return False
