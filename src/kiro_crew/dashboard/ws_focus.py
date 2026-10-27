"""The ``slot_focused`` resume prefetch, split out of ``dashboard/ws.py`` (its import path).

A focus frame on a persisted-but-idle slot starts the speculative ``session/load``.
Before that spawn the owner-only path binds the session to the owner as the user its
persisted MCP sign-ins belong to (``dashboard/mcp_grant_binding.py``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.mcp_grant_binding import OWNER_PRINCIPAL, abind_slot_to_principal
from kiro_crew.mcp_grant_principals import principal_for

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


#: Off-loop focus binds in flight, held so they are not collected mid-run.
_pending_binds: set[asyncio.Future] = set()


def _handle_slot_focused(
    state: DashboardState,
    slot_key: object,
    prev_task: "asyncio.Task | None",
    *,
    owner: bool,
) -> "asyncio.Task | None":
    """React to a client's ``slot_focused`` frame with a resume prefetch.

    Owner-only: an app-scoped socket is allowed on ``/api/ws`` for its own
    event streams, but a prefetch starts (or lets it cancel) owner-session
    processes and takes kiro-cli's native per-session lock — a permission
    boundary an app token does not cross. Non-owner frames are ignored
    entirely, including the cancel: ``prev_task`` can only be non-None for a
    socket that was owner when it armed one.

    Focusing a slot whose session is persisted but not live starts the
    speculative ``session/load`` (resume prefetch), overlapping the
    multi-second transcript replay with the user reading that history in the
    UI. ``prev_task`` is the prefetch THIS socket's previous focus armed;
    it is cancelled on every focus change so rapid tab flipping settles into
    at most one pending prefetch per connection (a repeat frame for the slot
    whose prefetch is still pending keeps it) — only the task this path
    created is touched, never one armed by the slot-create/project-set
    intent signals. ``slot_key`` of ``None``/empty means blur (tab hidden,
    no focused slot): cancel and do nothing else.

    Returns the task now pending for this socket, if any.
    """
    # circular import: ws -> chat_runner -> handlers/__init__ -> handlers/side -> ws
    from kiro_crew.dashboard.chat_runner import schedule_eager_spawn

    if not owner:
        return prev_task
    slot = state.get_slot(slot_key) if isinstance(slot_key, str) and slot_key else None
    if (
        slot is not None
        and prev_task is not None
        and not prev_task.done()
        and getattr(slot, "_eager_spawn_task", None) is prev_task
    ):
        # The same slot announced again (window focus, tab return, the
        # composer's compose intent) while its prefetch is still pending:
        # keep that load rather than restart it from the debounce.
        return prev_task
    if prev_task is not None and not prev_task.done():
        prev_task.cancel()
    if not isinstance(slot_key, str) or not slot_key:
        return None  # blur
    if slot is None or slot.running:
        return None
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return None
    session_key = effective_session_key(slot)
    if sessions.has_session(session_key):
        return None  # already live (in-memory check) — nothing to prefetch
    # Loop-safe resumability HINT (in-memory membership, no disk, no pruning —
    # the pruning ``resumable_sid`` lookup stays inside the spawn task's
    # get_or_create resume path). Checked HERE so a non-resumable slot never
    # reaches schedule_eager_spawn: creating a slot focuses it, and the focus
    # frame arriving behind the create signal would otherwise CANCEL the
    # create-armed fresh spawn (schedule_eager_spawn keeps one task per slot)
    # and then no-op — silently gutting the fresh eager-spawn path for every
    # new slot. Non-resumable focus preserves whatever task is pending.
    if not sessions.resumable_hint(session_key):
        return None
    # Owner-gated above: the owner is the user its persisted MCP sign-ins belong
    # to. The bind reads the session map, so it runs off the loop ahead of the
    # debounced spawn; a spawn that wins the race only persists nothing.
    if principal_for(session_key) != OWNER_PRINCIPAL:
        _pending_binds.add(
            task := asyncio.ensure_future(abind_slot_to_principal(slot, OWNER_PRINCIPAL, sessions))
        )
        task.add_done_callback(_pending_binds.discard)
    return schedule_eager_spawn(state, slot, allow_resume=True)
