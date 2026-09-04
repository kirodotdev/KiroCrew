"""Repo-root pytest configuration: the host-mutation floor.

``test/conftest.py`` holds the bulk of the suite's isolation, but it only applies
to ``test/``. ``[tool:pytest] testpaths`` also collects ``transfer`` and
``src/kiro_crew/apps/builtins`` (~108 test modules that ship inside the package,
next to the code they cover), and those get no ``test/conftest.py`` fixtures at
all -- only this file, plus that app's own ``tests/conftest.py`` where one exists.
Anything that must hold for EVERY test therefore has to live here, at the
rootdir, which is the one conftest pytest applies to every testpath.

Only the HOST-MUTATION FLOOR belongs in this file: the guards that must hold for a
test collected from any testpath, because what they protect is the
developer's machine rather than the correctness of one suite. Everything that is
merely suite-specific isolation stays in ``test/conftest.py``.

The floor has eight parts, and each one exists because the "remember to isolate
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
  random enough to read as a credential. See :data:`_SHORT_TMP_BASE`.
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

Imports at MODULE level are stdlib + pytest only, on purpose: a rootdir conftest is
imported before every collection, so pulling ``kiro_crew`` in here would make the
whole suite depend on import-time side effects of the package under test. The
fixtures that do need ``kiro_crew`` import it in their own body, which runs at test
setup -- by which point the test module has already imported the package anyway --
and tolerate an ImportError so a partial checkout cannot break collection.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import atexit
import contextlib
import gc
import getpass
import importlib
import linecache
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings

import pytest


def _root_can_create_real_symlink() -> bool:
    """Probe real-link capability for tests collected outside ``test/`` too.

    Ordinary Windows shells commonly lack ``SeCreateSymbolicLinkPrivilege``.
    This is a capability probe, not an OS guess: Developer Mode/elevated Windows
    runners retain the security coverage, while only the exact tests inventoried
    in ``test/requires-real-symlinks.txt`` skip on an incapable host.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "target")
        os.mkdir(target)
        try:
            os.symlink(
                target,
                os.path.join(tmp, "link"),
                target_is_directory=True,
            )
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_ROOT_HAS_REAL_SYMLINKS = _root_can_create_real_symlink()


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
        for candidate in tokens[index + 1 :]:
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


#: Originals of the sandbox-sweep functions the host-isolation floor patches,
#: stashed here (conftest-owned) rather than as attributes on the production
#: module. Tests reach them via the ``sandbox_sweep_original`` fixture — a
#: fixture rather than an importable name, because ``import conftest`` from a
#: test resolves to ``test/conftest.py``, not this rootdir file.
_SANDBOX_SWEEP_ORIGINALS: dict = {}


@pytest.fixture()
def sandbox_sweep_original():
    """Accessor for the pre-patch sandbox-sweep functions stashed by the floor."""
    return _SANDBOX_SWEEP_ORIGINALS.__getitem__


@pytest.fixture(scope="session")
def _sandbox_mount_source_root(tmp_path_factory):
    """An empty stand-in tmpfs root for the sandbox mount-source sweep.

    Session-scoped and owned by no individual test, because nothing ever writes
    into it -- it exists only so the sweep has a harmless directory to scan.
    """
    return tmp_path_factory.mktemp("sb-mount-src")


