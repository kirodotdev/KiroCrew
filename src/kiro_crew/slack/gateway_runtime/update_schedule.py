"""When an available update applies, and the fence the restart into it passes.

The scheduled update checks (provider-driven and the legacy layout-aware one), the
effect an update would have, the in-flight-work census that holds an apply back,
the pause, drain and settle around an apply, and the claimed restart that re-execs
the gateway.

Orchestrator fields read: ``_MANDATORY_UPDATE_MAX_DEFER_SECS``,
``_UPDATE_BUSY_RETRY_SECS``, ``_UPDATE_DRAIN_TIMEOUT_SECS``, ``_running_script_ids``,
``_session_tasks``, ``dashboard_state``, ``sessions``, ``subagent_mgr``,
``task_runner``.
Orchestrator fields written: ``_mandatory_update_deferred_at``,
``_mandatory_update_deferred_key``, ``_pending_update_respawn``,
``_pending_update_version``, ``_update_apply_deferred``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import sys

    from kiro_crew.slack.gateway import (
        AUTO_EFFECT_INSTALL,
        AUTO_EFFECT_MANDATORY,
        AUTO_EFFECT_NOTIFY,
        AUTO_ROUTE_GIT,
        AUTO_ROUTE_PROVIDER,
        AUTO_ROUTE_WHEEL,
        CHECK_SUCCEEDED,
        CHECK_UNCHECKED,
        EXTERNALLY_MANAGED_STAMPS,
        MANAGED_BY_COMMAND,
        Any,
        AutoUpdateEffect,
        Callable,
        GatewayOrchestrator,
        _remediation_command,
        _update_info,
        auto_update_effect,
        distribution,
        flush_breadcrumb_writes,
        inbound_spool,
        logger,
        platform_compat,
        record_auto_update_effect,
        redact_credentials,
        redact_exfiltration_urls,
        redact_log_via_context,
        resolve_restart_launcher,
        run_pre_exec_stops,
        shutdown_event,
        subprocess_executor,
        update_ownership,
        wait_for_unwinding_turns,
    )


#: The update coordinator's shutdown grace when the managed-venv apply module was
#: never loaded (no apply can be running); otherwise its own STOP_GRACE_SECS.
_UPDATE_STOP_GRACE_SECS = 3.0


def _in_flight_work_counts(self: GatewayOrchestrator) -> tuple[int, int]:
    """Return ``(turns, background)`` that a restart would interrupt.

    Unknown provider state is classified as background so a mandatory
    cooperative turn drain never cancels work whose ownership is unclear.
    """
    turns = 0
    background = 0
    state = self.dashboard_state
    if state is not None:
        try:
            for provider in state.sessions.active_providers():
                checker = getattr(provider, "has_active_turn", None)
                if not callable(checker):
                    background += 1
                    continue
                try:
                    if checker():
                        turns += 1
                except Exception:
                    logger.warning("in-flight count: provider turn state failed", exc_info=True)
                    background += 1
        except Exception:
            logger.warning("in-flight count: active_providers() failed", exc_info=True)
            background += 1

        slots = getattr(state, "_slots", None)
        if isinstance(slots, dict):
            seen_slot_tasks: set[int] = set()
            for slot in list(slots.values()):
                task = getattr(slot, "task", None)
                if task is not None:
                    marker = id(task)
                    if marker in seen_slot_tasks:
                        continue
                    seen_slot_tasks.add(marker)
                    try:
                        if not task.done():
                            turns += 1
                    except Exception:
                        logger.warning("in-flight count: dashboard slot task failed", exc_info=True)
                        background += 1

        workflows = getattr(state, "workflow_service", None)
        if workflows is not None:
            try:
                background += sum(
                    1
                    for run in workflows.list_runs()
                    if isinstance(run, dict) and run.get("status") == "running"
                )
            except Exception:
                logger.warning("in-flight count: workflow listing failed", exc_info=True)
                background += 1

    for task in list(self._session_tasks.values()):
        if not task.done():
            turns += 1

    sessions = self.sessions
    if sessions is not None:
        try:
            background += max(0, int(sessions.inbound_callback_count))
        except Exception:
            logger.warning("in-flight count: inbound callback state failed", exc_info=True)
            background += 1
    try:
        background += max(0, int(inbound_spool.pending_refusal_write_count()))
    except Exception:
        logger.warning("in-flight count: refusal write state failed", exc_info=True)
        background += 1
    # A managed-venv apply still building (an approved in-app one, say): a
    # restart now would cancel it and exec an older promotion over it. When
    # it promotes, its own restart path owns the exec.
    wheel_apply = self._wheel_apply_module()
    if wheel_apply is not None:
        background += wheel_apply.applies_in_flight()

    seen_handler_tasks: set[int] = set()
    handler_owners: list[object] = [
        self,
        *getattr(self, "_channel_handles", {}).values(),
    ]
    handler_owners.extend(
        peer
        for owner in list(handler_owners)
        if (peer := getattr(owner, "_peer", None)) is not None
    )
    for owner in handler_owners:
        tasks = getattr(owner, "_handler_tasks", None)
        if not isinstance(tasks, (set, list, tuple)):
            continue
        for task in list(tasks):
            marker = id(task)
            if marker in seen_handler_tasks:
                continue
            seen_handler_tasks.add(marker)
            try:
                if not task.done():
                    background += 1
            except Exception:
                logger.warning("in-flight count: channel handler state failed", exc_info=True)
                background += 1

    subagents = self.subagent_mgr
    if subagents is not None:
        try:
            background += max(0, int(subagents.pending_work_count))
        except Exception:
            logger.warning("in-flight count: subagent state failed", exc_info=True)
            background += 1

    background += len(self._running_script_ids)

    runner = self.task_runner
    if runner is not None:
        try:
            if runner.running:
                background += 1
        except Exception:
            logger.warning("in-flight count: TaskRunner state failed", exc_info=True)
            background += 1
    return turns, background


def _count_in_flight_work(self: GatewayOrchestrator) -> int:
    """Count all gateway work that an automatic restart would interrupt."""
    turns, background = self._in_flight_work_counts()
    return turns + background


def _live_update_handler_tasks(self: GatewayOrchestrator) -> list[asyncio.Task[Any]]:
    """Return deduplicated channel handler tasks the restart must preserve."""
    current = asyncio.current_task()
    seen: set[int] = set()
    pending: list[asyncio.Task[Any]] = []
    state = self.dashboard_state
    slots = getattr(state, "_slots", None) if state is not None else None
    if isinstance(slots, dict):
        for slot in list(slots.values()):
            task = getattr(slot, "task", None)
            if task is None or task is current:
                continue
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            if not task.done():
                pending.append(task)
    background_tasks = getattr(state, "_background_tasks", None) if state is not None else None
    if isinstance(background_tasks, (set, list, tuple)):
        for task in list(background_tasks):
            if task is current or not callable(getattr(task, "get_name", None)):
                continue
            if not task.get_name().startswith("dashboard-stage:"):
                continue
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            if not task.done():
                pending.append(task)

    owners: list[object] = [self, *getattr(self, "_channel_handles", {}).values()]
    owners.extend(
        peer for owner in list(owners) if (peer := getattr(owner, "_peer", None)) is not None
    )
    for owner in owners:
        tasks = getattr(owner, "_handler_tasks", None)
        if not isinstance(tasks, (set, list, tuple)):
            continue
        for task in list(tasks):
            marker = id(task)
            if marker in seen or task is current:
                continue
            seen.add(marker)
            if not task.done():
                pending.append(task)
    return pending


async def _drain_update_callback_work(self: GatewayOrchestrator, *, timeout: float | None) -> bool:
    """Wait without cancellation until callbacks and refusal writes are durable."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + max(0.0, timeout)
    while True:
        handlers = self._live_update_handler_tasks()
        writes = list(inbound_spool.pending_refusal_writes())
        reservations = 0
        if self.sessions is not None:
            try:
                reservations = max(0, int(self.sessions.inbound_callback_count))
            except Exception:
                logger.warning("update restart: callback count unavailable", exc_info=True)
                return False
        if not handlers and not writes and reservations <= 0:
            # Let callbacks already queued on the loop either reserve or
            # observe the closed gate, then recheck. The caller commits the
            # synchronous restart fence immediately after this returns.
            await asyncio.sleep(0)
            handlers = self._live_update_handler_tasks()
            writes = list(inbound_spool.pending_refusal_writes())
            reservations = (
                max(0, int(self.sessions.inbound_callback_count))
                if self.sessions is not None
                else 0
            )
            if not handlers and not writes and reservations <= 0:
                return True
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return False
        awaitables = [*handlers, *writes]
        if awaitables:
            _done, still_pending = await asyncio.wait(awaitables, timeout=remaining)
            if still_pending and deadline is not None and loop.time() >= deadline:
                return False
        else:
            await asyncio.sleep(0.01 if remaining is None else min(0.01, remaining))


