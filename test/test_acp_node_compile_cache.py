"""``NODE_COMPILE_CACHE`` for the agent engine.

A compile-cache entry is code every later engine runs, so the contract is about
who can write the directory:

* every value the variable can take sits under a crew-home leaf the OS sandbox
  mounts read-only, never under the agent-writable ``cache/``;
* Crew's own engine bundle reads ``engines/node-compile/kas-bundle`` once the
  gateway has filled it, and the gateway fills it outside the sandbox by running
  the sealed engine with no input, an empty home and no inherited environment;
* kiro-cli's engine gets ``engines/node-compile/kiro-cli`` only when the
  effective sandbox tier is ``off``;
* a value already in the environment, even an empty one, is kept,
  ``NODE_DISABLE_COMPILE_CACHE`` leaves it unset, and no failure breaks a spawn;
* a foreign harness does not get it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.acp import child_env_defaults as ced
from kiro_crew.acp import node_compile_cache as ncc
from kiro_crew.acp.child_env_defaults import apply_child_env_defaults
from kiro_crew.acp.node_compile_cache import NODE_COMPILE_CACHE_ENV, apply_node_compile_cache


@pytest.fixture
def no_thread_defaults(monkeypatch):
    monkeypatch.setattr(ced, "_host_defaults", dict, raising=True)
    monkeypatch.setattr(ced, "_configured_defaults", dict, raising=True)


def _cache_build_with(tmp_path):
    """A scratch cache-build dir holding one valid Node-cache tag dir with one file."""
    build = tmp_path / "build"
    tag = build / "v22.23.3-x64-9de703df"
    tag.mkdir(parents=True)
    (tag / "entry.bin").write_bytes(b"compiled")
    return build


def test_relocation_moves_a_valid_cache_into_engines(tmp_path):
    build = _cache_build_with(tmp_path)
    cache = tmp_path / "engines" / "node-compile" / "kas-bundle"
    cache.mkdir(parents=True)
    assert ncc._relocate_prewarm_cache(str(build), str(cache)) is True
    assert (cache / "v22.23.3-x64-9de703df" / "entry.bin").read_bytes() == b"compiled"


def test_relocation_merges_into_an_existing_tag(tmp_path):
    # An earlier bundle already left a cache; a new build adds a file under the
    # same version tag. The new file must land, not be discarded because the tag
    # dir already exists.
    cache = tmp_path / "engines" / "node-compile" / "kas-bundle"
    existing = cache / "v22.23.3-x64-9de703df"
    existing.mkdir(parents=True)
    (existing / "old.bin").write_bytes(b"old")
    build = _cache_build_with(tmp_path)  # same tag, a different file "entry.bin"
    assert ncc._relocate_prewarm_cache(str(build), str(cache)) is True
    assert (cache / "v22.23.3-x64-9de703df" / "old.bin").read_bytes() == b"old"
    assert (cache / "v22.23.3-x64-9de703df" / "entry.bin").read_bytes() == b"compiled"


def test_relocation_refuses_a_symlinked_entry(tmp_path):
    build = _cache_build_with(tmp_path)
    secret = tmp_path / "outside-secret"
    secret.write_text("sensitive", encoding="utf-8")
    # A symlink smuggled into the cache tree (would let a move follow it out).
    os.symlink(secret, build / "v22.23.3-x64-9de703df" / "link")
    cache = tmp_path / "engines" / "node-compile" / "kas-bundle"
    cache.mkdir(parents=True)
    assert ncc._relocate_prewarm_cache(str(build), str(cache)) is False
    assert list(cache.iterdir()) == [], "a batch with a symlink must be discarded whole"


def test_relocation_refuses_an_oversize_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ncc, "_MAX_CACHE_FILE_BYTES", 8, raising=True)
    build = _cache_build_with(tmp_path)
    (build / "v22.23.3-x64-9de703df" / "big.bin").write_bytes(b"x" * 64)
    cache = tmp_path / "engines" / "node-compile" / "kas-bundle"
    cache.mkdir(parents=True)
    assert ncc._relocate_prewarm_cache(str(build), str(cache)) is False
    assert list(cache.iterdir()) == []


def test_relocation_refuses_a_stray_top_level_name(tmp_path):
    build = _cache_build_with(tmp_path)
    (build / "not-a-cache-tag").mkdir()  # a top-level entry that is not a version tag
    cache = tmp_path / "engines" / "node-compile" / "kas-bundle"
    cache.mkdir(parents=True)
    assert ncc._relocate_prewarm_cache(str(build), str(cache)) is False
    assert list(cache.iterdir()) == []


@pytest.fixture
def sandbox_passthrough(monkeypatch):
    """Unit tests exercise the prewarm's OWN logic, not the real OS sandbox.

    Replace the sandbox chokepoint with a pass-through that returns the argv and
    env unchanged (no launcher) and records how it was called, so a test can
    assert the mode, the single writable scratch, and that the first-party flag
    is NOT passed (so a backend-less host fails closed).
    """
    calls: list[dict] = []

    def fake(
        argv,
        mode="standard",
        *,
        env=None,
        extra_writable_dirs=(),
        first_party_fixed_argv=False,
        **_kw,
    ):
        calls.append(
            {
                "argv": list(argv),
                "mode": mode,
                "extra_writable_dirs": tuple(extra_writable_dirs),
                "first_party_fixed_argv": first_party_fixed_argv,
            }
        )
        return list(argv), dict(env or {}), None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", fake, raising=True)
    return calls


def test_the_prewarm_runs_the_engine_inside_the_sandbox_confined(
    tmp_path, monkeypatch, sandbox_passthrough
):
    """The prewarm goes through the sandbox chokepoint (strict), gives the engine
    one writable scratch, and injects the ``--require`` confinement bootstrap."""
    cache = tmp_path / "engines" / "node-compile" / ncc.ROUTE_ENGINE_BUNDLE
    monkeypatch.setattr(ncc, "node_compile_cache_dir", lambda _route: str(cache), raising=True)
    argv = [
        "/opt/engine/node/bin/node",
        "--flag",
        "/opt/engine/bundle/acp-server.js",
        "--transport",
    ]
    seen: dict[str, object] = {}

    def fake_run(run_argv, **kwargs):
        seen["argv"] = list(run_argv)
        boot = run_argv[run_argv.index("--require") + 1]
        seen["bootstrap"] = Path(boot).read_text(encoding="utf-8")
        # Node writes the cache into the scratch build dir; the gateway relocates it.
        tag = Path(kwargs["env"][NODE_COMPILE_CACHE_ENV], "v22.23.3-x64-abc")
        tag.mkdir(parents=True, exist_ok=True)
        (tag / "entry.bin").write_bytes(b"x")
        return subprocess.CompletedProcess(run_argv, 0)

    monkeypatch.setattr("kiro_crew.sandbox.run_limited", fake_run, raising=True)

    assert ncc.prewarm_engine_bundle_cache(argv) is True

    (wrap,) = sandbox_passthrough
    assert wrap["mode"] == "strict", "the prewarm must run in the strict sandbox"
    assert wrap["first_party_fixed_argv"] is False, (
        "the prewarm must NOT pass first_party_fixed_argv: that flag would let a "
        "backend-less host run the engine unconfined; the prewarm fails closed instead"
    )
    run_argv = seen["argv"]
    assert run_argv[0] == argv[0], "the node binary stays first"
    assert run_argv[1:3] == ["--require", run_argv[2]], "the bootstrap precedes the engine script"
    assert run_argv[3:] == argv[1:], "the engine's own flags and script keep their order"
    boot_path = run_argv[2]
    scratch = os.path.dirname(boot_path)
    assert wrap["extra_writable_dirs"] == (scratch,), "only the prewarm scratch is writable"
    assert (
        os.path.join("run", "node-prewarm") in scratch
    ), "the scratch is under the runtime parent, not the read-only engines leaf"
    assert not boot_path.startswith(
        str(cache) + os.sep
    ), "the bootstrap is not written into the read-only engines cache"
    bootstrap = seen["bootstrap"]
    assert "_nodeModulePaths" in bootstrap, "the parent-walk is not confined"
    assert (
        "_resolveLookupPaths" in bootstrap
    ), "the final lookup list (home/global paths) is not confined"
    assert (
        cache / "v22.23.3-x64-abc"
    ).is_dir(), "the gateway did not relocate the cache into engines"
    assert any(n.startswith(".prewarmed-") for n in os.listdir(cache))


def test_the_prewarm_fails_closed_when_no_sandbox_backend(tmp_path, monkeypatch):
    """A host with no OS sandbox backend must never run the engine unconfined.

    The strict wrap raises ``SandboxUnavailableError`` (a ``RuntimeError``) on
    such a host. The prewarm must catch it, return False, and never reach the
    engine spawn (``run_limited``) -- otherwise the engine would run outside the
    sandbox and its cache would be relocated into the read-only ``engines`` leaf.
    """
    cache = tmp_path / "engines" / "node-compile" / ncc.ROUTE_ENGINE_BUNDLE
    monkeypatch.setattr(ncc, "node_compile_cache_dir", lambda _route: str(cache), raising=True)

    def raise_no_backend(*_a, **_kw):
        raise RuntimeError("no OS sandbox backend on this host")

    monkeypatch.setattr(
        "kiro_crew.sandbox_spawn.sandboxed_spawn_argv", raise_no_backend, raising=True
    )

    def forbidden_run(*_a, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("the engine was spawned despite no sandbox backend")

    monkeypatch.setattr("kiro_crew.sandbox.run_limited", forbidden_run, raising=True)

    argv = ["/opt/engine/node/bin/node", "/opt/engine/bundle/acp-server.js"]
    assert ncc.prewarm_engine_bundle_cache(argv) is False
    assert not cache.exists() or list(cache.iterdir()) == [], "no cache was relocated"


@pytest.fixture
def quiet_credentials(monkeypatch):
    """The harness hooks also inject/strip the API key, which reads the data home."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
    )


