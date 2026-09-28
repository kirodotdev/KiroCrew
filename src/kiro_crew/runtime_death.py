"""A runtime death is a PROCESS event, so it is classified once and read by all.

Recovery is written elsewhere as though a dying process were one session's
misfortune. Every tenant of a shared runtime catches its own ``AcpProcessDied``,
independently decides to reset, and independently charges the death to its own
retry budget -- so one process ending produces N resets, N budget charges, and N
unrelated accounts of why. One such death takes every session on the process down
with it -- a measured one carried 15, 8 of them mid-prompt -- and each of those
eight records the loss as its own failure.

This module holds the other half of that story. :func:`announce` is called ONCE,
where the death is detected, and records what the process was carrying at that
instant. Every tenant then reads the SAME record instead of re-deriving one:

* :func:`caused_by_this_session` -- the ONE question a retry budget or a circuit
  breaker has to answer before it charges anybody.

What a tenant is NEVER given is a pid. A session asks about the runtime handle it
already holds, exactly as :func:`kiro_crew.runtime_ownership.outstanding_leases`
answers by object identity, because a session that can name a pid can signal it.

Why the tenants PULL the record rather than being pushed it
-----------------------------------------------------------
A tenant learns of the death the way it already does -- its poisoned session
queue raises into whatever turn it had -- and reads this record while handling it.
No layer is woken on its behalf, because none needs to be: a tenant with no turn
in flight has nothing to recover and re-acquires on its next turn through the
ownership registry, which drops a dead runtime on the way past.

That is also why there is no observer registry here. A notification channel with
no subscriber is a mechanism whose first real consumer would have to redesign it,
so the layer that needs one adds it together with that consumer.

Two independent readings of co-tenancy, and why both are needed
---------------------------------------------------------------
``leases`` is what :mod:`kiro_crew.runtime_ownership` knew: the sessions that
took a lease through the registry. ``acp_sessions`` is how many ACP sessions the
process was actually multiplexing.

They disagree, and the gap is not noise. A sub-agent runs on its parent's runtime
through the parent provider rather than through an acquisition of its own, so it
holds no lease and the registry cannot see it -- while the runtime, which has a
queue per ACP session, can. Reading only the registry would call that process
single-tenant and hand the parent the bill for the child's death, which is the
misattribution this module exists to end. Reading only the runtime would miss a
tenant that has a lease but has not opened its session yet.

The lease reading is not available on every death path, and saying so is part of
the contract. The registry excludes a runtime it considers dead, and it decides
that by asking the runtime's own liveness -- which is already false once the
process has a return code. So on a death that is REAPED (the reader sees EOF and
reads a return code that is already set) the lease count is 0 whatever the
registry holds, and ``acp_sessions`` decides ``shared`` by itself. On a death the
caller INITIATES the death is marked before the process is waited on, so liveness
still holds and the leases are counted. The lease reading therefore closes the
lease-without-a-session gap on the kill path only; on the reaped path that gap is
open, and a tenant holding a lease with no ACP session yet is not seen.

So a process is shared when EITHER reading says so, and neither number is
presented as the tenant count: they are two measurements of one process, and the
honest summary is both.

This module is a LEAF: it imports nothing from ``kiro_crew``. It is reached from
the ACP layer below and from the dashboard, channel and automation layers above,
and an import in the other direction would close a cycle through
``acp.runtime``. The agent-SDK boundary gate refuses such an import outright,
type-only ones included, which is why the runtime is duck-typed here.
"""

from __future__ import annotations

import hashlib
import logging
import weakref
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeDeath:
    """One process's death, as the fact every tenant of it reads.

    Frozen because it is shared: the whole point is that N tenants read one
    record, and a record a reader can edit is a record the next reader cannot
    trust.
    """

    #: The process that died. Recorded for the LOG only -- it is never handed to
    #: a session, which asks by runtime handle.
    pid: int | None
    #: Why, as ``_mark_dead`` settled it.
    reason: str
    #: True for a deliberate teardown, False for every genuine death.
    expected: bool
    #: Leases the ownership registry held on it at that instant.
    leases: int
    #: ACP sessions the process was multiplexing at that instant.
    acp_sessions: int

    @property
    def shared(self) -> bool:
        """Whether more than one tenant was on this process when it died.

        Either reading is sufficient, for the reason the module docstring gives:
        a lease the runtime has not seen yet and a sub-agent session the registry
        never saw are both real tenants, and each is invisible to the other
        measurement. Note the asymmetry recorded there: ``leases`` is 0 on a
        reaped death whatever the registry held, so on that path ``acp_sessions``
        decides this alone.
        """
        return self.leases > 1 or self.acp_sessions > 1


