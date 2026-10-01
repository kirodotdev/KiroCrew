"""Reconcile the kernel's process list against the runtime ownership registry.

Every other sweep in this gateway asks one question -- "is this pid one I still
need?" -- and asks it of a record. This one asks the kernel instead, and compares
the two answers in BOTH directions, because each direction is a different leak:

alive but unowned
    A process runs inside this install's own agent slice and no record claims it.
    Nothing is ever going to end it: the sweeps that could are driven by the very
    records it is missing from, so it survives every pass and every gateway
    restart. Measured on a live instance: 20 such processes at rest, 57-97 under
    load.

owned but dead
    A record names a pid that is gone, or one the kernel has given to an unrelated
    process. Whoever holds the lease is still waiting on a runtime that cannot
    answer, and the record keeps a pid number reserved against a process that will
    never be signalled. Measured on a live instance: 0 to 23 over fifteen minutes
    of ordinary use.

    The recycled pid belongs in this direction and not in the healthy one, which
    is what makes the reading a usable safety number: a record whose pid now
    names a stranger is the exact input that turns a sweep signalling by pid into
    a kill of somebody else's process. It is counted, its record is retracted,
    and the stranger itself is never signalled -- the record proves only that our
    process is gone, never that the current holder is ours.

The two counts are published as ``unowned_alive`` and ``owned_dead`` so an
operator sees a leak while it is small. They are the SLI; the actions below are
what makes a non-zero reading temporary rather than permanent.

Why an unowned process is not simply killed
------------------------------------------
Absence from the record is evidence that something is unclaimed, NOT evidence
that it is abandoned. The unowned population on a healthy host is dominated by
processes with perfectly good owners that no *session* record describes: a
Playwright chromium tree owned by a browser panel, ``mcp start-server``
processes owned by a stub connection, sandbox shim wrappers owned by the spawn
in progress. Killing on first sight would take a user's live browser out from
under them and call it a leak fixed.

So a kill needs five independent things to line up, and any one of them missing
leaves the process alone and merely counted:

1. the process carries this install's own spawn marker, so it is ours to end;
2. it was unowned on the PREVIOUS pass too, which is what distinguishes an
   abandoned process from one whose record is a few milliseconds behind its
   spawn -- the window every registration has;
3. it is older than :data:`DEFAULT_MIN_AGE_SECS`, for the same window seen from
   the other side;
4. its argv names a harness this gateway manages, which is the kill seam's own
   recycle guard asked HERE, at the decision point, rather than only inside the
   seam. A sandbox shim, an ``mcp start-server`` broker and a sibling install's
   python interpreter all reach this point carrying our inherited marker, and the
   seam declines every one of them. Asked here, they are withheld by name; asked
   only inside the seam, each one first collects an ownership-gate allow and the
   kill attribution that allow writes -- 3151 attribution lines for 181 pids
   against 4 real kills, measured over 6.5 hours on one host;
5. :func:`~kiro_crew.runtime_ownership.authorize_runtime_kill` allows it, so a
   pid that turns out to be leased after all is refused at the last moment and
   the refusal is logged.

A pass also spends at most :data:`DEFAULT_MAX_KILLS` kills, so a reconciler that
is wrong about a whole population is wrong slowly enough to be noticed.

Why the budget can be turned down to zero
-----------------------------------------
``session.reconcile_max_kills`` bounds the kills one pass may perform, and its
ceiling is :data:`DEFAULT_MAX_KILLS` -- so it can only ever lower the shipped
budget, never raise it. At 0 the kill arm OBSERVES: the four local conditions are
still evaluated, a candidate that satisfies them all is audited as ``would_kill``,
published in :attr:`ReconcileReading.would_kill`, and never signalled. Nothing
above the budget check changes, so the leak reading an operator acts on is the
same reading either way.

An operator needs that setting because the evidence of abandonment here is "no
record on this data home claims this pid", and that evidence is only as wide as
the records THIS process can read. The agent slice is named from a hash of the
config directory, so every install sharing a data home shares the slice -- and a
runtime owned by a different process (a sibling gateway built from another
checkout, a long-lived CLI) is claimed by records that process holds. Its pid-file
row is the one thing bridging the two, and a row that was never written reads here
as an abandoned process carrying our marker. On such a host the unowned population
ran 250-504 per pass against 4 genuine strays in 6.5 hours.

Membership is narrower than the slice even for this install's own spawns, and one
such spawn answers by identity rather than by a record. A sandboxed tool subprocess
-- a build, an ``npx`` install, a provisioning run routed through
:func:`sandbox.sandboxed_spawn_argv` -- lands in this slice carrying our inherited
marker and is in no membership source, so it would read as unowned on every pass and
its argv0 basename would be all that stood between it and a signal once it outlives
the age floor. The chokepoint stamps ``KIROCREW_SANDBOX_TOOL`` on its whole tree and
:func:`process_is_sandbox_tool` reads that back, which takes such a tree out of the
candidate population on exec-time evidence -- paired with the managed-argv test,
because the marker is inherited and a harness that ends up inside a tool tree must
stay a candidate. An app backend's pid record is the other narrowing. Turning the
budget to 0 is how an operator on a shared-data-home host takes the reading without
the signal.

Why the dead direction acts immediately
---------------------------------------
Forgetting a dead pid harms nothing -- the process is already gone -- and the
lease holder learns its runtime died instead of waiting out a timeout. The same
two-pass confirmation is therefore not needed here, and a single liveness probe
that says DEAD is not enough on its own: an unsignalable pid is an unknown, not
an absence, and is left alone.

The three population names are the ones ``test/e2e/process_inventory.py`` uses
for the same reconciliation read from outside the process, and they classify the
recycled pid the same way, so a reading published here and an inventory taken by
that harness describe one host state in one vocabulary.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat, session_pid
from kiro_crew.mcp_gateway.daemon_control import configured_socket_path
from kiro_crew.process_identity import audit_kill_decision
from kiro_crew.runtime_ownership import (
    RUNTIME_OWNERSHIP,
    RUNTIME_TENANCY,
    authorize_runtime_kill,
    commit_runtime_teardown,
    release_runtime_teardown,
    tenancy_epoch,
)
from kiro_crew.session_pid import (
    _pid_age_seconds,
    _pid_start_token,
    _read_env_has_kirocrew_marker,
)
from kiro_crew.session_scope_reap import instance_slice_pids

logger = logging.getLogger(__name__)

#: How old an unowned process must be before it can be killed. A spawn publishes
#: its record after the process exists, so anything younger may simply be a
#: registration in flight.
DEFAULT_MIN_AGE_SECS = 300.0

#: Kills one pass may perform when nothing configures a budget. A reconciler that
#: has misjudged an entire population reaches this ceiling and stops, leaving the
#: rest counted and the operator a reading to act on.
#:
#: ``session.reconcile_max_kills`` can only lower it: that field's ceiling equals
#: this value, so every configured budget is at or below the shipped one and the
#: knob withholds signals rather than authorizing new ones. Its default is spelled
#: in ``config.sections`` and pinned equal to this one by
#: ``test_the_config_default_matches_the_module_default``, because config is a leaf
#: package and importing this module into it would be a cycle.
DEFAULT_MAX_KILLS = 5

#: Entries the "already announced this death" memory may hold. The per-pass prune
#: against the recorded population is what normally bounds it; this is the ceiling
#: for a population that churns faster than the prune reads it. Generous relative
#: to any real agent slice, because every eviction costs a duplicate notice.
MAX_NOTIFIED_DEAD = 4096


@dataclass(frozen=True, slots=True)
class ReconcileReading:
    """What one pass saw and did. ``supported`` false means it could not look."""

    supported: bool = True
    reason: str = ""
    #: Pids a record claims and the kernel still has, as the same process.
    owned_alive: int = 0
    #: Pids a record claims that are gone, or that now name a stranger.
    owned_dead: int = 0
    #: Live pids inside our own agent slice that no record claims, EXCEPT those
    #: :meth:`RuntimeReconciler._unowned` excludes: a pid marked as sandboxed tool
    #: work whose argv0 is not a managed harness. So this is the unclaimed population
    #: this arm can act on, which is what makes it the number to read beside
    #: ``would_kill``. ``resource_status.slice_ownership`` publishes a field of the
    #: same name computed as every unclaimed slice pid, so on a host doing long-lived
    #: tool work the two disagree BY DESIGN and the larger one is not a fault.
    unowned_alive: int = 0
    #: Unowned pids that met every condition and whose tree was signalled.
    killed: int = 0
    #: Unowned pids that met every local condition and were NOT signalled because
    #: the pass had no kill budget, which happens only where an operator has turned
    #: ``session.reconcile_max_kills`` down to 0. The number they read to decide
    #: whether restoring the budget would take anything they still want: it is 0 on
    #: a host with nothing to reclaim and non-zero on a host with a real stray, and
    #: neither says so through ``unowned_alive``, which counts the whole population
    #: this arm can act on.
    would_kill: int = 0
    #: Dead pids whose records were retracted.
    forgotten: int = 0

    def as_counter_fields(self) -> dict[str, str | int | bool | float]:
        """The reading as metric fields, named to match the liveness SLI."""
        return {
            "unowned_alive": self.unowned_alive,
            "owned_dead": self.owned_dead,
            "owned_alive": self.owned_alive,
            "killed": self.killed,
            "would_kill": self.would_kill,
            "forgotten": self.forgotten,
        }


def _sel_reconcile_kill(pid: int, outcome: str, reason: str) -> None:
    """This module's kill-decision audit, on the shared emitter.

    Shared with the cleanup sweeps so one change to the audit shape reaches every
    kill decision in this gateway rather than one of them.
    """
    audit_kill_decision(pid, outcome, reason, tool_name="runtime_reconcile")


def _session_pid_entry_owners() -> dict[int, tuple[int, str | None, str]]:
    """``{child_pid: (owning gateway pid, recorded start token, VERBATIM row)}`` for EVERY row.

    The whole session-tracking file, not one gateway's slice of it. A reconciler
    that compares records against the kernel sees pids this gateway never filed --
    a concurrent ``kirocrew chat`` on the same data home, a predecessor gateway
    whose rows outlived it -- and needs two things about each: its recorded
    identity, so a recycled pid is detectable, and its OWNER, because
    ``session_pid._untrack_session_pid`` matches on the CALLING process's own
    prefix and cannot remove anybody else's row. Without the owner this module
    would call the untracker for a foreign row, get ``True`` back from a rewrite
    that changed nothing, and report a retraction that never happened.

    The row is kept VERBATIM for the third thing: what a retraction is allowed to
    remove. ``session_pid._untrack_session_pid`` drops every line whose
    ``<gw_pid>:<pid>`` prefix matches, at ANY start token, so it cannot tell the row
    a pass inspected from a replacement's row under the same reused number -- and a
    pass decides to retract from a snapshot taken before its own awaits. Removing by
    number would take the replacement's row out with the dead one and leave a LIVE
    runtime tracked nowhere, which is the leak this module exists to prevent.
    :func:`_retract_session_rows` removes the text captured here instead, the same
    invariant the descendant half already holds: a line is removed because it was
    named, never because it shares a field.

    Read under the file's own lock, through that module's path and lock helpers, so
    it observes the same exclusion every other reader does. A later row for the same
    child pid wins, which is the precedence a line-ordered rewrite gives. Tolerant
    of malformed lines for the reason every reader of this file is: a live gateway
    appends to it concurrently.
    """
    owners: dict[int, tuple[int, str | None, str]] = {}
    path = session_pid._session_pid_file_path()
    try:
        with session_pid._session_pid_file_lock():
            if not path.exists():
                return owners
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("runtime_reconcile: could not read %s", path, exc_info=True)
        return owners
    for line in lines:
        entry = line.strip()
        parts = entry.split(":")
        if len(parts) not in (2, 3):
            continue
        try:
            gw_pid, child_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if child_pid <= 0:
            continue
        owners[child_pid] = (
            gw_pid,
            parts[2] or None if len(parts) == 3 else None,
            entry,
        )
    return owners


def _retract_session_rows(rows: Iterable[str]) -> None:
    """Remove exactly *rows* from the session tracking file.

    Whole-line equality against the text a pass captured, under that file's own
    lock -- the twin of :func:`_retract_descendant_rows`, and for the same reason.
    Both row grammars this file holds (``<gw_pid>:<pid>`` and
    ``<gw_pid>:<pid>:<token>``) are covered by one rule, because a verbatim line
    needs no grammar.

    Anything appended since the capture stays, including a replacement row under a
    reused pid. That is the whole point: the record removed is the one confirmed
    dead, not every record sharing its number.
    """
    doomed = {row.strip() for row in rows if row.strip()}
    if not doomed:
        return
    path = session_pid._session_pid_file_path()
    try:
        with session_pid._session_pid_file_lock():
            if not path.exists():
                return
            lines = path.read_text(encoding="utf-8").splitlines()
            kept = [ln for ln in lines if ln.strip() and ln.strip() not in doomed]
            session_pid._rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")
    except OSError:
        logger.warning("runtime_reconcile: could not retract from %s", path, exc_info=True)


def _descendant_pid_rows() -> dict[int, tuple[str, ...]]:
    """``{descendant pid: its VERBATIM rows}`` from the other tracking file.

    The rows are kept whole, not reduced to the pid that keys them, because the
    pid alone is not what identifies a record here. A ``child:parent[:token]`` row
    names which runtime the descendant belongs to and which process start that
    descendant was, and a pid legitimately carries one row per parent -- the file's
    own append dedupes on the ``child:parent`` prefix, not on the child.

    Retracting by pid would therefore remove records this pass never inspected. The
    number is reused; a replacement registered after this snapshot appends its own
    row under the same pid, and a pid-keyed removal takes that row out with the dead
    one. The live descendant would then be tracked nowhere, which is both a leak
    past every reaper that keys off this file and a candidate the next pass reads as
    unowned. :func:`_retract_descendant_rows` removes exactly the text captured
    here, which is the same invariant ``session_pid._replace_child_pids`` states for
    itself: a line is removed because it was named, never because it shares a field.

    A pid appearing only as the PARENT field of somebody else's row is NOT a record
    of itself and is absent from this result, matching the field the reapers read
    (:data:`session_pid._REAPABLE_PID_FIELD`).

    Read under that file's own lock, through that module's path and lock helpers.
    Tolerant of malformed lines for the reason every reader of these files is: a
    live gateway appends to them concurrently.
    """
    rows: dict[int, tuple[str, ...]] = {}
    path = session_pid._pid_file_path()
    try:
        with session_pid._pid_file_lock():
            if not path.exists():
                return rows
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("runtime_reconcile: could not read %s", path, exc_info=True)
        return rows
    for line in lines:
        entry = line.strip()
        if not entry:
            continue
        parts = entry.split(":")
        try:
            child_pid = int(parts[0])
        except ValueError:
            continue
        if child_pid <= 0:
            continue
        rows[child_pid] = (*rows.get(child_pid, ()), entry)
    return rows


def _retract_descendant_rows(rows: Iterable[str]) -> None:
    """Remove exactly *rows* from the descendant tracking file.

    Whole-line equality against the text a pass captured, under that file's own
    lock. Both row grammars the file holds are covered by one rule, because a
    verbatim line needs no grammar: a ``child:parent[:token]`` row and a legacy
    bare pid line are each removed when they are the line that was read.

    Anything appended since the capture stays, including a replacement row under a
    reused pid. That is the point -- the record removed is the one confirmed dead,
    not every record sharing its number.
    """
    doomed = {row.strip() for row in rows if row.strip()}
    if not doomed:
        return
    # Resolved BEFORE the try: the lock's own ``__enter__`` does a mkdir and opens
    # the lock file, so an OSError from it would reach a handler whose message reads
    # ``path`` -- raising UnboundLocalError out of the handler and losing both this
    # warning and the caller's. The readers above already resolve it first.
    path = session_pid._pid_file_path()
    try:
        with session_pid._pid_file_lock():
            if not path.exists():
                return
            lines = path.read_text(encoding="utf-8").splitlines()
            kept = [ln for ln in lines if ln.strip() and ln.strip() not in doomed]
            session_pid._rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")
    except OSError:
        logger.warning("runtime_reconcile: could not retract from %s", path, exc_info=True)


def _pid_identity(pid: int) -> str | None:
    """*pid*'s process-start identity, or ``None`` when it cannot be read.

    The same token every other reader in this repository compares a pid against
    (:func:`session_pid._pid_start_token`), so a mismatch here means what it means
    everywhere else: the kernel gave this number to a different process.
    """
    return _pid_start_token(pid)


def _leases_on_pid(pid: int) -> int:
    """Leases live runtimes on *pid* hold, asked through the lease table itself.

    A pid is all this reconciler has: it starts from the kernel's list of process
    ids, not from runtime objects, so the identity-first accessor has nothing
    stronger to prefer here. Asked through ``RUNTIME_OWNERSHIP`` because that is
    the table's own published surface, and a reconciler that reaches for a
    module-level convenience instead would break on a rename of a name it does
    not own.

    A dead runtime's leases do not count, which is what makes this a gate and not
    a lock: a process that has already exited cannot be harmed by a signal, while
    refusing to signal it would suppress the reap of its zombie and the sweep of
    the descendants that escaped its group.
    """
    return RUNTIME_OWNERSHIP.leases_on_pid(pid)


def _claims_on_pid(pid: int) -> int:
    """Tenancy claims live on *pid*, asked through the tenancy table itself.

    The gate asks TWO questions, and a shield that asks only the first is not the
    gate's shield. A lease says a session may end this runtime; a tenancy says a
    party that does not own the process is mid-flight on it -- a session-sharing
    sub-agent's turn, a Connect OAuth mint child -- and at ``cap=1`` such a party
    holds no lease at all, because an acquisition cannot join an occupied runtime.

    So a pid carrying only a tenancy is CLAIMED, and counting it unowned publishes
    every in-flight shared turn as a leak in the reading an operator and the SLI
    both read. The gate refuses its kill either way; what this fixes is the number,
    and the per-pass gate refusal and audit row that number dragged behind it.

    Asked through ``RUNTIME_TENANCY`` for the same reason the lease count is asked
    through ``RUNTIME_OWNERSHIP``: a pid is all this reconciler has, and that is the
    table's own published surface.
    """
    return RUNTIME_TENANCY.claims_on_pid(pid)


def process_is_a_managed_agent(pid: int) -> bool:
    """Whether *pid*'s argv names a harness this gateway manages.

    The kill seam's own recycle guard, asked at the DECISION point. It is the same
    predicate :func:`session_pid._is_managed_agent_process` applies immediately
    before it signals, and asking it twice is deliberate: the seam must keep asking,
    because a pid can change hands between this answer and that signal.

    What asking it HERE changes is who gets recorded. The unowned population is
    dominated by processes that carry our inherited spawn marker and are not
    harnesses at all -- a Playwright chromium tree, an ``mcp start-server`` broker, a
    sandbox shim, another install's python interpreter. Every one of them satisfies
    the other four local conditions, so without this one each would collect an
    ownership-gate allow and have the gate write a kill attribution naming a process
    the seam declines to touch.
    The gate's allow log is the record a maintainer reads to answer "did we signal
    somebody else's process", and at that volume it cannot answer.

    Answers from argv, so it says a pid is the KIND of process we manage and never
    that it is a particular one. That is why it is a condition and not an
    authorization: identity is re-read after it, the gate is asked after that, and
    the seam re-applies this same test last.
    """
    return session_pid._is_managed_agent_process(pid)


def process_is_ours(pid: int, *, proc_root: Path | None = None) -> bool:
    """Whether *pid* carries this install's spawn marker.

    The marker is an environment variable set on every process Kiro Crew spawns
    and inherited by its whole tree, so it answers for a descendant the spawn
    never recorded -- which is most of what an unowned reading is made of. It is
    read out of the kernel's exec-time copy rather than from any file this uid
    can write, because it is the one thing standing between a reconciler and a
    stranger's process: a same-uid process can forge a file or an argv, and
    cannot alter another process's exec-time environment.

    Fail-closed through :func:`session_pid._read_env_has_kirocrew_marker`'s
    tri-state answer: an unreadable environment is not ours, and a platform with
    no environ oracle has nothing that is, which makes the kill direction a
    no-op there rather than a guess.
    """
    return _read_env_has_kirocrew_marker(pid, proc_root) is True


def process_is_sandbox_tool(pid: int, *, proc_root: Path | None = None) -> bool:
    """Whether *pid* is a sandboxed tool subprocess this install spawned.

    A tree spawned through :func:`sandbox.sandboxed_spawn_argv` -- a build, an ``npx``
    install, a ``git``/``gh`` read, a provisioning run -- runs inside the slice this
    module reads, carries the inherited spawn marker, and is recorded nowhere, so the
    argv0 basename test is all that holds it back from a signal once it is older than
    the age floor. The chokepoint stamps ``KIROCREW_SANDBOX_TOOL`` on the whole tree,
    so this answers for a descendant no spawn recorded, and it answers out of the
    kernel's exec-time copy: a same-uid process can set its own argv or write any
    file, and cannot alter a running process's environment. That is the difference
    between a name and evidence.

    The marker means TOOL, not OWNERSHIP, which is why it is a separate variable from
    the one :func:`process_is_ours` reads. That one enables a kill and this one
    withholds it, so the two must be able to disagree.

    FAILS OPEN, the opposite of every other test here, because its answer is an extra
    sparing rather than a permission: only a positive read excludes. An unreadable
    environment and a platform with no environ oracle both leave *pid* in the
    candidate population, where the ownership, argv, two-pass and age conditions
    still govern it. Failing closed would widen what escapes those conditions on
    doubt, which is the one thing this module must never do.

    This answer alone NEVER excludes a pid, because the marker describes a TREE. The
    chokepoint accepts harness argv by design -- its ``is_kiro_cli`` parameter exists
    so a delegating spawn can route through it, and a pod child probe and an
    unattended fix-authoring agent both do -- and the marker is inherited, so a
    harness can carry it without being tool work.
    :meth:`RuntimeReconciler._unowned` therefore requires this answer AND a
    non-harness argv0 before it drops a pid.
    """
    return session_pid._env_is_sandbox_tool(pid, proc_root) is True


def process_age_secs(pid: int, *, proc_root: Path = Path("/proc")) -> float:
    """Seconds since *pid*'s process started, or ``0.0`` when unreadable.

    Delegates to :func:`session_pid._pid_age_seconds`, which reads the start time
    out of ``/proc/<pid>/stat`` field 22 on Linux and the process-start id on
    macOS. The directory's own ``st_ctime`` is NOT the process's age: procfs
    assigns it when the inode is instantiated, which can be long after the process
    started, so an age taken from it reads every candidate as younger than the
    floor and the kill arm never reclaims anything.

    Zero on failure is the fail-closed answer: it reads as "too young to touch"
    against :data:`DEFAULT_MIN_AGE_SECS`, so a pid whose age cannot be established
    is never old enough to kill.
    """
    age = _pid_age_seconds(pid, str(proc_root))
    if age is None:
        return 0.0
    return max(0.0, age)


class RuntimeReconciler:
    """One reconciliation pass, and the memory of the previous one.

    Every seam is injected so the whole decision path runs against a fake kernel
    with no real processes and no real signals. The instance is retained across
    passes because the two-pass confirmation IS its state: a pid unowned once is
    remembered, and only an unowned pid remembered from last time can be killed.
    """

    def __init__(
        self,
        *,
        slice_pids: Callable[[], set[int]],
        recorded_pids: Callable[[], set[int]],
        is_alive: Callable[[int], bool],
        identity_of: Callable[[int], str | None] = _pid_identity,
        was_recycled: Callable[[int], bool] = lambda _pid: False,
        is_ours: Callable[[int], bool] = process_is_ours,
        is_managed: Callable[[int], bool] = process_is_a_managed_agent,
        is_sandbox_tool: Callable[[int], bool] = process_is_sandbox_tool,
        leases_on: Callable[[int], int] = _leases_on_pid,
        claims_on: Callable[[int], int] = _claims_on_pid,
        authorize: Callable[[int, str], bool] | None = None,
        epoch_of: Callable[[int], int] = tenancy_epoch,
        commit_teardown: Callable[[int, int], bool] = commit_runtime_teardown,
        release_teardown: Callable[[int], None] = release_runtime_teardown,
        kill_tree: Callable[..., int],
        forget: Callable[[int], str],
        notify_dead: Callable[[int], None],
        audit: Callable[[int, str, str], None] = _sel_reconcile_kill,
        age_secs: Callable[[int], float] = process_age_secs,
        min_age_secs: float = DEFAULT_MIN_AGE_SECS,
        max_kills: int = DEFAULT_MAX_KILLS,
    ) -> None:
        self._slice_pids = slice_pids
        self._recorded_pids = recorded_pids
        self._is_alive = is_alive
        self._identity_of = identity_of
        self._was_recycled = was_recycled
        self._is_ours = is_ours
        self._is_managed = is_managed
        self._is_sandbox_tool = is_sandbox_tool
        self._leases_on = leases_on
        self._claims_on = claims_on
        self._authorize = authorize or _default_authorize
        self._epoch_of = epoch_of
        self._commit_teardown = commit_teardown
        self._release_teardown = release_teardown
        self._kill_tree = kill_tree
        self._forget = forget
        self._notify_dead = notify_dead
        self._audit = audit
        self._age_secs = age_secs
        self._min_age_secs = min_age_secs
        self._max_kills = max_kills
        #: Unowned pids seen on the previous pass, each with the process identity
        #: it had then. The second half of the two-pass confirmation, keyed on
        #: identity as well as number so a recycled pid cannot inherit it.
        self._unowned_last_pass: dict[int, str | None] = {}
        #: Pids whose holder has already been told their runtime died. A
        #: notification is a user-visible event and is owed once, while the
        #: owned-dead COUNT stays a fresh reading on every pass.
        #:
        #: Keyed on pid AND recorded identity, for the reason the two sibling
        #: memories in this class are: a pid is reused, and a number-keyed memory
        #: would let the NEXT session whose runtime lands on it die unannounced,
        #: learning only from its own turn timeout.
        #:
        #: Pruned every pass against the recorded population, and capped, because a
        #: memory nothing removes from grows for the gateway's lifetime. The prune
        #: is what normally holds it down; the cap is the backstop for a population
        #: that churns faster than it is read, and an overflow is COUNTED rather
        #: than silent -- dropping the oldest entries means those pids can be
        #: announced twice, which is a visible cost and belongs in the reading.
        self._notified_dead: dict[tuple[int, str | None], None] = {}
        #: Notification-memory entries dropped to stay under
        #: :data:`MAX_NOTIFIED_DEAD`, cumulative. Non-zero means some holder may be
        #: told twice about the same death.
        self._notified_dead_overflow = 0
        #: Process identity per candidate, captured when this pass classified it
        #: and re-compared in the instant before the signal.
        self._identities: dict[int, str | None] = {}
        #: The reason each withheld pid carried on the previous pass, so the audit
        #: records a CHANGE of reason rather than a steady state repeated forever.
        #: Keyed on pid AND identity, for the same reason the two-pass memory is: a
        #: recycled pid would otherwise inherit the previous process's "already
        #: recorded" state, and the replacement's first refusal would go unaudited.
        self._withheld_reason: dict[tuple[int, str | None], str] = {}

    def set_max_kills(self, budget: int) -> None:
        """Adopt *budget* as the per-pass kill ceiling for the passes that follow.

        A setter rather than a constructor value that outlives the process, because
        the instance is retained for the life of the gateway -- the two-pass
        confirmation IS its state -- while ``session.reconcile_max_kills`` is live
        config any writer can change. Frozen at construction, turning the budget down
        to observe-only or back up would need a gateway restart, which is the one
        thing an operator reading a leak should not have to do.

        Called by the cleanup tick before every pass, the same schedule
        ``_adopt_idle_policy`` re-reads the idle sweep's own knobs on. A negative
        value is taken as zero: the loader clamps it there already, and the
        fail-closed reading of an unexpected number is the one that does not kill.
        """
        self._max_kills = max(0, budget)

    def run_once(self) -> ReconcileReading:
        """Compare both truths, act on what only one of them knows, and report."""
        try:
            kernel = self._slice_pids()
        except Exception as exc:
            return ReconcileReading(supported=False, reason=f"cannot read the agent slice: {exc}")
        try:
            recorded = self._recorded_pids()
        except Exception as exc:
            # A registry that cannot be read makes EVERY live pid look unowned,
            # which is the one input that turns this pass into a massacre. Refuse
            # the whole pass rather than acting on half of it.
            return ReconcileReading(supported=False, reason=f"cannot read the registry: {exc}")

        owned_dead, forgotten = self._reconcile_dead(recorded)
        unowned = self._unowned(kernel, recorded)
        # Identity captured at classification, for the re-check in the instant
        # before each signal. Rebuilt per pass so a pid that left the population
        # leaves no stale identity behind.
        self._identities = {}
        for pid in unowned:
            try:
                self._identities[pid] = self._identity_of(pid)
            except Exception:
                logger.debug("runtime_reconcile: identity read failed pid=%s", pid, exc_info=True)
                self._identities[pid] = None
        killed, would_kill = self._reconcile_unowned(unowned)
        # The two-pass memory is keyed on pid AND identity. Keyed on the number
        # alone, a pid that exited and was reused between passes would inherit the
        # previous pass's confirmation and be eligible immediately -- a
        # confirmation about a process that has since gone.
        self._unowned_last_pass = dict(self._identities)
        return ReconcileReading(
            owned_alive=len(recorded) - owned_dead,
            owned_dead=owned_dead,
            unowned_alive=len(unowned),
            killed=killed,
            would_kill=would_kill,
            forgotten=forgotten,
        )

    # -- direction one: a record with no process --

    def _reconcile_dead(self, recorded: set[int]) -> tuple[int, int]:
        dead = 0
        forgotten = 0
        for pid in sorted(recorded):
            try:
                if self._is_alive(pid) and not self._is_stranger(pid):
                    continue
            except Exception:
                # An unreadable liveness probe is an unknown process, not an
                # absent one. Retracting a record on that answer is how a live
                # runtime loses the only thing that can ever find it again.
                continue
            dead += 1
            try:
                outcome = self._forget(pid)
                if outcome == "retracted":
                    forgotten += 1
                elif outcome == "failed":
                    # A retraction that was this pass's to make and did not land.
                    # Counting it would report work that did not happen; the stale
                    # entry is re-detected and re-pruned on the next pass.
                    logger.warning(
                        "runtime_reconcile: the record for pid=%s could not be retracted", pid
                    )
                else:
                    # "not-mine": a row owned by another gateway, or a pid known only
                    # to the MCP pidfile or the manager's in-memory union. There is no
                    # row here to remove and there never will be, so this is a steady
                    # state and not a fault -- at WARNING it would be one line per
                    # stale pid per cleanup tick for as long as the gateway runs.
                    logger.debug(
                        "runtime_reconcile: the record for pid=%s is not ours to retract", pid
                    )
            except Exception:
                logger.debug("runtime_reconcile: could not retract pid=%s", pid, exc_info=True)
            notified_key = self._notified_key(pid)
            if notified_key in self._notified_dead:
                # The count above is a true reading and stays honest every pass,
                # but the notification is a user-visible event and fires ONCE per
                # pid. Retraction does not always remove the pid from every record
                # -- one held in the manager's own live union is not in the file
                # the untracker rewrites -- so the same disagreement can be
                # rediscovered on every pass, and a notice per pass per cleanup
                # interval would keep appending to the user's chat for as long as
                # the gateway runs.
                continue
            self._remember_notified(notified_key)
            try:
                self._notify_dead(pid)
            except Exception:
                logger.debug("runtime_reconcile: could not notify for pid=%s", pid, exc_info=True)
        self._prune_notified(recorded)
        if dead:
            logger.warning(
                "runtime_reconcile owned_dead=%d forgotten=%d: records named processes "
                "that are gone or whose pid now belongs to a stranger",
                dead,
                forgotten,
            )
        return dead, forgotten

    # -- the notification memory, bounded --

    def _notified_key(self, pid: int) -> tuple[int, str | None]:
        """*pid* with the identity the kernel answers for it right now.

        The two ways a record reaches the dead direction give this key its two
        values, and both are what the notice is about. A pid with no process answers
        ``None`` and keeps answering ``None``, so its holder is told once. A pid the
        kernel REASSIGNED answers the stranger's identity -- a different key from the
        one the first death was filed under, which is exactly the case a
        number-keyed memory swallowed: the next session whose runtime lands on that
        number and dies would have learned only from its own turn timeout.

        Read live rather than from ``_identities``, which the unowned direction fills
        and clears for its own candidates and never holds a dead pid at all.
        """
        try:
            return (pid, self._identity_of(pid))
        except Exception:
            # An unreadable probe is not a reason to skip a notice; the entry then
            # shares the ``None`` key, costing a suppressed duplicate, never a wrong one.
            logger.debug("runtime_reconcile: identity probe failed pid=%s", pid, exc_info=True)
            return (pid, None)

    def _remember_notified(self, key: tuple[int, str | None]) -> None:
        """Record that this key's holder has been told, evicting oldest first at the cap."""
        self._notified_dead[key] = None
        while len(self._notified_dead) > MAX_NOTIFIED_DEAD:
            # Insertion-ordered, so the first key is the oldest. Counted because an
            # evicted pid can be announced a second time.
            self._notified_dead.pop(next(iter(self._notified_dead)))
            self._notified_dead_overflow += 1
            logger.warning(
                "runtime_reconcile: notification memory hit its %d-entry bound; "
                "%d entries dropped so far, so a death may be announced twice",
                MAX_NOTIFIED_DEAD,
                self._notified_dead_overflow,
            )

    def _prune_notified(self, recorded: set[int]) -> None:
        """Drop entries for pids no record claims any more.

        The notice is owed once per dead runtime, and a runtime whose record is gone
        can never be rediscovered as dead -- so its entry can only grow the memory.
        Rebuilt against the recorded population every pass, exactly as
        ``_unowned_last_pass`` and ``_withheld_reason`` are, which is what keeps this
        bounded by the slice rather than by the gateway's uptime.
        """
        for key in [key for key in self._notified_dead if key[0] not in recorded]:
            del self._notified_dead[key]

    def _is_stranger(self, pid: int) -> bool:
        """Whether the live process at *pid* is not the one the record named.

        A recorded start identity that DIFFERS from the live one proves the kernel
        reallocated the pid, so the tracked process is gone even though something
        answers at that number. An identity that cannot be read on either side is
        an unknown and never a mismatch, which is the rule
        ``session_pid._pid_start_token`` states for every reader of that token: a
        pid wrongly called a stranger would have its record retracted while its
        runtime is still serving.

        Subtractive only, exactly as that token is allowed to be used. A stranger
        verdict retracts a record; it never authorizes a signal, because the
        current holder of the pid is by definition not ours.
        """
        try:
            return self._was_recycled(pid)
        except Exception:
            logger.debug("runtime_reconcile: recycle check failed pid=%s", pid, exc_info=True)
            return False

    # -- direction two: a process with no record --

    def _unowned(self, kernel: set[int], recorded: set[int]) -> list[int]:
        mine = os.getpid()
        out: list[int] = []
        for pid in sorted(kernel):
            if pid <= 1 or pid == mine or pid in recorded:
                continue
            try:
                if self._leases_on(pid) or self._claims_on(pid):
                    continue
            except Exception:
                # Either table is authority enough to call a pid claimed, and an
                # unreadable one means claimed: this list decides a kill, so the
                # fail-closed answer is the only safe one.
                continue
            try:
                if self._is_sandbox_tool(pid) and not self._is_managed(pid):
                    # A tool subprocess from the sandbox chokepoint. It is in the
                    # slice and in no record, but it is not an agent runtime, so it
                    # leaves the population HERE rather than at a later condition:
                    # excluded before the list is built it is never counted in
                    # ``unowned_alive``, never reaches ``_why_not_yet``, and never
                    # collects a gate allow or a kill attribution naming it.
                    #
                    # BOTH tests, because the marker describes a TREE and the argv
                    # test is per process. A harness can carry the marker two ways:
                    # the chokepoint is called with harness argv directly (a pod
                    # child probe, an unattended fix-authoring agent -- that is what
                    # its ``is_kiro_cli`` parameter is for), or a marked tool tree
                    # spawns one, since an agent's terminal command routes through
                    # the same chokepoint and a shell can launch a runtime. Either
                    # way that process is the one stray class this arm can reach --
                    # an orphaned harness in the slice, in no record, past the floor
                    # -- so requiring "marked AND not a harness" keeps it a candidate.
                    #
                    # So the population that leaves here is exactly the population
                    # ``_why_not_yet`` would withhold by name anyway: the kill arm's
                    # behaviour is unchanged, and what changes is that these pids
                    # stop being counted, gated and attributed every pass. Both
                    # error directions land on that same pre-existing behaviour -- a
                    # wider basename set excludes FEWER pids, and a tool named like
                    # a harness is not excluded at all.
                    continue
            except Exception:
                # The opposite posture to the claimed check above, because the
                # answers mean opposite things. Claimed WITHHOLDS a kill, so doubt
                # must withhold; this exclusion also withholds, so doubt must NOT
                # exclude -- a pid whose marker cannot be read stays a candidate
                # under the ownership, argv, two-pass and age conditions, which is
                # exactly where it sits with no marker at all.
                logger.debug(
                    "runtime_reconcile: sandbox-tool check failed pid=%s", pid, exc_info=True
                )
            out.append(pid)
        return out

    def _reconcile_unowned(self, unowned: Iterable[int]) -> tuple[int, int]:
        killed = 0
        would_kill = 0
        withheld: list[tuple[int, str]] = []
        reasons_now: dict[tuple[int, str | None], str] = {}

        def hold(pid: int, why: str, outcome: str = "refused") -> None:
            """Record a withheld pid, auditing only a CHANGE of reason.

            Most of the unowned population is permanently withheld -- every MCP
            server and sandbox helper in the slice sits at "not a managed agent
            process" forever -- so one event per pid per pass would write
            thousands of identical rows a day into a log with a finite rotation
            ceiling, evicting the tool-invocation history an operator actually
            needs. A transition is the event; a steady state is not.

            *outcome* is the audit's verdict word. It is ``refused`` for a pid some
            condition withheld and ``would_kill`` for one that satisfied every
            condition and was withheld only because the pass had no budget: the
            second is the row an operator greps for before arming the budget, and
            collapsing it into the first would leave "nothing to reclaim" and "four
            strays sitting here" indistinguishable. Both are transition-audited, for
            the reason above -- a stray that stays a stray writes one row, not one
            per pass.
            """
            withheld.append((pid, why))
            key = (pid, self._identities.get(pid))
            reasons_now[key] = why
            if self._withheld_reason.get(key) != why:
                self._audit(pid, outcome, why)

        for pid in unowned:
            # The conditions are evaluated BEFORE the budget is consulted, so a pass
            # with no budget still classifies its candidates -- that is what makes
            # a zero-budget pass informative instead of merely quiet. It also
            # gives a pid held past an exhausted budget its real reason rather than
            # the budget's, and costs nothing on the common path: the argv test that
            # withholds most of the population is one cmdline read.
            reason = self._why_not_yet(pid)
            if reason:
                hold(pid, reason)
                continue
            # The identity read at classification, re-read HERE, immediately
            # before the signal. Everything above -- two passes, the marker, the
            # age floor -- inspected a pid NUMBER, and the kernel may hand that
            # number to another process at any point after each check.
            # Re-comparing the process-start identity narrows the exposure to
            # this comparison and the signal that follows it; the kill seam then
            # re-applies its own managed-agent check inside that gap, so a
            # replacement that is not one of ours is never signalled at all.
            if self._identity_changed(pid):
                hold(pid, "process identity changed since classification")
                continue
            if self._max_kills <= 0:
                # Observe-only, which an operator selects by setting the budget to
                # 0. Recorded as a candidate and left alone, and the ownership gate
                # is deliberately NOT asked: its
                # allow path writes "runtime kill pid=N caller=runtime_reconcile",
                # which is a statement that a kill was authorized, and on this path
                # none is. A reader of that log must be able to take it literally.
                would_kill += 1
                hold(pid, "observing only: the kill budget is zero", outcome="would_kill")
                continue
            if killed >= self._max_kills:
                hold(pid, "kill budget spent")
                continue
            # The gate LAST, because its allow path writes the kill attribution:
            # asked any earlier, every pid the checks above still withhold would
            # be recorded as a kill that never happened.
            if not self._authorize(pid, "unowned process inside our agent slice"):
                hold(pid, "refused by the ownership gate")
                continue
            # The gate's verdict is a statement about the past, and this pass runs on
            # the maintenance executor while a shared turn takes its tenancy on the
            # event loop. Between the verdict and the first signal sit the kill seam's
            # own descendant walk and its per-child marker reads, so a claim landing
            # in that window is invisible to the verdict. The epoch is read HERE,
            # right after the allow, and committed immediately before the signal --
            # the barrier the sibling kill path already takes, on exactly the
            # population a sub-agent claims a tenancy on: an orphaned shared runtime
            # with no lease and no record.
            epoch = self._epoch_of(pid)
            if not self._commit_teardown(pid, epoch):
                # A tenant claimed this process after the gate allowed it. Abandoning
                # is the whole point: the signal would land on a live turn that is
                # never retried.
                hold(pid, "a tenant claimed the process after the gate allowed it")
                continue
            try:
                signalled = self._kill_tree(pid, self._identities.get(pid))
            except Exception:
                withheld.append((pid, "kill failed"))
                reasons_now[(pid, self._identities.get(pid))] = "kill failed"
                self._audit(pid, "failed", "the kill raised")
                logger.debug("runtime_reconcile: kill failed pid=%s", pid, exc_info=True)
                continue
            finally:
                # Dropped on every exit from the signal, including the raise above: a
                # pid left committed is one no tenant can claim for the life of the
                # gateway.
                self._release_teardown(pid)
            if not signalled:
                # The tree-kill seam re-applies its own recycle guard and signals
                # NOTHING when the pid does not look like a managed agent process.
                # That is most of what an unowned reading is made of, so counting
                # an unsignalled call as a kill would spend the whole per-pass
                # budget on the same lowest pids every pass, forever, while the
                # SLI reported five kills and nothing changed.
                hold(pid, "kill signalled nothing")
                continue
            killed += 1
            # The seam's answer is a COUNT over the tree it walked, not a verdict on
            # the root: it signals every managed descendant it discovered and can
            # withhold the root's own signal after that, when the root stops being
            # the process this pass verified. So the line names the tree and the
            # count. Naming the root alone reported one long-lived pid as killed
            # twice an hour apart -- it was never signalled either time, and the
            # count came from a descendant -- and a reader chasing that had no way to
            # tell it from a process that survived a kill.
            self._audit(
                pid,
                "killed",
                f"unowned on two passes, our marker, past the age floor; {signalled} signalled",
            )
            logger.warning(
                "runtime_reconcile signalled %d process(es) in the tree at pid=%s: unowned on "
                "two consecutive passes, carries our spawn marker, older than %.0fs",
                signalled,
                pid,
                self._min_age_secs,
            )
        if withheld:
            logger.info(
                "runtime_reconcile: %d unowned process(es) counted and left alone (%s)",
                len(withheld),
                ", ".join(sorted({why for _pid, why in withheld})),
            )
        # Only this pass's reasons are retained, so a pid that leaves the
        # population and comes back is a transition again rather than inheriting a
        # reason nothing recorded.
        self._withheld_reason = reasons_now
        return killed, would_kill

    def _identity_changed(self, pid: int) -> bool:
        """Whether *pid* names a different process now than at classification.

        The identity is captured for every candidate when the pass classifies it
        and compared again in the instant before the signal. An identity that
        cannot be read on either side counts as CHANGED: an unreadable process is
        one this pass cannot claim to have inspected, and the fail-closed answer
        costs a withheld kill the next pass can retry.
        """
        recorded = self._identities.get(pid)
        if recorded is None:
            return True
        try:
            return self._identity_of(pid) != recorded
        except Exception:
            logger.debug("runtime_reconcile: identity re-read failed pid=%s", pid, exc_info=True)
            return True

    def _why_not_yet(self, pid: int) -> str:
        """Empty when every local precondition for killing *pid* holds.

        Ordered cheapest-first, and the argv test sits ahead of the marker read for
        a second reason beyond cost: it is the one that withholds most of this
        population, so putting it first is what makes the reason an operator reads
        the true one rather than whichever check happened to run earliest. It
        answers from one ``/proc/<pid>/cmdline`` read where the marker test reads
        ``/proc/<pid>/environ``, so the ordering also spends fewer reads per pass
        than the reverse.
        """
        if pid not in self._unowned_last_pass:
            return "first pass unowned"
        if self._unowned_last_pass[pid] != self._identities.get(pid):
            # Same number, different process: the confirmation the previous pass
            # earned belongs to a process that has since exited.
            return "first pass unowned"
        try:
            if not self._is_managed(pid):
                return "not a managed agent process"
        except Exception:
            return "not a managed agent process"
        try:
            if not self._is_ours(pid):
                return "no spawn marker"
        except Exception:
            return "no spawn marker"
        try:
            if self._age_secs(pid) < self._min_age_secs:
                return "younger than the age floor"
        except Exception:
            return "younger than the age floor"
        return ""


