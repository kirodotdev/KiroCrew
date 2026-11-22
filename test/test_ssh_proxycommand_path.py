"""An ssh child's ProxyCommand must find its tools under a GUI-launched gateway.

A macOS desktop-app gateway inherits launchd's minimal ``PATH``. The ssh
transport ran every ssh child (tunnel, token mint, remote restart, diagnostics
probes) with that PATH, so a host routed through a ``~/.ssh/config``
``ProxyCommand`` that runs an SSM connect helper -- which looks
``session-manager-plugin`` up by name -- failed with
``ssh exited 255: Error: session-manager-plugin is not installed`` while the
plugin sat in ``/usr/local/bin``. Only the SSM transport's ``aws`` child got the
install dirs appended.

The spawn tests below run a REAL child with exactly the ``env=`` production
hands ``ssh``. The child is a Python stand-in for ssh + its ProxyCommand: it
looks ``session-manager-plugin`` up by name on its OWN ``PATH``, which is the
lookup the proxy does. So the verdict is whether the plugin is reachable from
the child's actual environment, not whether some kwarg was passed. The stand-in
is launched through the interpreter rather than as a shell script so the same
test runs on every platform (a ``.cmd`` shim would put the remote command
line through ``cmd.exe`` parsing). The argv head production resolved is
recorded and checked separately.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.deploy import engine
from kiro_crew.instances import diagnostics, token_mint
from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# The spawn cases re-point asyncio.create_subprocess_exec at a real interpreter child
# (the ssh + ProxyCommand stand-in). Under -n auto --dist loadgroup those cold-starts
# would scatter across workers and stall a shard; this group pins the whole module to one
# worker so its children run serially on a single interpreter lane. The refusal-message
# cases need no live child and use a fake process (see refused_spawn).
pytestmark = pytest.mark.xdist_group(name="ssh_proxycommand_spawn")

_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
_NOT_INSTALLED = (
    "Error: session-manager-plugin is not installed. Please run the setup command first"
)

# What OpenSSH does with `ProxyCommand /abs/path/connect-helper %h`: it runs the
# proxy under its own PATH, and the proxy looks the plugin up by name. On
# success it prints what a remote `kirocrew token` would, or KC_STANDIN_OUT when
# a test sets it. KC_STANDIN_RC lets a test choose a different outcome.
_STANDIN = f"""
import os, shutil, sys
rc = os.environ.get("KC_STANDIN_RC")
if rc:
    sys.stderr.write(os.environ.get("KC_STANDIN_ERR", "") + "\\n")
    sys.exit(int(rc))
if shutil.which("session-manager-plugin") is None:
    sys.stderr.write({_NOT_INSTALLED!r} + "\\n")
    sys.stderr.write("kex_exchange_identification: Connection closed by remote host\\n")
    sys.exit(255)
