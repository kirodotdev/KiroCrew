"""Per-test resets and pins of process-global ``kiro_crew`` state, plus the
fences that keep the suite off live network endpoints.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own and ``from conftest import ...`` keeps working;
see the import block there. Plain module, never collected.
"""

from __future__ import annotations

import asyncio
import sys
import warnings

import pytest
from conftest_opt_in import drain_breadcrumb_writes

from kiro_crew.safety_override import reset_singleton as _reset_safety_override
from kiro_crew.safety_override import reset_yolo_policy_state as _reset_yolo_policy_state


@pytest.fixture(autouse=True)
def _small_default_host(_floor_monkeypatch):
    """Size every ``"auto"`` start limit from a fixed small host, not this machine.

    ``session_start_sizing`` reads the host once per process and caches it, so
    without a pin the first test on a worker to resolve ``"auto"`` decides the
    start widths every later test on that worker sees: 16 on a 128-core
    workstation, 2 on a CI runner. Tests that build a default ``SessionManager``
    and count its permits would then pass or fail by machine and by order. The
    pinned host sizes ``session_start_concurrency`` to 2, so the cold-start
    queues get their historical 4 / 2. A test about sizing overrides this.

    It also clears the runtime spawn width a ``SessionManager`` publishes for the
    process, so a manager one test built does not size another test's admission.
    """
    from kiro_crew import cold_start_sizing, session_start_sizing

    _floor_monkeypatch.setattr(cold_start_sizing, "_configured_runtime_spawn_width", None)

    _floor_monkeypatch.setattr(
        session_start_sizing,
        "_host_cached",
        session_start_sizing.HostCapacity(
            affinity_cpus=8, quota_cpus=None, cpus=8, available_gb=16.0
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_reexec_environment(_floor_monkeypatch):
    """Start every test with nothing kept for an exec successor.

    Consuming the managed-service launch marker keeps it in
    ``platform_compat._KEPT_FOR_REEXEC`` for the gateway's own exec restart, and
    ``launched_as_managed_service`` reads it there; a value one test's launch
    consumed must not make a later test's gateway read as service-launched, nor
    be written into the environment by a later test's stubbed exec.
    """
    from kiro_crew import platform_compat

    _floor_monkeypatch.setattr(platform_compat, "_KEPT_FOR_REEXEC", {})


@pytest.fixture(autouse=True)
def _approve_every_mcp_launch(request, _floor_monkeypatch):
    """Treat every gatewayd launch as operator-approved, except where it is the subject.

    gatewayd refuses a target command or declared env the operator has not
    approved (``mcp_gateway.launch_approval``), and a hermetic home has no
    approval store, so every resolver and forwarding test would otherwise
    exercise the refusal instead of the behaviour it pins. The stub toggle and
    seeding resolve the launch they approve from the agent specs, which a
    hermetic home does not carry either, so each name resolves to one stand-in
    launch. A module that tests the approval itself sets
    ``ENFORCE_LAUNCH_APPROVAL = True``.
    """
    if getattr(request.module, "ENFORCE_LAUNCH_APPROVAL", False):
        return
    try:
        from kiro_crew.mcp_gateway import launch_approval, launch_resolve
    except ImportError:
        return
    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(launch_approval, "launch_approved", lambda *_a, **_k: True)

    def _stand_in_launches(names, **_kwargs):
        env_hash = launch_approval.env_fingerprint({})
        return {
            n: [
                launch_approval.ResolvedLaunch(
                    launch_approval.launch_fingerprint(n, []),
                    n,
                    (),
                    frozenset({env_hash}),
                )
            ]
            for n in names
        }

    monkeypatch.setattr(launch_resolve, "resolve_launches", _stand_in_launches)


@pytest.fixture(autouse=True, scope="module")
def _release_source_corpus_after_module():
    """Drop ``test/source_corpus.py``'s cached file list at every module's teardown.

    The corpus helper streams file text (read, normalise, filter, parse, drop --
    one file live at a time) and memoizes only the sorted path list of the tree
    (a megabyte of ``Path`` objects), so what this hook releases is small. It
    stays because that list is an ``lru_cache`` global that would otherwise live
    for the rest of the xdist worker, and a module that plants a file under
    ``src/`` to prove its ratchet still fails needs the next module to re-walk.
    Module scope keeps the sharing the ratchets rely on while bounding the
    retention to the module that needed it. Import is deferred and tolerant so a
    module that never touches the corpus pays nothing.
    """
    yield
    try:
        from source_corpus import _clear_caches
    except ImportError:  # pragma: no cover - a partial checkout without the helper
        return
    _clear_caches()


@pytest.fixture(autouse=True)
def _drop_live_config_snapshot():
    """Give every test an unstarted process watcher, and leave none behind.

    ``kiro_crew.config.live`` keeps ONE process-global watcher, and every
    point-of-use reader (``SkillsLoader._max_triggered_now``, the channel
    dispatchers' ``_live_cfg``) prefers its snapshot over the config it was
    constructed with. A test that primes it and does not reset therefore sets the
    live config for every later test on the same xdist worker -- measured as a
    ``max_triggered`` of 0 leaking into ``test_explain_for_skill`` from an
    unrelated module. The reset is a lock and a ``None`` store, so it costs the
    ~57k tests that never prime nothing measurable.

    Reset on BOTH sides, so a test asserting on the subscription registry starts
    from an empty one whatever ran before it on this worker -- the registry is
    process-global too, and an entry another test left in it is indistinguishable
    from one the test under way registered.
    """
    from kiro_crew.config import live

    live.reset_for_tests()
    yield
    live.reset_for_tests()


@pytest.fixture(autouse=True)
def _inline_taskq_pump(_floor_monkeypatch):
    """Run the subagent pump and the store open inline for the suite.

    In production the pump is a coroutine whose store reads run on the task
    store's writer thread, and a manager built on a running loop opens its
    store on a worker; the suite's harnesses settle with ``sleep(0)`` loops
    and virtual clocks, and construct a manager and spawn on the next line,
    which cannot wait for a thread hop. Both off-loop paths are pinned by their
    own tests, which turn the switches back on.
    """
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    _floor_monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", False)
    _floor_monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", False)


@pytest.fixture(autouse=True)
def _release_stt_engine():
    """Never let a loaded speech model outlive the test that loaded it.

    ``kiro_crew.stt.engine`` keeps ONE recogniser per process on purpose: the
    whole point of the module is that an utterance does not pay for a model load.
    Under xdist that same property makes it cross-test state, and the instance
    carries the idle-eviction window the first caller passed, so a later test
    reading a different setting would silently get the earlier one.
    """
    yield
    from kiro_crew.stt import engine as stt_engine

    stt_engine._engine = None


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking.

    The pushed ``approval_modes`` verdict is reset WITH it, because it is the same
    leak wearing different clothes. That verdict is a module-level flag resolved when
    a platform context is installed, and this suite installs contexts constantly
    (~30 files call ``set_context``/``reset_context``). A DENY pushed by an earlier
    test therefore keeps refusing yolo arms in a later one that never configured a
    policy, which surfaces as INTERMITTENT failures in files that never touch
    governance — which tests share an xdist worker decides whether the stale flag is
    present. Resetting it here makes the next reader resolve the ceiling actually
    installed.
    """
    _reset_safety_override()
    _reset_yolo_policy_state()
    yield
    # Drained BEFORE the reset below, and (by pytest's fixture teardown order --
    # finalizers run in reverse of setup order, so a fixture set up AFTER this
    # one, e.g. a test's own ``monkeypatch.setenv("KIROCREW_HOME", ...)``, tears
    # down BEFORE this line runs) while any KIROCREW_HOME the test itself set is
    # still in effect. A publish enqueued mid-test resolves ``config_dir()`` on
    # the CALLING thread at enqueue time (see ``_sync_breadcrumb``), but the
    # worker that runs the write is on its own thread and can still be
    # mid-flight when the test function returns. Waiting here for that worker to
    # finish, before this fixture's own KIROCREW_HOME-independent state reset,
    # closes the window that let a delayed write land on the real operator home
    # instead of the test's temp dir (found in review).
    drain_breadcrumb_writes()
    _reset_safety_override()
    _reset_yolo_policy_state()


@pytest.fixture(autouse=True)
def _reset_degraded_config_observations():
    """Forget the loader's process-sticky malformed-config observations.

    ``kiro_crew.config.loader`` remembers every malformed config section it has
    ever seen for the LIFE OF THE PROCESS (deliberately: ``load()``'s migration
    repairs the file on first read, so the observation is the only surviving
    evidence, and the publish gate fails closed on it). Tests share one
    interpreter, so without this reset any test that writes a malformed
    ``config.json`` — loader error-path tests do — makes the publish/deploy
    gate deny in every LATER test in the same worker, failing tests in files
    that never touched the config (seen as ``TestPending`` 4xx refusals in
    ``test_deploy_handlers_coverage.py`` under random orderings).

    ``reset_degraded_observations`` documents tests as its only legitimate
    caller; this fixture is that caller.
    """
    from kiro_crew.config.loader import reset_degraded_observations

    reset_degraded_observations()
    yield
    reset_degraded_observations()


@pytest.fixture(autouse=True)
def _reset_channel_turn_ceiling():
    """Forget the channel turn ceiling's process-global per-conversation counts.

    ``kiro_crew.messaging.turn_ceiling`` keeps ONE counter for the whole process
    (``_SHARED``), keyed by session key, so a counted turn outlives the test that
    drove it. Tests share one interpreter and the Slack, Telegram and Discord
    inbound routes all drive the same handful of session keys, so a worker that
    runs more gated channel turns under one key than the ceiling allows latches
    that conversation for the rest of the worker: every LATER test on the same key
    gets a refused turn instead of the behaviour it asserts, in files that never
    mention the ceiling. A wide shard reaches the default of 90 and reds the
    native-route turn-verdict tests in ``test_slack_handler_turn_outcomes.py``,
    which then book neither success nor failure because the handler returns at
    the gate.

    The notification sink is module-global for the same reason and is cleared with
    it, so a test that registers an observer cannot be heard by the next one.
    """
    from kiro_crew.messaging.turn_ceiling import set_notification_sink, shared_ceiling

    shared_ceiling().clear()
    set_notification_sink(None)
    yield
    shared_ceiling().clear()
    set_notification_sink(None)


@pytest.fixture(autouse=True)
def _restore_autonudge_singleton():
    """Floor under ``autonudge._INSTANCE`` — the process-global service reference.

    ``AutoNudgeService.start()`` publishes itself here and ``stop()`` clears it, so a test
    that starts the service (or drives a dashboard handler that does) leaves a live
    instance behind, holding timer TASKS created on that test's event loop. Every later
    test in the same worker then reaches those tasks through the singleton, on a loop that
    has since closed — which is how `test_dashboard_chat_session_authority.py`'s
    ``TestCloseBroadcastDurability`` came to answer 500 with no production-code change,
    from a leak in a file that has nothing to do with it.

    Restores what the test INHERITED rather than a pristine ``None``, so a leak from an
    earlier test is not re-reported against every test after it. Restores silently rather
    than failing: production really does publish this singleton, and a test driving that
    code cannot avoid inheriting it — the damage is to other tests, and stopping it
    propagating is the part that is never optional.

    Retiring the leaked instance's timers goes through ``_cancel_timer``, which is the one
    place that knows a task on a closed loop must be dropped rather than cancelled.
    """
    from kiro_crew import autonudge as _an

    inherited = _an._INSTANCE
    try:
        yield
    finally:
        leaked = _an._INSTANCE
        if leaked is not None and leaked is not inherited:
            for loop_id in list(getattr(leaked, "_timers", {})):
                try:
                    leaked._cancel_timer(loop_id)
                except Exception:  # noqa: BLE001 - teardown must not mask the test result
                    pass
        _an._INSTANCE = inherited


@pytest.fixture(autouse=True)
def _reset_cjk_segmenter():
    """Forget session search's registered Chinese segmenter around each test.

    ``history_search.register_cjk_segmenter`` fills one process-global slot, and
    the gateway boot fills it (``_init_services``). Tests share one interpreter, so
    a test that boots the gateway would leave every LATER test in the worker parsing
    Chinese queries into dictionary words, and the character-path tests would red
    depending on which tests shared the worker. Read from ``sys.modules`` so a test
    that never imports search pays nothing.
    """
    module = sys.modules.get("kiro_crew.history_search")
    if module is not None:
        module.reset_cjk_segmenter_for_tests()
    yield
    module = sys.modules.get("kiro_crew.history_search")
    if module is not None:
        module.reset_cjk_segmenter_for_tests()


@pytest.fixture(autouse=True)
def _disable_dev_fleet_background_tasks(_floor_monkeypatch):
    """Stop dev-fleet's app-startup hook from starting its background loops.

    A test that boots the real app via ``dev_fleet.server.create_app()`` (to
    exercise middleware, for instance) otherwise starts ``_status_refresher``,
    a genuine network ``git fetch``, as a fire-and-forget task. That task can
    still be running when the test's client tears down, and cancelling it then
    is what leaked into unrelated tests and flaked the macOS backend job. A
    test that wants the real refresher overrides this itself
    via ``monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: False)``.
    """
    _floor_monkeypatch.setenv("KIROCREW_DEVFLEET_NO_BACKGROUND", "1")


@pytest.fixture(autouse=True)
def _no_restart_ready_timeout_override(_floor_monkeypatch):
    """Keep a developer's ``KIROCREW_RESTART_READY_TIMEOUT`` out of every test.

    ``kirocrew restart`` reads it on every call, and several test files drive
    ``cli_server._restart`` directly.
    """
    _floor_monkeypatch.delenv("KIROCREW_RESTART_READY_TIMEOUT", raising=False)


@pytest.fixture(autouse=True)
def _no_managed_venv_override(_floor_monkeypatch):
    """Keep a developer's ``KIROCREW_VENV`` out of every test.

    ``wheel_engine.managed_venv_layout`` reads it first, so an exported value
    would make a test that takes the real update lock create and hold
    ``${KIROCREW_VENV}.update.lock`` beside the operator's managed venv, racing
    that install's own updates. Without it the layout sits beside the test's
    scratch ``KIROCREW_HOME``. A test that needs a layout sets the variable itself.
    """
    _floor_monkeypatch.delenv("KIROCREW_VENV", raising=False)


@pytest.fixture(autouse=True)
def _isolate_kiro_window_cache():
    """Give every test an EMPTY ``model_registry._KIRO_WINDOWS``, then restore it.

    The kiro-list window cache is process-global module state with two ways to
    couple tests:

    * **Test-to-test leak** — a test that exercises ``/api/models`` (which calls
      ``refresh_kiro_windows``) or seeds the cache directly would otherwise leave
      entries behind, e.g. a GPT window seeded here makes a "non-registry model
      is unknown" test in another module wrongly see GPT as known.
    * **Import-time host leak** — ``model_registry`` calls ``_load_kiro_windows()``
      at import, which reads ``<config_dir>/model_windows.json``. On a developer
      box that file holds the operator's real cached windows (e.g. a locally
      served ``deepseek-3.2`` at a non-registry value), so a test asserting the
      static supplementary floor for that same id fails ONLY on that machine —
      green in CI (no such file), red locally. Snapshotting-then-restoring alone
      preserved that polluted baseline for the duration of each test body.

    Clearing before the test (and restoring the original snapshot after) makes
    every test start from the same empty cache regardless of what the host had on
    disk — so a local run matches CI. Tests that need entries seed them in their
    own body. The operator-declared windows (``_OPERATOR_WINDOWS``) get the same
    treatment, so a declaration one test applies never reaches the next.
    """
    import kiro_crew.model_registry as _mr

    saved = dict(_mr._KIRO_WINDOWS)
    saved_operator = _mr._OPERATOR_WINDOWS
    _mr._KIRO_WINDOWS.clear()
    _mr._OPERATOR_WINDOWS = {}
    try:
        yield
    finally:
        _mr._KIRO_WINDOWS.clear()
        _mr._KIRO_WINDOWS.update(saved)
        _mr._OPERATOR_WINDOWS = saved_operator


@pytest.fixture(autouse=True)
def _history_cleanup_throttle_closed(_floor_monkeypatch):
    """Keep ``history``'s hourly retention sweep closed unless a test opens it.

    The sweep runs on the first archive write after ``_last_cleanup`` is an hour old,
    and the module starts it at ``0.0``, so on each worker only the FIRST test that
    archived anything also expired sessions under whatever retention its home
    resolved: an order-dependent side effect. Pinned to infinity the throttle is
    always closed; a test of the sweep opens it with ``monkeypatch.setattr(history,
    "_last_cleanup", 0.0)`` and wins, and a raw assignment is undone after the test.
    """
    import math

    from kiro_crew import history

    _floor_monkeypatch.setattr(history, "_last_cleanup", math.inf)


@pytest.fixture(autouse=True)
def _isolate_advertised_model_cache(_floor_monkeypatch):
    """Keep one session's advertised model spellings inside its own test."""
    from kiro_crew import model_registry

    _floor_monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})