_UPDATE_BUSY_RETRY_SECS = 300.0


_MANDATORY_UPDATE_MAX_DEFER_SECS = 600.0


#: How long the pre-restart drain waits for callbacks and refusal writes to
#: become durable before giving up and deferring the restart.
#:
#: Named rather than inlined at the call site so a test that is about the
#: fetch/reset/venv SEQUENCE can shorten it. When nothing makes the drain
#: condition true, ``_drain_update_callback_work`` polls at 10ms to the
#: deadline, so an inlined 30.0 cost eleven such tests thirty seconds EACH --
#: ~330s of pure sleeping per full suite run, asserted on by none of them.
#: The value itself stays pinned by the test that IS about it
#: (``test_restart_fences_then_closes_and_final_drains`` asserts
#: ``drain:30.0``), so shortening it elsewhere cannot hide a change here.
_UPDATE_DRAIN_TIMEOUT_SECS = 30.0


async def _prepare_auto_update_apply(
    self: GatewayOrchestrator,
    *,
    mandatory: bool,
    mandatory_key: str = "",
) -> bool:
    """Pause admission and reach an idle boundary before automatic apply."""
    if shutdown_event.is_set():
        # A stop is already under way: an installer admitted now would be
        # stopped mid-write. The signal itself is the gate, because
        # ``close_all`` (which closes turn admission) runs only at the end
        # of the shutdown. ``pause_turn_admission_for_update`` checks it
        # again under its lock, for a stop that lands while it waits.
        logger.info("Auto-update not started: the gateway is shutting down")
        return False
    if not mandatory:
        self._mandatory_update_deferred_at = None
        self._mandatory_update_deferred_key = None
    sessions = self.sessions
    if sessions is None:
        return True
    try:
        paused = await sessions.pause_turn_admission_for_update()
    except Exception:
        logger.exception("Auto-update could not pause turn admission")
        self._update_apply_deferred = True
        return False
    if not paused:
        self._update_apply_deferred = True
        return False

    turn_busy, background_busy = self._in_flight_work_counts()
    busy = turn_busy + background_busy
    if busy <= 0:
        self._mandatory_update_deferred_at = None
        self._mandatory_update_deferred_key = None
        return True

    loop = asyncio.get_running_loop()
    now = loop.time()
    if mandatory:
        target_key = mandatory_key or "mandatory"
        if self._mandatory_update_deferred_key != target_key:
            self._mandatory_update_deferred_key = target_key
            self._mandatory_update_deferred_at = now
        elif self._mandatory_update_deferred_at is None:
            self._mandatory_update_deferred_at = now
    mandatory_due = bool(
        mandatory
        and self._mandatory_update_deferred_at is not None
        and now - self._mandatory_update_deferred_at >= self._MANDATORY_UPDATE_MAX_DEFER_SECS
    )
    if mandatory_due:
        logger.warning(
            "Mandatory update remains deferred after its grace period: "
            "%d active turn(s), %d background workload(s)",
            turn_busy,
            background_busy,
        )

    if await sessions.resume_turn_admission_after_update():
        self._schedule_inbound_replay()
    self._update_apply_deferred = True
    logger.info("Auto-update deferred: %d in-flight turn(s)", busy)
    if self.dashboard_state:
        self.dashboard_state.push_refresh("update_available")
    return False


async def _finish_auto_update_apply(self: GatewayOrchestrator) -> None:
    """Reopen admission when apply returns instead of replacing the process.

    The spool replay runs only when admission actually reopened: during a
    shutdown the pause is kept, and a replay then would tell senders to
    resend while the gateway is stopping.
    """
    sessions = self.sessions
    if sessions is None:
        return
    try:
        reopened = await sessions.resume_turn_admission_after_update()
    except Exception:
        logger.exception("Auto-update could not resume turn admission")
    else:
        if reopened:
            self._schedule_inbound_replay()


def _wheel_apply_module() -> Any:
    """The loaded managed-venv apply module, or ``None``. Never imports.

    Looked up in ``sys.modules``: no apply can be in flight in a process that
    never loaded it, and the stop paths must not import from a tree an update
    may have changed.
    """
    return sys.modules.get("kiro_crew.platform.wheel_apply")