sys.stdout.write(os.environ.get("KC_STANDIN_OUT", "http://localhost:7777?token={_TOKEN}") + "\\n")
"""


def _plant(directory: Path, name: str) -> Path:
    """An executable ``name`` that ``shutil.which`` resolves on this platform."""
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text("@echo off\r\n")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return path


@pytest.fixture
def gui_gateway(monkeypatch, tmp_path):
    """A gateway PATH holding ``ssh`` only; the plugin in an install dir.

    Mirrors a Finder/Dock launch: ``ssh`` is in the system bin dir the minimal
    PATH carries, ``session-manager-plugin`` is in ``/usr/local/bin`` (here a
    tmp stand-in patched into ``_AWS_BIN_DIRS``), which that PATH lacks.

    Every ``asyncio.create_subprocess_exec`` is recorded and re-pointed at the
    interpreter running :data:`_STANDIN`, with the production ``env=`` intact.
    """
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    _plant(sysbin, "ssh")
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    _plant(install, "session-manager-plugin")
    standin = tmp_path / "ssh_standin.py"
    standin.write_text(_STANDIN)
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(sysbin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)
    # Accept the planted tools on every platform: the real check's verdict on a tmp
    # dir depends on the account running the suite (root, a Windows CI runner's
    # ACLs). A refusal test sets its own check after this one.
    monkeypatch.setattr(github_runner, "validate_provider_executable", lambda candidate: candidate)

    real_exec = asyncio.create_subprocess_exec
    heads: list[str] = []

    async def exec_standin(*argv, **kw):
        heads.append(argv[0])
        return await real_exec(sys.executable, str(standin), *argv[1:], **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_standin)
    return sysbin, install, heads


class _FakeSshProcess:
    """A stand-in for the ssh child that answers without a real interpreter.

    The refusal-message cases check how a refused plugin maps to the error text,
    not real OS PATH inheritance, so they do not need a live child: this returns
    the stderr a ProxyCommand prints when the plugin is unreachable (exit 255),
    through the ``communicate`` / ``wait`` / ``stderr``-drain seams production
    reads. ``stdout`` and ``stderr`` are real ``StreamReader``s so the tunnel's
    concurrent drain works unchanged.
    """

    def __init__(self, stderr_text: str, returncode: int) -> None:
        self.returncode = returncode
        self.pid = UNALLOCATABLE_PID
        self._stdout_bytes = b""
        self._stderr_bytes = stderr_text.encode("utf-8")
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(self._stdout_bytes)
        self.stdout.feed_eof()
        self.stderr.feed_data(self._stderr_bytes)
        self.stderr.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout_bytes, self._stderr_bytes

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def send_signal(self, _sig: int) -> None:
        return None


@pytest.fixture
def refused_spawn(gui_gateway, monkeypatch):
    """``gui_gateway`` env, but the ssh child is faked, not a real interpreter.

    Used by the refusal-message cases: they assert on the error text a refused
    plugin produces, which production builds from the recorded refusal plus the
    child's exit, so a fake child carrying the plugin-not-found stderr and exit
    255 exercises every production path those tests pin.
    """
    stderr_text = _NOT_INSTALLED + "\nkex_exchange_identification: Connection closed by remote host"

    async def fake_exec(*_argv, **_kw):
        return _FakeSshProcess(stderr_text, 255)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return gui_gateway


def _is_resolved_ssh(head: str, sysbin: Path) -> bool:
    return os.path.isabs(head) and os.path.dirname(head) == str(sysbin)


@pytest.mark.asyncio
async def test_token_mint_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    token = await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    assert token == _TOKEN
    assert _is_resolved_ssh(heads[-1], sysbin)


@pytest.mark.asyncio
async def test_remote_restart_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    rc, err = await token_mint.run_remote_kirocrew("ds-abc", "restart", timeout_secs=10)
    assert (rc, err) == (0, "")
    assert _is_resolved_ssh(heads[-1], sysbin)


@pytest.mark.asyncio
async def test_diagnostics_ssh_probe_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    assert await diagnostics._probe_ssh("ds-abc", connect_timeout_secs=5) is True
    assert _is_resolved_ssh(heads[-1], sysbin)


@pytest.mark.asyncio
async def test_diagnostics_dashboard_probe_through_a_proxycommand_finds_the_plugin(
    gui_gateway, monkeypatch
):
    sysbin, _install, heads = gui_gateway
    # The HTTP status the remote curl prints for a dashboard that answers.
    monkeypatch.setenv("KC_STANDIN_OUT", "200")
    assert await diagnostics._probe_remote_dashboard("ds-abc", 7777, connect_timeout_secs=5)
    assert _is_resolved_ssh(heads[-1], sysbin)


def test_every_ssh_spawn_gets_the_same_env_as_the_ssm_aws_child(gui_gateway):
    """One widening rule: the ssh env is the aws env for the same resolved head."""
    sysbin, install, _heads = gui_gateway
    assert engine._TRUSTED_AWS_BIN_DIRS == ()

    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "-n", "ds-abc", "true"])

    assert _is_resolved_ssh(argv[0], sysbin)
    assert argv[1:] == ["-n", "ds-abc", "true"]
    assert env == engine.aws_spawn_env(argv[0])
    parts = env["PATH"].split(os.pathsep)
    # Appended: the inherited PATH keeps first claim on every name.
    assert parts == [str(sysbin), str(install)]


def test_ssh_env_excludes_a_directory_refused_without_an_aws_walk(monkeypatch, tmp_path):
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    _plant(sysbin, "ssh")
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(sysbin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    from kiro_crew import github_runner

    def _refuse_directory(_candidate):
        raise ValueError("directory trust refused")

    monkeypatch.setattr(github_runner, "validate_provider_directory", _refuse_directory)

    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc"])

    assert _is_resolved_ssh(argv[0], sysbin)
    assert env["PATH"] == str(sysbin)
    assert engine._TRUSTED_AWS_BIN_DIRS == ()


def test_ssh_found_only_in_the_install_dirs_is_not_widened_into_reach(monkeypatch, tmp_path):
    """No ssh on the inherited PATH: stay bare and unwidened, fail as before.

    Widening would let execvp find an ``ssh`` planted in a user-writable
    install dir that nothing vetted -- the fail-closed rule ``aws`` already has.
    """
    empty = tmp_path / "sysbin"
    empty.mkdir()
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    _plant(install, "ssh")
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc"])
    assert argv[0] == "ssh"
    assert env["PATH"] == str(empty)


@pytest.mark.asyncio
async def test_mint_names_the_path_searched_when_the_plugin_is_missing(monkeypatch, gui_gateway):
    sysbin, _install, _heads = gui_gateway
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())  # plugin genuinely absent
    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    msg = str(ei.value)
    assert "ProxyCommand" in msg
    assert f"({sysbin})" in msg
    assert "session-manager-plugin is not installed" in msg  # raw cause kept


@pytest.mark.asyncio
async def test_mint_does_not_blame_the_proxycommand_for_a_remote_command_not_found(
    monkeypatch, gui_gateway
):
    """A remote shell's "command not found" exits 127 and is the remote's problem."""
    monkeypatch.setenv("KC_STANDIN_RC", "127")
    monkeypatch.setenv("KC_STANDIN_ERR", "bash: line 1: kirocrew: command not found")
    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    assert "ProxyCommand" not in str(ei.value)
    assert "exited 127" in str(ei.value)


def test_tunnel_classifies_a_missing_proxy_tool_before_the_transport_drop():
    """ssh's trailing "Connection closed by remote host" must not win.

    That line is what ssh prints once any ProxyCommand exits, so reporting a
    transport drop would send the reader after the network.
    """
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    t._stderr_buf = (
        f"{_NOT_INSTALLED}\nkex_exchange_identification: Connection closed by remote host\n"
    )
    err = t._exit_error(255)
    assert err.startswith("a program your ssh config's ProxyCommand runs was not found")
    assert f"({t._child_path})" in err
    assert "session-manager-plugin is not installed" in err
    assert "transport drop" not in err


@pytest.mark.parametrize(
    "line",
    [
        "zsh:1: command not found: art",
        "/bin/bash: line 1: exec: connect-helper: not found",
        "sh: 1: exec: aws: not found",
        "SessionManagerPlugin is not found. Please refer to SessionManager Documentation here",
    ],
)
def test_tunnel_recognises_each_shells_missing_program_wording(line):
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin"
    t._stderr_buf = f"{line}\n"
    assert "ProxyCommand runs was not found" in t._exit_error(255)


def test_tunnel_only_blames_the_proxycommand_for_an_ssh_level_failure():
    """Not-found prose on a non-255 exit is not ssh's (e.g. a LocalCommand)."""
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin"
    t._stderr_buf = "sh: 1: exec: helper: not found\n"
    assert "ProxyCommand" not in t._exit_error(1)