@pytest.fixture(autouse=True)
def _isolate_sandbox_mount_source_roots(_sandbox_mount_source_root, monkeypatch):
    """Point the sandbox mount-source sweep away from the host's real tmpfs.

    ``sandbox._cleanup_stale_sandbox_mount_sources()`` -- reached from every
    ``cleanup_stale_sandbox_profiles()`` call, including the periodic sweep in
    ``session.py`` -- resolves its candidate roots from the REAL host:
    ``/run/user/$UID``, ``/dev/shm``, and the system tempdir. The tempdir is
    already redirected by the floor above, but the first two are the operator's
    live runtime tmpfs, so an unpinned test would (a) delete real orphaned
    bind-mount sources from the developer's machine -- a host mutation, however
    garbage-shaped -- and (b) have its removal COUNT inflated by whatever real
    orphans the host happens to carry, turning exact-count assertions into
    host-state-dependent flakes.

    A test that wants the sweep's real behaviour passes ``roots=`` explicitly or
    monkeypatches ``_mount_source_candidate_roots`` itself (a later patch wins
    and reverts independently); the real function stays reachable through this
    conftest's ``sandbox_sweep_original`` accessor so its resolution logic
    remains testable without mutating the production module.

    Eagerly imported -- ``sandbox`` is a low-level dependency most of the suite
    already pulls in, so this costs nothing and a lazy patch would leave the
    first importer unprotected -- but tolerant of a partial checkout: an
    unimportable module is skipped rather than failing collection. The setattr
    itself is STRICT: a silent miss on a renamed attribute would revert the
    whole suite to sweeping the operator's real tmpfs, which is exactly what
    this fixture exists to prevent.
    """
    try:
        sandbox_mod = importlib.import_module("kiro_crew.sandbox")
    except Exception:  # pragma: no cover - a partial checkout must not break collection
        return
    _SANDBOX_SWEEP_ORIGINALS.setdefault(
        "_mount_source_candidate_roots", sandbox_mod._mount_source_candidate_roots
    )
    monkeypatch.setattr(
        sandbox_mod,
        "_mount_source_candidate_roots",
        lambda: [str(_sandbox_mount_source_root)],
    )
    # Second half of the same floor: the pin scan reads the operator's real
    # /proc. Default it to fail-closed (nothing pinned, coverage unproven) so
    # a test that forgets to fix the scan's answer is INERT -- the sweep then
    # refuses to remove directories -- instead of silently depending on host
    # /proc state. A test fixes the answer by monkeypatching
    # ``_mount_pinned_source_names`` itself; ``TestMountPinnedSourceNames``
    # imports the function by name at module import, so the parser's own unit
    # tests are unaffected by this module-attribute patch.
    _SANDBOX_SWEEP_ORIGINALS.setdefault(
        "_mount_pinned_source_names", sandbox_mod._mount_pinned_source_names
    )
    monkeypatch.setattr(
        sandbox_mod,
        "_mount_pinned_source_names",
        lambda proc_root="/proc", **_kw: (set(), False),
    )
    # Third half, same floor, for the LEGACY (pre-prefix ``tmp*``) sweep. It
    # resolves its own narrower root chain -- the launcher's two tmpfs roots,
    # deliberately excluding the redirected system tempdir -- so without this it
    # would scan the developer's real /run/user/$UID and /dev/shm, and there it
    # matches names no Kiro Crew build has created since #6268: an unpinned test
    # could delete a stranger's temp entry on the host. The bind scan is
    # defaulted fail-closed for the same reason as the pin scan above: a test
    # that forgets to fix its answer gets an INERT sweep (coverage unproven ->
    # removes nothing, stamps nothing) rather than one reading host /proc.
    _SANDBOX_SWEEP_ORIGINALS.setdefault("_launcher_tmpfs_roots", sandbox_mod._launcher_tmpfs_roots)
    monkeypatch.setattr(
        sandbox_mod,
        "_launcher_tmpfs_roots",
        lambda: [str(_sandbox_mount_source_root)],
    )
    _SANDBOX_SWEEP_ORIGINALS.setdefault(
        "_bound_source_basenames", sandbox_mod._bound_source_basenames
    )

    def _unproven_bound_sources(proc_root="/proc", *, coverage=None, **_kw):
        # Fail closed on BOTH claims the legacy gate accepts, so no test can
        # reach the developer's real runtime tmpfs through it.
        if coverage is not None:
            coverage.covered = False
        return (set(), False)

    monkeypatch.setattr(sandbox_mod, "_bound_source_basenames", _unproven_bound_sources)


@pytest.fixture(autouse=True)
def _pin_kill_and_reap_group_probe(monkeypatch):
    """Stop ``kill_and_reap``'s group-kill skip from reading host process state.

    ``platform_compat.kill_and_reap()`` skips the process-GROUP kill when the
    child shares the gateway's own group (spawned without
    ``start_new_session``, so it leads no tree of its own). That decision is a
    live ``os.getpgid()`` probe -- and almost every test of this path hands it a
    SYNTHETIC pid (4242 is the shared convention) with the tree kill mocked. The
    probe then resolves that fake pid against the runner's REAL process table:
    when some genuine process happens to own it AND sits in the pytest run's own
    process group, the skip fires, no tree kill is recorded, and the assertion
    fails. Under xdist the runner spawns plenty of low-pid children in exactly
    that group, which is why this flaked in batches -- four tests across three
    files in one run -- and passed on re-run.

    The pin is the child-leads-its-own-group answer, i.e. tree kill attempted,
    which is what every one of those tests is written to assert. It is safe as a
    floor because the skip is an optimisation, not a safety guard: it exists so
    a routine timeout does not trip :func:`kill_process_tree`'s broadcast
    refusal on every same-group child. That refusal is the actual protection and
    is untouched here, so even a real same-group child reached through this
    fixture is refused a group signal and falls through to the pid-scoped kill.
    Pinning the shared ``_OWN_PGID`` instead WOULD disarm that refusal, which is
    why the seam is this one function.

    A test that wants the skip patches ``_shares_own_process_group`` itself -- a
    later patch wins and reverts independently (see
    ``TestKillAndReap::test_skips_the_group_kill_for_a_same_group_child``).
    Tolerant of a partial checkout, and STRICT on the attribute: a silent miss
    on a rename would quietly restore the flake this exists to remove.
    """
    try:
        platform_compat = importlib.import_module("kiro_crew.platform_compat")
    except Exception:  # pragma: no cover - a partial checkout must not break collection
        return
    monkeypatch.setattr(platform_compat, "_shares_own_process_group", lambda _pid: False)