def _dashboard_policy_applies() -> list["asyncio.Task[Any]"]:
    """Policy applies the dashboard started that are still running. Never imports.

    Same ``sys.modules`` lookup as :meth:`_wheel_apply_module`: an apply can be
    in flight only in a process whose dashboard update handlers are loaded, so
    a module that is not resident has nothing to stop.
    """
    handlers = sys.modules.get("kiro_crew.dashboard.handlers.updates")
    if handlers is None:
        return []
    return list(handlers.policy_applies_in_flight())


async def _settle_update_work(
    self: GatewayOrchestrator, update_task: "asyncio.Task[None] | None"
) -> None:
    """Wait, bounded, for the update coordinator and the applies it cancelled.

    The grace is the apply module's own (``STOP_GRACE_SECS``); with the module
    not loaded no apply can be running, so only the coordinator is waited on.
    """
    module = self._wheel_apply_module()
    grace = float(getattr(module, "STOP_GRACE_SECS", _UPDATE_STOP_GRACE_SECS))
    waits = []
    if module is not None:
        waits.append(module.stop_wheel_applies())
    if update_task is not None and not update_task.done():
        waits.append(asyncio.wait({update_task}, timeout=grace))
    if waits:
        await asyncio.gather(*waits)


async def _run_update_checks(self: GatewayOrchestrator) -> None:
    """Run the full check-and-apply coordinator now and every interval."""
    from kiro_crew.dashboard.handlers import _UPDATE_CHECK_INTERVAL

    while True:
        self._update_apply_deferred = False
        try:
            # A queued restart the user put off must not stop the checks: a
            # policy floor raised since, or a newer release, is still found,
            # and its own apply replaces the held respawn.
            if (
                getattr(self, "_pending_update_respawn", None) is None
                or await self._retry_pending_update_restart()
            ):
                await self._check_for_updates()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed cycle must not permanently kill automatic updates.
            logger.exception("Automatic update coordinator failed")
        delay = (
            self._UPDATE_BUSY_RETRY_SECS if self._update_apply_deferred else _UPDATE_CHECK_INTERVAL
        )
        await asyncio.sleep(delay)


async def _retry_pending_update_restart(self: GatewayOrchestrator) -> bool:
    """Run, or retry, only the safe restart after an update already applied.

    Admission pauses here and nowhere earlier: an apply that builds beside
    the live install leaves the gateway serving until this point. A pending
    restart that serves a policy floor keeps its mandatory grace across
    retries (``_pending_update_mandatory`` and its key).

    A voluntary managed-venv restart the user put off ("Later" in the update
    prompt, read for ``_pending_update_version``) waits: the respawn stays queued and the
    badge stays lit, so the update starts when the user installs it or the
    deferral lapses. A policy floor's restart does not wait.

    Returns True only for that hold, so the caller still runs the update
    check; every other outcome (restarted, busy, nothing queued) is False.
    """
    respawn = getattr(self, "_pending_update_respawn", None)
    if respawn is None:
        return False
    mandatory = getattr(self, "_pending_update_mandatory", False)
    version = getattr(self, "_pending_update_version", "")
    if not mandatory and version:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.update_deferral import user_deferred_update

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if user_deferred_update(cfg, version):
            logger.info("Update %s is ready but was put off by the user — not restarting", version)
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return True
    if not await self._prepare_auto_update_apply(
        mandatory=mandatory,
        mandatory_key=getattr(self, "_pending_update_mandatory_key", ""),
    ):
        return False
    try:
        await self._restart_after_update(respawn)
    finally:
        await self._finish_auto_update_apply()
    return False


async def _check_for_updates(self: GatewayOrchestrator) -> None:
    """Blocking update check — auto-applies if enabled, otherwise notifies.

    Delegates to the resolved :class:`~kiro_crew.platform.update_provider.CommandProvider`
    when security_policy.json's ``updates`` block defines the update commands
    (the enterprise escape hatch). When no policy-defined provider is active,
    ``resolve_provider`` returns ``None`` and we fall through to the existing
    layout-aware logic (backward compatible: no policy = existing behavior).
    """
    provider = None
    try:
        from kiro_crew.platform.update_provider import resolve_provider

        provider = await asyncio.get_running_loop().run_in_executor(None, resolve_provider)
    except Exception:
        # ONLY resolution is tolerated here. If reading the policy fails we
        # cannot know an operator selected a provider, so the built-in
        # behaviour is the honest default.
        logger.debug("Provider resolution failed, using legacy path", exc_info=True)

    # A policy-defined provider (enterprise escape hatch) OWNS the update from
    # here on. Its failures must NOT fall through to the legacy updater: doing
    # so would run the built-in git/CDN update on a host whose administrator
    # selected a different package manager, which is the bypass this seam
    # exists to prevent. `_check_for_updates_via_provider` reports its own
    # failures and leaves the install alone.
    if provider is not None:
        try:
            await self._check_for_updates_via_provider(provider)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Contained, not swallowed and not retried elsewhere: the update
            # check runs on the gateway's boot path, so an exception must not
            # escape into it, and it must not reach the legacy updater either
            # (that would run the built-in update the operator excluded).
            logger.exception("Policy-defined update provider failed")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed",
                    "Update provider failed — see logs; run manually: kirocrew update",
                )
        return

    # Legacy path: existing behavior for builtin/git auto-detected installs.
    await self._check_for_updates_legacy()


async def _auto_update_effect(
    self: GatewayOrchestrator, provider: object | None
) -> AutoUpdateEffect:
    """What an available update leads to here: the one answer every apply path acts on.

    Recorded for the status surface too, so the dashboard's auto-update
    switch reads the same derivation this loop branches on. ``notify`` never
    reaches ``_prepare_auto_update_apply``: admission is not paused for an
    update this install will not apply.
    """
    effect = await asyncio.to_thread(auto_update_effect, provider=provider)
    # git_probes defaults True, so the derivation always answers here.
    assert effect is not None
    record_auto_update_effect(effect)
    return effect