def test_a_real_auth_failure_is_still_an_auth_failure():
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._stderr_buf = "user@ds-abc: Permission denied (publickey).\n"
    assert t._exit_error(255).startswith("ssh auth failed")


@pytest.mark.asyncio
async def test_tunnel_start_reports_the_missing_plugin_with_the_child_path(
    monkeypatch, gui_gateway
):
    """End to end through ``_SshTunnel.start``: real spawn, real exit, real message."""
    sysbin, _install, heads = gui_gateway
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())  # plugin genuinely absent
    t = _SshTunnel("ds-1", "ds-abc", 1, 2, connect_timeout_secs=10)
    assert await t.start() is False
    assert "ProxyCommand runs was not found" in t.status.error
    assert f"({sysbin})" in t.status.error
    assert _is_resolved_ssh(heads[-1], sysbin)


_REFUSAL = "executable is owned by another user (uid 1501)"
# Why the refusal tests skip on Windows; the gui_gateway fixture says the same.
_POSIX_FALLBACK_DIRS = "fallback install dirs are POSIX literals; dead on Windows by design"


def _refuse_the_plugin(monkeypatch) -> dict[str, bool]:
    """Have the provenance check refuse ``session-manager-plugin`` until ``["repaired"]``.

    No refusal is recorded yet, as on a gateway where no plugin check has run.
    """
    from kiro_crew import github_runner

    state = {"repaired": False}

    def _validate(candidate):
        if os.path.basename(candidate) == "session-manager-plugin" and not state["repaired"]:
            raise ValueError(_REFUSAL)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    return state


