"""Operator-named shared MCP backends receive the gateway's boot-time ssh-agent socket.

Every pooled backend runs with ``SSH_AUTH_SOCK`` scrubbed (``manager._spawn_once``
before gatewayd, ``env_target_resolver`` again per backend). The operator can name
backends in ``ssh_auth_sock_consent.json`` (``mcp_gateway_backends``); the gateway
then hands gatewayd the socket it was STARTED with (``agent_socket.DAEMON_ENV``),
and gatewayd adds it back for exactly those names, after the scrub.

Pinned here, each negative beside a positive control taken the same way:

* the grant is a separate, strict field: one bad entry voids it, and it is
  independent of the agent-spawn ``enabled`` bit;
* the value is the boot value, carried across an in-app exec, never a socket the
  live environment was repointed at later;
* the daemon env and command line are unchanged without a grant, a boot socket,
  or on Windows, and the socket never travels on argv;
* gatewayd re-reads the grant on every resolve, offers the socket only to a
  granted, approved, non-control-plane name with a vetted path, re-judges it
  before the fork, and never widens what may spawn;
* the shared scrub itself is unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import platform_compat
from kiro_crew import sandbox as sandbox_mod
from kiro_crew import ssh_auth_sock_consent
from kiro_crew.mcp_gateway import agent_socket
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.mcp_gateway import manager as manager_mod
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.hashing import hash_command
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.testing.wait import async_wait_until

#: The real approval store decides in this module: the forward must never widen
#: what gatewayd is willing to spawn.
ENFORCE_LAUNCH_APPROVAL = True

_POSIX_ONLY = pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="unix sockets, POSIX paths and uid ownership"
)
_FIELD = ssh_auth_sock_consent.MCP_GATEWAY_BACKENDS_FIELD
_MARKER = agent_socket.BOOT_MARKER_ENV
_DAEMON_ENV = agent_socket.DAEMON_ENV
_APPROVED_CMD = sys.executable


@pytest.fixture(autouse=True)
def home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    # The floor's own instance: a test's monkeypatch.undo() must not drop this pin.
    # The floor's _fresh_reexec_environment already resets the boot-socket latch.
    data_home = tmp_path / "home"
    data_home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    _floor_monkeypatch.delenv(_MARKER, raising=False)
    _floor_monkeypatch.delenv(_DAEMON_ENV, raising=False)
    return data_home


def _grant(home: Path, payload: Any) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (home / "ssh_auth_sock_consent.json").write_text(text, encoding="utf-8")


def _private_dir(label: str) -> str:
    # Short-rooted, not tmp_path: a macOS tmp_path is too long for sun_path.
    directory = tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + label, dir=short_tmp_base())
    os.chmod(directory, 0o700)
    return directory


@pytest.fixture
def private_dir() -> Iterator[str]:
    directory = _private_dir("agentsock-")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def link_dir() -> Iterator[str]:
    directory = _private_dir("agentlink-")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def live_socket(private_dir: str) -> Iterator[str]:
    path = os.path.join(private_dir, "agent.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        yield path
    finally:
        server.close()


def _foreign_lstat(monkeypatch: pytest.MonkeyPatch, foreign: str) -> None:
    """Make ``os.lstat`` report *foreign* as owned by another user, nothing else."""
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if os.fspath(path) != foreign:
            return result
        fields = list(result)
        fields[4] = result.st_uid + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", _lstat)


# --- the grant ---------------------------------------------------------------


def test_grant_names_the_listed_servers(home: Path) -> None:
    _grant(home, {_FIELD: ["notes-mcp", "mail-mcp"]})
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == frozenset(
        {"notes-mcp", "mail-mcp"}
    )


@pytest.mark.parametrize("payload", [None, "{}\n", "not json", "[]", {_FIELD: None}])
def test_grant_is_empty_for_absent_precreated_or_malformed_stores(home: Path, payload) -> None:
    if payload is not None:
        _grant(home, payload)
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == frozenset()
    # Control: the same home answers a well-formed grant.
    _grant(home, {_FIELD: ["notes-mcp"]})
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == {"notes-mcp"}


@pytest.mark.parametrize(
    "bad", [7, "", " notes-mcp", "notes-mcp\n", "*", "a b", "notes/mcp", ["x"]]
)
def test_one_bad_entry_voids_the_whole_field(home: Path, bad) -> None:
    _grant(home, {_FIELD: ["notes-mcp", bad]})
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == frozenset()
    # Control: the same list without the bad entry grants its one name.
    _grant(home, {_FIELD: ["notes-mcp"]})
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == {"notes-mcp"}


@pytest.mark.parametrize("not_a_list", ["notes-mcp", {"notes-mcp": True}, True])
def test_a_field_that_is_not_a_list_grants_nothing(home: Path, not_a_list) -> None:
    _grant(home, {_FIELD: not_a_list})
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == frozenset()


def test_the_backend_grant_and_the_agent_grant_are_independent(home: Path) -> None:
    _grant(home, {"enabled": True})
    assert ssh_auth_sock_consent.is_granted() is True
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == frozenset()
    _grant(home, {_FIELD: ["notes-mcp"]})
    assert ssh_auth_sock_consent.is_granted() is False
    assert ssh_auth_sock_consent.mcp_gateway_backends_granted() == {"notes-mcp"}


# --- the boot value ------------------------------------------------------------


@_POSIX_ONLY
def test_record_takes_the_launch_value_and_drops_the_marker() -> None:
    environ = {"SSH_AUTH_SOCK": "/run/user/1/agent.sock", _MARKER: "/run/user/1/agent.sock"}
    agent_socket.record_boot_agent_socket(environ)
    assert agent_socket.boot_agent_socket() == "/run/user/1/agent.sock"
    assert _MARKER not in environ


@_POSIX_ONLY
def test_first_record_wins() -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/a.sock"})
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/b.sock"})
    assert agent_socket.boot_agent_socket() == "/run/a.sock"
    # Control: a fresh process takes its own value.
    agent_socket._reset_for_tests()
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/b.sock"})
    assert agent_socket.boot_agent_socket() == "/run/b.sock"


@_POSIX_ONLY
def test_an_exec_successor_takes_the_carried_value_not_a_repointed_one() -> None:
    repointed = {"SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1", _MARKER: "/home/u/.link/agent.sock"}
    agent_socket.record_boot_agent_socket(repointed)
    assert agent_socket.boot_agent_socket() == "/home/u/.link/agent.sock"
    # Control: without a marker this is a first launch, and its own value counts.
    agent_socket._reset_for_tests()
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1"})
    assert agent_socket.boot_agent_socket() == "/tmp/ssh-x/agent.1"


def test_an_empty_carry_means_the_lineage_started_without_a_socket() -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1", _MARKER: ""})
    assert agent_socket.boot_agent_socket() is None


@_POSIX_ONLY
@pytest.mark.parametrize("value", ["agent.sock", "/run/a\x00b", "/" + "a" * 200, ""])
def test_an_implausible_value_records_none(value: str) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": value})
    assert agent_socket.boot_agent_socket() is None
    # Control: an absolute, short path is recorded.
    agent_socket._reset_for_tests()
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/a.sock"})
    assert agent_socket.boot_agent_socket() == "/run/a.sock"


@_POSIX_ONLY
def test_the_carry_reaches_only_the_exec_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/a.sock")
    monkeypatch.setenv(_MARKER, "/run/a.sock")
    agent_socket.record_boot_agent_socket()
    assert _MARKER not in os.environ
    assert platform_compat.kept_for_reexec()[_MARKER] == "/run/a.sock"


def test_a_lineage_without_a_socket_carries_an_empty_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    agent_socket.record_boot_agent_socket()
    assert platform_compat.kept_for_reexec()[_MARKER] == ""


def test_cli_records_before_the_platform_boots() -> None:
    """``cli.main`` records the socket for ``gateway`` before ``boot_platform`` runs,
    so no companion code can have repointed ``SSH_AUTH_SOCK`` yet."""
    import kiro_crew.cli as cli_mod

    tree = ast.parse(Path(cli_mod.__file__).read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    record_line = boot_line = None
    guarded = False
    for node in ast.walk(main):
        if isinstance(node, ast.If) and "args.command == 'gateway'" in ast.unparse(node.test):
            if any(
                isinstance(n, ast.Call) and ast.unparse(n.func).endswith("record_boot_agent_socket")
                for n in ast.walk(node)
            ):
                guarded = True
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if name.endswith("record_boot_agent_socket") and record_line is None:
                record_line = node.lineno
            if name == "boot_platform" and boot_line is None:
                boot_line = node.lineno
    # Control: both calls were found, so the ordering below is a real comparison.
    assert record_line is not None and boot_line is not None
    assert guarded
    assert record_line < boot_line


# --- path vetting --------------------------------------------------------------


@_POSIX_ONLY
def test_a_live_socket_in_a_private_dir_is_offered(live_socket: str) -> None:
    assert agent_socket.agent_socket_refusal(live_socket) is None


@_POSIX_ONLY
def test_a_link_to_a_live_socket_is_offered(live_socket: str, link_dir: str) -> None:
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(live_socket, link)
    assert agent_socket.agent_socket_refusal(link) is None


@_POSIX_ONLY
def test_a_relative_link_resolves_against_its_own_directory(private_dir: str) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(os.path.join(private_dir, "agent.sock"))
    try:
        link = os.path.join(private_dir, "agent-link.sock")
        os.symlink("agent.sock", link)
        assert agent_socket.agent_socket_refusal(link) is None
    finally:
        server.close()


@_POSIX_ONLY
def test_a_missing_entry_in_a_private_dir_is_pending(private_dir: str, link_dir: str) -> None:
    assert agent_socket.agent_socket_refusal(os.path.join(private_dir, "later.sock")) is None
    # A link into an existing private dir whose socket is not there yet, likewise.
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(os.path.join(private_dir, "later.sock"), link)
    assert agent_socket.agent_socket_refusal(link) is None


@_POSIX_ONLY
def test_a_link_into_a_missing_dir_is_refused(private_dir: str, link_dir: str) -> None:
    """A gone per-connection directory is the state another user could recreate."""
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(os.path.join(private_dir, "gone", "agent.1"), link)
    assert agent_socket.agent_socket_refusal(link) == (
        "its link target: its directory does not exist"
    )
    # Control: once the directory exists, privately, the link is pending.
    os.mkdir(os.path.join(private_dir, "gone"), 0o700)
    assert agent_socket.agent_socket_refusal(link) is None


@_POSIX_ONLY
def test_a_socket_in_a_dir_others_can_write_is_refused(live_socket: str, private_dir: str) -> None:
    os.chmod(private_dir, 0o770)
    assert agent_socket.agent_socket_refusal(live_socket) == (
        "its directory is writable by other users"
    )
    # Control: the same socket once the directory is private again.
    os.chmod(private_dir, 0o700)
    assert agent_socket.agent_socket_refusal(live_socket) is None


@_POSIX_ONLY
def test_a_dir_another_user_owns_is_refused(
    live_socket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    assert agent_socket.agent_socket_refusal(live_socket) == (
        "its directory belongs to another user"
    )
    # Control: the real owner is offered it.
    monkeypatch.setattr(os, "geteuid", lambda: real)
    assert agent_socket.agent_socket_refusal(live_socket) is None


@_POSIX_ONLY
def test_a_dir_that_is_another_users_link_is_refused(
    private_dir: str, link_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished per-connection directory recreated as a link into one of the
    operator's own private directories must not pass as the operator's."""
    aliased = os.path.join(link_dir, "ssh-gone")
    os.symlink(private_dir, aliased)
    path = os.path.join(aliased, "agent.1")
    # Control: the operator's own link to the operator's own directory passes.
    assert agent_socket.agent_socket_refusal(path) is None
    _foreign_lstat(monkeypatch, aliased)
    assert agent_socket.agent_socket_refusal(path) == "its directory is a link another user owns"


