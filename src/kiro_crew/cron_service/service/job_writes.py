"""Add, update, enable and acknowledge cron jobs.

Each write has a synchronous form and an event-loop-safe ``*_async`` form whose
lock, reload, mutation and save run off the loop; the ``*_locked`` cores hold the
store lock for the whole transaction and refuse an unreadable store before they
mutate anything.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_jobs``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import milestones
from kiro_crew.cron_service.fields import validate_managed_by
from kiro_crew.cron_service.identity import bind_cron_memory
from kiro_crew.cron_service.model import CronJob

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Public API ──


def add_job(
    self: CronService,
    name: str,
    message: str,
    every_secs: int | None = None,
    at_ts: float | None = None,
    cron_expr: str | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
    delete_after_run: bool = False,
    created_by: str = "",
    approval_mode: str = "",
    enabled: bool = True,
    agent_id: str = "",
    member_id: str = "",
    model: str = "",
    silent: bool = False,
    timezone: str = "",
    skip_dates: list[str] | None = None,
    strict_schedule: bool = False,
    hide_in_chat: bool = False,
    folder_id: str = "",
    chat_folder_id: str = "",
    command: str = "",
    script: str = "",
    agent_sequence: list[str] | None = None,
    env: dict[str, str] | None = None,
    persistent_session: bool = True,
    session_key: str = "",
    minimal_context: bool = False,
    timeout: int = 0,
    timeout_secs: int = 0,
    auto_pause_after_failures: int | None = None,
    alert_after_failures: int | None = None,
) -> CronJob:
    """Add a new job. Provide one of ``every_secs``, ``at_ts``, or ``cron_expr``.

    ``enabled=False`` creates the job already paused (``user_paused=True``,
    mirroring :meth:`enable_job`) so the paused state is part of the FIRST
    persist — never an enabled-then-paused two-save window that a crash or
    a concurrent reader of the store could capture as enabled.

    ``timezone``/``skip_dates`` are validated HERE, at the persistence
    owner, and folded into the job before its single ``_save()`` -- so no
    caller can strand a half-populated or invalid job on disk, and every
    create path (MCP, apps SDK, dashboard, CLI) shares one check. This
    consolidates **every** first-save field
    (``agent_id``/``model``/``silent``/``strict_schedule``/``hide_in_chat``,
    ``command``/``script``/``agent_sequence``/``env``/``persistent_session``)
    into the same single locked build+persist, totalizing over all fields
    into the same single locked build+persist, totalizing over all fields
    the "fully-formed on first save" invariant. The MCP create path folds
    ``session_key``/``minimal_context``/``timeout`` here too, replacing its
    former create-then-mutate plus second unlocked ``_save()``.

    Synchronous variant: the lock+save runs INLINE and so must only be
    called from a loop-less context (CLI / MCP server process / a worker
    thread) — the ``_file_lock`` loop-safety guard rejects it on a running
    event loop. On the gateway loop use :meth:`add_job_async`. Accepts the
    same full field set as :meth:`add_job_async` so a caller (e.g. the
    synchronous ``CronSDK`` facade) can persist a fully-formed, owner-tagged
    job in the single locked transaction with no follow-up unlocked
    ``_save()``.
    """
    job = self._build_job(
        name,
        message,
        every_secs=every_secs,
        at_ts=at_ts,
        cron_expr=cron_expr,
        channel=channel,
        thread_ts=thread_ts,
        delete_after_run=delete_after_run,
        created_by=created_by,
        approval_mode=approval_mode,
        enabled=enabled,
        agent_id=agent_id,
        member_id=member_id,
        model=model,
        silent=silent,
        timezone=timezone,
        skip_dates=skip_dates,
        strict_schedule=strict_schedule,
        hide_in_chat=hide_in_chat,
        folder_id=folder_id,
        chat_folder_id=chat_folder_id,
        command=command,
        script=script,
        agent_sequence=agent_sequence,
        env=env,
        persistent_session=persistent_session,
        session_key=session_key,
        minimal_context=minimal_context,
        timeout=timeout,
        timeout_secs=timeout_secs,
        auto_pause_after_failures=auto_pause_after_failures,
        alert_after_failures=alert_after_failures,
    )
    self._persist_add_locked(job)
    self._arm_timer()
    logger.info("Added cron job '%s' (%s)", name, job.id)
    # The "first scheduled task" milestone, after the job is stored. A job an app
    # registers for itself (``created_by="app:<name>"``) is not the user
    # scheduling anything. Never raises.
    if not str(created_by or "").startswith("app:"):
        milestones.record("scheduled-task")
    return job


def add_job_if_absent(
    self: CronService,
    predicate: Callable[[CronJob], bool],
    **kwargs: Any,
) -> CronJob | None:
    """Build and persist a job only when no current store entry matches."""
    job = self._build_job(**kwargs)
    if not self._persist_add_if_absent_locked(predicate, job):
        return None
    self._arm_timer()
    return job


async def add_job_if_absent_async(
    self: CronService,
    predicate: Callable[[CronJob], bool],
    **kwargs: Any,
) -> CronJob | None:
    """Event-loop-native :meth:`add_job_if_absent`.

    Mirrors :meth:`add_job_async`: the job is built on-loop, the
    lock/sync/check/append/save core runs in a worker thread so the
    bounded ``_file_lock`` spin never parks the gateway loop, and timer
    arming stays on-loop. The absence check and the append happen under
    ONE store lock after a fresh ``_sync()``, so two concurrent
    registrars (e.g. a CLI enable racing gateway boot) cannot both
    observe the name as absent and persist duplicates. Returns None when
    a matching job already exists.
    """
    job = self._build_job(**kwargs)
    persisted = await asyncio.to_thread(self._persist_add_if_absent_locked, predicate, job)
    if not persisted:
        return None
    self._arm_timer()
    logger.info("Added cron job '%s' (%s) [if-absent]", job.name, job.id)
    return job


def add_managed_job(self: CronService, managed_by: str, **kwargs: Any) -> CronJob:
    """Create the job an installer owns under the key *managed_by*.

    *kwargs* are :meth:`add_job`'s fields and are validated by the same
    :func:`build_job` before any disk work. The key is unique across the
    store, and this is its only writer, so the uniqueness holds by
    construction: an existing job under the key is a refusal
    (``ValueError`` naming its id), checked INSIDE the store lock after
    ``_sync_for_write()`` so two installers racing the same key cannot both
    see it absent and persist two jobs.

    There is deliberately no replace-in-place. Every run-driven removal --
    a script that raises ``Done``, a one-shot consumed on completion, a
    deferred removal -- deletes its row BY ID with no record of which
    definition the run started from, so a replacement reusing the id would
    be deleted by the run it replaced. An installer re-installs with
    ``cron remove --managed-by KEY`` then ``cron add --managed-by KEY``: the
    new job gets a fresh id, so an in-flight run can only remove the dead
    one. Replace-in-place needs a definition-generation fence first.
    """
    validate_managed_by(managed_by)
    job = self._build_job(**kwargs)
    job.managed_by = managed_by
    # The shared locked check-then-append core: the key check runs after
    # the in-lock reload, so two racing installers cannot both persist.
    if not self._persist_add_if_absent_locked(lambda j: j.managed_by == managed_by, job):
        existing = ", ".join(
            j.id for j in self.list_jobs(include_disabled=True) if j.managed_by == managed_by
        )
        raise ValueError(
            f"a job is already managed by {managed_by!r} ({existing}); "
            f"remove it with 'cron remove --managed-by {managed_by}' first"
        )
    self._arm_timer()
    logger.info("Added managed cron job '%s' (%s) for %s", job.name, job.id, managed_by)
    return job


async def add_managed_job_async(self: CronService, managed_by: str, **kwargs: Any) -> CronJob:
    """Event-loop-native :meth:`add_managed_job`: same key check, off-loop disk work."""
    validate_managed_by(managed_by)
    job = self._build_job(**kwargs)
    job.managed_by = managed_by
    persisted = await asyncio.to_thread(
        self._persist_add_if_absent_locked, lambda j: j.managed_by == managed_by, job
    )
    if not persisted:
        raise ValueError(f"a job is already managed by {managed_by!r}")
    self._arm_timer()
    logger.info("Added managed cron job '%s' (%s) for %s", job.name, job.id, managed_by)
    return job


def _persist_add_if_absent_locked(
    self: CronService,
    predicate: Callable[[CronJob], bool],
    job: CronJob,
) -> bool:
    """Lock/reload/check/append/save — the atomic add-if-absent disk core.

    Like :meth:`_persist_add_locked` (no timer work, thread-safe, raises
    :class:`CronStoreBusy` on sustained contention) but the existence
    check happens INSIDE the same lock, after ``_sync()`` refreshed the
    in-memory view — closing the snapshot-then-append TOCTOU. Returns
    False when an existing job matches ``predicate``.

    Applies the same dead-parent guard as :meth:`_persist_add_locked`; see
    :meth:`_drop_owner_if_parent_gone`.
    """
    with self._file_lock():
        self._sync_for_write()
        if any(predicate(existing) for existing in self._jobs):
            return False
        bind_cron_memory(job)
        self._drop_owner_if_parent_gone(job)
        self._jobs.append(job)
        # A save that fails must leave the in-memory list as the caller found
        # it: the appended row is published to every reader the instant it
        # lands (``list_jobs`` hands out ``self._jobs`` itself), so a job the
        # store rejected would otherwise stay visible, be fired by the timer,
        # and be persisted by the next save that succeeds -- the exact
        # survive-a-failed-save defect the remove and update cores already
        # guard. Popping the row restores the list to exactly what disk still
        # holds, so no fingerprint reset is needed (the same reason
        # ``_update_job_locked`` omits one): the failed ``_save`` never reached
        # ``_record_fingerprint``, so the tracked digest still matches the
        # untouched file and the cache now matches it too. The remove cores
        # reset the fingerprint only because they reassign ``self._jobs`` and a
        # failed save leaves the cache missing rows that are still on disk.
        try:
            self._save()
        except BaseException:
            self._jobs.pop()
            raise
    job.audit_auto_pause_limit_change(None, job.session_key)
    return True


def _drop_owner_if_parent_gone(self: CronService, job: CronJob) -> None:
    """Blank a ``cron:`` owner whose parent is absent. MUST hold the store lock.

    Call after ``_sync_for_write()`` and before appending, so the decision is
    made against the authoritative reloaded store. This is what makes a child
    under a dead parent STRUCTURALLY impossible rather than merely cleaned up
    afterwards: the removal cascade
    (:meth:`_release_children_of_removed`) can only release children that
    existed when it scanned, and a run of the parent still in flight can call
    ``cron_add`` in the window between that scan and its own teardown — the
    new row would then be born stamped with a key no session can ever present
    again. Both halves resolve against the same in-lock reload, so whichever
    transaction lands second sees the other's write.

    Dropped rather than refused: the caller's request to schedule work is
    honoured and the row lands in the documented ownerless state the CLI and
    the Schedule page manage, which is the semantics every release path here
    uses. Refusing would lose the user's job over a race they did not cause.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    principal = seams.cron_job_id_from_session_key(job.session_key)
    if not principal or any(j.id == principal for j in self._jobs):
        return
    logger.warning(
        "Cron job %s created with owner %s whose cron no longer exists; "
        "storing it ownerless (manage from CLI or the Schedule page)",
        job.id,
        job.session_key,
    )
    job.session_key = ""


