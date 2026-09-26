"""The transcript side of the ``shake`` compaction method, for dashboard sessions.

The compaction coordinator owns no transcript, so it hands ``shake`` to the
surface through ``SessionManager.set_compaction_seed_writer``: this module reads
the rows the successor's replay would admit, splits them at the recent tail,
digests the dropped slice with large fenced blocks elided, and leaves the digest
as a ``compaction`` row in the slot's live window, written to disk before the
coordinator hears of it (``DashboardState.save_slot_strict``): the answer lets
the native conversation be dropped, so the row must survive a restart first.
The successor's first prompt merges the live window with the disk transcript,
so ``build_session_replay`` sees the seed either way.

The writer runs with the session's turn semaphore held (the coordinator takes
it first), so the rows it reads are quiescent. It answers the coordinator in
four words: ``written``; ``nothing`` when the complete conversation fits the
tail; ``tail_only`` when no digest was written but rows fall outside that tail;
or ``unsupported`` when no transcript lives here for this key. The first two
no-seed answers both recycle as ``soft`` under the same hold, but only
``nothing`` says every conversation row was carried. The coordinator asks
``supports`` first, before it projects the rotation at all, so a channel-born
session or a key with no tab never reaches ``soft`` by way of a projection the
writer was never asked about.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from kiro_crew.autonudge import is_channel_key
from kiro_crew.context import replay_walk_options, rotation_rows
from kiro_crew.session_compaction_methods import (
    SEED_BUDGET_DIVISOR,
    SEED_META_KIND,
    SEED_NOTHING,
    SEED_ROLE,
    SEED_TAIL_ONLY,
    SEED_UNSUPPORTED,
    SEED_WRITTEN,
    shake_elide,
    split_tail,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _build_seed(
    rows: Sequence[Mapping[str, Any]], *, window_tokens: int | None, method: str
) -> tuple[tuple[str, dict[str, Any]] | None, bool]:
    """Return the seed row and whether the tail split dropped at least one row."""
    options = replay_walk_options(window_tokens)
    budget = options["budget_chars"]
    split = split_tail(rows, **options)
    if not split.dropped:
        return None, False
    digest = shake_elide(split.dropped, budget_chars=max(1, budget // SEED_BUDGET_DIVISOR))
    if not digest.text:
        return None, True
    meta: dict[str, Any] = {
        "kind": SEED_META_KIND,
        "method": method,
        "through_row": digest.through_row,
        "through_ts": digest.through_ts,
        "dropped_rows": digest.rows_in,
    }
    return (digest.text, meta), True


def build_seed(
    rows: Sequence[Mapping[str, Any]], *, window_tokens: int | None, method: str
) -> tuple[str, dict[str, Any]] | None:
    """The seed row's ``(content, meta)`` for *rows*, or ``None`` when no seed can be written.

    *rows* are chronological and already admitted by the replay's own rules
    (``context.rotation_rows``). They are split with the replay's own walk
    options for a *window_tokens* model (``context.replay_walk_options``:
    budget, inject clipping, inject reserve), so the tail left alone is exactly
    the tail the successor's replay carries; everything older is digested into
    at most a ``SEED_BUDGET_DIVISOR``-th of that same budget. The digest names
    the exact row it covers through (``through_row``), not just its stamp.
    """
    return _build_seed(rows, window_tokens=window_tokens, method=method)[0]


def _seed_slot(state: DashboardState, key: str) -> Any | None:
    """The slot whose transcript a seed for *key* would be written to, or ``None``.

    The channel check comes first: a Slack or Discord conversation keeps its own
    session key even while a dashboard tab mirrors it, so ``dashboard_slot_key``
    alone would answer with that tab and digest a channel session's history into
    a tab its user may never open. A key with no tab (a cron, subagent or
    background session) has no transcript here either.
    """
    if is_channel_key(key):
        return None
    # circular import: chat_utils imports dashboard.state at module scope, and
    # state registers this writer, so a module-scope import here would close
    # state -> compaction_seed -> chat_utils -> state.
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_key = dashboard_slot_key(key)
    if not slot_key:
        return None
    return state.get_slot(slot_key)


def supports_seed(state: DashboardState, key: str) -> bool:
    """Whether this dashboard holds a transcript it can digest for *key* (no transcript read)."""
    return _seed_slot(state, key) is not None


async def write_seed(
    state: DashboardState, key: str, method: str, window_tokens: int | None
) -> str:
    """Append the ``shake`` seed row to the slot showing *key*; one of the ``SEED_*`` answers.

    ``SEED_UNSUPPORTED`` when no transcript for *key* lives here (see
    ``_seed_slot``), ``SEED_NOTHING`` when the complete conversation fits the
    successor's tail, ``SEED_TAIL_ONLY`` when no seed was written but rows fall
    outside that tail, and ``SEED_WRITTEN`` once the seed row is in the slot.
    The transcript read runs off the event loop: a large session's chained read
    is a file parse.
    """
    slot = _seed_slot(state, key)
    if slot is None:
        return SEED_UNSUPPORTED
    from kiro_crew.dashboard.chat_utils import slot_history_key

    expected_slot_name = slot.key
    expected_history_key = slot_history_key(slot)
    rows = await asyncio.to_thread(
        rotation_rows,
        state.conversation_log,
        expected_history_key,
        pending_messages=list(slot.messages),
    )
    seed, split_dropped_rows = _build_seed(rows, window_tokens=window_tokens, method=method)
    if seed is None:
        conversation_quota_cut = getattr(rows, "conversation_quota_cut", True)
        if split_dropped_rows or conversation_quota_cut:
            return SEED_TAIL_ONLY
        return SEED_NOTHING
    content, meta = seed
    # Hidden by contract: the transcript registries claim the role undrawn, so
    # the row must not go out as a live ``chat_message`` frame the transcript
    # view would hold. No presentation class: the row draws nothing on every
    # surface, and a collapsed marker for it is follow-up work.
    row = slot.append(SEED_ROLE, content, "", meta=meta, broadcast=False)
    # The answer lets the coordinator drop the native conversation, so the
    # digest must be on disk before it is given: a gateway restart between the
    # recycle and the periodic flush would otherwise reload a transcript with
    # no seed, and the successor would carry the tail alone. A write that does
    # not happen (a guarded metadata save in flight, a refused save, an error)
    # withdraws the row, so no later rotation carries a digest of rows that
    # were never dropped, and answers unsupported: native keeps the history.
    # The pins keep the save bound to the live tab and history file observed
    # before the off-loop read. A close, replacement, or rebind refuses the stale
    # save, so this path withdraws the seed.
    try:
        await asyncio.to_thread(
            state.save_slot_strict,
            slot,
            expected_slot_name=expected_slot_name,
            expected_history_key=expected_history_key,
        )
    except Exception:
        slot.withdraw(row)
        logger.warning(
            "Compaction seed for %s could not be made durable; withdrawn, handing to native",
            key,
            exc_info=True,
        )
        return SEED_UNSUPPORTED
    logger.info(
        "Compaction seed written for %s: %d rows digested into %d chars",
        key,
        meta["dropped_rows"],
        len(content),
    )
    return SEED_WRITTEN


class DashboardSeedWriter:
    """The ``SeedWriter`` the dashboard registers: ``supports_seed`` and ``write_seed`` bound to one state."""

    __slots__ = ("_state",)

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    def supports(self, key: str) -> bool:
        return supports_seed(self._state, key)

    async def __call__(self, key: str, method: str, window_tokens: int) -> str:
        return await write_seed(self._state, key, method, window_tokens)
