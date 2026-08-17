"""Repo-root pytest configuration: the host-mutation floor.

``test/conftest.py`` holds the bulk of the suite's isolation, but it only applies
to ``test/``. ``[tool:pytest] testpaths`` also collects ``transfer`` and
``src/kiro_crew/apps/builtins`` (~108 test modules that ship inside the package,
next to the code they cover), and those get no ``test/conftest.py`` fixtures at
all -- only this file, plus that app's own ``tests/conftest.py`` where one exists.
Anything that must hold for EVERY test therefore has to live here, at the
rootdir, which is the one conftest pytest applies to all three testpaths.

Only the HOST-MUTATION FLOOR belongs in this file: the guards that must hold for a
test collected from any of the three testpaths, because what they protect is the
developer's machine rather than the correctness of one suite. Everything that is
merely suite-specific isolation stays in ``test/conftest.py``.

The floor has four parts, and each one exists because the "remember to isolate
this" contract failed at least once:

* **Services.** ``$XDG_CONFIG_HOME`` is redirected and the stdlib spawn funnels
  refuse a ``systemctl``/``launchctl`` invocation carrying a mutating verb, so no
  test can reconfigure or restart the operator's real gateway (issue #1722).
* **The data home.** ``KIROCREW_HOME`` is pinned per test, and the ``~/.kiro``
  paths that production binds at IMPORT time (which the env var cannot reach) are
  pinned with it. Without this, the ~108 test modules that ship inside the package
  under ``src/kiro_crew/apps/builtins/*/tests/`` -- which see this conftest and no
  other -- write the operator's live ``~/.kiro/crew`` the moment they touch
  ``config_dir()``.
* **The system temp directory.** ``tempfile``'s base is redirected to a per-run
  directory for the whole process, so a bare ``mkdtemp()`` whose cleanup is missing
  or skipped leaves its directory somewhere this run owns and removes, instead of
  accumulating in the shared temp root forever. What was left behind is REPORTED
  first, so the leak is a red rather than silent inode consumption.
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

Imports at MODULE level are stdlib + pytest only, on purpose: a rootdir conftest is
imported before every collection, so pulling ``kiro_crew`` in here would make the
whole suite depend on import-time side effects of the package under test. The
fixtures that do need ``kiro_crew`` import it in their own body, which runs at test
setup -- by which point the test module has already imported the package anyway --
and tolerate an ImportError so a partial checkout cannot break collection.
"""

from __future__ import annotations

import asyncio.base_events
import contextlib
import getpass
import importlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings

import pytest

#: Service managers whose *mutating* subcommands reconfigure, start, or stop a
#: real service. Matched on BASENAME against every token of the argv, not just
#: ``argv[0]``, because in this codebase the interesting name is usually not
#: first: ``dev_fleet._run_cmd`` rewrites ``argv[0]`` to a trusted absolute path
#: and then routes the spawn through ``sandboxed_spawn_argv``, and
#: ``SystemdBackend.restart_detached`` invokes through ``systemd-run``, so the
#: real program ends up in the middle of the final argv behind a wrapper.
#:
#: Deliberately NOT here:
#:
#: * ``systemd-run`` — in this codebase it is not a service-control tool at all.
#:   ``sandbox`` wraps essentially EVERY subprocess in
#:   ``systemd-run --user --scope --slice=kirocrew-agents.slice -p MemoryMax=…``
#:   to apply cgroup resource limits, so denying it would refuse an ordinary
#:   ``git config`` spawn. Its one service-control use (``restart_detached``)
#:   passes ``systemctl restart`` as the wrapped command, which this guard
#:   catches on the inner token instead.
#: * ``sudo`` — a privilege prefix, not an action. Whether the spawn mutates
#:   anything is decided by the command it wraps, and ``sudo systemctl restart``
#:   is already caught on ``systemctl``.
_SERVICE_MANAGERS = frozenset({"systemctl", "launchctl"})

#: Subcommands of the managers above that CHANGE host service state.
#:
#: The verb matters as much as the binary. ``systemctl show``, ``systemctl cat``
#: and ``systemctl is-active`` are read-only queries, and tests legitimately run
#: them through the sandbox to inspect the environment they are running in — a
#: guard keyed on the binary alone would fail those for no safety gain. Only the
#: verbs below actually write.
_MUTATING_VERBS = frozenset(
    {
        # systemctl
        "start",
        "stop",
        "restart",
        "try-restart",
        "reload",
        "reload-or-restart",
        "try-reload-or-restart",
        "daemon-reload",
        "daemon-reexec",
        "enable",
        "disable",
        "reenable",
        "mask",
        "unmask",
        "preset",
        "revert",
        "set-property",
        "set-environment",
        "unset-environment",
        "import-environment",
        "edit",
        "link",
        "isolate",
        "kill",
        # launchctl
        "load",
        "unload",
        "bootstrap",
        "bootout",
        "kickstart",
        "remove",
        "submit",
        "setenv",
        "unsetenv",
        "attach",
    }
)

#: Programs that have no read-only mode worth allowing in a test: any invocation
#: rewrites host policy.
_ALWAYS_REFUSED = frozenset({"apparmor_parser"})

#: Test modules permitted to really mutate host service state.
#:
#: EMPTY, and it should stay that way. Every suite that exercises these paths
#: today already stubs them — ``test_service.py`` patches
#: ``service.linux.subprocess.run`` / ``service.macos.subprocess.run``,
#: ``test_pod.py`` and ``test_pod_launchd.py`` stub at the module boundary, and
#: the ``dev_fleet`` make-live tests stub ``_run_cmd``. So this guard breaks no
#: existing test, and an addition here means a test is about to restart a real
#: service on whoever runs the suite. Same shape as ``_ALLOWED`` in
#: ``test/test_spawn_preexec_guard.py``: an entry needs a comment saying why the
#: host mutation is acceptable.
_HOST_SERVICE_EXEC_ALLOWED_MODULES: frozenset[str] = frozenset()


