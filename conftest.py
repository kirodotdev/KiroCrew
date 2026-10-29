"""Repo-root pytest configuration: the host-mutation floor.

``test/conftest.py`` holds the bulk of the suite's isolation, but it only applies
to ``test/``. ``[tool:pytest] testpaths`` also collects ``transfer`` and
``src/kiro_crew/apps/builtins`` (~108 test modules that ship inside the package,
next to the code they cover), and those get no ``test/conftest.py`` fixtures at
all -- only this file, plus that app's own ``tests/conftest.py`` where one exists.
Anything that must hold for EVERY test therefore has to live here, at the
rootdir, which is the one conftest pytest applies to every testpath.

Only the HOST-MUTATION FLOOR belongs in this file, plus the two Determinism-contract
pieces described below: the guards that must hold for a test collected from any
testpath, because what they protect is the developer's machine rather than the
correctness of one suite. Everything that is
merely suite-specific isolation stays in ``test/conftest.py``.

The floor has nine parts, and each one exists because the "remember to isolate
this" contract failed at least once:

* **Git auto maintenance.** ``GIT_CONFIG_GLOBAL`` points at a session-owned file
  that turns ``maintenance.auto`` and ``gc.auto`` off, and the same keys are
  appended through ``GIT_CONFIG_COUNT``. A fixture commit otherwise leaves a
  detached ``git maintenance run --auto`` adding and removing lock files under
  ``.git``, and a test that copied or walked that repo right after saw a file
  vanish (issue #20310). See ``test/git_maintenance_helpers.py``. The git
  location variables (``GIT_DIR``, ``GIT_INDEX_FILE``, ``GIT_WORK_TREE``, ...)
  are removed as well: a suite started from a git hook inherits them, and a
  fixture repo's ``git add`` would then stage into the operator's real index.

* **Services.** ``$XDG_CONFIG_HOME`` is redirected and the stdlib spawn funnels
  refuse a ``systemctl``/``launchctl`` invocation carrying a mutating verb, so no
  test can reconfigure or restart the operator's real gateway (issue #1722).
* **The data home.** ``KIROCREW_HOME`` is pinned per test, and the ``~/.kiro``
  paths that production binds at IMPORT time (which the env var cannot reach) are
  pinned with it. Without this, the ~108 test modules that ship inside the package
  under ``src/kiro_crew/apps/builtins/*/tests/`` -- which see this conftest and no
  other -- write the operator's live ``~/.kiro/crew`` the moment they touch
  ``config_dir()``. ``KIROCREW_WORKSPACE`` is pinned alongside it for the same
  reason: ``workspace_root()`` is a SEPARATE default from ``config_dir()`` (it
  falls back to ``~/workplace/kirocrew-workspace``, not under the data home at
  all), so pinning only ``KIROCREW_HOME`` leaves it resolving to the operator's
  real ``~/workplace`` the moment a test reaches an agent working directory.
* **Credential environment.** Recognised fixed credentials and validated
  ``JIRA_TOKEN_<HEX>`` keys are restored after every test, so a fabricated
  ``.env`` cannot silently override the next test's credentials in the same
  worker.
* **The inherited shell environment.** The entries ``name_grant`` refuses as
  inherited preloads are removed for each test's duration and restored
  afterwards: the ``_ENV_PRELOAD_VARS`` names (``BASH_ENV``, ``ENV``, ...) and
  exported bash functions (``BASH_FUNC_*`` keys, or the legacy bare-name
  spelling whose value starts with ``() {``). RHEL-family hosts export ``which``
  as a function from ``/etc/profile.d/which2.sh``, and that refusal is checked
  before every narrower code, so 79 of 163 name-grant tests observed the wrong
  refusal on those hosts while Ubuntu CI stayed green (issue #8395).
* **The agent-spec home.** ``kiro_agents_dir()`` is a LAZY resolver, so neither of
  the two above reaches it, and a test that reaches the spec write path rewrites
  the machine-wide ``<kiro home>/agents/kirocrew.json`` -- the file that decides
  which MCP servers the operator's real agent has (issue #4912). The per-module
  override seams are pinned instead of ``KIRO_HOME``, which cannot be pinned
  without overriding ~35 tests' own ``Path.home()`` isolation.
* **The system temp directory.** ``tempfile``'s base is redirected to a per-run
  directory for the whole process, so a bare ``mkdtemp()`` whose cleanup is missing
  or skipped leaves its directory somewhere this run owns and removes, instead of
  accumulating in the shared temp root forever. What was left behind is REPORTED
  first, so the leak is a red rather than silent inode consumption. On macOS the
  base is additionally moved to ``/tmp`` -- the prefix Linux and CI already use --
  because the launchd per-user temp dir is long enough to break an AF_UNIX bind and
  random enough to read as a credential. See :data:`conftest_floor_tmp._SHORT_TMP_BASE`.
* **The sandbox mount-source sweep.** The periodic janitor's candidate roots
  (the operator's real ``/run/user/$UID`` and ``/dev/shm``) and its /proc pin
  scan are both pinned to inert stand-ins -- the roots to an empty directory,
  the scan to fail-closed -- so no test deletes real orphaned bind-mount
  sources from the developer's machine or depends on host process state.
* **The repository checkout.** The run fails when it ends with residue anywhere in
  the checkout, which is how a subprocess spawned without ``cwd=`` announces
  itself.

One consequence of living at the rootdir: the module name ``conftest`` is now
resolvable from the repository root as well as from ``test/``, and 11 test modules
import helpers by bare name (``from conftest import requires_git``). Under pytest's
default ``prepend`` import mode each test module's own directory goes on
``sys.path`` first, so those imports still bind to ``test/conftest.py`` -- but the
name IS shadowed, so switching to ``--import-mode=importlib`` would need those
imports made explicit first. The visible effect today is that isort now classifies
``conftest`` as first-party (a root-level module is inside its default
``src_paths``), which is why this change also reorders that import in the modules
that use it.

Why this floor exists (issue #1722): a test asserting that a staged cutover can
be *cancelled* rewrote the operator's real ``kirocrew-gateway.service`` drop-in
to point at its own pytest temp dir. pytest deleted the temp dir at the end of
the run; the drop-in survived, so systemd looped on ``203/EXEC`` — 548 failed
starts over 25 minutes. The test never intended to touch the host: it called the
real ``_make_live()`` because that function was the subject under test, and two
of that function's seams (the drop-in path and the subprocess layer) were left
for each test to remember to stub.

The fixtures below remove that "remember to" from the contract. None of them
changes the behaviour of a test that already isolates itself correctly: every one
sets a value a test can still override, and a test that sets its own
``KIROCREW_HOME`` or its own temp dir keeps winning.

Besides the floor, this file carries the two pieces of the Determinism contract
(``docs/system-specs/common/testing-conventions.md``) that every testpath needs and
that no test can import from ``test/``: the opt-in ``manual_clock``, ``seeded_rng``
and ``local_tz`` fixtures (requested by name, never autouse), and the per-test
publication of the running test's timeout to
``kiro_crew.testing.wait.runner_timeout_secs``, so a wait helper's default deadline is
half of the timeout pytest-timeout enforces on that test.

Imports at MODULE level are stdlib + pytest only, on purpose: a rootdir conftest is
imported before every collection, so pulling ``kiro_crew`` in here would make the
whole suite depend on import-time side effects of the package under test. The
fixtures that do need ``kiro_crew`` import it in their own body, which runs at test
setup -- by which point the test module has already imported the package anyway --
and tolerate an ImportError so a partial checkout cannot break collection.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib
import importlib.util
import linecache
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings

import _pytest.outcomes
import _pytest.runner
import pytest

# ── ACP frame recorder switch (rootdir floor) ───────────────────────────────
# ``kiro_crew.acp._frame_record`` starts its writer thread at IMPORT time when
# ``KIROCREW_ACP_RECORD_FRAMES`` is set, and ``acp.client`` (hence the dashboard
# server, the session handle, the Slack handler, ...) imports it transitively. An
# operator running the suite with a live recording configured would otherwise
# have every collected test module's fake frames appended to their real corpus
# file -- from ANY testpath, including the ~108 modules under
# ``src/kiro_crew/apps/builtins/*/tests/`` that never see ``test/conftest.py``.
# Cleared here, at rootdir-conftest import, which precedes every test module's
# first ``kiro_crew`` import; tests that need the switch set it themselves
# through monkeypatch.
os.environ.pop("KIROCREW_ACP_RECORD_FRAMES", None)


@pytest.fixture
def _floor_monkeypatch():
    """The isolation floor's OWN ``MonkeyPatch`` -- never the shared ``monkeypatch``.

    Every floor fixture below patches through this instance instead of the
    ``monkeypatch`` fixture a test also receives. The two are undone
    independently, and that is the point: ``monkeypatch.undo()`` reverts EVERY
    record on the instance it is called on, so a test that called it mid-way to
    drop one of its own patches -- ~90 sites do, to restore a function before a
    final assertion -- also dropped the floor's ``KIROCREW_HOME`` pin, and
    whatever ran after that resolved the OPERATOR's real data home. Five full
    runs on a developer machine left ``~/.kiro/crew/trash/session-storage.lock``
    (``test_session_storage`` ran ``empty_trash()`` against the real trash) and
    ``~/.kiro/crew/inbound-spool/refused.jsonl.lock`` behind this way.

    A test that pins something itself still wins: its ``monkeypatch`` is set up
    after the floor and torn down before it, so its value overrides during the
    test and the floor's value is what gets put back first, then the original.
    """
    mp = pytest.MonkeyPatch()
    # In-process CLI calls clear the sandbox markers and publish the UTF-8
    # process contract. Preserve the worker's exact outer environment for the
    # next test, including absent and explicitly empty values.
    cli_process_environment = {
        name: os.environ.get(name)
        for name in (
            "KIROCREW_SANDBOX_ACTIVE",
            "KIROCREW_SANDBOX_LEVEL",
            "PYTHONUTF8",
            "PYTHONIOENCODING",
        )
    }
    try:
        yield mp
    finally:
        mp.undo()
        for name, value in cli_process_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def monkeypatch(_floor_monkeypatch):
    """pytest's own ``monkeypatch``, re-declared to order it AFTER the floor.

    Body-identical to ``_pytest.monkeypatch.monkeypatch``; the only change is the
    dependency on ``_floor_monkeypatch``, which is what GUARANTEES the order the
    docstring above relies on: the floor is set up before the test's ``monkeypatch``
    and torn down after it, whatever autouse fixture in whatever conftest happens to
    request ``monkeypatch`` first. A test's ``setenv("KIROCREW_HOME", ...)`` therefore
    records the pinned value, restores it on the test's undo, and the floor's undo then
    restores the operator's. A test cannot tell the difference otherwise: same type,
    same scope, same undo semantics for everything IT patched.
    ``test_host_isolation_floor.py::TestTheDataHomeIsPinnedForEveryTestpath`` pins it.
    """
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


# ── the floor's own modules ───────────────────────────────────────────────────
#
# Most of the floor is defined in plain modules beside the suite, ``test/conftest_floor_*.py``,
# and bound here by name, because pytest registers fixtures and hooks from THIS module's
# namespace. They are loaded by file path: ``test/`` is off ``sys.path`` while this file runs,
# and tests also execute this file by path under other names. A loaded part is reused, so every
# copy of this file shares one module object per part. No bytecode is written for them, since
# this runs before ``pytest_configure`` moves bytecode off the checkout.
_FLOOR_PARTS = (
    "conftest_floor_home",
    "conftest_floor_tmp",
    "conftest_floor_configure",
    "conftest_floor_services",
    "conftest_floor_seams",
    "conftest_floor_restores",
    "conftest_floor_determinism",
    "conftest_floor_reporting",
    "conftest_floor_git",
)


def _load_floor_part(stem: str):
    """Load ``test/<stem>.py`` once per process, registered in ``sys.modules`` as *stem*."""
    module = sys.modules.get(stem)
    if module is not None:
        return module
    path = pathlib.Path(__file__).resolve().parent / "test" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module
    dont_write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[stem]
        raise
    finally:
        sys.dont_write_bytecode = dont_write_bytecode
    return module


for _stem in _FLOOR_PARTS:
    _load_floor_part(_stem)

# Once-per-run redirects pytest_configure installs, and the warning trim.
from conftest_floor_configure import (  # noqa: E402,F401 -- bound for pytest and for tests
    _AsyncFixtureScanGate,
    _gate_pytest_asyncio_fixture_scan,
    _pin_crew_log_off_for_the_process,
    _put_the_tree_under_test_first_on_child_paths,
    _redirect_bytecode_cache,
    _redirect_hypothesis_database,
    pytest_warning_recorded,
)

# Determinism fixtures and the report-only floors' detectors.
from conftest_floor_determinism import (  # noqa: E402,F401 -- bound for pytest and for tests
    _HOOKED_EVENTS,
    _NET_STRICT_ENV,
    _PACKAGE_TABLE,
    _PKG_ATTR_STRICT_ENV,
    _POOL_STATE,
    _TMP_AUDITED_EVENTS,
    _TZ_STATE,
    OffLoopbackNetworkRefused,
    _another_run_member,
    _AuditState,
    _bind_unbound_kiro_crew_modules,
    _classify_kill_target,
    _kill_finding,
    _network_finding,
    _PackageTable,
    _pool_activity,
    _process_zone,
    _runner_timeout_secs,
    local_tz,
    manual_clock,
    pytest_report_header,
    seeded_rng,
)

# Git auto maintenance off and git's location variables removed for every test.
from conftest_floor_git import (  # noqa: E402,F401 -- bound for pytest and for tests
    _GIT_MAINTENANCE,
    _git_global_config,
    _git_location_unset,
    _git_maintenance_off,
    _git_maintenance_off_entries,
    _git_vars_to_strip,
    _load_git_maintenance_helpers,
)

# Data home: the import-time floor and the per-test home, SEL and ~/.kiro pins.
from conftest_floor_home import (  # noqa: E402,F401 -- bound for pytest and for tests
    _AGENT_SPEC_HOOKS,
    _HOME_PIN_ARMED,
    _REAL_DATA_HOMES,
    _REAL_KIRO_HOME_AT_START,
    _SHARED_KIRO_PATHS,
    _TMP_ROOT_PREFIX,
    _agent_spec_seam_modules,
    _breadcrumb_guard,
    _isolate_agent_spec_home,
    _isolate_kiro_sessions_dir,
    _isolate_kirocrew_home,
    _isolate_sel_default_dir,
    _isolate_shared_kiro_paths,
    _isolate_subagents_dir,
    _isolation_dirs,
    _isolation_root,
    _refuse_a_real_data_home,
    _refuse_a_resolved_real_default_home,
    _resolved_real_default_home,
    _tmp_root_prefix_for_run,
    sel_private_root,
    unpinned_agent_spec_home,
    unpinned_kiro_sessions_dir,
)

# Collection-time marks, the teardown timer stop, and the CI annotations.
from conftest_floor_reporting import (  # noqa: E402,F401 -- bound for pytest and for tests
    _MAX_ANNOTATED_FAILURES,
    MAX_NODEID_CHARS,
    _base_nodeid,
    _failure_reason,
    _gha_escape,
    _refuse_oversized_nodeids,
    _report_location,
    _root_can_create_real_symlink,
    _ungrouped_nodeid,
    on_self_hosted_runner,
    pytest_collection_modifyitems,
    pytest_runtest_makereport,
)

# Teardown joins and the globals put back after every test.
from conftest_floor_restores import (  # noqa: E402,F401 -- bound for pytest and for tests
    _EMBED_EVICTOR_THREAD,
    _EXECUTOR_JOIN_SECS,
    _TELEMETRY_CONSENT_THREAD,
    _cancel_session_tree_checkpoints,
    _drain_windows_proactor_finalizers,
    _join_install_receipt_workers,
    _join_test_loop_executor,
    _live_embed_evictors,
    _live_telemetry_consent_workers,
    _no_leaked_embed_evictor,
    _no_leaked_telemetry_exporter,
    _restore_hooks_integration_globals,
    _restore_log_queue_listener,
    _restore_log_record_factory,
    _restore_logger_levels,
    _restore_relay_gateway_ingress,
    _resume_crew_log_eager_fold,
    _retire_crew_log_eager_fold,
)

# Host seams and process singletons pinned for every test.
from conftest_floor_seams import (  # noqa: E402,F401 -- bound for pytest and for tests
    _isolate_agent_state_sidecar,
    _isolate_launchd_paths,
    _isolate_pre_exec_stops,
    _isolate_sandbox_mount_source_roots,
    _isolate_xdg_config_home,
    _no_central_policy_fetch,
    _no_credential_env_residue,
    _no_default_builtin_skills_sync,
    _no_model_download,
    _pin_agent_state_sidecar,
    _pin_kill_and_reap_group_probe,
    _reset_member_eventlog_singleton,
    _reset_platform_context,
    _sandbox_mount_source_root,
    _xdg_config_root,
    sandbox_sweep_original,
)

# Host services: the mutation guard and the systemd user session.
from conftest_floor_services import (  # noqa: E402,F401 -- bound for pytest and for tests
    _ALWAYS_REFUSED,
    _HOST_SERVICE_EXEC_ALLOWED_MODULES,
    _SERVICE_MANAGERS,
    _block_host_service_mutation,
    _fail_on_refused_real_reclaim_write,
    _no_real_cgroup_reclaim_write,
    _no_systemd_user_session,
    _refusal_reason,
    real_user_session,
)

# Temp directories: run naming, the tempfile redirect, the residue report.
from conftest_floor_tmp import (  # noqa: E402,F401 -- bound for pytest and for tests
    _SHORT_TMP_ROOT_ENV,
    _TMP_ENV_VARS,
    _TMP_PER_TEST,
    _TMP_PER_TEST_ENV,
    _TMP_RESIDUE_STRICT_ENV,
    _create_tmp_root,
    _prefer_short_tmp_base,
    _redirect_tempfile_base,
    _remove_tree,
    _stop_git_discovery_above_the_temp_roots,
    _tmp_residue,
    _tmp_residue_report,
)

# Defined by conftest_floor_tmp only in per-test mode.
if _TMP_PER_TEST:
    from conftest_floor_tmp import _isolate_tempfile_base_per_test  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _scrub_inherited_preload_env(_floor_monkeypatch):
    """Hide the entries ``name_grant`` refuses as inherited preloads, then restore them.

    RHEL-family distributions (Amazon Linux 2023, RHEL, CentOS Stream, Fedora) ship
    ``/etc/profile.d/which2.sh``, which exports ``which`` as a shell function -- so
    ``BASH_FUNC_which%%`` sits in ``os.environ`` of any login shell before pytest even
    starts. ``name_grant._inherited_preload()`` fires its AMBIGUOUS_ENV refusal on
    exactly that shape, and that refusal is checked before every narrower code, so on
    such a host 79 of the 163 tests in ``test/test_name_grant.py`` observed
    ``inherited_env_can_redefine_programs`` instead of the code they assert, while
    Ubuntu CI (no ``which2.sh``) stayed green (issue #8395). The detector's OTHER half
    is host state just as easily: a login profile exporting ``BASH_ENV``, or a
    container image setting ``ENV``, reproduces the same failure shape with no
    ``which2.sh`` anywhere. The PRODUCT behaviour is correct -- an inherited preload
    genuinely can redefine a program the agent runs -- the gap was that this floor
    pins the data home, the credential env and the temp base, but not the environment
    the run inherited.

    The predicate is IMPORTED from ``name_grant`` rather than restated, so the scrub
    and the detector cannot drift apart, and it mirrors BOTH halves the detector
    checks: the ``_ENV_PRELOAD_VARS`` names (matched truthy, exactly as the detector's
    ``os.environ.get(var)`` reads them -- an empty value triggers no refusal and is
    left alone), and the exported-function pair (the ``BASH_FUNC_`` key prefix, plus
    the legacy bare-name spelling whose value starts with ``() {``). It is also
    deliberately no WIDER than that predicate: this is not a general "no exported
    functions" floor -- how wide the detector should be is ``name_grant``'s design
    question, and the scrub merely mirrors its answer. The import is deliberately
    UNGUARDED, like ``_no_credential_env_residue``'s: an ImportError escape hatch
    would disarm the scrub silently on a broken checkout and hand back the exact
    79-failure pattern this fixture exists to remove, as a wall of wrong refusal
    codes instead of one clear error.

    The removals use the private host-floor stack. A shared ``monkeypatch.undo()``
    cannot restore an inherited preload during the test. The public ``monkeypatch``
    fixture depends on ``_floor_monkeypatch``, so its stack unwinds first at teardown:
    an override of the SAME inherited key first reverts to "absent", and only then
    does this fixture's ``delenv`` record restore the inherited value. Reversing
    those steps would delete the restored host value and leak the scrub into the
    next test. ``test_name_grant.py::TestInheritedHostEnvironment``
    pins exactly that same-key case, and
    ``test_host_isolation_floor_restores.py::TestInheritedShellEnvironmentIsScrubbed`` drives
    one real cycle of this fixture directly.

    The deliberate AMBIGUOUS_ENV tests lose nothing: they construct their entries
    inside the test body, after the scrub. The teardown sweep below is the
    ``_no_credential_env_residue`` half of the contract: a matching entry still
    present at teardown is either a raw ``os.environ`` write (no undo record --
    swept here, stays gone) or a monkeypatch-managed one (its undo re-deletes
    tolerantly or restores what it recorded), so nothing this fixture scrubbed and
    nothing a test leaked survives past the test on this worker.
    """
    monkeypatch = _floor_monkeypatch
    from kiro_crew.name_grant import (
        _BASH_FUNC_KEY_PREFIX,
        _BASH_FUNC_VALUE_PREFIX,
        _ENV_PRELOAD_VARS,
    )

    def _is_inherited_preload(key: str, value: str) -> bool:
        if key in _ENV_PRELOAD_VARS:
            return bool(value)
        return key.startswith(_BASH_FUNC_KEY_PREFIX) or value.startswith(_BASH_FUNC_VALUE_PREFIX)

    for key in [k for k, v in os.environ.items() if _is_inherited_preload(k, v)]:
        monkeypatch.delenv(key)
    try:
        yield
    finally:
        for key in [k for k, v in os.environ.items() if _is_inherited_preload(k, v)]:
            os.environ.pop(key, None)


# ── the process working directory is shared state too ─────────────────


#: The working directory pytest started in, captured once. Restoring to THIS rather than
#: to a per-test snapshot is both simpler and more correct: it is the only value that is
#: certain to still exist, and it is where every test expects to begin.
_SESSION_CWD: str | None = None

#: Tests between ``linecache`` clears. Measured on a 1,266-test slice: 500 saves
#: 22.8 MiB and 100 saves 28.0 MiB, with no measurable difference in wall time, so the
#: CPU side of this trade is flat enough to take the memory. The cache is per-process,
#: so under ``-n auto`` each worker clears on its own count.
_LINECACHE_CLEAR_EVERY = 100
_TESTS_SINCE_LINECACHE_CLEAR = 0

#: How many objects ``pytest_collection_finish`` moved into the permanent generation.
#: ``None`` until it has run, which is what distinguishes "this hook did its job" from
#: the few hundred objects the interpreter had already frozen on its own.
_FROZEN_AT_COLLECTION: int | None = None


#: Module nodeids whose COLLECTION built the process-global metrics recorder.
#:
#: Filled by :func:`pytest_make_collect_report`, asserted empty by
#: ``test_host_isolation_floor.py::TestNoMetricIsEmittedAtImport``. A metric emitted
#: while a test module is being imported (a default argument evaluated at
#: definition time, a module constant built through a factory that counts itself)
#: runs ``get_recorder()`` before any test's pins exist. The recorder's first build
#: reads the OPERATOR's real config; with ``telemetry.enabled`` there, it starts a
#: ``PeriodicExportingMetricReader`` whose exporter is bound to the real
#: ``~/.kiro/crew/metrics`` and which exports every minute for the life of the xdist
#: worker. The per-test exporter-leak guard cannot see it (the thread predates every
#: test), and the per-test ``KIROCREW_TELEMETRY=0`` pin cannot reach it (it is already
#: built). The third full-run audit found four worker pids writing the operator's
#: metrics dir on every run from exactly one such default argument.
IMPORT_TIME_METRIC_EMITTERS: list[str] = []


def _metrics_provider_module():
    return sys.modules.get("kiro_crew.metrics.provider")


def _pin_telemetry_off_for_the_process() -> None:
    """``KIROCREW_TELEMETRY=0`` from process start, not just from each test's setup.

    The per-test pin (``_no_model_download``) covers the test body; this covers the
    stretch BEFORE the first test (conftest imports and the collection of every
    module), which is where an import-time metric emission builds the recorder. With
    the env pin in force the build is a no-op recorder that reads no config and starts
    no thread, so even an emitter this guard has not yet named cannot reach the host.
    Tests that exercise telemetry delete the variable themselves, as they already did.
    """
    os.environ["KIROCREW_TELEMETRY"] = "0"


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Name the module whose import built the metrics recorder, and undo it.

    The detection is ``metrics.provider._ever_built`` flipping across one module's
    collection: the flag is set by every build, including the no-op one the env pin
    above turns it into, so an emitter is caught whether or not it reached the host.
    ``reset_for_testing()`` then drops what was built (stopping an exporter thread if
    one exists), so the next module starts clean and the attribution stays per-module.
    Fail the collection report on the detecting worker: file shards do not all
    collect the test that asserts the record, and xdist forwards collection errors
    to the controller even when that worker executes no tests.
    """
    provider = _metrics_provider_module()
    built_before = bool(provider is not None and getattr(provider, "_ever_built", False))
    outcome = yield
    if not isinstance(collector, pytest.Module):
        return
    provider = _metrics_provider_module()
    if provider is None or not getattr(provider, "_ever_built", False):
        return
    if built_before:
        # Built before this module was reached, by a conftest import. Named as such
        # rather than blamed on whichever module happened to be first.
        IMPORT_TIME_METRIC_EMITTERS.append(f"conftest import (before {collector.nodeid})")
    else:
        IMPORT_TIME_METRIC_EMITTERS.append(collector.nodeid)
    with contextlib.suppress(Exception):
        provider.reset_for_testing()
    report = outcome.get_result()
    if not report.failed:
        report.outcome = "failed"
        report.longrepr = (
            f"Import-time metric emission: {IMPORT_TIME_METRIC_EMITTERS[-1]}. "
            "Build the value inside the test or fixture instead."
        )


def pytest_configure(config: pytest.Config) -> None:
    """Record the working directory pytest started in, before any test can move it."""
    _refuse_a_real_data_home()
    _pin_telemetry_off_for_the_process()
    _pin_crew_log_off_for_the_process()
    _prefer_short_tmp_base()
    _install_short_tmp_root()
    _redirect_hypothesis_database()
    _redirect_bytecode_cache()
    _install_determinism_audit_hook()
    _put_the_tree_under_test_first_on_child_paths()
    _gate_pytest_asyncio_fixture_scan()
    global _SESSION_CWD
    try:
        _SESSION_CWD = os.getcwd()
    except OSError:  # pragma: no cover - pytest could not have started here
        _SESSION_CWD = str(_REPO_ROOT)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Move the collected item tree out of the garbage collector's reach.

    Collecting every testpath leaves roughly three million objects alive, and they
    are alive for the rest of the run by design -- the item tree, its fixture closures,
    the rewritten assertion code. The garbage collector does not know that, so every
    full pass walks all of them looking for cycles it will never find. Worse, on the
    interpreters this suite supports the full collection is scheduled from a measure of
    how much long-lived material exists, so that static population also DELAYS
    collection: a test's own cyclic garbage waits longer before anything reclaims it.
    (The exact scheduling differs across 3.10-3.13 -- 3.13 replaced the generational
    threshold with an incremental collector -- but the static set is unscanned either
    way, which is the part this depends on.)

    ``gc.freeze()`` moves everything currently tracked into a permanent generation that
    is never scanned. Measured over 3,798 test executions: full passes went 1 -> 3 and
    the objects they reclaimed went 61,604 -> 423,380, a 6.9x improvement in how much
    cyclic garbage is actually collected during a run.

    The end-RSS effect is small on its own (-3.5 MiB) because freed memory stays in the
    allocator's arena rather than returning to the OS. What it buys is that the garbage
    is reclaimed at all, which is why it is worth keeping despite the modest number.

    Placed at collection finish, not at configure: before collection there is nothing
    worth freezing, and after the first test the population is no longer purely static.
    Freezing is safe here for the same reason it helps -- these objects were going to
    live for the whole session anyway. Verified rather than assumed: live tracked objects
    at session end are slightly FEWER with the freeze than without (3,221,373 vs
    3,235,522), uncollectable stays 0, and the frozen count itself falls during the run,
    so refcount reclamation still works inside the permanent generation. Only cyclic
    garbage created BEFORE the freeze could leak, and collection creates none.

    It also helps the sandbox's ``fork``-based probes rather than hurting them: this is
    the documented pre-fork optimization, and the 16 sandbox suites pass identically
    with and without it.

    Records the delta rather than leaving the effect to be inferred from
    ``gc.get_freeze_count()``: the interpreter already has a few hundred objects frozen
    before this runs, so a bare "is anything frozen" assertion passes with this hook
    deleted.
    """
    global _FROZEN_AT_COLLECTION
    gc.collect()
    before = gc.get_freeze_count()
    gc.freeze()
    _FROZEN_AT_COLLECTION = gc.get_freeze_count() - before


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    """Periodically drop ``linecache``'s copy of every source file that was read.

    ``linecache`` keeps the full TEXT of every file it is asked for, and nothing evicts
    it, so a worker accumulates source it will not look at again. Measured on a
    1,266-test slice: 90 files / 0.8 MiB after collection, growing to 170 files /
    11.4 MiB by the end of the run, and the ceiling is the 27.7 MiB of text in ``src``.

    The fillers are ``inspect.getsource`` (used by 128 test modules) and traceback
    rendering, one module at a time -- NOT the source-scanning guard tests, which read
    with ``Path.read_text()`` and add no linecache entries at all. The biggest single
    entries are simply the biggest modules (``dashboard/chat_runner.py`` at 408 KiB,
    ``slack/gateway.py`` at 388 KiB).

    This is the one of the three retention guards that pays on an ORDINARY slice rather
    than on a particular file, and it grows with the run: measured over 12,660 test
    executions, end RSS 1025 -> 924 MiB (-101 MiB, -10%), with the repeat-visit slope
    falling from 19.6 to 12.4 MiB per 1,000 tests.

    ``logfinish`` rather than teardown for tidiness, not safety: a failure report is
    unaffected either way. ``report.longrepr`` is a string already rendered at
    ``pytest_runtest_makereport``, and ``inspect.findsource`` calls
    ``linecache.checkcache()`` before ``getlines``, so a cleared or stale entry is
    simply re-read. Verified by running a two-failure file with no clearing, clearing at
    teardown, and clearing here: the ``FAILURES`` sections are byte-identical.

    Every N tests rather than every test because the cache is also what makes the next
    traceback cheap. The interval is a memory/CPU trade with a very flat CPU side --
    clearing 5x more often buys another ~5 MiB at no measurable time cost.
    """
    global _TESTS_SINCE_LINECACHE_CLEAR
    _TESTS_SINCE_LINECACHE_CLEAR += 1
    if _TESTS_SINCE_LINECACHE_CLEAR >= _LINECACHE_CLEAR_EVERY:
        _TESTS_SINCE_LINECACHE_CLEAR = 0
        linecache.clearcache()


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Budget ``-n auto`` by memory and by what other runs on this host hold.

    Registered HERE, at the rootdir, because a budget that only covers ``test/``
    is absent from ``transfer`` and from the in-package app suites -- and absent
    reads exactly like "decided not to clamp", so the gap is silent. Over-spawning
    workers takes the whole machine down, which makes this a host-protection
    concern and puts it on the same floor as the rest of them.

    The policy lives in :mod:`xdist_budget`; this is only the registration.
    Imported in-body to keep this file's module-level imports stdlib + pytest, and
    returning ``None`` on an ImportError hands the decision back to xdist's own
    default rather than breaking startup on a partial checkout.
    """
    try:
        import xdist_budget
    except ImportError:  # pragma: no cover - partial checkout
        return None
    return xdist_budget.resolve_workers()


def _join_telemetry_consent_worker() -> "BaseException | None":
    """Join the metrics provider's consent-recheck worker before this test's pins lift.

    ``metrics.provider.get_recorder()`` is on the path of ordinary production code
    (``skills``, ``session``, ``context``, ``hooks`` and the metric helpers all call
    it), and once per ``_CONSENT_RECHECK_SECS`` the call hands a consent re-read to a
    daemon thread. That thread runs ``KiroCrewConfig.load()``, which reaches
    ``config_dir()``, which honours ``KIROCREW_HOME`` *at call time* and ``mkdir``s
    the data home it names. The recorder is process-global and nothing resets it
    between tests, so the test that schedules the worker is rarely the test whose
    home it creates: the read lands a few milliseconds later, under the NEXT test's
    pin, and that test sees its own data home appear underneath it from a thread it
    never started. The same lazy-resolving-worker shape as the receipt writer
    :func:`_join_install_receipt_workers` joins, one module over.

    MEASURED, replaying one Windows CI shard's file list with a probe on every
    ``os.mkdir`` of a per-test home: the only off-main-thread creator of a home that
    was not the running test's own was this thread. The symptom it produced on CI was
    ``test_pathres_loop_starvation.py::test_the_inline_anchors_are_the_bounded_anchors``:
    the test builds the sensitive-target set twice and compares, and on Windows
    ``os.path.realpath`` keeps its ``\\\\?\\`` prefix when the directory above a
    not-yet-existing anchor is created between its two lookups -- so the first build
    carried ``\\\\?\\...\\<n>-kirocrew-home\\diag`` (``diag`` being the first anchor under
    the crew home) and the second did not.

    Same place and same reason as :func:`_join_install_receipt_workers`: the
    ``tryfirst`` teardown hook runs before any fixture unwinds, so the pins still hold
    and the worker's read lands in the home of the test that scheduled it. The module
    is found by thread name rather than imported, so a run that never builds a
    recorder pays one ``threading.enumerate``. Bounded by ``_EXECUTOR_JOIN_SECS``; a
    worker still alive past it is RETURNED as the failure to raise after the
    hookwrapper's ``yield`` (see :func:`_retire_crew_log_eager_fold` for why it is not
    raised here), because a consent read that does not finish in ten seconds is a
    wedged config read, and the next test would otherwise start with a live thread
    able to create its home at any moment.
    """
    workers = _live_telemetry_consent_workers()
    if not workers:
        return None
    deadline = time.monotonic() + _EXECUTOR_JOIN_SECS
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    still_alive = [t.name for t in workers if t.is_alive()]
    if not still_alive:
        return None
    return AssertionError(
        f"a telemetry consent-recheck worker {still_alive} is still running "
        f"{_EXECUTOR_JOIN_SECS:.0f}s after this test ended; its config read would land "
        "under the next test's KIROCREW_HOME. See conftest._join_telemetry_consent_worker."
    )


# Durations and phases pytest_runtest_logreport has already seen for the item whose
# runtest protocol is in flight, keyed by node id. The escape guard uses both to
# preserve one report per phase and to charge only time no logged report covers.
_escape_logged_reports: dict[str, tuple[float, set[str]]] = {}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    """Track logged phases and durations for the in-flight escape guard."""
    logged = _escape_logged_reports.get(report.nodeid)
    if logged is not None:
        duration, phases = logged
        _escape_logged_reports[report.nodeid] = (
            duration + max(report.duration, 0.0),
            phases | {report.when},
        )


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Turn a ``Failed`` that escapes the runtest protocol into that test's failure.

    Every ``pytest.fail`` raised inside setup, call or teardown is caught by
    ``CallInfo.from_call`` and becomes a report. The one that is NOT is
    pytest-timeout's: with ``timeout_func_only`` off, its SIGALRM handler can fire
    anywhere in the protocol -- while pytest is rendering a failure report, between
    phases -- and ``pytest.fail("Timeout >120.0s")`` then propagates out of
    ``pytest_runtest_protocol`` with no report logged. Under xdist that is fatal
    to the whole session, not the test: ``xdist.remote`` sends
    ``runtest_protocol_complete`` only when this hook RETURNS, so the controller
    either takes the worker's traceback as an INTERNALERROR or, when the worker
    goes on to finish, trips ``dsession.worker_workerfinished``'s
    ``assert not crashitem`` for the still-assigned item. Either way one slow
    test on an overloaded runner erases the shard's results.

    Outermost wrapper (``tryfirst``), so it sees what every inner wrapper --
    pytest-timeout's own included, which has already cancelled its timer by the
    time the outcome reaches here -- let through. Only ``Failed`` is repaired: an
    ``Exit`` escaping here is ``pytest.exit`` doing its job.

    The invariant is one report per phase. The earliest of setup and call without a
    logged report carries the escape, and teardown always runs unless it already
    logged. This leaves three branches: no call report synthesizes the earliest
    missing setup/call report, a call report without teardown puts the escape on the
    teardown report, and a logged teardown emits nothing extra.

    The teardown ``makereport`` hook disarms pytest-timeout before teardown report
    logging starts. All timed phases are complete there, so serialization and
    ``logfinish`` cannot create another timer escape. A logged teardown therefore
    reached the controller before branch three can be entered. An alarm in the setup
    or call logreport chain after this tracker runs but before xdist sends the report
    can still leave that original phase absent on the controller. The synthesized
    failure or teardown error makes that item fail loudly rather than pass green.

    ``_escape_logged_reports`` records phases and durations at the start of each
    logreport chain. ``pytest-split`` sums report durations by node id, so a
    synthesized report owns only protocol time not charged to a logged phase. A
    normal call keeps its elapsed time, while the replacement teardown owns only
    teardown time. The hook then returns normally so xdist completes the item.

    Teardown is required because live fixtures left on ``SetupState`` break the next
    item on the worker. A clean teardown receives the escaped failure. Its own error
    wins when teardown fails. Control-flow exceptions keep pytest's normal reraise
    and interactive handling.
    """
    protocol_start_perf = time.perf_counter()
    _escape_logged_reports[item.nodeid] = (0.0, set())
    try:
        outcome = yield
    finally:
        already_logged, logged_phases = _escape_logged_reports.pop(item.nodeid, (0.0, set()))
    protocol_stop = time.time()
    protocol_duration = time.perf_counter() - protocol_start_perf
    excinfo = outcome.excinfo
    if excinfo is None or not isinstance(excinfo[1], _pytest.outcomes.Failed):
        return
    escaped = excinfo[1]
    if "call" not in logged_phases:
        carrier = next(phase for phase in ("setup", "call") if phase not in logged_phases)

        def _reraise():
            raise escaped

        call = _pytest.runner.CallInfo.from_call(_reraise, carrier)
        call.duration = max(protocol_duration - already_logged, 0.0)
        call.stop = protocol_stop
        call.start = protocol_stop - call.duration
        report = item.ihook.pytest_runtest_makereport(item=item, call=call)
        item.ihook.pytest_runtest_logreport(report=report)
        _pytest.runner.call_and_report(item, "teardown", log=True, nextitem=nextitem)
    elif "teardown" not in logged_phases:
        call = _pytest.runner.CallInfo.from_call(
            lambda: item.ihook.pytest_runtest_teardown(item=item, nextitem=nextitem),
            "teardown",
            reraise=_pytest.runner.get_reraise_exceptions(item.config),
        )
        if call.excinfo is None:
            call = _pytest.runner.CallInfo(
                None,
                pytest.ExceptionInfo.from_exc_info(excinfo),
                start=call.start,
                stop=call.stop,
                duration=call.duration,
                when="teardown",
                _ispytest=True,
            )
        report = item.ihook.pytest_runtest_makereport(item=item, call=call)
        item.ihook.pytest_runtest_logreport(report=report)
        if _pytest.runner.check_interactive_exception(call, report):
            item.ihook.pytest_exception_interact(node=item, call=call, report=report)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    outcome.force_result(True)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Put the process working directory back, BEFORE any fixture teardown runs.

    A ``tryfirst`` HOOKWRAPPER: everything before the ``yield`` runs before any other
    teardown impl (so before any fixture unwinds), and the one thing after it (the
    verdict of ``_refuse_a_resolved_real_default_home``) runs after every fixture has
    been torn down. That split is load-bearing: the check must READ its evidence before
    the test's ``monkeypatch`` restores the global, but it must RAISE only once the
    floor has unwound, because an exception raised before the default teardown impl
    skips fixture finalization for the whole item and leaves every pin in place for
    the rest of the worker (1,245 downstream errors when it was first tried).

    The CWD is per-PROCESS, so under xdist one test's ``os.chdir`` silently becomes every
    later test's starting directory on that worker. That was survivable while the
    directory it pointed at outlived the run. It is not survivable now: with
    ``tmp_path_retention_policy = failed`` pytest removes a passing test's ``tmp_path`` at
    that test's own teardown, so a test that chdirs into ``tmp_path`` and does not come
    back leaves the worker sitting in a DELETED directory -- and then ``Path.cwd()`` raises
    ``FileNotFoundError`` in every later test that reaches it, including deep inside
    production code (``taskrunner.TaskRunner.__init__`` does ``work_dir or Path.cwd()``).

    Measured instance and its numbers:
    ``docs/system-specs/common/testing-conventions.md`` § Rules. It reads as "the suite is
    flaky" -- many files, each passing in isolation -- rather than as one missing line.

    **A ``tryfirst`` teardown hook rather than an autouse fixture, and the difference is
    load-bearing on Windows.** A fixture here would be an OUTER one (the rootdir conftest
    is set up before the test's own fixtures), so it would tear down LAST -- after
    ``tmp_path`` cleanup had already tried to remove a directory the process was still
    sitting in. Windows refuses to delete a process's current working directory, so the
    cleanup fails there. Making the fixture depend on ``tmp_path`` would invert the order,
    but at the price of allocating a directory for every test in the suite, which is
    exactly the per-test cost this change removed elsewhere. A ``tryfirst`` hookimpl runs
    before the default ``pytest_runtest_teardown``, which is what performs fixture
    finalization -- so the CWD is restored before ANY teardown, at no per-test cost.

    Restoring rather than failing is deliberate. The damage a leaked CWD does is to OTHER
    tests, so the floor's job is to stop it propagating; naming every offender is a
    separate cleanup, and one a red suite would not help with. A test that wants to change
    directory for its own duration keeps working, and ``monkeypatch.chdir`` (which reverts
    itself, and whose undo lands on the same value) remains the right tool inside a test.
    """
    _join_install_receipt_workers()
    _join_test_loop_executor(item)
    # LAST, and the order is load-bearing: everything above cancels tasks, joins executor
    # jobs and drains the crew-log writer, and the ``finally`` blocks that run as they
    # unwind APPEND -- which reaches ``note_commit``, and an enqueue after a stop starts a
    # fresh daemon nothing holds a handle to. Stopping the worker once its producers are
    # quiescent is what makes the stop final.
    wedged_eager_fold = _retire_crew_log_eager_fold()
    # After the fold worker is stopped: it is the fold that arms these, so a write armed
    # by its last entries is cancelled here rather than re-armed after the cancel.
    _cancel_session_tree_checkpoints()
    # After every step above: a cancelled task's ``finally`` or a drained writer can
    # itself call ``get_recorder()`` and schedule a consent re-read, and a worker
    # scheduled by teardown must still be joined under this test's pins.
    wedged_consent_worker = _join_telemetry_consent_worker()
    resolved_real_home = (
        _resolved_real_default_home() if item.stash.get(_HOME_PIN_ARMED, False) else None
    )
    _restore_session_cwd()
    yield
    _publish_runner_timeout(None)
    try:
        refused_network = _determinism_floors_end(item)
    except Exception as exc:  # noqa: BLE001 - a floor bug must not skip the checks below
        refused_network = None
        warnings.warn(f"[determinism floors] end-of-test check failed: {exc!r}", stacklevel=1)
    if resolved_real_home is not None:
        _refuse_a_resolved_real_default_home(resolved_real_home)
    if wedged_eager_fold is not None:
        # Reported AFTER the yield for the reason on _retire_crew_log_eager_fold: fixture
        # finalization has run by here, so failing the test costs nothing but the failure.
        raise wedged_eager_fold
    if wedged_consent_worker is not None:
        raise wedged_consent_worker
    if refused_network is not None:
        raise refused_network


def _restore_session_cwd() -> None:
    if _SESSION_CWD is None:  # pragma: no cover - configure always runs first
        return
    try:
        if os.getcwd() == _SESSION_CWD:
            return
    except OSError:
        # The CWD was deleted under us, so the comparison itself raises. Getting back to a
        # real directory is the whole point, so fall through and do it unconditionally.
        pass
    try:
        os.chdir(_SESSION_CWD)
    except OSError:  # pragma: no cover - the starting directory would have to be gone
        pass


# ── the sandbox probe cache is warm for every test, in every testpath ──


#: The verdict this worker's ONE real probe produced, so a later test that finds the
#: cache cold can be handed the same answer instead of paying another probe. ``None``
#: means "not established" -- either the probe has not run yet, or it came back
#: transient and left nothing to cache.
_probe_verdict: str | None = None
_probe_attempted = False


#: The session's own ``time.monotonic``, ``time.sleep`` and ``asyncio.sleep``, taken in
#: ``pytest_sessionstart`` (after every plugin's configure, before any test can patch
#: them): what :func:`_publish_runner_timeout` hands back to ``kiro_crew.testing.wait``.
_WAIT_CLOCKS: tuple | None = None


def _publish_runner_timeout(seconds: float | None) -> None:
    """Hand the running test's timeout, and the real clocks, to ``kiro_crew.testing.wait``.

    Set at setup and cleared after teardown, so a wait in a fixture's teardown still
    sees it. The module is imported here once its package is loaded, which is cheap
    (only ``wait`` itself and ``kiro_crew.loop_lock``) and lets a test that imports
    ``wait`` inside its body see its own timeout. The package itself is never
    imported here: its ``__init__`` loads the gateway harness, which must not happen
    under one test's pinned home. A test in a suite that never loaded
    ``kiro_crew.testing`` waits on the module's fallback instead.

    The module captures the clocks it waits with when it is first imported, so a test
    that patched ``time.monotonic`` and then imported it first would leave every later
    wait in the worker reading the fake. Writing the session's originals back at each
    setup confines that to the one test.
    """
    wait = sys.modules.get("kiro_crew.testing.wait")
    if wait is None and seconds is not None and "kiro_crew.testing" in sys.modules:
        try:
            wait = importlib.import_module("kiro_crew.testing.wait")
        except ImportError:  # pragma: no cover - a partial checkout
            return
    if wait is not None:
        wait.runner_timeout_secs = seconds
        if _WAIT_CLOCKS is not None:
            wait._monotonic, wait._sleep, wait._asyncio_sleep = _WAIT_CLOCKS


def pytest_runtest_setup(item):
    """Keep ``sandbox._backend`` warm for every test, at one probe per worker, and hand
    the crew log's eager fold worker back to it.

    The teardown floor fences that worker between tests (see
    :func:`_retire_crew_log_eager_fold`), because a wake arriving in that gap comes from
    the previous test's writer and folds against a home nobody pinned. A test is the other
    side of that gap: it is meant to reach the worker, so the fence lifts here.

    ``detect_backend()`` reached from a running event loop with a COLD cache
    deliberately refuses to probe -- the probe forks and waits, which must never
    happen on the loop -- and returns "none" with a self-described transient reason.
    Production fills the cache at gateway boot so that path is not reachable; a
    pytest worker has no boot, so the first async test to spawn through
    ``wrap_argv`` gets a hard ``SandboxUnavailableError`` reading "this host has no
    OS sandbox backend" on a host whose sandbox works perfectly.

    This lived in ``test/conftest.py``, which the in-package app suites never load,
    so the ~1490 tests under ``src/kiro_crew/apps/builtins/*/tests/`` had no warm
    cache at all -- 19 of them failed that way in a full run while every one passed
    when its own file was run alone. Whether an app test file happened to land on an
    xdist worker that had already run something under ``test/`` decided the verdict.

    Acting at SETUP rather than once per session is what makes it order-independent:
    the six ``test_sandbox_*.py`` files reset the cache in their own teardown
    (correctly -- they test the cache), and a session-scoped prewarm cannot undo that
    for whatever runs next on the worker.

    **RESTORING the first verdict, not re-probing.** A probe is not cheap on every
    host and its cost is not portable: it forks and then closes every inherited
    descriptor, which is O(``RLIMIT_NOFILE``) -- measured 1ms at a 1024 limit, 102ms
    at 524288 -- and on macOS it spawns a real ``sandbox-exec``. Re-probing per reset
    took ``test_sandbox_argv.py`` from 3.7s to 18.6s across its 145 tests. The answer
    cannot change within a process, so the second and later tests get the recorded
    one by assignment.

    Probing at most ONCE also bounds the transient case, which is the one that would
    otherwise be unbounded: a transient verdict is deliberately never cached, so
    "probe whenever cold" on a fork-starved host means two forks and a 50ms sleep for
    every test in the run. A probe failure is swallowed either way -- a host genuinely
    without a sandbox must still run the tests that do not need one.
    """
    _determinism_floors_begin(item)
    _resume_crew_log_eager_fold()
    _publish_runner_timeout(_runner_timeout_secs(item))
    global _probe_verdict, _probe_attempted
    try:
        from kiro_crew import sandbox

        if not _probe_attempted:
            _probe_attempted = True
            sandbox.detect_backend()
            _probe_verdict = sandbox._backend
        elif sandbox._backend is None and _probe_verdict is not None:
            sandbox._backend = _probe_verdict
    except Exception:  # pragma: no cover - never let the warm-up fail a test
        pass


# ── report-only determinism floors ────────────────────────────────────


#: Findings of the report-only floors below, for this process: ``(floor, nodeid,
#: detail)`` in arrival order. One aggregated warning at session end names them
#: (:func:`_report_only_floor_summary`); a per-call warning would be retained for the
#: whole session and would not merge in the summary.
_FLOOR_FINDINGS: list[tuple[str, str, str]] = []

#: Entries the summary prints per floor; the counts cover everything.
_FLOOR_SUMMARY_PER_FLOOR = 40


def _floor_report(floor: str, nodeid: str, detail: str) -> None:
    _FLOOR_FINDINGS.append((floor, nodeid, detail))


@pytest.fixture(scope="session", autouse=True)
def _report_only_floor_summary():
    """Warn once per worker with what the report-only floors saw.

    The floors in this section REPORT before they are allowed to fail anyone, so their
    findings can be counted across a full CI run before a strict switch is flipped.
    The warning is raised at session teardown, so it is recorded against the worker's
    last test and forwarded to the controller's warning summary under xdist.
    """
    yield
    if _FLOOR_FINDINGS:
        warnings.warn(_floor_summary(_FLOOR_FINDINGS), stacklevel=1)


def _floor_summary(findings: list[tuple[str, str, str]]) -> str:
    by_floor: dict[str, list[tuple[str, str]]] = {}
    for floor, nodeid, detail in findings:
        by_floor.setdefault(floor, []).append((nodeid, detail))
    lines = ["[determinism floors] report-only findings in this worker:"]
    for floor, rows in sorted(by_floor.items()):
        lines.append(f"  {floor}: {len(rows)} finding(s)")
        for nodeid, detail in rows[:_FLOOR_SUMMARY_PER_FLOOR]:
            lines.append(f"    {nodeid}: {detail}")
        if len(rows) > _FLOOR_SUMMARY_PER_FLOOR:
            lines.append(f"    ... and {len(rows) - _FLOOR_SUMMARY_PER_FLOOR} more")
    return "\n".join(lines)


def _check_zone_drift(item) -> None:
    before = item.stash.get(_TZ_STATE, None)
    if before is None:
        return
    after = _process_zone()
    if after != before:
        _floor_report("time zone", item.nodeid, f"changed the process zone {before} -> {after}")


def _check_pool_leftovers(item) -> None:
    before = item.stash.get(_POOL_STATE, None)
    if before is None:
        return
    for name, (busy, queued) in _pool_activity().items():
        if name not in before:
            _floor_report(
                "shared pool",
                item.nodeid,
                f"left work on executors.{name}: {busy} running, {queued} queued",
            )


@pytest.fixture(autouse=True)
def _kiro_crew_package_attr_floor(request):
    """Restore the ``kiro_crew`` module graph a test inherited; report the test that changed it.

    Compares, at teardown, every ``kiro_crew.*`` entry of ``sys.modules`` the test
    inherited and its parent package's attribute (plus the root package's own globals,
    which an in-process ``importlib.reload(kiro_crew)`` rebinds) with what they were at
    setup, using C-level ``map`` comparisons; only a change takes the slow path. A
    module the test imported for the first time is checked too: one left in
    ``sys.modules`` without its parent attribute is bound back. Reports by default and
    fails under ``KIROCREW_PKG_ATTR_STRICT=1``; restores either way. A module- or
    session-scoped fixture that replaces a module defeats the check for its own tests
    (the table is taken when the test starts), and one created lazily inside a test
    (``request.getfixturevalue``) has its replacement undone at that test's teardown.
    """
    table = _PACKAGE_TABLE
    if sys.modules.get("kiro_crew") is None:
        yield
        return
    if len(sys.modules) != table.modules_len or not table.intact():
        table.rebuild()
    yield
    changed: list[str] = []
    if not table.intact():
        changed = table.restore()
    if len(sys.modules) != table.modules_len:
        changed += _bind_unbound_kiro_crew_modules()
        table.rebuild()
    if not changed:
        return
    detail = "; ".join(changed[:10]) + (
        f" (+{len(changed) - 10} more)" if len(changed) > 10 else ""
    )
    if os.environ.get(_PKG_ATTR_STRICT_ENV) == "1":
        raise AssertionError(
            f"this test changed the kiro_crew module graph (restored): {detail}. Load a "
            "private copy with spec_from_file_location under a unique name instead of "
            "reloading or evicting a shared module (testing-conventions D11)."
        )
    _floor_report("package attrs", request.node.nodeid, detail)


_AUDIT = _AuditState()
_AUDIT_INSTALLED = False


#: The session temp base ``_isolate_tempfile_base`` owns while it is live, else "".
_TMP_CREATOR_BASE = ""

#: Entry name directly under that base -> (nodeid of the test running or last run when
#: ``tempfile`` made it, whether that test was still running, the creating thread's name
#: when it was not the main thread). The thread matters because a thread one test left
#: running can create during a LATER test, which the nodeid alone would blame. Bounded so
#: a suite that makes many bare temp dirs cannot grow it without limit; past the cap a
#: residue entry is simply reported without a creator, as it was before this existed.
_TMP_CREATORS: dict[str, tuple[str, bool, str]] = {}
_TMP_CREATORS_CAP = 200_000


def _note_tmp_creator(args: tuple) -> None:
    """Record which test ``tempfile`` was serving when it made an entry under the base.

    The residue report runs at SESSION teardown, so on its own it can only name the last
    test a worker ran -- almost never the culprit -- and the per-test mode that does name
    it changes timing enough that a leak which shows on one CI run in three never shows
    under it. This answers the same question in the run that leaks, at the cost of a dict
    entry per bare temp entry. Any thread counts, which is the point: an entry made after
    its test finished names a thread or callback that test left running.
    """
    base = _TMP_CREATOR_BASE
    if not base or len(_TMP_CREATORS) >= _TMP_CREATORS_CAP:
        return
    try:
        head, name = os.path.split(args[0])
    except Exception:  # noqa: BLE001 - an audit hook must never break the call it watches
        return
    if head == base:
        thread = threading.current_thread()
        thread_name = "" if thread is threading.main_thread() else thread.name
        _TMP_CREATORS[name] = (_AUDIT.nodeid, _AUDIT.active, thread_name)


def _install_determinism_audit_hook() -> None:
    global _AUDIT_INSTALLED
    if _AUDIT_INSTALLED:
        return
    _AUDIT_INSTALLED = True
    sys.addaudithook(_determinism_audit_hook)


def _determinism_audit_hook(event: str, args: tuple) -> None:
    if event not in _HOOKED_EVENTS:
        return
    if event in _TMP_AUDITED_EVENTS:
        _note_tmp_creator(args)
        return
    state = _AUDIT
    if not state.active:
        return
    try:
        if os.getpid() != state.pid:
            return
        if event.startswith("socket."):
            finding = _network_finding(event, args, state)
        else:
            finding = _kill_finding(event, args, state)
    except Exception:  # noqa: BLE001 - a floor must never break the call it watches
        return
    if finding is None:
        return
    key = finding
    state.seen[key] = state.seen.get(key, 0) + 1
    if key[0] == "network" and state.strict_net and not state.network_ok:
        if not key[1].startswith("udp-route-probe"):  # a datagram connect sends nothing
            raise OffLoopbackNetworkRefused(f"off-loopback network refused in a test: {key[1]}")


def _determinism_floors_begin(item) -> None:
    item.stash[_TZ_STATE] = _process_zone()
    item.stash[_POOL_STATE] = _pool_activity()
    state = _AUDIT
    state.nodeid = item.nodeid
    state.pid = os.getpid()
    state.parent_pid = os.getppid()
    state.network_ok = item.get_closest_marker("real_network") is not None
    state.strict_net = os.environ.get(_NET_STRICT_ENV) == "1"
    state.seen = {}
    state.active = True


def _determinism_floors_end(item) -> AssertionError | None:
    state = _AUDIT
    state.active = False
    seen, state.seen = state.seen, {}
    _check_zone_drift(item)
    _check_pool_leftovers(item)
    refused = []
    for (floor, detail), count in seen.items():
        if floor == "network" and state.network_ok:
            continue
        suffix = f" (x{count})" if count > 1 else ""
        _floor_report(floor, item.nodeid, detail + suffix)
        if floor == "network" and state.strict_net and not detail.startswith("udp-route-probe"):
            refused.append(detail)
    if not refused:
        return None
    return AssertionError(
        f"this test reached past loopback under {_NET_STRICT_ENV}=1: "
        + "; ".join(refused)
        + ". Stub the network at the seam the product calls, or mark the test "
        "@pytest.mark.real_network (testing-conventions D10)."
    )


# ── the system temp directory is host state too ───────────────────────


#: The temp roots THIS process created, captured at creation. The bytecode-mirror prune
#: reads this instead of ``tempfile.gettempdir()``, which by session teardown has been
#: restored to the platform temp root -- a mirror shared with every concurrent run.
_RUN_TEMP_ROOTS: list[str] = []

#: Set only in the process that CREATED the short root, so a worker never removes it.
_SHORT_TMP_ROOT_OWNED: str | None = None


def _install_short_tmp_root() -> None:
    """Mint one run-owned SHORT temp root and publish it for the whole run.

    A handful of fixtures cannot use ``tmp_path``: an ``AF_UNIX`` ``sun_path`` caps the
    bind/connect STRING at 108 bytes on Linux and 104 on macOS, and a path asserted in
    message metadata must not trip ``redact_credentials()`` (a macOS ``tmp_path`` carries
    high-entropy directory ids that do). Those fixtures reached for a literal ``/tmp``,
    which put ~40 anonymous ``/tmp/tmpXXXX`` and ``/tmp/kcsock-XXXX`` directories on the
    host, owned by nobody: the residue guard's allow-list and the hygiene probe both
    recognise the ``kc-pytest-<user>-<pid>-`` stem and nothing else, so a stray directory
    spelled any other way names no run and no test.

    ``tempfile.gettempdir()`` cannot serve here, which is the whole reason this root
    exists separately: under a long ``TMPDIR`` -- the run's own isolated base, a harness
    that pins ``TMPDIR`` under the checkout -- ``<base>/kcsock-xxxxxxxx/gw-prewarm.sock``
    is already past ``sun_path`` before a filename is appended (measured at 122 bytes
    against the 108-byte cap), and ``test_mcp_gateway_transport``'s ``_SUN_PATH_BUDGET``
    test goes red. So the root is created under the PLATFORM temp root, where the path is
    short by construction, and carries the run's own stem so it is attributable.

    Created in ``pytest_configure`` and published through the environment: an xdist worker
    is a child of the controller, inherits the variable, and therefore shares the one root
    instead of minting its own. Only the creating process removes it (``_SHORT_TMP_ROOT_OWNED``).
    """
    global _SHORT_TMP_ROOT_OWNED
    if os.environ.get(_SHORT_TMP_ROOT_ENV):
        return  # an xdist worker (or a nested session): the controller already made it
    parent = None if os.name == "nt" else "/tmp"
    if parent is not None and not os.path.isdir(parent):
        parent = None  # unusual POSIX host; the platform default still satisfies both rules
    try:
        root = tempfile.mkdtemp(prefix=f"{_tmp_root_prefix_for_run()}short-", dir=parent)
    except OSError:
        return  # no short root available; short_tmp_base() falls back to its old behaviour
    # No chmod: ``mkdtemp`` already creates the directory 0o700, which is what this root
    # needs in a world-writable temp dir. Setting it again only invites a permissions
    # linter to argue about a mode the stdlib picked.
    os.environ[_SHORT_TMP_ROOT_ENV] = root
    _SHORT_TMP_ROOT_OWNED = root


def _remove_short_tmp_root() -> None:
    """Remove the short root, but only in the process that created it."""
    global _SHORT_TMP_ROOT_OWNED
    root, _SHORT_TMP_ROOT_OWNED = _SHORT_TMP_ROOT_OWNED, None
    if root:
        shutil.rmtree(root, ignore_errors=True)
        if os.environ.get(_SHORT_TMP_ROOT_ENV) == root:
            del os.environ[_SHORT_TMP_ROOT_ENV]


@pytest.fixture(scope="session", autouse=True)
def _isolate_tempfile_base(tmp_path_factory):
    """Give the run its own ``tempfile`` base, then report and remove what leaked.

    AUTOSDE forbids a bare ``tempfile.mkdtemp()`` / ``TemporaryDirectory()`` whose
    destruction is not registered in the same scope, because those directories
    survive the run and accumulate across runs until they exhaust inodes -- MEASURED
    on this host, a ``/tmp`` tmpfs with a fixed 1,048,576-inode budget starts
    returning ENOSPC to unrelated processes with 90% of its BYTES still free.
    Enforcing that per call site is a contract every new test has to remember, and
    the shape that breaks it is invisible when reading the test:
    ``unittest.TestCase`` tearDown does NOT run when setUp raises, so a ``setUp:
    mkdtemp()`` paired with a ``tearDown: rmtree()`` leaks on every setUp failure --
    and it is the FAILING run, the one nobody is watching closely, that leaves the
    residue.

    Redirecting the base fixes the class instead of the call sites: whatever the suite
    creates without cleaning up lands in one directory this fixture owns, so the teardown
    can both NAME it (residue is still a defect) and REMOVE it (so the accumulation stops
    regardless of whether anyone acts on the report).

    The report WARNS by default and fails under ``KIROCREW_TMP_RESIDUE_STRICT``, which
    CI's ``Backend Tests*`` jobs set -- see ``_TMP_RESIDUE_STRICT_ENV`` for why a local
    run only warns.

    Under ``-n auto`` each xdist worker is its own process, so each gets its own
    root and reports only its own leaks; the controller runs no tests and creates
    none.

    **A run only ever deletes the root it created itself.** There is deliberately no sweep
    of other roots, and that is a design decision rather than an omission. Reclaiming a
    root left by a killed run means deciding that some OTHER directory is abandoned, and
    every available signal for that is unsound: the name can be pre-created by another
    local account, and a pid is meaningless across PID namespaces -- two containers sharing
    a bind-mounted temp directory can each hold the same pid, so "that process is gone" is
    a statement about the wrong namespace and the reward for getting it wrong is deleting
    a live run's data. The platform already owns this job (``systemd-tmpfiles`` on a timer,
    macOS's periodic cleanup, and a tmpfs cleared on reboot), and it owns it with
    information this process does not have. So a run killed before its teardown leaves one
    directory behind for the platform to reclaim -- bounded, and far smaller than the
    375,780-inode-per-run accumulation this redirect removes.

    **Why the platform's own temp dir rather than pytest's ``basetemp``.** Nesting
    under ``basetemp`` would have been tidier -- pytest already prunes it -- but it
    adds ``pytest-of-<user>/pytest-<n>/popen-gw<k>/`` to the front of every
    ``mkdtemp()`` path in the suite, roughly 60 characters. Windows still caps a
    path at 260 unless long paths are enabled, and a macOS ``AF_UNIX`` ``sun_path``
    is capped at ~104 bytes, so that nesting would trade an inode leak for a
    platform-specific path-length failure. A sibling of the platform temp root named
    ``kc-pytest-<pid>`` is SHORTER than what pytest's own ``tmp_path`` already
    hands out, so no existing path gets longer on any platform.

    Two things deliberately stay outside the redirect:

    * ``test/tmpdir_helpers.short_tmp_base()`` forces ``/tmp`` on POSIX, because
      macOS's per-user temp root alone already exceeds the ``AF_UNIX`` cap. Those
      sites clean up after themselves.
    * A test that patches ``tempfile.gettempdir`` or passes its own ``dir=`` still
      wins, as it should.
    """
    # Resolve pytest's basetemp FIRST, and discard the value: the call is what matters.
    # pytest computes basetemp lazily from `tempfile.gettempdir()` on first use, so
    # forcing it now pins it OUTSIDE the redirect below. Skip this and pytest's whole
    # basetemp lands INSIDE the run's temp root, where the teardown here would delete
    # it -- taking with it every failed test's retained `tmp_path`, which
    # `tmp_path_retention_policy = failed` exists to keep -- and adding ~25 characters
    # to every temp path in the suite, straight into the Windows 260-character cap and
    # the macOS AF_UNIX 104-byte cap. That ordering was previously accidental: an
    # unrelated fixture 300 lines above happened to call `mktemp` first.
    tmp_path_factory.getbasetemp()
    previous_tempdir = tempfile.tempdir
    previous_env = {name: os.environ.get(name) for name in _TMP_ENV_VARS}
    parent = pathlib.Path(tempfile.gettempdir())
    base = _create_tmp_root(parent)
    # Record the root at CREATION. The bytecode-mirror prune runs from
    # ``pytest_sessionfinish``, which is AFTER this fixture's finalizer has restored
    # ``tempfile.tempdir``: reading ``tempfile.gettempdir()` there resolves to the platform
    # temp root, and pruning that mirror would delete every concurrent run's bytecode.
    _RUN_TEMP_ROOTS.append(str(base))
    _redirect_tempfile_base(base)
    global _TMP_CREATOR_BASE
    # ``tempfile.tempdir`` verbatim: it is the ``dir`` every bare call joins its name to.
    _TMP_CREATOR_BASE = tempfile.tempdir
    previous_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
    _stop_git_discovery_above_the_temp_roots(base, tmp_path_factory)
    try:
        yield base
    finally:
        _TMP_CREATOR_BASE = ""
        creators = dict(_TMP_CREATORS)
        _TMP_CREATORS.clear()
        if previous_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = previous_ceiling
        tempfile.tempdir = previous_tempdir
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        # The same value the per-test fixture's REGISTRATION was decided on, so the
        # scan depth here cannot disagree with whether that fixture ever ran.
        per_test = _TMP_PER_TEST
        leaked = _tmp_residue(base, per_test=per_test)
        # Removed even when it is empty, and even when the report below raises:
        # leaving the root behind would itself be the accumulation this guards.
        _remove_tree(base)
        if not leaked:
            return
        report = _tmp_residue_report(base, leaked, per_test=per_test, creators=creators)
        if os.environ.get(_TMP_RESIDUE_STRICT_ENV):
            raise AssertionError(report)
        warnings.warn(report, stacklevel=1)


# ── the repository checkout is host state too ─────────────────────────


#: Repository root. This file lives at the rootdir, so its parent IS the root.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent

#: Name prefixes the TEST RUNNER owns, exempt regardless of what git says.
#:
#: These are created by pytest and coverage, not by a test, so they are outside
#: what this guard is looking for. They are also all declared in `.gitignore`
#: (`/.pytest_cache`, `/.coverage`, `/.coverage.*`, `/.cache`), so on a host where
#: `git check-ignore` classifies them this list changes nothing. It exists because
#: that classification proved platform-dependent: MEASURED, the same
#: `.pytest_cache` at the same commit reports ignored on Linux and NOT ignored on
#: the Windows runner, which fired this guard on three shards where every test
#: passed. Matched by prefix rather than exact name because coverage writes
#: per-process files (`.coverage.<host>.<pid>.<rand>`).
_ROOT_RESIDUE_ALLOWED_PREFIXES: tuple[str, ...] = (".pytest_cache", ".coverage", ".cache")


def _runner_owned(name: str) -> bool:
    """Whether *name* is test-runner scratch rather than something a test wrote."""
    return name.startswith(_ROOT_RESIDUE_ALLOWED_PREFIXES)


#: Root listing taken before collection, so only what the RUN adds is reported.
#: A developer's own untracked scratch file must not fail their suite.
_ROOT_BASELINE: set[str] | None = None


def _root_entries() -> set[str] | None:
    """Immediate children of the repository root, or ``None`` if unreadable."""
    try:
        return {child.name for child in _REPO_ROOT.iterdir()}
    except OSError:
        return None


def _not_ignored(names: set[str]) -> list[str] | None:
    """*names* that git would NOT ignore, or ``None`` when git cannot classify.

    Deferring to ``git check-ignore`` rather than a pattern list here keeps this
    guard honest about one thing: ``.pytest_cache``, ``.coverage`` and the build
    trees are already declared ignorable, and duplicating that list would drift.

    ``None`` is a THIRD answer, not a failure. ``check-ignore`` exits 0 when it
    ignored something, 1 when it ignored nothing, and 128 when it could not look
    at all -- a non-git export of the test tree, or a checkout git refuses for
    dubious ownership, which is what a uid mismatch under a mounted volume
    produces. MEASURED: 1 and 128 both come back with EMPTY stdout, so the exit
    code is the only thing that separates "nothing here is ignored" from "I never
    got to look". Reading 128 as the former would report every toolchain artifact
    the run created as residue and fail the whole suite on an environment where
    the question is unanswerable. A guard that cries wolf gets deleted, and then
    it protects nothing -- so the caller reports that it could not check and
    leaves the verdict alone.
    """
    if not names:
        return []
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(_REPO_ROOT),
            input="\n".join(sorted(names)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    ignored = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return [name for name in sorted(names) if name not in ignored]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot the repository root, on the controller only.

    Under ``-n auto`` every worker shares this filesystem, so letting each one
    snapshot and report would turn a single stray file into one failure per
    worker. The controller's session brackets all of them, which is exactly the
    window this guard wants.
    """
    global _ROOT_BASELINE, _WAIT_CLOCKS
    _WAIT_CLOCKS = (time.monotonic, time.sleep, asyncio.sleep)
    if hasattr(session.config, "workerinput"):
        return
    _ROOT_BASELINE = _root_entries()


def _prune_bytecode_mirror_of_this_runs_temp_roots(session: pytest.Session) -> None:
    """Drop the ``sys.pycache_prefix`` mirror trees keyed on THIS run's temp roots.

    :func:`_redirect_bytecode_cache` sends every ``.pyc`` to a per-user cache mirror, and
    its "the cache persists so warm imports stay warm" argument holds for sources in the
    CHECKOUT, whose absolute paths are stable. It does not hold for a source under
    ``tmp_path`` or the run's isolated temp base: those paths are new on every run, so the
    mirror gains one dead tree per run that nothing ever reads and nothing owns. Measured
    on one developer host before this guard: 14,816 orphaned ``.pyc`` files, 5.3 GB, across
    161 dead run roots.

    The suite compiles throwaway sources deliberately (``load_app_module``, a skill script
    imported by path, a packaging step's precompile), so the answer is not to stop writing
    bytecode -- it is that the writer must own the retirement of what it wrote. Scoped to
    the roots this run created, so a concurrent run's mirror is never touched.
    """
    prefix = getattr(sys, "pycache_prefix", None)
    if not prefix:
        return
    roots: list[str] = []
    try:
        roots.append(str(session.config._tmp_path_factory.getbasetemp()))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no basetemp was ever materialised
        pass
    for candidate in (*_RUN_TEMP_ROOTS, os.environ.get(_SHORT_TMP_ROOT_ENV) or ""):
        if candidate:
            roots.append(candidate)
    for root in roots:
        try:
            absolute = os.path.abspath(root)
        except OSError:  # pragma: no cover - unreadable cwd
            continue
        # The mirror path is the prefix plus the source's absolute path with its leading
        # separator dropped; on Windows the drive colon is replaced the same way CPython
        # does, so the join is done from the parts rather than by string surgery.
        drive, tail = os.path.splitdrive(absolute)
        mirrored = os.path.join(prefix, drive.replace(":", "") + tail.lstrip(os.sep))
        if os.path.isdir(mirrored):
            shutil.rmtree(mirrored, ignore_errors=True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run when the suite left new, non-ignored entries at the root.

    A test writes there without any ``touch`` or ``open`` in its own source: a
    child process inherits pytest's CWD, which is the repository root, so a
    subprocess spawned without ``cwd=`` puts every relative write into the
    checkout. That is invisible to a reviewer reading the test, it survives the
    run, and an empty file produced this way has already been committed and
    shipped. Detected here rather than cleaned: deleting an unexpected file is
    not this guard's call to make.

    Also releases the xdist worker slots this run holds, because a module may
    define ``pytest_sessionfinish`` only once and appending a second definition
    silently shadows this guard. Done FIRST, and before the early return below:
    the kernel would drop the locks at process exit anyway, but returning capacity
    at the end of the run rather than at interpreter teardown is the whole point,
    and the early return would otherwise skip it.
    """
    try:
        import xdist_budget

        xdist_budget.release_worker_slots()
    except ImportError:  # pragma: no cover - partial checkout
        pass

    _remove_short_tmp_root()
    _prune_bytecode_mirror_of_this_runs_temp_roots(session)

    # ── Windows ProactorEventLoop teardown cleanup (#4764) ─────────────────
    # On Windows + Python 3.12, asyncio.run() creates and closes a
    # ProactorEventLoop each call. The closed loop's IocpProactor leaves
    # pending I/O Completion Port handles whose __del__ methods fire during
    # interpreter shutdown and call loop.call_soon() on the closed loop,
    # raising "RuntimeError: Event loop is closed". This unraisable exception
    # writes to stderr and causes the xdist worker process to exit with code 1
    # — failing the CI shard with no named test failure.
    #
    # Fix: before the worker exits, ensure a FRESH open event loop is set as
    # current, run a gc.collect() to drain pending finalizers while the loop
    # is still usable, then install an atexit handler that keeps a usable loop
    # available during interpreter finalization.
    if sys.platform == "win32":
        _drain_windows_proactor_finalizers()

    if hasattr(session.config, "workerinput") or _ROOT_BASELINE is None:
        return
    current = _root_entries()
    if current is None:
        return
    added = {name for name in current - _ROOT_BASELINE if not _runner_owned(name)}
    residue = _not_ignored(added)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if residue is None:
        # Said out loud rather than skipped silently: if this ever starts
        # happening in CI, the guard has stopped working and the log is the only
        # place that would say so.
        if reporter is not None:
            reporter.write_line(
                "repository-root residue check skipped: git could not classify "
                f"{_REPO_ROOT} (not a checkout, or refused)"
            )
        return
    if not residue:
        return
    # Only promote a clean run to a failure. A non-zero *exitstatus* already
    # carries a more specific verdict than "tests failed" -- INTERRUPTED (2) and
    # INTERNAL_ERROR (3) tell a caller the run did not complete, and overwriting
    # either with TESTS_FAILED would report a finished, failing suite instead.
    # The residue is reported either way, since it is real regardless of how the
    # run ended.
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if reporter is None:
        return
    reporter.write_sep("=", "repository root residue", red=True)
    reporter.write_line(
        f"{len(residue)} new entr{'y' if len(residue) == 1 else 'ies'} at "
        f"{_REPO_ROOT}, left behind by this run:"
    )
    for name in residue:
        reporter.write_line(f"    {name}")
    reporter.write_line("")
    reporter.write_line(
        "A test must not write into the checkout. The usual cause is a "
        "subprocess spawned without cwd=: it inherits pytest's CWD, which is "
        "this directory, so every relative write lands here. Pass "
        "cwd=<a directory under tmp_path> to the spawn, and scope any assertion "
        "about the file to where that child actually ran."
    )


# ── Check-run annotations that name the failing test (issue #7296) ──────────
#
# A red shard is read from its check run's ANNOTATIONS, not from the log: that
# is what the PR page shows, what a fork contributor sees, and all that survives
# in a triage report. Nothing in this repo wrote one, so the annotations came
# from the `python` problem matcher that `actions/setup-python` registers by
# default, whose pattern is a traceback frame (`File "...", line N, in f`)
# followed by `raise SomeError('msg')`.
#
# MEASURED on the six Backend Tests jobs cited in #7296: that pattern matched
# pytest's WARNINGS SUMMARY every time and a pytest failure not once. All six
# reds were annotated only `Event loop is closed` at line 545 -- which is
# `asyncio/base_events.py:545` inside `_check_closed`, reached from a
# `PytestUnraisableExceptionWarning` about a garbage-collected coroutine, i.e. a
# WARNING. The reds themselves were ordinary named failures (a `git add`
# timeout, a missing diag.jsonl, sandbox-dependent project tests) and appeared
# in no annotation at all. Four unrelated PRs were triaged as an event-loop
# teardown flake on the strength of that, and the class had been "fixed" four
# times before.
#
# Replacing the matcher with a better matcher is not the fix. `--color=yes` is
# in the addopts, so the summary line a matcher would have to scrape reads
# `\x1b[31mFAILED\x1b[0m test/x.py::\x1b[1mtest_y\x1b[0m - AssertionError: ...`
# with escape sequences INSIDE the node id. The report objects already carry the
# path, the line and the node id as data, so the annotation is emitted from them
# here and nothing is parsed back out of rendered output.


def pytest_terminal_summary(
    terminalreporter: object, exitstatus: int, config: pytest.Config
) -> None:
    """Emit one ``::error`` annotation per failing test. See the note above.

    Controller-only: under xdist a worker's reports are sent back and counted
    here, so annotating from the worker too would double every line. Gated on
    ``GITHUB_ACTIONS`` because outside Actions these lines are noise nothing
    interprets, and a green run writes nothing at all.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    if hasattr(config, "workerinput"):  # pragma: no cover - xdist worker
        return
    stats = getattr(terminalreporter, "stats", None) or {}
    reports = [report for key in ("failed", "error") for report in stats.get(key, [])]
    if not reports:
        return
    write = getattr(terminalreporter, "write_line", None)
    if write is None:  # pragma: no cover - no terminal reporter (e.g. -p no:terminal)
        return
    for report in reports[:_MAX_ANNOTATED_FAILURES]:
        nodeid = str(getattr(report, "nodeid", "") or "") or "<unknown test>"
        path, line = _report_location(report)
        properties = []
        if path:
            properties.append(f"file={_gha_escape(path, is_property=True)}")
        if line is not None:
            properties.append(f"line={line}")
        properties.append(f"title={_gha_escape(nodeid, is_property=True)}")
        message = _gha_escape(f"{nodeid} - {_failure_reason(report)}", is_property=False)
        write(f"::error {','.join(properties)}::{message}")
    hidden = len(reports) - _MAX_ANNOTATED_FAILURES
    if hidden > 0:
        write(
            f"::notice::{len(reports)} tests failed or errored on this job; the first "
            f"{_MAX_ANNOTATED_FAILURES} are annotated. The remaining {hidden} are in "
            "this job's short test summary."
        )
