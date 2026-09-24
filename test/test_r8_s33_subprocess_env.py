"""Every gateway spawn that runs untrusted or third-party code passes a scrubbed env.

Four sites, one property: the child's environment is built from
:func:`kiro_crew.apps.registry.minimal_env`'s allowlist, so a gateway-seeded
channel credential cannot reach it. The two ``/bin/sh -c <detectInstalled>``
probes go one step further through :func:`registry._detect_probe_env`, because a
probe's command string is manifest-supplied rather than operator-authored: the
ambient git/ssh identity the allowlist passes for an operator's own clone is
dropped, so manifest code cannot authenticate as the operator.

The assertions are on the mapping handed to the spawn, never on child behaviour.
That is deliberate: the claim is about what the parent hands over, and a child
assertion would make the proof depend on this host having a usable sandbox
backend -- which is exactly the condition the parent-level scrub covers for.
``wrap_argv_async`` is stubbed to return the argv unchanged, modelling a host
where no launcher runs at all (no OS sandbox backend plus
``agent.sandbox_allow_unsandboxed_exec``, and Windows). At the ``standard`` tier
the launcher would not strip the channel keys even when it does run:
``sandbox._agent_scrub_prefixes`` adds ``_AGENT_DENIED_ENV_KEYS`` only for
``cc``/``strict``.

Each test also asserts the two location hints a child genuinely needs (``PATH``,
``HOME``) survive, and the registry cases assert the probe's verdict still
reaches its caller -- an env that scrubs the credential by breaking the feature
is not a fix.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest

# One gateway-seeded channel credential. config/loader.load_credentials() puts
# these in the gateway's environment for TRUSTED children only.
_PLANTED_KEY = "SLACK_BOT_TOKEN"
_PLANTED_VALUE = "xoxb-r8-regression-not-a-real-value"

# The ambient git/ssh identity. `minimal_env`'s allowlist passes all four on
# purpose, so an operator-initiated clone reaches the operator's own remotes;
# a manifest-supplied probe command must not be able to spend them. The
# `GIT_SSH_COMMAND` value stands in for an operator override that names a key.
_AMBIENT_IDENTITY = {
    "SSH_AUTH_SOCK": "/tmp/r8-regression-agent.sock",
    "SSH_AGENT_PID": "424242",
    "GIT_SSH": "/usr/bin/ssh",
    "GIT_SSH_COMMAND": "ssh -i r8-regression-operator-key",
}
# The keys `anonymous_git_env` REPLACES rather than drops: a probe that reaches
# `git` must not fire a host credential helper and must not prompt.
_GIT_SUPPRESSION = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
}
# Toolchain configuration an operator may have loaded with a secret: a `-D`
# password in MAVEN_OPTS, a credential store under GRADLE_USER_HOME, an import
# tree in PYTHONPATH/NODE_PATH. The general spawn allowlist carries all of them
# for operator-initiated spawns; a manifest-supplied probe gets none.
_TOOLCHAIN_KEYS = {
    "MAVEN_OPTS": "-Dhttps.proxyPassword=r8-regression-not-a-real-value",
    "GRADLE_USER_HOME": "r8-regression-gradle-home",
    "ANT_HOME": "r8-regression-ant-home",
    "PYTHONPATH": "r8-regression-import-tree",
    "NODE_PATH": "r8-regression-node-modules",
    "NVM_DIR": "r8-regression-nvm",
    "VIRTUAL_ENV": "r8-regression-venv",
    "CONDA_PREFIX": "r8-regression-conda",
    "JAVA_HOME": "r8-regression-jdk",
    "XDG_CACHE_HOME": "r8-regression-cache",
}


class _Captured(Exception):
    """Raised from the stubbed spawn so no child is ever created."""

    def __init__(self, kwargs: dict) -> None:
        super().__init__("spawn captured")
        self.kwargs = kwargs


def _assert_scrubbed_and_usable(captured: dict, site: str) -> None:
    assert "env" in captured, (
        f"{site}: the spawn passed no env=, so the child inherits the gateway's "
        f"whole os.environ including {_PLANTED_KEY}"
    )
    env = captured["env"]
    assert _PLANTED_KEY not in env, f"{site}: the spawn's env carries {_PLANTED_KEY}"
    # The allowlist half: a child with no PATH cannot resolve a program and one
    # with no HOME cannot find its per-user config (pip.conf, .gitconfig).
    assert "PATH" in env, f"{site}: PATH was scrubbed, so the child cannot find any program"
    assert "HOME" in env, f"{site}: HOME was scrubbed, so the child has no per-user config root"


def _assert_no_ambient_identity(captured: dict, site: str) -> None:
    """A manifest-supplied probe command cannot authenticate as the operator."""
    from kiro_crew.apps import registry

    env = captured["env"]
    for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH"):
        assert key not in env, (
            f"{site}: the probe's env carries {key}, so manifest-supplied shell code "
            f"can authenticate through the operator's own git/ssh identity"
        )
    # Dropping the operator's override is not enough: the value that replaces it
    # must itself offer no identity and no agent.
    ssh_command = env.get("GIT_SSH_COMMAND", "")
    assert (
        ssh_command != _AMBIENT_IDENTITY["GIT_SSH_COMMAND"]
    ), f"{site}: the probe's env carries the operator's own GIT_SSH_COMMAND"
    assert "IdentityAgent=none" in ssh_command, (
        f"{site}: GIT_SSH_COMMAND is {ssh_command!r}, which does not force batch mode "
        f"with no identity, so an ssh remote can still authenticate as the operator"
    )
    # A credential helper named by host or user git config must not fire, and a
    # probe must fail rather than prompt the operator for a password.
    for key, value in _GIT_SUPPRESSION.items():
        assert env.get(key) == value, (
            f"{site}: {key} is {env.get(key)!r}, not {value!r}, so a manifest-chosen "
            f"remote can still reach the operator's git credentials"
        )
    assert env.get("GIT_CONFIG_GLOBAL") == os.devnull, (
        f"{site}: GIT_CONFIG_GLOBAL is {env.get('GIT_CONFIG_GLOBAL')!r}, so the "
        f"operator's own git config -- and any credential helper it names -- still "
        f"applies to a manifest-supplied command"
    )
    # No toolchain variable reaches a probe: the keep set admits location hints
    # only, so a name that carries a secret has no route in whatever its spelling.
    for key in _TOOLCHAIN_KEYS:
        assert key not in env, (
            f"{site}: the probe's env carries {key}, which an operator may have "
            f"loaded with a credential"
        )
    # Control on the assertions above: the general spawn allowlist DOES pass the
    # identity and toolchain keys, so their absence here is this code path's own
    # doing rather than an empty environment.
    allowlisted = registry.minimal_env()
    missing = (set(_AMBIENT_IDENTITY) | set(_TOOLCHAIN_KEYS)) - set(allowlisted)
    assert not missing, (
        f"minimal_env no longer passes {sorted(missing)}, so this test can no longer "
        f"tell a probe-specific drop from an allowlist-wide one"
    )


def _assert_routed_through_chokepoint(chokepoint: list[dict], site: str) -> None:
    """The probe must not hand-roll the launcher + cgroup pair.

    `sandboxed_spawn_argv` is the one path that forwards the systemd bus locators
    the cgroup ceiling's own `systemd-run --user` wrapper needs -- and drops them
    again inside the scope. A caller that wraps by hand and passes a narrowed env
    makes `systemd-run` exit before the command runs, which with DEVNULL stderr
    reads as "not installed" for every app on a cgroup-delegated host.
    """
    assert len(chokepoint) == 1, (
        f"{site}: the probe did not route through sandboxed_spawn_argv_async, so the "
        f"cgroup wrapper gets an environment with no reachable user bus"
    )
    assert (
        chokepoint[0]["mode"] == "strict"
    ), f"{site}: the probe asked for mode={chokepoint[0]['mode']!r}, not 'strict'"
    assert "XDG_RUNTIME_DIR" not in chokepoint[0]["env"], (
        f"{site}: the caller-built env carries XDG_RUNTIME_DIR; the locator is the "
        f"wrapper's capability, forwarded by the chokepoint and dropped inside the "
        f"scope, never a name the probe itself receives"
    )


def _fake_proc(returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode

    async def _communicate():
        return (b"", b"")

    proc.communicate = _communicate
    return proc


# ---------------------------------------------------------------------------
# memory.py -- the two pip spawns
# ---------------------------------------------------------------------------
def test_pip_bootstrap_spawn_env_is_scrubbed(monkeypatch):
    """``memory._ensure_pip_available`` runs ``python -m ensurepip``."""
    from kiro_crew.dashboard.handlers import memory

    monkeypatch.setenv(_PLANTED_KEY, _PLANTED_VALUE)
    # A None entry in sys.modules makes ``import pip`` raise ImportError, which
    # is the branch that spawns, without touching the real package.
    monkeypatch.setitem(sys.modules, "pip", None)

    async def _no_launcher(argv, *a, **kw):
        return list(argv), None

    monkeypatch.setattr(memory, "wrap_argv_async", _no_launcher)
    monkeypatch.setattr(memory, "cgroup_scope_argv", lambda argv: list(argv))

    async def _capture(*args, **kwargs):
        raise _Captured(kwargs)

    monkeypatch.setattr(memory, "create_subprocess_limited", _capture)

    with pytest.raises(_Captured) as excinfo:
        asyncio.run(memory._ensure_pip_available())
    _assert_scrubbed_and_usable(excinfo.value.kwargs, "memory._ensure_pip_available")


def test_faiss_install_spawn_env_is_scrubbed(monkeypatch):
    """The ``pip install faiss-cpu`` spawn in the enable-embeddings handler.

    Driven through the module's own coroutine rather than the route: the spawn is
    reached once pip imports and faiss does not, and the capture raises out
    before any of the handler's later wiring runs.
    """
    from kiro_crew.dashboard.handlers import memory

    monkeypatch.setenv(_PLANTED_KEY, _PLANTED_VALUE)
    monkeypatch.setitem(sys.modules, "pip", MagicMock())
    monkeypatch.setitem(sys.modules, "faiss", None)

    async def _no_launcher(argv, *a, **kw):
        return list(argv), None

    monkeypatch.setattr(memory, "wrap_argv_async", _no_launcher)
    monkeypatch.setattr(memory, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(memory, "model_file_present", lambda *a, **kw: True)
    monkeypatch.setattr(memory, "get_shared_embedder", lambda *a, **kw: MagicMock(dim=1024))

    async def _capture(*args, **kwargs):
        raise _Captured(kwargs)

    monkeypatch.setattr(memory, "create_subprocess_limited", _capture)

    request = MagicMock()
    request.app = {"state": MagicMock(consolidator=None)}

    memory._embedding_setup_status = {"step": "idle", "error": ""}
    try:
        with pytest.raises(_Captured) as excinfo:
            asyncio.run(memory.api_memory_enable_embeddings(request))
    finally:
        memory._embedding_setup_status = {"step": "idle", "error": ""}
    _assert_scrubbed_and_usable(excinfo.value.kwargs, "memory.api_memory_enable_embeddings")


# ---------------------------------------------------------------------------
# registry.py -- the two /bin/sh -c detectInstalled probes
# ---------------------------------------------------------------------------
def _stub_registry_probe(monkeypatch, registry, returncode: int) -> tuple[list[dict], list[dict]]:
    """Stub the probe's sandbox chokepoint and spawn.

    Returns ``(spawn_kwargs, chokepoint_calls)``. The chokepoint stub returns the
    caller's argv and env unchanged, which models the host where no launcher and no
    cgroup wrapper run -- the state in which the env the caller built is the only
    control on what the probe can read.
    """
    captured: list[dict] = []
    chokepoint: list[dict] = []

    async def _no_launcher(argv, mode=None, *, env=None, **kw):
        chokepoint.append({"argv": list(argv), "mode": mode, "env": dict(env or {})})
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(registry, "sandboxed_spawn_argv_async", _no_launcher)

    async def _capture(*args, **kwargs):
        captured.append(kwargs)
        return _fake_proc(returncode)

    monkeypatch.setattr(registry, "create_subprocess_limited", _capture)
    return captured, chokepoint


def _plant_gateway_identity(monkeypatch) -> None:
    monkeypatch.setenv(_PLANTED_KEY, _PLANTED_VALUE)
    for key, value in {**_AMBIENT_IDENTITY, **_TOOLCHAIN_KEYS}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("returncode,expected", [(0, {"r8-probe"}), (1, set())])
def test_detect_probe_spawn_env_is_scrubbed(monkeypatch, returncode, expected):
    """``registry._detect_installed_probe`` runs ``/bin/sh -c <detectInstalled>``.

    The command string comes from an app-registry manifest, which the online path
    reads from an external registry, so this child runs registry-supplied shell
    code. Both probe verdicts are driven: the scrub must not swallow either answer
    on its way back to the caller.
    """
    from kiro_crew.apps import registry

    _plant_gateway_identity(monkeypatch)
    monkeypatch.setattr(registry, "app_execution_denied", lambda *a, **kw: "")
    captured, chokepoint = _stub_registry_probe(monkeypatch, registry, returncode)

    entry = {"name": "r8-probe", "detectInstalled": "true"}
    detected = asyncio.run(registry._detect_installed_probe([entry], {}))

    assert len(captured) == 1
    _assert_scrubbed_and_usable(captured[0], "registry._detect_installed_probe")
    _assert_no_ambient_identity(captured[0], "registry._detect_installed_probe")
    _assert_routed_through_chokepoint(chokepoint, "registry._detect_installed_probe")
    assert detected == expected


def test_install_detect_guard_spawn_env_is_scrubbed(monkeypatch):
    """The same probe inside ``registry.install_from_registry``.

    A returncode of 0 means the app is already on the machine, and the install
    stops with that message -- the positive control that the guard's verdict
    still reaches its caller.
    """
    from kiro_crew.apps import registry

    _plant_gateway_identity(monkeypatch)

    entry = {
        "name": "demoapp",
        "repo": "https://example.com/demo.git",
        "detectInstalled": "true",
    }
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "app_execution_denied", lambda *a, **kw: "")
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    captured, chokepoint = _stub_registry_probe(monkeypatch, registry, 0)

    result = asyncio.run(registry.install_from_registry("demoapp"))

    assert len(captured) == 1
    _assert_scrubbed_and_usable(captured[0], "registry.install_from_registry")
    _assert_no_ambient_identity(captured[0], "registry.install_from_registry")
    _assert_routed_through_chokepoint(chokepoint, "registry.install_from_registry")
    assert result["ok"] is False
    assert "already installed on this machine" in result["error"]