@pytest.fixture(autouse=True)
def _no_credential_env_residue():
    """Restore the credential env vars a test may have had INJECTED into it.

    ``KiroCrewConfig.load_credentials()`` deliberately propagates every credential
    it reads into ``os.environ`` with ``setdefault``, so a spawned child (sandboxed
    agent, MCP server, cron subprocess) inherits it through ``Popen``'s default
    ``env=os.environ.copy()``. Any test that points ``env_path()`` at a fabricated
    ``.env`` therefore leaves those fake credentials in the WORKER's environment,
    for every test that follows it.

    The usual guard does not catch this, which is what makes it worth a floor:
    ``monkeypatch.delenv(KEY, raising=False)`` on a key that is ABSENT records
    nothing to undo, so a value written during the test is restored to nothing.

    And the residue is not inert -- ``load_credentials`` lets ``os.environ`` WIN
    over the file it just read, so the next test pointing at a different ``.env``
    is answered with the previous test's token. Observed between
    ``test_handlers_messaging_coverage.py`` and ``test_review_fixes.py``: a Slack
    token from one file's temp ``.env`` silently satisfied the other's assertion,
    and only when both landed in the same worker.

    Bounded to the recognised fixed keys and the validated ``JIRA_TOKEN_<HEX>``
    shape. Two linear environment scans per test also catch a dynamic key that
    did not exist at setup, without masking an unrelated environment change.
    """
    from kiro_crew.config.loader import _CREDENTIAL_KEYS, _JIRA_TOKEN_RE

    fixed = frozenset(_CREDENTIAL_KEYS)

    def _is_credential(key: str) -> bool:
        return key in fixed or _JIRA_TOKEN_RE.match(key) is not None

    before = {key: value for key, value in os.environ.items() if _is_credential(key)}
    try:
        yield
    finally:
        current = {key for key in os.environ if _is_credential(key)}
        for key in current | before.keys():
            if key not in before:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before[key]


