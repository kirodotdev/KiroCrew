"""The Code tab's publish and takedown: a mixin of :class:`~.service.WorkspaceService`.

Kept in its own module so the refresh owner stays under the file cap. Its state
(the store, the channel locks, the board cache) and its Slack writes are the
service's own; nothing here holds state of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

if TYPE_CHECKING:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot

logger = logging.getLogger(__name__)

#: The tab that replaces an owed Code tab whose id was never learned, so it can be
#: removed by the id Slack answers with: one fixed line, nothing of the channel's
#: own code or of any helper agent's, safe for every audience the channel has.
WITHDRAWN_TAB = "This Code tab was withdrawn.\n"
#: The first wait before that replacement is tried again, doubling to the cap.
WITHDRAW_BACKOFF_SECS = 60.0
WITHDRAW_BACKOFF_MAX_SECS = 1800.0


def _tell_owner(record: WorkspaceRecord) -> None:
    """A dashboard notice that a Code tab with helper agents' work could not be withdrawn."""
    from kiro_crew.slack.code_channels import surface

    notify = getattr(surface.dashboard_state(), "notify", None)
    if not callable(notify):
        return
    try:
        notify(
            "slack",
            "Code tab not withdrawn",
            "A code channel's Code tab may still show helper agents' commits the "
            "channel no longer admits, and Slack did not accept its replacement. "
            "Kiro Crew keeps retrying; remove the tab in Slack to end it now.",
            meta={"channel_id": record.channel_id, "session_key": record.session_key},
        )
    except Exception:
        logger.debug("code channel %s: Code tab notice failed", record.channel_id, exc_info=True)