_GATEWAY_UID = 1000
_THIRD_PARTY_UID = 1001


def _tree_owned_by(uid: int, prefix: Path) -> type[Path]:
    """A ``Path`` whose ``stat`` reports *uid* as the owner of *prefix* and all below it."""
    root = os.path.realpath(prefix)

    class _OwnedPath(type(Path())):  # type: ignore[misc]
        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            real = super().stat(follow_symlinks=follow_symlinks)
            if os.path.commonpath([root, os.path.realpath(self)]) != root:
                return real
            fields = list(real)
            fields[4] = uid  # st_uid
            return os.stat_result(fields)

    return _OwnedPath


def _refused_in_the_aws_package_tree(monkeypatch, tmp_path, install):
    """A stock AWS ``.pkg`` plugin the check refuses, read once as the cloud preflight does.

    ``install`` links into the package tree, as ``/usr/local/bin`` does. A third
    account owns the tree, so the gateway sees another owner and is offered the
    root-owner fix. Returns the refusal the preflight reports.
    """
    prefix = tmp_path / "sessionmanagerplugin"
    (prefix / "bin").mkdir(parents=True)
    binary = prefix / "bin" / "session-manager-plugin"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    link = install / "session-manager-plugin"
    link.unlink()
    link.symlink_to(binary)
    # The tree's owner and the gateway's uid are pinned, not read from the account
    # running the suite: as root the real owner is 0, which the remedy always allows.
    monkeypatch.setattr(cloud_ssm, "Path", _tree_owned_by(_THIRD_PARTY_UID, prefix))
    monkeypatch.setattr(os, "geteuid", lambda: _GATEWAY_UID)
    monkeypatch.setattr(cloud_ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
    monkeypatch.setattr(cloud_ssm.platform, "system", lambda: "Darwin")
    _refuse_the_plugin(monkeypatch)
    assert cloud_ssm.session_manager_plugin_installed() is False
    refusal = cloud_ssm.session_manager_plugin_refusal()
    assert refusal is not None
    assert refusal.remedy_command == f"sudo chown -R root:wheel {prefix}"
    return refusal


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
def test_a_refused_plugin_is_off_the_ssh_path_with_no_prior_plugin_check(gui_gateway, monkeypatch):
    """The ssh child's PATH does not wait for an SSM check to learn of a refusal.

    A ProxyCommand looks the plugin up by name, so a refused one on that PATH would run.
    """
    sysbin, install, _heads = gui_gateway
    _refuse_the_plugin(monkeypatch)

    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])

    assert _is_resolved_ssh(argv[0], sysbin)
    assert str(install) not in env["PATH"].split(os.pathsep)
    assert shutil.which("session-manager-plugin", path=env["PATH"]) is None


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
def test_a_repaired_plugin_is_back_on_the_next_ssh_path(gui_gateway, monkeypatch):
    """A refusal the cloud preflight recorded clears at the next ssh spawn after a repair."""
    sysbin, install, _heads = gui_gateway
    plugin = _refuse_the_plugin(monkeypatch)
    assert cloud_ssm.session_manager_plugin_installed() is False
    _argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])
    assert str(install) not in env["PATH"].split(os.pathsep)

    plugin["repaired"] = True
    _argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])

    assert env["PATH"].split(os.pathsep) == [str(sysbin), str(install)]


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
def test_removing_a_refused_tool_puts_its_dir_back_on_the_next_ssh_path(gui_gateway, monkeypatch):
    """A refused ``aws`` the operator deletes stops holding the accepted plugin's dir off PATH."""
    sysbin, install, _heads = gui_gateway
    aws = _plant(install, "aws")
    from kiro_crew import github_runner

    def _validate(candidate):
        if os.path.realpath(candidate) == os.path.realpath(aws):
            raise ValueError(_REFUSAL)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    _argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])
    assert str(install) not in env["PATH"].split(os.pathsep)

    aws.unlink()
    _argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])

    assert env["PATH"].split(os.pathsep) == [str(sysbin), str(install)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
@pytest.mark.asyncio
async def test_mint_names_the_refused_plugin_and_its_fix(refused_spawn, monkeypatch, tmp_path):
    _sysbin, install, _heads = refused_spawn
    refusal = _refused_in_the_aws_package_tree(monkeypatch, tmp_path, install)

    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=FALLBACK_TIMEOUT_SECS)

    msg = str(ei.value)
    assert refusal.path in msg
    assert refusal.reason in msg
    assert refusal.remedy_command in msg
    assert "make it reachable from that PATH" not in msg


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
@pytest.mark.asyncio
async def test_tunnel_exit_names_the_refused_plugin_and_its_fix(
    refused_spawn, monkeypatch, tmp_path
):
    _sysbin, install, _heads = refused_spawn
    refusal = _refused_in_the_aws_package_tree(monkeypatch, tmp_path, install)
    t = _SshTunnel("ds-1", "ds-abc", 1, 2, connect_timeout_secs=FALLBACK_TIMEOUT_SECS)

    async def _never_reachable() -> bool:
        return False

    # Only the child's exit ends the wait, whatever this host has listening.
    t._port_reachable = _never_reachable

    assert await t.start() is False
    assert refusal.path in t.status.error
    assert refusal.reason in t.status.error
    assert refusal.remedy_command in t.status.error
    assert "make it reachable from that PATH" not in t.status.error