@pytest.fixture(autouse=True)
def _isolate_display_verdicts():
    """Turn ``display_verdicts`` persistence off after every test.

    Any test that boots the dashboard's startup hooks turns it on for the whole
    process, and nothing turns it off. A later test in the same worker then gets
    the persistent MAC key instead of the per-process salt, and the cache-key
    tests in ``test_chat_utils`` fail depending on test order.
    """
    try:
        yield
    finally:
        from kiro_crew.dashboard import display_verdicts as _dv

        _dv.disable_for_tests()


@pytest.fixture(autouse=True)
def _disarm_agent_slice_memory_high():
    """Disarm the agent-slice ``MemoryHigh`` reconcile for every test.

    ``cgroup_scope_argv`` reconciles ``MemoryHigh`` on the shared agent slice
    via a real ``systemctl --user set-property`` before wrapping a spawn. On a
    Linux host WITH cgroup delegation the probe passes for real, so any test
    that reaches ``cgroup_scope_argv`` (spawn-audit, the real pids.max
    enforcement test, integration paths) would mutate the developer's live
    user manager — exactly the class of side effect the root conftest's
    host-service guard refuses (``set-property`` is a mutating verb), turning
    those tests into guard failures. Pre-disarm via the module's own kill
    switch and restore all four state globals after, so tests of the
    reconciler itself can re-arm explicitly in their own body.
    """
    import kiro_crew.sandbox as _sb

    saved_disabled = _sb._SLICE_MEMHIGH_DISABLED
    saved_applied = _sb._SLICE_MEMHIGH_APPLIED
    saved_events_seen = _sb._SLICE_MEMHIGH_EVENTS_SEEN
    saved_climb_warned = _sb._SLICE_MEMHIGH_CLIMB_WARNED
    _sb._SLICE_MEMHIGH_DISABLED = True
    try:
        yield
    finally:
        _sb._SLICE_MEMHIGH_DISABLED = saved_disabled
        _sb._SLICE_MEMHIGH_APPLIED = saved_applied
        _sb._SLICE_MEMHIGH_EVENTS_SEEN = saved_events_seen
        _sb._SLICE_MEMHIGH_CLIMB_WARNED = saved_climb_warned


