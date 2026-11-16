"""The channel's Slack status while only its workers move, for :class:`~.service.WorkspaceService`.

A mixin, kept in its own module so the refresh owner stays under the file cap.
Its state (``_working``, ``_status_hold``, ``_status_failures``, ``_conductor_gen``,
``_store``), its clock and its board cache are the service's own, set in the
service's ``__init__``; nothing here holds state of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.slack.code_channels.workspace.store import (
    WorkspaceRecord,
    WorkspaceStore,
    record_key,
)

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot

logger = logging.getLogger(__name__)


async def _set_status(channel_id: str, status: str) -> bool:
    from kiro_crew.slack.code_channels.workspace import service

    return await service._set_status(channel_id, status)


async def _clear_status(channel_id: str) -> bool:
    from kiro_crew.slack.code_channels.workspace import service

    return await service._clear_status(channel_id)


class WorkerStatus:
    """Set, hold and hand back the channel's status for its workers' turns."""

    if TYPE_CHECKING:
        _working: dict[str, str]
        _status_hold: dict[str, float]
        _status_failures: dict[str, int]
        _conductor_gen: dict[str, int]
        _store: WorkspaceStore
        _clock: Callable[[], float]

        def _cached_board(self, key: str) -> BoardSnapshot | None: ...

    async def _worker_status(self, record: WorkspaceRecord, now: dict[str, bool]) -> None:
        """The channel's Slack status while only its workers are moving.

        The bound session's own turns drive the status (the core's hooks); between
        them it follows the workers: ``suspended`` while one waits on you (or the
        conductor's last turn left it waiting), ``processing`` while one runs, and
        back to what the conductor's turn end showed once none does.
        """
        from kiro_crew.slack.code_channels import status as channel_status
        from kiro_crew.slack.code_channels.board.model import WAITING_ON_YOU

        bound_running = now.get(record.session_key, False)
        key = record.key
        if bound_running:
            # The bound session's own turn drives the status (the core's hooks).
            self._working.pop(key, None)
            self._status_hold.pop(key, None)
            self._status_failures.pop(key, None)
            return
        if channel_status.held(record.channel_id):
            # The agent asked the channel's people and waits on them: its workers
            # neither replace nor clear that, only its next turn does.
            self._working.pop(key, None)
            return
        hold = self._status_hold.get(key)
        if hold is not None:
            if self._cached_board(key) is None and self._clock() < hold:
                # The board was dropped only while the channel's sharing reading
                # is re-read: the status it set stays until the board is back
                # (or the hold runs out), instead of flapping off and on.
                return
            self._status_hold.pop(key, None)
        workers = any(run for k, run in now.items() if k != record.session_key)
        board = self._cached_board(key)
        waiting = board is not None and any(r.state == WAITING_ON_YOU for r in board.rows)
        conductor_waits = channel_status.after_turn(record.channel_id) == "suspended"
        if waiting or (conductor_waits and (workers or key in self._working)):
            want = "suspended"
        elif workers:
            want = "processing"
        else:
            want = ""
        sent = self._working.get(key, "")
        if want == "processing" and sent == want:
            # Slack ends ``processing`` after an hour and a code channel's driver
            # arms no keepalive, so a worker running longer would lose the loader.
            # The driver is asked again each pass; its own refresh window
            # (PROCESSING_REFRESH_SECS) decides when a write actually goes out.
            await _set_status(record.channel_id, want)
        elif want and want != sent:
            if await _set_status(record.channel_id, want):
                self._working[key] = want
        elif not want and sent:
            # Hand back what the conductor's turn end left (suspended or active).
            # Forgotten only once Slack took it: a failed hand-back is sent again
            # on the next pass, never left showing the workers' status.
            if await _set_status(record.channel_id, "suspended" if conductor_waits else "active"):
                self._working.pop(key, None)
                self._status_failures.pop(key, None)
            elif self._status_gave_up(key):
                self._working.pop(key, None)

    async def _drain_worker_status(self) -> None:
        """With the feature off, clear the statuses the watcher set; nothing new is sent."""
        if not self._working:
            return
        for record in await asyncio.to_thread(self._store.all):
            if record.key in self._working and await _clear_status(record.channel_id):
                self._working.pop(record.key, None)

    def _note_conductor_turns(self, key: str, conductor: Any) -> None:
        """Forget the status the watch set once a conductor turn ran since the last pass.

        The conductor's turn hooks set the channel status themselves (``processing``,
        then what its end leaves) without telling the watch. A turn that started
        and ended between two passes is seen only as a new turn generation; the
        watch's own record is then stale, so it is dropped and the workers' status
        (``suspended`` for one still waiting on you) is sent again.
        """
        generation = getattr(conductor, "_turn_generation", None) if conductor else None
        if not isinstance(generation, int):
            return
        before = self._conductor_gen.get(key)
        self._conductor_gen[key] = generation
        if before is not None and before != generation:
            self._working.pop(key, None)
            self._status_failures.pop(key, None)
            self._status_hold.pop(key, None)  # the status it held was overwritten too

    def _status_gave_up(self, key: str) -> bool:
        """Count one refused hand-back; True once :data:`_status_retries()` were refused.

        Bounded, so a refusal Slack keeps giving (the app left the channel) is
        not re-sent every pass forever; the conductor's next turn sets it again.
        """
        count = self._status_failures.get(key, 0) + 1
        if count < _status_retries():
            self._status_failures[key] = count
            return False
        self._status_failures.pop(key, None)
        logger.info(
            "code channel %s: status hand-back refused %d times; left to the next turn",
            key,
            count,
        )
        return True

    # ── turn end ────────────────────────────────────────────────────────────

    def _forget_worker_status(self, binding: Any) -> None:
        """The bound session's turn drives the channel status itself, so the
        watcher's record of the status it wrote (``processing`` / ``suspended``),
        its refused hand-backs and any hold are stale after it."""
        key = record_key(binding.team_id or "", binding.channel_id or "")
        self._working.pop(key, None)
        self._status_failures.pop(key, None)
        self._status_hold.pop(key, None)


def _status_retries() -> int:
    """``service._STATUS_RETRIES``, read at call time (tests shorten it)."""
    from kiro_crew.slack.code_channels.workspace import service

    return service._STATUS_RETRIES
