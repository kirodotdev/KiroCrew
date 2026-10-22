"""Per-test pins of host seams that production binds once: launchd and XDG paths,
the sandbox mount-source sweep, the kill probe, credential environment, model
downloads, central policy, the platform context and other process singletons.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import os
import pathlib
import sys

import pytest


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
def _isolate_xdg_config_home(_xdg_config_root, _floor_monkeypatch):
    """Point ``$XDG_CONFIG_HOME`` at a tmp dir so no test writes a real unit file.

    ``dev_fleet._dropin_path()`` resolves the make-live systemd drop-in as
    ``$XDG_CONFIG_HOME/systemd/user/kirocrew-gateway.service.d/make-live.conf``,
    falling back to ``~/.config`` when the variable is unset — which is the
    default on most developer machines and in CI. So a test that reaches the
    cutover path without stubbing ``_dropin_path`` writes the operator's real
    drop-in. That is how a test once took the gateway down.

    Redirecting the variable fixes the whole class rather than one call site,
    because the production code already honours it (its docstring notes a literal
    ``~/.config`` would be the wrong directory on a host that sets XDG). It is
    also not dev-fleet-specific: ``pptx_maker/backend/paths.py`` resolves against
    the same variable, so its tests stop touching the real config dir too.

    A test that wants its own value still wins — it sets XDG later, and reverts
    independently.
    """
    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_xdg_config_root))


@pytest.fixture(autouse=True)
def _isolate_launchd_paths(_xdg_config_root, _floor_monkeypatch):
    """Pin the launchd install paths, which ``$XDG_CONFIG_HOME`` cannot reach.

    The macOS half of Make Live does not resolve through XDG at all. Its paths are
    module globals bound at IMPORT time from ``Path.home()``::

        PLIST_DIR    = ~/Library/LaunchAgents
        PLIST_PATH   = PLIST_DIR / "dev.kirocrew.gateway.plist"
        LOG_DIR      = ~/Library/Logs/...        (+ STDOUT_LOG, STDERR_LOG)
        LIVE_PROGRAM = launchd_live_program()    (under ~/Library/Application Support)

    That is the same import-time-binding class the suite already documents:
    an env var read *after* the module captured the path changes nothing, so the
    redirect above leaves the launchd side wide open.

    It is reachable today, not hypothetically. ``macos.install()`` calls
    ``write_live_program(render_live_program(kirocrew_bin()))`` with no path
    argument, so the launcher lands on the real ``LIVE_PROGRAM`` even in a test that
    carefully pinned every ``PLIST_*`` constant — which
    ``test_install_writes_plist_and_loads`` does. Raised in review of the drop-in fix.

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
    monkeypatch = _floor_monkeypatch
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
#: test resolves to ``test/conftest.py``, not the rootdir conftest this part belongs to.
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
def _isolate_sandbox_mount_source_roots(_sandbox_mount_source_root, _floor_monkeypatch):
    """Point the sandbox mount-source sweep away from the host's real tmpfs.

    ``sandbox._cleanup_stale_sandbox_mount_sources()`` -- reached from every
    ``cleanup_stale_sandbox_profiles()`` call, including the periodic sweep in
    ``session_parts/sweeps.py`` -- resolves its candidate roots from the REAL host:
    ``/run/user/$UID``, ``/dev/shm``, and the system tempdir. The tempdir is
    already redirected by the tempfile floor, but the first two are the operator's
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
    monkeypatch = _floor_monkeypatch
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
    # matches names no current Kiro Crew build creates: an unpinned test
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
def _pin_kill_and_reap_group_probe(_floor_monkeypatch):
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
    monkeypatch = _floor_monkeypatch
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
    from kiro_crew.config.loader import _JIRA_TOKEN_RE, CREDENTIAL_KEYS

    fixed = frozenset(CREDENTIAL_KEYS)

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
def _no_model_download(_floor_monkeypatch, _isolation_dirs):
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

    The same floor covers hosted feature-video media
    (``KIROCREW_SKIP_FEATURE_VIDEO_DOWNLOAD``), honored by
    ``feature_videos_cache.ensure_all`` and
    ``start_background_feature_video_download``: the gateway boot path kicks that
    transfer too, and a test that stands up the server must not reach a CDN.
    """
    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("KIROCREW_SKIP_FEATURE_VIDEO_DOWNLOAD", "1")
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
def _no_default_builtin_skills_sync(request, _floor_monkeypatch):
    """Stop a bare ``SkillsLoader()`` / ``ContextBuilder()`` from syncing builtins.

    ``SkillsLoader.__init__`` defaults ``install_builtins=True``, which calls
    ``_ensure_builtin_skills`` — a stat walk plus capped content hashing over
    the whole builtin-skills tree, then a real copy to disk on first divergence
    (~20s on a loaded host). ``ContextBuilder.__init__`` builds exactly that
    default (``self.skills = skills or SkillsLoader()``) whenever a test omits
    ``skills=``, so every such call site pays the sync even though the test
    under it almost never asserts anything about builtin skills. ~70 call
    sites across ``test/`` construct ``ContextBuilder()`` bare; fixing it here
    once is cheaper than a per-call-site ``skills=SkillsLoader(install_builtins=False)``
    that every new call site would have to remember.

    Patches the constructor itself, not a module-level flag, because that is
    the seam production code actually reads — ``install_builtins`` is a plain
    keyword default baked into ``SkillsLoader.__init__``'s signature, not a
    config value the class re-reads later. The wrapper only rewrites an
    OMITTED argument: a caller that explicitly passes ``install_builtins=`` in
    either form (keyword or positional) is left alone, so ``test_skills.py``
    passing ``install_builtins=False`` and ``test_crystallize_skill.py``
    asserting on a real sync with ``install_builtins=True`` both keep getting
    exactly what they asked for. A test whose contract IS the default — the
    event-loop guard in ``test_builtin_skill_sync_safety.py`` asserts that a
    bare ``SkillsLoader(skills_path=...)`` syncs when built off a running loop
    — opts out with ``@pytest.mark.real_builtin_skills_sync`` (registered in
    ``setup.cfg``) and gets the constructor untouched.
    """
    monkeypatch = _floor_monkeypatch
    if request.node.get_closest_marker("real_builtin_skills_sync") is not None:
        return

    from kiro_crew.skills import SkillsLoader

    original_init = SkillsLoader.__init__

    @functools.wraps(original_init)
    def _init_without_builtins_by_default(self, *args, **kwargs):
        # 2nd positional slot (after self, i.e. args[0]) is skills_path; the
        # 3rd (args[1]) is install_builtins. Only default it when the caller
        # supplied neither the keyword nor that positional slot.
        if "install_builtins" not in kwargs and len(args) < 2:
            kwargs["install_builtins"] = False
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(SkillsLoader, "__init__", _init_without_builtins_by_default)


