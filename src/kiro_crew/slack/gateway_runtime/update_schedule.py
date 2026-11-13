"""What the scheduler does with an available update, and what a restart waits on.

The scheduled update checks (provider-driven and the legacy layout-aware one) only
stage a managed-venv build or announce an update: no unattended path applies an
update or restarts a running gateway, and the scheduler has no restart code at all.
Also here: the effect an update would have, the in-flight-work census, and the
callback drain and pause release the user-clicked restart takes
(:mod:`kiro_crew.restart_admission`).

Orchestrator fields read: ``_UPDATE_BUSY_RETRY_SECS``, ``_UPDATE_DRAIN_TIMEOUT_SECS``,
``_running_script_ids``, ``_session_tasks``, ``cron_svc``, ``dashboard_state``,
``sessions``, ``subagent_mgr``, ``task_runner``.
Orchestrator fields written: ``_update_apply_deferred``.

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
        GatewayOrchestrator,
        _remediation_command,
        _update_info,
        auto_update_effect,
        distribution,
        inbound_spool,
        logger,
        record_auto_update_effect,
        redact_log_via_context,
        shutdown_event,
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


async def _drain_update_callback_work(
    self: GatewayOrchestrator, *, timeout: float | None, include_turns: bool = True
) -> bool:
    """Wait without cancellation until callbacks and refusal writes are durable.

    ``include_turns=False`` leaves the running turn and handler tasks alone and
    waits only for inbound callback reservations and refusal writes: a restart
    the person confirmed cuts those turns (``close_all`` unwinds them), so
    waiting on them would only end in a refusal nobody asked for.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + max(0.0, timeout)

    def _callback_claims() -> int:
        # A claim spans the whole turn its message started, so a drain that does
        # not wait for turns leaves out the claims held by tasks that own a
        # running session turn: ``close_all`` cuts those turns, which releases
        # the claims, and waiting on them only ends in a refusal.
        sessions = self.sessions
        if sessions is None:
            return 0
        count = max(0, int(sessions.inbound_callback_count))
        if include_turns or count == 0:
            return count
        try:
            boundary = sessions._allocation_boundary()
            running = {
                s.turn_owner for s in list(boundary._sessions.values()) if s.turn_owner is not None
            }
            held = sum(
                1
                for token in list(boundary._inbound_callback_reservations)
                if getattr(token, "owner", None) in running
            )
            return max(0, count - held)
        except Exception:
            logger.debug("update restart: turn-owned callbacks unreadable", exc_info=True)
            return count

    while True:
        handlers = self._live_update_handler_tasks() if include_turns else []
        writes = list(inbound_spool.pending_refusal_writes())
        reservations = 0
        if self.sessions is not None:
            try:
                reservations = _callback_claims()
            except Exception:
                logger.warning("update restart: callback count unavailable", exc_info=True)
                return False
        if not handlers and not writes and reservations <= 0:
            # Let callbacks already queued on the loop either reserve or
            # observe the closed gate, then recheck. The caller commits the
            # synchronous restart fence immediately after this returns.
            await asyncio.sleep(0)
            handlers = self._live_update_handler_tasks() if include_turns else []
            writes = list(inbound_spool.pending_refusal_writes())
            reservations = _callback_claims() if self.sessions is not None else 0
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


#: How long the pre-restart drain waits for callbacks and refusal writes to
#: become durable before deferring the restart. Named so a test about the
#: fetch/reset/venv SEQUENCE can shorten it (the drain polls at 10ms to the
#: deadline, 30s of sleeping per test otherwise); the value itself stays pinned
#: by the test that IS about it (``test_restart_fences_then_closes_and_final_drains``).
_UPDATE_DRAIN_TIMEOUT_SECS = 30.0


async def _finish_auto_update_apply(self: GatewayOrchestrator) -> None:
    """Reopen admission when apply returns instead of replacing the process.

    The spool replay runs only when admission actually reopened: during a
    shutdown the pause is kept, and a replay then would tell senders to
    resend while the gateway is stopping.
    """
    sessions = self.sessions
    if sessions is None:
        return
    # The holder is cleared only once the resume returned, with no await
    # between: a pause queued behind this resume returns re-entrantly on the
    # pause being released, and must still see whose it is (and stand aside)
    # rather than take it and have it reopened under it.
    try:
        reopened = await sessions.resume_turn_admission_after_update()
    except Exception:
        logger.exception("Auto-update could not resume turn admission")
    else:
        if reopened:
            self._schedule_inbound_replay()
    finally:
        self._update_pause_holder = None


