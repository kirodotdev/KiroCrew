"""Runtime ownership: the pool owns the process, a session holds a LEASE.

A session must never be able to name a process. It asks for a runtime, gets a
LEASE, and gives the lease back; whether that release ends the process is the
pool's decision and nobody else's.

:class:`RuntimeOwnership`
    The lease table. A runtime lives while at least one lease is outstanding. The
    LAST release hands the runtime back for teardown; every earlier release only
    drops a reference. ``cap`` bounds how many leases one process serves, and so
    bounds the blast radius of that process dying.

:class:`RuntimeTenancy`
    The tenancy table. A LEASE is a session's claim on a runtime it MAY end; a
    TENANCY is a claim by a party that is using a process it may NOT end -- a
    session-sharing subagent mid-turn on its principal's runtime, an OAuth mint
    child mid-exchange. The two are separate because they answer different
    questions and are held by different parties, and because a tenancy has to
    outlive the lease: a principal's last release forgets the entry, and that is
    the exact moment a co-tenant becomes undefended.

:func:`authorize_runtime_kill`
    The gate. One place asks "does anyone still hold this?" before a runtime
    dies, and one place records WHO asked. Without the table the gate has nothing
    to consult; without the gate the table is advisory and any caller with a pid
    can still take a process out from under its tenants.

:class:`PidRefcount`
    The same rule one layer down, for the sweep shields: a pid stays shielded
    until its LAST holder drops it.

Why the gate does NOT read the sweep shield
-------------------------------------------
The sweep shield (``session_pid._PROTECTED_PIDS``) answers a different question,
and a gate that consulted it would refuse every ordinary teardown. Every
``AcpRuntime`` shields its OWN pid as it spawns and drops that shield only once
its kill has succeeded, and the app worker pool and the knowledge LLM pool shield
processes they own and recycle -- so the shield is held for the whole life of the
very process its owner is entitled to end. Reading it in the gate turns each of
those owners into a caller refused its own teardown, which leaks the process:
the failure this module's "why the gate and the release sites are one change"
note describes, one layer out.

So a self-shield is not a tenancy. A tenancy is taken by a party that is NOT the
process's owner, is named, and is released when that party is done -- which is
what lets the gate refuse on its behalf without ever refusing an owner.

Why the gate and the release sites are one change
-------------------------------------------------
The gate can only be switched on in the same change that makes releasing
universal. A gate wired to a provider that holds a lease for its lifetime also
refuses the force-kill paths -- ``_sync_kill_provider`` is reached through
``_dispatch_hard_kill`` and through the dashboard's reset-all fallback -- so a
teardown that does not release first is declined, and the process it declines to
signal leaks. Every path that legitimately ends a runtime therefore releases
before it signals, and that is why they all move together.

Why the gate is separate from the killing
-----------------------------------------
Ownership and identity are different questions and both must be answered:

* ownership -- "does anyone still need this runtime?" -- is THIS module, and a
  wrong answer kills a co-tenant's live process;
* identity -- "is this pid still the process I recorded?" -- is
  :mod:`kiro_crew.process_identity`, and a wrong answer kills a stranger that
  inherited a recycled pid.

So :func:`authorize_runtime_kill` returns a verdict rather than doing the
killing, and a caller that owns a careful escalation -- a SIGTERM grace before
SIGKILL, a process group proved by a vouching member, a Windows tree pinned by
handle -- keeps it and asks the gate once, at the top. Folding those into a
general kill helper would mean replacing a group id captured while the leader was
alive with one re-resolved from the pid at signal time, which is the identity
defect above.

This module is a LEAF on purpose: it imports nothing from ``kiro_crew``.
``session_pid`` is the pid bookkeeping leaf and calls the gate, so an import from
here into the agent layer would close ``session_pid -> acp -> acp.runtime ->
session_pid`` and raise ``ImportError`` on the first import of either
(``test_agent_lifecycle_cycle.py`` pins the absence), and the agent-SDK boundary
gate refuses such an import outright, type-only ones included.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Iterable, Iterator, MutableSet
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Leases one chat runtime may serve. ONE, so every chat session keeps its own
#: process: at this cap no acquisition can join an occupied runtime, so each
#: release is a last release and hands the process straight back. Raising it is a
#: behaviour change and needs the eligibility rules that decide WHICH sessions
#: may share a process, which do not live here.
CHAT_RUNTIME_CAP = 1


@runtime_checkable
class OwnedRuntime(Protocol):
    """What this table needs of a runtime: a pid, and whether it is still alive.

    A Protocol rather than an import of the concrete runtime class. The
    agent-SDK boundary gate refuses application code that reaches the ACP layer,
    type-only imports included -- and it is right to, because this module is
    reached FROM below. It is also the honest contract: the table reads a pid and
    asks whether the process is alive, and calls nothing else on what it holds.
    """

    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...


class PidRefcount(MutableSet[int]):
    """A pid shield that counts its holders instead of merely listing them.

    A ``set`` of shielded pids is wrong as soon as two holders shield one pid:
    the first to leave calls ``discard`` and tears the shield off a process the
    second is still using. This counts, so a pid stays shielded until the LAST
    holder drops it, and holders stay independent -- which is the same rule the
    lease table applies to runtimes, one layer up.

    Reads like the set it replaces (``in``, iteration, ``len``, truthiness,
    ``set(...)``, set algebra), so a holder that pairs every ``add`` with one
    ``discard`` needs no change. A pid whose count reaches zero is REMOVED rather
    than left at zero, so iteration and truthiness never report a pid nothing
    holds.
    """

    def __init__(self, initial: Iterable[int] | None = None) -> None:
        self._counts: Counter[int] = Counter()
        for pid in initial or ():
            self.add(pid)

    def add(self, value: int) -> None:
        """Take a reference on *value*; the first one raises the shield."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            self._counts[value] += 1

    def discard(self, value: int) -> None:
        """Drop ONE reference; the shield falls only when the last one goes."""
        remaining = self._counts.get(value)
        if remaining is None:
            return
        if remaining <= 1:
            del self._counts[value]
        else:
            self._counts[value] = remaining - 1

    def count(self, value: int) -> int:
        """How many holders *value* currently has (0 when unshielded)."""
        return self._counts.get(value, 0)

    def clear(self) -> None:
        """Drop every reference on every pid."""
        self._counts.clear()

    def __contains__(self, value: object) -> bool:
        return value in self._counts

    def __iter__(self) -> Iterator[int]:
        return iter(list(self._counts))

    def __len__(self) -> int:
        return len(self._counts)

    def __repr__(self) -> str:
        return f"PidRefcount({dict(self._counts)!r})"


