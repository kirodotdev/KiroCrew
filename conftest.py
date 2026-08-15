"""Repo-root pytest configuration: the host-mutation floor.

``test/conftest.py`` holds the bulk of the suite's isolation, but it only applies
to ``test/``. ``[tool:pytest] testpaths`` also collects ``transfer`` and
``src/kiro_crew/apps/builtins`` (42 test modules that ship inside the package,
next to the code they cover), and those get no ``test/conftest.py`` fixtures at
all. Anything that must hold for EVERY test therefore has to live here, at the
rootdir, which is the one conftest pytest applies to all three testpaths.

Only the host-mutation floor belongs in this file. It is deliberately narrow: a
guard that makes it impossible for a test to reconfigure or restart the
developer's real Kiro Crew service, and one that fails the run when a test leaves
residue in the repository checkout. Everything else stays in ``test/conftest.py``.

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

The two fixtures below remove that "remember to" from the contract. Neither
changes the behaviour of a test that already isolates itself correctly.

Imports are stdlib + pytest only, on purpose: a rootdir conftest is imported
before every collection, so pulling ``kiro_crew`` in here would make the whole
suite depend on import-time side effects of the package under test.
"""

from __future__ import annotations

import asyncio.base_events
import importlib
import os
import pathlib
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
