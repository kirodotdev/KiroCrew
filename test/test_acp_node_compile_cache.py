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
    no_thread_defaults, quiet_credentials
) -> None:
    from kiro_crew.config.paths import data_home
    from kiro_crew.sandbox_leaves import _CREW_READONLY_LEAVES

    home = data_home().resolve()
    sealed = [home / leaf for leaf in _CREW_READONLY_LEAVES]
    values = _every_value_the_spawn_hooks_set()
    assert values, "no spawn hook set the cache at all"
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
    """Stands in for ``subprocess.run`` of the engine: records the call, writes like Node."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        Path(kwargs["env"][NODE_COMPILE_CACHE_ENV], "v22-x64-abc").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(argv, 0)


def test_the_prewarm_runs_the_engine_with_no_input_and_no_inherited_env(
    monkeypatch, tmp_path
) -> None:
    fake = _FakeEngine()
    monkeypatch.setattr(ncc.subprocess, "run", fake, raising=True)
    monkeypatch.setenv("SOME_SECRET_TOKEN", "x")
    argv = [str(tmp_path / "node"), "--flag", str(tmp_path / "acp-server.js"), "--stdio"]

    assert ncc.prewarm_engine_bundle_cache(argv) is True

    (call,) = fake.calls
    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    assert call["argv"] == argv
    assert call["stdin"] is subprocess.DEVNULL
    assert set(call["env"]) == {"PATH", "HOME", NODE_COMPILE_CACHE_ENV}
    assert call["env"][NODE_COMPILE_CACHE_ENV] == cache
    assert call["env"]["HOME"] != os.environ.get("HOME")
    assert not os.path.exists(call["env"]["HOME"]), "the scratch home is removed afterwards"
    assert any(name.startswith(".prewarmed-") for name in os.listdir(cache))

    assert ncc.prewarm_engine_bundle_cache(argv) is True
    assert len(fake.calls) == 1, "a stamped build is not prewarmed again"


def test_a_failed_prewarm_leaves_no_stamp(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ncc.subprocess, "run", _FakeEngine(fail=True), raising=True)
    argv = [str(tmp_path / "node"), str(tmp_path / "acp-server.js")]

    assert ncc.prewarm_engine_bundle_cache(argv) is False
    cache = ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE)
    assert not [n for n in os.listdir(cache) if n.startswith(".prewarmed-")]


def test_a_changed_engine_file_is_prewarmed_again(monkeypatch, tmp_path) -> None:
    fake = _FakeEngine()
    monkeypatch.setattr(ncc.subprocess, "run", fake, raising=True)
    bundle = tmp_path / "acp-server.js"
    bundle.write_text("a", encoding="utf-8")
    argv = [str(tmp_path / "node"), str(bundle)]
    assert ncc.prewarm_engine_bundle_cache(argv) is True
    bundle.write_text("a longer bundle", encoding="utf-8")
    assert ncc.prewarm_engine_bundle_cache(argv) is True
    assert len(fake.calls) == 2


def test_ensure_starts_one_prewarm_per_build(monkeypatch, tmp_path) -> None:
    started: list[list[str]] = []

    class _InlineThread:
        def __init__(self, *, target, args, name, daemon):
            self._run = lambda: (started.append(args[0]), target(*args))

        def start(self):
            self._run()

    monkeypatch.setattr(ncc.threading, "Thread", _InlineThread, raising=True)
    monkeypatch.setattr(ncc, "_prewarm_started", set(), raising=True)
    monkeypatch.setattr(ncc.subprocess, "run", _FakeEngine(fail=True), raising=True)
    argv = [str(tmp_path / "node"), str(tmp_path / "acp-server.js")]

    ncc.ensure_engine_bundle_cache(argv)
    ncc.ensure_engine_bundle_cache(argv)
    assert started == [
        argv
    ], "a failed build is retried by the next gateway process, not every spawn"


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