@_POSIX_ONLY
def test_a_socket_another_user_owns_is_refused(
    live_socket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _foreign_lstat(monkeypatch, live_socket)
    assert agent_socket.agent_socket_refusal(live_socket) == "it belongs to another user"


@_POSIX_ONLY
def test_a_link_target_another_user_owns_is_refused(
    live_socket: str, link_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(live_socket, link)
    _foreign_lstat(monkeypatch, live_socket)
    assert agent_socket.agent_socket_refusal(link) == "its link target belongs to another user"


@_POSIX_ONLY
def test_a_regular_file_or_a_missing_dir_is_refused(private_dir: str, link_dir: str) -> None:
    regular = os.path.join(private_dir, "not-a-socket")
    Path(regular).write_text("x", encoding="utf-8")
    assert agent_socket.agent_socket_refusal(regular) == "it is not a socket or a link to one"
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(regular, link)
    assert agent_socket.agent_socket_refusal(link) == "its link target is not a socket"
    assert agent_socket.agent_socket_refusal(os.path.join(private_dir, "gone", "a.sock")) == (
        "its directory does not exist"
    )


@_POSIX_ONLY
def test_a_link_into_a_dir_others_can_write_is_refused(
    live_socket: str, private_dir: str, link_dir: str
) -> None:
    link = os.path.join(link_dir, "agent-link.sock")
    os.symlink(live_socket, link)
    os.chmod(private_dir, 0o777)
    assert agent_socket.agent_socket_refusal(link) == (
        "its link target: its directory is writable by other users"
    )
    os.chmod(private_dir, 0o700)
    assert agent_socket.agent_socket_refusal(link) is None


def test_windows_is_never_offered_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert agent_socket.agent_socket_refusal("/run/a.sock") == "not offered on Windows"


# --- the gateway side: what gatewayd is spawned with ---------------------------


class _SpawnCaptured(RuntimeError):
    pass


def _captured_daemon_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[str], dict[str, str]]:
    captured: dict[str, Any] = {}

    async def _fake_spawn(*argv, env=None, **_kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})
        raise _SpawnCaptured

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    monkeypatch.setattr(manager, "_gatewayd_log_path", lambda: tmp_path / "gatewayd.stdout")
    monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])
    with pytest.raises(_SpawnCaptured):
        asyncio.run(manager._spawn_once())
    return captured["argv"], captured["env"]