def _persist_add_locked(self: CronService, job: CronJob) -> None:
    """Lock/reload/append/save for a new job — the thread-safe disk core.

    Does NO timer work (``_arm_timer`` needs the event loop), so
    :meth:`add_job_async` can run it in an executor thread. Raises
    :class:`CronStoreBusy` if the store lock stays contended past the
    timeout. Mirrors the :meth:`_remove_jobs_locked` batch precedent.

    Applies the dead-parent guard before the append; see
    :meth:`_drop_owner_if_parent_gone`.
    """
    with self._file_lock():
        self._sync_for_write()
        bind_cron_memory(job)
        self._drop_owner_if_parent_gone(job)
        self._jobs.append(job)
        # Roll the append back on a failed save, for the reason
        # :meth:`_persist_add_if_absent_locked` spells out: a row the store
        # rejected must not survive in the live list. Popping restores the
        # list to what disk still holds, so no fingerprint reset is needed.
        try:
            self._save()
        except BaseException:
            self._jobs.pop()
            raise
    job.audit_auto_pause_limit_change(None, job.session_key)


async def add_job_async(
    self: CronService,
    name: str,
    message: str,
    every_secs: int | None = None,
    at_ts: float | None = None,
    cron_expr: str | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
    delete_after_run: bool = False,
    created_by: str = "",
    approval_mode: str = "",
    enabled: bool = True,
    agent_id: str = "",
    member_id: str = "",
    model: str = "",
    silent: bool = False,
    timezone: str = "",
    skip_dates: list[str] | None = None,
    strict_schedule: bool = False,
    hide_in_chat: bool = False,
    folder_id: str = "",
    chat_folder_id: str = "",
    command: str = "",
    script: str = "",
    agent_sequence: list[str] | None = None,
    env: dict[str, str] | None = None,
    persistent_session: bool = True,
    session_key: str = "",
    minimal_context: bool = False,
    timeout: int = 0,
    timeout_secs: int = 0,
    auto_pause_after_failures: int | None = None,
    alert_after_failures: int | None = None,
    source_preset: str = "",
    source_template_prompt: str = "",
) -> CronJob:
    """Event-loop-safe :meth:`add_job`: the lock+save runs off the loop.

    The gateway's aiohttp/Slack handlers run on the sole asyncio event loop;
    calling the sync :meth:`add_job` there parks the loop in the bounded lock
    spin under contention. This builds+validates on the loop (no I/O),
    offloads the lock+persist to a worker thread via ``asyncio.to_thread``
    (the disk core is thread-safe — flock on separate fds mutually excludes
    in-process too), then re-arms the timer back on the loop. Raises
    :class:`CronStoreBusy` (retryable) on sustained contention; the public
    boundaries translate it to a clean 409 / structured error.

    Optional presentation/routing fields (``agent_id``, ``model``,
    ``silent``, ``timezone``, ``strict_schedule``, ``hide_in_chat``) are
    applied during the single locked build+persist so callers never need a
    follow-up unlocked ``_save()`` (which could race a concurrent create and
    drop a job).
    """
    job = self._build_job(
        name,
        message,
        every_secs=every_secs,
        at_ts=at_ts,
        cron_expr=cron_expr,
        channel=channel,
        thread_ts=thread_ts,
        delete_after_run=delete_after_run,
        created_by=created_by,
        approval_mode=approval_mode,
        enabled=enabled,
        agent_id=agent_id,
        member_id=member_id,
        model=model,
        silent=silent,
        timezone=timezone,
        skip_dates=skip_dates,
        strict_schedule=strict_schedule,
        hide_in_chat=hide_in_chat,
        folder_id=folder_id,
        chat_folder_id=chat_folder_id,
        command=command,
        script=script,
        agent_sequence=agent_sequence,
        env=env,
        persistent_session=persistent_session,
        session_key=session_key,
        minimal_context=minimal_context,
        timeout=timeout,
        timeout_secs=timeout_secs,
        auto_pause_after_failures=auto_pause_after_failures,
        alert_after_failures=alert_after_failures,
    )
    # Dashboard-only template provenance. Set on the freshly-built job
    # BEFORE the off-loop persist -- the object has no other reference yet,
    # so this is still a single fully-formed first save, not a
    # build-then-mutate-then-second-save. Kept off _build_job because only
    # this async path is ever called with them (the sync add_job, CLI, MCP
    # and apps SDK never carry a template), so threading them through the
    # shared constructor would be surface with no consumer.
    if source_preset:
        job.source_preset = source_preset
        job.source_template_prompt = source_template_prompt
    await asyncio.to_thread(self._persist_add_locked, job)
    self._arm_timer()
    logger.info("Added cron job '%s' (%s)", name, job.id)
    if not str(created_by or "").startswith("app:"):
        milestones.record("scheduled-task")
    return job


