"""Cron SDK — app-scoped cron job management.

Wraps CronService with ownership enforcement so apps can only manage
their own cron jobs. Jobs are tagged with ``created_by = "app:{app_name}"``
for filtering and permission checks.

Concurrency safety & the sync/async contract
---------------------------------------------
The public mutation API — ``add_job`` / ``remove_job`` / ``update_job`` /
``remove_all`` — is **synchronous**, preserving the contract third-party App
Kit apps are written against (making them ``async def`` without a shim would
turn ``ctx.cron.add_job(...)`` into an un-awaited coroutine that never runs).
Each has an
``*_async`` sibling (``add_job_async`` / ``remove_job_async`` /
``update_job_async`` / ``remove_all_async``) for callers already on the gateway
event loop.

* **Sync methods never run on the loop.** ``_run_sync_mutator`` runs the
  blocking ``CronService`` mutator INLINE when there is no running event loop on
  the current thread (the genuinely loop-less contexts: CLI, MCP server process,
  a worker thread). When a loop IS running — an app calling the synchronous
  ``ctx.cron.*`` SDK from an on-loop ``on_startup`` hook or route handler — the
  call is REFUSED with ``CronSyncOnLoopError`` naming the ``*_async`` sibling.
  Offloading to a worker thread is not viable: the caller still has to
  block on the worker's result, so the loop stays parked for the bounded lock
  window and the whole gateway (chat, timers, heartbeats) stalls with it. Inline
  is not an option either — ``CronService._file_lock``'s structural guard
  rejects a store-lock acquisition on a thread with a live loop. On-loop sync
  callers must use ``*_async``. No in-tree caller is affected; ``bridges.py``
  and ``hooks_integration.py`` use the async variants exclusively.
* ``remove_all`` removes every owned job in ONE atomic
  ``CronService.remove_jobs_by_owner`` transaction (not a per-id loop, and not
  a cache-only id snapshot): the owned set is SELECTED inside the same
  ``_file_lock`` transaction that removes it, against the freshly-reloaded
  on-disk state, so a job this app created in another process since the last
  cache refresh is still seen and removed — closing the cross-process window
  where uninstall could delete the app while leaving an ENABLED owned cron
  orphaned. A contended store removes them all or none and raises
  ``CronStoreBusy`` — never a partial state that leaves some app jobs orphaned
  and still ENABLED. ``CronStoreBusy`` propagates so cleanup failure is
  reported, not masked as 0.
* CronService uses atomic_write (write-to-tmp + os.replace) for persistence;
  cross-process safety is handled by its ``fcntl.flock`` store lock.
* Timer (re)arming after a mutation is owned by CronService itself (via
  ``_arm_timer`` / ``call_soon_threadsafe``): no caller has to drain an arm.
* If a cron job is executing when ``remove_all()`` is called (e.g. on disable),
  the running job completes its current iteration but won't be scheduled again.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

from kiro_crew.cron_script import resolve_script_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class CronSyncOnLoopError(RuntimeError):
    """Raised when a synchronous ``CronSDK`` mutator is called on a running
    event loop, where it could only complete by parking that loop.

    Carries the name of the ``*_async`` sibling to call instead. Distinct from
    ``CronLoopSafetyError`` (which the store lock raises): this one is refused
    at the SDK boundary before any lock is attempted.
    """


def _run_sync_mutator(
    fn: Callable[..., _T], *args: Any, _api: str = "", **kwargs: Any
) -> _T:
    """Invoke a blocking ``CronService`` sync mutator, or REFUSE if the caller
    is on a running event loop.

    * No running loop (CLI / MCP process / worker thread) → run ``fn`` inline.
      This is the intended synchronous path and the published SDK contract.
    * A running loop on this thread → raise ``CronSyncOnLoopError``.

    Why refuse rather than offload: handing ``fn`` to a worker thread moves the
    ``_file_lock`` acquisition off the loop thread, but the caller still has to
    block on the worker's result — so the loop stays parked for the whole
    bounded lock window (up to ``_LOCK_TIMEOUT_SECS``), freezing every other
    gateway task (chat, timers, heartbeats). Relocating the lock does not
    unblock the loop. Nor can the mutator run inline on the loop: CronService's
    structural guard (``CronLoopSafetyError``) rejects a store-lock acquisition
    on a thread with a live loop. There is no correct synchronous on-loop
    answer, so the SDK fails fast and names the ``*_async`` sibling instead of
    silently trading an app's convenience for a gateway-wide stall.

    The refusal is deterministic and immediate — an app author hits it on the
    first run of an on-loop call site, not under production lock contention.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)  # loop-less — the intended synchronous path
    api = _api or getattr(fn, "__name__", "this method")
    raise CronSyncOnLoopError(
        f"CronSDK.{api}() is synchronous and cannot be called from a running "
        f"event loop: it would park the loop for the bounded cron-store lock "
        f"window and stall the whole gateway. Await CronSDK.{api}_async() "
        f"instead (same arguments and return value). Synchronous ctx.cron.* "
        f"calls remain supported off-loop (CLI, MCP process, worker thread)."
    )