@pytest.fixture(autouse=True)
def _scrub_inherited_preload_env(monkeypatch):
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

    The removals are recorded on the test's shared ``monkeypatch`` instance rather
    than hand-rolled, and that is load-bearing, not convenience. Same-scope autouse
    fixtures are ordered so that OTHER monkeypatch users run first (alphabetically in
    practice), so a hand-rolled save/restore here tears down BEFORE monkeypatch's
    undo -- and for a test that ``monkeypatch.setenv``-overrides the SAME key the
    host inherited (recorded "was absent", because this scrub had removed it), the
    undo then deletes the inherited value the hand-rolled restore had just put back,
    leaking the removal out of the test. On one shared undo stack the nesting is
    correct BY CONSTRUCTION: the test's later record is undone first (its delete
    tolerates the key already being gone), then this fixture's ``delenv`` record
    restores the inherited value. ``test_name_grant.py::TestInheritedHostEnvironment``
    pins exactly that same-key case, and
    ``test_host_isolation_floor.py::TestInheritedShellEnvironmentIsScrubbed`` drives
    one real cycle of this fixture directly.

    The deliberate AMBIGUOUS_ENV tests lose nothing: they construct their entries
    inside the test body, after the scrub. The teardown sweep below is the
    ``_no_credential_env_residue`` half of the contract: a matching entry still
    present at teardown is either a raw ``os.environ`` write (no undo record --
    swept here, stays gone) or a monkeypatch-managed one (its undo re-deletes
    tolerantly or restores what it recorded), so nothing this fixture scrubbed and
    nothing a test leaked survives past the test on this worker.
    """
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


#: A short, path-shaped temp prefix for Darwin. macOS resolves the per-user temp dir to
#: ``/var/folders/<2 chars>/<30 random chars>/T``, which is both LONG and free of any
#: character outside ``[A-Za-z0-9/]`` -- and two limits the suite has nothing to do with
#: fall straight out of that:
#:
#: * ``sun_path`` for an AF_UNIX socket is 104 bytes on Darwin (108 on Linux), so a
#:   socket under a pytest temp dir cannot be bound at all: ``OSError: AF_UNIX path too
#:   long`` is raised before the behaviour under test runs.
#: * ``security._BARE_SECRET_RUN_RE`` matches a >=40-character run of ``[A-Za-z0-9+/]``,
#:   and ``/`` is IN that class, so a temp path is ONE contiguous run. The 30-character
#:   random segment carries that run over the 4.3-bits/char entropy floor whose whole
#:   job is to keep file paths out, so any temp path echoed through a redactor comes
#:   back ``[REDACTED: credential]`` and the assertion sees a mangled path.
#:
#: Both are properties of the HOST's temp prefix rather than of the code under test:
#: these same tests pass on Linux and in CI, where ``gettempdir()`` is ``/tmp``. Giving
#: macOS the prefix Linux already has is the smallest change that removes both, and it
#: removes them for every test at once instead of teaching each one about a platform
#: limit it is not about.
#:
#: The posture does not change. ``/tmp`` is world-writable with a sticky bit on Linux
#: too, pytest still builds its per-user ``pytest-of-<user>`` tree inside it, and this
#: file's own temp root is still ``mkdtemp``-created with O_EXCL and mode 0700 -- see
#: :func:`_create_tmp_root` for why that is what makes a shared parent safe.
_SHORT_TMP_BASE = "/tmp"


def _prefer_short_tmp_base() -> None:
    """Point this run's temp base at :data:`_SHORT_TMP_BASE` on Darwin.

    Called from ``pytest_configure``, which is early enough: pytest resolves its
    ``basetemp`` lazily from ``tempfile.gettempdir()`` on the first ``mktemp`` /
    ``getbasetemp()``, and that cannot happen before a fixture runs. Both the module
    global and the env vars are set, because ``gettempdir()`` MEMOISES into
    ``tempfile.tempdir`` and a spawned child reads the env instead.
    """
    if sys.platform != "darwin" or not os.path.isdir(_SHORT_TMP_BASE):
        return
    tempfile.tempdir = _SHORT_TMP_BASE
    for name in _TMP_ENV_VARS:
        os.environ[name] = _SHORT_TMP_BASE


def pytest_configure(config: pytest.Config) -> None:
    """Record the working directory pytest started in, before any test can move it."""
    _prefer_short_tmp_base()
    global _SESSION_CWD
    try:
        _SESSION_CWD = os.getcwd()
    except OSError:  # pragma: no cover - pytest could not have started here
        _SESSION_CWD = str(_REPO_ROOT)


@pytest.hookimpl(trylast=True)
def pytest_warning_recorded(warning_message, when, nodeid, location) -> None:
    """Drop a recorded warning's ``source`` object once the warning has been rendered.

    Every warning this suite emits is retained for the whole session, and one kind of
    warning brings a whole test's object graph with it.

    The chain: ``_pytest/warnings.py`` records each warning with
    ``pytest_warning_recorded.call_historic(...)``, and pluggy appends those kwargs to
    ``_HookCaller._call_history`` -- a list it never clears, because a historic hook
    exists precisely to be replayed to plugins registered later. So the
    ``warnings.WarningMessage`` survives to the end of the run. Ordinarily that is a
    few hundred bytes. But ``WarningMessage.source`` is the OBJECT the warning is
    about, and for ``RuntimeWarning: coroutine '...' was never awaited`` that object is
    the coroutine: it holds its frame, its frame holds every local, and those locals
    hold whatever the test built. One un-awaited coroutine therefore pins an entire
    test's graph for the session, in every worker.

    ``trylast`` is what makes this free of diagnostic loss, and it is not a
    preference. ``TerminalReporter.pytest_warning_recorded`` renders the warning to a
    plain string IMMEDIATELY -- including ``tracemalloc_message(source)``, the
    "Object allocated at:" traceback when tracemalloc is tracing and the "Enable
    tracemalloc" hint when it is not -- and stores that string. Running last means the
    text is already built before the reference goes, so nothing a reader would have
    seen is lost. Clearing it ``tryfirst`` WOULD lose that suffix, which is why the
    ordering is pinned by a test.

    Kept as a BACKSTOP rather than for its average saving, and the distinction is
    measured. Where un-awaited coroutines cluster it is large: ``test_dashboard_chat.py``
    emits 60 of them across 590 tests, and clearing ``source`` takes that file from 284
    to 219 MiB. On a mixed slice it is worth nothing at all -- an AsyncMock-dense
    3,401-test set emits 11 and measures +1.7 MiB, i.e. noise. So this is not a
    general-purpose win; it removes a tail risk whose cost is one attribute write per
    warning, and which scales with however many un-awaited coroutines the suite acquires
    later.

    The residual edge: a plugin registered AFTER a warning was recorded receives the
    replay with ``source`` already gone. Conftest plugins are all registered before
    tests run, so in this repo that is unreachable.
    """
    warning_message.source = None


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
    ``ci.yml`` slices the suite into duration-balanced pytest-split groups and a leak only
    damages tests in the SAME process, so PR CI usually cannot observe it at all; the
    release job runs the suite whole and is otherwise the first place it appears -- as
    failures in files unrelated to the cause, long after the diff merged. Restoring here
    removes the class outright rather than improving the odds of noticing it.

    Restoring rather than failing is deliberate, for the same reason the CWD restore in
    ``pytest_runtest_teardown`` above does not blame either: installing this factory is a
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
    red whenever ``test_cli.py`` or ``test_cpp_seam_failclosed.py`` precede them on the
    worker, reading ``assert <QueueListener object ...> is None``. That file's own
    ``_pristine_logging`` cannot absorb it: it clears the global in TEARDOWN only, which
    makes the file self-clean but leaves it defenceless against a leak that is already
    present at its SETUP. Under ``-n auto --dist loadgroup`` which worker an ordinary test
    lands on varies run to run, so it surfaces as an intermittent failure rather than a
    reproducible ordering bug -- it cost upstream PR #6798 a red ``Backend Tests (3.10, 1)``
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
    reds whenever ``test_cli.py::TestCronCli::test_cli_argparse_cron_add_agent_flag``
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
    """Apply exact capability skips, then Windows' tracked known-gap skips.

    Real-symlink tests are listed individually rather than intercepting
    ``os.symlink`` globally.  A global interception also catches production
    compatibility helpers before they can handle WinError 1314 and fall back to
    a junction, silently dropping the Windows behavior those tests exist to
    cover.  Exact collection markers leave every non-link path untouched.

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
    if not _ROOT_HAS_REAL_SYMLINKS:
        listfile = _REPO_ROOT / "test" / "requires-real-symlinks.txt"
        try:
            text = listfile.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - list file absent in a partial checkout
            text = ""
        requires_real_symlink = {
            _base_nodeid(ln.strip())
            for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        marker = pytest.mark.skip(
            reason="test requires real symlink creation; host lacks that capability"
        )
        for item in items:
            if _base_nodeid(item.nodeid) in requires_real_symlink:
                item.add_marker(marker)

    if not platform_compat_or_none() or not platform_compat_or_none().IS_WINDOWS:
        return
    listfile = _REPO_ROOT / "test" / "windows-expected-failures.txt"
    try:
        text = listfile.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - list file absent in a partial checkout
        return
    expected = {
        _base_nodeid(ln.strip())
        for ln in text.splitlines()
        if ln.strip() and not ln.startswith("#")
    }
    marker = pytest.mark.skip(
        reason="known Windows gap -- tracked in test/windows-expected-failures.txt"
    )
    for item in items:
        if _base_nodeid(item.nodeid) in expected:
            item.add_marker(marker)


def _base_nodeid(nodeid: str) -> str:
    """A node id reduced to file + class + function, with params and xdist group gone.

    Under ``--dist loadgroup`` -- which ``setup.cfg`` supplies to EVERY run -- xdist
    rewrites each item's nodeid to ``<nodeid>@<group>`` for anything carrying an
    ``xdist_group`` mark. Splitting on ``[`` alone therefore compares different strings
    in different invocations: a PARAMETRIZED id loses the suffix with its params, while
    an UNPARAMETRIZED one keeps it. So a plain entry silently stopped matching the
    grouped tests under the default invocation, and an entry written WITH the suffix to
    compensate then stopped matching under ``-n0``, where xdist never adds it.

    Stripping both parts makes one spelling -- the plain node id -- correct in every
    mode, which is the only spelling the list file documents.
    """
    return nodeid.split("[")[0].split("@")[0]


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
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in request.node.nodeid)
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