def update_job(self: CronService, job_id: str, **kwargs: Any) -> CronJob | None:
    """Update fields on an existing job. Returns updated job or None if not found.

    Accepted kwargs: name, message, every_secs, cron_expr, agent_id, channel,
    approval_mode, silent, skip_dates, timezone, thread_ts, model,
    timeout_secs (per-wake execution budget, 1..86400),
    auto_pause_after_failures (0..10000; 0 = never auto-pause),
    alert_after_failures (1..10000; failures in a row before the alert is sent).

    Raises :class:`CronStoreBusy` if the store lock is contended past the
    timeout; see :meth:`update_job_async` for the event-loop-safe variant.
    """
    job = self._update_job_locked(job_id, **kwargs)
    if job is not None:
        self._arm_timer()
    return job


async def update_job_async(self: CronService, job_id: str, **kwargs: Any) -> CronJob | None:
    """Event-loop-safe :meth:`update_job`: the lock+save runs off the loop.

    Offloads the lock/reload/mutate/save core to a worker thread, then
    re-arms the timer on the loop. Raises :class:`CronStoreBusy` (retryable)
    on sustained contention.

    ``chat_folder_transition_out``: pass a dict to learn the folder
    ``chat_folder_id`` held before this call replaced it. Filled under the
    store's file lock, so it is atomic with the write; owned by the caller, so
    concurrent callers cannot see or overwrite each other's answer.
    """
    job = await asyncio.to_thread(self._update_job_locked_kw, job_id, kwargs)
    if job is not None:
        self._arm_timer()
    return job


