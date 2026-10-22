"""The host-service floor: no test may start, stop or reconfigure a real
service, and the test process tree runs without a systemd user session.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import os
import pathlib
import shutil
import subprocess
import sys

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
_SERVICE_MANAGERS = frozenset({"systemctl", "launchctl", "schtasks", "schtasks.exe"})

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
        # schtasks (Windows Task Scheduler). Verbs are `/Create`-style switches
        # and case-insensitive on the command line, so they are matched lowercased.
        "/create",
        "/delete",
        "/run",
        "/end",
        "/change",
    }
)

#: Programs that have no read-only mode worth allowing in a test: any invocation
#: rewrites host policy.
_ALWAYS_REFUSED = frozenset({"apparmor_parser"})

#: Test modules permitted to really mutate host service state.
#:
#: EMPTY, and it should stay that way. Every suite that exercises these paths
#: today already stubs them — ``test_service.py`` and its sibling suites patch
#: ``service.linux.subprocess.run`` / ``service.macos.subprocess.run``,
#: ``test_pod_launchd.py`` and the ``pod_helpers.py`` importers stub at the module boundary, and
#: the ``dev_fleet`` make-live tests stub ``_run_cmd``. So this guard breaks no
#: existing test, and an addition here means a test is about to restart a real
#: service on whoever runs the suite. Same shape as ``_ALLOWED`` in
#: ``test/test_spawn_preexec_guard.py``: an entry needs a comment saying why the
#: host mutation is acceptable.
#:
#: The two entries below are the real-service end-to-end suites, and they are the
#: reason the guard also recognises the pod CLI (see ``_POD_MUTATING_VERBS``): a
#: test that drives ``kirocrew pod up`` through a CHILD interpreter reaches
#: ``systemctl start`` / ``launchctl bootstrap`` / ``schtasks /Create`` one
#: process removed, where the in-process manager check cannot see it. Both
#: suites self-skip unless an operator sets their own opt-in variable
#: (``KIROCREW_E2E_SCENARIOS`` / ``KIROCREW_E2E_POD_WINDOWS``), run against a
#: hermetic plane (their own root, env dir, port base and unit prefix), and tear
#: down every pod, the plane root and the plane's template unit in ``finally``.
_HOST_SERVICE_EXEC_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # The pod scenario suite (nightly ``pod-scenarios``): boots one real pod
        # per session on the ``kirocrew-e2e-pod`` plane and reclaims it.
        "e2e.scenarios.test_cron_fire",
        "e2e.scenarios.test_service_install_dry_run",
        "e2e.scenarios.test_settings_save",
        "e2e.scenarios.test_subagent_spawn",
        "e2e.scenarios.test_wheel_install",
        # The Windows pod boot canary: one real Task Scheduler task on a
        # per-run plane, removed by ``pod down`` before the test returns.
        "test_pod_windows_boot",
    }
)

#: ``kirocrew pod`` verbs that create, start, stop or delete a host service
#: definition (a systemd unit, a launchd agent, a Task Scheduler task). The pod
#: CLI is how a test reaches a service manager without naming one, so a spawn of
#: ``kirocrew pod <verb>`` or ``python -m kiro_crew pod <verb>`` is refused for
#: every module not listed above. Read-only verbs (``ls``, ``status``, ``api``,
#: ``logs``) stay allowed.
_POD_MUTATING_VERBS = frozenset({"up", "down", "install", "prune", "restart"})


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
        for candidate in tokens[index + 1 :]:
            # `schtasks` verbs are `/Create`-style switches: `_basename` would
            # strip the slash, so the raw token is compared lowercased as well.
            if _basename(candidate) in _MUTATING_VERBS or candidate.lower() in _MUTATING_VERBS:
                return f"{name} {candidate!r} changes host service state"
    pod_reason = _pod_cli_refusal(tokens)
    if pod_reason:
        return pod_reason
    return None


def _pod_cli_refusal(tokens: list[str]) -> str | None:
    """Name the ``kirocrew pod`` mutation in *tokens*, or ``None``.

    Matches the console script (``kirocrew``, ``kirocrew.exe``) and the module
    form (``python -m kiro_crew``), then requires the literal ``pod`` subcommand
    followed by a verb from :data:`_POD_MUTATING_VERBS`. Anything looser would
    refuse a test that merely passes ``"pod"`` as an argument to something else.
    """
    for index, token in enumerate(tokens):
        name = _basename(token)
        if name in {"kirocrew", "kirocrew.exe"}:
            rest = tokens[index + 1 :]
        elif token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "kiro_crew":
            rest = tokens[index + 2 :]
        else:
            continue
        if len(rest) >= 2 and rest[0] == "pod" and rest[1] in _POD_MUTATING_VERBS:
            return f"kirocrew pod {rest[1]!r} changes host service state"
        return None
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
        f"_HOST_SERVICE_EXEC_ALLOWED_MODULES in test/conftest_floor_services.py "
        f"(bound by the root conftest.py) with a comment explaining why."
    )


@pytest.fixture(autouse=True)
def _block_host_service_mutation(request, _floor_monkeypatch):
    """Fail loudly if a test really starts, stops, or reconfigures a service.

    Redirecting ``$XDG_CONFIG_HOME`` (the seams part) stops a test from *writing* a real
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
    monkeypatch = _floor_monkeypatch
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