def _tokens(argv: object, *, shell: bool = False) -> list[str]:
    """Normalise every spawn-API argv shape into a list of string tokens.

    Accepts a string (shell form, or a lone program), a ``PathLike``, or a
    sequence of either. Anything uninterpretable yields no tokens: this guard
    refuses on a POSITIVE match only, so an exotic argv shape can never turn into
    a spurious failure in an unrelated suite.
    """
    if isinstance(argv, (str, bytes, os.PathLike)):
        raw = os.fsdecode(argv)
        return raw.split() if shell else [raw]
    if isinstance(argv, (list, tuple)):
        out = []
        for item in argv:
            if isinstance(item, (str, bytes, os.PathLike)):
                out.append(os.fsdecode(item))
        return out
    return []


def _basename(token: str) -> str:
    # PurePath handles both separators, so a Windows C:\...\sc.exe form and a
    # POSIX /usr/bin/systemctl normalise the same way.
    return pathlib.PurePath(token.replace("\\", "/")).name


def _refusal_reason(argv: object, *, shell: bool = False) -> str | None:
    """Describe why *argv* mutates host service state, or ``None`` if it does not.

    A service manager alone is not enough — the argv must also carry a mutating
    verb AFTER the manager token. Scanning only the tail keeps a unit named after
    a verb, or a wrapper flag, from being read as the action.
    """
    tokens = _tokens(argv, shell=shell)
    for index, token in enumerate(tokens):
        name = _basename(token)
        if name in _ALWAYS_REFUSED:
            return f"{name!r} rewrites host security policy"
        if name not in _SERVICE_MANAGERS:
            continue
        for candidate in tokens[index + 1:]:
            if _basename(candidate) in _MUTATING_VERBS:
                return f"{name} {candidate!r} changes host service state"
    return None


def _refuse(reason: str, argv: object) -> None:
    """Fail the test with the stub it is missing, not just 'permission denied'."""
    raise AssertionError(
        f"Test tried to run a command that mutates host service state: {reason} "
        f"(see issue #1722).\n"
        f"  argv: {argv!r}\n"
        f"This spawn must be stubbed. Depending on the code under test:\n"
        f"  - dev_fleet make-live: stub BOTH `_run_cmd` and `_dropin_path`\n"
        f"  - kiro_crew.service.*: patch `service.<platform>.subprocess.run`\n"
        f"  - pod runtime: stub the runtime's `systemctl` / `launchctl` helper\n"
        f"Read-only queries (`systemctl show`, `cat`, `is-active`) are allowed and "
        f"need no stub.\n"
        f"If this test genuinely must drive a real service, add its module to "
        f"_HOST_SERVICE_EXEC_ALLOWED_MODULES in the root conftest.py with a "
        f"comment explaining why."
    )


@pytest.fixture(scope="session")
def _xdg_config_root(tmp_path_factory):
    """One tmp dir per session (per xdist worker) to stand in for ``~/.config``.

    Session-scoped so the redirect below costs one ``mkdir`` for the whole run
    rather than one per test: nothing here is written by a passing test — the
    point of the guard is that these writes should not happen at all — so a
    per-test directory would isolate nothing and add a syscall to every test.
    """
    return tmp_path_factory.mktemp("xdg")