@_POSIX_ONLY
def test_no_grant_leaves_the_daemon_env_unchanged(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})
    _argv, env = _captured_daemon_spawn(monkeypatch, tmp_path)
    assert _DAEMON_ENV not in env
    # Control: the same gateway with a grant hands the daemon its boot socket.
    _grant(home, {_FIELD: ["notes-mcp"]})
    argv, env = _captured_daemon_spawn(monkeypatch, tmp_path)
    assert env[_DAEMON_ENV] == "/run/boot.sock"
    # Never on the command line, which every local process can read.
    assert not [part for part in argv if "/run/boot.sock" in part]


@_POSIX_ONLY
def test_the_daemon_gets_the_boot_socket_not_the_live_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-x/agent.9")
    _grant(home, {_FIELD: ["notes-mcp"]})
    _argv, env = _captured_daemon_spawn(monkeypatch, tmp_path)
    assert env[_DAEMON_ENV] == "/run/boot.sock"
    # The daemon's own SSH_AUTH_SOCK stays scrubbed; DAEMON_ENV is the only channel.
    assert [key for key in env if key.startswith("SSH_AUTH_SOCK")] == [_DAEMON_ENV]
    assert env["PATH"]


def test_the_agent_spawn_grant_alone_hands_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})
    _grant(home, {"enabled": True})
    _argv, env = _captured_daemon_spawn(monkeypatch, tmp_path)
    assert _DAEMON_ENV not in env


