"""Hook reconciler — reload app backend hooks when the CLI mutates them out-of-process.

The problem this solves (issue #7880)
--------------------------------------
An app's ``backend.hooks`` (``on_startup``, ``on_shutdown``, ``routes``) run
*inside the gateway process*. The gateway loads them once — at boot
(:func:`kiro_crew.apps.hooks_integration.on_gateway_startup`) and on the
dashboard enable/disable HTTP handlers — and thereafter serves whatever module
object is cached in ``sys.modules`` under the app's ``_kirocrew_app_<name>.*``
namespace.

The CLI ``kirocrew app {enable,disable,install,uninstall}`` subcommands are a
DIFFERENT process. They mutate the app on disk directly — flipping
``installed.json``, moving files, rotating ``.app_secret`` — and never call the
gateway. So after a CLI reinstall the running gateway keeps executing the OLD
module, the old ``on_startup`` background task keeps running, and its calls
start failing auth because ``.app_secret`` rotated underneath it. Only a full
``kirocrew restart`` picked up the change, and nothing said so.

This is the same class of gap the CLI already closes for CRON jobs (it writes
``crons.json`` and the gateway's timer tick re-syncs by content digest) and for
UI assets (the dev-mode watcher live-reloads ``ui/``). Python hooks had no such
reconciler. This module is it, modelled on :mod:`kiro_crew.apps.dev_mode`'s
singleton poll watcher.

One source of truth, no shadow bookkeeping
------------------------------------------
The set of "which apps have hooks loaded, under what on-disk signature" lives in
:mod:`kiro_crew.apps.hooks_integration` (``record_loaded_hook_signature`` /
``clear_loaded_hook_signature`` / ``loaded_hook_signature``), and EVERY driver
of the lifecycle updates it: the dashboard enable/disable handlers, the boot
startup pass, and this reconciler. That shared record is what keeps the
reconciler from fighting the in-process handlers — a dashboard-driven enable
records the new signature itself, so the reconciler sees no drift and does NOT
re-run the app's ``on_startup`` on its next tick (and symmetrically a dashboard
disable clears the record, so the reconciler does not re-run ``on_shutdown``
after teardown already reported clean). The reconciler only acts on a genuine
divergence between disk and that shared loaded-state.

How it works
------------
A single gateway-side task polls installed apps on an interval. For each app it
asks ``hook_signature(app_info)`` for the current on-disk identity and compares
to the shared loaded-signature. Three transitions drive the existing in-process
lifecycle, so no new teardown/reimport logic is invented here:

* loaded, now **disabled or gone**          -> :func:`on_app_disable`
  (runs ``on_shutdown`` when resolvable, deregisters routes, ``unload_app_modules``).
* loaded, **code or secret changed**         -> ``on_app_disable`` then
  :func:`on_app_enable` (full evict + reimport under the new secret).
* not loaded, now **enabled with hooks**      -> :func:`on_app_enable`.

``on_app_disable`` refuses to tear down while a timed-out ``on_startup`` task is
still owned (it clears the loaded-signature only past that guard), so the
reconciler observes the record still present and simply retries next tick.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from kiro_crew.apps.hooks_integration import (
    clear_loaded_hook_signature,
    compute_hook_signature,
    hook_enable_denied,
    loaded_hook_apps,
    loaded_hook_manifest,
    loaded_hook_signature,
    manifest_declares_hooks,
    on_app_disable,
    on_app_enable,
    record_loaded_hook_signature,
)
from kiro_crew.apps.lifecycle import app_has_retained_startup, apps_with_retained_startup
from kiro_crew.apps.manager import app_lifecycle_lock, get_app, list_apps
from kiro_crew.apps.module_loader import unload_app_modules

logger = logging.getLogger(__name__)

#: Poll cadence. Hooks change far less often than UI files (dev_mode polls at
#: 1s), and each tick walks every installed app's manifest via ``list_apps``, so
#: this is deliberately slow: a CLI reinstall going live within ~15s is the goal,
#: not sub-second latency. The interval is the lever that keeps the always-on
#: cost of that walk small on a gateway that rarely changes apps (dev_mode avoids
#: the walk with a sentinel because it polls every 1s; at this cadence the walk
#: is cheap enough not to warrant that machinery, and honesty about the cost is
#: in the interval rather than a claim that no walk happens).
POLL_INTERVAL_SECS = 15.0

#: A single app's turn in a pass is bounded by this WATCHDOG so a nonterminating
#: ``on_shutdown`` cannot wedge the whole pass (and thus every future tick). It is
#: NOT a cancel: the straggler keeps running to completion in the background (its
#: own lifecycle lock prevents a double-entry next tick); we only stop *waiting*
#: on it so the pass returns and the loop keeps polling. Generously larger than a
#: normal teardown so it only trips on a genuine hang.
PER_APP_PASS_WATCHDOG_SECS = 60.0

#: Total wall-clock budget for the shutdown-time drain (the in-flight-pass wait +
#: the one final reconcile pass) in ``stop_hook_reconciler``. ``_hooks_shutdown``
#: runs this BEFORE ``on_gateway_shutdown`` under its own ~10s budget, so a hung
#: reconcile here must NOT starve the backend shutdown sweep -- if the drain does
#: not settle within this bound we stop waiting and let shutdown proceed (the
#: straggler is not cancelled; process exit reclaims it). Kept well under the
#: ~10s ``_hooks_shutdown`` budget.
SHUTDOWN_DRAIN_BUDGET_SECS = 5.0

#: Set once ``stop_hook_reconciler`` begins the shutdown sweep. A watchdog can
#: leave a per-app reconcile task running past the pass that spawned it; without
#: this flag such a straggler could reach ``_enable_app`` and re-register hooks
#: AFTER ``on_gateway_shutdown`` has torn everything down (surviving an in-process
#: restart). Every ``_reconcile_app`` re-checks this under the app's lifecycle
#: lock immediately before doing any enable/teardown and bails if set, so no app
#: code is (re)started once shutdown has begun.
_stopping: bool = False

_reconcile_task: asyncio.Task | None = None

#: The reconcile pass currently running (one ``reconcile_once`` call), or None
#: between ticks. ``stop_hook_reconciler`` awaits this BEFORE cancelling the loop
#: so an in-flight app teardown (an async ``on_shutdown`` doing worker cleanup /
#: buffer flush) is never cancelled mid-flight -- which would leave app code
#: running or lose buffered data.
_active_pass: asyncio.Task | None = None

#: Per-app in-flight ``_reconcile_app`` task. A pass only spawns a new task for an
#: app that has none here: a straggler left running by the watchdog still holds the
#: app's lifecycle lock, so re-spawning it every tick would pile up waiters blocked
#: on that lock until the gateway OOMs. Skipping an app with a live task means at
#: most ONE reconcile per app is ever outstanding; the entry clears when the task
#: finishes (done callback), so the next tick picks the app up normally.
_inflight_app_tasks: dict[str, asyncio.Task] = {}


def _clear_inflight(app_name: str, task: asyncio.Task) -> None:
    """Done-callback: drop the in-flight registry slot for ``app_name``.

    Identity-compared: a later tick may already have replaced the entry with a
    fresh task, and only the task that owns the slot should clear it.
    """
    if _inflight_app_tasks.get(app_name) is task:
        _inflight_app_tasks.pop(app_name, None)


# The gateway services an enable/disable needs, captured at watcher start so the
# reconciler can build the same AppContext the boot and dashboard paths do.
_cron_service: Any = None
_broadcast_fn: Any = None
_spawn_impl: Any = None


async def _disable_loaded(name: str, app_info: dict[str, Any] | None) -> bool:
    """Drive the in-process teardown for an app whose hooks are currently loaded.

    The CALLER holds ``app_lifecycle_lock(name)``. Returns True when teardown
    settled — ``on_app_disable`` cleared the shared loaded-signature past its
    startup-ownership guard, so the caller can rely on ``loaded_hook_signature``
    being ``None`` afterwards. Returns False when a detached ``on_startup`` task
    is still owned; ``on_app_disable`` reports that as a ``startup_cleanup``
    failure and leaves the record in place, so the reconciler retries next tick
    rather than forcing a half-swapped state.

    ``app_info`` is the current on-disk app state, or ``None`` when the app has
    been UNINSTALLED between ticks. In BOTH cases teardown resolves the running
    hook from the manifest it was actually loaded from — retained in the shared
    registry (``loaded_hook_manifest``) — not from ``app_info``: on a code change
    ``app_info`` is the REPLACEMENT manifest, whose ``on_shutdown``/routes/module
    identity may differ from the live one, and on uninstall it is ``None`` yet a
    background task the old ``on_startup`` spawned is still live in the gateway
    and must be shut down. Only if no retained manifest exists do we fall back to
    ``app_info`` (then to the manifest-less, hooks-skipped teardown: route dereg +
    module unload + detached startup-task stop still run by name).
    """
    # Always tear the running hook down from the manifest it was LOADED from,
    # never the caller's (possibly newer) on-disk ``app_info``. On a code change
    # the reload branch passes the replacement manifest, but the live routes,
    # module identity and ``on_shutdown`` belong to the OLD manifest recorded in
    # the shared registry; resolving teardown from the replacement would stop the
    # wrong ``on_shutdown`` (or none, if it was renamed/removed) and leave the
    # old hook running. The retained manifest is the authoritative record of what
    # was actually LOADED, so it -- and only it -- drives ``on_shutdown``.
    #
    # A retained manifest exists ONLY for an app the gateway loaded healthily
    # (the enable/boot record gate retains it, and the denied/degraded paths
    # deliberately record signature-only with NO manifest). So ``run_app_hooks``
    # keys off the retained manifest, NOT off whether ``app_info`` is present: an
    # execution-DENIED app that is later disabled still has ``get_app`` returning
    # its on-disk state (``app_info`` non-None), but nothing of it was ever
    # started, so running its ``on_shutdown`` would be a shutdown-only execution
    # vector. With no retained manifest we run the hooks-skipped teardown (route
    # dereg + module unload + detached startup-task stop still run by name).
    retained = loaded_hook_manifest(name)
    if retained is not None:
        app_info = {"name": name, "manifest": retained}
        run_app_hooks = True
    else:
        app_info = {"name": name, "manifest": {}}
        run_app_hooks = False
    result = await on_app_disable(
        name,
        app_info,
        run_app_hooks=run_app_hooks,
        bounded_startup_cleanup=True,
    )
    if str(result.get("startup_cleanup", "")).startswith("failed:"):
        logger.info("hook reconcile: %s still has a retained startup hook; will retry", name)
        return False
    # A failed on_shutdown means the app's own stop routine did NOT complete, so
    # its worker may still be live. Treat it as UNSETTLED: keep the loaded record
    # (on_app_disable leaves the signature in place only when startup ownership
    # was unclear, so re-record here) so the reconciler retries rather than
    # accepting teardown and letting the worker survive disable/uninstall.
    if result.get("hooks_shutdown") == "failed":
        logger.info("hook reconcile: %s on_shutdown failed; retaining record for retry", name)
        await record_loaded_hook_signature(name, {"name": name, "manifest": retained or {}})
        return False
    # Settled teardown: unload the app's modules so a later reinstall
    # (disable/enable without a process restart) re-imports fresh code instead of
    # reusing stale transitive helper modules still resident in sys.modules --
    # otherwise old code stays active after a CLI reinstall. Bumps the load
    # generation, invalidating any cached shutdown callable from this generation.
    unload_app_modules(name)
    return True


async def _enable_app(name: str, app_info: dict[str, Any]) -> None:
    """Drive the in-process reimport (routes + on_startup) for a newly-live app.

    The CALLER holds ``app_lifecycle_lock(name)``. ``on_app_enable`` records the
    shared loaded-signature itself on success (and re-checks admission), so a
    partial failure leaves no record and the next tick retries.
    """
    await on_app_enable(
        name,
        app_info,
        cron_service=_cron_service,
        broadcast_fn=_broadcast_fn,
        spawn_impl=_spawn_impl,
    )


async def _reconcile_app(name: str, snapshot_info: dict[str, Any] | None) -> None:
    """Reconcile ONE app under its lifecycle lock, re-reading state inside it.

    ``snapshot_info`` is what the tick's ``list_apps`` read for this app (or
    ``None`` if it was absent then). It only decides that this app is WORTH
    examining; the authoritative state used to act is re-read here under
    ``app_lifecycle_lock(name)`` via ``get_app``, because a dashboard
    enable/disable/uninstall or a trust withdrawal may have run between the
    snapshot and now. Holding the lock is what closes the revive-during-
    trust-withdrawal race GPT flagged: a concurrent revoke either has not started
    (we see it next tick) or has taken the lock first and we block until it is
    done and then observe the withdrawn state.
    """
    async with app_lifecycle_lock(name):
        # Authoritative re-read under the lock. get_app touches disk, so off-loop.
        current = await asyncio.to_thread(get_app, name)
        loaded = loaded_hook_signature(name)

        # --- teardown branch: loaded, but now gone / disabled / hookless ---
        gone = current is None
        turned_off = current is not None and (
            not current.get("enabled") or not manifest_declares_hooks(current)
        )
        # A degraded/timed-out startup leaves the loaded-signature record CLEARED
        # (so the wiring retries on recovery) yet its detached startup task keeps
        # running. Such an app has ``loaded is None`` but still needs teardown when
        # it goes away, or the task is orphaned after uninstall/trust removal.
        retained = loaded is None and app_has_retained_startup(name)
        if (loaded is not None or retained) and (gone or turned_off):
            if await _disable_loaded(name, current):
                logger.info("hook reconcile: tore down hooks for %s", name)
            return

        # Past teardown, every remaining branch (re)starts app code. Once the
        # shutdown sweep has begun, a watchdog-stranded task reaching here would
        # re-register hooks AFTER on_gateway_shutdown tore them down (surviving an
        # in-process restart). Teardown above stays allowed (the final drain needs
        # it); enable does not. Checked UNDER the lock so it observes a concurrent
        # stop that took the lock first.
        if _stopping:
            return

        # --- load / reload branch: enabled hook app whose signature changed ---
        if current is None or not current.get("enabled") or not manifest_declares_hooks(current):
            return
        sig = await compute_hook_signature(current)
        if sig == loaded:
            # Same signature as recorded. Normally nothing to do -- but a DENIED
            # app was recorded SIGNATURE-ONLY (anti-churn) with no manifest, and
            # granting it execution trust does NOT change the hook signature. So a
            # plain ``sig == loaded`` short-circuit would strand a now-admitted app
            # forever. Distinguish the two: a real loaded app retains a manifest; a
            # denied record does not. For a manifest-less (denied) record, re-check
            # admission under the lock -- if it is now admitted, fall through and
            # load it; if still denied, stay quiet (no per-tick churn).
            if loaded_hook_manifest(name) is not None:
                return
            if await asyncio.to_thread(hook_enable_denied, name):
                return
            # Now admitted with unchanged hooks -> load them (fall through).
            logger.info("hook reconcile: loading now-admitted app %s", name)
            await _enable_app(name, current)
            return
        # Re-check admission UNDER the lock before running any app code: an app
        # mid-trust-withdrawal (or already denied) must never be revived here.
        denied = await asyncio.to_thread(hook_enable_denied, name)
        if denied:
            # A loaded app reinstalled out-of-process from a source that no
            # longer matches its execution grant lands here (sig changed +
            # now denied). The denied enable path deregisters routes + records
            # the anti-churn signature + drops the retained manifest, but it does
            # NOT stop the already-loaded module or the background task its
            # on_startup spawned -- so withdrawn-admission code would keep running
            # indefinitely while the app stays enabled. Tear the loaded module
            # down FIRST (bail/retry next tick if it does not settle), then route
            # through on_app_enable's denied path so the denial handling stays in
            # one place. Reached only when the signature actually changed, so no
            # per-tick churn.
            if loaded is not None and not await _disable_loaded(name, current):
                return
            await _enable_app(name, current)
            return
        if loaded is not None:
            # Reinstall of new code under a still-enabled app: evict the stale
            # module + old startup task BEFORE reimporting, or the new on_startup
            # is refused as a duplicate and the stale module (old .app_secret)
            # keeps serving. If teardown does not settle, retry next tick.
            if not await _disable_loaded(name, current):
                return
            logger.info("hook reconcile: reloading changed hooks for %s", name)
        else:
            logger.info("hook reconcile: loading hooks for newly-enabled %s", name)
        try:
            await _enable_app(name, current)
        except Exception:
            # A reimport failure must not wedge the loop. on_app_enable records
            # the shared signature only on healthy wiring, so leaving it unset
            # re-attempts next tick; clear defensively against a partial enable.
            clear_loaded_hook_signature(name)
            logger.exception("hook reconcile: reload failed for %s", name)


async def reconcile_once(installed: list[dict[str, Any]]) -> None:
    """One reconcile pass over the given ``list_apps`` snapshot.

    The caller reads ``installed`` off the event loop and hands it in. This
    coroutine decides WHICH apps to examine from the snapshot + the shared
    loaded-set, then reconciles each one under its own lifecycle lock (state is
    re-read inside the lock — see ``_reconcile_app``). Reads/writes the shared
    loaded-signature registry in ``hooks_integration`` — never a private map.
    """
    by_name = {a.get("name", ""): a for a in installed}

    # Every app worth a look this tick: those we have loaded (to catch a
    # disable/uninstall) plus every enabled hook-declaring app on disk (to catch
    # a new install / reinstall). Deduplicated; the per-app lock + re-read makes
    # the exact snapshot value non-authoritative, so this set only needs to be a
    # superset of what actually changed.
    candidates_set = set(loaded_hook_apps())
    candidates_set.update(
        name
        for name, info in by_name.items()
        if info.get("enabled") and manifest_declares_hooks(info)
    )
    # Also examine apps whose loaded record was cleared on a degraded startup but
    # whose detached startup task is still live -- they must be torn down when they
    # go away, not orphaned (see app_has_retained_startup / the teardown branch).
    candidates_set.update(apps_with_retained_startup())
    # A stable order so the gather arg list and the result zip below line up.
    candidates = sorted(candidates_set)
    # Reconcile every candidate CONCURRENTLY, isolating each app's failure.
    # Concurrency (not a serial loop) is what keeps one app's slow ``on_shutdown``
    # from starving the others -- a later CLI-disabled app must not wait behind a
    # still-running teardown. Crucially we do NOT wrap each app in a cancelling
    # ``wait_for``: teardown runs ``on_shutdown`` and only THEN deregisters routes
    # + clears the loaded record, so cancelling it mid-flight would leave a
    # disabled app's routes callable. Each app instead runs to completion under
    # its own lifecycle lock; ``return_exceptions=True`` collects (rather than
    # propagates) a raising app so the others still finish, and the raiser is
    # retried next tick.
    #
    # ``on_app_disable`` bounds a hung retained STARTUP task, but the app's own
    # ``on_shutdown`` coroutine is awaited unbounded -- a nonterminating one would
    # hang this whole pass and, since the loop awaits the pass, wedge every future
    # tick (later CLI disables never reconcile). So each app's turn is bounded by
    # a WATCHDOG that does NOT cancel it: ``asyncio.wait`` returns when the timeout
    # elapses but leaves the straggler RUNNING, so the pass returns and the loop
    # keeps polling while the stuck teardown finishes on its own -- honoring both
    # "never cancel a teardown mid-``on_shutdown``" and "a hung teardown must not
    # wedge future passes".
    #
    # A straggler still holds the app's lifecycle lock, so we must NOT re-spawn it
    # every tick: a new ``_reconcile_app`` task would just block on that lock, and
    # the waiters would pile up until the gateway OOMs. ``_inflight_app_tasks``
    # tracks the one outstanding task per app; a candidate that already has a live
    # task is SKIPPED this tick (it is retried once the straggler finishes and its
    # done-callback clears the entry). At most one reconcile per app is ever live.
    app_tasks: dict[asyncio.Task, str] = {}
    for name in candidates:
        existing = _inflight_app_tasks.get(name)
        if existing is not None and not existing.done():
            logger.debug(
                "hook reconcile: %s still has an in-flight teardown; skipping this tick",
                name,
            )
            continue
        task = asyncio.ensure_future(_reconcile_app(name, by_name.get(name)))
        _inflight_app_tasks[name] = task
        # Clear the registry slot when the task finishes so the next tick can
        # re-examine the app (identity-compared inside _clear_inflight: a later
        # tick may already have replaced the entry).
        task.add_done_callback(partial(_clear_inflight, name))
        app_tasks[task] = name
    if not app_tasks:
        return
    done, pending = await asyncio.wait(app_tasks.keys(), timeout=PER_APP_PASS_WATCHDOG_SECS)
    for task in done:
        exc = task.exception()
        if exc is not None:
            logger.error(
                "hook reconcile: %s failed this tick; other apps unaffected, will retry",
                app_tasks[task],
                exc_info=exc,
            )
    for task in pending:
        # Left RUNNING on purpose -- not cancelled. Its lifecycle lock blocks a
        # double-entry next tick; we only stopped waiting so the pass can return.
        logger.warning(
            "hook reconcile: %s teardown exceeded %.0fs; letting it finish in the "
            "background so it does not wedge future passes",
            app_tasks[task],
            PER_APP_PASS_WATCHDOG_SECS,
        )


async def _reconcile_loop() -> None:
    """Poll installed apps and reconcile loaded hooks to disk on each tick."""
    global _active_pass
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            # list_apps walks the apps dir + reads two files per app — off loop.
            installed = await asyncio.to_thread(list_apps)
            # Run the pass as a tracked task and await it SHIELDED: if the loop is
            # cancelled (gateway shutdown / in-process restart) while a teardown's
            # async on_shutdown is mid-flight, the shield keeps that pass running
            # to completion instead of interrupting worker cleanup / a buffer
            # flush. stop_hook_reconciler awaits _active_pass before cancelling,
            # so the pass is normally already done by the time cancellation lands;
            # the shield covers the race where the sleep above is what gets
            # cancelled just as a pass starts.
            _active_pass = asyncio.ensure_future(reconcile_once(installed))
            try:
                await asyncio.shield(_active_pass)
            finally:
                _active_pass = None
        except asyncio.CancelledError:
            # If a pass is still in flight (shield let the cancel through to the
            # await), let it finish before unwinding — never abandon a teardown.
            if _active_pass is not None and not _active_pass.done():
                try:
                    await _active_pass
                except Exception:
                    pass
                _active_pass = None
            break
        except Exception:
            logger.exception("hook reconcile loop error")
            await asyncio.sleep(POLL_INTERVAL_SECS)


def init_hook_reconciler(
    *,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> None:
    """Start the singleton hook reconciler (idempotent). Called at gateway startup.

    MUST be called AFTER ``on_gateway_startup`` has loaded the boot-time hooks:
    that pass records each loaded app's signature in the shared registry, so the
    reconciler's first tick sees no drift and does no redundant work — no seeding
    step of its own is needed. Synchronous and does NO IO: it only captures the
    gateway service handles and schedules the loop, so it adds nothing to the
    critical path before the dashboard socket binds (the first ``list_apps`` walk
    happens on the loop's own first tick, off the event loop).
    """
    global _reconcile_task, _cron_service, _broadcast_fn, _spawn_impl
    if _reconcile_task is not None and not _reconcile_task.done():
        return
    global _stopping
    _stopping = False
    _cron_service = cron_service
    _broadcast_fn = broadcast_fn
    _spawn_impl = spawn_impl
    _reconcile_task = asyncio.get_running_loop().create_task(_reconcile_loop())
    logger.info("app hook reconciler started (%.0fs cadence)", POLL_INTERVAL_SECS)


async def stop_hook_reconciler() -> None:
    """Cancel the reconciler task and await teardown (shutdown / tests).

    Drains any in-flight reconcile pass FIRST (a teardown's async ``on_shutdown``
    may be doing worker cleanup / a buffer flush), then cancels the loop and
    awaits it so the coroutine has unwound before returning — an in-process
    gateway restart can then start a fresh reconciler without the old one
    lingering with stale service handles. After cancelling, runs ONE final
    reconcile pass so a CLI disable/uninstall that landed after the last poll is
    settled before ``on_gateway_shutdown`` (which only tears down what is still
    loaded) — otherwise that app's ``on_shutdown`` flush would be skipped and its
    buffered data lost. That final pass is one-shot, not a resurrected poll loop,
    so nothing can re-import or re-start a hook after the shutdown sweep. The
    shared loaded-signature registry is owned by ``hooks_integration`` and is
    deliberately NOT cleared here: ``on_gateway_shutdown`` handles teardown of the
    live hooks, and a fresh boot re-records signatures via ``on_gateway_startup``.
    """
    global _reconcile_task
    global _stopping
    # Mark stopping BEFORE anything else: any watchdog-stranded per-app task still
    # running from a prior pass (tracked in _inflight_app_tasks) re-checks this
    # under its lock before enabling, so it cannot re-register hooks during or
    # after the shutdown sweep. Set first so even a task about to take the lock
    # observes it.
    _stopping = True
    task = _reconcile_task
    _reconcile_task = None
    if task is not None:
        # ONE shared deadline for the ENTIRE reconciler shutdown drain. Each step
        # below waits only for whatever remains of the single budget, so the
        # cumulative wait (in-flight drain + cancel unwind + final drain + strand
        # coordination) can never exceed SHUTDOWN_DRAIN_BUDGET_SECS -- otherwise
        # sequential 5s waits would blow the ~10s gateway deadline and skip the
        # backend cleanup _hooks_shutdown runs next.
        loop = asyncio.get_running_loop()
        _drain_deadline = loop.time() + SHUTDOWN_DRAIN_BUDGET_SECS

        def _remaining() -> float:
            return max(0.0, _drain_deadline - loop.time())

        # Let any in-flight reconcile pass finish FIRST: cancelling the loop while
        # a teardown's async on_shutdown is running would interrupt its worker
        # cleanup / buffer flush (app code survives or buffered data is lost).
        # asyncio.shield keeps the pass RUNNING if we stop waiting (never cancelled
        # mid-teardown); process exit reclaims a true hang.
        pass_ = _active_pass
        drained = True
        if pass_ is not None and not pass_.done():
            try:
                await asyncio.wait_for(asyncio.shield(pass_), timeout=_remaining())
            except asyncio.TimeoutError:
                # The pass is hung. It stays running under the shield, but we must
                # NOT wait on it again below -- the loop's cancel handler re-awaits
                # _active_pass, which would block up to the 60s pass watchdog and
                # blow the shared drain budget before backend cleanup.
                drained = False
            except Exception:
                pass
        task.cancel()
        if drained:
            # Pass already settled: awaiting the cancelled loop unwinds promptly,
            # but still bound it to the shared deadline for safety.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_remaining())
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        else:
            # Hung pass: bound the unwind by whatever remains of the shared budget,
            # so cancellation cannot re-await the straggler past it. The shielded
            # pass keeps running; process exit reclaims a true hang.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_remaining())
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

        # ONE-SHOT terminal drain: a CLI disable/uninstall landing in the window
        # between the last poll and this stop would otherwise never reach the
        # gateway -- the loop is now cancelled, and on_gateway_shutdown (called
        # right after this returns) tears down only what is still loaded, so a
        # just-disabled app's on_shutdown flush would be skipped and its buffered
        # data lost. Run a SINGLE final reconcile pass to settle that pending
        # disable before shutdown. This is a one-shot flush, NOT a resurrected
        # poll loop -- the recurring poll stays cancelled, so nothing can re-import
        # or re-start a hook after the shutdown sweep. Bounded by whatever remains
        # of the shared drain budget so it cannot exceed _hooks_shutdown's deadline
        # and leave spawned backends running past exit; best-effort, and any
        # error/timeout must not block gateway shutdown.
        try:
            installed = await asyncio.to_thread(list_apps)
            await asyncio.wait_for(reconcile_once(installed), timeout=_remaining())
        except asyncio.TimeoutError:
            logger.warning(
                "hook reconcile: final drain pass exceeded the %.0fs shared budget; "
                "proceeding to gateway shutdown",
                SHUTDOWN_DRAIN_BUDGET_SECS,
            )
        except Exception:
            logger.exception("hook reconcile: final drain pass before shutdown failed")

        # Coordinate any watchdog-stranded per-app tasks left running from an
        # earlier pass: with _stopping set they bail at the enable point, so a
        # bounded wait lets them unwind before on_gateway_shutdown rather than
        # racing the sweep. Not cancelled (a teardown mid-on_shutdown must finish);
        # bounded so a genuine hang cannot exceed the shutdown budget -- process
        # exit reclaims it.
        inflight = [t for t in _inflight_app_tasks.values() if not t.done()]
        if inflight:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(asyncio.shield(t) for t in inflight), return_exceptions=True),
                    timeout=_remaining(),
                )
            except (asyncio.TimeoutError, Exception):
                pass