@pytest.fixture
def sel_private_root(tmp_path_factory):
    """Bind the SEL singleton to a directory PRIVATE to the requesting test.

    ``_isolate_sel_default_dir`` above gives every test on a worker ONE shared
    SEL directory, which is the right default tier: the writer is a daemon
    thread on a process singleton, and a per-test ``tmp_path`` would be deleted
    underneath it (see that fixture's docstring). But one shared root also
    means one shared CHAIN LOCK, and a test whose assertion transitively
    depends on a fail-closed critical audit WINNING that lock can be refused by
    a concurrent writer it never created — the loop-side acquire is a single
    non-blocking attempt by design (issue #7029, the issue-radar trust flake).

    This is the per-test tier of the same seam. It rebinds the singleton to a
    fresh directory nothing else writes, so no other test — on this worker or
    any other — can hold this test's chain lock:

    * The directory comes from ``tmp_path_factory``, whose numbered dirs are
      never deleted mid-run (a lingering writer can never resurrect a removed
      path) and whose basetemp is already per-xdist-worker (``popen-gwN``), so
      the isolation holds across workers as well as across tests.
    * The instance is built ``sync=True``: every event is written inline on its
      caller's thread and NO background writer starts, so the only writers on
      this root are the test's own threads — which the module-level chain-hold
      registry already lets join one another.
    * ``_default_dir()`` is deliberately NOT repointed: the displaced shared
      directory stays reachable through it, which is what lets a differential
      test hold the SHARED root's lock and prove this test no longer contends
      for it.

    Teardown restores the PRIOR singleton (not ``None``), so later tests on
    this worker resume the session default exactly where it left off instead of
    minting a second instance — and a second writer thread — on the same
    directory.
    """
    try:
        from kiro_crew.sel import SecurityEventLog
    except ImportError:  # pragma: no cover - partial checkout
        yield None
        return
    root = tmp_path_factory.mktemp("sel-private")
    prior_instance = SecurityEventLog._instance
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    try:
        # Inside the try: ``__new__`` publishes to ``_instance`` before
        # ``__init__`` runs, so a failing ``_init_locked`` would otherwise
        # orphan the half-built object AND lose ``prior_instance`` — every
        # later test on this worker would then re-init against the shared
        # default with a second writer thread, the exact hazard the restore
        # exists to prevent.
        SecurityEventLog(base_dir=root, sync=True)
        yield root
    finally:
        SecurityEventLog._instance = prior_instance
        # Class attribute only (each instance shadows it in __new__); reset to
        # the declared default for symmetry with test_sel.py's convention —
        # the restored instance keeps its own per-instance _initialized=True.
        SecurityEventLog._initialized = False


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


