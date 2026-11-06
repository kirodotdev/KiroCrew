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
import hashlib
import json
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
    project_path: str = "",
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
    person_owner: bool = False,
) -> CronJob:
    """Add a new job. Provide one of ``every_secs``, ``at_ts``, or ``cron_expr``.

    ``enabled=False`` creates the job already paused (``user_paused=True``,
    as :meth:`enable_job` does) in the FIRST persist, so no crash or
    concurrent reader can catch it enabled between two saves.

    Every field, ``timezone``/``skip_dates`` validation included, is
    validated and folded in before the single locked ``_save()``, so no
    create path (MCP, apps SDK, dashboard, CLI) can strand a half-built or
    invalid job on disk or need a follow-up unlocked ``_save()``.

    Synchronous: the lock+save runs INLINE, so call it only without a
    running loop (CLI, MCP server process, worker thread); the
    ``_file_lock`` guard rejects it on a running event loop. On the gateway
    loop use :meth:`add_job_async`, which takes the same fields.
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
        project_path=project_path,
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
    # Only MCP ``cron_add`` knows this. A key, not a flag: a moved session_key voids it.
    if person_owner:
        job.person_owner_key = job.session_key
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

    *kwargs* are :meth:`add_job`'s fields, validated by :func:`build_job`
    before any disk work. The key is unique across the store and this is
    its only writer: an existing job under the key raises ``ValueError``
    naming its id, checked INSIDE the store lock after
    ``_sync_for_write()`` so two racing installers cannot both persist.

    No replace-in-place: run-driven removals (``Done``, a consumed
    one-shot, a deferred removal) delete BY ID, so a replacement reusing
    the id would be deleted by the run it replaced. Re-install with
    ``cron remove --managed-by KEY`` then ``cron add --managed-by KEY``;
    the fresh id keeps an in-flight run on the dead job. Replace-in-place
    needs a definition-generation fence first.
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
        # A failed save must leave the list as disk still holds it: readers
        # see ``self._jobs`` itself, so a rejected row would be fired by the
        # timer and persisted by the next good save. Popping it restores the
        # list exactly; the failed ``_save`` never reached
        # ``_record_fingerprint``, so no fingerprint reset is needed (unlike
        # the remove cores, which reassign ``self._jobs``).
        try:
            self._save()
        except BaseException:
            self._jobs.pop()
            raise
    job.audit_auto_pause_limit_change(None, job.session_key)
    return True


def _drop_owner_if_parent_gone(self: CronService, job: CronJob) -> None:
    """Blank a ``cron:`` owner whose parent is absent. MUST hold the store lock.

    Call after ``_sync_for_write()`` and before appending. The removal
    cascade (:meth:`_release_children_of_removed`) releases only children
    present when it scanned, and an in-flight run of the parent can
    ``cron_add`` after that scan; checking here against the same in-lock
    reload makes a child under a dead parent impossible, whichever
    transaction lands second.

    Dropped rather than refused: the job lands in the ownerless state the
    CLI and the Schedule page manage, as every release path does, instead
    of losing the user's job over a race they did not cause.
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
    project_path: str = "",
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

    The sync :meth:`add_job` would park the gateway's one event loop in the
    lock spin. This builds and validates on the loop (no I/O), runs the
    lock+persist via ``asyncio.to_thread`` (flock on separate fds excludes
    in-process too), then re-arms the timer on the loop. Raises
    :class:`CronStoreBusy` (retryable) on sustained contention; the public
    boundaries turn it into a 409 / structured error.

    Every field is applied in the single locked build+persist, so callers
    never need a follow-up unlocked ``_save()`` that could race a
    concurrent create and drop a job.
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
        project_path=project_path,
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
    project_path ("" clears it; agent jobs only),
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


def ack_job(self: CronService, job_id: str, summary: str, ack_id: str = "") -> bool:
    """Acknowledge a cron notification — stores summary for future context.

    *ack_id* names this entry so :meth:`unack_job` can take back exactly it.
    Raises :class:`CronStoreBusy` on lock contention; see
    :meth:`ack_job_async` for the event-loop-safe variant.
    """
    return self._ack_job_locked(job_id, summary, ack_id)