_CLOUDFLARED_MISSING = "bash: cloudflared: command not found"


def _refused_plugin_beside(gui_gateway, monkeypatch, *, plant_cloudflared: bool) -> Path:
    """``gui_gateway`` with its plugin refused and a ProxyCommand whose ``cloudflared``
    is not found; ``cloudflared`` sits in the plugin's dir when *plant_cloudflared*."""
    _sysbin, install, _heads = gui_gateway
    if plant_cloudflared:
        _plant(install, "cloudflared")
    _refuse_the_plugin(monkeypatch)

    async def fake_exec(*_argv, **_kw):
        return _FakeSshProcess(_CLOUDFLARED_MISSING, 255)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return install


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
@pytest.mark.asyncio
@pytest.mark.parametrize("plant_cloudflared", [True, False])
async def test_mint_blames_a_refused_plugin_only_for_a_program_in_its_dir(
    gui_gateway, monkeypatch, plant_cloudflared
):
    """The refused plugin's dir is off PATH, so ``cloudflared`` beside it is not found."""
    install = _refused_plugin_beside(gui_gateway, monkeypatch, plant_cloudflared=plant_cloudflared)

    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=FALLBACK_TIMEOUT_SECS)

    msg = str(ei.value)
    blamed = f"the gateway leaves {install} off ssh's PATH" in msg
    assert blamed is plant_cloudflared
    assert ("make it reachable from that PATH" in msg) is not plant_cloudflared


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
@pytest.mark.asyncio
@pytest.mark.parametrize("plant_cloudflared", [True, False])
async def test_tunnel_blames_a_refused_plugin_only_for_a_program_in_its_dir(
    gui_gateway, monkeypatch, plant_cloudflared
):
    install = _refused_plugin_beside(gui_gateway, monkeypatch, plant_cloudflared=plant_cloudflared)
    t = _SshTunnel("ds-1", "ds-abc", 1, 2, connect_timeout_secs=FALLBACK_TIMEOUT_SECS)

    async def _never_reachable() -> bool:
        return False

    # Only the child's exit ends the wait, whatever this host has listening.
    t._port_reachable = _never_reachable  # type: ignore[method-assign]

    assert await t.start() is False
    blamed = f"the gateway leaves {install} off ssh's PATH" in t.status.error
    assert blamed is plant_cloudflared