@pytest.fixture
def sandbox_off(monkeypatch):
    monkeypatch.setattr(ncc, "_sandbox_is_off", lambda: True, raising=True)


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setattr(ncc, "_sandbox_is_off", lambda: False, raising=True)


def _engine_bundle_node() -> str:
    """A path inside Crew's engine root, which ``_runs_crew_engine_bundle`` accepts."""
    from kiro_crew.acp import kas_bundle

    node = kas_bundle.kas_engines_root() / "0.0.0" / "node" / "bin" / "node"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("", encoding="utf-8")
    return str(node)


def _every_value_the_spawn_hooks_set() -> list[str]:
    from kiro_crew.acp.client import _resolve_spawn_env
    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp.harness.kiro import KiroHarness

    envs: list[dict[str, str]] = []
    env: dict[str, str] = {"PATH": "/usr/bin"}
    KiroHarness().apply_spawn_env(env)
    envs.append(env)
    envs.append(_resolve_spawn_env({"PATH": "/usr/bin"}, kiro_api_key=True))
    for cli_owned in (True, False):
        env = {"PATH": "/usr/bin"}
        KasHarness().apply_spawn_env(env, cli_owned_auth=cli_owned)
        envs.append(env)
    node = _engine_bundle_node()
    Path(os.path.dirname(node)).mkdir(parents=True, exist_ok=True)
    try:
        Path(ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)).mkdir(parents=True)
    except (AttributeError, TypeError):
        pass  # a build without per-route directories
    env = {"PATH": "/usr/bin"}
    KasHarness().apply_spawn_env(env, spawned_binary=node)
    envs.append(env)
    return [e[NODE_COMPILE_CACHE_ENV] for e in envs if NODE_COMPILE_CACHE_ENV in e]