@_POSIX_ONLY
def test_a_grant_without_a_boot_socket_hands_nothing_and_says_why(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-x/agent.9")
    _grant(home, {_FIELD: ["notes-mcp"]})
    with caplog.at_level(logging.WARNING, logger=manager_mod.logger.name):
        assert GatewayManager._backend_agent_socket() is None
    assert any("notes-mcp" in r.getMessage() for r in caplog.records)
    # Control: with a recorded boot socket the same grant hands it over.
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})
    assert GatewayManager._backend_agent_socket() == "/run/boot.sock"


@_POSIX_ONLY
def test_windows_hands_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})
    _grant(home, {_FIELD: ["notes-mcp"]})
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert GatewayManager._backend_agent_socket() is None
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert GatewayManager._backend_agent_socket() == "/run/boot.sock"


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="the native Windows case")
def test_native_windows_hands_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_socket, "_boot_socket", "C:\\agent\\agent.sock")
    _grant(home, {_FIELD: ["notes-mcp"]})
    assert GatewayManager._backend_agent_socket() is None


def test_an_unreadable_grant_hands_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_socket.record_boot_agent_socket({"SSH_AUTH_SOCK": "/run/boot.sock"})

    def _boom() -> frozenset[str]:
        raise OSError("store on fire")

    monkeypatch.setattr(ssh_auth_sock_consent, "mcp_gateway_backends_granted", _boom)
    assert GatewayManager._backend_agent_socket() is None