# ── no systemd user session for the test process tree ─────────────────


#: The session-bus locators the operator's shell carried, captured before the floor
#: removes them. ``real_user_session`` hands exactly these back to the one test that
#: needs the real user manager.
_REAL_USER_SESSION_ENV_AT_START: dict[str, str] = {
    name: os.environ[name]
    for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    if name in os.environ
}


def _reset_cgroup_scope_probe() -> None:
    """Drop ``sandbox``'s memoised cgroup-scope probe so the next call re-reads the env."""
    sb = sys.modules.get("kiro_crew.sandbox")
    if sb is not None:
        sb._CGROUP_SCOPE_PROBE = None


@pytest.fixture(autouse=True, scope="session")
def _no_systemd_user_session():
    """Run the whole session, children included, without a systemd user session.

    ``sandbox.cgroup_scope_argv`` wraps a spawn in ``systemd-run --user --scope
    --slice=kirocrew-agents-<token>.slice`` whenever its probe finds a user session,
    and the token is a hash of the data home. This floor pins a FRESH ``KIROCREW_HOME``
    per test, so on a developer host every test that reaches the wrapper -- and every
    ``kirocrew`` CLI child a test spawns, which probes for itself -- creates a NEW
    transient slice, and systemd never garbage-collects a slice: five full runs left
    over 4,000 ``kirocrew-agents-*.slice`` units loaded in the operator's user manager.
    The same session bus is what let a late reconcile thread run a real
    ``systemctl --user set-property`` on the operator's agents slice after its test's
    monkeypatch was undone.

    The probe's own documented gate is ``XDG_RUNTIME_DIR`` ("no systemd user
    session"), so removing the bus locators for the session is the whole fix: it
    reaches children through the inherited environment, it cannot be outrun by a
    thread, and it matches CI, where the runner has no user session either. The
    host-service guard above cannot do this job -- it sees in-process spawns only.

    A test that needs the real user manager opts in with :func:`real_user_session`.
    """
    for name in _REAL_USER_SESSION_ENV_AT_START:
        os.environ.pop(name, None)
    _reset_cgroup_scope_probe()
    try:
        yield
    finally:
        os.environ.update(_REAL_USER_SESSION_ENV_AT_START)
        _reset_cgroup_scope_probe()


@pytest.fixture
def real_user_session(monkeypatch):
    """Opt ONE test back into the operator's real systemd user session.

    Skips where the operator has none. On teardown the per-instance agents slice the
    test's pinned data home maps to is stopped, because systemd would otherwise keep
    it loaded forever (see :func:`_no_systemd_user_session`). That stop is issued
    through ``os.posix_spawn`` on purpose: it is cleanup of a unit this very test
    created, and the host-service guard's ``Popen`` funnel would refuse the verb.
    """
    if "XDG_RUNTIME_DIR" not in _REAL_USER_SESSION_ENV_AT_START:
        pytest.skip("no systemd user session on this host")
    for name, value in _REAL_USER_SESSION_ENV_AT_START.items():
        monkeypatch.setenv(name, value)
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb, "_CGROUP_SCOPE_PROBE", None)
    try:
        yield
    finally:
        slice_name = sb._agents_slice_name()
        systemctl = shutil.which("systemctl")
        if systemctl and slice_name != sb._CGROUP_AGENTS_SLICE:
            pid = os.posix_spawn(
                systemctl, [systemctl, "--user", "stop", "-q", slice_name], os.environ
            )
            os.waitpid(pid, 0)
        sb._CGROUP_SCOPE_PROBE = None
