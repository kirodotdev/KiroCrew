"""The unreadable-main-spec episode: a rebuild that cannot read ``kirocrew.json``.

The rebuild keeps the user's spec and skips its cycle (``_skip_unreadable_main_spec``)
instead of writing defaults over a file whose contents it could not read, and a rebuild
that writes the spec again closes the episode (``_note_main_spec_readable``). While the
episode is open the main agent and the specs mirrored from it are refused
(``require_main_spec_projected``), a start that cannot be pinned to a file is bound to
the episode (``require_main_spec_launch_bound``), and every refusal writes its SEL
denial and schedules the early retry (``refuse_unprojected_main_spec_start``). New work
on the episode, its gate or its early retry goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`. The state below is this module's initial
binding; ``kiro_crew.agent`` holds the live one, which the functions read and rebind.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from kiro_crew.agent import (
        _HEARTBEAT_AGENT_FILENAME,
        AGENT_FILENAME,
        WORKER_AGENT_FILENAME,
        ForkGovernanceUnresolved,
        _AgentSpecUnreadable,
        _conductor_spec_held,
        _EarlyRetryRunning,
        _install_sibling_specs,
        _read_spec_capped,
        agent_spec_path,
        kiro_agents_dir_path,
        logger,
        managed_mcp,
        project_agent_names,
        retry_held_conductor_specs,
        sel,
    )


#: Whether the last rebuild skipped because ``kirocrew.json`` could not be read
#: (:func:`_skip_unreadable_main_spec`), cleared once a rebuild writes it. While
#: set, the main spec and the heartbeat and worker specs mirrored from it may
#: still carry auto-approvals an earlier ceiling derived.
_main_spec_unreadable = False

#: Counts the skips of :func:`_skip_unreadable_main_spec`, under the episode lock. A
#: rebuild reads it before it reads the spec and hands it to
#: :func:`_note_main_spec_readable`, which closes the episode only if it is still the
#: one the rebuild saw: a skip that landed while that rebuild was writing is a newer
#: episode, and the older write never read what that skip failed on.
_main_spec_episode_revision = 0

#: The governance answer generation (``governance_answer_generation``) the last
#: sibling pass of an unreadable episode ran under, or ``None`` outside an
#: episode. A later skip under the same generation has nothing new to re-filter,
#: so it runs no sibling pass (:func:`_skip_unreadable_main_spec`).
_main_spec_skip_generation: int | None = None

#: The ``name`` the last WRITTEN main spec declared, or ``None`` when no
#: rebuild in this process has written it. The refusal names it, and it is one
#: of the main spec's own names. It cannot say what the file declares now: a
#: rename by hand after that write is unseen, and an unreadable file cannot be
#: asked. So which starts are gated is decided by the spec FILE
#: (:func:`_governed_by_main_spec`), and a bare name, which no file pins, is
#: refused for the whole episode (:func:`require_main_spec_launch_bound`).
_main_spec_declared_name: str | None = None

#: The delays, in seconds, of the early held-spec retries one unreadable-spec
#: episode schedules once a governed start is refused. The last delay then
#: repeats for as long as the episode is open, so a lock of any length is
#: picked up within a minute of its end, at the cost of one spec read and one
#: rebuild attempt a minute while the spec stays unreadable, and none once a
#: rebuild has written it.
_MAIN_SPEC_EARLY_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 60.0)

#: Guards the early-retry timer and the episode's warning state below. Held
#: only for bookkeeping, never across the retry, a log call or an SEL write.
_main_spec_episode_lock = threading.Lock()

#: The one pending early-retry timer, or ``None``. A refusal while one is
#: pending schedules nothing more (debounced).
_main_spec_retry_timer: Any = None

#: How many early retries this episode has scheduled, counted up to the number
#: of delays (the last one then repeats); reset when it closes.
_main_spec_retry_attempts = 0

#: Set at gateway shutdown (:func:`cancel_main_spec_early_retry`): a retry
#: already running then schedules no successor. Cleared when a gateway starts
#: (:func:`resume_main_spec_early_retry`), so a restart in the same process gets
#: early retries again.
_main_spec_retry_stopped = False

#: The warnings this episode already logged at WARNING: ``read:<errno>`` for
#: the skip (a changed cause warns again), ``retry`` for a failed early retry,
#: ``denial-audit`` for an SEL denial that could not be written (every refusal
#: still tries its own, :func:`_audit_main_spec_denial`). Episode state, under
#: :data:`_main_spec_episode_lock`: each one is decided in the critical section
#: that reads or sets :data:`_main_spec_unreadable`, and the close clears it in
#: the same section, so a key never outlives its episode or collides with the
#: next one. Mutated in place, never rebound.
_main_spec_warned: set[str] = set()


def _first_main_spec_warning(kind: str) -> bool:
    """Record *kind* for this episode; whether it was new. Caller holds the episode lock."""
    first = kind not in _main_spec_warned
    _main_spec_warned.add(kind)
    return first


def _spec_hold_open() -> bool:
    """Whether a rebuild still owes a retry: a held conductor spec, or an open episode.

    The one predicate the retry and the boot baseline read, so an open
    unreadable-spec episode is always a hold, whichever rebuild last settled
    ``_conductor_spec_held``. Read under the episode lock, the lock every writer
    of either flag takes.
    """
    with _main_spec_episode_lock:
        return _conductor_spec_held or _main_spec_unreadable


def _start_main_spec_retry_timer(delay: float, callback: Callable[[], None]) -> Any:
    """Start the early retry on a daemon thread, off the event loop."""
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.name = "kirocrew-main-spec-early-retry"
    timer.start()
    return timer


#: Indirection tests replace to drive the early retry on a simulated clock; ``None``
#: starts each retry on a daemon thread (:func:`_start_main_spec_retry_timer`).
_main_spec_retry_timer_factory: Callable[[float, Callable[[], None]], Any] | None = None


def _schedule_main_spec_early_retry() -> None:
    """Schedule the next early run of :func:`retry_held_conductor_specs` while the episode is open.

    Bounds how long a read failure (a Windows sharing violation under a
    concurrent writer holding the spec open) keeps the main agent refused after
    it ends: the episode gets retries 5, 15 and 60 s apart, then one every 60 s
    until a rebuild writes the spec. One timer at most is pending, and none
    once the episode has closed or the gateway is shutting down. The retry is
    the existing rebuild, so the flag still clears only when that rebuild
    writes the spec; this admits nothing.
    """
    global _main_spec_retry_timer, _main_spec_retry_attempts
    with _main_spec_episode_lock:
        if (
            _main_spec_retry_timer is not None
            or not _main_spec_unreadable
            or _main_spec_retry_stopped
        ):
            return
        last = len(_MAIN_SPEC_EARLY_RETRY_DELAYS) - 1
        delay = _MAIN_SPEC_EARLY_RETRY_DELAYS[min(_main_spec_retry_attempts, last)]
        _main_spec_retry_attempts = min(_main_spec_retry_attempts + 1, last + 1)
        # The run is told its own handle, so it can tell whether the slot still
        # holds it. Filled under this lock, which the run takes first, so the
        # run never sees it empty.
        own: list[Any] = []
        factory = _main_spec_retry_timer_factory or _start_main_spec_retry_timer
        try:
            handle = factory(delay, lambda: _run_main_spec_early_retry(own))
        except Exception:
            # A thread that cannot start leaves the hourly wake as the retry.
            _main_spec_retry_timer = None
            logger.debug("could not schedule the early main-spec retry", exc_info=True)
            return
        own.append(handle)
        _main_spec_retry_timer = handle


def cancel_main_spec_early_retry() -> None:
    """Cancel the pending early main-spec retry and schedule none after it.

    Called at gateway shutdown, so no retry thread rebuilds specs while the
    process is being torn down. The unreadable flag is left as it is: a
    shutdown admits nothing.
    """
    global _main_spec_retry_timer, _main_spec_retry_stopped
    with _main_spec_episode_lock:
        _main_spec_retry_stopped = True
        pending, _main_spec_retry_timer = _main_spec_retry_timer, None
    if pending is not None:
        with contextlib.suppress(Exception):
            pending.cancel()


def resume_main_spec_early_retry() -> None:
    """Allow early main-spec retries again, at gateway start.

    Undoes :func:`cancel_main_spec_early_retry`'s stop, so a gateway restarted
    in the same process (an embedding, a harness re-running the orchestrator)
    gets the 5, 15 and 60 s retries of its next episode instead of only the
    hourly wake. Schedules nothing itself: the next refused start does.
    """
    global _main_spec_retry_stopped
    with _main_spec_episode_lock:
        _main_spec_retry_stopped = False


def _run_main_spec_early_retry(own: list[Any]) -> None:
    """Run one early held-spec retry, then schedule the next while the spec stays unread.

    *own* holds the handle :func:`_schedule_main_spec_early_retry` stored for
    this run. ``threading.Timer.cancel()`` cannot stop a callback already past
    its check, so a close, a shutdown or a newer episode's timer may have taken
    the slot by the time this runs. The run claims the slot only while it still
    holds that handle, and otherwise does nothing: it never overwrites another
    episode's timer (which would leave two retries pending, one that shutdown
    cannot cancel). A run that has not yet claimed the slot never rebuilds
    during teardown; one that claimed it before shutdown finishes its rebuild
    and schedules no successor.
    """
    global _main_spec_retry_timer
    running = _EarlyRetryRunning()
    with _main_spec_episode_lock:
        # Shutdown is covered too: it clears the slot in the same critical
        # section that sets the stop flag.
        if not own or _main_spec_retry_timer is not own[0]:
            return
        _main_spec_retry_timer = running
    try:
        retry_held_conductor_specs()
    except Exception:
        logger.debug("early held conductor spec retry failed", exc_info=True)
    finally:
        with _main_spec_episode_lock:
            # Only this run's own marker: a close or a shutdown during the run
            # already took it, and a later episode may have armed its own timer.
            if _main_spec_retry_timer is running:
                _main_spec_retry_timer = None
    _schedule_main_spec_early_retry()


def _audit_main_spec_denial(
    agent: str, *, superseded: Literal["process", "session", "read"] | None = None
) -> None:
    """Record a best-effort SEL denial for this refused start.

    The existing governance-denial event (``log_governance_decision``,
    ``outcome="denied"``), no new event type; the cause and remedy ride in its
    ``reason``. Written for EVERY refusal, a retried start included, because each refusal is
    a permission decision. Enqueued to the SEL writer, not written with
    ``critical=True``: a refusal is raised on the event loop too (the
    projection's send translation), where a synchronous write would block, and
    the refusal stands without its row. The writer logs a persistence failure
    itself; an error raised before the enqueue is logged here at WARNING once per
    episode and at debug after that. Never raises, and the caller refuses the
    start regardless.

    *superseded* is the kind of a refusal made after the episode closed
    (:func:`refuse_unprojected_main_spec_start`): the spec reads and was rebuilt,
    so the row records what was read before the episode ended and what replaces
    it, not a read access to restore. ``None`` is a refusal while an episode is
    open.
    """
    try:
        reason = {
            None: (
                "cause=unreadable_main_spec; the last rebuild could not read the main "
                "agent spec; remedy: restore read access, the next rebuild writes it"
            ),
            "process": (
                "cause=unreadable_main_spec_superseded_view; the process runs a view of "
                "the main agent spec built before the last unreadable-spec episode, and "
                "the spec has been rebuilt since; remedy: start the agent again"
            ),
            "session": (
                "cause=unreadable_main_spec_superseded_view; the skill views the start "
                "would use were prepared before the last unreadable-spec episode ended, "
                "and the main agent spec has been rebuilt since; remedy: start a new session"
            ),
            "read": (
                "cause=unreadable_main_spec_superseded_read; the agent was read by name "
                "before the last unreadable-spec episode ended, and the main agent spec "
                "has been rebuilt since; remedy: start the agent again"
            ),
        }[superseded]
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

        sel().log_governance_decision(
            session_key=HOST_SESSION_KEY,
            agent=agent,
            tool_name=f"agent_start:{agent}",
            scope="agent_start",
            item=agent,
            outcome="denied",
            reason=reason,
        )
    except Exception:
        with _main_spec_episode_lock:
            # The flag was read without this lock, so a close may have landed
            # during the SEL write: warn, but record no key for the next
            # episode to inherit.
            first_warning = not _main_spec_unreadable or _first_main_spec_warning("denial-audit")
        (logger.warning if first_warning else logger.debug)(
            "could not audit the refused start of agent %r; the start is refused anyway",
            agent,
            exc_info=True,
        )


def _is_main_spec_file(resolved: Path, agents_dir: Path, files: tuple[str, ...]) -> bool:
    """Whether *resolved* is the main spec or one of its mirrors, by path or file identity.

    The same file reached another way (a hard link, a symlink, a differently
    spelled path) is found by ``st_dev``/``st_ino``. Any error reading
    *resolved*'s identity, a missing file included, counts as the main spec: a
    doubt is never "unrelated". A main or mirror file that does not exist
    cannot be the file; any other error reading one is a doubt too.
    """
    for name in files:
        if resolved == agents_dir / name:
            return True
    try:
        os.stat(resolved)
    except OSError:
        return True
    for name in files:
        candidate = agents_dir / name
        try:
            if os.path.samefile(resolved, candidate):
                return True
        except FileNotFoundError:
            if os.path.lexists(candidate):
                # The candidate is there, so the source vanished (or the
                # candidate is a dangling link): a doubt, not "unrelated".
                return True
            continue
        except OSError:
            return True
    return False


def _main_spec_files() -> tuple[str, ...]:
    """The main spec's file name and the names of the specs mirrored from it."""
    return (AGENT_FILENAME, _HEARTBEAT_AGENT_FILENAME, WORKER_AGENT_FILENAME)


def _main_spec_own_names() -> set[str | None]:
    """The names the main spec and its mirrors are known by: their stems and its last declared name."""
    names: set[str | None] = {Path(name).stem for name in _main_spec_files()}
    names.add(_main_spec_declared_name)
    return names


def _is_main_spec_source(resolved: Path) -> bool:
    """:func:`_is_main_spec_file` against the live agents directory."""
    return _is_main_spec_file(resolved, kiro_agents_dir_path(), _main_spec_files())


def _governed_by_main_spec(agent: str, project_dir: str | Path | None = None) -> bool:
    """Whether a start of *agent* runs on ``kirocrew.json`` or a spec mirrored from it.

    Keyed on the spec FILE *agent* resolves to, never on its name: the main
    agent can be renamed in ``kirocrew.json``, and a name set misses every
    rename. A resolved file that is the main spec or one of its mirrors, by
    path or by file identity, is governed. Another readable spec is admitted only
    when it DECLARES *agent*: a file found by its stem alone loses to any spec
    declaring the name, and the unreadable main spec may be that spec once it
    reads again, so a stem match (a nameless spec, or one declaring another
    name) is governed for the episode. So is a name that resolves to no file,
    or cannot be resolved, since the resolver skips the unreadable main spec
    and it may be the one declaring the name. The names the main spec and its
    mirrors are known by (their stems and the name the last written main spec
    declared) take the same rule. Outside those, an unreadable main spec widens
    nothing: a user agent whose own file declares its name starts as usual.

    *project_dir* is the cwd the backend runs with. kiro-cli resolves
    ``--agent`` against ``<cwd>/.kiro/agents`` first, so a name a readable
    project spec declares runs that spec, not the main one, and is not governed.
    A name that resolves to the main spec or a mirror stays governed even then.
    """
    try:
        resolved = agent_spec_path(agent)
    except Exception:
        return True
    if resolved is not None and _is_main_spec_source(resolved):
        return True
    # The names the main spec and its mirrors are known by stay governed even
    # when a project declares them, so the main agent is never admitted on a
    # project file's say-so while its own spec cannot be read.
    if agent not in _main_spec_own_names() and agent in project_agent_names(
        project_dir, operation="require_fork_governance", source="unknown"
    ):
        return False
    if resolved is None:
        return True
    # Resolved to another file. Only a spec that DECLARES the name outranks the
    # main spec in the resolver; found by stem alone, the unreadable main spec
    # may be the one declaring it, and kiro-cli runs that once it reads again.
    return not _declares_agent_name(resolved, agent)


def _declares_agent_name(spec: Path, agent: str) -> bool:
    """Whether *spec* reads and declares *agent* as its ``name``; an unreadable spec does not."""
    try:
        data = _read_spec_capped(spec)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("name") == agent


def require_main_spec_projected(agent: str, project_dir: str | Path | None = None) -> None:
    """Refuse the main agent and its mirrors while their spec cannot be read.

    Their ``allowedTools`` bypass the PreToolUse gate, as a fork's do, and a
    spec the last rebuild could not read was not re-projected, so a session
    started on it could run grants the governance ceiling or a profile has since
    revoked. Fail-closed and bounded to the unreadable episode: on a healthy
    host the flag is clear and this returns at once. While it is set, every
    start this covers is refused, and the flag clears only when an existing
    rebuild path (:func:`retry_held_conductor_specs`, a config change the
    watcher applies, a restart) reads and writes the spec
    (:func:`_note_main_spec_readable`). A refusal schedules that same retry
    early (:func:`_schedule_main_spec_early_retry`, 5, 15 and 60 s apart, then
    every 60 s while the episode is open), so a start is admitted within about
    a minute of the lock clearing, and writes a best-effort SEL denial for every
    refusal (:func:`_audit_main_spec_denial`) before it raises. Which starts it covers is
    :func:`_governed_by_main_spec`, with *project_dir* the cwd the backend runs
    with. It resolves which spec the start runs on, reading specs to do so, and
    runs no rebuild. It is also the main-spec part of
    :func:`require_fork_governance` that a ``session/set_mode`` switch calls on
    its own: it reads no lineage sidecar and waits on no fork refresh.
    """
    if not _main_spec_unreadable or not _governed_by_main_spec(agent, project_dir):
        return
    raise ForkGovernanceUnresolved(refuse_unprojected_main_spec_start(agent))


def main_spec_episode() -> tuple[int, bool]:
    """The unreadable-main-spec episode as one reading: ``(revision, open)``.

    A reader that consumes a spec takes this before the read and hands it to
    :func:`unprojected_main_spec_source` after, so an episode that opened, or
    opened and closed, while it read is seen.
    """
    with _main_spec_episode_lock:
        return _main_spec_episode_revision, _main_spec_unreadable


def unprojected_main_spec_source(before: tuple[int, bool]) -> bool:
    """Whether bytes just read from a spec were read across an unreadable-main-spec episode.

    True when an episode was open at *before* (:func:`main_spec_episode`,
    taken before the read), is open now, or began in between, whatever file
    was read: no read during an episode can be shown not to be the kept main
    spec (a link, a rename or a retarget can put it under any name), so no read
    is classified. The view built from it is refused for that start or switch.
    Healthy host: one locked read, no filesystem access.
    """
    revision, unreadable = main_spec_episode()
    return bool(before[1] or unreadable or revision != before[0])


def require_main_spec_launch_bound(
    agent: str,
    project_dir: str | Path | None = None,
    *,
    before: tuple[int, bool] | None = None,
    view_source: str | Path | None = None,
    superseded: Literal["process", "session"] = "process",
) -> None:
    """Refuse every start while an unreadable-main-spec episode is open, bound to the episode.

    The start gate (:func:`require_main_spec_projected`) refuses the main agent
    and its mirrors. Every other start is refused here for the episode as well,
    one SEL denial and an early retry each (the gate's own refusal), because no
    start during it can be shown not to load the kept file: kiro-cli resolves a
    bare name (no prepared view: projection disabled or at its ceiling, an agent
    the projection has no entry for, an app launch copy, KAS's read by name) on
    its own, a renamed main agent cannot be learned from a file that cannot be
    read, and a link or a retarget can put the kept file under any name. So no
    name, project spec or view source is exempt. *project_dir* is unused here
    and kept for the callers' symmetry with the start gate.

    *before* is a :func:`main_spec_episode` reading taken before a read that
    Crew makes by name itself (the KAS host): the start is refused when an
    episode was open then, is open now or began in between. A start checked
    while no episode is open ran before the rebuild that failed, exactly as a
    start that runs before any rebuild applies a changed ceiling.

    *view_source* is for a session opened on a process that was launched on a
    prepared view: the spec file that view was built from, recorded at the
    launch (:attr:`NativeSkillProjection.sources`). For it, *before* is the
    episode reading that view was built against, recorded beside the source
    (:attr:`NativeSkillProjection.episodes`), and ``None`` means unknown. The
    process keeps that view for its whole life, and a rebuild that closes an
    episode rewrites the spec, never the view. So a view is admitted only when
    it was built while no episode was open and at the current revision: a view
    built before an episode opened may carry grants no rebuild re-projected,
    before the episode closes and after, whatever file it was read from. Since
    the revision only grows, such a refusal is permanent for that process,
    which its holder recycles.

    *superseded* names what replaces a view refused after the episode closed,
    for its message and SEL row (:func:`refuse_unprojected_main_spec_start`):
    ``"process"`` for a session open or launch frame, whose holder recycles the
    process, ``"session"`` for a ``session/set_mode`` frame, which recycles
    nothing and whose view the next session start prepares again. A bare name
    refused with no episode open is a spec read by name before it ended.
    """
    revision, unreadable = main_spec_episode()
    if view_source is not None:
        if before is not None and not (unreadable or before[1] or before[0] != revision):
            return
        # With no episode open the spec reads and was rebuilt: what is refused is
        # an old view, which the process's holder or the next session replaces.
        raise ForkGovernanceUnresolved(
            refuse_unprojected_main_spec_start(agent, superseded=superseded)
        )
    if not (unreadable or (before is not None and (before[1] or before[0] != revision))):
        return
    raise ForkGovernanceUnresolved(refuse_unprojected_main_spec_start(agent, superseded="read"))


def refuse_unprojected_main_spec_start(
    agent: str, *, superseded: Literal["process", "session", "read"] = "read"
) -> str:
    """Record the refusal of *agent*'s start on the unprojected main spec; return its message.

    Every refusal writes its SEL denial (:func:`_audit_main_spec_denial`) and
    schedules the early retry (:func:`_schedule_main_spec_early_retry`), whichever
    check refused: the start gate (:func:`require_main_spec_projected`), the launch
    bound (:func:`require_main_spec_launch_bound`) or the native skill projection,
    which refuses a view read across an episode
    (:func:`unprojected_main_spec_source`).

    While an episode is open the message says the spec could not be read and
    asks for read access; a refusal by name (*superseded* ``"read"``: the start
    gate, a bare name, a KAS read) of a name other than the main spec's own also
    says why that name is refused: it is resolved by name, and the unreadable
    spec may declare it. A refused view is not resolved by name, so it gets no
    such clause. With no episode open the spec reads and was rebuilt, so what is
    refused was read before the episode ended, and the message and SEL row say
    what replaces it instead, by *superseded*: ``"process"``, a view a process
    runs for life, which its holder replaces (start the agent again);
    ``"session"``, skill views prepared for a session, which the next session
    start prepares again (start a new session); ``"read"``, a spec read by name
    (start the agent again, which reads it anew).
    """
    path = kiro_agents_dir_path() / AGENT_FILENAME
    declared = (
        f" (it last declared the agent name {_main_spec_declared_name!r})"
        if _main_spec_declared_name
        else ""
    )
    kind = None if main_spec_episode()[1] else superseded
    _audit_main_spec_denial(agent, superseded=kind)
    _schedule_main_spec_early_retry()
    if kind == "process":
        return (
            f"agent {agent!r} may not start on this process: it runs a view of the "
            f"agent spec {path} built before the last unreadable-spec episode, so "
            "its auto-approvals were not re-projected through the current governance "
            "ceiling and profiles. The spec has been rebuilt and the process is being "
            "replaced; start the agent again"
        )
    if kind == "session":
        return (
            f"agent {agent!r} may not start: the skill views this start would use were "
            f"prepared before the last unreadable-spec episode on the agent spec {path} "
            "ended, so they predate the rebuild that re-projected its auto-approvals "
            "through the current governance ceiling and profiles. Start a new session, "
            "which prepares them again"
        )
    if kind == "read":
        return (
            f"agent {agent!r} may not start: it was read by name before the last "
            f"unreadable-spec episode on the agent spec {path} ended, and that spec may "
            "be the one the read found, so what was read predates the rebuild that "
            "re-projected its auto-approvals through the current governance ceiling and "
            "profiles. Start the agent again, which reads it anew"
        )
    by_name = (
        ""
        if agent in _main_spec_own_names() or superseded != "read"
        else f" {agent!r} is resolved by name when it starts, and that spec may declare it."
    )
    return (
        f"agent {agent!r} may not start: the last rebuild could not read the agent "
        f"spec {path}{declared}, so its auto-approvals were not re-projected through "
        f"the current governance ceiling and profiles.{by_name} Restore read access to "
        "the file if it is still denied. The next rebuild picks it up (an early retry "
        "within about a minute of the lock clearing, the hourly maintenance retry, or "
        "a gateway restart), and the agent can start once that rebuild has written it"
    )


def _skip_unreadable_main_spec(
    path: Path,
    exc: _AgentSpecUnreadable,
    refresh_forks: bool | Literal["defer"],
    *,
    clean: bool,
    wrote_out: list[bool] | None,
    held_out: list[bool] | None,
) -> Path:
    """End a rebuild that found the main spec present but unreadable.

    Writing defaults would erase the user's spec, so ``kirocrew.json``
    stays as it is, and so do the heartbeat and worker specs mirrored from it.
    The steps that write other files still run (:func:`_install_sibling_specs`),
    so a moved ceiling still re-filters the forks and conductor specs, and a
    fork refresh that cannot complete still records its failure for the fork
    gate. The skip is reported as an unwritten, held rebuild: ``wrote`` is
    False, so the ceiling memo stays pending and the default-model applier
    reports the change as not landed, and the held flag keeps the boot baseline
    unseeded and has the hourly maintenance wake retry the rebuild
    (:func:`retry_held_conductor_specs`). The flag and the warning come first,
    because the sibling steps can raise.

    The warning is once per episode, and so is the sibling pass unless the
    governance generation moves: the config watcher retries a failed
    default-model change every tick, and each retry lands here.
    """
    global _conductor_spec_held, _main_spec_unreadable, _main_spec_skip_generation
    global _main_spec_episode_revision
    from kiro_crew.platform.governance_profiles import governance_answer_generation

    cause = exc.__cause__
    errno_ = getattr(cause, "errno", None)
    with _main_spec_episode_lock:
        first_skip = not _main_spec_unreadable
        _conductor_spec_held = True
        _main_spec_unreadable = True
        _main_spec_episode_revision += 1
        first_warning = _first_main_spec_warning(f"read:{errno_}")
    (logger.warning if first_warning else logger.debug)(
        "cannot read the existing agent spec %s (%s); leaving it in place "
        "instead of resetting it to defaults until a rebuild can read it",
        path,
        exc,
    )
    # The sibling pass runs on the first skip of an episode and again only when
    # the governance generation moved since the last one: the config watcher
    # retries a pending default-model change every tick, and each retry lands
    # here, where a repeat pass under an unchanged generation re-filters nothing.
    # A pure read, no filesystem access.
    # The generation is recorded only after a pass that raised nothing, held no
    # conductor write and left no fork refresh deferred: a deferred refresh (the
    # boot path) settles after this returns, and a crew-bound shared template it
    # then holds is retried only by a later pass under the same generation.
    generation = governance_answer_generation()
    if first_skip or generation != _main_spec_skip_generation:
        try:
            gated_off = managed_mcp._gated_off_servers()
            sibling_held = _install_sibling_specs(
                refresh_forks, gated_off, clean=clean, mirror_main=False
            )
        finally:
            # Again after, raise or not: the sibling steps settle the memo to the
            # conductor installers' own verdict, which knows nothing of this skip.
            with _main_spec_episode_lock:
                _conductor_spec_held = True
        if not sibling_held and refresh_forks != "defer":
            with _main_spec_episode_lock:
                _main_spec_skip_generation = generation
    if wrote_out is not None:
        wrote_out.append(False)
    if held_out is not None:
        held_out.append(True)
    return path


def _note_main_spec_readable(path: Path, declared_name: str, episode_revision: int) -> None:
    """Close an unreadable-spec episode for *path* once a rebuild has written it.

    *declared_name* is the ``name`` the written spec declares. *episode_revision* is
    :data:`_main_spec_episode_revision` as the rebuild read it before it read the
    spec. A skip that landed since is a newer episode, one this write never read past,
    so it stays open with its retry; the older rebuild only records the name. Ends the
    episode's early retries and its audit-failure warning too, so the next
    episode gets its own.
    """
    global _main_spec_unreadable, _main_spec_skip_generation, _main_spec_declared_name
    global _main_spec_retry_timer, _main_spec_retry_attempts
    with _main_spec_episode_lock:
        _main_spec_declared_name = declared_name
        if _main_spec_episode_revision != episode_revision:
            return
        was_open = _main_spec_unreadable
        _main_spec_skip_generation = None
        _main_spec_unreadable = False
        pending, _main_spec_retry_timer = _main_spec_retry_timer, None
        _main_spec_retry_attempts = 0
        _main_spec_warned.clear()
    if pending is not None:
        with contextlib.suppress(Exception):
            pending.cancel()
    if was_open:
        logger.info("the agent spec %s reads again and was rebuilt", path)