def test_a_refused_plugin_dir_already_on_the_path_is_not_blamed():
    """The gateway's own PATH carries the dir, so the refusal hid nothing."""
    plugin_dir = token_mint.RefusedAws(
        "/opt/homebrew/bin", "plugin dir message", frozenset({"cloudflared"})
    )
    child_path = os.pathsep.join(["/usr/bin", "/opt/homebrew/bin"])

    cause = token_mint.proxy_tool_missing_cause(
        _CLOUDFLARED_MISSING, child_path, "refused plugin", refused_plugin_dir=plugin_dir
    )

    assert cause == token_mint.proxy_tool_missing_message(child_path)


@pytest.mark.skipif(os.name == "nt", reason=_POSIX_FALLBACK_DIRS)
def test_a_refused_plugin_in_a_later_dir_is_off_the_ssh_path(monkeypatch, tmp_path):
    """Each tool is validated in every appendable dir, not only up to its first pass.

    Two install dirs each hold one accepted tool next to one refused one:
    ``homebrew`` has an accepted ``session-manager-plugin`` but a refused ``aws``,
    ``usrlocal`` has an accepted ``aws`` but an other-owner ``session-manager-plugin``.
    Stopping each tool's walk at its first pass would validate only the homebrew
    plugin and the usrlocal aws, leaving the usrlocal plugin unchecked on the ssh
    child's PATH where a ProxyCommand would run it. The whole dir must be dropped.
    """
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    _plant(sysbin, "ssh")
    homebrew = tmp_path / "homebrew-bin"
    homebrew.mkdir()
    _plant(homebrew, "session-manager-plugin")
    hb_aws = _plant(homebrew, "aws")
    usrlocal = tmp_path / "usr-local-bin"
    usrlocal.mkdir()
    _plant(usrlocal, "aws")
    ul_plugin = _plant(usrlocal, "session-manager-plugin")
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(sysbin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(homebrew), str(usrlocal)))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)

    refused = {str(hb_aws), str(ul_plugin)}

    def _validate(candidate):
        if os.path.realpath(candidate) in {os.path.realpath(p) for p in refused}:
            raise ValueError(_REFUSAL)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)

    _argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc", "true"])

    # The unchecked usrlocal plugin must not be reachable by the ssh child: both
    # fallback dirs hold a refused tool, so neither may be appended.
    assert shutil.which("session-manager-plugin", path=env["PATH"]) is None
    parts = env["PATH"].split(os.pathsep)
    assert str(homebrew) not in parts
    assert str(usrlocal) not in parts


@pytest.mark.parametrize(
    ("stderr", "names_the_refusal"),
    [
        ("Error: session-manager-plugin is not installed", True),
        ("SessionManagerPlugin is not found", True),
        ("sh: 1: exec: connect-helper: not found", False),
    ],
)
def test_the_refusal_is_the_cause_only_when_stderr_names_the_plugin(stderr, names_the_refusal):
    """Another program the ProxyCommand could not find is reported with the PATH."""
    refused = "session-manager-plugin is installed at /p but was refused: r."

    cause = token_mint.proxy_tool_missing_cause(stderr, "/usr/bin:/bin", refused)

    path_cause = token_mint.proxy_tool_missing_message("/usr/bin:/bin")
    assert cause == (refused if names_the_refusal else path_cause)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
@pytest.mark.asyncio
async def test_diagnosis_names_the_refused_plugin_and_its_fix(refused_spawn, monkeypatch, tmp_path):
    """The ssh step keeps its code and names the plugin for a host whose ProxyCommand runs it."""
    _sysbin, install, _heads = refused_spawn
    refusal = _refused_in_the_aws_package_tree(monkeypatch, tmp_path, install)

    result = await diagnostics.diagnose_instance(
        "ds-abc", 7777, 0, connect_timeout_secs=FALLBACK_TIMEOUT_SECS
    )

    assert result.code == diagnostics.SSH_UNREACHABLE
    assert result.probes == [{"name": "ssh", "ok": False}]
    assert refusal.path in result.reason
    assert refusal.reason in result.reason
    assert refusal.remedy_command in result.reason
    assert "ProxyCommand" in result.reason