# --- gatewayd takes it out of its own env ------------------------------------------


@pytest.fixture
def quiet_amain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.setattr(gw.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(gw, "warm_backend", lambda: None)
    return ["--socket", str(tmp_path / "cov.sock")]


@pytest.mark.asyncio
async def test_amain_passes_no_socket_by_default(
    quiet_amain: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = AsyncMock()
    monkeypatch.setattr(gw, "run_gatewayd", run)
    await gw._amain(quiet_amain)
    assert run.await_args is not None
    assert run.await_args.kwargs["backend_agent_socket"] is None


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_amain_takes_the_socket_out_of_its_env(
    quiet_amain: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = AsyncMock()
    monkeypatch.setattr(gw, "run_gatewayd", run)
    monkeypatch.setenv(_DAEMON_ENV, "/run/a.sock")
    await gw._amain(quiet_amain)
    assert run.await_args is not None
    assert run.await_args.kwargs["backend_agent_socket"] == "/run/a.sock"
    assert _DAEMON_ENV not in os.environ


def test_an_implausible_daemon_value_is_dropped() -> None:
    environ = {_DAEMON_ENV: "relative.sock"}
    assert agent_socket.take_daemon_socket(environ) is None
    assert _DAEMON_ENV not in environ


# --- the daemon side: the resolver wrapper -------------------------------------


class _Sel:
    rows: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        type(self).rows.append(kwargs)


@pytest.fixture
def sel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _Sel.rows = []
    monkeypatch.setattr(gw, "SecurityEventLog", _Sel)
    return _Sel.rows


def _inner(env: dict[str, str] | None = None):
    target_env = {"PATH": "/usr/bin", "HOME": "/home/u"} if env is None else env
    return lambda _key: ("demo-bin", ["--stdio"], target_env, "/work")


def _key(name: str) -> Any:
    return SimpleNamespace(server_name=name)


@_POSIX_ONLY
def test_only_a_granted_server_gets_the_socket(
    home: Path, live_socket: str, sel: list[dict[str, Any]]
) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket
    assert "SSH_AUTH_SOCK" not in resolver(_key("mail-mcp"))[2]
    assert [(r["resources"], r["outcome"]) for r in sel] == [("notes-mcp", "allowed")]


@_POSIX_ONLY
def test_a_refused_launch_stays_refused(home: Path, live_socket: str) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(lambda _key: None, live_socket)
    assert resolver(_key("notes-mcp")) is None


@_POSIX_ONLY
@pytest.mark.parametrize("spelling", ["exact", "variant"])
def test_a_control_plane_backend_never_gets_it_even_if_named(
    home: Path, live_socket: str, spelling: str
) -> None:
    control_plane = sorted(gw.CONTROL_PLANE_BACKENDS)[0]
    name = control_plane if spelling == "exact" else control_plane.upper().replace("-", "_")
    _grant(home, {_FIELD: [name, "notes-mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert "SSH_AUTH_SOCK" not in resolver(_key(name))[2]
    # Control: the other name in the same grant is offered it.
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket


@_POSIX_ONLY
def test_names_match_exactly(home: Path, live_socket: str) -> None:
    _grant(home, {_FIELD: ["notes_mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert "SSH_AUTH_SOCK" not in resolver(_key("notes-mcp"))[2]
    assert resolver(_key("notes_mcp"))[2]["SSH_AUTH_SOCK"] == live_socket


@_POSIX_ONLY
def test_the_inner_env_is_never_mutated(home: Path, live_socket: str) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    inner_env = {"PATH": "/usr/bin"}
    resolver = gw.agent_socket_resolver(_inner(inner_env), live_socket)
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket
    assert inner_env == {"PATH": "/usr/bin"}


@_POSIX_ONLY
def test_a_revoke_applies_at_the_next_resolve(
    home: Path, live_socket: str, sel: list[dict[str, Any]]
) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket
    _grant(home, {_FIELD: []})
    assert "SSH_AUTH_SOCK" not in resolver(_key("notes-mcp"))[2]
    assert [r["outcome"] for r in sel] == ["allowed", "denied"]


@_POSIX_ONLY
def test_a_refused_path_is_not_offered_and_is_audited_once(
    home: Path, live_socket: str, private_dir: str, sel: list[dict[str, Any]]
) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    os.chmod(private_dir, 0o777)
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert "SSH_AUTH_SOCK" not in resolver(_key("notes-mcp"))[2]
    assert "SSH_AUTH_SOCK" not in resolver(_key("notes-mcp"))[2]
    assert [(r["outcome"], r["error"]) for r in sel] == [
        ("denied", "its directory is writable by other users")
    ]
    # Control: once the directory is private the next resolve offers it.
    os.chmod(private_dir, 0o700)
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket
    assert [r["outcome"] for r in sel] == ["denied", "allowed"]


@_POSIX_ONLY
def test_an_unreadable_grant_fails_closed(
    home: Path, live_socket: str, monkeypatch: pytest.MonkeyPatch, sel: list[dict[str, Any]]
) -> None:
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    # Control: the readable grant offers this socket to this name.
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket

    def _boom() -> frozenset[str]:
        raise OSError("store on fire")

    monkeypatch.setattr(ssh_auth_sock_consent, "mcp_gateway_backends_granted", _boom)
    assert "SSH_AUTH_SOCK" not in resolver(_key("notes-mcp"))[2]
    assert [r["outcome"] for r in sel] == ["allowed", "denied"]


@_POSIX_ONLY
def test_an_audit_failure_never_breaks_resolution(
    home: Path, live_socket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken:
        def log_api_access(self, **_kwargs: Any) -> None:
            raise RuntimeError("sel on fire")

    monkeypatch.setattr(gw, "SecurityEventLog", _Broken)
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(_inner(), live_socket)
    assert resolver(_key("notes-mcp"))[2]["SSH_AUTH_SOCK"] == live_socket


# --- the real resolver and the real approval store -------------------------------


def _pool_key(server: str) -> PoolKey:
    return PoolKey(
        server_name=server,
        command_args_hash=hash_command(_APPROVED_CMD, []),
        effective_env_hash=launch_approval.env_fingerprint({}),
        work_dir="/work",
        binary_version="",
        os_uid=os.getuid() if hasattr(os, "getuid") else 0,
    )


def _approve(server: str) -> None:
    approvals = launch_approval.LaunchApprovals()
    stem = launch_approval.target_stem(server)
    approvals.approved[stem] = {launch_approval.launch_fingerprint(_APPROVED_CMD, [], {})}
    approvals.approved_pairs[stem] = {
        launch_approval.launch_pair(
            hash_command(_APPROVED_CMD, []), launch_approval.env_fingerprint({})
        )
    }
    launch_approval._write(
        launch_approval.approvals_path(), launch_approval._document(approvals, {})
    )


@pytest.fixture
def daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    target = shlex.quote(_APPROVED_CMD)
    monkeypatch.setenv("KIROCREW_MCP_TARGET_NOTES_MCP", target)
    monkeypatch.setenv("KIROCREW_MCP_TARGET_OTHER_MCP", target)
    # What a daemon spawned by an older gateway, or by hand, might still carry.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/inherited/agent.sock")


@_POSIX_ONLY
def test_an_approved_granted_backend_gets_the_handed_value(
    home: Path, live_socket: str, daemon_env: None
) -> None:
    _approve("notes-mcp")
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(gw.env_target_resolver, live_socket)
    target = resolver(_pool_key("notes-mcp"))
    assert target is not None
    assert target[2]["SSH_AUTH_SOCK"] == live_socket


@_POSIX_ONLY
def test_an_approved_ungranted_backend_stays_scrubbed(
    home: Path, live_socket: str, daemon_env: None
) -> None:
    _approve("other-mcp")
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(gw.env_target_resolver, live_socket)
    target = resolver(_pool_key("other-mcp"))
    assert target is not None
    assert "SSH_AUTH_SOCK" not in target[2]


@_POSIX_ONLY
def test_an_unapproved_backend_stays_refused_whatever_the_grant(
    home: Path, live_socket: str, daemon_env: None
) -> None:
    _approve("other-mcp")
    _grant(home, {_FIELD: ["notes-mcp"]})
    resolver = gw.agent_socket_resolver(gw.env_target_resolver, live_socket)
    assert resolver(_pool_key("notes-mcp")) is None
    # Control: the approved name in the same daemon resolves.
    assert resolver(_pool_key("other-mcp")) is not None


# --- run_gatewayd wiring ---------------------------------------------------------


@_POSIX_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("socket_path", [None, "/run/a.sock"])
async def test_run_gatewayd_serves_through_the_wrapper_only_when_given_a_socket(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch, socket_path: str | None
) -> None:
    """The resolver the connection handler receives IS the wrapper, or is not."""
    wrapped: list[Any] = []
    served: list[Any] = []
    real = gw.agent_socket_resolver

    def _recording(inner, path):
        wrapper = real(inner, path)
        wrapped.append(wrapper)
        return wrapper

    async def _serve(_reader, writer, _pool, resolver, *_args, **_kwargs):
        served.append(resolver)
        writer.close()

    monkeypatch.setattr(gw, "agent_socket_resolver", _recording)
    monkeypatch.setattr(gw, "_handle_connection", _serve)
    monkeypatch.setattr(gw, "_OWNER_PID", gw._OWNER_PID)
    monkeypatch.setattr(gw, "_OWN_START_TIME", gw._OWN_START_TIME)
    sock = short_sock_dir / "wrap.sock"
    stop = asyncio.Event()
    run = asyncio.create_task(
        gw.run_gatewayd(
            sock,
            max_backends=2,
            idle_timeout_secs=60,
            stop_event=stop,
            target_resolver=lambda _key: None,
            backend_agent_socket=socket_path,
        )
    )
    try:
        await async_wait_until(lambda: sock.exists() or run.done(), describe=lambda: run)
        assert not run.done(), run
        _reader, writer = await asyncio.wait_for(transport.connect(sock), timeout=30)
        writer.close()
        await async_wait_until(lambda: served or run.done(), describe=lambda: run)
    finally:
        stop.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(run, timeout=30)
    assert len(served) == 1
    if socket_path is None:
        assert wrapped == []
    else:
        assert served[0] is wrapped[0]


# --- the final backend env ------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_after_the_resolver_drops_the_offered_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_mcp_gateway_daemon_path import _captured_backend_env

    env = await _captured_backend_env(
        monkeypatch,
        tmp_path,
        {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/run/offered.sock"},
        {},
    )
    assert env["SSH_AUTH_SOCK"] == "/run/offered.sock"


def _queued_key(tmp_path: Path) -> PoolKey:
    return PoolKey(
        server_name="notes-mcp",
        command_args_hash="a" * 8,
        effective_env_hash="e" * 8,
        work_dir=str(tmp_path),
        binary_version="1.0",
        os_uid=1000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["withdrawn", "kept", "granted"])
async def test_a_decision_changed_while_queued_is_not_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    """The pre-fork re-resolve re-judges the socket: a grant withdrawn, or a path
    refused, while the spawn waited for capacity must not reach the child. A
    socket that became available meanwhile is left out, as first decided."""
    process = MagicMock()
    process.returncode = None
    process.pid = 99_999_999_999
    now = time.monotonic()
    backend = Backend(
        pool_key=_queued_key(tmp_path),
        process=process,
        stdin=MagicMock(),
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )

    async def _noop_pump() -> None:
        return None

    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    spawn = AsyncMock(return_value=backend)
    monkeypatch.setattr(gw, "spawn_backend", spawn)
    monkeypatch.setattr(gw, "_declared_env_to_forward", lambda _key: {})
    monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, _home: (dict(env), set()))
    monkeypatch.setattr(gw, "_spawns_own_control_plane", lambda *_a, **_kw: False)
    resolves: list[int] = []

    def _resolver(_key):
        resolves.append(1)
        first = len(resolves) == 1
        env = {"PATH": "/usr/bin"}
        if (
            (change == "withdrawn" and first)
            or change == "kept"
            or (change == "granted" and not first)
        ):
            env["SSH_AUTH_SOCK"] = "/run/offered.sock"
        return "demo-bin", [], env, str(tmp_path)

    if change == "withdrawn":
        with pytest.raises(gw._TargetUnknown):
            await gw._acquire_backend(BackendPool(max_backends=2), _queued_key(tmp_path), _resolver)
        spawn.assert_not_awaited()
    else:
        await gw._acquire_backend(BackendPool(max_backends=2), _queued_key(tmp_path), _resolver)
        assert spawn.await_args is not None
        launched = spawn.await_args.kwargs["env"].get("SSH_AUTH_SOCK")
        # Control: an unchanged grant launches with the socket; a late one without.
        assert launched == ("/run/offered.sock" if change == "kept" else None)
        assert backend._stdout_task is not None
        await backend._stdout_task
    assert len(resolves) == 2


# --- the shared scrub is unchanged --------------------------------------------------


def test_the_forward_leaves_the_shared_scrub_alone() -> None:
    """The sibling of ``test_shared_constant_never_mutated``: the socket is added
    back after the scrub, on its own channel, so neither prefix list changes and
    the scrub still removes it -- and both handover names -- everywhere else."""
    assert "SSH_AUTH_SOCK" in sandbox_mod._SENSITIVE_ENV_PREFIXES
    assert "SSH_AUTH_SOCK" in manager_mod._SENSITIVE_ENV_PREFIXES
    for key in ("SSH_AUTH_SOCK", _MARKER, _DAEMON_ENV):
        assert manager_mod.is_credential_env_key(key), key
    scrubbed = manager_mod._scrub_sensitive_env(
        {"SSH_AUTH_SOCK": "/a", _MARKER: "/b", _DAEMON_ENV: "/c", "PATH": "/usr/bin"}
    )
    assert scrubbed == {"PATH": "/usr/bin"}