@pytest.fixture(autouse=True)
def _no_central_policy_fetch(_floor_monkeypatch):
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
    monkeypatch = _floor_monkeypatch
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
def _reset_platform_context(_floor_monkeypatch):
    """Clear the process-global PlatformContext between tests, and pin the profile.

    A test that composes a non-default context (e.g. an enterprise-overlay probe)
    must not leak it into the next test.  ``current_context()`` lazily rebuilds
    the standalone default on next access.

    Also pins ``KIROCREW_PROFILE=standalone`` so the profile a test boots under
    comes from the test, never from the operator's shell: a dev box with a real
    SSO-marker directory, or a shell whose parent process exported
    ``KIROCREW_PROFILE`` set to the enterprise profile (a Kiro Crew agent session
    does exactly that for every child it spawns), would otherwise make
    ``boot_platform`` resolve the enterprise profile and fail CLOSED (no companion installed) for every test
    that drives ``run_gateway`` / boot / the governance hook.  A test that wants
    the enterprise profile overrides this env via its own ``monkeypatch.setenv`` (it
    runs after this autouse fixture), or composes the context directly via
    ``set_context`` without booting.

    Lives HERE, at the rootdir, for the same reason ``_isolate_kirocrew_home`` does:
    the ~108 test modules under ``src/kiro_crew/apps/builtins/*/tests/`` never see
    ``test/conftest.py``.  With the pin only there, an inherited
    ``KIROCREW_PROFILE=enterprise`` reddens 150+ builtin-app tests (every governance-gated
    route answers 500, every gated notification is dropped) while ``test/`` stays
    green.
    """
    from kiro_crew.platform.bootstrap import _reset_boot_state
    from kiro_crew.platform.context import reset_context

    _floor_monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    reset_context()
    _reset_boot_state()
    yield
    reset_context()
    _reset_boot_state()


@pytest.fixture(autouse=True)
def _isolate_pre_exec_stops(_floor_monkeypatch):
    """Give every test its own registry of pre-exec stops.

    ``kiro_crew.pre_exec_stops`` keeps one process-wide registry, filled by a
    startup hook (the instance tunnels) and emptied by its cleanup hook. A test
    that runs the startup hook without the cleanup would leave its stop behind,
    and a later test's restart in the same worker would call it.
    """
    from kiro_crew import pre_exec_stops

    _floor_monkeypatch.setattr(pre_exec_stops, "_stops", {})


