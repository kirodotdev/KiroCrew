"""The later restore of the crewmate threads the boot restore had to defer.

The open-tab restore resolves a crewmate thread from its binding, and a binding
that names its crewmate by id resolves only through the Crew config. A config
read that finds no such crewmate at that moment (a rewrite in flight, say) leaves
the thread unbuilt; the restore keeps its key in the reopen seed
(``state.unrestored_slot_keys``) and names it in
``state.deferred_member_slot_keys``, and this pass, run once after the boot
settles, builds it then or drops it. Only those deferred keys are this pass's:
a seed kept for another reason (an unreadable read, a remote-only transcript) is
left alone. A crewmate's own check-ins load the thread too
(``goal_reminder._load_thread``), so this pass covers the crewmates with none.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How long after the boot restore the pass runs: long enough for the boot's own
#: config writes to land, short enough that a crewmate is back within minutes.
MEMBER_RESTORE_RETRY_DELAY_S = 120.0


def deferred_member_slot_keys(state: Any) -> list[str]:
    """The crewmate thread keys the open-tab restore deferred, built since or not."""
    deferred = getattr(state, "deferred_member_slot_keys", None) or ()
    return sorted(key for key in deferred if isinstance(key, str))


def closed_in_this_process(state: Any, slot_key: str) -> bool:
    """Whether *slot_key*'s tab was closed in this process (its close tombstone).

    The restore's own guards see a close only once its ``closed`` flag is saved,
    or when it lands during the restore's read; a close that began earlier and
    is still saving shows only here. An automatic load never builds such a tab.
    """
    from kiro_crew.dashboard.channel_slots import slot_close_instant

    return slot_close_instant(state, slot_key) is not None


async def retry_deferred_member_slots(
    state: Any, *, delay: float = MEMBER_RESTORE_RETRY_DELAY_S
) -> int:
    """Wait *delay* seconds, then build each deferred crewmate thread; return how many.

    The retry is every deferred key's last: it leaves the deferred set either
    way. It leaves the reopen seed when it is built, when another path built or
    closed it first, and when its binding still names no crewmate (one deleted
    for good). Any other failure (a read error, a transcript that did not build)
    keeps the seed, so the next boot's restore decides it like any other. The
    threads it builds get the boot's parked-send pass (:func:`resume_parked_sends`).
    """
    await asyncio.sleep(delay)
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

    built = 0
    for key in deferred_member_slot_keys(state):
        if state.get_slot(key) is not None or closed_in_this_process(state, key):
            # Built by another path meanwhile (the user opened it, a check-in
            # loaded it), or closed: the slots answer for it now.
            end_deferral(state, key)
            continue
        _drop_retry_mark(state, key)
        binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, key)
        # Asked again after the read: the user can open and close the thread
        # meanwhile, and the rebuild below starts its own close guard only on entry.
        if state.get_slot(key) is not None or closed_in_this_process(state, key):
            end_deferral(state, key)
            continue
        if binding is None:
            # Still no crewmate for the binding after the boot settled: one deleted
            # for good is not carried from boot to boot. The next flush prunes the
            # key; a check-in or an open still loads it once the binding resolves.
            end_deferral(state, key)
            logger.warning("member slot retry: %s still names no crewmate; dropping it", key)
            continue
        try:
            slot = await rehydrate_slot_from_history_async(state, key)
        except Exception:
            # A transient read failure: the key stays in the reopen seed, and the
            # next boot's restore decides it as it decides any other seed.
            logger.warning("member slot retry: restoring %s failed", key, exc_info=True)
            continue
        if slot is None:
            # The binding resolves but the transcript did not build (unreadable,
            # remote-only, closed on disk): also the next boot's restore to decide.
            logger.warning("member slot retry: %s did not build; keeping its seed", key)
            continue
        end_deferral(state, key)
        built += 1
        logger.info("member slot retry: restored %s", key)
    if built:
        await resume_parked_sends(state)
    return built


def _drop_retry_mark(state: Any, key: str) -> None:
    """Take *key* out of the deferred set, leaving its reopen seed where it is."""
    deferred = getattr(state, "deferred_member_slot_keys", None)
    if isinstance(deferred, set):
        deferred.discard(key)


def end_deferral(state: Any, key: str) -> None:
    """Take *key* out of the deferred set and the reopen seed the restore put it in."""
    _drop_retry_mark(state, key)
    seed = getattr(state, "unrestored_slot_keys", None)
    if isinstance(seed, set):
        seed.discard(key)


async def resume_parked_sends(state: Any) -> None:
    """Resume the accepted parked sends of threads built after the boot restore.

    The boot runs ``drain_restored_parked_sends`` once, over the slots it built;
    a thread built later carries its accepted parked sends in its queue and
    needs the same pass, or they wait there and its crewmate reads as busy.
    """
    from kiro_crew.dashboard.slot_queue_repository import drain_restored_parked_sends

    try:
        await drain_restored_parked_sends(state)
    except Exception:
        logger.warning("resuming parked sends after a late restore failed", exc_info=True)


def schedule_member_slot_retry(state: Any) -> asyncio.Task[int] | None:
    """Start :func:`retry_deferred_member_slots` when the restore deferred a thread."""
    if not deferred_member_slot_keys(state):
        return None
    task = asyncio.create_task(retry_deferred_member_slots(state))
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return task