@pytest.fixture(autouse=True)
def _isolate_xdg_config_home(_xdg_config_root, monkeypatch):
    """Point ``$XDG_CONFIG_HOME`` at a tmp dir so no test writes a real unit file.

    ``dev_fleet._dropin_path()`` resolves the make-live systemd drop-in as
    ``$XDG_CONFIG_HOME/systemd/user/kirocrew-gateway.service.d/make-live.conf``,
    falling back to ``~/.config`` when the variable is unset — which is the
    default on most developer machines and in CI. So a test that reaches the
    cutover path without stubbing ``_dropin_path`` writes the operator's real
    drop-in. That is what took the gateway down in #1722.

    Redirecting the variable fixes the whole class rather than one call site,
    because the production code already honours it (its docstring notes a literal
    ``~/.config`` would be the wrong directory on a host that sets XDG). It is
    also not dev-fleet-specific: ``pptx_maker/backend/paths.py`` resolves against
    the same variable, so its tests stop touching the real config dir too.

    A test that wants its own value still wins — it sets XDG later, and reverts
    independently.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_xdg_config_root))


@pytest.fixture(autouse=True)
def _isolate_launchd_paths(_xdg_config_root, monkeypatch):
    """Pin the launchd install paths, which ``$XDG_CONFIG_HOME`` cannot reach.

    The macOS half of Make Live does not resolve through XDG at all. Its paths are
    module globals bound at IMPORT time from ``Path.home()``::

        PLIST_DIR    = ~/Library/LaunchAgents
        PLIST_PATH   = PLIST_DIR / "dev.kirocrew.gateway.plist"
        LOG_DIR      = ~/Library/Logs/...        (+ STDOUT_LOG, STDERR_LOG)
        LIVE_PROGRAM = launchd_live_program()    (under ~/Library/Application Support)

    That is the same import-time-binding class the suite already documents (#874):
    an env var read *after* the module captured the path changes nothing, so the
    redirect above leaves the launchd side wide open.

    It is reachable today, not hypothetically. ``macos.install()`` calls
    ``write_live_program(render_live_program(kirocrew_bin()))`` with no path
    argument, so the launcher lands on the real ``LIVE_PROGRAM`` even in a test that
    carefully pinned every ``PLIST_*`` constant — which
    ``test_install_writes_plist_and_loads`` does. Raised in review of #1722.

    Every binding is patched rather than just the canonical one, because both
    consumers import by value: ``dev_fleet.gateway_service`` holds its own
    ``PLIST_PATH`` and its own ``launchd_live_program`` reference, and
    ``LaunchdBackend.live_program()`` calls that function fresh on each use instead
    of reading the constant.

    ``gateway_service`` is patched only when it is already imported. It is a heavy
    module and forcing it in for all ~31k tests would cost more than it protects; a
    test that imports it later binds from the already-patched ``service.macos``, so
    it inherits the tmp paths anyway.

    Tolerant by design: an unimportable module is skipped rather than failing
    collection, and every attribute uses ``raising=False`` so a renamed constant
    does not become a suite-wide error.
    """
    root = pathlib.Path(_xdg_config_root) / "launchd"
    launcher = root / "live-gateway"
    plist_dir = root / "LaunchAgents"
    plist_path = plist_dir / "dev.kirocrew.gateway.plist"
    log_dir = root / "Logs"

    eager: dict[str, dict[str, object]] = {
        "kiro_crew.service.macos": {
            "PLIST_DIR": plist_dir,
            "PLIST_PATH": plist_path,
            "LOG_DIR": log_dir,
            "STDOUT_LOG": log_dir / "gateway.log",
            "STDERR_LOG": log_dir / "gateway.err",
            "LIVE_PROGRAM": launcher,
        },
        "kiro_crew.service.common": {"launchd_live_program": lambda: launcher},
    }
    lazy: dict[str, dict[str, object]] = {
        "kiro_crew.apps.builtins.dev_fleet.gateway_service": {
            "PLIST_PATH": plist_path,
            "launchd_live_program": lambda: launcher,
        },
    }

    for name, attrs in eager.items():
        try:
            imported = importlib.import_module(name)
        except Exception:  # pragma: no cover - a partial checkout must not break collection
            continue
        for attr, value in attrs.items():
            monkeypatch.setattr(imported, attr, value, raising=False)

    for name, attrs in lazy.items():
        already = sys.modules.get(name)
        if already is None:
            continue
        for attr, value in attrs.items():
            monkeypatch.setattr(already, attr, value, raising=False)


@pytest.fixture(autouse=True)
def _block_host_service_mutation(request, monkeypatch):
    """Fail loudly if a test really starts, stops, or reconfigures a service.

    Redirecting ``$XDG_CONFIG_HOME`` above stops a test from *writing* a real
    unit file, but not from *running* ``systemctl --user daemon-reload`` or
    ``systemctl --user restart kirocrew-gateway.service``. Those restart the
    developer's live gateway even when the drop-in they read is pristine, so the
    second half of the floor has to be an execution guard.

    Patched at the deepest stdlib funnels rather than at the names call sites
    happen to use, so the guard cannot be bypassed by import style:

    * ``subprocess.Popen.__init__`` — every sync spawn funnels here, including
      ``run``, ``check_call`` and ``check_output``, and including a module that
      did ``from subprocess import run`` (that ``run`` still resolves ``Popen``
      through its own module globals).
    * ``BaseEventLoop.subprocess_exec`` / ``subprocess_shell`` — every
      ``asyncio.create_subprocess_*`` funnels here, so patching these also covers
      ``from asyncio import create_subprocess_exec``.
    * ``os.execve`` — ``live_target.maybe_reexec()`` execs into another checkout,
      which would REPLACE the pytest process with a real gateway. Guarded
      unconditionally: no argv inspection makes that sane in a test.

      ``os.execv`` is deliberately NOT guarded. The two exec paths in this
      codebase use different funnels, and the difference is load-bearing:
      ``maybe_reexec`` needs ``execve`` because it hands the gateway a modified
      environment across the exec, while ``spawn/exec_shim`` uses ``execv``
      precisely because it passes its inherited environment through untouched
      ("execv, not execve: the environment this process was given IS the
      environment the caller built"). The shim's exec also happens in a CHILD
      process (it is spawned as ``python -c <shim source>``), so trapping
      ``execv`` here protects nothing and only breaks the shim's in-process unit
      tests of its own 127-on-exec-failure contract.
      ``test_host_service_guard.py`` ratchets the assumption that
      ``live_target`` still execs through ``execve``.

    The mechanism follows ``test/test_update_git_guard.py``, which already
    monkeypatches ``create_subprocess_exec`` to raise on an unwanted spawn. The
    difference is scope: this one is autouse, so it holds for tests nobody
    thought to write a guard for.

    Everything else is delegated to the real implementation untouched — which
    matters more than it sounds, because ``sandbox`` wraps nearly every spawn in
    this codebase in ``systemd-run --scope`` for cgroup limits. A guard keyed on
    binaries rather than verbs refused ``git config``.
    """
    if request.module is not None and request.module.__name__ in _HOST_SERVICE_EXEC_ALLOWED_MODULES:
        return

    real_popen_init = subprocess.Popen.__init__
    real_exec = asyncio.base_events.BaseEventLoop.subprocess_exec
    real_shell = asyncio.base_events.BaseEventLoop.subprocess_shell

    def guarded_popen_init(self, args=(), *rest, **kwargs):
        reason = _refusal_reason(args, shell=bool(kwargs.get("shell")))
        if reason:
            _refuse(reason, args)
        return real_popen_init(self, args, *rest, **kwargs)

    def guarded_subprocess_exec(self, protocol_factory, program=None, *args, **kwargs):
        argv = [program, *args]
        reason = _refusal_reason(argv)
        if reason:
            _refuse(reason, argv)
        return real_exec(self, protocol_factory, program, *args, **kwargs)

    def guarded_subprocess_shell(self, protocol_factory, cmd=None, **kwargs):
        reason = _refusal_reason(cmd, shell=True)
        if reason:
            _refuse(reason, cmd)
        return real_shell(self, protocol_factory, cmd, **kwargs)

    def guarded_exec(*argv, **kwargs):
        raise AssertionError(
            "Test called os.execve, which would REPLACE the pytest process with "
            f"another checkout's gateway (see issue #1722). argv: {argv!r}\n"
            "Stub the re-exec seam instead (e.g. live_target.maybe_reexec)."
        )

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop, "subprocess_exec", guarded_subprocess_exec
    )
    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop, "subprocess_shell", guarded_subprocess_shell
    )
    monkeypatch.setattr(os, "execve", guarded_exec)


# ── the process working directory is shared state too ─────────────────


#: The working directory pytest started in, captured once. Restoring to THIS rather than
#: to a per-test snapshot is both simpler and more correct: it is the only value that is
#: certain to still exist, and it is where every test expects to begin.
_SESSION_CWD: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Record the working directory pytest started in, before any test can move it."""
    global _SESSION_CWD
    try:
        _SESSION_CWD = os.getcwd()
    except OSError:  # pragma: no cover - pytest could not have started here
        _SESSION_CWD = str(_REPO_ROOT)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """Put the process working directory back, BEFORE any fixture teardown runs.

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
    return {
        t for t in threading.enumerate() if _OTEL_THREAD_MARKER in t.name and t.is_alive()
    }


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


# ── the sandbox probe cache is warm for every test, in every testpath ──


#: The verdict this worker's ONE real probe produced, so a later test that finds the
#: cache cold can be handed the same answer instead of paying another probe. ``None``
#: means "not established" -- either the probe has not run yet, or it came back
#: transient and left nothing to cache.
_probe_verdict: str | None = None
_probe_attempted = False


def pytest_runtest_setup(item):
    """Keep ``sandbox._backend`` warm for every test, at one probe per worker.

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


# ── tracked Windows gaps apply to every testpath ──────────────────────


def pytest_collection_modifyitems(config, items):
    """On Windows, skip the tracked known-gap tests (all parametrizations).

    The list lives in ``test/windows-expected-failures.txt`` -- one unparametrized node
    id per line, captured from the first Windows CI runs. It is a burn-down backlog:
    fixed tests get their line deleted, and anything NOT on the list still fails the
    job, so the Windows line holds for the tests that pass today.

    Lives HERE rather than in ``test/conftest.py`` because the list already names node
    ids under ``src/kiro_crew/apps/builtins/auto_improvement/tests/``, and a hook rooted
    at ``test/`` is never registered when only in-package tests are collected -- which is
    exactly what CI's reduced-scope Windows job does when a diff touches no path under
    ``test/``. Those entries are also absent from ``BACKEND_DESELECTS``, so they were
    collected unskipped and the shard went red for a gap that was already tracked.

    The list file itself stays under ``test/``, read by path from here. Node ids are
    always spelled with ``/`` even on Windows, so the in-package entries need no
    translation.
    """
    if not platform_compat_or_none() or not platform_compat_or_none().IS_WINDOWS:
        return
    listfile = _REPO_ROOT / "test" / "windows-expected-failures.txt"
    try:
        text = listfile.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - list file absent in a partial checkout
        return
    expected = {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}
    marker = pytest.mark.skip(
        reason="known Windows gap -- tracked in test/windows-expected-failures.txt"
    )
    for item in items:
        if item.nodeid.split("[")[0] in expected:
            item.add_marker(marker)


def platform_compat_or_none():
    """``kiro_crew.platform_compat``, or ``None`` when it cannot be imported.

    Imported lazily so this rootdir conftest keeps its module-level imports to the
    stdlib: it is loaded before every collection, and a module-scope import of the
    package under test would make the whole suite depend on that package's import-time
    side effects.
    """
    try:
        from kiro_crew import platform_compat
    except ImportError:  # pragma: no cover - partial checkout
        return None
    return platform_compat


# ── the system temp directory is host state too ───────────────────────


#: Prefix for the run's own temp base, a sibling of the platform temp root.
#:
#: The name is ``kc-pytest-<user>-<pid>``. The pid is what lets a later run tell an
#: ABANDONED root (its process is gone) from one a concurrent run is still using. The
#: user segment is not decoration: on POSIX the platform temp root is SHARED between
#: accounts, so a bare pid collides across users -- two accounts can hold the same pid
#: at the same time, and the second would try to reuse a directory it cannot write.
#: Windows gives each account its own temp root, so there the segment is redundant and
#: harmless.
_TMP_ROOT_PREFIX = "kc-pytest-"


def _tmp_root_prefix_for_run() -> str:
    """``kc-pytest-<user>-<pid>-`` -- the stem this run's temp root is created under.

    The user segment is not decoration: on POSIX the platform temp root is SHARED between
    accounts, so a bare pid collides across users -- two accounts can hold the same pid at
    the same time. The pid is what lets a later run tell an ABANDONED root from one a
    concurrent run is still using. The trailing hyphen is where ``mkdtemp`` appends its
    random component; see :func:`_create_tmp_root` for why that randomness is required and
    not cosmetic.
    """
    try:
        raw = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry and no env fallback
        raw = "u"
    user = "".join(ch if ch.isalnum() else "_" for ch in raw)[:24] or "u"
    return f"{_TMP_ROOT_PREFIX}{user}-{os.getpid()}-"


def _create_tmp_root(parent: pathlib.Path) -> pathlib.Path:
    """Create this run's temp root under *parent*, atomically and unguessably.

    ``mkdtemp`` rather than ``mkdir(exist_ok=True)`` on a name derived from the pid, and
    the difference is a local privilege boundary rather than a style choice. The platform
    temp root is world-writable with a sticky bit on POSIX, and a pid-derived name is
    PREDICTABLE -- so another local account can pre-create that exact name as a SYMLINK to
    a directory it controls. ``mkdir(..., exist_ok=True)`` succeeds against a
    symlink-to-directory, the session redirect then follows it, and every temp write in
    the run -- including whatever secrets a test fixture fabricates -- lands somewhere the
    other account chose and can read.

    ``mkdtemp`` closes that in three ways at once: it appends a random component, so the
    name cannot be guessed ahead of time; it creates with ``O_EXCL``, so it fails rather
    than adopting anything that already exists; and it sets mode ``0o700``, so the
    directory is unreadable to other accounts once made. It also makes the name
    UNPREDICTABLE to this suite, which is what lets the invariant below hold: a run can
    only ever name the root it created itself.

    The pid stays in the name for a human reading a stray directory, not for machinery:
    nothing parses it. See :func:`_isolate_tempfile_base` for why this run never touches a
    root it did not create.
    """
    return pathlib.Path(tempfile.mkdtemp(prefix=_tmp_root_prefix_for_run(), dir=parent))


#: Env vars ``tempfile`` consults, so a CHILD process inherits the redirect too.
#: A test that spawns a helper which writes to its temp dir would otherwise put
#: that file in the real ``/tmp``, where nothing prunes it.
_TMP_ENV_VARS = ("TMPDIR", "TEMP", "TMP")

#: Opt-in diagnostic: give every test its OWN temp base, named after the test.
#:
#: Off by default because a directory per test is precisely the per-test cost the
#: suite's fixture audit exists to avoid (paid ~26.5k times). It is the escape hatch
#: for the one question the session-scoped guard below cannot answer: a single stray
#: directory in a 26k-test run names no culprit. Re-run the suspect subset with
#: ``KIROCREW_TMP_PER_TEST=1`` and the residue's parent directory IS the test id.
_TMP_PER_TEST_ENV = "KIROCREW_TMP_PER_TEST"

#: Names under the run's temp base that are NOT this suite's residue.
#:
#: Each entry is something OTHER than a test creating a directory it forgot to remove,
#: which is the only thing this guard is about. Measured from CI, where the surfaces this
#: developer host cannot reach are exercised:
#:
#: * ``pytest-of-`` -- a NESTED pytest's own ``basetemp`` tree. Several tests spawn one,
#:   and it resolves ``gettempdir()`` after the redirect has taken effect, so it computes
#:   its basetemp inside ours. A child runner's bookkeeping, with its own retention.
#: * ``kirocrew-computer-shots`` -- the computer-use screenshot spool, which production
#:   deliberately keeps under ``tempfile.gettempdir()`` as a persistent ring buffer
#:   (pinned by ``test_computer_use_capture.py``). Long-lived BY DESIGN, so its presence
#:   is the feature working, not a test leaking.
#: * ``playwright-`` / ``.org.chromium.`` -- created by the browser and its driver, which
#:   inherit the redirected ``TMPDIR`` like any other child. Third-party scratch this
#:   suite does not own and cannot register cleanup for.
_TMP_RESIDUE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "pytest-of-",
    "kirocrew-computer-shots",
    "playwright-",
    ".org.chromium.",
)

#: Make temp residue FAIL the run rather than warn.
#:
#: Off by default, and that is a staged rollout rather than a soft opinion. The guard
#: found real residue on CI surfaces this host cannot reach, and the entries that remain
#: after the exclusions above are single ``mkstemp`` FILES rather than the ``mkdtemp``
#: directories the rule is written about -- one inode each, several of them created by
#: production code a test merely reached. Failing the suite on that set today would block
#: every unrelated change while the set is attributed, and a guard that blocks unrelated
#: work is a guard somebody deletes.
#:
#: So: the residue is removed either way (which is the whole inode win), and it is
#: REPORTED either way. Set this to make it fatal -- in a burn-down branch, or in CI once
#: the remaining set is empty. Same shape as ``windows-expected-failures.txt``: a known
#: set, visible, with a way to hold the line once it is closed.
_TMP_RESIDUE_STRICT_ENV = "KIROCREW_TMP_RESIDUE_STRICT"


def _redirect_tempfile_base(base: pathlib.Path) -> None:
    """Point ``tempfile`` AND every child process at *base*.

    ``tempfile.tempdir`` is the module global ``gettempdir()`` memoises into, so
    assigning it directly is what covers code already holding a reference to the
    module; the env vars are what cover a child process, which re-derives its own.
    Both are needed: patching only the global leaves subprocess writes in the real
    temp dir, and setting only the env vars is a no-op for a process that already
    resolved ``gettempdir()`` once.

    All three names are set because the platforms disagree on which one is real:
    ``TMPDIR`` is the POSIX spelling and ``TEMP``/``TMP`` are what Windows and the
    tools spawned there read. Setting a name the running platform ignores is inert,
    so there is no need to branch.
    """
    base.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(base)
    for name in _TMP_ENV_VARS:
        os.environ[name] = str(base)


def _remove_tree(path: pathlib.Path) -> bool:
    """Delete *path*, defeating Windows read-only files. True when it is gone.

    ``shutil.rmtree(..., ignore_errors=True)`` is the reflexive spelling and it is the
    WRONG one here: on Windows a mode-444 file cannot be unlinked, a git checkout is
    full of them (loose objects are written read-only), and ``ignore_errors`` swallows
    every such failure so the caller reports success over a tree still on disk. Five
    test modules combine a bare ``mkdtemp()`` with a real ``git`` spawn, so that is not
    hypothetical. ``platform_compat.rmtree_force`` clears the attribute and retries, and
    returns a filesystem-derived boolean rather than the hook's opinion.
    """
    try:
        from kiro_crew import platform_compat as _pc
    except ImportError:  # pragma: no cover - partial checkout
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()
    return _pc.rmtree_force(path)


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

    The report WARNS by default and fails only under ``KIROCREW_TMP_RESIDUE_STRICT`` --
    see ``_TMP_RESIDUE_STRICT_ENV`` for why that is a staged rollout and not a shrug.

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
    _redirect_tempfile_base(base)
    try:
        yield base
    finally:
        tempfile.tempdir = previous_tempdir
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        per_test = bool(os.environ.get(_TMP_PER_TEST_ENV))
        leaked = _tmp_residue(base, per_test=per_test)
        # Removed even when it is empty, and even when the report below raises:
        # leaving the root behind would itself be the accumulation this guards.
        _remove_tree(base)
        if not leaked:
            return
        report = _tmp_residue_report(base, leaked, per_test=per_test)
        if os.environ.get(_TMP_RESIDUE_STRICT_ENV):
            raise AssertionError(report)
        warnings.warn(report, stacklevel=1)


def _tmp_residue(base: pathlib.Path, *, per_test: bool) -> list[str]:
    """Names left under *base*, excluding what is not a leak.

    In per-test mode the immediate children are the per-test bases the fixture itself
    created, so the scan descends one level and reports ``<test id>/<name>``. Without
    that, every test in the run would be reported as its own leak and the mode would
    answer nothing.
    """
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    residue: list[str] = []
    for child in children:
        if child.name.startswith(_TMP_RESIDUE_ALLOWED_PREFIXES):
            continue
        if not per_test:
            residue.append(child.name)
            continue
        try:
            residue.extend(f"{child.name}/{leaf.name}" for leaf in sorted(child.iterdir()))
        except OSError:
            continue
    return residue


def _tmp_residue_report(base: pathlib.Path, leaked: list[str], *, per_test: bool) -> str:
    """The message naming what the run left behind, and how to find its owner."""
    shown = leaked[:20]
    more = f"    ... and {len(leaked) - len(shown)} more\n" if len(leaked) > len(shown) else ""
    hint = (
        "Each name above is a test id, so the leak is in that test."
        if per_test
        else f"Re-run the suspect subset with {_TMP_PER_TEST_ENV}=1 and each residue "
        f"name becomes the id of the test that leaked it."
    )
    return (
        f"{len(leaked)} temporary entr{'y' if len(leaked) == 1 else 'ies'} outlived "
        f"this run under {base}:\n"
        + "".join(f"    {name}\n" for name in shown)
        + more
        + "\nThis is reported at session teardown, so it is attributed to the last "
        "test this worker ran -- that test is almost certainly NOT the culprit.\n"
        "A test must register the destruction of anything it creates in the SAME "
        "scope. Use pytest's tmp_path, or pair every tempfile.mkdtemp() with "
        "self.addCleanup(shutil.rmtree, path, ignore_errors=True) on the line "
        "after it -- NOT an rmtree in tearDown, which unittest skips entirely when "
        "setUp raises.\n" + hint
    )


@pytest.fixture(autouse=True)
def _isolate_tempfile_base_per_test(_isolate_tempfile_base, request):
    """Opt-in: give this test its own temp base so a leak names its own test.

    Inert unless ``KIROCREW_TMP_PER_TEST`` is set, so the steady-state cost is one
    environment read per test. See ``_TMP_PER_TEST_ENV``.

    Named from the NODEID, not ``node.name``. The bare function name carries no module
    or class, and 807 function names are duplicated across this suite (``test_defaults``
    appears 17 times, ``test_invalid_json_is_400`` 29), so a name-keyed directory would
    report a leak against a name shared by dozens of tests -- answering the wrong
    question in the one mode that exists to answer it precisely. The nodeid is kept
    TAIL-first under the length cap, because the distinguishing part is at the end.
    """
    if not os.environ.get(_TMP_PER_TEST_ENV):
        return
    safe = "".join(
        ch if (ch.isalnum() or ch in "-._") else "_" for ch in request.node.nodeid
    )
    _redirect_tempfile_base(_isolate_tempfile_base / safe[-100:])


# ── the operator's data home is host state too ────────────────────────


@pytest.fixture(scope="session")
def _isolation_root(tmp_path_factory):
    """One session-scoped parent for the per-test isolation dirs below.

    ``tmp_path_factory.mktemp`` picks its numbered suffix by scanning the whole
    basetemp, so its cost grows with the number of entries already there. The
    autouse fixtures that need a directory ``mkdir`` under this root instead, which
    is a single syscall and does not scan.

    Named ``i`` rather than something descriptive to keep the paths short: Windows
    still caps a path at 260 characters unless long paths are enabled, and
    everything a test writes under ``KIROCREW_HOME`` nests inside here.
    """
    return tmp_path_factory.mktemp("i")


@pytest.fixture
def _isolation_dirs(_isolation_root):
    """Return an allocator for this test's isolation PATHS.

    Each call returns ``<root>/<counter>-<name>``, so one test's paths cannot collide
    with another's (the counter is per-process, and pytest-xdist gives every worker
    its own ``basetemp``). Flat rather than nested, to keep Windows paths short.

    **A path, not a directory: nothing is created.** Every consumer only needs somewhere
    to POINT, and creates the directory itself if it ever writes -- ``config_dir()``
    creates the data home, ``create_agent_folder`` creates the subagent registry,
    ``OLLAMA_MODELS`` is only ever read. Creating them eagerly cost a ``mkdir`` per name
    on every test and left the directories behind for the whole session, because they are
    not ``tmp_path`` dirs and no retention policy reaches them.

    MEASURED on one full ``test/`` run at ``-n 8``: 366,716 of the run's 372,126
    basetemp inodes -- 98.5% -- were these directories, ~317k of them empty. On a
    ``/tmp`` tmpfs with a fixed 1,048,576-inode budget that is a third of the machine's
    entire allowance spent on directories nothing ever opened, and it is what made a
    second concurrent run fail with ENOSPC while 90% of the bytes were free.

    A future consumer that genuinely needs the directory to exist should ``mkdir`` at its
    own call site, where the reason is visible, rather than through a flag here.
    """
    made: dict[str, pathlib.Path] = {}
    _isolation_dirs.seq += 1  # type: ignore[attr-defined]
    stem = _isolation_dirs.seq  # type: ignore[attr-defined]

    def _get(name: str) -> pathlib.Path:
        path = made.get(name)
        if path is None:
            path = _isolation_root / f"{stem}-{name}"
            made[name] = path
        return path

    return _get


_isolation_dirs.seq = 0  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _isolate_kirocrew_home(_isolation_dirs, monkeypatch):
    """Pin ``KIROCREW_HOME`` to a per-test tmp dir, for EVERY testpath.

    This lives at the rootdir rather than in ``test/conftest.py`` because the leak it
    closes is worst in the testpaths that conftest does not reach. The ~108 test
    modules under ``src/kiro_crew/apps/builtins/*/tests/`` ship inside the package and
    see only this file, so before this fixture existed here any of them that touched
    ``config_dir()`` resolved the operator's live data home -- and that resolution is
    not read-only: ``config_dir()`` CREATES the home and its marker on first use, and
    can run the one-time ``~/.kirocrew`` -> ``~/.kiro/crew`` migration as a side
    effect. Two of the eight app suites had grown their own redirect fixture; the
    other six had not, which is exactly the "remember to" contract this file exists to
    delete.

    A test that sets its own ``KIROCREW_HOME`` still wins: ``monkeypatch.setenv``
    applied later in setup overrides this, and reverts independently.

    ``config.paths._resolved_home`` is reset with it. ``config_dir()`` memoises the
    resolved home in that module global for the process lifetime, and under xdist one
    worker runs thousands of tests in one process, so a value cached by an earlier
    test would otherwise leak into a later one. Resetting it also invalidates
    ``_config_dir_memo``, which is keyed on that global by identity.

    ``KIROCREW_PROJECT_DIR`` is cleared for a different reason -- to match CI on a dev
    box. It is auto-set to the repo root when running from a checkout, so
    ``skills._project_skills_dir()`` resolves the repo's real ``skills/`` and a test
    driving ``_ensure_builtin_skills`` against a tmp dir sees live skills as a
    "source", flipping relocation behaviour: green in CI, red locally.

    ``KIROCREW_BOUND_PORT`` is cleared because ``_export_bound_port`` writes it into
    the real process environment when a test boots a server, so a port exported by one
    test would leak into every later test's port resolution on that worker.
    ``KIROCREW_DEV_MODE`` / ``KIROCREW_STRICT_ON_LOOP_PERSIST`` are cleared so a
    developer who exports them does not flip the off-loop-IO guards strict for the
    whole suite.

    ``KIRO_HOME`` is deliberately NOT pinned here, even though the lazy
    ``config.paths.kiro_home()`` does name the operator's real machine-wide kiro-cli
    home. The env var takes precedence over ``Path.home()`` by design, and ~35 tests
    isolate that resolver the other way round -- ``patch("pathlib.Path.home",
    return_value=tmp_path)`` -- so pinning the variable overrides their own isolation
    and they read an empty directory instead of the tree they just built. A test that
    reaches ``kiro_home()`` therefore isolates it itself, with whichever of the two
    levers it already uses.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(_isolation_dirs("kirocrew-home")))
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
    monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    monkeypatch.delenv("KIROCREW_STRICT_ON_LOOP_PERSIST", raising=False)
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _isolate_sel_default_dir(tmp_path_factory):
    """Redirect the Security Event Log's default dir to a session-local tmp dir.

    SEL is a process SINGLETON whose writer is a DAEMON THREAD, and
    ``_init_locked`` binds ``self._dir`` once, from whatever ``_default_dir()``
    resolved at that moment. Two consequences, and the second is why this belongs at
    the rootdir rather than in ``test/conftest.py``:

    * Every event any test emits through the default ``sel()`` appends to ONE file.
      Unredirected that is the operator's real ``security_events.jsonl`` -- a
      non-atomic append, shared across xdist workers.
    * Whichever test calls ``sel()`` FIRST fixes the directory for the whole
      process, and the writer thread keeps using it after that test ends. When the
      first caller is a test whose home is a per-test tmp dir, the thread outlives
      the tmp dir and RE-CREATES it on the next flush -- ``_flush_batch`` opens with
      ``self._dir.mkdir(parents=True, exist_ok=True)``. So the directory reappears
      *after* the test's own cleanup removed it, and no amount of tidying in the
      test can win against a thread that rebuilds the path. MEASURED: this is
      exactly what left one stray ``mkdtemp`` directory behind per run of the
      ops-mission-control suite. Full telling:
      ``docs/system-specs/common/testing-conventions.md`` § Rules.

    Session scope is what fixes both: the thread's directory is stable for the whole
    run and belongs to no individual test, so nothing deletes it underneath the
    writer, and the thread is not churned per test.

    Patches the ``_default_dir()`` accessor rather than a captured constant, because
    the module resolves its default lazily so that importing ``kiro_crew.sel`` never
    triggers the one-time data-home migration as an import side effect. Tests that
    manage their own ``SecurityEventLog`` (passing ``base_dir`` and resetting
    ``_instance``) are unaffected.
    """
    try:
        from kiro_crew import sel as _sel
    except ImportError:  # pragma: no cover - partial checkout
        yield
        return
    original_default = _sel._default_dir
    original_instance = _sel.SecurityEventLog._instance
    sel_dir = tmp_path_factory.mktemp("sel")
    _sel._default_dir = lambda: sel_dir
    _sel.SecurityEventLog._instance = None
    try:
        yield
    finally:
        _sel._default_dir = original_default
        _sel.SecurityEventLog._instance = original_instance


#: ``~/.kiro`` paths production binds at IMPORT time, which ``KIROCREW_HOME`` cannot
#: reach: the module captured an absolute path from ``Path.home()`` before any test
#: set an environment variable, so the env override is read too late to matter.
#:
#: This directory is a SEPARATE isolation axis from the data home. ``~/.kiro`` is
#: kiro-cli's own home -- machine-wide, shared with the real installed agent -- so a
#: test that writes ``~/.kiro/settings/mcp.json`` edits the MCP servers of the
#: operator's live agent, not a copy of them.
#:
#: Each entry is ``(module, attribute, path relative to the per-test kiro home)``.
#: The relative paths keep production's real SHAPE (``.kiro/settings/mcp.json``, not a
#: flattened name) because several tests assert on the path's suffix, and a
#: same-shaped tmp path keeps those assertions meaningful.
#:
#: ``test/test_host_isolation_floor.py`` ratchets this table against the
#: ``Path.home()`` bindings ``src/kiro_crew`` actually has, so a new one cannot land
#: unpinned.
_SHARED_KIRO_PATHS: tuple[tuple[str, str, str], ...] = (
    ("kiro_crew.agent", "_KIRO_MCP_JSON", ".kiro/settings/mcp.json"),
    ("kiro_crew.agent", "_CC_MCP_JSON", ".claude.json"),
    ("kiro_crew.agent", "_DEFAULT_KIRO_HOOKS_DIR", ".kiro/hooks"),
    ("kiro_crew.learn", "_DEFAULT_DIR", ".kiro/crew"),
    ("kiro_crew.apps.bridges", "_LEGACY_SHARED_MCP_PATH", ".kiro/settings/mcp.json"),
    ("kiro_crew.dashboard.handlers.mcp", "_GLOBAL_MCP_JSON", ".kiro/settings/mcp.json"),
    # A DERIVED sibling (`_GLOBAL_MCP_JSON.with_suffix(".lock")`), and it has to move
    # WITH its json or the pair is worse than either alone: `_McpFileLockSync.__enter__`
    # creates `_GLOBAL_MCP_JSON.parent` and then touches `_MCP_LOCK_PATH`, so redirecting
    # only the json makes the code create a tmp directory and then touch a lock in the
    # REAL one -- whose parent nothing created, giving `FileNotFoundError` on any host
    # where `~/.kiro/settings` does not already exist. It passed on a developer box only
    # because that directory was there, and on CI only because an earlier test had
    # leaked into it. This is the sibling-binding case the ratchet's own docstring says
    # it cannot see, which is why the set is enumerated here by hand.
    ("kiro_crew.dashboard.handlers.mcp", "_MCP_LOCK_PATH", ".kiro/settings/mcp.lock"),
)


@pytest.fixture(autouse=True)
def _isolate_shared_kiro_paths(_isolation_dirs, monkeypatch):
    """Redirect the import-time ``~/.kiro`` bindings to a per-test tmp tree.

    Patches only a module ALREADY in ``sys.modules``, the same tolerance
    ``_isolate_launchd_paths`` uses: several of these modules are heavy, and importing
    them for all ~26.5k tests would cost far more than the leak they close.

    That filter is not a coverage gap, but the reason is narrower than "collection
    imports everything": a module that is not loaded has no binding for a test to
    REACH, so there is nothing to leak. The residual hole is a module first imported
    inside a test's own body, after this fixture has already run for that test.

    Creates nothing. Every path here names a file whose absence is the normal
    fresh-install state, so a READER handles it already, and every test in the suite
    that WRITES one of them patches the same attribute itself (which wins over this
    fixture). Pre-creating the ``.kiro/settings`` parents instead cost a ``mkdir`` per
    entry on every test in the suite -- see ``_isolation_dirs`` for what that added up
    to. So a test that touches none of these modules pays one ``sys.modules`` lookup
    per entry and no syscall at all.
    """
    targets = [
        (module, attr, relative)
        for module, attr, relative in _SHARED_KIRO_PATHS
        if sys.modules.get(module) is not None
    ]
    if not targets:
        return
    root = _isolation_dirs("kiro-home")
    for module, attr, relative in targets:
        monkeypatch.setattr(
            sys.modules[module], attr, root.joinpath(*relative.split("/")), raising=False
        )


# ── other real host paths a test must not reach ───────────────────────
#
# Same test as the data home above: each of these protects something on the
# operator's machine rather than the correctness of one suite, so it holds for every
# testpath. They were in ``test/conftest.py``, which the ~108 in-package test modules
# never load -- so an in-package test that spawned a subagent wrote the real registry,
# and one that reached the embeddings boot path could start a 610MB download.


@pytest.fixture(autouse=True)
def _isolate_subagents_dir(_isolation_dirs, monkeypatch):
    """Pin the subagent registry dir to a tmp dir for the whole suite.

    ``kiro_crew.subagent_persistence._SUBAGENTS_DIR`` is bound at import time to
    ``config_dir() / "subagents"``, so the ``KIROCREW_HOME`` safety net above
    cannot retroactively redirect it. Any test that calls ``SubagentManager.spawn``
    or ``create_agent_folder`` without isolating this global itself would write
    stub agent folders into the operator's real ``~/.kirocrew/subagents/``. On the
    next gateway start, orphan reconciliation sweeps those stubs and floods the
    logs with "lost to gateway restart" warnings (e.g. tasks ``t`` / ``ls /tmp``).
    Redirecting the module global gives every test an isolated, empty registry.
    """
    monkeypatch.setattr(
        "kiro_crew.subagent_persistence._SUBAGENTS_DIR",
        _isolation_dirs("subagents"),
    )


@pytest.fixture(autouse=True)
def _no_model_download(monkeypatch, _isolation_dirs):
    """Never let a test trigger the 610MB embedding-model download.

    Embeddings are always-on, so any test that boots the gateway/server
    startup path would otherwise kick ``start_background_model_download()``.
    The env escape hatch is honored by ``ModelDownloadManager.ensure_model``
    and ``start_background_model_download`` — a test that wants to exercise
    the download path monkeypatches the manager's HTTP calls directly
    (see test_embeddings.py) rather than unsetting this.

    ``OLLAMA_MODELS`` is additionally pinned to an empty tmp dir so the
    legacy-blob salvage fast-path (``_salvage_legacy_ollama_blob``) can never
    read the developer's real ``~/.ollama`` store — without this, download
    tests would pass/fail machine-dependently on hosts that ran the
    Ollama-era embeddings.
    """
    monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("OLLAMA_MODELS", str(_isolation_dirs("ollama-models")))
    # Force telemetry OFF for every test. `_consent_enabled` reads this env var BEFORE
    # the config flag, which is what makes it a reliable gate: ~15 tests patch
    # `KiroCrewConfig.load` with a bare MagicMock, whose `telemetry.enabled` is TRUTHY,
    # so a real recorder starts and `Path(cfg.local_dir)` resolves the mock to the
    # RELATIVE path `MagicMock/load().telemetry.local_dir/...` -- writing metrics and a
    # lock file into the repo root, plus a background reader thread that outlives the
    # test. Tests that exercise telemetry delete this var themselves (test/metrics/).
    monkeypatch.setenv("KIROCREW_TELEMETRY", "0")


@pytest.fixture(autouse=True)
def _isolate_agent_state_sidecar(_isolation_dirs, monkeypatch):
    """Pin the agent_state sidecar to a tmp dir for the whole suite.

    ``kiro_crew.agent_state`` stores per-agent bookkeeping (model_managed,
    cc_model) in ``~/.kirocrew/agent_model_state.json`` via ``config_dir()``.
    Tests that exercise the install / refresh / migration / PATCH paths would
    otherwise read and write the operator's real sidecar. Redirect
    ``config_dir`` — referenced as a module attribute at call time — to a fresh
    tmp dir so every test starts from empty state.
    """
    sidecar_root = _isolation_dirs("agent-state")
    monkeypatch.setattr("kiro_crew.agent_state.config_dir", lambda: sidecar_root)

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
    global _ROOT_BASELINE
    if hasattr(session.config, "workerinput"):
        return
    _ROOT_BASELINE = _root_entries()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run when the suite left new, non-ignored entries at the root.

    A test writes there without any ``touch`` or ``open`` in its own source: a
    child process inherits pytest's CWD, which is the repository root, so a
    subprocess spawned without ``cwd=`` puts every relative write into the
    checkout. That is invisible to a reviewer reading the test, it survives the
    run, and an empty file produced this way has already been committed and
    shipped. Detected here rather than cleaned: deleting an unexpected file is
    not this guard's call to make.
    """
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