def _default_authorize(pid: int, reason: str) -> bool:
    return authorize_runtime_kill(pid, reason=reason, caller="runtime_reconcile")


def _mcp_backend_pids() -> set[int]:
    """Pids of the MCP backends gatewayd is hosting, from its own pidfile.

    A second record, and not an optional one: an MCP backend lives in gatewayd's
    pool, not in any session map, so every backend would read as unowned against
    the session registry alone -- and backends are a large, long-lived population
    that carries the same spawn marker a reconciler kill requires. gatewayd
    publishes them beside its socket precisely so a process outside it can answer
    for them, which is what makes this readable from the gateway.

    RAISES rather than answering an empty set when a file that EXISTS cannot be
    read, so a corrupt or unreadable pidfile refuses the pass
    (:meth:`RuntimeReconciler.run_once` turns a registry error into
    ``supported=False``) instead of presenting every live backend as an unowned
    process.

    An ABSENT file is a different answer and a real one: nothing is hosted. No
    broker runs at all under the default empty stub configuration, gatewayd
    unlinks the file on a clean shutdown, and it appears only a heartbeat after
    start -- so treating absence as a refusal would leave both directions and the
    liveness SLI permanently inert behind one debug line, which is the quietest
    possible way for this module to do nothing. A file that reads as empty says
    the same thing: gatewayd is up and hosts nothing.
    """
    path = Path(f"{configured_socket_path()}.backends")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    pids: set[int] = set()
    for token in raw.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 1:
            pids.add(pid)
    return pids


