"""gatewayd's own PATH must carry the MCP launcher dirs, not just ``~/.local/bin``.

The field failure this pins: the dashboard reported an MCP server as "failed to
start in this session" with ``JSON-RPC error: -32000: backend gone: exit
rc=127`` for ``slack-mcp``, ``talos-web-mcp`` and ``local-chorus-mcp``, while
every other pooled server on the same daemon came up fine.

rc=127 is ``/bin/sh``'s "command not found", and the split between the failing
and working servers is what names the cause. A server whose spec command is an
absolute binary (``~/.toolbox/bin/builder-mcp``) needs no PATH lookup at all.
The three failures are the ones whose command is a thin wrapper script that
exec's a BARE tool name::

    #!/bin/sh
    exec <tool> <subcommand> local-chorus-mcp

``GatewayManager._spawn_once`` built the daemon's env by prepending exactly ONE
hand-picked directory (``~/.local/bin``), so a gateway launched from the desktop
app handed gatewayd a PATH without the dir holding that tool. Every such wrapper
then failed its exec, the backend exited 127 before writing a byte of protocol,
and the only thing the session saw was "backend gone" -- no mention of PATH, of
the wrapper, or of the tool it could not find.

Three properties are pinned:

* the daemon's PATH is built by :func:`kiro_crew.env.augmented_path` -- the same
  helper the kiro-cli spawn path uses -- so the launcher dirs cannot drift apart
  between the two spawn sites,
* that build runs OFF the event loop, because on a cold cache it globs every
  version-manager root for Node bin dirs, and
* the mechanism itself: a bare-name wrapper really does exit 127 under the old
  one-directory PATH and really does start under the fixed one. Without this last
  test the first is just an assertion about a string.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.env as env_mod
from kiro_crew.env import augmented_path, emit_env, mcp_runtime_path, mcp_search_path
from kiro_crew.mcp_gateway import gatewayd as gatewayd_mod
from kiro_crew.mcp_gateway import manager as manager_mod
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey


def _same_dir(a: str, b: str) -> bool:
    """Compare two PATH entries as DIRECTORIES, not as strings.

    ``_EXTRA_PATH_DIRS`` spells its entries with forward slashes
    (``"{home}/.toolbox/bin"``), so on Windows the built value mixes separators
    while a test rebuilding the same directory with ``pathlib`` gets all
    backslashes. The strings differ; the directory does not. ``normpath``
    settles the separators, ``normcase`` the case-insensitivity.
    """
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class _SpawnCaptured(RuntimeError):
    """Raised from the fake spawn so the test stops at the env we came for."""


def _captured_daemon_env(monkeypatch, tmp_path: Path, inherited_path: str) -> dict[str, str]:
    """Run ``_spawn_once`` far enough to capture the env it would spawn with."""
    captured: dict[str, str] = {}

    async def _fake_spawn(*_argv, env=None, **_kwargs):
        captured.update(env or {})
        raise _SpawnCaptured

    monkeypatch.setenv("PATH", inherited_path)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    monkeypatch.setattr(manager, "_gatewayd_log_path", lambda: tmp_path / "gatewayd.stdout")
    monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])

    with pytest.raises(_SpawnCaptured):
        asyncio.run(manager._spawn_once())
    return captured


def _pool_key(tmp_path: Path) -> PoolKey:
    return PoolKey(
        server_name="declared-path-mcp",
        agent_name="daemon-path-test",
        command_args_hash="a" * 8,
        effective_env_hash="e" * 8,
        work_dir=str(tmp_path),
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="b" * 8,
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="c" * 8,
    )


async def _noop_pump() -> None:
    return None


async def _captured_backend_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited: dict[str, str],
    declared: dict[str, str],
) -> dict[str, str]:
    """Run gatewayd's backend spawn far enough to capture its final environment."""
    key = _pool_key(tmp_path)
    process = MagicMock()
    process.returncode = None
    # Above every supported platform's pid_max, so a cleanup path that signals
    # it cannot reach a live process -- under pytest-xdist that would be a
    # sibling worker, which dies with no assertion and no traceback. A literal
    # rather than test_update_provider's shared spelling: Python's stdlib owns
    # the name ``test``, so on Windows ``test.test_update_provider`` resolves
    # into the stdlib package and the module fails to import.
    process.pid = 99_999_999_999
    now = time.monotonic()
    backend = Backend(
        pool_key=key,
        process=process,
        stdin=MagicMock(),
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    spawn = AsyncMock(return_value=backend)
    monkeypatch.setattr(gatewayd_mod, "spawn_backend", spawn)
    monkeypatch.setattr(
        gatewayd_mod,
        "_declared_env_to_forward",
        lambda _key: dict(declared),
    )
    monkeypatch.setattr(
        gatewayd_mod,
        "resolve_secret_uris",
        lambda env, _home: (dict(env), set()),
    )
    monkeypatch.setattr(gatewayd_mod, "_spawns_own_control_plane", lambda *_a, **_kw: False)

    await gatewayd_mod._acquire_backend(
        BackendPool(max_backends=2),
        key,
        lambda _key: ("demo-bin", [], dict(inherited), str(tmp_path)),
    )

    assert spawn.await_args is not None
    assert backend._stdout_task is not None
    await backend._stdout_task
    return dict(spawn.await_args.kwargs["env"])


# ------------------------------------------------- the daemon's PATH is augmented


def test_the_daemon_inherits_the_augmented_launcher_path(monkeypatch, tmp_path) -> None:
    """The daemon PATH is ``mcp_runtime_path``, with ``augmented_path`` intact.

    ``augmented_path`` is kept as one contiguous block in its exact kiro-cli
    order, so the two spawn sites share one launcher precedence. Contributed
    directories may lead it; nothing may reorder it. With nothing contributed
    the value is byte-identical to ``augmented_path``.
    """
    minimal = os.pathsep.join(["/usr/bin", "/bin"])
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    env = _captured_daemon_env(monkeypatch, tmp_path, minimal)

    assert env["PATH"] == mcp_runtime_path(minimal)
    assert env["PATH"] == augmented_path(minimal)
    augmented = augmented_path(minimal).split(os.pathsep)
    assert env["PATH"].split(os.pathsep)[-len(augmented) :] == augmented


def test_operator_configured_extra_dir_reaches_the_daemon(monkeypatch, tmp_path) -> None:
    """An ``mcp.extra_path_dirs`` entry reaches the daemon, AHEAD of the guesses.

    A wrapper resolved out of an operator dir can exec a bare tool that lives
    there, and the operator dir outranks every built-in guess -- the rule
    ``mcp_search_path`` documents. ``augmented_path`` follows as one block.
    """
    extra = str(tmp_path / "operator-bin")
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    env_mod.publish_config_path_dirs([extra])

    minimal = os.pathsep.join(["/usr/bin", "/bin"])
    env = _captured_daemon_env(monkeypatch, tmp_path, minimal)
    parts = env["PATH"].split(os.pathsep)

    assert parts[0] == extra
    augmented = augmented_path(minimal).split(os.pathsep)
    assert parts[-len(augmented) :] == augmented
    assert parts == [extra, *augmented]


def test_a_contributed_dir_that_duplicates_a_guess_leads_and_appears_once(
    monkeypatch, tmp_path
) -> None:
    """Membership alone would not catch a contributed dir landing dead last.

    The fixture is a built-in guess that an operator ALSO names. It must appear
    exactly once, at the front, and be dropped from its position inside the
    built-in block; the rest of ``augmented_path`` keeps its relative order.

    The fixture is read back out of ``augmented_path``'s own output rather than
    rebuilt here, so it is byte-identical to the entry it has to collapse with.
    Rebuilding it would make this test depend on which separator style the
    module happens to spell its guesses with -- a different concern, and the
    one ``_same_dir`` handles.
    """
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    minimal = os.pathsep.join(["/usr/bin", "/bin"])
    augmented = augmented_path(minimal).split(os.pathsep)
    # Everything ahead of the inherited entries is this module's own guess block.
    builtin_block = augmented[: augmented.index(minimal.split(os.pathsep)[0])]
    assert len(builtin_block) >= 2, "need a guess that is not already first"
    fixture = builtin_block[1]

    env_mod.publish_config_path_dirs([fixture])
    env = _captured_daemon_env(monkeypatch, tmp_path, minimal)
    parts = env["PATH"].split(os.pathsep)

    assert parts.count(fixture) == 1
    assert parts[0] == fixture
    assert parts[1:] == [d for d in augmented if d != fixture]


def test_the_toolbox_bin_dir_reaches_the_daemon(monkeypatch, tmp_path) -> None:
    """The specific directory the field failure was missing.

    ``~/.toolbox/bin`` holds the tool that every launcher wrapper exec's by bare
    name. This is the assertion that goes red if someone narrows the
    augmentation back to a hand-picked subset.
    """
    minimal = os.pathsep.join(["/usr/bin", "/bin"])
    env = _captured_daemon_env(monkeypatch, tmp_path, minimal)

    toolbox_bin = str(Path(os.path.expanduser("~")) / ".toolbox" / "bin")
    assert any(_same_dir(entry, toolbox_bin) for entry in env["PATH"].split(os.pathsep))


def test_an_operator_path_entry_is_not_dropped(monkeypatch, tmp_path) -> None:
    """Augmenting must ADD dirs, never replace the inherited ones -- an operator
    who put a launcher on the gateway's own PATH must still be able to reach it.
    """
    inherited = os.pathsep.join(["/opt/site/bin", "/usr/bin", "/bin"])
    env = _captured_daemon_env(monkeypatch, tmp_path, inherited)

    parts = env["PATH"].split(os.pathsep)
    assert "/opt/site/bin" in parts


def test_the_path_is_built_off_the_event_loop(monkeypatch, tmp_path) -> None:
    """The PATH build must not run on the loop.

    On a cold cache ``mcp_runtime_path`` calls ``augmented_path``, which globs
    every version-manager root for Node bin dirs. This method already offloads
    its other blocking calls, so dropping ``to_thread`` would be silent because
    the value would be identical and every other test here would still pass.
    """
    loop_thread: dict[str, int] = {}
    build_thread: dict[str, int] = {}

    def _spy(base: str) -> str:
        build_thread["ident"] = threading.get_ident()
        return mcp_runtime_path(base)

    monkeypatch.setattr(manager_mod, "mcp_runtime_path", _spy)

    async def _record_loop_thread() -> None:
        loop_thread["ident"] = threading.get_ident()

    captured: dict[str, str] = {}

    async def _fake_spawn(*_argv, env=None, **_kwargs):
        captured.update(env or {})
        raise _SpawnCaptured

    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    mgr = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    monkeypatch.setattr(mgr, "_gatewayd_log_path", lambda: tmp_path / "gatewayd.stdout")
    monkeypatch.setattr(mgr, "_credential_watch_paths", lambda: [])

    async def _drive() -> None:
        await _record_loop_thread()
        with pytest.raises(_SpawnCaptured):
            await mgr._spawn_once()

    asyncio.run(_drive())

    assert build_thread.get("ident") is not None, "mcp_runtime_path was never called"
    assert build_thread["ident"] != loop_thread["ident"]
    # The worker-produced value is still exactly what the child receives.
    assert captured["PATH"] == mcp_runtime_path(os.pathsep.join(["/usr/bin", "/bin"]))


@pytest.mark.asyncio
async def test_declared_path_keeps_launcher_dirs_after_declared_entries(
    monkeypatch, tmp_path
) -> None:
    """A backend keeps spec PATH entries first and every managed launcher behind them."""
    declared_dirs = [tmp_path / "spec-bin-a", tmp_path / "spec-bin-b"]
    launcher_dir = tmp_path / "operator-launcher-bin"
    host_path = os.pathsep.join(["/usr/bin", "/bin"])
    declared_path = os.pathsep.join(map(str, declared_dirs))
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    env_mod.publish_config_path_dirs([str(launcher_dir)])
    monkeypatch.setenv("PATH", host_path)

    expected = mcp_search_path(declared_path).split(os.pathsep)
    loop_thread = threading.get_ident()
    build_threads: list[int] = []
    real = gatewayd_mod.mcp_search_path

    def _spy(value: str) -> str:
        build_threads.append(threading.get_ident())
        return real(value)

    monkeypatch.setattr(gatewayd_mod, "mcp_search_path", _spy)
    captured = await _captured_backend_env(
        monkeypatch,
        tmp_path,
        {"PATH": host_path},
        {"PATH": declared_path},
    )
    actual = captured["PATH"].split(os.pathsep)

    assert len(actual) == len(expected)
    assert all(_same_dir(got, want) for got, want in zip(actual, expected))
    assert all(_same_dir(actual[index], path) for index, path in enumerate(declared_dirs))
    assert _same_dir(actual[len(declared_dirs)], launcher_dir)
    assert build_threads and all(thread != loop_thread for thread in build_threads)


@pytest.mark.asyncio
async def test_emitted_declared_path_is_forwarded_verbatim_and_only_gains_dirs(
    monkeypatch, tmp_path
) -> None:
    """An ``emit_env``-expanded declaration is the backend PATH's verbatim prefix.

    The agent config stores a declared ``env.PATH`` as the complete value
    ``emit_env`` renders under the process that rebuilds it: the spec's pins,
    then that process's augmented PATH. The rewriter forwards that stored value
    into the sidecar, so what gatewayd receives has no readable authored
    boundary. gatewayd therefore keeps the whole stored value first, byte for
    byte, and only APPENDS the directories it does not carry: the contributed
    ``mcp.extra_path_dirs`` ahead of the daemon-only inherited entries.

    Consequence pinned here: every command name that resolves inside the stored
    value resolves to the same binary the declaration alone selects, and a name
    that resolves nowhere in it gains the operator's directory instead of
    failing. The emit runs under one PATH and the spawn under a smaller one, as
    a CLI-rendered config and a daemon spawn do.
    """
    cli_path = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    daemon_path = os.pathsep.join(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    override = str(tmp_path / "operator-override-bin")
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    env_mod.publish_config_path_dirs([override])

    # A deliberate pin that ALSO occurs in the augmented block: it must stay
    # first and appear once, the same as any other stored entry.
    monkeypatch.setenv("PATH", cli_path)
    augmented = augmented_path(cli_path).split(os.pathsep)
    pin = next(entry for entry in augmented if entry not in cli_path.split(os.pathsep))
    stored = emit_env({"PATH": pin})["PATH"].split(os.pathsep)
    assert stored[0] == pin
    assert override not in stored

    monkeypatch.setenv("PATH", daemon_path)
    captured = await _captured_backend_env(
        monkeypatch,
        tmp_path,
        {"PATH": daemon_path},
        {"PATH": os.pathsep.join(stored)},
    )
    actual = captured["PATH"].split(os.pathsep)

    assert actual[: len(stored)] == stored
    tail = actual[len(stored) :]
    assert tail[0] == override
    assert not set(tail) & set(stored)
    assert set(tail) - {override} <= set(daemon_path.split(os.pathsep))
    assert actual.count(pin) == 1


@pytest.mark.asyncio
async def test_windows_authored_path_lands_on_the_inherited_path_key(monkeypatch, tmp_path) -> None:
    """A ``Path`` declaration is applied to the key the child's loader reads.

    On POSIX ``Path`` and ``PATH`` are distinct variables, so writing the
    composed value under the spec's spelling would hand the backend an env with
    no PATH at all. The declared VALUE wins; the daemon's own key spelling stays.
    """
    declared_dir = tmp_path / "windows-spec-bin"
    host_path = os.pathsep.join(["/usr/bin", "/bin"])
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    monkeypatch.setenv("PATH", host_path)
    expected = mcp_search_path(str(declared_dir)).split(os.pathsep)

    captured = await _captured_backend_env(
        monkeypatch,
        tmp_path,
        {"PATH": "/inherited-only", "KEEP": "inherited"},
        {"Path": str(declared_dir)},
    )
    path_keys = [key for key in captured if key.upper() == "PATH"]
    actual = captured["PATH"].split(os.pathsep)

    assert path_keys == ["PATH"]
    assert len(actual) == len(expected)
    assert all(_same_dir(got, want) for got, want in zip(actual, expected))
    assert _same_dir(actual[0], str(declared_dir))


@pytest.mark.asyncio
async def test_no_declared_path_leaves_the_inherited_path_unchanged(monkeypatch, tmp_path) -> None:
    """A declaration with no PATH overlays other keys without rebuilding PATH."""
    inherited_path = os.pathsep.join(["/daemon/launcher-bin", "/usr/bin"])

    def _unexpected(_value: str) -> str:
        raise AssertionError("mcp_search_path must not run without a declared PATH")

    monkeypatch.setattr(gatewayd_mod, "mcp_search_path", _unexpected)
    captured = await _captured_backend_env(
        monkeypatch,
        tmp_path,
        {"PATH": inherited_path, "KEEP": "inherited"},
        {"KEEP": "declared"},
    )

    assert captured["PATH"] == inherited_path
    assert captured["KEEP"] == "declared"
    assert [key for key in captured if key.upper() == "PATH"] == ["PATH"]


# ------------------------------------------- the mechanism the rc=127 came from


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX #! wrapper + /bin/sh exec")
def test_a_bare_name_wrapper_exits_127_without_the_launcher_dir(tmp_path) -> None:
    """Reproduce the failure and its fix with a real wrapper and a real exec.

    ``tool_dir`` stands in for the launcher tool's own bin dir and ``wrapper``
    for the one-line script that exec's it by bare name. Under a PATH missing
    ``tool_dir`` the exec fails and the process exits 127 -- exactly the code
    the daemon reported as "backend gone". Adding the directory is the whole
    difference.
    """
    tool_dir = tmp_path / "toolbox-bin"
    tool_dir.mkdir()
    tool = tool_dir / "faux-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    wrapper = tmp_path / "faux-mcp-server"
    wrapper.write_text('#!/bin/sh\nexec faux-tool start-server x "$@"\n')
    wrapper.chmod(0o755)

    without = subprocess.run(
        [str(wrapper)],
        env={"PATH": os.pathsep.join(["/usr/bin", "/bin"])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    assert without.returncode == 127

    with_dir = subprocess.run(
        [str(wrapper)],
        env={"PATH": os.pathsep.join([str(tool_dir), "/usr/bin", "/bin"])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    assert with_dir.returncode == 0