#: Deaths by runtime IDENTITY. Weak keys, so a record lives exactly as long as
#: something can still ask about that runtime: while a tenant holds the provider
#: that holds it, the question is answerable, and once nothing holds it the
#: question is unanswerable anyway and the row is collected with it. A strong map
#: here would retain every dead runtime -- and its stderr ring and its child
#: record -- for the gateway's life.
_deaths: "weakref.WeakKeyDictionary[Any, RuntimeDeath]" = weakref.WeakKeyDictionary()


def announce(
    runtime: object,
    *,
    reason: str,
    expected: bool,
    leases: int,
    acp_sessions: int,
) -> RuntimeDeath:
    """Record a process's death ONCE, as the fact every tenant of it will read.

    Called from the one place that detects the death, before the pending futures
    are failed and the session queues poisoned -- so a tenant woken by its
    poisoned queue can already read why it was woken.

    Returns the record so the caller can log from it rather than recomposing the
    same facts in different words.
    """
    death = RuntimeDeath(
        pid=_pid_of(runtime),
        reason=reason,
        expected=expected,
        leases=max(0, leases),
        acp_sessions=max(0, acp_sessions),
    )
    try:
        _deaths[runtime] = death
    except TypeError:
        # Not weak-referenceable (a slotted test double). Only the per-runtime
        # lookup is unavailable, and its callers treat a missing record as "not
        # attributable", which charges exactly as before.
        logger.debug("runtime_death: runtime is not weak-referenceable, record not keyed")
    if death.shared:
        # WARNING, not INFO: this is the line that says one process ending is
        # about to surface as several sessions failing, and the gateway runs at
        # WARNING, so INFO here would mean the log of every deployment that has
        # the problem is missing the one line that names it.
        logger.warning(
            "runtime_death SHARED pid=%s leases=%d acp_sessions=%d expected=%s reason=%s",
            death.pid,
            death.leases,
            death.acp_sessions,
            death.expected,
            death.reason,
        )
    return death


def death_of(target: object) -> RuntimeDeath | None:
    """The death record for *target*, or None when it has not died here.

    *target* may be the runtime itself or a provider in front of one, so the
    lookup takes whatever handle a holder already has instead of reaching for a
    pid.

    Deliberately NOT part of this module's public surface: the one question a
    caller outside here has is :func:`caused_by_this_session`, and a record
    reader exported without a consumer is a shape the first real consumer would
    have to redesign.
    """
    for candidate in _runtime_candidates(target):
        try:
            death = _deaths.get(candidate)
        except TypeError:
            continue
        if death is not None:
            return death
    return None


def caused_by_this_session(target: object) -> bool:
    """Whether the session holding *target* is answerable for its death.

    The ONE predicate a retry budget, a circuit breaker or an auto-pause counter
    asks before it charges a failure. False means the process was carrying other
    tenants when it ended, so no single session's account is the right place to
    record it -- charging one anyway is how five deaths of a shared process
    auto-paused a cron that had done nothing wrong.

    True is the conservative answer and is returned whenever the question cannot
    be settled: no record, a record for a process this session was alone on. That
    keeps a single-tenant runtime -- every runtime at ``CHAT_RUNTIME_CAP`` 1 with
    no sub-agent on it -- charging exactly as it charges today.
    """
    death = death_of(target)
    if death is None:
        return True
    return not death.shared


#: Sessions whose runtime kept dying under them, and how many times running.
#: Bounded, and evicted least-recently-TOUCHED: a session key is added only by a
#: SHARED death and removed by its next landed turn, so the live set is at most
#: the sessions currently riding dying processes -- but a session that never
#: lands again would otherwise leave its row forever, and that abandoned row is
#: the only thing this cap exists to drop.
#:
#: Least-recently-touched is load-bearing, not a refinement. A plain
#: insertion-order eviction drops the row inserted first, and re-assigning a
#: dict key does not move it, so the victim was whichever session started dying
#: EARLIEST -- the one whose streak is furthest along and closest to tripping its
#: caller's ladder. Its streak then read 0 again, and because a shared death
#: never advances the session's own retry counter, nothing else bounded the
#: recovery: the session was re-queued forever. Touching a row therefore moves it
#: to the end, which is what makes the victim an abandoned row rather than the
#: most active one.
_SHARED_STREAK_MAX_KEYS = 512

