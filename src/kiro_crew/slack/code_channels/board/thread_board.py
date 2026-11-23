"""The refresh of a Conductor thread's board.

A code channel's board is refreshed by its workspace (``WorkspaceService``: a
folder, a worktree, a Code tab, a context bar). A Conductor thread has none of
those and needs none of the ``agents.*`` scopes they use, so this is the board
alone: read the conductor's own ledger (:func:`..board.read`), fill each worker's
PR and CI, publish the one message in the thread (:func:`..board.publish`).

The triggers are the package's own (:func:`..board.note_ledger_write`,
:func:`..board.note_session_turn`); they reach :func:`schedule` through
``board._schedule`` for a thread's room key. Refreshes of one thread are serial
and coalesced: one pass plus at most one queued rerun, behind a short debounce, so
a burst of worker reports costs one edit.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from kiro_crew.slack.code_channels.surface_store import room_key

logger = logging.getLogger(__name__)

#: ``room key -> the running refresh``, the rooms asked to run once more, and the
#: newest binding each trigger carried.
_tasks: dict[str, asyncio.Future[None]] = {}
_again: set[str] = set()
_latest: dict[str, Any] = {}


def schedule(binding: Any, *, delay: float | None = None) -> bool:
    """Refresh *binding*'s board soon (after the debounce); coalesces with one running.

    False when there is no running loop to run it on.
    """
    from kiro_crew.slack.code_channels.workspace.service import BOARD_DEBOUNCE_SECS

    room = room_key(binding)
    _latest[room] = binding
    if room in _tasks:
        _again.add(room)
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    wait = BOARD_DEBOUNCE_SECS if delay is None else delay

    async def _run() -> None:
        pause = wait
        try:
            while True:
                if pause > 0:
                    await asyncio.sleep(pause)
                _again.discard(room)
                try:
                    await refresh(_latest.get(room, binding))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("conductor thread %s: board refresh failed", room, exc_info=True)
                if room not in _again:
                    return
                pause = BOARD_DEBOUNCE_SECS
        finally:
            _tasks.pop(room, None)
            _latest.pop(room, None)
            _again.discard(room)

    _tasks[room] = loop.create_task(_run())
    return True


async def drain() -> None:
    """Wait for every scheduled refresh (shutdown, tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks.values()), return_exceptions=True)


def reset_for_tests() -> None:
    for task in _tasks.values():
        try:
            task.cancel()
        except RuntimeError:  # its loop is closed: nothing left to cancel
            pass
    _tasks.clear()
    _again.clear()
    _latest.clear()


async def refresh(binding: Any) -> None:
    """One pass: the board read, filled and published, only while it is still the thread's.

    The board's own pass (``surface.own_pass``), as a code channel's refresh is: only
    a Stop of the work that scheduled it, or one during its own awaits, leaves the
    board unsent, and one pass in a clean context publishes it then.
    """
    from kiro_crew.slack.code_channels import board as board_pkg
    from kiro_crew.slack.code_channels import surface

    room = room_key(binding)
    with surface.own_pass():
        if surface.channel_stopped(room):
            board_pkg.settle(room, unsent=True)
            return
        token = surface.admit()
        try:
            await _refresh(binding)
        finally:
            surface.release(token)


async def _refresh(binding: Any) -> None:
    from kiro_crew.slack.code_channels import board as board_pkg
    from kiro_crew.slack.code_channels import surface
    from kiro_crew.slack.code_channels.board import source
    from kiro_crew.slack.code_channels.store import store

    room = room_key(binding)
    # The binding as the store holds it now: an ended thread publishes nothing more.
    current = await asyncio.to_thread(
        store().get_thread_binding, binding.channel_id, binding.anchor_ts
    )
    if current is None or current.ended_at:
        board_pkg.forget_channel(room)
        board_pkg.settle(room, unsent=False)
        return
    state = surface.dashboard_state()
    snapshot = read = await board_pkg.read(current)
    # A read the scope refused only in passing (session map busy, room not freshly
    # read) leaves the board owed: re-armed under settle's backoff.
    passing = False
    if read is None:
        try:
            with surface.own_pass():
                passing = source.scope_refusal(state, current) == source.SCOPE_PASSING
        except Exception:
            passing = False
    if snapshot is not None:
        try:
            snapshot = await _fill_prs(state, current, snapshot)
        except Exception:
            # The forge reads fail alone: the rows go out as the workers reported them.
            logger.warning("conductor thread %s: worker PRs unread", room, exc_info=True)
    unsent = False
    try:
        done = await board_pkg.publish(current, snapshot)
        unsent = read is not None and done in board_pkg.UNSENT
    except Exception:
        logger.warning("conductor thread %s: board publish failed", room, exc_info=True)
        unsent = read is not None
    board_pkg.settle(room, unsent=unsent or passing)


async def _fill_prs(state: Any, binding: Any, snapshot: Any) -> Any:
    """Each worker's PR and CI, read as a code channel's are, from the conductor's project.

    A thread has no worktree: the checkout is the conductor session's own project,
    and a row keeps the PR number its worker reported when that project is no
    GitHub checkout ``gh`` can read (no CI then). Reuses the workspace service's
    cached, forge-confirmed lookups; with no service the rows are left as read.
    shortcut: a worker working in ANOTHER repository is told apart by its session's
    project folder only, as a code channel's board does.
    """
    from kiro_crew.slack.code_channels.board import source
    from kiro_crew.slack.code_channels.workspace import core as workspace

    service = workspace.service()
    slot = source.find_slot(state, str(binding.session_key or ""))
    project = str(getattr(slot, "project", "") or "") if slot is not None else ""
    if service is None or not project:
        return snapshot
    checkout: Any = SimpleNamespace(worktree="", repo_root=project)
    return await service._fill_worker_prs(checkout, snapshot)
