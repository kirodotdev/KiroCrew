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
from pathlib import Path
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
    _refuse_sync_on_loop(_api or getattr(fn, "__name__", "this method"))
    return fn(*args, **kwargs)  # loop-less — the intended synchronous path


def _refuse_sync_on_loop(api: str) -> None:
    """Raise ``CronSyncOnLoopError`` when this thread has a running event loop.

    Extracted rather than copied so the refusal has ONE spelling. Two sites need
    it and they must agree: :func:`_run_sync_mutator`, which guards the store
    mutation itself, and the synchronous ``add_job``, which must refuse BEFORE it
    vets. A second hand-written copy of the predicate is how the two drift.

    The earlier ordering refused only at the mutator, so an on-loop caller paid
    the vet's filesystem work first -- a stat, a body read for the content scan,
    and on the first call for that SDK instance a walk of the builtin manifest
    sources -- and got the refusal afterwards. The refusal is supposed to be the
    cheap, immediate answer an app author hits on their first on-loop call, so
    doing disk work on the loop before producing it defeats the point of failing
    fast. Refusing first costs nothing on the off-loop path, which is the only
    path that goes on to do the work.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # loop-less — the caller may proceed on its own thread
    raise CronSyncOnLoopError(
        f"CronSDK.{api}() is synchronous and cannot be called from a running "
        f"event loop: it would park the loop for the bounded cron-store lock "
        f"window and stall the whole gateway. Await CronSDK.{api}_async() "
        f"instead (same arguments and return value). Synchronous ctx.cron.* "
        f"calls remain supported off-loop (CLI, MCP process, worker thread)."
    )


#: Prefix the host writes into a cron job's ``created_by`` to mark the job as
#: owned by an installed app, rather than by a person.
#:
#: ``created_by`` is shared with a human creator's Slack user ID, so the prefix
#: is what separates the two readings. An app never supplies the value --
#: :class:`CronSDK` stamps it from the app name the host resolved -- so an app
#: can neither claim another app's jobs nor disguise its own as a person's.
APP_OWNER_PREFIX = "app:"


def owner_tag(app_name: str) -> str:
    """The ``created_by`` value marking a cron job as owned by *app_name*."""
    return f"{APP_OWNER_PREFIX}{app_name}"


def app_owner_name(created_by: str | None) -> str:
    """The app owning a job with this ``created_by``, or ``""`` if not app-owned.

    Returning the empty string rather than ``None`` for "not an app" keeps the
    result usable in a boolean test without the caller distinguishing an absent
    field from a person-owned one: both mean the same thing here.

    A bare ``"app:"`` names no app and is rejected. It is reachable: the stamp
    is built from a resolved app name, and an empty name would otherwise yield
    an empty key that matches no app while still reading as app-owned.
    """
    value = created_by or ""
    if not value.startswith(APP_OWNER_PREFIX):
        return ""
    return value[len(APP_OWNER_PREFIX) :]


class CronSDK:
    """App-scoped cron job management."""

    def __init__(self, app_name: str, cron_service: Any) -> None:
        self._app_name = app_name
        self._bundle_root: Path | None = None
        self._bundle_root_cached = False
        self._cron = cron_service
        self._owner_prefix = owner_tag(app_name)

    @property
    def app_name(self) -> str:
        return self._app_name

    # ── Shared vetting (deny-by-default, before any job is built) ──

    def _app_bundle_root(self) -> Path | None:
        """This app's own tree, the base a relative ``script`` spec resolves against.

        Derived ONLY from ``self._app_name``, which this SDK instance was
        constructed with and no caller can change. That is a security boundary,
        not a convenience: ``ctx.cron`` hands a ``CronSDK`` to every app holding
        the ``cron`` permission, and the resolved root is what
        :func:`resolve_script_path` confines the script to before it is persisted
        for the launcher to execute. A root taken from a method keyword would let
        one app name a root of its choosing and get a ``.py`` under it executed,
        which is the cross-bundle confinement bypass the root exists to prevent.

        Chosen the same way ``apps.bridges._registration_source`` chooses it: a
        shipped builtin's bundle is its IMMUTABLE package directory, so a mutable
        installed directory borrowing a builtin's name cannot supply the script;
        everything else uses the installed snapshot. Imports are function-local,
        matching the ``mcp_cron`` imports below, because ``bridges`` imports this
        module and a module-level edge back into the apps package would close a
        cycle.

        BLOCKING on its first call: ``shipped_builtin_app_root`` WALKS the builtin
        manifest sources (``iterdir``, then ``resolve`` + ``read_text`` +
        ``json.loads`` per entry). MEMOISED per instance so a registrar looping
        over an app's jobs pays that walk once, not once per job, and the async
        mutators run the vet that reaches it inside ``asyncio.to_thread`` so the
        walk never lands on the gateway event loop.

        Returns ``None`` when the root cannot be determined, which leaves
        :func:`resolve_script_path` on its context-free behaviour rather than
        inventing a base directory.
        """
        if self._bundle_root_cached:
            return self._bundle_root
        try:
            from kiro_crew.apps.execution import shipped_builtin_app_root
            from kiro_crew.apps.manager import app_dir

            shipped = shipped_builtin_app_root(self._app_name)
            self._bundle_root = shipped if shipped is not None else app_dir(self._app_name)
        except Exception:  # noqa: BLE001 — an undeterminable root must not deny the job
            self._bundle_root = None
        self._bundle_root_cached = True
        return self._bundle_root

    def _vet_command_script(self, name: str, command: str, script: str) -> str:
        """Vet ``command`` / ``script`` BEFORE a job is created (deny-by-default).

        Returns the ``script`` spec to PERSIST: unchanged when empty, otherwise
        the resolved absolute ``"<path>.py:<func>"``. Callers store the returned
        value, because every later consumer re-resolves ``job.script`` holding
        no app context — the fire-time governance gate, the launcher, the
        dashboard source endpoint — and a relative spec on the record would be
        re-resolved against THEIR process CWD.

        A rejected payload never lands in the cron service's in-memory state.
        Safe even if a future caller reaches a mutator without the upstream
        ``bridges.py`` vetting; legitimate callers (which already vet and skip
        bad entries) are unaffected. command/script are NOT executed here — at
        fire time the gateway routes them through the OS-level sandbox
        (``cron_script.run_command_sandboxed`` / ``run_script_sandboxed``). The
        ``mcp_cron`` vetting imports are lazy to avoid the
        ``mcp_cron -> security -> ... -> bridges -> cron_sdk`` import cycle.
        BLOCKING, and it always was: it stats the script, reads its body for the
        content scan, and on its first call for this instance walks the builtin
        manifest sources to find the bundle. So it runs either on a thread the
        caller already owns -- the sync ``add_job`` path, which calls
        ``_refuse_sync_on_loop`` BEFORE reaching here, so an on-loop caller is
        turned away without paying any of this -- or inside ``asyncio.to_thread``,
        which is how both async mutators call it. An earlier revision named
        ``_run_sync_mutator`` as the guard for the sync path; that refusal is real
        but lands AFTER this function, so it did not keep the work off the loop. The
        bundle root is NOT a parameter: it is derived from ``self._app_name``
        alone, because a caller-chosen root would be a confinement bypass (see
        :meth:`_app_bundle_root`).

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

            # resolve_script_path rejects a missing or sensitive file, and any
            # path outside ~/.kiro/crew/crons/ or this app's own bundle, by
            # raising. Emit a SEL denied audit on that path too, mirroring
            # bridges.py, so every denial is audited. `app_root` is what makes a
            # bundle-relative spec ("job.py:run", the only spelling an app can
            # write without knowing the install location) resolve next to the
            # app instead of against the gateway process's CWD.
            try:
                file_path, func_name = resolve_script_path(script, app_root=self._app_bundle_root())
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
            return f"{file_path}:{func_name}"
        return script

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
        timezone: str,
        skip_dates: list[str] | None,
        folder_id: str = "",
    ) -> dict[str, Any]:
        """Build the kwargs common to the sync/async ``CronService.add_job``.

        Threads every field so the job is persisted FULLY-FORMED and owner-tagged
        in ONE locked build+persist (no follow-up unlocked ``_save()`` that could
        race a concurrent create).

        ``timezone``/``skip_dates`` are threaded here rather than left to a
        follow-up ``update_job``: they are the two calendar-validity-sensitive
        fields ``CronService.add_job`` owns and validates before its single
        locked save, so passing them at create keeps the fully-formed-on-first-
        save invariant instead of persisting a job that resolves to UTC and
        correcting it in a second write.
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
            timezone=timezone or "",
            skip_dates=skip_dates or None,
            folder_id=folder_id or "",
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
        timezone: str = "",
        skip_dates: list[str] | None = None,
    ) -> Any:
        """Create a cron job owned by this app. **Synchronous** (preserves the
        published SDK contract). See :meth:`add_job_async` for the loop-native
        variant. Raises ``CronSyncOnLoopError`` if called on a running event loop
        (use :meth:`add_job_async` there). Returns the created CronJob object.

        ``timezone`` is an IANA zone name (e.g. ``"America/Los_Angeles"``) that
        the schedule and any ``skip_dates`` are evaluated in. Leaving it empty
        falls back to the gateway config's timezone and then to UTC, so an app
        scheduling against a user's local time should pass it explicitly.
        ``CronService.add_job`` validates both fields before the single locked
        save, so an unknown zone or a malformed ``YYYY-MM-DD`` raises
        ``ValueError`` at create time instead of silently resolving to UTC when
        the job fires.

        A manifest folder is NOT assignable here: app cron registration goes
        through :meth:`add_job_if_absent_async`, which is the only method
        ``bridges`` calls, so a ``folder_id`` on this method would be an
        untested parameter no caller reaches.
        """
        # Refuse BEFORE vetting, not after. The vet is blocking -- a stat, a body
        # read for the content scan, and on this instance's first call a walk of
        # the builtin manifest sources -- and `_run_sync_mutator` below would only
        # refuse once that work was already done on the loop. Same predicate, one
        # spelling: see `_refuse_sync_on_loop`.
        _refuse_sync_on_loop("add_job")
        script = self._vet_command_script(name, command, script)
        job = _run_sync_mutator(
            self._cron.add_job,
            _api="add_job",
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled, timezone=timezone, skip_dates=skip_dates,
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
        timezone: str = "",
        skip_dates: list[str] | None = None,
    ) -> Any:
        """Event-loop-native :meth:`add_job`: routes through
        ``CronService.add_job_async`` (bounded store-lock spin offloaded to a
        worker thread), so an on-loop caller awaits without ever parking the
        loop. Returns the created CronJob object.

        ``timezone``/``skip_dates`` behave exactly as in :meth:`add_job` --
        validated at the persistence owner and folded into the single locked
        save, so a job never exists with the wrong calendar settings. Folder
        assignment is likewise absent for the reason given there.
        """
        # Off-loop: the vet stats the script, reads its body for the content
        # scan, and may walk the builtin manifest sources for the bundle root.
        # This coroutine is awaited on the gateway loop (app enable, gateway
        # start), so running that filesystem work inline would park every request
        # and the heartbeat for its duration.
        script = await asyncio.to_thread(self._vet_command_script, name, command, script)
        job = await self._cron.add_job_async(
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled, timezone=timezone, skip_dates=skip_dates,
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
        timezone: str = "",
        skip_dates: list[str] | None = None,
        folder_id: str = "",
    ) -> Any:
        """Atomic add-if-absent by job name; returns None when already present.

        Routes through ``CronService.add_job_if_absent``, whose existence check
        and append happen under ONE store file lock after a fresh ``_sync()`` —
        so two concurrent registrars (e.g. a CLI enable racing gateway boot)
        cannot both snapshot the name as absent and persist duplicates. The
        bounded lock spin runs in a worker thread, keeping on-loop callers safe.

        ``timezone``/``skip_dates`` are threaded through the same build as
        :meth:`add_job`, so the winning registrar's job is calendar-correct on
        its first and only save.
        """
        # Off-loop, same reason as add_job_async: this is the method `bridges`
        # awaits on the gateway loop for every app cron at enable and at start.
        # The vet derives the bundle root itself, from this SDK's own app name,
        # and memoises it -- so the walk happens once per instance rather than
        # once per job, and never on the loop.
        script = await asyncio.to_thread(self._vet_command_script, name, command, script)
        job = await self._cron.add_job_if_absent_async(
            lambda existing, n=name: existing.name == n,
            **self._add_job_kwargs(
                name, message,
                every_secs=every_secs, cron_expr=cron_expr, agent=agent,
                command=command, script=script, agent_sequence=agent_sequence,
                env=env, persistent_session=persistent_session, silent=silent,
                enabled=enabled, timezone=timezone, skip_dates=skip_dates,
                folder_id=folder_id,
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
            j
            for j in self._cron.list_jobs(include_disabled=True)
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
            self._cron.remove_job,
            job_id,
            _api="remove_job",
            actor=self._owner_prefix,
            source="cron_sdk",
        )
        self._audit_remove(job_id)
        return result

    async def remove_job_async(self, job_id: str) -> bool:
        """Event-loop-native :meth:`remove_job` (routes through
        ``CronService.remove_job_async``)."""
        self._assert_owned(job_id, "cron_remove_job")
        result = await self._cron.remove_job_async(
            job_id, actor=self._owner_prefix, source="cron_sdk"
        )
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

    # ── Enable / disable ──

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        """Pause/resume an owned job without replacing its ID or history.

        Synchronous, off-loop only. Ownership is rechecked inside the service's
        lock against the freshly loaded store, not an SDK cache snapshot.
        """
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        try:
            result = _run_sync_mutator(
                self._cron.enable_job,
                job_id,
                enabled,
                expected_owner=self._owner_prefix,
                _api="set_enabled",
            )
        except PermissionError:
            self._audit_enabled(job_id, "denied")
            raise
        self._audit_enabled(job_id, "ok")
        return result

    async def set_enabled_async(self, job_id: str, enabled: bool) -> bool:
        """Event-loop-native :meth:`set_enabled`; same ownership and audit rules."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        try:
            result = await self._cron.enable_job_async(
                job_id, enabled, expected_owner=self._owner_prefix
            )
        except PermissionError:
            self._audit_enabled(job_id, "denied")
            raise
        self._audit_enabled(job_id, "ok")
        return result

    def _audit_enabled(self, job_id: str, outcome: str) -> None:
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_set_enabled",
            outcome=outcome,
            resources=job_id,
        )

    # ── Update ──

    def update_job(self, job_id: str, **kwargs: Any) -> Any:
        """Update a job only if owned by this app. **Synchronous**; raises
        ``CronSyncOnLoopError`` on a running event loop (use
        :meth:`update_job_async` there). Raises ``PermissionError`` if the job
        belongs to a different app.
        Returns the updated CronJob or None.
        """
        self._assert_owned(job_id, "cron_update_job")
        if "enabled" in kwargs or "user_paused" in kwargs:
            raise ValueError("Use set_enabled or set_enabled_async to pause/resume a job")
        result = _run_sync_mutator(self._cron.update_job, job_id, _api="update_job", **kwargs)
        self._audit_update(job_id)
        return result

    async def update_job_async(self, job_id: str, **kwargs: Any) -> Any:
        """Event-loop-native :meth:`update_job` (routes through
        ``CronService.update_job_async``)."""
        self._assert_owned(job_id, "cron_update_job")
        if "enabled" in kwargs or "user_paused" in kwargs:
            raise ValueError("Use set_enabled or set_enabled_async to pause/resume a job")
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