def _publish_provider_update_state(self: GatewayOrchestrator, result: object) -> None:
    """Mirror a provider's verdict into the dashboard's authoritative status.

    The SSE snapshot renders the update badge from
    ``dashboard/handlers/updates_verdict.py::_update_info["update_available"]``, which
    only the LEGACY check writes. A provider carries its own
    :class:`UpdateCheckResult`, so notifying without this leaves the badge
    reading a stale (usually null) value and the operator never sees that a
    policy-defined update is waiting.

    Written in the capability contract's vocabulary, and ``check_status`` is
    stamped alongside the verdict: under that contract "up to date" means
    ``check_status == "succeeded" and update_available is False``, so writing
    the verdict without the status would leave a provider's real answer
    indistinguishable from a check that never ran.
    """
    from kiro_crew.dashboard.handlers.updates import _update_info

    _update_info["update_available"] = bool(getattr(result, "available", False))
    remote = str(getattr(result, "remote_version", "") or "")
    if remote:
        _update_info["latest_version"] = remote
    _update_info["check_status"] = CHECK_SUCCEEDED


async def _check_for_updates_via_provider(self: GatewayOrchestrator, provider: object) -> None:
    """Provider-delegated update check and apply."""
    from kiro_crew import __version__ as _running_version
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.update_governance import (
        min_version,
        unattended_apply_allowed,
        update_required,
    )
    from kiro_crew.platform.update_provider import UpdateProvider

    # Loaded BEFORE provider.apply(): the legacy-nested-venv migration the
    # resolver exists for deletes the venv this process imports from, so a
    # deferred import after a successful apply would raise
    # ModuleNotFoundError and leave a closed-session gateway un-restarted
    # (found in review). The module stays off the boot path either way —
    # this method runs from the update timer, not from gateway start.
    from kiro_crew.platform.wheel_apply import snapshot_memory_before_update, snapshot_refusal
    from kiro_crew.platform.wheel_engine import respawn_executable

    assert isinstance(provider, UpdateProvider)

    async def _snapshot_memory_or_skip(result: Any) -> bool:
        """A fresh copy of every memory store before ``provider.apply()``; False skips it.

        The same copy-or-refuse step `kirocrew update` and the git auto-update take.
        Nobody is watching this path, so a copy that did not land skips the update and
        says so; it never applies blind and never exits the gateway. Retried on the
        next check.
        """
        copied, failure = await asyncio.to_thread(snapshot_memory_before_update)
        if not failure:
            logger.info("Auto-update: pre-update memory snapshot, %d store(s) copied", copied)
            return True
        failure, _ = redact_exfiltration_urls(failure)
        failure, _ = redact_credentials(failure)
        failure = failure[:500]
        logger.warning("Auto-update skipped: pre-update memory snapshot failed: %s", failure)
        message = snapshot_refusal(failure)
        self._notice_wheel_update_once(
            f"snapshot_failed:provider:{result.remote_version or ''}",
            "Kiro Crew update skipped",
            message,
        )
        self._publish_provider_update_state(result)
        if self.dashboard_state:
            self.dashboard_state.push_update_progress("failed", message)
            self.dashboard_state.push_refresh("update_available")
        return False

    def _unattended_installs_turned_off(result: Any) -> bool:
        """True, and reported, when policy turned unattended installs off mid-cycle.

        The update stays offered for a person's Update; nothing was applied.
        """
        if unattended_apply_allowed():
            return False
        logger.info(
            "Auto-update not applied: the security policy turned off unattended "
            "installs while this update waited"
        )
        self._publish_provider_update_state(result)
        if self.dashboard_state:
            self.dashboard_state.push_refresh("update_available")
        return True

    async def _restart_unless_apply_changed_nothing(result: Any) -> None:
        """Restart into the applied update, or report an apply that changed nothing.

        The check runs again first. When it still offers the version the apply
        was for, the apply left the install as it was (for example a package
        manager pin the apply honours and the check ignores). Restarting then
        would boot into the same check and the same apply on every cycle,
        closing every session each time, so the outcome is reported and the
        gateway keeps running. A re-check that errors, finds nothing, or offers
        another version is not that loop, and the restart goes ahead.
        """
        assert isinstance(provider, UpdateProvider)
        after = await provider.check()
        offered = str(result.remote_version or "")
        if after.available and not after.error and str(after.remote_version or "") == offered:
            message = (
                f"The update command finished, but the check still offers {offered}: "
                "the install did not change, so Kiro Crew is not restarting. "
                "Check the policy's apply_command and check_command."
            )
            logger.warning(
                "Auto-update: apply_command exited 0 but the check still offers %s — "
                "the install did not change; not restarting",
                offered,
            )
            self._notice_wheel_update_once(
                f"apply_unchanged:provider:{offered}",
                "Kiro Crew update changed nothing",
                message,
            )
            self._publish_provider_update_state(after)
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("failed", message)
                self.dashboard_state.push_refresh("update_available")
            return
        await self._restart_after_update(respawn_executable)

    # Say who owns updates on every path, "already latest" included, so the
    # dashboard need not run its own check to learn it.
    _update_info["managed_by"] = MANAGED_BY_COMMAND
    effect = await self._auto_update_effect(provider)
    # ``can_apply`` read off that one derivation rather than asked of the
    # provider again: it is the PROVIDER's answer, not the install shape's
    # (a reader told "cannot apply" because the shape is a wheel would be
    # wrong on every policy-managed host), and the apply paths below never
    # reach ``_publish_provider_update_state`` where a verdict is published.
    _update_info["can_apply"] = effect.route == AUTO_ROUTE_PROVIDER
    result = await provider.check()

    # The mandatory floor is an enterprise ceiling and is evaluated FIRST,
    # before any check-error early return: a host below min_version must
    # still be updated even when the provider's check could not complete
    # (a timed-out or misconfigured command must not strand the host below
    # the policy floor).
    mandatory_required = update_required(_running_version)
    if not mandatory_required:
        self._mandatory_update_deferred_at = None
        self._mandatory_update_deferred_key = None
    if mandatory_required:
        # Guard against an infinite update→restart loop: only apply when the
        # check found a NEWER build available. If the floor is pinned above
        # the highest installable build (a policy typo, or a floor set ahead
        # of the current release), applying would reinstall the same version,
        # restart, and re-enter this branch forever. When no newer build is
        # available we notify and stop — the git path's no-new-commits early
        # return is the equivalent guard.
        if not result.available:
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s, "
                "but no newer build is available to apply — notifying, not looping",
                _running_version,
                min_version(),
            )
            self._publish_provider_update_state(result)
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return
        if effect.effect != AUTO_EFFECT_MANDATORY:
            # The floor mandates it but nothing here can apply it: tell the
            # operator instead of pausing admission for an apply that fails.
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s, "
                "but %s — notifying",
                _running_version,
                min_version(),
                effect.reason,
            )
            self._publish_provider_update_state(result)
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return
        if not await self._prepare_auto_update_apply(
            mandatory=True,
            mandatory_key=f"provider:{min_version()}:{result.remote_version or ''}",
        ):
            self._publish_provider_update_state(result)
            return
        try:
            if not await _snapshot_memory_or_skip(result):
                return
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s — "
                "applying mandatory update via provider (overrides auto_update)",
                _running_version,
                min_version(),
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("pulling", "Applying mandatory update…")
            self._pending_update_version = ""
            success = await provider.apply()
            if success:
                await _restart_unless_apply_changed_nothing(result)
            elif self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed", "Update apply failed — run manually: kirocrew update"
                )
        finally:
            await self._finish_auto_update_apply()
        return

    # Below the mandatory floor: a check error is a non-answer, not a
    # verdict — report it and stop rather than treating it as "up to date".
    if result.error:
        logger.info("Update check did not complete (%s)", result.error)
        return

    if result.available:
        from kiro_crew.update_deferral import user_deferred_update

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        deferred = user_deferred_update(cfg, str(result.remote_version or ""))
        if cfg.auto_update and effect.effect == AUTO_EFFECT_INSTALL and not deferred:
            if not await self._prepare_auto_update_apply(mandatory=False):
                self._publish_provider_update_state(result)
                return
            try:
                # The effect was derived before the check and the drain, and a
                # policy installed since then decides. Read before the memory
                # copy so a refused install spends none, and after it, with no
                # wait between that read and the apply.
                if _unattended_installs_turned_off(result):
                    return
                if not await _snapshot_memory_or_skip(result):
                    return
                if _unattended_installs_turned_off(result):
                    return
                logger.info("Auto-update enabled — applying update via provider")
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress("pulling", "Downloading update…")
                # The provider replaced the install on disk, so its queued
                # restart is not held: only the managed-venv route, whose
                # running tree is untouched, waits for "Later".
                self._pending_update_version = ""
                success = await provider.apply()
                if success:
                    await _restart_unless_apply_changed_nothing(result)
                elif self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed", "Update apply failed — run manually: kirocrew update"
                    )
            finally:
                await self._finish_auto_update_apply()
        else:
            if deferred:
                logger.info(
                    "Update %s was put off by the user — not installing it unattended",
                    result.remote_version,
                )
            elif cfg.auto_update:
                logger.info("Auto-update is on, but %s — notifying instead", effect.reason)
            self._publish_provider_update_state(result)
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
    else:
        print("👻 Already on latest version")


