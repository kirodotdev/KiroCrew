"""The work board's cache and lineage reads, for :class:`~.service.WorkspaceService`.

A mixin, kept in its own module so the refresh owner stays under the file cap.
Its state (``_boards``, ``_board_owner``, ``_board_refreshed``, ``_conductor_of``,
``_tasks``), its clock and its board reader are the service's own, set in the
service's ``__init__``; nothing here holds state of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.code_channels.board.model import BoardSnapshot
    from kiro_crew.slack.code_channels.workspace.service import BoardReader

logger = logging.getLogger(__name__)


class BoardCache:
    """The cached board per channel, owned by the conductor it was read for."""

    if TYPE_CHECKING:
        _boards: dict[str, BoardSnapshot]
        _board_owner: dict[str, str]
        _board_refreshed: dict[str, float]
        _conductor_of: dict[str, str]
        _tasks: dict[str, asyncio.Future[None]]
        _clock: Callable[[], float]
        _read_board: BoardReader | None

        def _forget_board(self, key: str, channel_id: str, *, hold: bool = True) -> bool: ...

    def _lineage_of(self, state: Any, record: WorkspaceRecord) -> dict[str, tuple[bool, int]]:
        """``slot key -> (turn running, generation)`` for the bound session's own workers.

        The store's worker records (written at each worker's birth, so they hold
        across a restart) and the sessions the channel's last board names. A key
        with no live slot is left out, so a closed worker reads as a turn ended.
        """
        from kiro_crew.slack.code_channels.workspace.service import _find_slot

        keys: set[str] = set()
        if record.session_key:
            try:
                from kiro_crew.slack.code_channels.store import store

                keys.update(store().workers_of(record.session_key))
            except Exception:
                logger.debug("code channel %s: worker lineage unreadable", record.channel_id)
        board = self._cached_board(record.key)
        if board is not None:
            keys.update(row.session_key for row in board.rows if row.session_key)
        keys.discard(record.session_key)
        out: dict[str, tuple[bool, int]] = {}
        for worker in sorted(keys):
            slot = _find_slot(state, worker)
            if slot is None:
                continue
            generation = getattr(slot, "_turn_generation", 0)
            out[worker] = (
                bool(getattr(slot, "turn_running", False)),
                generation if isinstance(generation, int) else 0,
            )
        return out

    def _board_due(self, key: str, workers: dict[str, tuple[bool, int]]) -> bool:
        """The backstop: a live board not refreshed for :data:`BOARD_BACKSTOP_SECS`.

        Live means a worker of the channel is running or a row is working or
        waiting; an idle or finished board is left alone.
        """
        from kiro_crew.slack.code_channels.board.model import ACTIVE_STATES
        from kiro_crew.slack.code_channels.workspace.service import (
            BOARD_BACKSTOP_SECS,
            _board_refused_permanently,
        )

        board = self._cached_board(key)
        if board is None:
            return False
        live = any(run for run, _gen in workers.values()) or any(
            row.state in ACTIVE_STATES for row in board.rows
        )
        if not live or key in self._tasks:
            return False
        if _board_refused_permanently(key.split(":", 1)[-1]):
            # Slack refused the board for good (not in channel, archived ...):
            # the backstop does not re-try it every minute; a trigger still does.
            return False
        last = self._board_refreshed.get(key)
        return last is None or self._clock() - last >= BOARD_BACKSTOP_SECS

    async def _has_board(self, record: WorkspaceRecord) -> bool:
        """Whether the channel's surface record names a board message (posted before)."""
        if self._read_board is None:
            return False
        try:
            from kiro_crew.slack.code_channels.surface_store import get_async

            surface = await get_async(record.channel_id)
        except Exception:
            return False
        return bool(surface is not None and str(getattr(surface, "board_ts", None) or ""))

    def board(self, team_id: str, channel_id: str, session_key: str = "") -> BoardSnapshot | None:
        """The channel's last published board (PR-filled), or ``None``.

        With *session_key* (the caller's binding's conductor), a board cached
        under another conductor is not returned: a replaced conductor's workers
        are not the new one's.
        """
        key = f"{team_id}:{channel_id}"
        board = self._cached_board(key)
        owner = self._board_owner.get(key, "")
        if board is not None and session_key and owner and owner != session_key:
            return None
        return board

    def _cached_board(self, key: str) -> BoardSnapshot | None:
        """The cached board, unless it was read for another conductor (then forgotten).

        Every read of the cache goes through here: the channel's conductor is
        the one :attr:`_conductor_of` names (the watch and each refresh set it
        from the binding), and a board cached while another session was the
        conductor describes that session's workers, not this one's.
        """
        board = self._boards.get(key)
        if board is None:
            return None
        owner = self._board_owner.get(key, "")
        conductor = self._conductor_of.get(key, "")
        if owner and conductor and owner != conductor:
            # Not a sharing doubt: no status hold is armed for the old workers.
            self._forget_board(key, key.split(":", 1)[-1], hold=False)
            return None
        return board

    def _note_conductor(self, binding: Any) -> None:
        """Record *binding*'s session as its channel's conductor, then drop a stale board.

        Called by every path that reads the cache for a binding (the watch, each
        refresh, every bar and diff publish), so a conductor replaced before the
        next watch pass never sees its predecessor's workers.
        """
        session = str(getattr(binding, "session_key", "") or "")
        team = str(getattr(binding, "team_id", "") or "")
        channel = str(getattr(binding, "channel_id", "") or "")
        if not session or not channel:
            return
        key = f"{team}:{channel}"
        self._conductor_of[key] = session
        self._cached_board(key)
