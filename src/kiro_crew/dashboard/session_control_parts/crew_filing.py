"""Crew Mode filing: whether a child a session-control verb makes is kept out of folders.

A chat that turned Crew Mode on, and every session it dispatches, stays out of the
person's folders. ``session_create`` and ``session_fork`` ask the same question about
their caller -- is it in a turned-on Crew Mode tree, as the slot list stamps it
(``crew_mode``)? -- and answer the caller the same way when a folder it named was
dropped. Both read that rule here, so the two verbs cannot drift apart.
"""

from __future__ import annotations

import logging
from typing import Any

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


def _frame_parents(state: Any) -> dict[str, Any] | None:
    """Each live slot's ``parent`` as the slot frame resolves it, or ``None`` on a fault.

    The Crew Mode tree test reads it so an adoption edge and a closed creator's live
    ancestor count here as they count in the session list. A fault costs only those
    edges: the test then reads ``_created_by`` alone.
    """
    from kiro_crew.dashboard.state import _attach_slot_parents

    try:
        rows = state._lineage_rows()
        _attach_slot_parents(rows, getattr(state, "spend_slot_by_session", None))
    except Exception:
        logger.debug("slot parents unavailable for the Crew Mode tree test", exc_info=True)
        return None
    return {row["key"]: row.get("parent") for row in rows}


def _in_turned_on_crew_tree(state: Any, caller_key: str) -> bool:
    """Whether *caller_key* is in a tree whose root turned Crew Mode on.

    The rule the slot list stamps (``crew_mode.slot_in_turned_on_tree``), read over the
    slot frame's parents (:func:`_frame_parents`), so a child a Crew Mode caller makes
    is filed in no folder, whichever verb makes it.
    """
    from kiro_crew import crew_mode

    return crew_mode.slot_in_turned_on_tree(
        state._slots,
        caller_key,
        _frame_parents(state),
        settled=crew_mode.lineage_is_settled(),
    )


def _crew_filing_answer(*, folder_named: bool, in_crew_tree: bool) -> dict[str, Any]:
    """The reply keys that tell a caller where a Crew Mode child was filed.

    ``folder_ignored`` says the folder the caller named was dropped (``crew_mode``);
    ``crew_mode`` says the child belongs to a chat that turned Crew Mode on, so it is
    filed in no folder. A child outside such a tree gets neither key.
    """
    if not in_crew_tree:
        return {}
    return {**({"folder_ignored": "crew_mode"} if folder_named else {}), "crew_mode": True}