async def _restart_after_update_claimed(
    self: GatewayOrchestrator, launcher: str | None, exe: str | None
) -> None:
    """The restart's drain-to-exec half, run with the process restart claimed."""
    if self.dashboard_state:
        self.dashboard_state.push_update_progress("restarting", "Preparing safe restart…")
        from kiro_crew.dashboard.chat import save_all_slots_to_history

        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    save_all_slots_to_history,
                    self.dashboard_state,
                ),
                timeout=5.0,
            )
        except Exception:
            logger.debug(
                "Dashboard slot save before update restart failed",
                exc_info=True,
            )
    # Same reason as the dashboard restart path: os.execv does not drain the
    # safety-override writer. No await is permitted between final drain and exec.
    try:
        await asyncio.to_thread(flush_breadcrumb_writes, 2.0)
    except Exception:
        logger.debug("Breadcrumb flush before update restart failed", exc_info=True)

    if not await self._drain_update_callback_work(timeout=self._UPDATE_DRAIN_TIMEOUT_SECS):
        self._update_apply_deferred = True
        update_ownership.note_restart_deferred()
        logger.warning("Update applied but restart deferred: callback/refusal work did not drain")
        if self.dashboard_state:
            self.dashboard_state.push_update_progress(
                "restarting", "Update ready — restart deferred until messages are safe"
            )
        return

    # Past the last deferral: the restart commits here, and a deferral it
    # retried is over (a lost fence below means a real shutdown anyway).
    update_ownership.restart_committed()
    sessions = self.sessions
    if sessions is not None:
        # Yield-free with the successful drain above: a callback is now
        # either already counted, or it observes this fence and writes its
        # refusal synchronously before returning. That closes the final
        # snapshot-to-exec window without holding ordinary retries closed.
        if not sessions.fence_update_restart():
            self._update_apply_deferred = True
            logger.warning("Update restart fence lost to real shutdown; deferring restart")
            return
        await sessions.close_all()
        # close_all() cut every dashboard turn that was mid-reply, and each one
        # saves the text its user watched stream as it unwinds, after the save
        # above. The exec below would beat that save.
        await wait_for_unwinding_turns(self.dashboard_state)

    # The broker this gateway spawned dies with it, the same as ``_shutdown``
    # does on a clean exit; an adopted daemon belongs to its own owner and is
    # left alone, as ``GatewayManager.shutdown`` already does. The exec below
    # skips that shutdown, and the successor can only replace a survivor whose
    # owner pid is its own (an exec that kept the pid) or gone. Through a
    # launcher that runs the new gateway as a child, this pid lives on as its
    # supervisor: the daemon's owner-liveness check keeps passing, and the
    # successor refuses a broker "owned by another live gateway" for its whole
    # lifetime. Sessions are closed, so nothing is mid-call. The stop never
    # raises and is bounded: the daemon's own drain budget on SIGTERM, then a
    # SIGKILL and a reap of its pooled backends if the drain does not finish.
    await self._stop_mcp_broker()
    # Supervised children go the same way, for the same reason: the exec
    # skips the cleanup hooks that stop them, and a child whose parent pid
    # survives the exec is never reclaimed by the new image. Each owner
    # registers its stop with the pre-exec seam. Bounded and never raises.
    await run_pre_exec_stops()

    # Any callback that ran while close_all awaited used the synchronous
    # fenced refusal path. Drain pre-fence workers and registered handlers
    # one final time; never cancel or force through them.
    await self._drain_update_callback_work(timeout=None)
    if shutdown_event.is_set():
        # The operator asked this process to stop while the restart was
        # being prepared. Exec'ing would swallow that stop; the applied
        # update takes effect at the next start instead.
        logger.info("Update applied; restart skipped because the gateway is shutting down")
        return
    logger.info("Update callback drain complete, restarting gateway")
    self._pending_update_respawn = None
    self._pending_update_version = ""
    # The exec is past the point of no return: the guard above removed the
    # reachable failures, but only the kernel can refuse the image itself
    # (wrong architecture, truncated, replaced since the check). Returning
    # from here is what strands the gateway, so hand that outcome to the
    # exec seam's own fatal partner instead of unwinding into the update
    # coordinator, which logs and loops with every session already closed.
    try:
        if launcher is not None:
            platform_compat.reexec_launcher(launcher, sys.argv[1:])
        else:
            platform_compat.reexec_python_module("kiro_crew", sys.argv[1:], executable=exe)
    except OSError:
        await platform_compat.exit_after_failed_restart_exec(launcher or exe)