def build_reconciler(
    *,
    active_pids: Callable[[], set[int]],
    notify_dead: Callable[[int], None],
    min_age_secs: float = DEFAULT_MIN_AGE_SECS,
    max_kills: int = DEFAULT_MAX_KILLS,
) -> RuntimeReconciler:
    """The reconciler wired to this gateway's real records and real signals.

    *active_pids* is the SessionManager's own live-pid union -- only the manager
    knows it -- and is passed as a callable so each pass reads it fresh rather
    than reconciling against a set gathered an interval ago.

    The kill seam is :func:`session_pid._kill_pid_tree`, which re-applies its own
    argv recycle guard immediately before signalling. Reusing it rather than
    reaching for a kill primitive is deliberate twice over: the recycle guard is
    a different question from ownership and both must be answered, and it adds no
    new unattributed primitive call site to the repo.

    *max_kills* defaults to :data:`DEFAULT_MAX_KILLS`, so a caller that does not
    thread ``session.reconcile_max_kills`` through gets the shipped budget, which
    is the behaviour every existing caller already had.
    """

    def entry_index() -> dict[int, tuple[int, str | None, str]]:
        """``{pid: (owning gateway pid, recorded start identity, verbatim row)}`` for every row.

        Read across EVERY gateway's rows, not just this process's, for the same
        reason membership is: a row filed by a concurrent CLI or a predecessor
        gateway on this data home names a pid in the very slice this reconciler
        reads. Its identity is what the recycle check needs, and its OWNER is what
        decides whether a retraction here can actually remove it.
        """
        return _session_pid_entry_owners()

    #: The session file as this pass read it. Refreshed by ``recorded()``, which
    #: ``run_once`` calls once at the top of every pass, so the identities the
    #: recycle check compares against are the ones the pass classified -- and one
    #: locked file read serves the whole pass instead of one per recorded pid.
    snapshot: dict[int, tuple[int, str | None, str]] = {}

    #: The descendant file as this pass read it, ``{pid: verbatim rows}``. Refreshed
    #: on the same schedule and for the same reason, plus one of its own: a
    #: retraction removes the ROWS this pass inspected, so it needs their text, and
    #: a pid with no row here has nothing in this file to remove.
    descendants: dict[int, tuple[str, ...]] = {}

    def recorded() -> set[int]:
        """Every pid any record on this data home claims, and its identities.

        The membership question and the kernel question must be asked at the SAME
        scope or the difference between them is not a leak. The kernel side is
        scoped to the DATA HOME -- the agent slice is named from a hash of the
        config directory -- so a second process on the same home puts its agent
        runtimes in the very slice this reconciler reads: a ``kirocrew chat`` or
        ``run`` doing agent work in-process (the gateway lock bars a second
        gateway, not a second CLI), and this gateway's own namespace-sandbox
        children, which appear only in the descendant pid file because the pid
        that is tracked and leased is the launcher parent.

        Membership therefore comes from BOTH pid files across EVERY gateway pid.
        Asking only for this process's own session entries would leave every one of
        those processes unclaimed by construction: their records are filed under a
        different gateway pid or in the other file, and the lease table is
        per-process memory that cannot see them either. They would pass the marker
        test (it is inherited), age past the floor, and be signalled -- the exact
        harm this module exists to prevent, delivered by it.

        The session-file snapshot carries each row's OWNER and start identity: the
        identity is what the recycle check compares against, and the owner is what
        decides whether a retraction here can remove the row at all. The
        descendant-file snapshot carries which remover each of its rows needs. Both
        are read here so one locked read of each file serves the whole pass;
        membership does not come from either.
        """
        snapshot.clear()
        snapshot.update(entry_index())
        descendants.clear()
        descendants.update(_descendant_pid_rows())
        tracked, complete = session_pid._read_tracked_agent_pids()
        if not complete:
            # A partial read of the tracking files is the one input that makes a
            # live runtime look unowned, so it refuses the pass rather than
            # authorizing a kill on incomplete membership. The same completeness
            # requirement the scope reaper imposes on kill-authorizing callers.
            raise RuntimeError("the tracked-pid snapshot is incomplete")
        return set(active_pids()) | _mcp_backend_pids() | tracked | set(snapshot)

    def was_recycled(pid: int) -> bool:
        """Whether every identity RECORDED for *pid*, in either file, disagrees with the live one.

        Both files are asked, because a pid recorded in only one of them is the
        common case: a namespace-sandbox child has a descendant row and no session
        row at all. Reading the session file alone left such a pid ``owned_alive``
        after its number was reused, so its dead row was never retracted and the
        replacement kept a record that named somebody else's process.

        Subtractive, as every reader of this token must be: a mismatch proves the
        kernel reallocated the number, an unreadable identity on either side proves
        nothing, and one recorded identity that still MATCHES means the process we
        tracked is the one answering -- whatever the other rows say.
        """
        recorded_tokens = set()
        entry = snapshot.get(pid)
        if entry is not None and entry[1]:
            recorded_tokens.add(entry[1])
        for row in descendants.get(pid, ()):
            parts = row.split(":")
            # ``child:parent[:token]``; a legacy bare pid line records no identity.
            if len(parts) >= 3 and parts[2]:
                recorded_tokens.add(parts[2])
        if not recorded_tokens:
            # No entry, or only legacy rows with no identity recorded. Nothing to
            # compare, so nothing is proven.
            return False
        live_token = session_pid._pid_start_token(pid)
        if not live_token:
            # Unknown on the live side. Never a mismatch.
            return False
        return live_token not in recorded_tokens

    def is_alive(pid: int) -> bool:
        # PID_UNSIGNALABLE is an unknown, not a death. Only a confirmed
        # PID_DEAD retracts a record, so a permission-denied probe leaves the
        # record standing for the next pass.
        return platform_compat.pid_liveness(pid) != platform_compat.PID_DEAD

    def kill_tree(pid: int, expected_start: str | None = None) -> int:
        """Signal *pid*'s tree, pinned to the identity this pass verified; the count killed.

        ``expected_start`` is handed DOWN rather than checked here, and that is the
        whole point. Checking at this door leaves the seam's own descendant walk --
        a ``pgrep``, unbounded in time -- between the verdict and the root's signal,
        and a managed runtime that takes the number inside that walk passes the argv
        gate because it genuinely is one of ours. Pinned, the seam re-reads the
        identity before the walk and again immediately before the root signal.

        The count is READ, not discarded: the seam signals nothing for a pid that
        stops looking like the process we verified, and a caller that ignored that
        would count no-ops as kills.
        """
        total, _root = session_pid._kill_pid_tree(pid, expected_start=expected_start)
        return total

    def forget(pid: int) -> str:
        """Retract this pid's record; ``"retracted"``, ``"not-mine"`` or ``"failed"``.

        A record's SOURCE decides which remover can retract it, and no remover
        reports whether a row matched -- each returns whether its rewrite of the file
        succeeded, which an unchanged rewrite also does. So the answer here is an
        OBSERVED disappearance: the row was present in that source before and is
        absent from it after. Anything weaker reports retractions that did not
        happen, on every pass, for as long as the record survives.

        Membership is wider than the session file, so the session untracker alone
        cannot retract most of it. A descendant tracked only in ``kiro_pids.txt``
        (this gateway's namespace-sandbox children are all filed there, because the
        pid that is tracked and leased is the launcher parent) has no session row at
        all, and rewriting the session file leaves its record exactly where it was.

        Two sources are deliberately not retracted, and that is why the answer is
        three-valued rather than a bool. A session row owned by a concurrent CLI or a
        predecessor gateway is a real dead record, but ``_untrack_session_pid``
        matches on the CALLING process's prefix and cannot remove anybody else's row
        -- those are the next gateway start's to clear, once its owner reads dead.
        And a pid known only to the MCP backend pidfile or to the manager's
        in-memory union has no row in either tracking file: the MCP sweep owns the
        first and the manager owns the second. Both answer ``"not-mine"`` FOREVER, by
        design, so folding them into the same ``False`` a real failure gets would
        publish one WARNING per stale pid per cleanup tick for the gateway's life --
        a steady state reported as a fault. ``"failed"`` is reserved for a retraction
        that was this pass's to make and did not land, which is the only one worth
        waking anybody for.

        The descendant retraction removes the ROWS this pass captured, never every
        row sharing the pid. A pid is reused, so a replacement descendant can have
        appended its own row under the same number since the snapshot; taking that
        row out would leave a live process tracked nowhere, which is the leak this
        file exists to prevent. Success is likewise the captured rows being ABSENT
        on a fresh read, not the remover's answer -- a replacement keeps the pid
        present while the dead record is gone.
        """
        mine = False
        removed = False
        entry = snapshot.get(pid)
        if entry is not None:
            owner, _token, row = entry
            if not owner or owner == os.getpid():
                mine = True
                # The captured row, never the number: the untracker matches the
                # ``<gw_pid>:<pid>`` prefix at any token and would take a
                # replacement's row out with this dead one.
                _retract_session_rows((row,))
                surviving = _session_pid_entry_owners().get(pid)
                if surviving is None or surviving[2] != row:
                    removed = True
        captured = descendants.get(pid, ())
        if captured:
            mine = True
            _retract_descendant_rows(captured)
            surviving_rows = _descendant_pid_rows().get(pid, ())
            if not any(row in surviving_rows for row in captured):
                removed = True
        if removed:
            return "retracted"
        return "failed" if mine else "not-mine"

    return RuntimeReconciler(
        slice_pids=instance_slice_pids,
        recorded_pids=recorded,
        is_alive=is_alive,
        was_recycled=was_recycled,
        kill_tree=kill_tree,
        forget=forget,
        notify_dead=notify_dead,
        min_age_secs=min_age_secs,
        max_kills=max_kills,
    )
