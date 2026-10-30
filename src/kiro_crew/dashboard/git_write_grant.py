"""The per-session allow for git history writes (``hooks.confirm_git_writes``).

With the setting on (the default), a dashboard shell call that writes git
history asks the person even where session trust, YOLO, a native crew or a
pattern grant would approve it (``chat_turn/tool_approval.py``). This module owns
the one way a session stops asking: :func:`allow_git_writes`, called by

* the slot-approve route, when the person picks "Allow git writes in this
  session" on a git-write card (``by="person"``), and
* ``session_create`` with ``allow_git_writes=True``, when a dispatching session
  opens a worker that has to commit as part of its task
  (``by="session_create:<caller slot>"``).

Each grant is written to the SEL and projected on the slot
(``git_writes_allowed``); a card the person answered also keeps ``git_writes``
as its resolved decision. It is held in memory only, like
every other approval grant, so a restart returns the session to asking. Setting
the session's approval mode to Normal ends it (``slot_approval.apply_approval_mode``).
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: ``_git_writes_by`` for a grant the person made on a card.
BY_PERSON = "person"

#: Prefix of ``_git_writes_by`` for a grant ``session_create`` made.
BY_SESSION_CREATE = "session_create:"


def git_writes_allowed(slot: Any) -> bool:
    """Whether ``slot`` holds the per-session allow."""
    return bool(getattr(slot, "_git_writes_allowed", False))


def allow_git_writes(slot: Any, *, by: str, caller: str) -> None:
    """Allow git history writes in ``slot`` without a card, and record it.

    ``by`` is stored on the slot (``person`` or ``session_create:<key>``);
    ``caller`` is the SEL caller. Nothing is appended to the transcript: the
    card the person answered keeps ``git_writes`` as its resolved decision, and a
    ``session_create`` child must start empty, so its create audit carries the
    fact instead. Idempotent: a second grant to a slot that holds one writes
    nothing.
    """
    if git_writes_allowed(slot):
        return
    slot._git_writes_allowed = True
    slot._git_writes_by = _keep_create_mark(slot, by)
    try:
        sel().log_api_access(
            caller=caller,
            operation="git_writes:allow",
            outcome="enabled",
            resources=f"slot={getattr(slot, 'key', '')} by={by}",
        )
    except Exception:
        logger.warning("SEL audit failed for the git-writes allow", exc_info=True)


def _keep_create_mark(slot: Any, new: str) -> str:
    """``_git_writes_by`` after a change: a session opened with ``allow_git_writes``
    keeps that mark for its whole life, so ending the allow or granting it again
    on a card never lets it pass the allow on to a child it opens."""
    current = str(getattr(slot, "_git_writes_by", "") or "")
    return current if current.startswith(BY_SESSION_CREATE) else new


def end_git_writes(slot: Any, *, caller: str) -> None:
    """End the per-session allow on ``slot``, if it holds one, and record it."""
    if not git_writes_allowed(slot):
        return
    slot._git_writes_allowed = False
    slot._git_writes_by = _keep_create_mark(slot, "")
    try:
        sel().log_api_access(
            caller=caller,
            operation="git_writes:end",
            outcome="disabled",
            resources=f"slot={getattr(slot, 'key', '')}",
        )
    except Exception:
        logger.warning("SEL audit failed for ending the git-writes allow", exc_info=True)