@pytest.fixture(autouse=True)
def _reset_live_execution_records():
    """A reused temporary home must not inherit another test's live records."""

    def clear():
        for name, attribute in (
            ("kiro_crew.execution_context", "_LIVE_EXECUTIONS"),
            ("kiro_crew.execution_context", "_VOUCHED_EXECUTIONS"),
            ("kiro_crew.subagent_persistence", "_LIVE_RUN_STATES"),
        ):
            module = sys.modules.get(name)
            if module is not None:
                getattr(module, attribute).clear()
        # The overflow throttle is scalar process state, not a container, so it
        # needs its own reset. A test that leaves the flag ARMED makes the next
        # test's first episode silent, which reads as a missing log line rather
        # than as leaked state.
        execution = sys.modules.get("kiro_crew.execution_context")
        if execution is not None:
            execution._vouched_overflow_reported = False
            execution._vouched_overflow_count = 0

    clear()
    try:
        yield
    finally:
        clear()


@pytest.fixture(autouse=True)
def _reset_runtime_ownership_tables(request):
    """A kill gate must answer for THIS test's leases, not a neighbour's.

    ``runtime_ownership`` keeps one process-wide lease table and one tenancy
    table -- there is one registry per gateway -- and the kill gate refuses any
    pid found in either. Test doubles reuse a handful of pids (``4242`` appears
    in about two hundred files), so a lease or tenancy left behind by an
    earlier test on the same xdist worker makes a later reaper test read
    ``runtime still leased by another tenant`` and ``outcome == "refused"`` in
    place of the failed-kill wording it asserts. Measured under a macOS
    sweep as three same-worker reds in one run (``test_cron_reaper``,
    ``test_subagent_force_stop_audit`` x2) that passed in the other four.
    Reset on both sides, like the per-module resets those suites already carry,
    so a test that raises mid-lease cannot hand its pid to the next one.

    A file whose last test READS the table to prove the file left it clean -- the
    positive control beside ``test_runtime_reconcile_tenancy``'s leak scan -- must see the
    table as its own tests left it, or that read passes vacuously against a table
    the predecessor's teardown wiped. Such a file opts out MODULE-WIDE with
    ``pytestmark = pytest.mark.keep_runtime_ownership_tables`` and carries its own
    intra-file resets; marking the reading test alone would not do, since the
    wipe that matters is the one at the previous test's teardown.
    """
    if request.node.get_closest_marker("keep_runtime_ownership_tables") is not None:
        yield
        return

    def clear():
        module = sys.modules.get("kiro_crew.runtime_ownership")
        if module is not None:
            module._reset_for_tests()

    clear()
    try:
        yield
    finally:
        clear()