#: Longest session key this table RETAINS verbatim. Capping the row COUNT bounds
#: memory only when every field each row retains is bounded too, and the key IS a
#: field: 512 rows of an unbounded string is unbounded. Callers supply the key,
#: and a dashboard slot key's length is not bounded anywhere on the way here --
#: ``_normalize_slot_key`` folds its charset without shortening it -- so the cap
#: above on its own does not bound this table.
#:
#: A longer key is stored as a fixed-length DIGEST rather than truncated, because
#: two sessions sharing a prefix must not collapse into one streak: a merged
#: streak bounds unrelated sessions together and would stand a loop down for a
#: process it never rode. Same reasoning and same shape as
#: ``messaging/turn_ceiling.py``'s own retained-key bound.
_MAX_RETAINED_KEY_CHARS = 256
_shared_streaks: dict[str, int] = {}


def _store_key(session_key: str) -> str:
    """The bounded form of *session_key*, as this table retains and looks it up.

    Applied at EVERY door -- record, read and clear -- because a digest used at
    one and the raw string at another is worse than neither: the row would be
    written under one name and looked up under a second, so the streak would read
    0 forever and the bound it feeds would never fire.
    """
    if len(session_key) <= _MAX_RETAINED_KEY_CHARS:
        return session_key
    return "sha256:" + hashlib.sha256(session_key.encode("utf-8", "surrogatepass")).hexdigest()


def note_shared_death(session_key: str) -> int:
    """Count one death of a process *session_key* was SHARING; return the streak.

    The counterpart to not charging a shared death to the session's own retry
    budget. Recovery still has to be bounded -- a process that dies on every
    replacement would otherwise be re-queued forever -- so the attempts are
    counted here instead, where they are attributed to the thing that is actually
    failing rather than to a session that did nothing.

    The CAP is deliberately not here. This module records; the caller that
    re-queues owns the policy and already holds its own ladder's limit, and a
    second copy of that number in a leaf module is a second number to keep in
    step.

    Recording a death MOVES the row to the end of the table, so the key-count
    bound above can only ever drop the row that has gone longest without one.
    Dropping an active row would zero the streak a caller's ladder is reading,
    and that ladder is the only bound a shared death has.
    """
    if not session_key:
        return 0
    stored = _store_key(session_key)
    streak = _shared_streaks.pop(stored, 0) + 1
    _shared_streaks[stored] = streak
    while len(_shared_streaks) > _SHARED_STREAK_MAX_KEYS:
        dropped = next(iter(_shared_streaks))
        dropped_streak = _shared_streaks.pop(dropped)
        logger.warning(
            "runtime_death: dropping %s's shared-death streak of %d to stay within "
            "%d tracked keys -- it had gone the longest without a death, but if it "
            "is still recovering, its next death starts from 1 and its caller's "
            "ladder restarts with it",
            dropped,
            dropped_streak,
            _SHARED_STREAK_MAX_KEYS,
        )
    return streak


def clear_shared_deaths(session_key: str) -> None:
    """Forget *session_key*'s shared-death streak: a turn on it completed.

    Any landed turn clears it, for the reason a landed turn clears the session's
    own recovery counter: the streak is a reading of whether this session can get
    work done at all, and a completed turn proves it can. That is also the
    conservative direction -- it can only let recovery continue, never stop it.
    """
    _shared_streaks.pop(_store_key(session_key), None)


def shared_deaths(session_key: str) -> int:
    """*session_key*'s current shared-death streak (0 when it has none)."""
    return _shared_streaks.get(_store_key(session_key), 0)


def _runtime_candidates(target: object) -> list[object]:
    """*target* and the runtimes reachable one hop in front of it.

    Two shapes reach these call sites: the runtime-backed session provider, which
    holds its runtime as ``_runtime``, and the outer provider that swaps one in
    as ``_client`` once startup completes. Duck-typed rather than imported by
    class, because this module sits below the ACP layer and the agent-SDK
    boundary check refuses knowledge of it, a type-only import included.
    """
    seen: list[object] = []
    for candidate in (
        target,
        getattr(target, "_runtime", None),
        getattr(getattr(target, "_client", None), "_runtime", None),
        getattr(target, "_client", None),
    ):
        if candidate is None:
            continue
        if any(candidate is existing for existing in seen):
            continue
        seen.append(candidate)
    return seen


def _pid_of(target: object) -> int | None:
    """The pid *target* carries, for the LOG, or None when it carries no real one.

    Rejects everything that is not a real, positive, non-init pid, the same way
    :mod:`kiro_crew.runtime_ownership` does and for the same reason: a ``Mock``
    attribute coerces to 1 through ``__index__``, so a test double would
    otherwise be logged as init.
    """
    pid = getattr(target, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    return pid


def _reset_for_tests() -> None:
    """Drop all death state. Tests only -- there is one record per gateway."""
    _deaths.clear()
    _shared_streaks.clear()


__all__ = [
    "RuntimeDeath",
    "announce",
    "caused_by_this_session",
    "clear_shared_deaths",
    "note_shared_death",
    "shared_deaths",
]