def _one_plugin_dir(monkeypatch, tmp_path) -> Path:
    """One fallback dir holding only ``session-manager-plugin``, nothing yet refused."""
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    _plant(install, "session-manager-plugin")
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)
    return install


class _SpyTrustLock:
    """The trust lock, noting whether sweep B found it already held."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.arrived = threading.Event()
        self.held_up = False

    def __enter__(self) -> None:
        if threading.current_thread().name == "sweep-b" and not self.arrived.is_set():
            self.held_up = self._lock.locked()
            self.arrived.set()
        self._lock.acquire()

    def __exit__(self, *_exc) -> None:
        self._lock.release()


def test_a_stale_accept_does_not_erase_a_newer_refusal(monkeypatch, tmp_path):
    """Sweep A accepts the plugin while sweep B, started later, sees it refused.

    A's accept reflects the plugin before it broke, so it must not be the last
    verdict: the refused plugin's dir would be back on the next ssh child's PATH.
    The sweep holds the trust lock, so B waits for A and its refusal stays.
    """
    install = _one_plugin_dir(monkeypatch, tmp_path)
    from kiro_crew import github_runner

    spy = _SpyTrustLock()
    monkeypatch.setattr(engine, "_AWS_BIN_DIR_TRUST_LOCK", spy)
    a_checking = threading.Event()
    a_may_accept = threading.Event()

    def _validate(candidate):
        if threading.current_thread().name == "sweep-b":
            raise ValueError(_REFUSAL)
        a_checking.set()
        assert a_may_accept.wait(FALLBACK_TIMEOUT_SECS)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)
    sweep_a = threading.Thread(target=engine.refresh_aws_tool_trust, name="sweep-a")
    sweep_b = threading.Thread(target=engine.refresh_aws_tool_trust, name="sweep-b")

    sweep_a.start()
    assert a_checking.wait(FALLBACK_TIMEOUT_SECS)
    sweep_b.start()
    assert spy.arrived.wait(FALLBACK_TIMEOUT_SECS)
    if not spy.held_up:
        # Without the lock B refuses first and A accepts last: the stale order.
        sweep_b.join(FALLBACK_TIMEOUT_SECS)
    a_may_accept.set()
    sweep_a.join(FALLBACK_TIMEOUT_SECS)
    sweep_b.join(FALLBACK_TIMEOUT_SECS)

    assert engine._REFUSED_AWS_BIN_DIRS == {("session-manager-plugin", str(install))}
    assert spy.held_up


def _refused_aws_beside(monkeypatch, tmp_path, *others: str) -> tuple[Path, token_mint.RefusedAws]:
    """A fallback dir whose ``aws`` the sweep refuses, with *others* planted beside it."""
    install = tmp_path / "opt-homebrew-bin"
    install.mkdir()
    for name in ("aws", *others):
        _plant(install, name)
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)

    def _refuse_aws(candidate):
        if os.path.splitext(os.path.basename(candidate))[0] == "aws":
            raise ValueError(_REFUSAL)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse_aws)
    engine.refresh_aws_tool_trust()
    refused_aws = token_mint.refused_aws_message()
    assert refused_aws.message
    return install, refused_aws


@pytest.mark.parametrize(
    "stderr",
    ["sh: 1: exec: cloudflared: not found", "bash: cloudflared: command not found"],
)
def test_a_refused_aws_is_the_cause_when_it_keeps_a_dir_off_the_path(monkeypatch, tmp_path, stderr):
    """A refused ``aws`` leaves its dir off PATH, so the program beside it is not found."""
    install, refused_aws = _refused_aws_beside(monkeypatch, tmp_path, "cloudflared")

    cause = token_mint.proxy_tool_missing_cause(stderr, "/usr/bin:/bin", "", refused_aws)

    assert cause == refused_aws.message
    assert _REFUSAL in cause
    assert str(install) in cause
    assert cloud_ssm.REFUSAL_NEXT_STEP["other_owner"] in cause


@pytest.mark.parametrize(
    "stderr",
    [_NOT_INSTALLED, "sh: 1: exec: cloudflared: not found"],
)
def test_a_refused_aws_is_not_blamed_for_a_program_not_in_its_dir(monkeypatch, tmp_path, stderr):
    """A missing plugin, or a program installed nowhere, gets the PATH message."""
    _install, refused_aws = _refused_aws_beside(monkeypatch, tmp_path)

    cause = token_mint.proxy_tool_missing_cause(stderr, "/usr/bin:/bin", "", refused_aws)

    assert cause == token_mint.proxy_tool_missing_message("/usr/bin:/bin")


def test_a_refused_aws_is_not_blamed_when_its_dir_is_already_on_the_path(monkeypatch, tmp_path):
    """The gateway's own PATH carries the dir, so the refusal hid nothing."""
    install, refused_aws = _refused_aws_beside(monkeypatch, tmp_path, "cloudflared")
    child_path = os.pathsep.join(["/usr/bin", str(install)])

    cause = token_mint.proxy_tool_missing_cause(
        "bash: cloudflared: command not found", child_path, "", refused_aws
    )

    assert cause == token_mint.proxy_tool_missing_message(child_path)