@pytest.fixture(autouse=True)
def _reset_session_switch_locks(_floor_monkeypatch):
    """Tests reuse session keys across loops; the gateway has one serving loop."""
    import weakref

    from kiro_crew import llm_helpers

    _floor_monkeypatch.setattr(
        llm_helpers, "_slot_switch_session_locks", weakref.WeakValueDictionary()
    )


@pytest.fixture(scope="session", autouse=True)
def _shut_down_shared_pools_in_session():
    """Reap the lazily created shared executors before the session ends.

    ``executors.path_resolve_executor()`` is a process-lifetime
    ``SubprocessPoolExecutor`` created by whichever test first resolves a path
    after the previous teardown; its reaper thread (``mc-pathres-reaper``) spawns
    two children that live until ``atexit``. A sweep attributed
    those children to 73 unrelated tests across 17 files as ``leaked_child``,
    because the pool's own shutdown ran after every per-test observation. Closing
    it here puts the reap inside the session, where the leak reporting can see
    it, and leaves nothing for ``atexit`` to do.
    """
    yield
    from kiro_crew import executors as _executors

    _executors.shutdown_maintenance_executor()


@pytest.fixture(autouse=True)
def _no_boot_sandbox_sweep(_floor_monkeypatch):
    """A real ``SessionManager`` must not sweep the host's sandbox profiles from a test.

    ``SessionManager.get_or_create`` arms the cleanup loop, whose boot reclaim
    submits ``kiro_crew.session.cleanup_stale_sandbox_profiles`` -- the REAL
    ``/proc`` pin scan over the data home -- to the shared ``mc-maint`` executor.
    ``close_all()`` cancels the asyncio task but cannot stop the executor
    thread, so on a loaded xdist worker the scan finishes seconds later, inside
    whichever test is then running, and logs at WARNING on
    ``kiro_crew.sandbox`` (the tenth sweep caught it as a second record in an
    unrelated test's ``caplog``). The seam is the name the session module
    rebinds at import, so the module is imported here rather than looked up in
    ``sys.modules``: a guard on "already loaded" would let the first test on a
    worker that imports the session module inside its body reach the real
    sweep. This conftest's own imports already load it, so the import costs
    nothing extra. A test of the sweep itself patches the same name inside its
    body (``TestCleanupLoop``) and so overrides this.
    """
    from kiro_crew import session as session_mod

    _floor_monkeypatch.setattr(session_mod, "cleanup_stale_sandbox_profiles", lambda *a, **kw: 0)


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure a USABLE (open) event loop exists for tests that call
    ``asyncio.get_event_loop().run_until_complete(...)`` (e.g. test_knowledge).

    Two failure modes this guards, both seen on the loaded CI farm under xdist:
      * no current loop set (``RuntimeError``) — Python 3.9 Semaphore default_factory; and
      * a current loop that is set but CLOSED — left behind by a prior test in the same
        worker that ran ``asyncio.run(...)`` (which on 3.12 closes its loop at teardown).
        ``get_event_loop()`` returns that closed loop WITHOUT raising, so the next
        ``run_until_complete`` blows up with ``RuntimeError: Event loop is closed``. We
        detect a closed/absent loop and install a fresh open one so each test starts clean.
    """
    created_loop = None
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            created_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(created_loop)
    except RuntimeError:
        created_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(created_loop)

    yield

    # ``asyncio.run`` clears the policy's current loop, so the next test needs a
    # replacement.  On Windows each ProactorEventLoop owns a socketpair; leaving
    # every replacement open eventually exhausts the ephemeral-port range and
    # makes ``new_event_loop()`` hang until pytest kills every xdist worker.
    if created_loop is not None and not created_loop.is_closed():
        created_loop.close()
    try:
        if asyncio.get_event_loop() is created_loop:
            asyncio.set_event_loop(None)
    except RuntimeError:
        pass


@pytest.fixture(autouse=True)
def _restore_default_child_watcher():
    """Restore a FRESH ThreadedChildWatcher after every test.

    Some tests install a real, non-default asyncio child watcher via the
    gateway's ``_install_child_watcher()`` -- notably
    ``test_cli_main_entry.py::test_real_subprocess_works_after_install_on_linux``, which on
    Linux installs a ``PidfdChildWatcher`` and runs ``asyncio.run``. On exit,
    ``asyncio.run`` detaches the watcher's loop, leaving a loop-less watcher in
    the global policy. Two distinct failures follow from that leak, and which
    one bites depends purely on xdist sharding, so adding or removing unrelated
    tests can flip a green run red with no production-code change:

    * On 3.10 the leaked watcher's ``is_active()`` is False, so the NEXT
      subprocess-spawning test fails with "asyncio.get_child_watcher() is not
      activated, subprocess support is not available".
    * On 3.12 the leaked watcher is still ATTACHED to callbacks bound to a loop
      that later closes. ``set_event_loop`` calls ``watcher.attach_loop()``,
      which reaps already-exited children and fires their callbacks -- against
      the closed loop -- raising ``RuntimeError: Event loop is closed``. Since
      pytest-asyncio calls ``set_event_loop`` when setting up every test that
      needs a loop, ONE leaked watcher fails every later test in that worker.

    The condition is therefore derived from whether the watcher API EXISTS, not
    from a version number: child watchers were only DEPRECATED in 3.12 and are
    removed in 3.14. A previous ``sys.version_info >= (3, 12): return`` guard
    skipped this cleanup on exactly the version where the second failure mode
    lives, which is what turned one leaked watcher into thousands of cascading
    failures in a full parallel run.
    """
    yield
    get_watcher = getattr(asyncio, "get_child_watcher", None)
    set_watcher = getattr(asyncio, "set_child_watcher", None)
    threaded = getattr(asyncio, "ThreadedChildWatcher", None)
    if not (get_watcher and set_watcher and threaded):
        # 3.14+, or a platform with no child watchers at all -- nothing to do.
        return
    try:
        with warnings.catch_warnings():
            # 3.12 deprecates these; the call is still the only way to clear the
            # leak on 3.12, so silence the warning rather than skip the fix.
            warnings.simplefilter("ignore", DeprecationWarning)
            current = get_watcher()
            # Install a FRESH watcher when the current one is the wrong type OR
            # is still holding pid->callback entries: those callbacks are bound
            # to a loop that may already be closed, and matching on type alone
            # would leave them in place.
            if not isinstance(current, threaded) or getattr(current, "_callbacks", None):
                set_watcher(threaded())
    except Exception:  # noqa: BLE001 -- isolation cleanup must never fail a test
        # Test-isolation cleanup must never fail a test; worst case is the
        # pre-existing leak, which the next test's loop setup also tolerates.
        pass


@pytest.fixture(autouse=True, scope="module")
def _fake_computer_use_backend():
    """Register the shipped FAKE computer-use backend for the whole suite.

    Computer use reads another application's accessibility tree, captures its
    window pixels, and synthesizes clicks/keystrokes into it. CI must never do
    any of that, so this is one of the TWO mechanisms that keep the native path
    unreachable in tests:

    1. this process-wide registration, so ``get_shared_backend()`` always
       returns ``FakeComputerUseBackend`` (``platform_id == "fake"``);
    2. structural — the package has no module-scope ``CDLL``/``find_library``, so
       importing it on a Linux runner loads nothing native.

    Both are asserted: ``test_computer_use_backend.py::
    test_ci_never_selects_a_native_backend`` pins (1) and
    ``test_computer_use_unsupported.py`` pins (2).

    MODULE-scoped, not function-scoped, deliberately: the registration is a
    single module-global assignment plus a singleton drop, and paying that (plus
    the ``kiro_crew.testing.fake_computer_use`` import) on all ~16k tests would
    be pure overhead. Any test that swaps the backend itself is responsible for
    restoring it (see that file's ``restore_registry`` fixture) — a
    function-scoped fixture here would paper over such a leak instead of letting
    it fail.
    """
    from kiro_crew.computer_use.backend import (
        register_computer_use_backend,
        reset_shared_backend,
    )
    from kiro_crew.testing.fake_computer_use import FakeComputerUseBackend

    register_computer_use_backend(FakeComputerUseBackend)
    reset_shared_backend()
    yield
    register_computer_use_backend(None)
    reset_shared_backend()


@pytest.fixture(autouse=True)
def _no_release_feed_network(_floor_monkeypatch) -> None:
    """Make the update check's network seam unreachable for the whole suite.

    ``handlers.updates._do_update_check`` now has a second branch: any install
    that is NOT a git checkout is compared against the release-channel feed on the
    CDN. Two ordinary things reach it without a test asking to — ``/api/status``
    fires ``_do_update_check`` as a background task once
    ``_UPDATE_CHECK_INTERVAL`` has elapsed (and ``_last_update_check`` starts at
    ``0.0``, so the first call always qualifies), and any direct call in a test
    env with no ``KIROCREW_PROJECT_DIR`` takes the feed branch by definition.

    Without this fixture the suite would make real HTTPS requests to
    ``updates.crew.kiro.dev`` — slow, flaky, offline-hostile, and CI traffic
    nobody asked for. Tests that WANT a feed response stub this same seam, which
    overrides the fixture for that test.

    The refusal is an ``AssertionError`` because that is the loudest signal
    available, but note ``_do_update_check``'s outer ``except Exception`` net will
    convert it into ``error="unknown"`` rather than failing the test — so this is
    a NETWORK guard first and a diagnostic second. A test that means to exercise
    the feed branch must stub the seam and assert on the result.
    """

    async def _refuse(url: str) -> tuple[int, bytes]:
        raise AssertionError(
            f"test reached the real release feed ({url}) — stub "
            "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes instead"
        )

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes", _refuse, raising=True
    )


@pytest.fixture(autouse=True)
def _no_carried_auto_update_effect(_floor_monkeypatch) -> None:
    """Start every test with no derived ``auto_update_effect`` on the status frame.

    The gateway's update loop records the effect it acts on, and a recorded one
    arms the status path's background re-derivation (git probes, policy reads).
    Left over from one test, it changes the status field the next test reads and
    can start that derivation inside an unrelated test's event loop.
    """
    from kiro_crew.dashboard.handlers import updates

    _floor_monkeypatch.setattr(updates, "_auto_effect", None)
    _floor_monkeypatch.setattr(updates, "_auto_effect_task", None)
    _floor_monkeypatch.setattr(updates, "_shape_effect", None)


@pytest.fixture(autouse=True)
def _no_live_kernel_memory_pressure(_floor_monkeypatch) -> None:
    """Pin the macOS kernel memory-pressure level to "unknown" for the suite.

    ``resource_status.probe`` reads it, and through the probe so do
    ``prewarm_allowance``, the posture surfaces and the subagent gate's pressure
    hold. On a macOS runner the live level is whatever the machine is under, so
    without this pin a busy runner at WARN would hold spawns and zero the prewarm
    allowance in tests that never asked about pressure. ``None`` is the value
    every caller fails open on, so it is what a non-macOS host already reads.
    A test about the level patches it in its own body, on top of this; the
    reader's own tests capture the real function at import. Patched through the
    floor's own ``MonkeyPatch`` so a test's ``monkeypatch.undo()`` cannot lift it.
    """
    _floor_monkeypatch.setattr(
        "kiro_crew.platform_compat.memory_pressure_level", lambda: None, raising=True
    )
    # On a macOS runner that pinned None would otherwise make the probe log its
    # once-per-process "level unreadable" WARNING into whichever test came first.
    _floor_monkeypatch.setattr(
        "kiro_crew.resource_status._pressure_unreadable_reported", True, raising=True
    )


@pytest.fixture(autouse=True)
def _no_live_catalog_network(_floor_monkeypatch):
    """Make the official app catalog's network seam unreachable for the suite.

    ``official_catalog._open_catalog`` is THE seam every catalog fetch goes
    through (its own docstring says tests must intercept there). Two paths
    reach it without a test asking to: the install path's
    ``inventory_for_install`` performs a fresh, deliberately UNCACHED HTTPS
    fetch of ``official-registry.json`` on every call, and store
    listings can trigger ``load_official_catalog``. Without this fixture,
    any test that walks either path makes a real HTTPS request to the live
    CDN — slow, offline-hostile, and nondeterministic: the test's verdict
    then depends on the CI runner's network, and one transient failure also
    poisons the module's on-disk failure memory for the rest of the worker.

    Tests that want a catalog answer stub a higher seam
    (``fetch_document``, ``fetch_inventory_entries``, or
    ``inventory_for_install``), which keeps this fixture from ever being
    reached. A test that genuinely needs the real opener — e.g. against a
    loopback server it started itself — must opt in explicitly: request
    this fixture by name and monkeypatch ``_open_catalog`` back to the
    original it yields.

    The refusal is an ``AssertionError`` — deliberately OUTSIDE the
    exception family ``fetch_document`` degrades on — but note the install
    path's fail-closed ``except Exception`` in
    ``registry._resolve_registry_row`` will convert it into a catalog
    refusal rather than failing the test with this message. Like
    ``_no_release_feed_network`` above, this is a NETWORK guard first and a
    diagnostic second.
    """
    from kiro_crew.apps import official_catalog

    original = official_catalog._open_catalog

    def _refuse(req: object) -> None:
        url = getattr(req, "full_url", repr(req))
        raise AssertionError(
            f"test reached the live app catalog ({url}) — stub "
            "kiro_crew.apps.official_catalog.fetch_document (or a higher "
            "seam such as inventory_for_install) instead"
        )

    _floor_monkeypatch.setattr(official_catalog, "_open_catalog", _refuse, raising=True)
    yield original


@pytest.fixture(autouse=True)
def _reset_create_rate_limit_buckets():
    """Clear the session/folder creation rate limiter between tests.

    ``kiro_crew.dashboard.create_rate_limit`` keeps its per-(verb, caller)
    buckets in MODULE-LEVEL state (deliberately: the production guard needs no
    durable state), so every session-creating test in a pytest process
    accumulates timestamps under shared caller keys. A shard whose test
    composition performs more than the per-window budget of creates within one
    wall-clock window then refuses a legitimate test create with
    ``create_rate_limited`` — a pass/fail outcome decided by shard composition
    and runner speed, not the code under test. The limiter's own direct tests build their scenarios on
    top of a clean slate, so clearing between tests changes nothing for them.
    """
    from kiro_crew.dashboard import create_rate_limit

    with create_rate_limit._lock:
        create_rate_limit._buckets.clear()
    yield
    with create_rate_limit._lock:
        create_rate_limit._buckets.clear()


@pytest.fixture(autouse=True)
def _crewmate_dynamic_capabilities_on():
    """Run every test with every crewmate's Dynamic capabilities ON unless it says otherwise.

    The per-crewmate preview (``KiroCrewAgentConfig.dynamic_capabilities``) ships
    off, but the suite pins the crewmate-driven capability path it holds
    (find_capabilities, the card, the hand-off summary, the nudge). A test of the
    per-crewmate read sets ``crewmate_dynamic_capabilities._override = None``
    itself (``test/test_crewmate_dynamic_capabilities.py``).
    """
    from kiro_crew import crewmate_dynamic_capabilities

    prior = crewmate_dynamic_capabilities._override
    crewmate_dynamic_capabilities._override = True
    yield
    crewmate_dynamic_capabilities._override = prior
