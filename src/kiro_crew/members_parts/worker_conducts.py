"""Which of a Bot's worker sessions conduct: thread-born lineage and the owner's switch.

A Bot's own thread hands every task to one worker. When the owner sets
``agents.<name>.worker_conducts`` (``KiroCrewAgentConfig.worker_conducts``, off by
default), each worker that thread opens may run as a conductor for its item: it gets
the session-control mount the thread has, the conductor's work-ledger grants and the
conductor rules, and its own ``session_create`` is forced onto the Bot's worker as the
thread's is (``rfc-bot-workers-conduct.md``). Which workers that is comes from two
facts kept apart on purpose:

* **lineage**, recorded once per session and never re-judged: a worker the Bot's own
  thread created is *thread-born*. The mark is this process's in-memory registry
  beside the backend-pick table (``member_sessions._SESSION_BACKEND_PICKS``), fed at
  mint by ``session_create`` and ``session_fork`` from the caller's key, and at
  restore by the slot metadata reader and at a revive by ``session_revive`` from the
  slot's VERIFIED ``created_by`` (an ``UNVERIFIED_CREATOR`` verdict marks nothing
  until the session tree confirms the claim, when
  ``session_revive.reconfirm_restored_creators`` marks it; a History resume, which
  restores no creator, drops the mark). A
  conducting worker's own children and forks are created by a ``chat-`` key, so they
  are never marked, whatever ``work_ledger.max_depth`` says: one conducting level, by
  lineage.
* **the switch**, read at each marked worker's session start from the Bot's record,
  never stored on the mark, so turning it off reaches each worker at its next start
  (a resume included) and a worker minted while it was off conducts once it is on.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.members_parts.member_sessions import (
    _slot_part,
    crewmate_worker_member_id,
    is_crewmate_worker_session,
    is_member_session_key,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")

#: Slot keys of the worker sessions a Bot's own thread created, in this process.
#: Lineage only: whether such a worker conducts is read from its Bot's record at
#: every session start (:func:`conducting_worker_session`).
_THREAD_BORN_WORKER_SLOTS: set[str] = set()

#: Slot keys whose fail-closed answer (an unreadable config or record) was logged,
#: so a worker left plain does not log at every read of the predicate.
_LOGGED_PLAIN: set[str] = set()


def note_worker_lineage(session_key: str, creator_key: str) -> None:
    """Record whether *session_key* was created by a Bot's own thread.

    The one writer of the thread-born mark, called where the slot's creator is
    known on a gateway-vouched path: at mint, with the ``session_create`` or
    ``session_fork`` caller's slot key; at restore and at a revive, with the slot's
    verified ``created_by``, and when the session tree lifts a restore's fence, with
    the creator it confirms; at a History resume, with no creator, since that path
    restores none. A creator that is a member DM key (``member-<slug>``,
    any layer spelling) marks the session; any other creator, a ``chat-`` key (a
    conducting worker's own child or fork), a cron stamp or an unverified claim
    included, leaves it unmarked and drops a stale mark.
    """
    if not session_key:
        return
    slot = _slot_part(session_key)
    if creator_key and is_member_session_key(creator_key):
        _THREAD_BORN_WORKER_SLOTS.add(slot)
    else:
        _THREAD_BORN_WORKER_SLOTS.discard(slot)
    _LOGGED_PLAIN.discard(slot)


def is_thread_born_worker_session(session_key: str | None) -> bool:
    """Whether this process knows *session_key* as a session a Bot's thread created."""
    return bool(session_key) and _slot_part(str(session_key)) in _THREAD_BORN_WORKER_SLOTS


def is_thread_born_crewmate_worker(session_key: str | None) -> bool:
    """Whether *session_key* is a Bot's own worker that the Bot's thread created.

    The two in-memory facts every conducting test starts from, the thread-born mark
    and the attested worker record (:func:`is_crewmate_worker_session`), with no
    config read. A session that also took the session-control mount at its start
    holds the conducting kit (``mate_worker_conducts.holds_conducting_kit``).
    """
    return is_thread_born_worker_session(session_key) and is_crewmate_worker_session(session_key)


def conducting_worker_session(session_key: str | None, config: Any = None) -> bool:
    """Whether the worker on *session_key* conducts: marked, known, and its Bot says so.

    All three must hold: the session is thread-born (:func:`note_worker_lineage`), it
    is a crewmate worker this process knows (:func:`is_crewmate_worker_session`, from
    the attested execution record), and that crewmate's record has
    ``worker_conducts`` NOW. Read where a session starts (its mount key, and its
    projected spec where the caller does not hand down that start's mount, both from
    the cached load); what a running worker does per call follows the kit that start
    gave it (``mate_worker_conducts.holds_conducting_kit``).
    The first two are in-memory tests, so a session that is neither reads no config.
    The record comes from *config* when the caller already holds the loaded config,
    else from the fingerprint-cached ``KiroCrewConfig.load()``.

    Fails CLOSED, the way ``member_dispatch_enabled`` and ``crew_panel_enabled`` do:
    a config read that raises, a config that loaded having discarded the ``agents``
    section or the whole file, and a member id that names no record in the config
    all answer False, so the worker runs as a plain worker, logged once per session.
    The switch being off is the default, not a failure, and is not logged.
    """
    if not is_thread_born_crewmate_worker(session_key):
        return False
    member_id = crewmate_worker_member_id(str(session_key))
    if not member_id:
        return False
    # circular import: the config loader's provider-backend path imports this
    # family, so both names are resolved at call time like the seams in
    # ``member_sessions``.
    from kiro_crew.config.loader import DEGRADED_WHOLE_CONFIG, KiroCrewConfig
    from kiro_crew.execution_context import member_config_for_id

    slot = _slot_part(str(session_key))
    try:
        cfg = config if config is not None else KiroCrewConfig.load()
        if cfg.degraded_sections & {DEGRADED_WHOLE_CONFIG, "agents"}:
            _log_plain(slot, "the agents section of the config could not be read")
            return False
        record = member_config_for_id(cfg, member_id)[1]
    except Exception:  # noqa: BLE001 - an unreadable config or an unknown id: plain worker
        _log_plain(slot, "its Bot's record could not be read")
        return False
    # The switch off is the default, not a failure: no log line for it.
    return getattr(record, "worker_conducts", False) is True


def _log_plain(slot: str, why: str) -> None:
    """Say once per session why a thread-born worker is left a plain worker."""
    if slot in _LOGGED_PLAIN:
        return
    _LOGGED_PLAIN.add(slot)
    logger.warning("worker session %s runs as a plain worker: %s", slot, why)


def mounts_session_control(session_key: str | None, config: Any = None) -> bool:
    """Whether *session_key* is a session the dashboard session-control server is
    mounted into: a crew member's DM thread (:func:`is_member_session_key`) or one of
    its conducting workers (:func:`conducting_worker_session`).

    The one predicate the session-array composers read, so the two populations
    cannot drift between the provider's key, the runtime's create and load paths and
    the per-chat client's array. Which population holds the mount still decides what
    rides it: the crew panel and the member grant set are the thread's alone.

    A worker's answer reads its Bot's record from *config* when the caller holds the
    loaded config, else from the cached config load, the source its spec reads
    (blocking): a caller on the event loop runs this off it.
    """
    return is_member_session_key(session_key) or conducting_worker_session(session_key, config)