@dataclass
class _Entry:
    """One live runtime and the LEASES held on it.

    Keyed by lease rather than by session key, because a session key is not a
    count. The allocator legitimately starts the same session twice at once (it
    carries a race budget for exactly that), and both starts must be releasable
    on their own: if the two shared one reference, the loser's teardown would
    drop it and the winner's live process would be killed mid-turn.
    """

    runtime: OwnedRuntime
    key: Hashable
    leases: dict[str, str] = field(default_factory=dict)

    def has_room(self, cap: int) -> bool:
        return len(self.leases) < cap

    def session_keys(self) -> list[str]:
        return sorted(set(self.leases.values()))


@dataclass
class Acquisition:
    """What :meth:`RuntimeOwnership.acquire` handed back.

    ``lease`` is this acquisition's own handle and the ONLY thing that releases
    it. Two acquisitions -- even for one session key -- get two leases, and the
    runtime dies when the last of them is gone.

    ``joined`` is False for the acquisition that FOUNDED the runtime and True for
    every one that landed on an existing one. Callers use it for the two
    decisions that differ: whether the provider owns the process, and whether
    per-session start work that rewrites process-level state may run.
    """

    runtime: OwnedRuntime
    joined: bool
    leases_on_runtime: int
    lease: str


class RuntimeOwnership:
    """The lease table: which runtimes exist and who still needs them.

    One lock serializes the whole registry rather than one lock per key. The
    critical section is a dict lookup plus, for a miss, one spawn, and holding it
    across that spawn is a deliberate trade of concurrency for simplicity: the
    bookkeeping this lock protects -- entry list, lease index, last-runtime map --
    stays provably consistent because nothing else can observe it mid-spawn.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._by_lease: dict[str, _Entry] = {}
        self._last_for_session: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    # -- writes --

    async def acquire(
        self,
        key: Hashable,
        session_key: str,
        spawn: Callable[[], Awaitable[OwnedRuntime]],
        *,
        cap: int = CHAT_RUNTIME_CAP,
    ) -> Acquisition:
        """Take a LEASE on a runtime, spawning one only when needed.

        Placement: the runtime this session key last landed on, when it is still
        alive, compatible and has room -- which keeps a restart on the process
        its history is on -- then the first compatible runtime with room,
        otherwise ``spawn()``. Compatible means an equal *key*, and room means
        fewer than *cap* leases are held on it.

        Every call mints its OWN lease, including a second call for a session key
        that already holds one. That is the point rather than an oversight: the
        allocator legitimately starts one session twice at once, and each start
        has to be releasable without ending the other.

        At ``cap=1`` no entry with a lease has room, so every acquisition spawns
        and serves exactly one lease -- which is the unpooled behaviour, with the
        lease recorded.

        A dead runtime is never handed out and never counted: it is dropped on the
        way past, so the caller that finds none alive spawns, and the sessions
        that were on it rejoin through this same path on their own next turn.
        """
        async with self._lock:
            self._drop_dead_locked()
            entry = self._pick_locked(key, cap=cap, session_key=session_key)
            joined = entry is not None
            if entry is None:
                runtime = await spawn()
                entry = _Entry(runtime=runtime, key=key)
                self._entries.append(entry)
            lease = uuid.uuid4().hex
            entry.leases[lease] = session_key
            self._by_lease[lease] = entry
            self._last_for_session[session_key] = entry
            logger.info(
                "runtime_ownership outcome=%s pid=%s leases=%d cap=%d runtimes=%d",
                "joined" if joined else "spawned",
                _pid_of(entry.runtime),
                len(entry.leases),
                cap,
                len(self._entries),
            )
            return Acquisition(
                runtime=entry.runtime,
                joined=joined,
                leases_on_runtime=len(entry.leases),
                lease=lease,
            )

    async def release(self, lease: str) -> OwnedRuntime | None:
        """Drop ONE lease; return the runtime to kill only if it was the last.

        The runtime is returned rather than killed here so the caller performs
        the teardown it already performs, with its own logging and error
        handling. Returns None while any other lease is outstanding, which is
        what stops one session's close, reset or model switch -- or the teardown
        of a start that lost a same-key race -- from killing a process another
        acquisition is still using.

        At ``cap=1`` there is never another lease, so this always returns the
        runtime and the caller always kills it, exactly as it did before the
        table existed.

        Releasing is not optional for a caller that is about to signal. The gate
        refuses a runtime whose lease is still out, so a kill path that skips its
        release refuses its own teardown and leaks the process.
        """
        async with self._lock:
            entry = self._by_lease.pop(lease, None)
            if entry is None:
                return None
            entry.leases.pop(lease, None)
            if entry.leases:
                logger.info(
                    "runtime_ownership outcome=released pid=%s remaining_leases=%d",
                    _pid_of(entry.runtime),
                    len(entry.leases),
                )
                return None
            self._forget_entry_locked(entry)
            logger.info(
                "runtime_ownership outcome=last_release pid=%s",
                _pid_of(entry.runtime),
            )
            return entry.runtime

    # -- reads --

    def leases_on_pid(self, pid: int) -> int:
        """How many leases LIVE runtimes on *pid* hold, for the kill gate.

        A dead runtime's leases are not counted, and that is the difference
        between a gate and a lock. A process that has already exited cannot be
        harmed by the signal, while refusing to signal it suppresses the reap of
        its zombie and the sweep of the descendants that escaped its group --
        which is the leak the teardown exists to stop. So death releases
        ownership, and only a live process can be defended.
        """
        total = 0
        for entry in self._entries:
            if not entry.leases:
                continue
            if _pid_of(entry.runtime) != pid:
                continue
            if not self._alive(entry):
                continue
            total += len(entry.leases)
        return total

    def lease_keys_on_pid(self, pid: int) -> frozenset[str]:
        """WHICH sessions hold a live lease on *pid*, for the auth boundary.

        The keys, not a count. :meth:`leases_on_pid` answers the kill gate, which
        only needs to know whether anyone would be harmed by a signal; a caller
        deciding whether a DECLARED session key belongs on this process needs the
        names, because a count admits whatever key it is handed.

        The same liveness rule as :meth:`leases_on_pid`, for the same reason: a
        dead runtime's leases are on their way out of the table, and reporting them
        would name sessions on a process that has already exited.

        An EMPTY set is "this table knows of no session here", never "no session
        is here". Every legitimate caller whose lease lives in a table this
        process does not own -- and every caller with no lease at all, which is
        every tenant -- answers empty, so a consumer must treat empty as absence
        of evidence.
        """
        keys: set[str] = set()
        for entry in self._entries:
            if not entry.leases:
                continue
            if _pid_of(entry.runtime) != pid:
                continue
            if not self._alive(entry):
                continue
            keys.update(k for k in entry.leases.values() if k)
        return frozenset(keys)

    def leases_on_runtime(self, runtime: object) -> int:
        """How many leases this exact runtime OBJECT holds, for the kill gate.

        Identity, not pid: it answers for the object the caller is about to kill
        even before that object has a pid, and it cannot be confused by a second
        runtime that later inherits the same number. Dead runtimes are excluded
        for the reason :meth:`leases_on_pid` gives.
        """
        for entry in self._entries:
            if entry.runtime is not runtime:
                continue
            if not entry.leases or not self._alive(entry):
                return 0
            return len(entry.leases)
        return 0

    # -- internals --

    def _pick_locked(self, key: Hashable, *, cap: int, session_key: str) -> _Entry | None:
        sticky = self._last_for_session.get(session_key)
        if (
            sticky is not None
            and any(e is sticky for e in self._entries)
            and sticky.key == key
            and sticky.has_room(cap)
            and self._alive(sticky)
        ):
            return sticky
        for entry in self._entries:
            if entry.key == key and entry.has_room(cap) and self._alive(entry):
                return entry
        return None

    @staticmethod
    def _alive(entry: _Entry) -> bool:
        probe = getattr(entry.runtime, "is_alive", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            return False

    def _drop_dead_locked(self) -> None:
        for entry in list(self._entries):
            if not self._alive(entry):
                orphaned = entry.session_keys()
                self._forget_entry_locked(entry)
                if orphaned:
                    logger.warning(
                        "runtime_ownership outcome=dead_runtime_dropped pid=%s sessions=%d",
                        _pid_of(entry.runtime),
                        len(orphaned),
                    )

    def _forget_entry_locked(self, entry: _Entry) -> None:
        """Drop every reference to *entry*, in any order its callers use.

        The session-key index is pruned BY VALUE rather than by asking the entry
        which keys it holds. Those keys are derived from the lease map, and
        ``release`` empties that map before it forgets the entry -- so a
        key-derived prune finds nothing and strands one index row, holding the
        entry and its runtime, per session key. At ``cap=1`` every release is a
        last release, which makes that one stranded row per teardown for the
        gateway's life. Scanning by value costs one pass over an index the size of
        the live session count and cannot be defeated by call order.
        """
        for lease in list(entry.leases):
            if self._by_lease.get(lease) is entry:
                del self._by_lease[lease]
        for session_key, held in list(self._last_for_session.items()):
            if held is entry:
                del self._last_for_session[session_key]
        entry.leases.clear()
        self._entries = [e for e in self._entries if e is not entry]


#: The gateway's one runtime ownership table. A module-level singleton because
#: the ownership question is global: a kill gate can only refuse on behalf of a
#: lease that was taken in the same registry it consults.
RUNTIME_OWNERSHIP = RuntimeOwnership()


class RuntimeTeardownCommitted(RuntimeError):
    """A tenancy was asked for on a process whose teardown has already committed.

    Its own type, rather than the ``None`` that means "nothing to defend", because
    the two need opposite handling: None is the answer for a target a signal could
    not reach anyway, while this says a live process is being ended and any defence
    granted now would be a lie to its holder.
    """


@dataclass
class _Claim:
    """One party's claim on a process it is using but does not own.

    ``owed`` records that a kill of this claim's target was REFUSED while this
    claim was held, which is the positive evidence the hand-back needs. It rides
    on the claim rather than in a table keyed by pid so that the debt cannot
    outlive the tenancy that earned it: a pid the OS later hands to a different
    process reaches a different claim record, and a process the gate never
    refused for has no record carrying the mark at all.
    """

    target: object
    pid: int | None
    holder: str
    owed: bool = False
    #: The SESSION this claim is taken for, or ``""`` when the claimer is not a
    #: session at all (the OAuth mint child). Recorded beside ``holder`` rather
    #: than parsed back out of it: ``holder`` is a label written to be read in a
    #: refusal log and free to change its wording, and an auth decision must not
    #: rest on a log format. A claimer that names no session is simply not
    #: reported by :meth:`RuntimeTenancy.tenant_keys_on_pid`, which is correct --
    #: it has no session key to bind and never declares one.
    session_key: str = ""


class RuntimeTenancy:
    """Who is USING a process without owning it, so the gate can refuse for them.

    A lease says "this session may end this runtime". A tenancy says "I am
    mid-flight on this process and a signal would destroy work", and the two
    cannot be one table:

    * a tenant may hold no lease at all and must not be given one -- at
      ``cap=1`` an acquisition cannot join an occupied runtime, so a subagent
      asking for a lease would either be placed on a second process or found a
      duplicate entry for the one it is already sharing;
    * a tenancy must SURVIVE the last lease release. That release is precisely
      when a co-tenant becomes undefended: it forgets the entry and hands the
      runtime to its owner to kill, while the subagent's turn is still running on
      it. A claim stored on the entry would be destroyed by the event it exists
      to survive;
    * a tenant may be a process with no entry in the lease table at all. The
      OAuth mint child is a plain ``kiro-cli`` child of a Connect flow, not a
      pooled chat runtime, so there is nothing to attach a lease to and the claim
      has to be answerable by bare pid.

    Claims are COUNTED and NAMED. Counted for the reason :class:`PidRefcount`
    gives -- two subagents legitimately share one principal's runtime, and the
    first to finish must not strip the defence off the second. Named because a
    refusal is only actionable if it says who is keeping the process alive; an
    unpaired claim is then a leak whose owner the log identifies.

    Placement and ``cap`` are untouched by design: nothing here is consulted by
    :meth:`RuntimeOwnership.acquire`, so a runtime serving one session and no
    subagents behaves exactly as it did before this table existed.
    """

    def __init__(self) -> None:
        self._claims: dict[str, _Claim] = {}
        #: How many claims this pid has EVER taken. A killer reads it beside the
        #: gate's verdict and hands it back to :meth:`commit_teardown`: the
        #: verdict is computed on one thread and consumed on another, so a claim
        #: that lands in between would otherwise be invisible to a kill already
        #: authorized.
        self._epochs: dict[int, int] = {}
        #: Pids whose teardown has COMMITTED -- a signal is about to be, or has
        #: already been, delivered. A claim on one of these is refused rather than
        #: granted, because the first signal cannot be recalled: a claim accepted
        #: after SIGTERM leaves is delivered would hand its holder a defence that
        #: defends nothing, and the turn it belongs to dies on a process it
        #: believed was protected. Entries are removed when the teardown finishes.
        self._committed: set[int] = set()
        # A threading lock, not an asyncio one: claims are taken and dropped from
        # SYNCHRONOUS code (the mint's pid claim, the gate itself), so an awaitable
        # lock could not be acquired at the call sites that need it.
        self._lock = threading.Lock()

    def claim(self, target: object, *, holder: str, session_key: str = "") -> str | None:
        """Record that *holder* is using *target*, and return the claim's handle.

        *session_key* names the SESSION the claim is taken for, and is what the
        auth boundary reads back: a claimer that declares a session key over the
        dashboard socket must appear in this table under that same key. It is a
        separate argument rather than something derived from *holder* because
        *holder* is a refusal-log label, and a caller that legitimately holds no
        session -- the OAuth mint child -- must pass nothing rather than a
        made-up name. Defaulted so the existing callers and the mint keep their
        exact shape; a claim with no key is defended by the gate exactly as
        before and is simply not reported as a binding.

        None means there was nothing to defend, and the caller needs no branch for
        it: :meth:`release` is a no-op for None. That is the answer whenever no pid
        can be resolved -- a number this module refuses to treat as a process (see
        :func:`_pid_of`), or an object that carries none. A tenant is by definition
        mid-flight on a RUNNING process, so a target with no pid is not an early
        claim to honour: there is nothing a signal could reach, and defending it
        would only stand in the way of the teardown of whatever it becomes.

        *target* SHOULD be the object -- a runtime or a client -- rather than its
        pid. The object carries a liveness probe, which is what stops an unpaired
        claim from defending a pid the OS has since handed to someone else, and it
        answers by identity even when a second process later inherits its number.

        Raises :class:`RuntimeTeardownCommitted` when a teardown of this pid has
        already committed. That refusal has to be DISTINCT from the None above,
        which says "nothing to defend, no branch needed": here there is a process
        and it is being ended, so a caller that read None would proceed believing
        its work is protected. Granting the claim cannot help it -- the first
        signal is already out and no table can recall it -- so the only honest
        answer is to say the process is gone and let the caller get another one.
        """
        pid = self._pid_of_target(target)
        if pid is None:
            return None
        handle = uuid.uuid4().hex
        with self._lock:
            if pid in self._committed:
                # Read under the SAME lock the commit takes, which is what makes
                # the barrier a barrier: a check outside it could pass just as the
                # killer commits, and the claim would then be inserted behind a
                # teardown that has already stopped looking for tenants.
                raise RuntimeTeardownCommitted(
                    f"pid {pid} is being torn down; a tenancy on it would defend nothing"
                )
            # A refusal already standing on this process is INHERITED, in the same
            # critical section as the insert. The teardown the gate declined still
            # has to happen once the process is idle, and the tenant that performs
            # it is whichever one leaves last -- which may be a turn that started
            # after the refusal. Without this the debt would leave with the claims
            # that were held at refusal time and the runtime would be stranded:
            # unowned, self-shielded against the sweep, and never visited again.
            #
            # Only a LIVE claim's debt is inheritable, the same pairing every other
            # reader of :meth:`_names` uses. A claim matched by bare pid may belong
            # to a process that has already exited, and the OS is free to hand that
            # number to something unrelated; inheriting a dead claim's mark would
            # arm this tenant to hand back a runtime whose refused teardown already
            # happened by other means, so the hand-back would end a process the
            # refusal was never about. A dead claim's debt is also moot: the
            # teardown it recorded has nothing left to tear down.
            owed = any(
                self._names(c, target, pid) and self._alive(c) and c.owed
                for c in self._claims.values()
            )
            self._claims[handle] = _Claim(
                target=target,
                pid=pid,
                holder=str(holder),
                owed=owed,
                session_key=str(session_key or ""),
            )
            # Bumped INSIDE the lock, with the insert. A killer that read the
            # epoch before this claim existed re-reads it before it signals and
            # finds it moved, which is what stops an authorized kill from landing
            # on a turn that started after the verdict was computed.
            self._epochs[pid] = self._epochs.get(pid, 0) + 1
        logger.info(
            "runtime_tenancy outcome=claimed pid=%s holder=%s claims=%d",
            pid,
            holder,
            self.claims_on(target),
        )
        return handle

    def release(self, handle: str | None) -> object | None:
        """Drop ONE claim; return the runtime to tear down only if a kill is owed.

        A returned object means a kill was REFUSED on this target while a claim was
        held, this was the last claim, and no lease owns it either -- so the
        teardown declined on this tenant's behalf has nobody left to perform it,
        and the tenant that just left is that somebody.

        The recorded refusal is the whole condition, and it has to be, because
        lease absence means nothing here: NO shared runtime holds a lease. The
        companion runtime is spawned bare, the task-run path forces
        ``_owns_runtime = False`` on the provider it hands the runtime to, and
        ``acquire_session_lease`` is reached from the chat registration path alone.
        So "unleased" describes every healthy shared runtime, and a hand-back keyed
        on it would end a task run's runtime at the close of its first step -- with
        the run-scoped map still pointing at it and its real teardown still to come
        -- and would take a sibling subagent's process out from under it between
        turns.

        Without any hand-back the refusal would instead become a leak: a
        principal's teardown releases its lease, is REFUSED because a subagent is
        mid-turn, and returns without signalling; the subagent then finishes onto a
        process that no session owns, that the sweep skips because the runtime
        still holds its own shield, and that no later caller has a reason to visit.
        Keying on the refusal covers that case and no other.

        The refusal is read off this claim record, so it cannot be spent on the
        wrong process: a pid the OS reassigns later reaches a different record, and
        a record only carries the mark if the gate refused while it -- or a claim it
        inherited from on the same process -- was held.

        Returns None for a claim taken on a bare pid: a number cannot be torn
        down through this interface, and the only such claimer -- the mint -- ends
        its own child itself.
        """
        if handle is None:
            return None
        with self._lock:
            claim = self._claims.pop(handle, None)
        if claim is None:
            return None
        target = claim.target
        remaining = self.claims_on(target)
        logger.info(
            "runtime_tenancy outcome=released pid=%s holder=%s remaining_claims=%d",
            claim.pid,
            claim.holder,
            remaining,
        )
        if remaining:
            return None
        if isinstance(target, int) or claim.pid is None:
            return None
        if not claim.owed:
            return None
        if outstanding_leases(target):
            return None
        logger.warning(
            "runtime_tenancy outcome=kill_owed_handback pid=%s holder=%s: "
            "a kill was refused for this tenant and nobody else can complete it",
            claim.pid,
            claim.holder,
        )
        return target

    def refuse_for_tenants(self, target: object) -> tuple[int, list[str]]:
        """Decide a tenancy refusal and record its debt in ONE critical section.

        Returns how many live tenants stand in the way and who they are. A count
        of zero means nothing was recorded and the caller may proceed.

        Deciding and recording together is the whole point of this method, rather
        than a count read here and a debt written there. The gate runs on an
        executor thread while claims are taken on the event loop, so between a
        separate read and write the last tenant can leave -- and the debt then
        written belongs to a process with no tenant, to be consumed by whoever
        claims that pid next. Marking the claim records the count was taken from
        makes the two one act, and keeps the debt on the tenancy it came from: a
        later process inheriting this pid has its own records, and none of them
        carries the mark.

        The liveness probe runs under the lock, which the counting readers
        deliberately avoid. It is a non-blocking check on a handle this table
        already holds, and the alternative is precisely the gap above.
        """
        pid = self._pid_of_target(target)
        with self._lock:
            live = [
                claim
                for claim in self._claims.values()
                if self._names(claim, target, pid) and self._alive(claim)
            ]
            for claim in live:
                claim.owed = True
            return len(live), sorted({claim.holder for claim in live})

    def epoch(self, target: object) -> int:
        """How many claims *target*'s pid has taken, for a killer's re-validation.

        A killer reads this beside the gate's verdict and again immediately before
        it signals. Those two reads share a thread with each other but NOT with a
        claim: ``_sync_kill_provider`` runs on an executor thread while a turn
        claims on the event loop, and between the verdict and the first signal sit
        a start-id read, a group resolution and an unbounded descendant walk. A
        claim landing in that window is invisible to an already-computed verdict,
        and the turn it belongs to dies.
        """
        pid = self._pid_of_target(target)
        if pid is None:
            return 0
        with self._lock:
            return self._epochs.get(pid, 0)

    def commit_teardown(self, target: object, epoch: int) -> bool:
        """Close this pid to new tenants and answer whether the signal may go.

        The LAST check a killer makes and the barrier it raises, in one critical
        section, because those cannot be two acts. An authorized verdict is a
        statement about the past: it is computed on an executor thread while a turn
        claims on the event loop, and between the verdict and the first signal sit a
        start-id read, a group resolution and an unbounded descendant walk. Checking
        there and signalling here leaves that window open -- and re-checking before
        each signal, which is what this replaces, only ever abandoned the LATER
        SIGKILL. The SIGTERM was already delivered, its seconds-long grace is by this
        function's own reckoning ample time for a shared turn to start, and nothing
        can recall a signal. So the window has to be CLOSED rather than narrowed.

        False on either of two grounds, and the caller must not signal:

        * a live tenant holds this pid now -- the ordinary refusal;
        * a claim arrived and left since *epoch*. A turn that ran after the verdict
          means the verdict describes a state the process has left, and one deferred
          teardown -- the next drain revisits it -- is cheaper than one killed turn.

        True raises the barrier, and the caller MUST pair it with
        :meth:`release_teardown` on every exit. An unreleased barrier is a pid no
        tenant can ever claim again, which is the leak that made a reservation
        released at each of the killer's early exits the wrong shape; one release in
        a ``finally`` around the whole signalling section is the right one.
        """
        pid = self._pid_of_target(target)
        if pid is None:
            return False
        with self._lock:
            live = any(
                self._names(claim, target, pid) and self._alive(claim)
                for claim in self._claims.values()
            )
            if live or self._epochs.get(pid, 0) != epoch:
                return False
            self._committed.add(pid)
            return True

    def release_teardown(self, target: object) -> None:
        """Drop the barrier :meth:`commit_teardown` raised, on every exit path.

        Idempotent and silent about a pid that holds none, so a ``finally`` can call
        it without knowing whether the commit succeeded.
        """
        pid = self._pid_of_target(target)
        if pid is None:
            return
        with self._lock:
            self._committed.discard(pid)

    def claims_on(self, target: object) -> int:
        """Live claims on *target*, by object identity when it is not a pid."""
        if not isinstance(target, int):
            held = self.claims_on_runtime(target)
            if held:
                return held
        pid = self._pid_of_target(target)
        if pid is None:
            return 0
        return self.claims_on_pid(pid)

    def claims_on_pid(self, pid: int) -> int:
        """How many live claims name *pid*, for the kill gate.

        A dead target's claims are not counted, for the reason
        :meth:`RuntimeOwnership.leases_on_pid` gives: refusing to signal a process
        that has already exited suppresses the reap of its zombie and the sweep of
        the descendants that escaped its group, which is the leak the teardown
        exists to stop.
        """
        with self._lock:
            claims = list(self._claims.values())
        return sum(1 for c in claims if c.pid == pid and self._alive(c))

    def tenant_keys_on_pid(self, pid: int) -> frozenset[str]:
        """WHICH sessions hold a live claim on *pid*, for the auth boundary.

        The counterpart of :meth:`RuntimeOwnership.lease_keys_on_pid`, and the
        half that matters most there: a session-sharing sub-agent holds no lease
        and is never registered with the session manager, so this table is the
        ONLY account of it the gateway has. Reported from ``session_key`` rather
        than from ``holder`` for the reason :class:`_Claim` gives.

        Live claims only, matching every other reader of this table: a claim whose
        process has exited names a session that is not on that process.

        Empty means this table knows of no session on the pid. That is the answer
        for a pid whose only claimer is the mint child, and for every pid with no
        claims at all, so a consumer must read it as absence of evidence rather
        than as a statement that nobody is there.
        """
        with self._lock:
            claims = list(self._claims.values())
        return frozenset(
            c.session_key for c in claims if c.pid == pid and c.session_key and self._alive(c)
        )

    def claims_on_runtime(self, runtime: object) -> int:
        """How many live claims name this exact OBJECT, for the kill gate.

        Identity, not pid, so a claim cannot be confused by a second process that
        later inherits the same number. It says nothing about a target with no pid:
        :meth:`claim` refuses those, because a tenant is mid-flight on a running
        process and there is nothing a signal could reach.
        """
        with self._lock:
            claims = list(self._claims.values())
        return sum(1 for c in claims if c.target is runtime and self._alive(c))

    @staticmethod
    def _names(claim: _Claim, target: object, pid: int | None) -> bool:
        """Whether *claim* is about *target*, by object identity or by its pid.

        Both, because the two callers hold different things: a tenant claims the
        OBJECT it is running a turn on, while ``_sync_kill_provider`` asks the gate
        about a BARE PID it resolved from a handle. A claim on a sibling object
        that resolves to the same pid counts too -- it is the same process, and
        the signal would reach it either way.
        """
        if claim.target is target:
            return True
        return pid is not None and claim.pid == pid

    @staticmethod
    def _pid_of_target(target: object) -> int | None:
        """The pid *target* names, reading the ACP client's private slot as well.

        :func:`_pid_of` is deliberately left alone: it serves the LEASE path, and
        widening it there would change which processes an existing lease lookup
        resolves. This adds one fallback for this table only, because the shapes
        that take tenancies disagree on the spelling -- ``AcpRuntime`` publishes
        ``pid`` as a property while ``AcpClient``, which is what a Connect flow
        holds for its mint child, keeps only ``_pid``.

        Resolving it matters rather than being cosmetic: ``_sync_kill_provider``
        asks the gate with a BARE PID, so a claim that failed to record one would
        be invisible to the very killer this table has to refuse.
        """
        direct = _pid_of(target)
        if direct is not None:
            return direct
        return _pid_of(getattr(target, "_pid", None))

    @staticmethod
    def _alive(claim: _Claim) -> bool:
        """Whether the claimed process is still running.

        Both probe names are accepted because both shapes take claims: the shared
        ACP runtime answers ``is_alive`` and the mint's client answers
        ``is_process_alive``. A target with neither is treated as alive -- the
        table must not invent a death it cannot observe, and a claim on such a
        target is bounded by its holder's own release.
        """
        for name in ("is_alive", "is_process_alive"):
            probe = getattr(claim.target, name, None)
            if callable(probe):
                try:
                    return bool(probe())
                except Exception:
                    return False
        return True

    def _reset(self) -> None:
        """Drop every claim, epoch and teardown barrier. Tests only.

        Refusal debt needs no separate clearing: it lives on the claim records, so
        dropping those drops it. The barrier does, because it is keyed by pid: a
        test that leaves one standing would make every later claim on that number
        raise.
        """
        with self._lock:
            self._claims.clear()
            self._epochs.clear()
            self._committed.clear()


#: The gateway's one tenancy table, a singleton for the same reason
#: :data:`RUNTIME_OWNERSHIP` is: the gate can only refuse on behalf of a claim
#: taken in the registry it consults.
RUNTIME_TENANCY = RuntimeTenancy()


def _pid_of(target: object) -> int | None:
    """The pid *target* names, whether it IS one or merely carries one.

    Rejects everything that is not a real, positive, non-init pid. Test
    stand-ins are the sharp edge: a ``Mock`` attribute coerces to 1 through
    ``__index__``, and a pid of 1 or below also selects the ``kill(0)`` /
    ``kill(-n)`` process-group semantics rather than one process.
    """
    pid = target if isinstance(target, int) else getattr(target, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    return pid


def session_keys_bound_to_pid(pid: int) -> frozenset[str]:
    """Every session this gateway records as living on *pid*, from BOTH tables.

    The union, because the two tables hold different populations and neither is
    the whole answer: a chat session appears as a LEASE, and a session-sharing
    sub-agent or task run appears as a TENANCY with no lease at all. A check
    written against either one alone would refuse exactly the callers the other
    table exists for.

    This is the gateway's own memory, in this process, about runtimes it owns --
    which is what makes it usable at an auth boundary. The roster
    :mod:`kiro_crew.session_pid_sig` publishes answers the same question on disk,
    in a directory the agent sandbox can write, and the session manager's
    snapshot omits a shared sub-agent's session entirely.

    An EMPTY set is NO EVIDENCE, and every consumer owes that reading. A pid this
    gateway did not place a session on -- a warm-pool runtime before its claim, a
    pooled MCP backend, a cron script, anything belonging to another install --
    answers empty, and so does a pid whose only claimer named no session. Absence
    here says the tables cannot speak about the pid, never that the caller does
    not belong on it.
    """
    return RUNTIME_OWNERSHIP.lease_keys_on_pid(pid) | RUNTIME_TENANCY.tenant_keys_on_pid(pid)


def outstanding_leases(target: object) -> int:
    """Leases held on *target*, by object identity when it is a runtime.

    Identity first because it is the stronger answer and is available earlier: a
    runtime object is the thing the caller holds, while its pid can be absent
    before a spawn and can name a different process after an exit.
    """
    if not isinstance(target, int):
        held = RUNTIME_OWNERSHIP.leases_on_runtime(target)
        if held:
            return held
    pid = _pid_of(target)
    if pid is None:
        return 0
    return RUNTIME_OWNERSHIP.leases_on_pid(pid)


def note_runtime_kill(target: object, *, reason: str, caller: str) -> None:
    """Record that a runtime is about to be killed, and by whom.

    Called at the DECISION point, before anything is signalled, which is what
    makes it usable: the existing death log is written where the process is
    reaped, so it says a process died without saying who ended it.

    WARNING, and not debug chatter. The gateway runs at WARNING, so INFO here
    would mean the one line naming who fired is missing from the log of every
    deployment that has the problem.
    """
    logger.warning(
        "runtime kill pid=%s caller=%s reason=%s",
        _pid_of(target),
        caller,
        reason,
    )


def claim_runtime_tenancy(target: object, *, holder: str, session_key: str = "") -> str | None:
    """Defend *target* from the kill gate while *holder* is using it.

    For a party that is mid-flight on a process it does not own and must not end:
    a session-sharing subagent running a turn on its principal's runtime, a
    Connect flow's mint child performing its token exchange. Pair every call with
    :func:`release_runtime_tenancy`, and hold the claim for as long as -- and no
    longer than -- a signal would destroy work.

    A claimer that IS a session passes *session_key*, which also binds it: the
    dashboard's peer check admits a declared key on a shared process only while
    one of the two ownership tables names it, and for a sub-agent this claim is
    the only entry there is. A claimer that is not a session passes nothing.

    Raises :class:`RuntimeTeardownCommitted` when this pid's teardown has already
    committed; see :meth:`RuntimeTenancy.claim` for why that cannot be the same
    answer as the None it returns for a target with nothing to defend.
    """
    return RUNTIME_TENANCY.claim(target, holder=holder, session_key=session_key)


def release_runtime_tenancy(handle: str | None) -> object | None:
    """Stop defending a process; return it when this claim was the last light on.

    A returned runtime is ORPHANED: no lease owns it and no tenancy uses it, so
    the caller that just left must tear it down. See
    :meth:`RuntimeTenancy.release` for why that hand-back is not optional.
    """
    return RUNTIME_TENANCY.release(handle)


def tenancy_epoch(target: object) -> int:
    """A killer's token: how many tenancy claims *target*'s pid has ever taken.

    Read it right after the gate authorizes and hand it to
    :func:`commit_runtime_teardown` immediately before the first signal. The
    verdict and the signal are separated by real work on a thread that does not
    own the claim, so a verdict alone is a statement about the past.
    """
    return RUNTIME_TENANCY.epoch(target)


def commit_runtime_teardown(target: object, epoch: int) -> bool:
    """Close *target*'s pid to new tenants, and answer whether the signal may go.

    The killer's last check and its barrier in one act. False means abandon the
    kill; True means signal, and PAIR IT with :func:`release_runtime_teardown` in a
    ``finally`` covering every exit -- a barrier left standing is a pid no tenant
    can claim for the life of the gateway. See :meth:`RuntimeTenancy.commit_teardown`
    for why narrowing the window is not enough to close it.
    """
    return RUNTIME_TENANCY.commit_teardown(target, epoch)


def release_runtime_teardown(target: object) -> None:
    """Drop the teardown barrier on *target*'s pid. Idempotent, for a ``finally``."""
    RUNTIME_TENANCY.release_teardown(target)


