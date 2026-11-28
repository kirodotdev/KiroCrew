"""A per-pass memo of which ancestors are links, for ``first_linked_ancestor``.

A bulk caller that screens many paths under shared folders in one pass (the skills
listing's agent annotation, which runs the Windows link screen once per glob) opens
:func:`ancestor_link_memo`; inside it ``platform_compat.first_linked_ancestor`` tests
each distinct ancestor once instead of once per path. The walk stays root-first and
stops at the first link, so a link at a shared ancestor is still reported for every
path beneath it.

An ancestor that becomes a link during the block is not re-tested by later calls in
it. Nothing outlives the block, so the next one tests it again.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

#: The ancestor answers kept, set only inside :func:`ancestor_link_memo`. A context
#: variable, so a thread or task that does not run in the block's context never sees
#: it. A task or ``asyncio.to_thread`` call started inside the block inherits that
#: context and shares the dict, so open the block only around synchronous work.
_MEMO: contextvars.ContextVar[dict[str, bool] | None] = contextvars.ContextVar(
    "kiro_crew_ancestor_link_memo", default=None
)


def active_memo() -> dict[str, bool] | None:
    """The open block's ``{ancestor: is_link}`` answers, or None outside one."""
    return _MEMO.get()


@contextlib.contextmanager
def ancestor_link_memo() -> Iterator[None]:
    """Keep ``first_linked_ancestor``'s per-ancestor answers for this block.

    A nested block shares the outer one's answers.
    """
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)