#: Modules carrying the documented ``KIRO_AGENTS_DIR`` override hook (``None`` = live).
#: Each is ``(module, attribute)``; the fixture below points them all at ONE per-test
#: directory, so the hook production already offers "a caller (test/tooling)" is set
#: by default instead of per test.
_AGENT_SPEC_HOOKS: tuple[tuple[str, str], ...] = (
    ("kiro_crew.agent", "KIRO_AGENTS_DIR"),
    ("kiro_crew.agent_discovery", "_KIRO_AGENTS_DIR"),
    ("kiro_crew.apps.bridges", "KIRO_AGENTS_DIR"),
    ("kiro_crew.cli_doctor", "KIRO_AGENTS_DIR"),
    ("kiro_crew.doctor_deadpath", "KIRO_AGENTS_DIR"),
)

#: The real user home, captured at IMPORT -- before any test can patch
#: ``Path.home``. The pin below compares against it to tell "nobody redirected the
#: home, so use the per-test dir" from "this test redirected it, so follow the test".
#: ``patch("<module>.Path.home", ...)`` is the dominant isolation idiom in this suite
#: and it is global by construction (``from pathlib import Path`` binds the same class
#: object everywhere, so patching an attribute on it patches it for every module), which
#: is why the pin can detect it at all.
_REAL_USER_HOME = pathlib.Path.home()


@pytest.fixture(scope="session")
def _agent_spec_seam_modules():
    """Import the HOOK modules once per worker process.

    Ordering is what this buys: the per-test fixture below can only patch a module that
    is already imported, and these four carry the write seams #4912 names, so "whatever
    collection happened to import" is not a strong enough guarantee for them.

    Scoped to the hook table on purpose. The resolver-binding table is far wider and
    includes the heaviest modules in the repo (``slack.gateway``), which no worker should
    import to protect a binding that module cannot reach unless a test imported it
    anyway. Those keep the patch-if-imported tolerance ``_isolate_shared_kiro_paths``
    uses.

    ``ImportError`` is tolerated per module, matching the convention the other fixtures
    here follow: a partial checkout must not break collection, and a module that cannot
    import has no binding to leak.
    """
    for module, _attr in _AGENT_SPEC_HOOKS:
        try:
            importlib.import_module(module)
        except ImportError:  # pragma: no cover - partial checkout
            continue