async def _check_for_updates_legacy(self: GatewayOrchestrator) -> None:
    """Legacy update check — the existing layout-aware logic."""
    try:
        from kiro_crew import __version__ as _running_version
        from kiro_crew.dashboard.handlers import _do_update_check, _update_info

        # Before the check: it reads no check result, and the status frame
        # should not wait out a slow fetch to learn it.
        effect = await self._auto_update_effect(None)
        await _do_update_check()
        # Snapshot: the branches below read several keys with awaits
        # between them, and a dashboard-triggered check running
        # concurrently replaces the cache wholesale.
        info = dict(_update_info)
        if info.get("managed_by") == MANAGED_BY_COMMAND and effect.route is not None:
            # The check resolved a policy provider that was not configured
            # when the effect was derived, which a live policy refresh can
            # do between the two. A provider OWNS the update, so no
            # built-in route may apply it: the next cycle resolves the
            # provider up front and routes there.
            effect = AutoUpdateEffect(
                AUTO_EFFECT_NOTIFY,
                None,
                "a policy update command now owns this install",
            )
            record_auto_update_effect(effect)
        from kiro_crew.platform.update_governance import min_version, update_required

        mandatory_target_key = (
            f"legacy:{min_version()}:{info.get('channel') or ''}:"
            f"{info.get('latest_version') or ''}:{info.get('managed_by') or ''}"
        )

        # A policy-pinned minimum version makes the update MANDATORY: it
        # overrides the user's auto_update=False, because user config sits
        # under the enterprise ceiling and an operator opting out must not
        # hold a fleet on a build the policy forbids.
        #
        # Checked BEFORE the `available` branch, and deliberately independent
        # of it: the mandate is about whether THIS host satisfies the floor,
        # not about whether a newer build was advertised. `_auto_apply_update`
        # still applies the source pin and its own no-new-commits early
        # return, so this cannot bypass the ceiling or loop.
        mandatory_required = update_required(_running_version)
        if not mandatory_required:
            self._mandatory_update_deferred_at = None
            self._mandatory_update_deferred_key = None
        if mandatory_required:
            # A mandatory floor is handled by the route `auto_update_effect`
            # chose, because "apply" means different things per install shape:
            #   * git checkout -> git fetch + reset applies.
            #   * managed venv -> the shadow engine can apply it beside the
            #     live venv, so a floor does drive it; a floor above the newest
            #     build notifies instead of reinstalling the same bytes forever.
            #   * externally managed (dmg/appimage/deb/rpm/nsis/docker: no
            #     `can_apply` and no command) -> its own updater owns this; the
            #     backend must not drive a git reset on a non-git tree nor show
            #     an inapplicable CLI-update badge.
            if effect.effect == AUTO_EFFECT_MANDATORY and effect.route == AUTO_ROUTE_GIT:
                # Apply only when a newer build is available AND the checkout
                # can take it cleanly — not on commit distance alone, and not
                # over local divergence. `_auto_apply_update` resets hard onto
                # the upstream tip, so the gate is two signals the successful
                # check already computed:
                #   * `version_newer` — the target's `__version__` outranks the
                #     one this process imported. The same signal the voluntary
                #     git branch below reads. Without it a primary-branch
                #     checkout below the floor is reset to every intermediate
                #     commit on every cycle and at every boot until `__version__`
                #     meets the floor, which never advances the imported version
                #     one commit at a time.
                #   * `update_available` — `can_fast_forward or restart_pending`
                #     (behind-only, or already pulled and awaiting a restart). A
                #     DIVERGED checkout (ahead and behind) reads `version_newer`
                #     true but `update_available` false, and resetting it would
                #     discard the local commits. The destructive step has its
                #     own ahead-count refusal, but gating here keeps a diverged
                #     host on the clean notify path instead of a fetch-then-bail.
                # A floor above the newest build, or a diverged checkout, then
                # notifies rather than churning — the wheel branch's
                # no-newer-build guard is the same stop.
                if not (info.get("version_newer") and info.get("update_available")):
                    # Keep a FAILED/unparseable check distinct from a healthy
                    # "nothing newer / diverged" verdict: a non-answer must not
                    # read as a compliance decision in the log. Either way, do
                    # NOT write `update_available` into the shared check cache —
                    # the wheel branch this mirrors only refreshes the badge, and
                    # a write here would overwrite the real verdict (a failed or
                    # diverged check) with a fabricated "update available".
                    if info.get("check_status") != CHECK_SUCCEEDED:
                        logger.warning(
                            "Version compliance: running %s is below the policy minimum "
                            "%s, but the update check did not succeed (status %s) — "
                            "notifying, not resetting",
                            _running_version,
                            min_version(),
                            info.get("check_status") or CHECK_UNCHECKED,
                        )
                    else:
                        logger.warning(
                            "Version compliance: running %s is below the policy minimum "
                            "%s, but no newer build is cleanly applicable — notifying, "
                            "not resetting to every upstream commit",
                            _running_version,
                            min_version(),
                        )
                    if self.dashboard_state:
                        self.dashboard_state.push_refresh("update_available")
                    return
                if not await self._prepare_auto_update_apply(
                    mandatory=True,
                    mandatory_key=mandatory_target_key,
                ):
                    return
                try:
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s — "
                        "applying a mandatory update (overrides auto_update)",
                        _running_version,
                        min_version(),
                    )
                    self._pending_update_version = ""
                    await self._auto_apply_update(mandatory=True)
                finally:
                    await self._finish_auto_update_apply()
                return
            # A managed-venv install cannot apply in-process, but the shadow
            # engine can, and a policy floor outranks auto_update. Runtime
            # ownership is authoritative: older managed wheels have no build
            # stamp, while a foreign source or wheel must never be rewritten.
            if (
                effect.effect == AUTO_EFFECT_MANDATORY
                and effect.route == AUTO_ROUTE_WHEEL
                # The installer command is composed by the CHECK, so it is
                # not part of the static derivation: without it the apply
                # would pause admission and then warn-skip.
                and _remediation_command(info)
            ):
                # Only apply when a NEWER build is available; otherwise the
                # apply reinstalls the same below-floor version and the
                # execv-restart re-enters this branch forever (the git path's
                # no-new-commits guard is the equivalent). A floor pinned
                # above the latest build must notify, not loop.
                if not info.get("update_available"):
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but no newer build is available — notifying, not looping",
                        _running_version,
                        min_version(),
                    )
                    if self.dashboard_state:
                        self.dashboard_state.push_refresh("update_available")
                    return
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s — "
                    "applying mandatory update beside the managed venv "
                    "(overrides auto_update)",
                    _running_version,
                    min_version(),
                )
                # Admission stays open while the new tree builds; only the
                # restart into it pauses, under the mandatory grace. A
                # promotion that would detach the sandbox's AppArmor profile
                # waits for the operator instead, retried on the short
                # cadence with a notice naming the two commands.
                if shutdown_event.is_set():
                    logger.info("Auto-update not started: the gateway is shutting down")
                    return
                await self._auto_apply_wheel_update(
                    str(info.get("channel") or ""),
                    str(info.get("latest_version") or ""),
                    mandatory=True,
                    mandatory_key=mandatory_target_key,
                )
                return
            if effect.blocked:
                # The source pin refuses every update path, `kirocrew update`
                # included, so there is nothing to point the operator at.
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s, "
                    "but %s — not applying",
                    _running_version,
                    min_version(),
                    effect.reason,
                )
                return
            # Everything below cannot apply here, so the operator has to act.
            # Two of the three cases light the badge; the third deliberately
            # does not, because a dmg/appimage/deb/rpm/nsis/docker install cannot
            # act on a CLI-update badge and its own updater owns the upgrade.
            #
            # Where the badge IS lit, `check_status` and `error_code` are left
            # exactly as the check left them. Stamping them "succeeded" would
            # erase the only evidence that the check path itself is broken,
            # which is worse than a payload carrying two independent facts: an
            # update is mandated (a LOCAL determination against the policy pin,
            # which does not need the feed) and the feed check did not complete.
            if _remediation_command(info):
                # A plain `pip install` into an environment the user manages
                # (not pipx, not the installer's managed venv) must not be
                # told to "run `kirocrew update`": that command re-runs the
                # installer, which adds a second copy and leaves this process
                # on the old version. Give it the in-place upgrade hint
                # instead — the same words `kirocrew update` itself now
                # prints for this shape. pipx and the managed venv keep the
                # `kirocrew update` pointer, which upgrades them correctly.
                from kiro_crew.platform.update_layout import (
                    PipUpgradeHint,
                    non_managed_pip_update_hint,
                )
                from kiro_crew.platform.wheel_engine import (
                    running_from_managed_venv,
                    running_from_pipx,
                )

                # The shape probes stat the filesystem (venv layout
                # resolution, the pipx marker file), and building the
                # non-managed hint reads the channel file too — so both run
                # off the event loop in one dispatch, like the other
                # update-path probes on it. The channel is already resolved
                # on `info`, so pass it through rather than re-reading it.
                _info_channel = info.get("channel")
                _channel = str(_info_channel) if _info_channel else None

                def _resolve_hint() -> tuple[bool, tuple[PipUpgradeHint, str]]:
                    self_updating = running_from_managed_venv() or running_from_pipx()
                    hint: tuple[PipUpgradeHint, str] = (
                        (PipUpgradeHint(command=None, note=""), "")
                        if self_updating
                        else non_managed_pip_update_hint(_channel)
                    )
                    return self_updating, hint

                is_self_updating, (upgrade, restart) = await asyncio.to_thread(_resolve_hint)
                if is_self_updating:
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but this install (%s) cannot apply it unattended (%s) — "
                        "run `kirocrew update`",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                        effect.reason,
                    )
                elif upgrade.command is not None:
                    # The upgrade command embeds the CDN base, and a
                    # userinfo-bearing KIROCREW_CDN_BASE (user:pass@host)
                    # passes _SAFE_CDN_BASE_RE — so this durable,
                    # dashboard-served log line is redacted before it is
                    # logged. The gateway composes a companion context, so it
                    # uses the context-aware gate-side spelling
                    # (redact_log_via_context) rather than the OSS baseline:
                    # a host with a companion loaded gets that companion's
                    # extra credential regexes. The command the user is told
                    # to run is unaffected.
                    logged_upgrade = redact_log_via_context(upgrade.command)
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but this install (%s) cannot apply it unattended (%s) — upgrade "
                        "this environment in place with `%s`, then `%s`",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                        effect.reason,
                        logged_upgrade,
                        restart,
                    )
                else:
                    # The signed wheel could not be fetched/verified, so
                    # there is no safe upgrade command to log — a
                    # name-resolving --extra-index-url form would reopen the
                    # dependency-confusion vector. Log the failure report
                    # instead. The note carries the channel artifact URL
                    # (CDN base embedded), redacted through the context-aware
                    # gate-side spelling like the sibling branch above.
                    logged_note = redact_log_via_context(upgrade.note)
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but this install (%s) cannot apply it unattended (%s) — the "
                        "signed upgrade %s Once upgraded, run `%s`",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                        effect.reason,
                        logged_note,
                        restart,
                    )
                _badge = True
            elif info.get("check_status") in ("unchecked", "checking"):
                # The check no-ops while another one is in flight, so the cache
                # can hold no verdict here — and with no verdict there is no
                # `managed_by` either. The baked distribution stamp answers the
                # one question the badge needs and costs no I/O, so an
                # externally managed install is not handed a CLI-update badge it
                # cannot act on.
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s, but no "
                    "check has reached a verdict yet — the next cycle decides which surface "
                    "owns the upgrade",
                    _running_version,
                    min_version(),
                )
                _badge = distribution() not in EXTERNALLY_MANAGED_STAMPS
            else:
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s, but "
                    "this install (%s) is updated by its own updater — not applying from "
                    "the backend",
                    _running_version,
                    min_version(),
                    info.get("managed_by") or "unknown",
                )
                _badge = False
            if _badge:
                _update_info["update_available"] = True
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
            return

        if info.get("update_available"):
            logger.info("Updates available from remote")
            from kiro_crew.config import KiroCrewConfig

            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            # `_auto_apply_update` replaces code with git fetch + reset, so it
            # serves only the git route `auto_update_effect` chose. A managed
            # venv is replaced by the shadow engine, which the branch below
            # drives instead.
            #
            # `version_newer` is the other half, and it is not redundant:
            # `update_available` is true on commit distance alone, which for a
            # source checkout means any upstream commit — acting on that would
            # `git reset --hard` a developer's tree within 12 hours of one,
            # where before it only happened at a release. Commit distance
            # without a version bump lights the badge below instead, and the
            # dashboard's own apply path (`git pull`, dirty tree refused) is
            # the non-destructive way in.
            from kiro_crew.update_deferral import user_deferred_update

            # "Later" in an update prompt is the user's answer for that version:
            # it stays offered, and is never installed and restarted unattended.
            deferred = user_deferred_update(cfg, str(info.get("latest_version") or ""))
            installs = cfg.auto_update and effect.effect == AUTO_EFFECT_INSTALL and not deferred
            if installs and effect.route == AUTO_ROUTE_GIT and info.get("version_newer"):
                if not await self._prepare_auto_update_apply(mandatory=False):
                    return
                try:
                    logger.info("Auto-update enabled — applying update")
                    # The git route reloads `kiro_crew` after rebuilding, so a
                    # held restart would leave the floor check reading the new
                    # `__version__` for old code; its restart is not held.
                    self._pending_update_version = ""
                    await self._auto_apply_update()
                finally:
                    await self._finish_auto_update_apply()
            elif installs and effect.route == AUTO_ROUTE_WHEEL and _remediation_command(info):
                # Only the managed venv self-updates: the engine builds the new
                # version beside the tree serving this process and flips the
                # stable link, with admission open. Other source and wheel
                # installs notify.
                if shutdown_event.is_set():
                    # A stop is already under way: an apply started now would
                    # be cancelled moments later, before its outcome is audited.
                    logger.info("Auto-update not started: the gateway is shutting down")
                    return
                logger.info("Auto-update enabled for managed install — building beside it")
                await self._auto_apply_wheel_update(
                    str(info.get("channel") or ""), str(info.get("latest_version") or "")
                )
            else:
                if deferred:
                    logger.info(
                        "Update %s was put off by the user — not installing it unattended",
                        info.get("latest_version"),
                    )
                elif cfg.auto_update and not installs:
                    logger.warning(
                        "Auto-update is on, but this install (%s) cannot apply it "
                        "unattended (%s) — notifying instead",
                        info.get("managed_by") or "unknown",
                        effect.reason,
                    )
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
        elif info.get("error_code"):
            # A check that could not run is NOT "already on latest" — saying so
            # is the exact false reassurance the honesty pair in
            # `handlers/updates_check.py` exists to prevent.
            logger.info("Update check did not complete (%s)", info.get("error_code"))
        elif info.get("check_status") == CHECK_SUCCEEDED:
            print("👻 Already on latest version")
        else:
            # DEFERRED (a desktop bundle whose own updater owns this), or a
            # check that never ran. Neither carries an `error_code`, so keying
            # only on that would fall through to the reassurance above and
            # claim a verdict nothing produced.
            logger.info(
                "No update verdict to report (check_status=%s)",
                info.get("check_status") or CHECK_UNCHECKED,
            )
    except Exception:
        logger.debug("Update check failed", exc_info=True)