def test_a_repaired_then_rebroken_plugin_is_logged_again(monkeypatch, tmp_path, caplog):
    install = _one_plugin_dir(monkeypatch, tmp_path)
    from kiro_crew import github_runner

    state = {"broken": True}

    def _validate(candidate):
        if state["broken"]:
            raise ValueError(_REFUSAL)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)
    caplog.set_level("WARNING", logger=engine.logger.name)

    def _warnings() -> int:
        return sum("refusing session-manager-plugin" in r.getMessage() for r in caplog.records)

    engine.refresh_aws_tool_trust()
    engine.refresh_aws_tool_trust()
    assert _warnings() == 1
    state["broken"] = False
    engine.refresh_aws_tool_trust()
    assert engine._REFUSED_AWS_BIN_DIRS == set()
    state["broken"] = True
    engine.refresh_aws_tool_trust()

    assert _warnings() == 2
    assert engine._REFUSED_AWS_BIN_DIRS == {("session-manager-plugin", str(install))}


class _DiscardSpySet(set):
    """A refused-pairs set that records every pair taken out of it, by any method."""

    def __init__(self) -> None:
        super().__init__()
        self.taken_out: list[tuple[str, str]] = []

    def discard(self, pair) -> None:
        self.taken_out.append(pair)
        super().discard(pair)

    def remove(self, pair) -> None:
        self.taken_out.append(pair)
        super().remove(pair)

    def pop(self):
        pair = super().pop()
        self.taken_out.append(pair)
        return pair

    def clear(self) -> None:
        self.taken_out.extend(self)
        super().clear()

    def difference_update(self, *others) -> None:
        others = tuple(list(other) for other in others)
        for other in others:
            self.taken_out.extend(pair for pair in other if pair in self)
        super().difference_update(*others)

    def __isub__(self, other):  # type: ignore[misc]
        self.difference_update(other)
        return self


def test_a_new_refusal_reason_never_takes_the_pair_off_the_refused_set(monkeypatch, tmp_path):
    """``tool_spawn_env`` reads the refused set without the lock, so a still-refused
    pair must never leave it, even for a moment while its reason changes."""
    install = _one_plugin_dir(monkeypatch, tmp_path)
    refused = _DiscardSpySet()
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", refused)
    from kiro_crew import github_runner

    newest = "executable is group-writable"
    reasons = iter([_REFUSAL, "executable is world-writable", newest])

    def _validate(_candidate):
        raise ValueError(next(reasons))

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)

    for _ in range(3):
        engine.refresh_aws_tool_trust()

    pair = ("session-manager-plugin", str(install))
    assert pair not in refused.taken_out
    assert set(refused) == {pair}
    # The pair keeps only its newest warning, so the refusal read back is current.
    pair_warnings = {key for key in engine._WARNED_AWS_TOOL_REFUSALS if key[:2] == pair}
    assert [key[3] for key in pair_warnings] == [newest]
    refusal = engine.refused_aws_tool("session-manager-plugin")
    assert refusal is not None and refusal.reason == newest