def test_every_cache_directory_is_one_the_sandbox_mounts_read_only(
    sandbox_off, no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.config.paths import data_home
    from kiro_crew.sandbox_leaves import _CREW_READONLY_LEAVES

    home = data_home().resolve()
    sealed = [home / leaf for leaf in _CREW_READONLY_LEAVES]
    values = _every_value_the_spawn_hooks_set()
    assert {os.path.basename(value) for value in values} == {
        ncc.ROUTE_ENGINE_BUNDLE,
        ncc.ROUTE_KIRO_CLI,
    }, "both routes must be checked: the kiro-cli one is set only with the sandbox off"
    for value in values:
        path = Path(value).resolve()
        assert any(path.is_relative_to(leaf) for leaf in sealed), (
            f"{value} is not under a read-only sandbox leaf: a sandboxed agent could "
            "plant compiled code there that every later engine runs"
        )


def test_the_directories_are_per_route_under_engines() -> None:
    from kiro_crew.config.paths import data_home

    root = data_home() / "engines" / "node-compile"
    assert ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE) == str(root / "kas-bundle")
    assert ncc.node_compile_cache_dir(ncc.ROUTE_KIRO_CLI) == str(root / "kiro-cli")


def test_kiro_cli_gets_no_cache_while_the_sandbox_is_on(
    sandbox_on, no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.acp.harness.kiro import KiroHarness

    env = {"PATH": "/usr/bin"}
    KiroHarness().apply_spawn_env(env)
    assert NODE_COMPILE_CACHE_ENV not in env
    assert not os.path.exists(ncc.node_compile_cache_dir(ncc.ROUTE_KIRO_CLI))


def test_kiro_cli_gets_its_own_cache_with_the_sandbox_off(
    sandbox_off, no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.acp.client import _resolve_spawn_env
    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp.harness.kiro import KiroHarness

    expected = ncc.node_compile_cache_dir(ncc.ROUTE_KIRO_CLI)
    env = {"PATH": "/usr/bin"}
    KiroHarness().apply_spawn_env(env)
    assert env[NODE_COMPILE_CACHE_ENV] == expected
    assert os.path.isdir(expected)
    assert (
        _resolve_spawn_env({"PATH": "/usr/bin"}, kiro_api_key=True)[NODE_COMPILE_CACHE_ENV]
        == expected
    )
    env = {"PATH": "/usr/bin"}
    KasHarness().apply_spawn_env(env, cli_owned_auth=True)
    assert env[NODE_COMPILE_CACHE_ENV] == expected


def test_the_engine_bundle_reads_its_cache_only_once_the_gateway_made_it(
    sandbox_off, no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.acp.harness.kas import KasHarness

    node = _engine_bundle_node()
    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    env = {"PATH": "/usr/bin"}
    KasHarness().apply_spawn_env(env, spawned_binary=node)
    assert NODE_COMPILE_CACHE_ENV not in env
    assert not os.path.exists(cache), "the spawn path must not create the sealed cache"

    Path(cache).mkdir(parents=True)
    env = {"PATH": "/usr/bin"}
    KasHarness().apply_spawn_env(env, spawned_binary=node)
    assert env[NODE_COMPILE_CACHE_ENV] == cache


class _FakeEngine:
    """Stands in for ``run_limited`` of the engine: records the call, writes like Node."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        tag = Path(kwargs["env"][NODE_COMPILE_CACHE_ENV], "v22.23.3-x64-abc")
        tag.mkdir(parents=True, exist_ok=True)
        (tag / "entry.bin").write_bytes(b"x")
        return subprocess.CompletedProcess(argv, 0)


def test_the_prewarm_runs_the_engine_with_no_input_and_no_inherited_env(
    monkeypatch, tmp_path, sandbox_passthrough
) -> None:
    fake = _FakeEngine()
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", fake, raising=True)
    monkeypatch.setenv("SOME_SECRET_TOKEN", "x")
    argv = [str(tmp_path / "node"), "--flag", str(tmp_path / "acp-server.js"), "--stdio"]

    assert ncc.prewarm_engine_bundle_cache(argv) is True

    (call,) = fake.calls
    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    assert call["argv"][0] == argv[0], "the node binary stays first"
    assert call["argv"][1] == "--require", "the confinement bootstrap is injected"
    assert call["argv"][3:] == argv[1:], "the engine's own flags and script follow unchanged"
    assert call["stdin"] is subprocess.DEVNULL
    assert set(call["env"]) == {"PATH", "HOME", NODE_COMPILE_CACHE_ENV}
    # The engine writes into a scratch build dir, not the engines cache; the
    # gateway relocates the entries afterward.
    assert call["env"][NODE_COMPILE_CACHE_ENV] != cache
    assert call["env"]["HOME"] != os.environ.get("HOME")
    assert not os.path.exists(call["env"]["HOME"]), "the scratch home is removed afterwards"
    assert any(name.startswith(".prewarmed-") for name in os.listdir(cache))
    assert any(
        entry.is_dir() and not entry.name.startswith(".prewarmed-")
        for entry in Path(cache).iterdir()
    ), "the relocated cache entry is not in engines"

    assert ncc.prewarm_engine_bundle_cache(argv) is True
    assert len(fake.calls) == 1, "a stamped build is not prewarmed again"


def test_a_failed_prewarm_leaves_no_stamp(monkeypatch, tmp_path, sandbox_passthrough) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", _FakeEngine(fail=True), raising=True)
    argv = [str(tmp_path / "node"), str(tmp_path / "acp-server.js")]

    assert ncc.prewarm_engine_bundle_cache(argv) is False
    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    assert not [n for n in os.listdir(cache) if n.startswith(".prewarmed-")]


def test_a_changed_engine_file_is_prewarmed_again(
    monkeypatch, tmp_path, sandbox_passthrough
) -> None:
    fake = _FakeEngine()
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", fake, raising=True)
    bundle = tmp_path / "acp-server.js"
    bundle.write_text("a", encoding="utf-8")
    argv = [str(tmp_path / "node"), str(bundle)]
    assert ncc.prewarm_engine_bundle_cache(argv) is True
    bundle.write_text("a longer bundle", encoding="utf-8")
    assert ncc.prewarm_engine_bundle_cache(argv) is True
    assert len(fake.calls) == 2


def test_ensure_starts_one_prewarm_per_build(monkeypatch, tmp_path, sandbox_passthrough) -> None:
    started: list[list[str]] = []

    class _InlineThread:
        def __init__(self, *, target, args, name, daemon):
            self._run = lambda: (started.append(args[2]), target(*args))  # (home, fn, argv, cache)

        def start(self):
            self._run()

    monkeypatch.setattr(ncc.threading, "Thread", _InlineThread, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", set(), raising=True)
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", _FakeEngine(fail=True), raising=True)
    argv = [str(tmp_path / "node"), str(tmp_path / "acp-server.js")]

    ncc.ensure_engine_bundle_cache(argv)
    ncc.ensure_engine_bundle_cache(argv)
    assert started == [
        argv
    ], "a failed build is retried by the next gateway process, not every spawn"


def test_the_background_prewarm_fills_the_directory_its_caller_resolved(
    monkeypatch, tmp_path
) -> None:
    """The worker thread never reads the data home: it gets the directory and home by value."""
    from kiro_crew.config.paths import data_home

    handed: list[tuple] = []

    class _RecordingThread:
        def __init__(self, *, target, args, name, daemon):
            handed.append((name, args))

        def start(self):
            pass

    monkeypatch.setattr(ncc.threading, "Thread", _RecordingThread, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", set(), raising=True)
    argv = [str(tmp_path / "node"), str(tmp_path / "acp-server.js")]

    ncc.ensure_engine_bundle_cache(argv)

    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    assert handed == [
        (ncc.PREWARM_THREAD_NAME, (data_home(), ncc.prewarm_engine_bundle_cache, argv, cache))
    ]


def test_a_prewarm_thread_that_cannot_start_does_not_fail_the_spawn(monkeypatch, tmp_path) -> None:
    class _NoThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    started: set[str] = set()
    monkeypatch.setattr(ncc.threading, "Thread", _NoThread, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", started, raising=True)

    ncc.ensure_engine_bundle_cache([str(tmp_path / "node"), str(tmp_path / "acp-server.js")])

    assert started == set(), "a build whose prewarm never started is tried again"


#: Only turns a hang into a named failure; the join returns in milliseconds.
_HANG_CEILING_S = 60.0


def test_the_test_floor_joins_a_prewarm_and_forgets_its_build(
    monkeypatch, tmp_path, sandbox_passthrough
) -> None:
    """A test that reaches the engine spawn leaves no prewarm running behind it.

    ``sandbox_passthrough`` is required: without it the real strict wrap raises on
    a host with no sandbox backend (a common CI runner), so the worker fails
    closed and exits before the thread enumeration below, racing it to a
    ``StopIteration``. The pass-through lets the worker reach ``_held_engine`` and
    block there until released, so it is reliably alive when enumerated.
    """
    import threading

    from conftest_floor_restores import _join_node_compile_prewarm

    release = threading.Event()

    def _held_engine(*_args, **_kwargs):
        release.wait()
        raise OSError("no engine")

    monkeypatch.setattr("kiro_crew.sandbox.run_limited", _held_engine, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", set(), raising=True)
    ncc.ensure_engine_bundle_cache([str(tmp_path / "node"), str(tmp_path / "acp-server.js")])
    worker = next(t for t in threading.enumerate() if t.name == ncc.PREWARM_THREAD_NAME)
    assert ncc._prewarm_started

    release.set()
    assert _join_node_compile_prewarm(_HANG_CEILING_S) is None

    assert not worker.is_alive()
    assert ncc._prewarm_started == set()


def test_a_foreign_backend_spawn_does_not_get_it(
    sandbox_off, no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.acp.client import _resolve_spawn_env

    env = _resolve_spawn_env({"PATH": "/usr/bin"}, kiro_api_key=False)
    assert NODE_COMPILE_CACHE_ENV not in env


def test_a_foreign_harness_hook_does_not_get_it(sandbox_off, no_thread_defaults) -> None:
    from kiro_crew.acp.harness.codex import CodexHarness

    env: dict[str, str] = {}
    CodexHarness().apply_spawn_env(env)
    assert NODE_COMPILE_CACHE_ENV not in env


def test_the_thread_pool_defaults_alone_do_not_set_it(sandbox_off, no_thread_defaults) -> None:
    env: dict[str, str] = {}
    apply_child_env_defaults(env)
    assert env == {}


@pytest.mark.parametrize("value", ["/somewhere/else", ""])
def test_a_value_already_present_is_kept(sandbox_off, value: str) -> None:
    env = {NODE_COMPILE_CACHE_ENV: value}
    apply_node_compile_cache(env)
    assert env == {NODE_COMPILE_CACHE_ENV: value}


def test_node_disable_compile_cache_leaves_it_unset(sandbox_off) -> None:
    env = {"NODE_DISABLE_COMPILE_CACHE": "1"}
    apply_node_compile_cache(env)
    assert NODE_COMPILE_CACHE_ENV not in env


def test_a_directory_that_cannot_be_created_is_skipped_quietly(
    sandbox_off, tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        ncc, "node_compile_cache_dir", lambda _route: str(blocker / "x"), raising=True
    )
    env: dict[str, str] = {}
    apply_node_compile_cache(env)
    assert env == {}


def test_an_unresolvable_route_is_skipped_quietly(monkeypatch) -> None:
    def boom() -> bool:
        raise RuntimeError("no config")

    monkeypatch.setattr(ncc, "_sandbox_is_off", boom, raising=True)
    env: dict[str, str] = {}
    apply_node_compile_cache(env)
    assert env == {}


def test_the_agent_env_scrub_keeps_it() -> None:
    from kiro_crew.sandbox_spawn import scrub_agent_subprocess_env

    scrubbed = scrub_agent_subprocess_env({NODE_COMPILE_CACHE_ENV: "/x", "PATH": "/usr/bin"})
    assert scrubbed.get(NODE_COMPILE_CACHE_ENV) == "/x"