def authorize_runtime_kill(target: object, *, reason: str, caller: str) -> bool:
    """The ONE ownership gate a runtime kill passes, and where the shot is logged.

    False means REFUSED, on either of two grounds:

    * a live runtime still has LEASES outstanding, so the caller holds a pid it
      does not own. A caller that legitimately ends this runtime releases its
      lease first and is then authorized; one that signals without releasing is
      refusing its own teardown;
    * a live process still has TENANCIES outstanding -- somebody who does not own
      it is mid-flight on it. That refusal is not a caller's mistake: the owner
      may well have released correctly, and the answer is to let the tenant
      finish. The tenant's own last release hands the orphan back for teardown.

    Both outcomes are logged at WARNING -- the allow path through
    :func:`note_runtime_kill`, so there is one attribution implementation and the
    refusal is the only thing this adds.
    """
    held = outstanding_leases(target)
    if held:
        logger.warning(
            "runtime_ownership REFUSED kill pid=%s leases=%d caller=%s reason=%s: "
            "the process is still leased, so this caller must release before it signals",
            _pid_of(target),
            held,
            caller,
            reason,
        )
        return False
    # One call, because the count and the debt it justifies must be one act: a
    # count read here and a debt recorded in a second call can straddle the last
    # tenant's departure, and the debt left behind then belongs to a process that
    # has none -- to be spent on whichever runtime holds that pid later.
    tenants, holders = RUNTIME_TENANCY.refuse_for_tenants(target)
    if tenants:
        logger.warning(
            "runtime_ownership REFUSED kill pid=%s tenants=%d holders=%s caller=%s reason=%s: "
            "a party that does not own this process is still using it",
            _pid_of(target),
            tenants,
            ",".join(holders) or "unnamed",
            caller,
            reason,
        )
        return False
    note_runtime_kill(target, reason=reason, caller=caller)
    return True