class CronSDK:
    """App-scoped cron job management."""

    def __init__(self, app_name: str, cron_service: Any) -> None:
        self._app_name = app_name
        self._cron = cron_service
        self._owner_prefix = f"app:{app_name}"

    @property
    def app_name(self) -> str:
        return self._app_name

    # ── Shared vetting (deny-by-default, before any job is built) ──

    def _vet_command_script(self, name: str, command: str, script: str) -> None:
        """Vet ``command`` / ``script`` BEFORE a job is created (deny-by-default).

        A rejected payload never lands in the cron service's in-memory state.
        Safe even if a future caller reaches a mutator without the upstream
        ``bridges.py`` vetting; legitimate callers (which already vet and skip
        bad entries) are unaffected. command/script are NOT executed here — at
        fire time the gateway routes them through the OS-level sandbox
        (``cron_script.run_command_sandboxed`` / ``run_script_sandboxed``). The
        ``mcp_cron`` vetting imports are lazy to avoid the
        ``mcp_cron -> security -> ... -> bridges -> cron_sdk`` import cycle.
        Raises ``ValueError`` on rejection (SEL-audited).
        """
        if command:
            from kiro_crew.mcp_cron import _vet_shell_command

            err = _vet_shell_command(command)
            if err:
                sel().log_api_access(
                    caller="cron_sdk",
                    operation="cron_command_vetted",
                    outcome="denied",
                    resources=f"app={self._owner_prefix} cron={name}",
                    error=err,
                )
                raise ValueError(f"cron command rejected: {err}")
            sel().log_api_access(
                caller="cron_sdk",
                operation="cron_command_vetted",
                outcome="allowed",
                resources=f"app={self._owner_prefix} cron={name}",
            )
        if script:
            from kiro_crew.mcp_cron import _vet_script_file

            # resolve_script_path rejects paths outside ~/.kiro/crew/crons/ (and
            # missing/sensitive files) by raising. Emit a SEL denied audit on
            # that path too, mirroring bridges.py, so every denial is audited.
            try:
                file_path, _ = resolve_script_path(script)
            except (PermissionError, FileNotFoundError, ValueError) as exc:
                sel().log_api_access(
                    caller="cron_sdk",
                    operation="cron_script_vetted",
                    outcome="denied",
                    resources=f"app={self._owner_prefix} cron={name}",
                    error=str(exc),
                )
                raise ValueError(f"cron script rejected: {exc}") from exc
            err = _vet_script_file(file_path)
            if err:
                sel().log_api_access(
                    caller="cron_sdk",
                    operation="cron_script_vetted",
                    outcome="denied",
                    resources=f"app={self._owner_prefix} cron={name}",
                    error=err,
                )
                raise ValueError(f"cron script rejected: {err}")
            sel().log_api_access(
                caller="cron_sdk",
                operation="cron_script_vetted",
                outcome="allowed",
                resources=f"app={self._owner_prefix} cron={name}",
            )

    def _add_job_kwargs(
        self,
        name: str,
        message: str,
        *,
        every_secs: int | None,
        cron_expr: str | None,
        agent: str,
        command: str,
        script: str,
        agent_sequence: list[str] | None,
        env: dict[str, str] | None,
        persistent_session: bool,
        silent: bool,
        enabled: bool,
    ) -> dict[str, Any]:
        """Build the kwargs common to the sync/async ``CronService.add_job``.

        Threads every field so the job is persisted FULLY-FORMED and owner-tagged
        in ONE locked build+persist (no follow-up unlocked ``_save()`` that could
        race a concurrent create).
        """
        return dict(
            name=name,
            message=message,
            every_secs=every_secs,
            cron_expr=cron_expr,
            agent_id=agent or "",
            command=command or "",
            script=script or "",
            agent_sequence=agent_sequence or None,
            env=env or None,
            persistent_session=persistent_session,
            silent=silent,
            enabled=enabled,
            created_by=self._owner_prefix,
        )

    # ── Create ──

    def add_job(
        self,
        name: str,
        message: str,
        *,
        every_secs: int | None = None,
        cron_expr: str | None = None,
        agent: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        silent: bool = False,
        enabled: bool = True,
    ) -> Any:
        """Create a cron job owned by this app. **Synchronous** (preserves the
        published SDK contract). See :meth:`add_job_async` for the loop-native
        variant. Raises ``CronSyncOnLoopError`` if called on a running event loop
        (use :meth:`add_job_async` there). Returns the created CronJob object.
        """
        self._vet_command_script(name, command, script)
        job = _run_sync_mutator(
            self._cron.add_job,
            _api="add_job",
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled,
            ),
        )
        self._audit_add(job)
        return job

    async def add_job_async(
        self,
        name: str,
        message: str,
        *,
        every_secs: int | None = None,
        cron_expr: str | None = None,
        agent: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        silent: bool = False,
        enabled: bool = True,
    ) -> Any:
        """Event-loop-native :meth:`add_job`: routes through
        ``CronService.add_job_async`` (bounded store-lock spin offloaded to a
        worker thread), so an on-loop caller awaits without ever parking the
        loop. Returns the created CronJob object.
        """
        self._vet_command_script(name, command, script)
        job = await self._cron.add_job_async(
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled,
            ),
        )
        self._audit_add(job)
        return job

    async def add_job_if_absent_async(
        self,
        name: str,
        message: str,
        *,
        every_secs: int | None = None,
        cron_expr: str | None = None,
        agent: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        silent: bool = False,
        enabled: bool = True,
    ) -> Any:
        """Atomic add-if-absent by job name; returns None when already present.

        Routes through ``CronService.add_job_if_absent``, whose existence check
        and append happen under ONE store file lock after a fresh ``_sync()`` —
        so two concurrent registrars (e.g. a CLI enable racing gateway boot)
        cannot both snapshot the name as absent and persist duplicates. The
        bounded lock spin runs in a worker thread, keeping on-loop callers safe.
        """
        self._vet_command_script(name, command, script)
        job = await self._cron.add_job_if_absent_async(
            lambda existing, n=name: existing.name == n,
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled,
            ),
        )
        if job is not None:
            self._audit_add(job)
        return job

    def _audit_add(self, job: Any) -> None:
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_add_job",
            outcome="ok",
            resources=job.id,
        )
        logger.info("App %s created cron job: %s (id=%s)", self._app_name, job.name, job.id)

    # ── Read ──

    def list_jobs(self) -> list[Any]:
        """List only jobs owned by this app."""
        return [
            j for j in self._cron.list_jobs(include_disabled=True)
            if getattr(j, "created_by", "") == self._owner_prefix
        ]

    # ── Remove one ──

    def remove_job(self, job_id: str) -> bool:
        """Remove a job only if owned by this app. **Synchronous**; raises
        ``CronSyncOnLoopError`` on a running event loop (use
        :meth:`remove_job_async` there). Raises ``PermissionError`` if the job
        belongs to a different app.
        """
        self._assert_owned(job_id, "cron_remove_job")
        result = _run_sync_mutator(
            self._cron.remove_job, job_id, _api="remove_job"
        )
        self._audit_remove(job_id)
        return result

    async def remove_job_async(self, job_id: str) -> bool:
        """Event-loop-native :meth:`remove_job` (routes through
        ``CronService.remove_job_async``)."""
        self._assert_owned(job_id, "cron_remove_job")
        result = await self._cron.remove_job_async(job_id)
        self._audit_remove(job_id)
        return result

    def _audit_remove(self, job_id: str) -> None:
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_remove_job",
            outcome="ok",
            resources=job_id,
        )
        logger.info("App %s removed cron job: %s", self._app_name, job_id)

    # ── Update ──

    def update_job(self, job_id: str, **kwargs: Any) -> Any:
        """Update a job only if owned by this app. **Synchronous**; raises
        ``CronSyncOnLoopError`` on a running event loop (use
        :meth:`update_job_async` there). Raises ``PermissionError`` if the job
        belongs to a different app.
        Returns the updated CronJob or None.
        """
        self._assert_owned(job_id, "cron_update_job")
        result = _run_sync_mutator(
            self._cron.update_job, job_id, _api="update_job", **kwargs
        )
        self._audit_update(job_id)
        return result

    async def update_job_async(self, job_id: str, **kwargs: Any) -> Any:
        """Event-loop-native :meth:`update_job` (routes through
        ``CronService.update_job_async``)."""
        self._assert_owned(job_id, "cron_update_job")
        result = await self._cron.update_job_async(job_id, **kwargs)
        self._audit_update(job_id)
        return result

    def _audit_update(self, job_id: str) -> None:
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_update_job",
            outcome="ok",
            resources=job_id,
        )
        logger.info("App %s updated cron job: %s", self._app_name, job_id)

    # ── Remove all (atomic) ──

    def remove_all(self) -> int:
        """Remove all jobs owned by this app in ONE atomic transaction.

        Called on disable/uninstall. Delegates to
        ``CronService.remove_jobs_by_owner_sync``, which — inside a SINGLE
        ``_file_lock`` transaction — reloads from disk, SELECTS every job whose
        ``created_by`` matches this app, removes them, and saves. Selecting the
        owned set INSIDE the lock (against the freshly-reloaded on-disk state,
        not a cache-only ``list_jobs()`` snapshot) is what closes the
        cross-process orphan window: a job this app created in another process
        since the last cache refresh is still seen and removed, so uninstall
        cannot delete the app while leaving an ENABLED owned cron behind.
        All-or-nothing — a contended store removes them all or none and raises
        ``CronStoreBusy``, never a partial state that leaves some app jobs
        orphaned and still ENABLED. **Synchronous**; raises
        ``CronSyncOnLoopError`` on a running event loop (use
        :meth:`remove_all_async` there). Propagates ``CronStoreBusy`` so a
        failed cleanup is reported, not masked as ``0``. Returns the count
        removed.
        """
        removed = _run_sync_mutator(
            self._cron.remove_jobs_by_owner_sync, self._owner_prefix, _api="remove_all"
        )
        self._audit_remove_all(removed)
        return len(removed)

    async def remove_all_async(self) -> int:
        """Event-loop-native :meth:`remove_all`: routes through the atomic
        ``CronService.remove_jobs_by_owner`` (off-loop worker). Selects the
        owned set INSIDE the lock against the reloaded on-disk state (closing
        the cross-process orphan window), with the same all-or-nothing
        guarantee and ``CronStoreBusy`` propagation."""
        removed = await self._cron.remove_jobs_by_owner(self._owner_prefix)
        self._audit_remove_all(removed)
        return len(removed)

    def _audit_remove_all(self, removed: list[str]) -> None:
        if not removed:
            return
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_remove_all",
            outcome="ok",
            resources=",".join(removed),
        )
        logger.info("App %s removed %d cron job(s)", self._app_name, len(removed))

    # ── Ownership helpers ──

    def _assert_owned(self, job_id: str, operation: str) -> None:
        """Raise ``PermissionError`` (SEL-audited) if ``job_id`` isn't ours."""
        if self._find_owned_job(job_id) is None:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation=operation,
                outcome="denied",
                resources=job_id,
                error="ownership violation",
            )
            raise PermissionError(f"Job {job_id} not owned by app {self._app_name}")

    def _find_owned_job(self, job_id: str) -> Any | None:
        """Find a job by ID, only if owned by this app."""
        for job in self._cron.list_jobs(include_disabled=True):
            if job.id == job_id and getattr(job, "created_by", "") == self._owner_prefix:
                return job
        return None
