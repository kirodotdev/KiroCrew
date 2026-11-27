"""The durable record that a claimed row's parent conversation ended.

A parent-end teardown refuses to cancel a row that is claimed and not started, and
leaves it to its claimer. The teardown mark that tells the claimer the parent ended
lives in memory and ages out, so the teardown also writes a ``parent_end`` tombstone on
the row's run folder, and a claimer whose own cancel cannot reach the store writes one
when it retains the claim. The boot reconcile settles the row ``cancelled`` on it, the
retained-claim retry reads it instead of the mark, and the reaper's tombstone pruner
keeps it while the row is not terminal.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.subagent_manager.admission.types import PARENT_END_TOMBSTONE_CAUSE

logger = logging.getLogger("kiro_crew.subagent")


async def record_parent_end(
    agent_id: str,
    parent_session_key: str,
    verb: str = "",
    *,
    attempt: int,
    stop_origin: str = "",
) -> None:
    """Write a ``parent_end`` tombstone for *agent_id*, a claimed row whose parent ended.

    *attempt* is the generation of the row's claim. The record names it, and its
    ``outcome`` is ``stopped``, the ending a parent end gives a live run. The run
    folder is the row id's, so it can hold the tombstone of an earlier attempt, and
    the record replaces that one (``write_tombstone_if_absent``). A tombstone that
    names *attempt* stays. If it is a ``parent_end`` one, the teardown wrote this
    record before the claimer retained the claim, and the call counts it as recorded.
    Any other cause is the run's own ending, which is logged. *stop_origin*
    defaults to the teardown's own wording for *verb*. Written off the loop, since it
    is file I/O with an fsync. A write that fails is logged and skipped, leaving the
    in-memory mark as the only record. Only a persistent run has a durable row, so no
    incognito or temporary run reaches this write.
    """
    from kiro_crew.subagent_persistence import write_tombstone_if_absent

    try:
        written = await asyncio.to_thread(
            write_tombstone_if_absent,
            agent_id,
            cause=PARENT_END_TOMBSTONE_CAUSE,
            recovery_action="none",
            attempt=attempt,
            outcome="stopped",
            parent_session=parent_session_key,
            stop_origin=stop_origin or f"parent conversation ended ({verb or 'unnamed'})",
        )
    except Exception:
        logger.warning("Recording the parent end of claimed row %s failed", agent_id, exc_info=True)
        return
    if not written:
        logger.info(
            "Claimed row %s keeps the tombstone of its attempt %d; its parent end is not recorded",
            agent_id,
            attempt,
        )


async def leave_to_claimer(
    agent_id: str, parent_session_key: str, verb: str, *, attempt: int
) -> None:
    """The teardown's hand-off of a claimed, unstarted row: log it and record the parent end.

    The claimer reads the teardown mark before and after its start write and cancels the
    row instead of starting it (``claim_and_start``); the tombstone written here is what
    still says so once the mark is gone. *attempt* is the generation of that claim.
    """
    logger.info(
        "Teardown: leaving %s to its claimer -- the row is claimed and "
        "not started; the claimer reads the teardown mark and cancels it",
        agent_id,
    )
    await record_parent_end(agent_id, parent_session_key, verb, attempt=attempt)