#: Sentinel for "this object has no lease slot at all", distinct from a slot
#: holding None, which is a real holder that currently leases nothing.
_MISSING: object = object()


def _lease_holder(provider: object) -> object | None:
    """The object on *provider* that can hold a runtime lease, or None.

    Two shapes reach the kill paths: the runtime-backed session provider itself,
    and the outer provider that swaps one in as ``_client`` once startup
    completes. Duck-typed rather than imported by class, because this module sits
    below the ACP layer and the agent-SDK boundary check refuses knowledge of it,
    a type-only import included.

    A holder is recognised by its lease SLOT, not by having the two methods. Test
    stand-ins are the sharp edge here exactly as they are for pids: a ``MagicMock``
    answers every ``hasattr`` and returns a ``MagicMock`` from the call, which is
    not awaitable -- so a method-shaped check turns every mocked provider on these
    paths into a TypeError. A real holder's slot is ``None`` or the lease string.
    """
    for candidate in (provider, getattr(provider, "_client", None)):
        if candidate is None:
            continue
        slot = getattr(candidate, "_runtime_lease", _MISSING)
        if slot is not _MISSING and (slot is None or isinstance(slot, str)):
            return candidate
    return None


async def acquire_session_lease(provider: object) -> None:
    """Record a registered session's claim on its runtime. No-op for other shapes.

    Called where the session joins the registry, so the lease and registry
    membership mean the same thing. A provider with no runtime to lease -- a
    non-ACP backend, a placeholder client before startup swapped in the real one
    -- simply has no holder and is skipped.
    """
    holder = _lease_holder(provider)
    if holder is not None:
        await holder.acquire_runtime_lease()  # type: ignore[attr-defined]