async def ack_job_async(self: CronService, job_id: str, summary: str, ack_id: str = "") -> bool:
    """Event-loop-safe :meth:`ack_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = await asyncio.to_thread(self._ack_job_locked, job_id, summary, ack_id)
    # ack itself changes no schedule. If the worker's _sync() reloaded an
    # external change, its _load() re-armed the timer thread-safely via the
    # bound loop (see _arm_timer) — no drain needed here.
    return ok


def _acked_items_digest(items: list[str]) -> str:
    """A fingerprint of *items*, saved beside the ids written against them."""
    # ASCII-escaped, so a lone surrogate (an emoji cut in half by a client's
    # 200-character slice) hashes instead of failing to encode.
    encoded = json.dumps(items, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16]


def _aligned_ack_ids(job: CronJob) -> list[str]:
    """``job.acked_item_ids`` as one id per ``acked_items`` entry ("" = none).

    The ids count only while the items are the ones they were written
    against (same length and the saved digest). A writer that changed the
    items and kept the ids, as an older build does, leaves them naming no
    entry at all, so an unack by id finds nothing rather than the wrong entry.
    """
    ids = job.acked_item_ids
    items = job.acked_items
    if (
        not isinstance(ids, list)
        or len(ids) != len(items)
        or job.acked_item_ids_digest != _acked_items_digest(items)
    ):
        return [""] * len(items)
    return [i if isinstance(i, str) else "" for i in ids]


def _ack_job_locked(self: CronService, job_id: str, summary: str, ack_id: str = "") -> bool:
    """Lock/reload/mutate/save core of :meth:`ack_job`."""
    with self._file_lock():
        self._sync_for_write()
        for job in self._jobs:
            if job.id == job_id:
                before = job.acked_items
                before_ids = job.acked_item_ids
                before_digest = job.acked_item_ids_digest
                # Built before any of it is assigned, so a failure here leaves
                # the job as it was. Keep only last 20 acks.
                items = [*job.acked_items, summary[:500]][-20:]
                ids = [*_aligned_ack_ids(job), ack_id][-20:]
                digest = _acked_items_digest(items)
                job.acked_items, job.acked_item_ids, job.acked_item_ids_digest = items, ids, digest
                # Restore the list a failed save never persisted: the mutated
                # object is the one ``list_jobs`` already handed out, so a
                # rejected ack must not linger in the live cache.
                try:
                    self._save()
                except BaseException:
                    job.acked_items = before
                    job.acked_item_ids = before_ids
                    job.acked_item_ids_digest = before_digest
                    raise
                return True
    return False


def unack_job(self: CronService, job_id: str, ack_id: str | None = None) -> bool:
    """Remove an acked item from a cron job: the one *ack_id* names, else the newest.

    With *ack_id*, only the entry its :meth:`ack_job` added is removed; a job
    that does not hold it (aged out of the last 20, or already removed) is
    left alone (``False``), so neither a repeat nor an equal summary
    acknowledged later can lose a different entry. Raises
    :class:`CronStoreBusy` on lock contention; see :meth:`unack_job_async`
    for the event-loop-safe variant.
    """
    return self._unack_job_locked(job_id, ack_id)


async def unack_job_async(self: CronService, job_id: str, ack_id: str | None = None) -> bool:
    """Event-loop-safe :meth:`unack_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = await asyncio.to_thread(self._unack_job_locked, job_id, ack_id)
    # See ack_job_async: any external-change re-arm self-heals in the worker.
    return ok


def _unack_job_locked(self: CronService, job_id: str, ack_id: str | None = None) -> bool:
    """Lock/reload/mutate/save core of :meth:`unack_job`."""
    with self._file_lock():
        self._sync_for_write()
        for job in self._jobs:
            if job.id == job_id and job.acked_items:
                ids = _aligned_ack_ids(job)
                if ack_id is None:
                    index = len(job.acked_items) - 1
                else:
                    if not ack_id or ack_id not in ids:
                        return False
                    index = ids.index(ack_id)
                before = job.acked_items
                before_ids = job.acked_item_ids
                before_digest = job.acked_item_ids_digest
                # Built before any of it is assigned (see :meth:`_ack_job_locked`).
                items = job.acked_items[:index] + job.acked_items[index + 1 :]
                del ids[index]
                digest = _acked_items_digest(items)
                job.acked_items, job.acked_item_ids, job.acked_item_ids_digest = items, ids, digest
                # Restore the list a failed save never persisted (see
                # :meth:`_ack_job_locked`).
                try:
                    self._save()
                except BaseException:
                    job.acked_items = before
                    job.acked_item_ids = before_ids
                    job.acked_item_ids_digest = before_digest
                    raise
                return True
    return False