@pytest.fixture(autouse=True)
def _isolate_agent_state_sidecar(_isolation_dirs, _floor_monkeypatch):
    """Pin the agent_state sidecar to a tmp dir for the whole suite.

    ``kiro_crew.agent_state`` stores per-agent bookkeeping (model_managed,
    cc_model) in ``~/.kirocrew/agent_model_state.json`` via ``config_dir()``.
    Tests that exercise the install / refresh / migration / PATCH paths would
    otherwise read and write the operator's real sidecar. Redirect
    ``config_dir`` — referenced as a module attribute at call time — to a fresh
    tmp dir so every test starts from empty state.

    The module is taken from ``sys.modules`` (``import_module``), never through the
    dotted string ``"kiro_crew.agent_state.config_dir"``: that string resolves each
    component as an ATTRIBUTE of its parent package, so a test that evicted
    ``kiro_crew.agent_state`` and re-imported it without restoring
    ``kiro_crew``'s attribute made this setup raise ``AttributeError`` for every
    later test on the worker (2,167 setup errors on one Windows worker).
    """
    _pin_agent_state_sidecar(_floor_monkeypatch, _isolation_dirs("agent-state"))


def _pin_agent_state_sidecar(monkeypatch, sidecar_root) -> None:
    agent_state = importlib.import_module("kiro_crew.agent_state")
    monkeypatch.setattr(agent_state, "config_dir", lambda: sidecar_root)


#: How long a test's teardown waits for the member event-log writes it queued. A
#: slow runner disk retires each queued append in tens of milliseconds, so this is
#: generous; a queue that does not drain in it is a wedge worth failing on.
_MEMBER_EVENTLOG_DRAIN_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _reset_member_eventlog_singleton(_isolate_kirocrew_home):
    """Drain the member event-log queue a test filled, and reset the service singleton.

    ``get_service()`` memoises one ``MemberEventLogService`` for the process,
    rebuilding it only when the crew-log root changes. The root is derived from
    ``KIROCREW_HOME``, which the floor points at a fresh per-test directory -- so a
    test that touches the service (directly, or through a dashboard handler /
    ``members.record_activity``) leaves a live singleton BOUND TO THAT TEST'S HOME,
    and the next test on the same xdist worker inherits it after that home has been
    torn down. Its cached ``MemberLog`` objects hold open OS handles under the dead
    directory, which is harmless on POSIX but not on Windows: the stale handles block
    the tmp-dir teardown and the very first write in the inheriting test then fails,
    so ``record_activity`` returns ``False`` -- a shard-only red on Windows CI.

    Teardown first DRAINS the writes the test queued. Dashboard DM messages and slot
    transitions reach the log through ``eventlog_hooks.submit``: one process-wide
    worker thread that is otherwise drained only at interpreter exit. Undrained, a
    test's queued write runs during whatever test comes next on the worker, opens
    that member's log inside the NEXT test's home, and holds its lock there; a test
    that then touches the same member from the event-loop thread meets the held lock
    and ``record_activity(...)`` answers ``False``. This fixture depends on
    ``_isolate_kirocrew_home``, so its teardown runs while the home pin still holds:
    every queued write lands in the home of the test that queued it, and a queue
    that does not drain in time fails the test that filled it.

    It lives in the rootdir conftest because the in-package app suites
    (``src/kiro_crew/apps/builtins/**/tests``) reach the same queue and see no other
    conftest. Both modules are looked up in ``sys.modules`` rather than imported, so
    a test that never loads them pays nothing; the drain is bound at setup when the
    module is already loaded, so a test that replaces ``drain_for_shutdown`` with a
    wedged stand-in does not wedge this teardown. Reset at both ends, so a test that
    runs after a leak still starts on a clean singleton.
    """
    hooks = sys.modules.get("kiro_crew.eventlog_hooks")
    drain = getattr(hooks, "drain_for_shutdown", None)
    _reset_member_eventlog_service()
    try:
        yield
    finally:
        if drain is None:
            hooks = sys.modules.get("kiro_crew.eventlog_hooks")
            drain = getattr(hooks, "drain_for_shutdown", None)
        drained = drain(_MEMBER_EVENTLOG_DRAIN_SECONDS) if drain is not None else True
        _reset_member_eventlog_service()
        if not drained:
            raise TimeoutError(
                "queued member event-log writes did not finish within "
                f"{_MEMBER_EVENTLOG_DRAIN_SECONDS:.0f}s of the test that queued them"
            )


def _reset_member_eventlog_service() -> None:
    service = sys.modules.get("kiro_crew.eventlog.service")
    if service is not None:
        service.set_service(None)