async def release_session_lease(provider: object) -> None:
    """Give up a session's claim on its runtime. Idempotent; no-op for other shapes.

    For the paths that end a session WITHOUT a graceful ``shutdown`` -- a failure
    after registration, the dashboard's force-kill fallback. A path that shuts the
    provider down normally has already released inside ``shutdown``.
    """
    holder = _lease_holder(provider)
    if holder is not None:
        await holder.release_runtime_lease()  # type: ignore[attr-defined]


def _reset_for_tests() -> None:
    """Drop all ownership state. Tests only -- there is one registry per gateway."""
    RUNTIME_OWNERSHIP._entries.clear()
    RUNTIME_OWNERSHIP._by_lease.clear()
    RUNTIME_OWNERSHIP._last_for_session.clear()
    RUNTIME_TENANCY._reset()


__all__ = [
    "Acquisition",
    "CHAT_RUNTIME_CAP",
    "OwnedRuntime",
    "PidRefcount",
    "RUNTIME_OWNERSHIP",
    "RUNTIME_TENANCY",
    "RuntimeOwnership",
    "RuntimeTeardownCommitted",
    "RuntimeTenancy",
    "acquire_session_lease",
    "authorize_runtime_kill",
    "claim_runtime_tenancy",
    "commit_runtime_teardown",
    "note_runtime_kill",
    "outstanding_leases",
    "release_runtime_teardown",
    "release_runtime_tenancy",
    "release_session_lease",
    "session_keys_bound_to_pid",
    "tenancy_epoch",
]