def _update_job_locked_kw(self: CronService, job_id: str, kwargs: dict[str, Any]) -> CronJob | None:
    """``asyncio.to_thread`` shim so kwargs cross the thread boundary as a dict."""
    return self._update_job_locked(job_id, **kwargs)


def enable_job(
    self: CronService, job_id: str, enabled: bool = True, *, expected_owner: str | None = None
) -> bool:
    """Enable or disable a job by ID.

    Raises :class:`CronStoreBusy` on lock contention; see
    :meth:`enable_job_async` for the event-loop-safe variant.
    """
    ok = (
        self._enable_job_locked(job_id, enabled, expected_owner=expected_owner)
        if expected_owner is not None
        else self._enable_job_locked(job_id, enabled)
    )
    if ok:
        self._arm_timer()
    return ok


async def enable_job_async(
    self: CronService, job_id: str, enabled: bool = True, *, expected_owner: str | None = None
) -> bool:
    """Event-loop-safe :meth:`enable_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = (
        await asyncio.to_thread(
            self._enable_job_locked, job_id, enabled, expected_owner=expected_owner
        )
        if expected_owner is not None
        else await asyncio.to_thread(self._enable_job_locked, job_id, enabled)
    )
    if ok:
        self._arm_timer()
    return ok


def ack_job(self: CronService, job_id: str, summary: str) -> bool:
    """Acknowledge a cron notification — stores summary for future context.

    Raises :class:`CronStoreBusy` on lock contention; see
    :meth:`ack_job_async` for the event-loop-safe variant.
    """
    return self._ack_job_locked(job_id, summary)


async def ack_job_async(self: CronService, job_id: str, summary: str) -> bool:
    """Event-loop-safe :meth:`ack_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = await asyncio.to_thread(self._ack_job_locked, job_id, summary)
    # ack itself changes no schedule. If the worker's _sync() reloaded an
    # external change, its _load() re-armed the timer thread-safely via the
    # bound loop (see _arm_timer) — no drain needed here.
    return ok


def _ack_job_locked(self: CronService, job_id: str, summary: str) -> bool:
    """Lock/reload/mutate/save core of :meth:`ack_job`."""
    with self._file_lock():
        self._sync_for_write()
        for job in self._jobs:
            if job.id == job_id:
                before = list(job.acked_items)
                job.acked_items.append(summary[:500])
                # Keep only last 20 acks
                job.acked_items = job.acked_items[-20:]
                # Restore the list a failed save never persisted: the mutated
                # object is the one ``list_jobs`` already handed out, so a
                # rejected ack must not linger in the live cache.
                try:
                    self._save()
                except BaseException:
                    job.acked_items = before
                    raise
                return True
    return False


def unack_job(self: CronService, job_id: str) -> bool:
    """Remove the most recent acked item from a cron job.

    Raises :class:`CronStoreBusy` on lock contention; see
    :meth:`unack_job_async` for the event-loop-safe variant.
    """
    return self._unack_job_locked(job_id)


async def unack_job_async(self: CronService, job_id: str) -> bool:
    """Event-loop-safe :meth:`unack_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = await asyncio.to_thread(self._unack_job_locked, job_id)
    # See ack_job_async: any external-change re-arm self-heals in the worker.
    return ok


def _unack_job_locked(self: CronService, job_id: str) -> bool:
    """Lock/reload/mutate/save core of :meth:`unack_job`."""
    with self._file_lock():
        self._sync_for_write()
        for job in self._jobs:
            if job.id == job_id and job.acked_items:
                before = list(job.acked_items)
                job.acked_items.pop()
                # Restore the list a failed save never persisted (see
                # :meth:`_ack_job_locked`).
                try:
                    self._save()
                except BaseException:
                    job.acked_items = before
                    raise
                return True
    return False