@pytest.fixture(autouse=True)
def _isolate_agent_spec_home(_agent_spec_seam_modules, _isolation_dirs, monkeypatch):
    """Pin the AGENT-SPEC home to a per-test tmp dir, for EVERY testpath.

    A third isolation axis, distinct from both the data home and the import-time
    ``~/.kiro`` bindings above. The agent specs are the file kiro-cli reads to learn
    which MCP servers exist, and ``kiro_agents_dir()`` is a LAZY resolver
    (``kiro_home()`` -> ``$KIRO_HOME`` or ``Path.home()/.kiro``), so neither
    ``KIROCREW_HOME`` nor ``_SHARED_KIRO_PATHS`` reaches it -- the shared-path ratchet's
    own docstring records that lazy resolvers are outside its scope.

    Without this, any test reaching the write path (``rebuild_agent_config`` and the
    per-agent writers around it, ``apps.bridges._register_agents``) rewrites the
    operator's machine-wide ``<kiro home>/agents/kirocrew.json``. Confirmed live
    (#4912): a suite run inside a throwaway clone left every managed server's
    ``command`` pointing into that clone's venv and pinned the per-test data home into
    their ``env``, because ``_managed_mcp_env`` stamps the WRITER's paths. Both stop
    existing when the run ends, so afterwards every new session on the machine spawned
    ``kirocrew-core`` from a deleted venv against a data home recreated empty ->
    ``read_local_secret()`` returned "" -> every internal HTTP call failed
    ``internal_auth_mismatch`` (``received=absent``), killing ``spawn_run``,
    ``learn_add`` and ``cron_*`` while in-process tools kept working. A gateway restart
    healed it, and the next suite run re-broke it, which is what made it look
    intermittent and unfixable.

    ``KIRO_HOME`` is NOT the lever used here, deliberately -- see
    ``_isolate_kirocrew_home`` for why pinning that variable is refused: ~35 tests
    isolate ``kiro_home()`` the other way round with
    ``patch("pathlib.Path.home", ...)``, and an env pin overrides their own isolation
    so they read an empty directory instead of the tree they just built. Pinning the
    per-module seams instead leaves both of those levers untouched.

    One directory for every entry, not one each: production resolves a single agents
    dir, so a test that writes a spec through one seam and reads it through another
    (``rebuild_agent_config`` then ``agent_discovery``) has to see the same tree.

    Unlike ``_isolate_shared_kiro_paths``, the HOOK half does not settle for patching
    whatever happens to be in ``sys.modules``. That fixture's residual hole -- a module
    first imported inside a test's own body, after the fixture already ran -- is
    tolerable for a path that is only read, and not for these: ``rebuild_agent_config``
    and ``apps.bridges._register_agents`` WRITE, and a test that imports its subject in
    its own body is a normal shape here. ``_agent_spec_seam_modules`` therefore imports
    those four once per worker process. MEASURED cold on this tree: agent 177 ms,
    cli_doctor 87 ms, bridges 33 ms, agent_discovery 19 ms -- ~315 ms per worker for a
    whole session, against a shard that runs for minutes. Per-TEST import would have
    been the unaffordable shape, which is what that other fixture's tolerance is really
    about.

    The resolver-binding half keeps the patch-if-imported tolerance: it is wide and
    includes the repo's heaviest modules, and a module nobody imported has no bound name
    for a test to reach.

    Creates nothing -- an absent agents dir is the normal fresh-install state, and
    every writer ``mkdir(parents=True)`` first.

    A test that sets its own value still wins, through EITHER lever. ``monkeypatch``
    applied later in setup overrides the seams directly; and a test that sets
    ``KIRO_HOME`` -- the documented env override, which several tests use to place the
    agents dir under their own ``tmp_path`` -- is deferred to by the replacement
    resolver installed here. Deferring is safe because the variable is CLEARED first,
    so anything visible afterwards was chosen by the test rather than exported by the
    operator (whose value would name another real home).
    """
    monkeypatch.delenv("KIRO_HOME", raising=False)
    root = _isolation_dirs("kiro-agents")
    paths = sys.modules.get("kiro_crew.config.paths")

    def _pinned_agents_dir() -> pathlib.Path:
        """The per-test dir, unless this test redirected the home itself."""
        if paths is None:  # pragma: no cover - defensive: package not importable
            return root
        if os.environ.get("KIRO_HOME") or pathlib.Path.home() != _REAL_USER_HOME:
            return paths.ambient_agents_dir()
        return root

    for module, attr in _AGENT_SPEC_HOOKS:
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, attr, root, raising=False)
    if paths is not None:
        monkeypatch.setattr(paths, "_agents_dir_override", _pinned_agents_dir)
    return paths.ambient_agents_dir if paths is not None else None


@pytest.fixture
def unpinned_agent_spec_home(_isolate_agent_spec_home, monkeypatch):
    """Opt out of the agent-spec pin, for a test that ASSERTS on the real layout.

    Two drift guards need the real resolution rather than an isolated copy: one checks
    that ``security``'s ``.kiro/agents`` literal is still the home-relative tail of
    ``kiro_agents_dir()``, the other that ``apps.bridges`` still targets the
    machine-wide registry (which is why a pod may not run ``app``). Pinned to a tmp
    path, both assert about a path that does not ship -- weakening the guard to satisfy
    an isolation fixture, which is backwards. Same call the shared-path ratchet's
    ``_EXCLUDED`` makes for its security anchors.

    READ-ONLY use only. This hands back the real machine-wide location, so a test that
    WRITES through it edits the operator's live agent. Nothing here stops that; the
    two current users only assert.
    """
    for module, attr in _AGENT_SPEC_HOOKS:
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, attr, None, raising=False)
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_agents_dir_override", None)
    return _isolate_agent_spec_home