async def _restart_after_update_body(self: GatewayOrchestrator, respawn: Callable[[], str]) -> None:
    """Save state and restart only after callback/refusal work is durable.

    ``respawn`` is loaded before apply can replace the environment. If the
    pre-fence drain cannot finish, retain it and retry the restart in five
    minutes with admission reopened; never force through accepted work.

    The interpreter is established BEFORE any of that. When it is missing, this
    returns without saving, fencing or draining, because an exec that cannot
    succeed must not be reached after every session has been closed.

    Owns a missing bundle while it runs (``update_ownership``), so the
    stale-asset watchdog does not race its teardown. A deferral while
    callback work drains is owned for a while too, from the first deferral
    on (a retry does not extend it), so the watchdog does not force the
    restart it just put off.
    """
    logger.info("Update applied, preparing a callback-safe gateway restart")
    self._pending_update_respawn = respawn
    launcher = await asyncio.to_thread(resolve_restart_launcher)
    exe = await asyncio.to_thread(respawn) if launcher is None else None
    # Off-loop for the same reason as the two resolvers above: both predicates
    # are metadata syscalls against a pathname this process does not control,
    # and an interpreter on a stalled network mount would freeze every gateway
    # task -- including the heartbeat -- rather than one restart.
    usable = launcher is not None
    if not usable and exe:
        usable = await asyncio.to_thread(platform_compat.execv_target_available, exe)
    if not usable:
        # No usable interpreter: the apply pruned the tree this process was
        # running from. RETURN BEFORE saving, fencing or draining. Reaching
        # the exec with no interpreter closes every session first and then
        # raises ENOENT. Admission itself does come back --
        # ``_finish_auto_update_apply`` resumes it -- but the sessions
        # ``close_all()`` tore down do not, and ``_pending_update_respawn``
        # is cleared just before the exec, so nothing retries: what survives
        # is a gateway with no sessions, running a different version from the
        # install on disk. Deferring here keeps the sessions instead, and
        # ``_pending_update_respawn`` stays set so
        # ``_retry_pending_update_restart`` finishes the update once an
        # operator repairs the install.
        # Not a deferral the watchdog waits out: with the tree pruned the
        # bundle is gone too, and its exit lets the supervisor relaunch
        # through its own command, which a retry here cannot do. So one an
        # earlier drain armed ends here too.
        update_ownership.clear_restart_deferral()
        self._update_apply_deferred = True
        logger.error(
            "Update applied but restart deferred: no usable interpreter. "
            "Restore the interpreter and this retries itself -- the retry "
            "re-resolves it, so no configuration change is needed."
        )
        if self.dashboard_state:
            self.dashboard_state.push_update_progress(
                "restarting",
                "Update applied — restart needs a usable interpreter",
            )
        return
    # ONE restart sequence per process: the dashboard's ``_restart_gateway``
    # claims the same flag, and two sequences must never both drain sessions
    # and race separate successors. A restart already under way owns the
    # exec; this one stays pending and retries if that one is refused.
    state = self.dashboard_state
    if state is not None and getattr(state, "_gateway_restart_in_progress", False) is True:
        self._update_apply_deferred = True
        logger.info("A gateway restart is already in progress; the update restart waits")
        return
    if state is not None:
        state._gateway_restart_in_progress = True
    try:
        # Through the class, not ``self``: the restart is also driven with a
        # stand-in ``self`` that carries only the state it reads.
        await GatewayOrchestrator._restart_after_update_claimed(self, launcher, exe)
    finally:
        if state is not None:
            state._gateway_restart_in_progress = False