def _wheel_apply_module() -> Any:
    """The loaded managed-venv apply module, or ``None``. Never imports.

    Looked up in ``sys.modules``: no apply can be in flight in a process that
    never loaded it, and the stop paths must not import from a tree an update
    may have changed.
    """
    return sys.modules.get("kiro_crew.platform.wheel_apply")


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
    pauses admission or applies anything: an update this install will not
    prepare is only announced.
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
    """Provider-delegated update check. It reports; it never applies.

    The policy's ``apply_command`` replaces the install this process runs from
    in place, and a running gateway is never updated that way (decision entry
    "updates never restart a running gateway"). So a voluntary update and a mandatory floor alike light
    the dashboard's update notice, and a person's Update runs the command, behind
    the same quiescence confirmation as a restart.
    """
    from kiro_crew import __version__ as _running_version
    from kiro_crew.platform.update_governance import min_version, update_required
    from kiro_crew.platform.update_provider import UpdateProvider

    assert isinstance(provider, UpdateProvider)

    # Say who owns updates on every path, "already latest" included, so the
    # dashboard need not run its own check to learn it.
    _update_info["managed_by"] = MANAGED_BY_COMMAND
    effect = await self._auto_update_effect(provider)
    # ``can_apply`` read off that one derivation rather than asked of the
    # provider again: it is the PROVIDER's answer, not the install shape's
    # (a reader told "cannot apply" because the shape is a wheel would be
    # wrong on every policy-managed host).
    _update_info["can_apply"] = effect.route == AUTO_ROUTE_PROVIDER
    result = await provider.check()

    # The mandatory floor is an enterprise ceiling and is evaluated FIRST,
    # before any check-error early return: a host below min_version must
    # still be told, even when the provider's check could not complete.
    if update_required(_running_version):
        if not result.available:
            # A floor pinned above the highest installable build (a policy typo,
            # or a floor set ahead of the current release): nothing to apply.
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s, "
                "but no newer build is available to apply",
                _running_version,
                min_version(),
            )
        else:
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s; "
                "the update command replaces the running install in place, so it "
                "is applied by a person (Update), not while the gateway runs",
                _running_version,
                min_version(),
            )
        self._publish_provider_update_state(result)
        if self.dashboard_state:
            self.dashboard_state.push_refresh("update_available")
        return

    # Below the mandatory floor: a check error is a non-answer, not a
    # verdict — report it and stop rather than treating it as "up to date".
    if result.error:
        logger.info("Update check did not complete (%s)", result.error)
        return

    if result.available:
        from kiro_crew.config import KiroCrewConfig

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if cfg.auto_update:
            logger.info("Auto-update is on, but %s; notifying instead", effect.reason)
        self._publish_provider_update_state(result)
        if self.dashboard_state:
            self.dashboard_state.push_refresh("update_available")
    else:
        print("👻 Already on latest version")


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

        # A policy-pinned minimum version makes the update MANDATORY: it
        # overrides the user's auto_update=False, because user config sits
        # under the enterprise ceiling and an operator opting out must not
        # hold a fleet on a build the policy forbids.
        #
        # Checked BEFORE the `available` branch, and deliberately independent
        # of it: the mandate is about whether THIS host satisfies the floor,
        # not about whether a newer build was advertised. The apply
        # (`kirocrew update` at the next start) still applies the source pin and
        # its own no-new-commits early return, so this cannot bypass the ceiling.
        mandatory_required = update_required(_running_version)
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
                # A git reset rewrites the tree this process serves from, and a
                # running gateway is never updated that way (decision entry "updates
                # never restart a running gateway"): the floor's notice stays up and a person's Update
                # applies it, behind the quiescence confirmation. Nothing is
                # written into the shared check cache here: it holds the real
                # verdict, and a fabricated "update available" would overwrite a
                # failed or diverged check.
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s; "
                    "a git update replaces the running checkout in place, so it is "
                    "applied by a person (Update), not while the gateway runs",
                    _running_version,
                    min_version(),
                )
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
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
                # instead, the command `kirocrew update` prints for this
                # shape, and say it runs from the user's own terminal: an
                # agent can read this log line. pipx and the managed venv keep
                # the `kirocrew update` pointer, which upgrades them correctly.
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
                        "but this install (%s) cannot apply it unattended (%s) — from your "
                        "own terminal, upgrade this environment in place with `%s`, then run `%s`",
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
                        "signed upgrade %s Once upgraded, run `%s`. Run both steps from your "
                        "own terminal.",
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
            # Only the managed venv installs unattended, and only by building
            # beside the running tree (the branch below). A git checkout's reset
            # and a policy command rewrite the tree this process serves from, so
            # `auto_update_effect` makes them `notify`: a person's Update applies
            # them, and commit distance without a version bump only lights the
            # badge.
            from kiro_crew.update_deferral import user_deferred_update

            # "Later" in an update prompt is the user's answer for that version:
            # it stays offered, and is never staged unattended.
            deferred = user_deferred_update(cfg, str(info.get("latest_version") or ""))
            installs = cfg.auto_update and effect.effect == AUTO_EFFECT_INSTALL and not deferred
            if installs and effect.route == AUTO_ROUTE_WHEEL and _remediation_command(info):
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
                elif cfg.auto_update and effect.manual_only:
                    # The operator's policy chose this, so it is not a fault: the
                    # provider path logs the same case at INFO.
                    logger.info("Auto-update is on, but %s — notifying instead", effect.reason)
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