class CodeTab:
    """Publish the Code tab, and owe its takedown while it may carry helper agents."""

    if TYPE_CHECKING:
        _store: WorkspaceStore
        _clock: Callable[[], float]
        #: ``channel key -> (next try, wait after it)`` for :meth:`_withdraw_unknown`.
        _withdraw_wait: dict[str, tuple[float, float]]
        #: Channels whose owner was told a Code tab could not be withdrawn.
        _withdraw_told: set[str]

        def _lock(self, key: str) -> asyncio.Lock: ...

        def _board_still_admitted(self, binding: Any) -> bool: ...

        def _write_fence(self, binding: Any, *, board: bool) -> Callable[[], bool] | None: ...

        def _worker_projects(self, board: Any) -> dict[str, str]: ...

        def _build_diff(
            self,
            record: WorkspaceRecord,
            board: BoardSnapshot | None = None,
            trees: dict[str, str] | None = None,
        ) -> tuple[str, str, str] | None: ...

        async def _call(
            self, method: str, record: WorkspaceRecord, *args: Any, **kwargs: Any
        ) -> Any: ...

    async def _publish_diff(
        self,
        record: WorkspaceRecord,
        binding: Any = None,
        board: BoardSnapshot | None = None,
        *,
        replacing: bool = False,
    ) -> bool:
        """Publish the Code tab when it changed; False when a tab that must go did not.

        With *board*, the tab is the combined change (:meth:`_build_diff`), sent
        only while the board's scope admits *binding*: the client asks it right
        before the raw send, after the git work and every await. Refused there,
        the channel's own diff goes instead. *replacing*: the tab up may carry
        helper agents' commits, so when no channel-only diff can be built it is
        taken down.
        """
        if record.slack_off:
            return True
        if not record.worktree:
            # No tree to diff (rebound to a folder that is not a repository): a
            # tab an earlier tree opened would describe code this channel no
            # longer works in, so it goes.
            return await self._clear_diff(record)
        trees = self._worker_projects(board) if board is not None else {}
        # A record whose diff base was dropped (repaired on load) builds nothing
        # until it rebinds: the same rule as a build that failed.
        built = (
            await asyncio.to_thread(self._build_diff, record, board, trees)
            if record.base_sha
            else None
        )
        if built is None:
            # Nothing built (git unavailable for a moment): a tab that may carry
            # helper agents the scope refuses now is taken down; one it
            # admits stays until the next pass rebuilds it.
            if replacing and (board is None or not self._board_still_admitted(binding)):
                return await self._clear_diff(record)
            # A tab owed the board's helper agents is kept and the pass re-armed
            # until it builds; the channel's own retries on its next turn, and a
            # record with no diff base waits for the rebind its turn start makes.
            return board is None or not record.base_sha
        content, base_label, head_label = built
        if not content:
            return await self._clear_diff(record)
        from kiro_crew.slack.code_channels.workspace.publishing import _digest

        digest = _digest(content, base_label, head_label)
        if digest == record.diff_hash and not (record.diff_workers and board is None):
            # Unchanged. Not when a tab built with the board may be up and this one
            # is the channel's own: the stored digest may predate a send whose
            # answer was lost, so the channel-only tab is sent to replace it.
            self._note_tab(record, board)
            return True
        gate = (lambda: self._board_still_admitted(binding)) if board is not None else None
        prior = record.diff_workers
        if board is not None and not prior:
            # Owed from before the send, on disk: a send whose answer is lost (or
            # a pass cancelled or killed after it) may have landed, and the tab's
            # id is then never learned. Not written down, nothing is sent.
            if not await self._owe_tab(record):
                return False
        uncertain: list[bool] = []
        from kiro_crew.slack.code_channels.workspace.publishing import _FENCED

        view_id = await self._call(
            "set_diff_view",
            record,
            content,
            base_branch=base_label,
            head_branch=head_label,
            gate=gate,
            uncertain=uncertain,
            # At Slack's view cap a stale tab of ours makes room; the channel's
            # activation (Off or Review refuse) is asked right before the send.
            room_for="diff",
            fence=self._write_fence(binding, board=False),
        )
        if view_id is _FENCED:
            view_id = None  # set to Off or Review meanwhile: nothing was sent
        if view_id is None:
            if not any(uncertain):
                record.diff_workers = prior  # never applied: the tab up is unchanged
                if board is not None and not prior:
                    await self._unowe_tab(record)  # undo this send's own write-ahead
            if gate is not None and not gate():
                # Refused at the send: the channel's own tab instead, under the
                # same activation fence (an Off or Review sends nothing at all).
                return await self._publish_diff(record, binding, replacing=True)
            # A tab that carries (or replaces one carrying) helper agents must land:
            # the pass stays unsent and is re-armed. The channel's own retries next turn.
            return board is None and not replacing
        record.diff_hash = digest
        if isinstance(view_id, str) and view_id:
            record.view_id = view_id
        self._note_tab(record, board)
        return True

    async def _owe_tab(self, record: WorkspaceRecord) -> bool:
        """Store ``diff_workers=True`` for *record*'s channel before a send built with the board."""
        try:
            async with self._lock(record.key):
                current = await asyncio.to_thread(
                    self._store.get, record.team_id, record.channel_id
                )
                if current is None:
                    return False
                if not current.diff_workers:
                    current.diff_workers = True
                    await asyncio.to_thread(self._store.put, current)
        except Exception:
            logger.warning(
                "code channel %s: Code tab not owed; not sent", record.channel_id, exc_info=True
            )
            return False
        record.diff_workers = True
        return True

    async def _unowe_tab(self, record: WorkspaceRecord) -> None:
        """Undo :meth:`_owe_tab` after Slack refused the send it was written for.

        Only while the stored tab is still the one this pass started from, so a
        rebind that carried the write-ahead flag does not keep an obligation for
        a tab that never went out. A failed write keeps the flag (the safe side).
        """
        try:
            async with self._lock(record.key):
                current = await asyncio.to_thread(
                    self._store.get, record.team_id, record.channel_id
                )
                if current is not None and current.diff_workers:
                    if current.view_id == record.view_id:
                        current.diff_workers = False
                        await asyncio.to_thread(self._store.put, current)
        except Exception:
            logger.debug("code channel %s: Code tab owed flag kept", record.channel_id)

    @staticmethod
    def _note_tab(record: WorkspaceRecord, board: BoardSnapshot | None) -> None:
        """Which tab is up now: one built with the board (it may carry helper agents)."""
        record.diff_workers = board is not None

    async def _clear_diff(self, record: WorkspaceRecord) -> bool:
        """No changes any more (reverted, or merged into the base): drop the diff tab.

        ``setView`` refuses an empty diff, so a tab that was published is removed
        instead, whenever its view id is known: the digest alone may have been
        reset for a republish (a rebind, a raced refresh) while the tab stands.
        Never published at all, there is simply no tab yet. True once no tab is up.
        A tab built with the board whose id was never learned (its answer was lost)
        is replaced by a fixed line, then removed (:meth:`_withdraw_unknown`).
        """
        if not record.view_id:
            if not record.diff_workers:
                return True
            return await self._withdraw_unknown(record)
        if await self._call("remove_view", record, record.view_id) is not None:
            record.diff_hash, record.view_id = "", ""
            record.diff_workers = False
            return True
        return False

    async def _withdraw_unknown(self, record: WorkspaceRecord) -> bool:
        """Take down an owed tab whose id was never learned; True once it is down.

        ``setView`` keeps one diff tab per channel and answers with its id, so a
        tab of :data:`WITHDRAWN_TAB` (a fixed line, nothing of the channel's or
        any helper agent's) replaces it, and that id is then removed. Tried at
        most once per :data:`WITHDRAW_BACKOFF_SECS` (doubling to
        :data:`WITHDRAW_BACKOFF_MAX_SECS`); while it fails the record stays owed,
        and the owner is told once that a Code tab could not be withdrawn.
        """
        key = record.key
        now = self._clock()
        due, wait = self._withdraw_wait.get(key, (0.0, WITHDRAW_BACKOFF_SECS))
        if now < due:
            return False
        view_id = await self._call("set_diff_view", record, WITHDRAWN_TAB, gate=None)
        if view_id is not None:
            # Slack answered: the helper agents' tab is replaced by the fixed line.
            self._withdraw_wait.pop(key, None)
            self._withdraw_told.discard(key)  # a later failure is told again
            record.diff_hash, record.diff_workers = "", False
            if isinstance(view_id, str) and view_id:
                record.view_id = view_id
                return await self._clear_diff(record)
            return True  # the fixed line stays up: its id was not answered
        self._withdraw_wait[key] = (now + wait, min(wait * 2, WITHDRAW_BACKOFF_MAX_SECS))
        if key not in self._withdraw_told:
            self._withdraw_told.add(key)
            _tell_owner(record)
        return False

    async def _take_down_owed(self, team: str, channel: str) -> bool:
        """Take down a tab that may carry helper agents, in a channel set to Off or Review.

        The operator stopped the bot answering there, which refuses the board but
        not Slack writes, so the tab comes down now (by id, else as
        :meth:`_withdraw_unknown` does) rather than outlive the switch. True when
        nothing is owed any more, or nothing can be sent (``slack_off``: kept on
        disk for the first pass after the channel's views come back).
        """
        record = await asyncio.to_thread(self._store.get, team, channel)
        if record is None or not record.diff_workers or record.slack_off:
            return True
        start = (record.view_id, record.diff_hash)
        done = await self._clear_diff(record)
        async with self._lock(record.key):
            current = await asyncio.to_thread(self._store.get, team, channel)
            if current is not None and (current.view_id, current.diff_hash) == start:
                current.view_id, current.diff_hash = record.view_id, record.diff_hash
                current.diff_workers = record.diff_workers
                current.slack_off = current.slack_off or record.slack_off
                await asyncio.to_thread(self._store.put, current)
        return done
