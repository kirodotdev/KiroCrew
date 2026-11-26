"""Which Bots have a turn running now, by member id.

``GET /v1/crew/bots`` reads it to say ``busy``. A turn is noted with the execution
it is bound to, so the member id is the one the turn runs as: not the label a slot
was picked under, and not a name looked up again. Three kinds of turn note
themselves, each once its execution is settled, and each takes its note out first
thing in its own ``finally``: a dashboard turn (``_turn_admit`` and ``_run_chat``),
a scheduled run of one agent (``_cron_run_single``) and a webhook turn that runs
the registered agent (``_run_hook_inner`` and ``_run_hook_agent``).

Only a turn that runs as the Bot itself is noted: an execution selected as a
member that holds a member id. A helper that carries a Bot's member id under a
template (a Bot's worker session) is not the Bot's own turn. One turn under a
template is: a Bot's own scheduled run, which a Bot that owns its agent file runs
on its worker (``begin_scheduled``).

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


def begin_scheduled(turn: Any, member_id: Any) -> None:
    """Note that the scheduled run *turn* is the Bot *member_id*'s own, when it names one.

    For the scheduled run alone: *member_id* is the one its fire captured from a
    member selection. A Bot that owns its agent file runs its schedule on its
    worker, under a template, and that run is still the Bot's own job. No other
    turn on a worker is.
    """
    try:
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
