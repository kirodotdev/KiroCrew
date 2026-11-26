"""Which Bots have a dashboard turn running now, by member id.

``GET /v1/crew/bots`` reads it to say ``busy``. A turn is noted with the execution
its admission settled on (``_turn_admit``), so the member id is the one the turn
runs as: not the label a slot was picked under, and not a name looked up again.
The turn's own ``finally`` in ``_run_chat`` takes it out.

Only a turn that runs as the Bot itself is noted: an execution selected as a
member that holds a member id. A helper that carries a Bot's member id under a
template (a Bot's worker session) is not the Bot's own turn.

Every call runs on the event loop, so the table needs no lock. Neither ``begin``
nor ``end`` raises: a fault here must not end or break a turn.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ``id(turn)`` -> (the turn, the data home, the member id). The entry holds the turn
#: itself, so its id cannot be given to another object while the entry is here.
_RUNNING: dict[int, tuple[Any, str, str]] = {}


def _home() -> str:
    from kiro_crew.config.paths import data_home

    return str(data_home())


def begin(turn: Any, execution: Any) -> None:
    """Note that *turn* runs as the Bot *execution* names, when it names one."""
    try:
        if execution is None or execution.selection_kind != "member":
            return
        member_id = execution.member_id
        if not isinstance(member_id, str) or not member_id:
            return
        _RUNNING[id(turn)] = (turn, _home(), member_id)
    except Exception:
        logger.warning("could not note a running Bot turn", exc_info=True)


def end(turn: Any) -> None:
    """Take *turn* out. A turn that was never noted, or is already out, is left alone."""
    try:
        entry = _RUNNING.get(id(turn))
        if entry is not None and entry[0] is turn:
            del _RUNNING[id(turn)]
    except Exception:
        logger.warning("could not clear a running Bot turn", exc_info=True)


def running_member_ids() -> frozenset[str]:
    """The member ids with a noted turn in this data home. Empty when it cannot be read."""
    try:
        home = _home()
        return frozenset(member_id for _, at, member_id in _RUNNING.values() if at == home)
    except Exception:
        logger.warning("could not read the running Bot turns", exc_info=True)
        return frozenset()