@pytest.fixture(autouse=True)
def _no_model_download(monkeypatch, _isolation_dirs):
    """Never let a test download model weights over the network.

    Embeddings are always-on, so any test that boots the gateway/server
    startup path would otherwise kick ``start_background_model_download()``.
    The env escape hatch is honored by ``ModelDownloadManager.ensure_model``,
    ``start_background_model_download`` and ``stt.models.ModelStore.ensure`` (the
    whisper weights, 148MB at the default) — a test that wants to exercise a
    download path monkeypatches that manager's HTTP calls directly (see
    test_embeddings.py, test_stt_engine.py) rather than unsetting this.

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
def _no_central_policy_fetch(monkeypatch):
    """Keep every test off the operator's fleet policy endpoint, and stop the poller.

    Two independent hazards, both of which have to be closed here rather than in
    ``test/conftest.py``: the ~108 test modules under
    ``src/kiro_crew/apps/builtins/*/tests/`` never see that file, and several of them
    call ``boot_platform``.

    **The environment.** ``KIROCREW_POLICY_URL`` and its siblings make
    ``load_security_policy`` fetch a ceiling over the network. A developer running
    the suite on a centrally-governed machine would otherwise have every boot-path
    test hit their own fleet endpoint — and, with the fail-closed default, watch
    unrelated tests abort when it is slow or unreachable.

    **The thread.** ``policy_distribution`` keeps a module-global refresher whose
    daemon thread INSTALLS A CEILING into the process-global context. A leaked one
    outlives ``_reset_platform_context``, so it would swap a fetched ceiling into
    whichever test happened to be running when its interval elapsed — the "singleton
    with a daemon thread beats every filesystem cleanup" class, and a maximally
    confusing one, since the victim is a test that never mentioned governance.
    Stopping it is a real ``stop()``, not a dropped reference: the thread's target is
    a bound method, so nothing else clears it.
    """
    # Swept by PREFIX rather than by an enumerated list. A hand-written list here
    # would go stale the moment a sibling variable is added, silently reintroducing
    # the exact hazard this fixture exists to close — and this fixture must not import
    # the policy module at setup to read its canonical tuple, because that would put
    # the governance evaluator on every test's import path.
    for var in [k for k in os.environ if k.startswith("KIROCREW_POLICY_")]:
        monkeypatch.delenv(var, raising=False)
    yield
    # Imported at teardown, not at setup: the module pulls in the governance
    # evaluator, and an autouse fixture must not add that to every test's import
    # cost. If it was never imported, no refresher can be running.
    module = sys.modules.get("kiro_crew.platform.policy_distribution")
    if module is not None:
        with contextlib.suppress(Exception):
            module.stop_refresher(0.5)
        # Every process-global the module keeps — the fetch cooldown, the digest of
        # the document this process installed, and the cache-directory memo. Each is
        # invisible to a test that does not know it exists, and each leaks into the
        # next test a different way.
        with contextlib.suppress(Exception):
            module.reset_process_state()


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
    exit code 1 — failing the CI shard with no named test failure (issue #4764).

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

#: Failures that get their own annotation before the rest are only counted.
#: GitHub keeps a bounded number per check run and drops the excess with no
#: notice, so past this point the annotation list stops being something anyone
#: reads and the short test summary in the log is the better artifact.
_MAX_ANNOTATED_FAILURES = 10

#: Annotation messages are cut to this many characters. A full assertion diff is
#: a page long, does not fit the check-run UI, and is already in the log.
_MAX_ANNOTATION_CHARS = 400


def _gha_escape(value: str, *, is_property: bool) -> str:
    """Escape *value* for a GitHub Actions workflow command.

    The runner splits a command on ``,`` and on ``::``, and every pytest node id
    contains ``::`` -- unescaped, the annotation is truncated at the first one
    and names the FILE instead of the test, which is most of the defect this
    exists to fix. Property values need the two structural characters escaped on
    top of the data set, per the workflow-commands spec.
    """
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if is_property:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def _report_location(report: object) -> tuple[str, int | None]:
    """Repository-relative path and 1-based line for *report*, best effort.

    ``report.location`` is a ``(path, lineno, domain)`` triple whose line is
    0-BASED, while an annotation line is 1-based. A collection error carries no
    line at all, and a guessed one points the reader at the wrong statement, so
    ``None`` means "omit ``line=``" rather than "line 1".
    """
    location = getattr(report, "location", None) or ()
    path = location[0] if len(location) >= 1 and isinstance(location[0], str) else ""
    if not path:
        path = str(getattr(report, "fspath", "") or "")
    raw_line = location[1] if len(location) >= 2 else None
    line = raw_line + 1 if isinstance(raw_line, int) and raw_line >= 0 else None
    return path, line


def _cut(reason: str) -> str:
    """*reason* trimmed to :data:`_MAX_ANNOTATION_CHARS`."""
    reason = reason.strip()
    if len(reason) > _MAX_ANNOTATION_CHARS:
        return reason[: _MAX_ANNOTATION_CHARS - 3] + "..."
    return reason


def _failure_reason(report: object) -> str:
    """One-line reason for *report*, cut to :data:`_MAX_ANNOTATION_CHARS`.

    Prefers the line pytest marks with ``E`` in the first column, which is the
    error itself. ``longrepr.reprcrash.message`` looks like the obvious source
    and is right for an assertion, but for a FIXTURE error it is the preamble --
    MEASURED, an annotation built from it reads ``file <path>, line 5`` and
    spends its whole width saying nothing, while the ``E`` line two rows down
    says ``fixture 'x' not found``. Source lines in the same block are indented,
    so anchoring at column 0 does not mistake a statement for the verdict.

    ``reprcrash`` is the fallback, then the first rendered line; a report with
    none of the three still gets an annotation, because the node id was the part
    that was missing.
    """
    rendered = str(getattr(report, "longreprtext", "") or "")
    for raw in rendered.splitlines():
        if raw.startswith("E ") and raw[1:].strip():
            return _cut(raw[1:])
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    reason = str(getattr(crash, "message", "") or "")
    if not reason.strip():
        reason = rendered
    lines = [line for line in reason.strip().splitlines() if line.strip()]
    if not lines:
        return "no failure detail in the report"
    return _cut(lines[0])


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
