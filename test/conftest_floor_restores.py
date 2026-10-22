"""What a test leaves running or rebound, retired or put back: background
workers and loop leftovers joined before the pins lift, telemetry exporters, the
logging globals, the hooks dispatcher, and Windows proactor finalizers.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import gc
import logging
import sys
import threading
import time
import warnings

import pytest


def _join_install_receipt_workers() -> None:
    """Join any ``install_receipt.dispatch()`` worker before this test's pins lift.

    ``dispatch()`` hands the receipt write to a daemon thread. The worker receives
    every environment-derived input (the data home, the config fields) from the
    dispatching thread and reads nothing itself -- but a thread still alive when
    ``monkeypatch`` undoes ``KIROCREW_HOME`` is a thread whose WRITE lands after the
    test that owned it is gone. The per-test probe in the 5x hygiene run saw exactly
    that: the ``kirocrew-install-receipt`` thread alive after teardown in 3 of 5
    runs, and once a receipt secret written into the operator's real ``~/.kiro/crew``.

    Structural rather than opt-in, for the same reason as the CWD restore: the
    test that leaks is never the test that fails, so relying on each
    dispatch-triggering test to remember ``wait_for_pending_receipt_writes()`` is the
    shape that let this through. It lives in the ``tryfirst`` teardown hook for the
    ordering reason documented on ``pytest_runtest_teardown``: an autouse fixture
    from this conftest is an OUTER fixture and would tear down AFTER ``monkeypatch``
    had already restored the environment. With no worker registered this is one
    lock acquire; the module is looked up rather than imported so a test run that
    never touches the receipt path pays nothing.
    """
    mod = sys.modules.get("kiro_crew.apps.install_receipt")
    waiter = getattr(mod, "wait_for_pending_receipt_writes", None)
    if waiter is not None:
        waiter()


#: Thread name ``metrics.provider._schedule_consent_check_locked`` gives its worker.
#: Matched by name, like the exporter guard below: the module keeps no handle to the
#: thread (only an in-flight flag), so the name is the one thing a test can find it by.
_TELEMETRY_CONSENT_THREAD = "kirocrew-telemetry-consent"


def _live_telemetry_consent_workers() -> list:
    return [
        t for t in threading.enumerate() if t.name == _TELEMETRY_CONSENT_THREAD and t.is_alive()
    ]


def _resume_crew_log_eager_fold() -> None:
    """Lift the eager-fold fence for the test about to run.

    The mirror of :func:`_retire_crew_log_eager_fold`: that one fences the gap between
    tests, this one hands the worker back to the test itself. Looked up rather than
    imported, so a run that never touches the crew log pays nothing.
    """
    mod = sys.modules.get("kiro_crew.crew_log.eager")
    resume = getattr(mod, "resume_for_tests", None)
    if resume is not None:
        resume()


def _retire_crew_log_eager_fold() -> "BaseException | None":
    """Retire the crew log's append path and its fold worker before this test's pins lift.

    The worker is a process-wide daemon that the PRODUCTION append path starts: an
    ``emit`` call with the crew log on reaches ``note_commit``, which starts the thread.
    So any test that appends a work or panel entry has one running, whether or not it
    knows the module exists -- and the worker resolves ``KIROCREW_HOME`` when it folds,
    which is the pin that lifts a moment later. Stopping it here rather than in each
    file's own fixture is the point: the next test file to call ``on_work_recorded``
    should be safe by default, not by remembering.

    Same place and same reason as :func:`_join_install_receipt_workers` -- the
    ``tryfirst`` teardown hook runs before any fixture finalization, so the pins still
    hold. The module is looked up rather than imported, so a run that never touches the
    crew log pays nothing.

    ``retire_for_tests`` owns the ORDER (drain the writer, then stop the worker, and stop
    nothing if the writer will not drain) because both halves are its own; this floor owns
    only WHEN and WHERE TO REPORT. The failure is RETURNED, never raised from here: raising
    on this side of the hookwrapper's ``yield`` would skip every fixture finalization behind
    it, which is worse than the leak it reports. The caller raises it after the ``yield``
    instead, beside :func:`_refuse_a_resolved_real_default_home`, which is the established
    place for "this test left damage".
    """
    mod = sys.modules.get("kiro_crew.crew_log.eager")
    retire = getattr(mod, "retire_for_tests", None)
    if retire is None:
        return None
    return retire()


#: How long the executor join waits before giving up on a wedged job. Long enough for
#: a config read on a loaded host; short enough that a job blocked forever surfaces as
#: a slow teardown rather than a hung worker (pytest-timeout is not armed here).
_EXECUTOR_JOIN_SECS = 10.0


def _join_test_loop_executor(item) -> None:
    """Retire the test loop's leftovers before this test's pins lift.

    Two things outlive a test on its own event loop, and pytest-asyncio 0.21 ends the
    loop with a bare ``loop.close()`` that waits for neither:

    * **Pending tasks.** A task the code under test detached and the test never awaited
      is destroyed with the loop, and a coroutine destroyed mid-await gets
      ``GeneratorExit`` thrown into it -- so its ``finally`` blocks run at garbage
      collection, whenever that is. ``_run_chat``'s queue-cycle ``finally`` reaches a
      synchronous ``KiroCrewConfig.load()``; run after the ``KIROCREW_HOME`` pin is gone,
      that resolves the OPERATOR's data home. A fresh fake ``HOME`` grew ``~/.kiro/crew``
      and the breadcrumb after four subagent ``on_done`` tests exactly this way, and not
      one of them failed.
    * **Default-executor jobs.** ``asyncio.to_thread`` and ``run_in_executor(None, ...)``
      hand work to the loop's default ``ThreadPoolExecutor``; ``close()`` shuts it down
      without waiting, so a running or queued ``KiroCrewConfig.load()`` finishes after
      the pins are gone. A started thread cannot be cancelled by anything the loop does.

    This does what :func:`asyncio.run` does at shutdown -- cancel every pending task,
    run the loop until they have finished, then join the executor -- and does it in the
    same place and for the same reason as :func:`_join_install_receipt_workers`: the
    ``tryfirst`` teardown hook runs before ANY fixture teardown, so the pins still hold.
    Both steps are bounded by ``_EXECUTOR_JOIN_SECS``; a task that will not finish or a
    job that will not return costs one slow teardown, never a hung worker.

    The executor is then made usable again. ``shutdown_default_executor`` latches the
    loop into refusing every later ``run_in_executor(None, ...)``, and an inner
    fixture's async teardown may still need one; clearing the latch and the pool
    reference lets such a call build a fresh executor, exactly as a never-used loop
    would. Both names are CPython's ``BaseEventLoop`` attributes, guarded so a loop
    implementation without them is simply left alone.
    """
    loop = getattr(item, "funcargs", {}).get("event_loop")
    if loop is None or not isinstance(loop, asyncio.AbstractEventLoop):
        return
    if loop.is_closed() or loop.is_running():
        return
    try:
        # Repeat until nothing is pending: a cancelled task's ``finally`` can start a
        # NEW task (a dashboard turn's queue cycle dispatches the next queued turn), and
        # a single pass over a snapshot never sees it. Left pending, ``loop.close()``
        # orphans it and the collector logs ``Task was destroyed but it is pending!``
        # into whichever later test is capturing logs then. One deadline
        # bounds every pass together, so a task that respawns on each cancel still
        # costs one slow teardown, never a hung worker.
        deadline = time.monotonic() + _EXECUTOR_JOIN_SECS
        while True:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.wait(pending, timeout=remaining))
        if getattr(loop, "_default_executor", None) is not None:
            loop.run_until_complete(loop.shutdown_default_executor(timeout=_EXECUTOR_JOIN_SECS))
            for name, reset in (("_executor_shutdown_called", False), ("_default_executor", None)):
                if hasattr(loop, name):
                    setattr(loop, name, reset)
    except Exception:  # pragma: no cover - a wedged loop must not turn teardown red
        return


# ── no test may leave a telemetry exporter running ────────────────────

#: Thread-name marker for OpenTelemetry's periodic metric exporter. Matched on the
#: name because the SDK is an optional import: naming the class would make this
#: guard depend on it being installed.
_OTEL_THREAD_MARKER = "Otel"


def _live_exporter_threads() -> set:
    """The exporter threads alive right now, as OBJECTS.

    Objects, not names: the SDK gives every ``PeriodicExportingMetricReader`` ticker
    the same name, so a name-keyed set makes a second leak indistinguishable from the
    first one still running.
    """
    return {t for t in threading.enumerate() if _OTEL_THREAD_MARKER in t.name and t.is_alive()}


def _stop_leaked_exporter(thread) -> None:
    """Best-effort: stop *thread* through the reader that owns it.

    The reader is unreachable from ``metrics.provider`` by this point -- a stubbed
    ``shutdown`` is exactly what orphans it -- but the thread's target is the
    reader's own bound method, so the thread still knows its owner. Reaching through
    ``__self__`` is the only handle left, and stopping the thread is what keeps one
    leak from reddening everything that follows it on this worker.

    A thread we cannot reach is WARNED about rather than passed over: it means the
    guard reported the leak and then left it running, which is the state that
    corrupts every later fork child on the worker, so it must not be silent.
    """
    owner = getattr(getattr(thread, "_target", None), "__self__", None)
    if owner is None:
        warnings.warn(
            f"telemetry exporter thread {thread.name!r} cannot be stopped: its target "
            "exposes no owning reader, so it will keep running for the rest of this "
            "worker and every later fork child will be multithreaded",
            stacklevel=2,
        )
        return
    try:
        owner.shutdown(timeout_millis=1)
    except Exception as exc:  # pragma: no cover - SDK-shape dependent
        warnings.warn(
            f"could not stop telemetry exporter thread {thread.name!r}: {exc!r}",
            stacklevel=2,
        )


@pytest.fixture(autouse=True)
def _no_leaked_telemetry_exporter():
    """Fail the test that leaves an OTel exporter thread alive, then clean up.

    A leaked ``PeriodicExportingMetricReader`` is not just an idle thread. The SDK
    registers an ``os.register_at_fork(after_in_child=...)`` hook that RESTARTS that
    thread in every fork child, so from then on the sandbox's userns probe forks a
    child that is multithreaded -- and ``unshare(CLONE_NEWUSER)`` implies
    ``CLONE_THREAD``, which the kernel refuses with EINVAL unless the caller is
    single-threaded. EINVAL is classified permanent, so the worker caches "this host
    has no sandbox backend" and every later sandboxed spawn fails closed. Measured:
    one leak reddened 19 tests across two app suites, all of which pass alone, and
    none of which is a metrics test.

    The thread also keeps its own reader alive (its target is a bound method), so no
    amount of dropping references clears it; only a real ``shutdown()`` does. That is
    why a test that observes the shutdown call must SPY on it and delegate, never
    replace it.

    Attributed by DIFFERENCE, not by observation: a thread alive at setup was left by
    an earlier test, so only threads that appeared during this one are reported. That
    is what makes the message name the culprit. The first version reported whatever
    test was running when a leak was first seen, which on a Windows shard blamed three
    tests in ``test_perf_boot_path.py`` that run every line of their subject in a
    SUBPROCESS and cannot leak a thread into the worker at all.

    Cleaning up after failing is deliberate: the leak damages every LATER test, so
    letting it stand would turn one defect into a red suite -- and with cleanup, the
    difference cannot double-report either.
    """
    before = _live_exporter_threads()
    yield
    leaked = _live_exporter_threads() - before
    if not leaked:
        return
    provider = sys.modules.get("kiro_crew.metrics.provider")
    if provider is not None:
        with contextlib.suppress(Exception):
            provider.reset_for_testing()
    for thread in leaked:
        _stop_leaked_exporter(thread)
    raise AssertionError(
        "this test STARTED a telemetry exporter thread and left it running: "
        f"{sorted(t.name for t in leaked)}. A metrics provider must be shut down for "
        "real (reset_for_testing()), and a test that stubs `shutdown` must delegate "
        "to it -- see "
        "conftest._no_leaked_telemetry_exporter."
    )


# ── the logging record factory goes back after every test ─────────────


@pytest.fixture(autouse=True)
def _restore_log_record_factory():
    """Put ``logging``'s record factory back, so one test cannot rewrite every later record.

    ``logging.setLogRecordFactory`` is ONE process-global slot. The wrapper the suite
    reaches -- ``log_redaction``'s, installed by ``cli._setup_cli_logging`` for a
    long-lived command -- is deliberately destructive to every record it then creates: it
    materializes ``msg`` and clears ``args`` so a handler cannot re-format, and it renders
    ``exc_info`` into ``exc_text`` and clears ``exc_info`` so a handler cannot re-render an
    unredacted traceback. Correct for redaction, and invisible to the test that leaves it
    installed, because the damage lands on whatever unrelated test later asserts on
    ``record.exc_info``, ``record.args``, or a deferred ``%s``.

    Measured: two tests failed this way in a release run and neither is a logging test --
    ``test_pid_lifecycle.py::TestFindOrphanMcpCandidates::
    test_unexpected_probe_error_keeps_traceback`` asserting a probe failure kept its
    traceback, and ``test_log_redaction`` itself hitting ``RecursionError`` because
    installing over the already-installed wrapper captured it as its own base factory.

    **Sharding hides this class, so the floor cannot rely on a full-suite run to find it.**
    CI assigns whole files to a shard before import on every platform, and a leak only
    damages tests in the SAME process, so PR CI
    usually cannot observe it at all; the
    release job runs the suite whole and is otherwise the first place it appears -- as
    failures in files unrelated to the cause, long after the diff merged. Restoring here
    removes the class outright rather than improving the odds of noticing it.

    Restoring rather than failing is deliberate, for the same reason the CWD restore in
    ``pytest_runtest_teardown`` (rootdir conftest) does not blame either: installing this factory is a
    legitimate, unavoidable side effect of every test that drives the real ``cli.main()``
    or ``_setup_cli_logging`` in-process, which production does once per process and never
    undoes. Blaming them would demand pure bookkeeping with no test value, and the damage
    is to OTHER tests, so stopping it propagating is the whole job.
    ``log_redaction.uninstall_log_redaction()`` is there for a test that wants to assert on
    the uninstalled state itself; the install/uninstall contract is pinned by
    ``test_log_redaction.py``, not by this floor.

    The restore target is what this test INHERITED, not a pristine ``LogRecord``. That is
    what lets a higher-scoped fixture install one for the whole class or module without
    this floor tearing it out from under the second test, and it is also why this is a
    fixture rather than the cheaper ``pytest_runtest_teardown`` hookimpl the CWD restore
    uses: a conftest ``pytest_runtest_setup`` runs BEFORE the item's own fixtures, so its
    snapshot would miss such an installer and the teardown would then rip it out after the
    class's FIRST test. Measured cost of the fixture protocol over the hookimpl: ~50us per
    test, ~3s of CPU across the suite, spread over the xdist workers.

    The BOUNDARY that follows from the same choice: a class-, module- or session-scoped
    fixture that installs a factory and never removes it leaks PAST its own scope, because
    every later test inherits it and so restores to it. No fixture in this repo does that
    -- every installer sits in a test body, which is inside this fixture's window -- but a
    new higher-scoped one has to uninstall itself; the floor cannot tell that case from a
    deliberate one.
    """
    before = logging.getLogRecordFactory()
    yield
    if logging.getLogRecordFactory() is not before:
        logging.setLogRecordFactory(before)


# ── the CLI log queue listener goes back after every test ────────────


@pytest.fixture(autouse=True)
def _restore_log_queue_listener():
    """Stop the ``QueueListener`` a test leaves running, so no later test inherits it.

    ``cli._LOG_QUEUE_LISTENER`` is ONE process-global slot, per worker, and the sibling of
    the record factory above: ``_setup_cli_logging`` installs BOTH in the same
    ``command in _LONG_LIVED_COMMANDS`` branch, so every test that drives the real
    ``cli.main()`` for ``serve`` / ``gateway`` / ``chat`` leaks both. The factory half has
    been floored since it was found; this half was not, and it is the one the SHORT-LIVED
    branch then trips over. ``_setup_cli_logging("status", ...)`` takes the ``else`` path,
    which correctly never touches the global -- the listener a long-lived command started
    genuinely still exists -- so a test asserting "a short-lived verb starts no listener"
    reads the PREVIOUS test's listener and fails at its own first line.

    Measured: two tests in ``test_cli_logging.py`` assert that global is ``None`` after a
    short-lived setup -- ``TestDrainBeforeHardExit::test_no_listener_is_a_silent_no_op``
    and ``TestQueueOffLoop::test_short_lived_command_keeps_sync_handler`` -- and both go
    red whenever ``test_cli_main_entry.py`` or ``test_cpp_seam_failclosed.py`` precede them on the
    worker, reading ``assert <QueueListener object ...> is None``. That file's own
    ``_pristine_logging`` cannot absorb it: it clears the global in TEARDOWN only, which
    makes the file self-clean but leaves it defenceless against a leak that is already
    present at its SETUP. Under ``-n auto --dist loadgroup`` which worker an ordinary test
    lands on varies run to run, so it surfaces as an intermittent failure rather than a
    reproducible ordering bug -- it once cost an upstream PR a red ``Backend Tests (3.10, 1)``
    shard while ``(3.12, 1)`` passed at the identical commit.

    STOPPING rather than only reassigning, which is where this differs from the factory:
    the leak is a live daemon thread holding the file handler's open descriptor on a
    ``gateway.log`` under a ``tmp_path`` the next test deletes, so dropping the reference
    alone would clear the assertion and keep the thread and the fd. ``cli`` is reached
    through ``sys.modules`` rather than imported, as ``_no_leaked_telemetry_exporter``
    does: a worker that never imported it cannot hold a listener, and importing it here
    would charge every testpath ~0.5s and ~54MB for the ratchet in
    ``test_cli_lazy_imports.py`` to then measure in a subprocess anyway.

    Restoring rather than blaming, for the same reason as ``_restore_log_record_factory``
    above: starting the listener is what the entry point under test is FOR, and production
    starts it once per process and never undoes it. The damage is to OTHER tests, so
    stopping it propagating is the whole job.

    HANDLERS stay untouched, the boundary ``_restore_logger_levels`` below draws and for
    its reasons. The ``_CliLogQueueHandler`` left on the ``kiro_crew`` logger therefore
    outlives the listener it fed, holding an unattended queue; it is the same handler
    accumulation that fixture already records as a separate defect, and
    ``test_cli_logging.py``'s ``_pristine_logging`` is what absorbs it today by clearing
    both handler lists at setup.

    The restore target is what the test INHERITED, so a higher-scoped fixture that starts
    a listener for a whole class or module is not torn out from under its second test --
    and, as there, such a fixture has to stop its own listener, because every later test
    then inherits it and so restores to it.
    """
    cli = sys.modules.get("kiro_crew.cli")
    before = getattr(cli, "_LOG_QUEUE_LISTENER", None)
    yield
    cli = sys.modules.get("kiro_crew.cli")
    if cli is None:
        return
    after = getattr(cli, "_LOG_QUEUE_LISTENER", None)
    if after is before:
        return
    if after is not None:
        # Drains and joins the listener thread, then closes the file handler. Suppressed
        # because a floor must not fail a test for state it is only cleaning up, and the
        # restore below has to happen either way.
        with contextlib.suppress(Exception):
            cli._stop_log_queue_listener()
    cli._LOG_QUEUE_LISTENER = before


# ── the hooks system's process-wide dispatcher goes back after every test ──


@pytest.fixture(autouse=True)
def _restore_hooks_integration_globals():
    """Put ``hooks_integration._lifecycle_dispatcher`` / ``_route_registry`` back.

    ``init_hooks_system`` -- which every test that builds the real dashboard app
    reaches through ``server.py`` -- assigns BOTH module globals and nothing in
    production ever clears them: a gateway sets them once at boot. In a worker they
    therefore carry the LAST such test's ``LifecycleDispatcher`` into every later
    test, together with whatever that test passed as ``cron_service`` -- routinely
    a ``MagicMock``. The trust-revoke teardown (``teardown_app_runtime`` ->
    ``on_app_disable`` -> ``_cleanup_app_crons``) reads that global and awaits the
    stale mock's cron store, gets ``object MagicMock can't be used in 'await'
    expression``, and reports ``hooks disable failed`` -- so the route answers 409
    ``teardown_incomplete`` for an app whose teardown had nothing to do.

    Measured on a five-run hygiene sweep: seven to nine of
    ``test_trusted_apps_api.py``'s revoke tests were red in EVERY round with that
    body, a different subset each round, and all of them pass alone -- the file
    is a victim, not the leak. Reproduced by replaying one worker's 1,874 files
    in order at ``-n0`` with a debug hook on the handler, which named the stale
    dispatcher and its ``MagicMock`` cron service. Restored rather than blamed,
    like the log-record factory above: the assignment is production's, the
    tests that trigger it are exercising real boot code, and any of ~170 files
    that build the app can be the one that lands before the victim. Reached
    through ``sys.modules`` so a worker that never imported the module pays
    nothing and no import is charged to the lazy-import ratchets.
    """
    hi = sys.modules.get("kiro_crew.apps.hooks_integration")
    before = None
    if hi is not None:
        before = (
            getattr(hi, "_lifecycle_dispatcher", None),
            getattr(hi, "_route_registry", None),
        )
    yield
    hi = sys.modules.get("kiro_crew.apps.hooks_integration")
    if hi is None:
        return
    if before is None:
        # Imported DURING the test: whatever it set is the test's, and the module
        # started life with both slots empty.
        before = (None, None)
    hi._lifecycle_dispatcher, hi._route_registry = before


# ── logger levels go back after every test ──────────────────────────


#: What a logger nobody has configured looks like. ``logging.getLogger(name)`` builds
#: exactly this, so a name MISSING from the "before" snapshot restores to it rather than
#: being passed over -- otherwise a test that creates a logger and configures it leaks
#: through the one gap a snapshot cannot see.
_PRISTINE_LOGGER: tuple[int, bool] = (logging.NOTSET, False)


def _logger_levels() -> dict[str, tuple[int, bool]]:
    """``(level, disabled)`` for the root logger and every logger by name.

    ``loggerDict`` also holds ``PlaceHolder`` entries for the un-instantiated middle of a
    dotted name; those carry no level and are skipped. The root logger is not in it at
    all, so it is added under ``""`` -- the name ``logging.getLogger`` maps back to it.
    """
    snapshot: dict[str, tuple[int, bool]] = {
        name: (obj.level, obj.disabled)
        for name, obj in list(logging.Logger.manager.loggerDict.items())
        if isinstance(obj, logging.Logger)
    }
    root = logging.getLogger()
    snapshot[""] = (root.level, root.disabled)
    return snapshot


@pytest.fixture(autouse=True)
def _restore_logger_levels():
    """Put every logger's level and ``disabled`` flag back after each test.

    A level is PROCESS-GLOBAL and HIERARCHICAL, which together are what make a leak here
    so hard to attribute: ``Logger.debug`` checks the EFFECTIVE level, so an explicit
    level left on ``kiro_crew`` decides what every ``kiro_crew.*`` logger in the worker
    may emit, and it outranks the root level ``caplog.at_level()`` sets. The victim then
    sees ``caplog.text == ""`` -- not the wrong text, NOTHING -- from a test that passes
    alone, in a file that has nothing to do with the cause.

    Measured: ``test_slack_gateway_more_coverage.py::TestDeliverCronResponse::
    test_options_post_failure_still_delivers_text`` asserts on a ``logger.debug`` line and
    reds whenever ``test_cli_cron.py::TestCronCli::test_cli_argparse_cron_add_agent_flag``
    shares its worker -- an ARGPARSE test, in a file with no connection to Slack. It
    drives the real ``cli.main()``, whose ``_setup_cli_logging`` pins ``kiro_crew`` at
    WARNING, exactly as production does once per process and never undoes. Test modules
    across the suite drive ``main()`` that way.

    Restoring rather than blaming, for the same reason as ``_restore_log_record_factory``
    above: configuring logging is what the entry point under test is FOR, so demanding
    per-module bookkeeping from every test that reaches it buys no coverage and is one
    forgotten fixture away from reappearing. The damage is to OTHER tests, so stopping it
    propagating is the whole job.

    **HANDLERS are deliberately not restored, and the boundary is not squeamishness.** A
    handler is routinely paired with a module-global that records it as installed --
    ``dashboard.handlers.updates._log_ring_handler_installed`` is the live example -- and
    a floor can detach the handler but cannot know to clear the flag. That leaves the
    module in a state neither a test nor production can otherwise reach: the singleton
    reports installed while nothing is attached, so the next caller is handed a handler
    that receives nothing. Restoring the ROOT logger's handler list is unsafe for a
    second, independent reason: pytest's ``catching_logs`` adds one per test PHASE and
    removes it at the phase boundary, so a list snapshotted during setup would be written
    back during teardown, re-attaching the setup phase's handler and dropping the one the
    teardown phase is capturing through.

    The handlers ``_setup_cli_logging`` leaves on ``kiro_crew`` do accumulate -- each open
    on a ``gateway.log`` under a ``tmp_path`` the next test deletes -- but that is a
    separate defect from this one, and it is not what empties ``caplog``.
    ``test_cli_logging.py``'s own ``_pristine_logging`` fixture is what absorbs it today,
    by clearing both handler lists at setup rather than by trusting its inheritance.

    Measured cost: ~30us per snapshot at ~450 live loggers, so ~60us per test.
    """
    before_disable = logging.Logger.manager.disable
    before = _logger_levels()
    yield
    for name, after in _logger_levels().items():
        level, disabled = before.get(name, _PRISTINE_LOGGER)
        if after == (level, disabled):
            continue
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.disabled = disabled
    if logging.Logger.manager.disable != before_disable:
        logging.disable(before_disable)


def _drain_windows_proactor_finalizers() -> None:
    """Suppress 'Event loop is closed' from ProactorEventLoop transport finalizers.

    On Windows with Python 3.12+, ``asyncio.run()`` creates a ProactorEventLoop,
    runs the coroutine, then CLOSES the loop. The IocpProactor's transport objects
    store a reference to their own loop (``self._loop``). When those transports are
    garbage-collected later, their ``__del__`` methods call
    ``self._loop.call_soon()`` on the *original* closed loop — not whatever loop is
    currently set. This raises ``RuntimeError: Event loop is closed`` as an
    unraisable exception.

    On an xdist worker process, unraisable exceptions write to stderr and cause
    exit code 1 — failing the CI shard with no named test failure.

    The fix: install a ``sys.unraisablehook`` that silences ``RuntimeError: Event
    loop is closed`` from asyncio transport ``__del__`` methods, and an ``atexit``
    handler that forces GC while the hook is still active to drain pending
    finalizers before interpreter shutdown (where unraisablehook itself may be
    torn down).
    """
    _original_unraisablehook = sys.unraisablehook

    def _suppress_closed_loop(unraisable: sys.UnraisableHookArgs) -> None:
        """Suppress 'Event loop is closed' from transport __del__."""
        exc = unraisable.exc_value
        if isinstance(exc, RuntimeError) and str(exc) == "Event loop is closed":
            # Silently swallow — the transport is being finalized after its loop
            # closed, which is harmless (the I/O is already done).
            return
        # Everything else goes to the original hook.
        if _original_unraisablehook is not None:
            _original_unraisablehook(unraisable)
        else:
            sys.__unraisablehook__(unraisable)

    sys.unraisablehook = _suppress_closed_loop

    # Force GC while the hook is active to drain finalizers before interpreter
    # shutdown tears down sys.unraisablehook itself.
    gc.collect()
    gc.collect()

    def _final_gc_pass() -> None:
        """Last-resort GC at interpreter exit while the hook is still installed."""
        try:
            gc.collect()
        except (TypeError, AttributeError):
            pass

    atexit.register(_final_gc_pass)
