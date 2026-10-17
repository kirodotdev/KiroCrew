"""Tests for the Instances (multi-instance management) feature: SshTunnelManager.

Covers SshTunnelManager (mocked subprocess via an injected tunnel factory + mint):
its ssh argv, multiplexing pins and Windows refusal, plus the guard that every
manager-connect test pins the port probe. The rest of the feature's tests live in
sibling ``test_instances_*.py`` files.

Async paths are driven through ``asyncio.run`` from sync test functions so the
suite needs no asyncio pytest plugin.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe

# ── SshTunnelManager (mocked) ─────────────────────────────────────────────────


#: Test classes that drive ``SshTunnelManager.connect`` on the REAL port probe on
#: purpose, each with the reason. Everything else must pin the probe.
_REAL_PORT_PROBE_CLASSES = {
    "test_instances_orphan_reclaim.py::TestOrphanForwarderReclaim": "occupancy of a real port is the trigger",
}


def test_every_manager_connect_test_pins_the_port_probe():
    """A test that connects a manager must not depend on the host's free ports.

    ``connect()`` probes its allocated loopback port for real. A test that hands
    the manager a fixed ``base_port`` and leaves the probe real passes only where
    nothing else holds that port; on a shared macOS runner something does, and
    ``connect()`` quietly returns an error status instead of a tunnel.

    Two shapes are checked: a class that builds and connects a manager, and a
    module-level helper that builds one for other tests to connect. Either must
    pin the probe outside any single ``test_`` method -- ``_patch_port_probe``
    or a patch of ``_is_port_free``, written in place or reached through a
    module-level helper that does it -- or be named above with the reason the
    real probe is the subject.
    """
    import ast

    def _seg(src: str, node) -> str:
        return ast.get_source_segment(src, node) or ""

    def _pins(seg: str, helpers: set) -> bool:
        if "_patch_port_probe(" in seg or '"_is_port_free", lambda' in seg:
            return True
        return any(f"{name}(" in seg for name in helpers)

    def _pinned(src: str, node, helpers: set) -> bool:
        if not isinstance(node, ast.ClassDef):
            return _pins(_seg(src, node), helpers)
        return any(
            isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not m.name.startswith("test_")
            and _pins(_seg(src, m), helpers)
            for m in node.body
        )

    offenders = []
    for path in sorted(Path(__file__).parent.rglob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "SshTunnelManager(" not in src:
            continue
        top = [
            n
            for n in ast.parse(src).body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        # Module-level helpers that pin the probe themselves, e.g. a `_free_ports`.
        helpers = {
            n.name
            for n in top
            if not isinstance(n, ast.ClassDef)
            and not n.name.startswith("test_")
            and _pins(_seg(src, n), set())
        }
        for node in top:
            seg = _seg(src, node)
            if "SshTunnelManager(" not in seg:
                continue
            # A builder matters only where the same file connects what it builds.
            builder = (
                not isinstance(node, ast.ClassDef)
                and not node.name.startswith("test_")
                and ".connect(" in src
            )
            if ".connect(" not in seg and not builder:
                continue
            if _pinned(src, node, helpers):
                continue
            key = f"{path.name}::{node.name}"
            if key not in _REAL_PORT_PROBE_CLASSES:
                offenders.append(key)
    assert offenders == [], (
        "these build or connect a SshTunnelManager on the host's real port probe; "
        f"pin it with _patch_port_probe: {offenders}"
    )


class TestSshTunnelArgvCompression:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def test_compression_flag_present_by_default(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879)
        assert "-C" in argv
        # -C must sit before the -L forward / host (an ssh option, not a positional)
        assert argv.index("-C") < argv.index("-L")
        assert argv[0] == "ssh" and argv[-1] == "host-a"

    def test_compression_flag_omitted_when_disabled(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879, compression=False)
        assert "-C" not in argv
        # the rest of the shape is intact
        assert "BatchMode=yes" in argv and "AddressFamily=inet" in argv
        assert "127.0.0.1:7779:127.0.0.1:7879" in argv

    @pytest.mark.asyncio
    async def test_manager_threads_compression_to_tunnel(self, tmp_path):
        # The manager must thread ssh_compression to the tunnel factory on
        # connect() -- that flag is what _build_ssh_tunnel_argv uses to add/omit
        # -C. Drive a real connect and assert the captured value both ways.
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState

        captured: dict = {}

        def factory(*a, compression=True, **k):
            captured["compression"] = compression
            return _FakeTunnel(*a, compression=compression, **k)

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        # ssh_compression=False -> factory receives compression=False
        mgr_off = SshTunnelManager(
            reg, base_port=53400, ssh_compression=False, mint_token=ok_mint, tunnel_factory=factory
        )
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        assert (await mgr_off.connect("cd-1")).state == TunnelState.CONNECTED
        assert captured["compression"] is False

        # default (on) -> factory receives compression=True
        captured.clear()
        mgr_on = SshTunnelManager(reg, base_port=53500, mint_token=ok_mint, tunnel_factory=factory)
        reg.add(name="CD2", ssh_host="cd-2-alias", instance_id="cd-2")
        assert (await mgr_on.connect("cd-2")).state == TunnelState.CONNECTED
        assert captured["compression"] is True


class TestTunnelStatusCarriesTheDialIdentity:
    """The status surfaces the identity the tunnel was ACTUALLY dialled with.

    A credential path binds to this (not to the agent-writable registry row), so
    the fields have to name the node and account the open port reaches and must
    not change for the life of the tunnel.
    """

    def test_an_ssm_tunnel_surfaces_its_node_and_account(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        tun = _SshTunnel(
            "ssm-1",
            "",
            53410,
            7879,
            transport="ssm",
            ssm_target="mi-0a1b2c3d4e5f67890",
            aws_profile="prof",
            aws_region="us-east-1",
        )
        d = tun.status.to_dict()
        assert d["transport"] == "ssm"
        assert d["ssm_target"] == "mi-0a1b2c3d4e5f67890"
        assert d["aws_profile"] == "prof"
        assert d["aws_region"] == "us-east-1"

    def test_an_ssh_tunnel_carries_no_ssm_coordinates(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        # Even if an SSH forward is handed SSM-shaped values, the status reports
        # none, so a record requiring an SSM node can never match an SSH tunnel.
        tun = _SshTunnel(
            "ssh-1",
            "host.example",
            53411,
            7879,
            transport="ssh",
            ssm_target="mi-should-be-ignored",
            aws_profile="prof",
            aws_region="us-east-1",
        )
        d = tun.status.to_dict()
        assert d["transport"] == "ssh"
        assert d["ssm_target"] == ""
        assert d["aws_profile"] == ""
        assert d["aws_region"] == ""


class TestSshTunnelWindowsRefusal:
    """On native Windows an ssh-tunnel spawn failure names the platform.

    The surface is documented "not yet" supported on Windows, where the
    ``ssh -N -L`` child needs the OpenSSH client on PATH (not guaranteed). When
    that spawn fails, ``start()`` reports "SSH tunnel is not yet supported on
    Windows" instead of a generic spawn error indistinguishable from "ssh is
    missing", mirroring ``apps/lifecycle_scripts.py``. Crucially it does NOT
    refuse before the spawn: a Windows host whose ``ssh`` IS on PATH still spawns
    and connects, so a tunnel that works today is untouched. SSM/POSIX unchanged.
    """

    @pytest.mark.asyncio
    async def test_ssh_spawn_failure_on_windows_names_the_platform(self, monkeypatch):
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import (
            _WINDOWS_SSH_TUNNEL_UNSUPPORTED,
            TunnelState,
            _SshTunnel,
        )

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        # ssh is not on PATH on this Windows host, so the spawn raises OSError —
        # the exact generic-error case the issue reports.
        async def _ssh_missing(*a, **k):
            raise FileNotFoundError("[WinError 2] The system cannot find the file specified")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ssh_missing)

        tun = _SshTunnel("cd-1", "cd-1-alias", 53400, 7879, transport="ssh")
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        # Names the platform constraint, not just "failed to spawn ssh tunnel".
        assert tun.status.error.startswith(_WINDOWS_SSH_TUNNEL_UNSUPPORTED)
        assert "not yet supported on Windows" in tun.status.error
        assert not tun.status.error.startswith("failed to spawn")

    @pytest.mark.asyncio
    async def test_ssh_tunnel_is_not_refused_before_spawn_on_windows(self, monkeypatch):
        # The fix must NOT close a path that may work: a Windows host whose ssh
        # IS on PATH spawns successfully, so start() reaches the spawn rather than
        # refusing up front. (The spawn is stubbed to raise AFTER being entered,
        # so we only assert the spawn was reached, not the connect outcome.)
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        reached_spawn = False

        async def _spawn(*a, **k):
            nonlocal reached_spawn
            reached_spawn = True
            raise OSError("stop here, after entering the spawn")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

        tun = _SshTunnel("cd-1", "cd-1-alias", 53400, 7879, transport="ssh")
        await tun.start()

        assert reached_spawn is True, "start() must reach the spawn, not refuse before it"

    @pytest.mark.asyncio
    async def test_ssm_spawn_failure_on_windows_keeps_the_generic_message(self, monkeypatch):
        # The Windows wording is scoped to the ssh transport: an SSM spawn
        # failure on Windows keeps the generic spawn-error message.
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        async def _fail(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

        tun = _SshTunnel(
            "ssm-1", "", 53410, 7879, transport="ssm", ssm_target="i-0123456789abcdef0"
        )
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        assert tun.status.error.startswith("failed to spawn ssm tunnel")
        assert "Windows" not in tun.status.error

    @pytest.mark.asyncio
    async def test_ssh_spawn_failure_on_posix_keeps_the_generic_message(self, monkeypatch):
        # Control: on POSIX an ssh spawn failure keeps the generic message
        # (behaviour unchanged off Windows).
        from kiro_crew import platform_compat
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        async def _fail(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

        tun = _SshTunnel("cd-2", "cd-2-alias", 53420, 7879, transport="ssh")
        ok = await tun.start()

        assert ok is False
        assert tun.status.state == TunnelState.ERROR
        assert tun.status.error.startswith("failed to spawn ssh tunnel")
        assert "Windows" not in tun.status.error


requires_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh not available")

#: Hang guard for a ``ssh -G`` config resolution, NOT a performance budget. The call does
#: no network work, so any real duration is process-spawn overhead -- and on a loaded
#: 4-core Windows runner that starves: at 30s this timed out and failed the shard on a
#: config that was correct. Widening the guard cannot weaken an assertion (a genuine hang
#: still fails, a few seconds later); pinning it low turns runner load into a red build.
_SSH_CONFIG_PROBE_TIMEOUT_SECS = 120


def _ssh_effective_config(tmp_path, config_text: str, ssh_args: list[str], host: str) -> dict:
    """Return ssh's OWN resolved settings (``ssh -G``) for *ssh_args* under a config.

    Asks the real ssh binary how it would interpret the production command line,
    rather than asserting on option strings: the point at issue is precedence
    between the command line and ``~/.ssh/config``, which only ssh can answer.

    *ssh_args* is everything between the ``ssh`` binary and the host, flags
    included. Passing the whole thing rather than only the ``-o`` pairs matters:
    some settings resolve differently depending on flags like ``-N``.
    """
    cfg = tmp_path / "ssh_config"
    cfg.write_text(config_text.format(host=host, sock=str(tmp_path / "cm-%r@%h:%p")), "utf-8")
    # ``cwd=tmp_path``: the real binary is the point of this probe (only ssh can
    # answer its own precedence rules), so it stays real -- but it runs pinned to
    # the test's own directory rather than inheriting the worker's checkout cwd.
    out = subprocess.run(
        ["ssh", "-G", "-F", str(cfg), *ssh_args, host],
        capture_output=True,
        text=True,
        timeout=_SSH_CONFIG_PROBE_TIMEOUT_SECS,
        cwd=tmp_path,
    )
    assert out.returncode == 0, f"ssh -G failed: {out.stderr}"
    # Repeated keys are accumulated, not overwritten: ssh prints one
    # ``identityfile`` line per candidate, and how many appear varies by
    # release and by whether the config named one.
    resolved: dict[str, str] = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        key, value = key.strip().lower(), value.strip()
        resolved[key] = f"{resolved[key]}\n{value}" if key in resolved else value
    return resolved


def _ssh_args(argv: list[str]) -> list[str]:
    """Everything between the ``ssh`` binary and the trailing host."""
    return argv[1:-1]


class TestSshTunnelMultiplexing:
    """The supervised-child contract must survive a user's ssh_config.

    Multiplexing moves the local forward off the child the gateway supervises:
    ssh hands it to an existing shared connection and exits 0. That recreates
    the fork-and-exit shape ``-N`` without ``-f`` exists to avoid, so a tunnel
    that is genuinely serving reports ``ssh exited with code 0`` and is torn
    down.
    """

    _HOST = "kc-test-multiplex-host"

    #: A user config that enables multiplexing for the instance host.
    _ADVERSARIAL_CONFIG = (
        "Host {host}\n"
        "  HostName 127.0.0.1\n"
        "  User probeuser\n"
        "  ControlMaster auto\n"  # wokeignore:rule=master
        "  ControlPath {sock}\n"
        "  ControlPersist 10m\n"
    )

    def test_tunnel_argv_pins_multiplexing_off(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("host-a", 7779, 7879)
        assert "ControlPath=none" in argv
        assert "ControlMaster=no" in argv  # wokeignore:rule=master
        # Options, so they precede the -L forward and the positional host.
        assert argv.index("ControlPath=none") < argv.index("-L")
        assert argv.index("ControlMaster=no") < argv.index("-L")  # wokeignore:rule=master
        assert argv[-1] == "host-a"

    @requires_ssh
    def test_user_ssh_config_cannot_re_enable_multiplexing(self, tmp_path):
        """Ask the real ssh how it resolves the production argv, twice.

        The pinned run must end up sharing nothing. The unpinned run over the
        SAME config is the control: it shows ssh honouring the user's settings,
        so the assertions above test the pins rather than restating ssh's
        defaults.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        pinned = _ssh_effective_config(tmp_path, self._ADVERSARIAL_CONFIG, args, self._HOST)
        assert pinned.get("controlpath") in (None, "none")
        assert pinned.get("controlmaster") in ("no", "false")  # wokeignore:rule=master

        bare = ["-N", "-L", "127.0.0.1:7779:127.0.0.1:7879"]
        unpinned = _ssh_effective_config(tmp_path, self._ADVERSARIAL_CONFIG, bare, self._HOST)
        assert unpinned.get("controlpath") not in (None, "none")
        assert unpinned.get("controlmaster") == "auto"  # wokeignore:rule=master

    @requires_ssh
    def test_pins_do_not_override_a_user_ignoreunknown(self, tmp_path):
        """A pinned `-o` must not displace a directive the user also sets.

        ssh takes the FIRST value obtained for a directive and reads the command
        line before ``~/.ssh/config``, so pinning a single-valued directive here
        silently discards the user's own. ``IgnoreUnknown`` is the one that
        bites: it is how a cross-platform config carries an option this ssh does
        not recognise, and losing it turns a working config into ``Bad
        configuration option`` -- every tunnel then fails where it would
        connect. Multiplexing is safe to pin because a supervised tunnel must
        never share a connection; that reasoning does not generalise.

        The keyword is invented so no OpenSSH release knows it, which keeps the
        result independent of platform and version.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        cfg = tmp_path / "ssh_config"
        cfg.write_text(
            "IgnoreUnknown UserPrivateOption\n"
            "UserPrivateOption yes\n"
            "\n"
            f"Host {self._HOST}\n"
            "  HostName 127.0.0.1\n",
            "utf-8",
        )
        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        out = subprocess.run(
            ["ssh", "-G", "-F", str(cfg), *args, self._HOST],
            capture_output=True,
            text=True,
            timeout=_SSH_CONFIG_PROBE_TIMEOUT_SECS,
            cwd=tmp_path,
        )
        assert out.returncode == 0, f"production argv broke a working config: {out.stderr}"
        assert "bad configuration option" not in out.stderr.lower()

    @requires_ssh
    def test_per_host_ssh_config_is_still_inherited(self, tmp_path):
        """Only process ownership is overridden; connection coordinates are not.

        The registry carries no inline `-i`/`-p`/`-J` fields and relies on the
        ssh-config alias path for identity, port, and bastion reachability, so
        pinning must not turn the argv into a general ssh_config override.
        """
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        config = (
            self._ADVERSARIAL_CONFIG
            + "  Port 2222\n"
            + "  IdentityFile ~/.ssh/some-key.pem\n"
            + "  ProxyCommand /bin/true %h %p\n"
        )
        args = _ssh_args(_build_ssh_tunnel_argv(self._HOST, 7779, 7879))
        resolved = _ssh_effective_config(tmp_path, config, args, self._HOST)

        assert resolved.get("hostname") == "127.0.0.1"
        assert resolved.get("user") == "probeuser"
        assert resolved.get("port") == "2222"
        assert "some-key.pem" in resolved.get("identityfile", "")
        assert resolved.get("proxycommand", "").startswith("/bin/true")
        # The argv's own pins are still in force alongside the inherited values.
        assert resolved.get("batchmode") == "yes"
        assert resolved.get("exitonforwardfailure") == "yes"
        assert resolved.get("addressfamily") == "inet"


class TestSshTunnelManager:
    @pytest.fixture(autouse=True)
    def _free_ports(self, monkeypatch):
        # Connect now probes _is_port_free (CSE SEC-016 mirror conflict check).
        # Keep these unit tests hermetic / independent of the host's real ports.
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_connect_persists_and_idempotent(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        st = await mgr.connect("cd-1")
        assert st.state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        inst = reg.get("cd-1")
        # local_port is ALLOCATED from the tunnel base, not mirrored onto
        # remote_port.
        assert inst.was_connected is True
        assert inst.local_port >= mgr._allocator.base_port
        assert inst.local_port != inst.remote_port
        assert reg.get_last_active().id == "cd-1"
        # idempotent
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED

    @pytest.mark.asyncio
    async def test_reconnect_prefers_recorded_port_for_origin_stability(self, tmp_path):
        """A crew keeps its recorded loopback port across a gateway restart.

        The pane is an iframe served at ``http://<host>:<local_port>``, so the
        loopback port IS its browser origin, and origin-keyed client state —
        ``localStorage`` UI preferences (mc-chat-config, pinLastPrompt,
        hideEmptyFolderBody, ...) — is reset whenever that origin moves. The
        recorded ``local_port`` survives a restart (``shutdown`` keeps registry
        hints), so connect() must PREFER it: a first-free allocation lets a crew
        land on a DIFFERENT port after the restart whenever another instance
        holds the lower port, moving the origin and the settings behind it. The
        preference keeps both put.
        """
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="a-alias", instance_id="a")
        reg.add(name="B", ssh_host="b-alias", instance_id="b")

        # First boot: A takes base, B takes base+1 (B's connect excludes A's
        # recorded port via the registry).
        base = mgr._allocator.base_port
        assert (await mgr.connect("a")).local_port == base
        assert (await mgr.connect("b")).local_port == base + 1

        # Restart: a fresh manager over the SAME registry, which still holds both
        # recorded ports. Reconnect B FIRST — the order that, under first-free,
        # would hand B the now-vacant lower port (base) and move its origin.
        mgr2 = SshTunnelManager(
            reg, base_port=base, mint_token=mgr._mint_token, tunnel_factory=_FakeTunnel
        )
        st_b = await mgr2.connect("b")
        assert st_b.state == TunnelState.CONNECTED
        assert st_b.local_port == base + 1, "B must keep its recorded port, not slide to base"
        assert reg.get("b").local_port == base + 1
        # A then reconnects onto its own recorded port too — no collision.
        st_a = await mgr2.connect("a")
        assert st_a.local_port == base
        assert reg.get("a").local_port == base

    @pytest.mark.asyncio
    async def test_connect_rebuild_replaces_a_connected_tunnel(self, tmp_path):
        """``rebuild=True`` is the pane's Retry after a watchdog verdict on a
        document that DID navigate: every probe says the tunnel is fine, so the
        idempotent connect would hand the same (stalled) forwarder back. The
        rebuild must stop the old child, spawn a new one, keep the user's
        connect intent, and answer CONNECTED with a live token."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        first = await mgr.connect("cd-1")
        assert first.state == TunnelState.CONNECTED
        old_tunnel = mgr._tunnels["cd-1"]

        second = await mgr.connect("cd-1", rebuild=True)
        assert second.state == TunnelState.CONNECTED
        assert mgr._tunnels["cd-1"] is not old_tunnel, "a rebuild must spawn a new forwarder"
        assert old_tunnel.stopped, "the stalled forwarder must be stopped, not orphaned"
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        inst = reg.get("cd-1")
        assert inst.was_connected is True, "rebuild keeps the connect intent (keep_intent)"
        assert inst.local_port == second.local_port
        assert inst.local_port >= mgr._allocator.base_port
        # The freed port is excluded from the re-allocation: a cause bound to the
        # old port (not just a stalled stream on the old forwarder) is escaped too.
        assert second.local_port != first.local_port, "rebuild must not re-use the port it freed"
        # A plain connect afterwards is idempotent on the NEW tunnel.
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        assert mgr._tunnels["cd-1"] is not old_tunnel

    @pytest.mark.asyncio
    async def test_connect_rebuild_teardown_failure_is_an_error_status(self, tmp_path):
        """A stop that raises during rebuild must NOT propagate out of connect()
        (the handler would turn it into an unexplained 500). It is reported like
        every other connect failure: ERROR status with the reason, retained for
        last_error, and the old tunnel is left tracked and intact — nothing is
        removed unless the stop succeeded."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        first = await mgr.connect("cd-1")
        assert first.state == TunnelState.CONNECTED
        old_tunnel = mgr._tunnels["cd-1"]

        async def boom():
            raise RuntimeError("kill failed: EPERM")

        old_tunnel.stop = boom

        st = await mgr.connect("cd-1", rebuild=True)
        assert st.state == TunnelState.ERROR
        assert "EPERM" in (st.error or "")
        assert "EPERM" in (mgr.last_error("cd-1") or "")
        # The live tunnel is untouched: still tracked, credential still held.
        assert mgr._tunnels["cd-1"] is old_tunnel
        assert mgr.get_token("cd-1") == "SECRET_TOK"
        assert reg.get("cd-1").was_connected is True

    @pytest.mark.asyncio
    async def test_connect_only_if_connected_declines_without_side_effects(self, tmp_path):
        """The viewport's auto-warm must never bring a tunnel up or touch the
        connect intent: for an absent tunnel it answers DISCONNECTED, spawns
        nothing, mints nothing, and leaves was_connected exactly as the user
        last set it (here: cleared by an explicit disconnect)."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        # Never connected.
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.DISCONNECTED
        assert "cd-1" not in mgr._tunnels
        assert not mgr.get_token("cd-1")
        assert reg.get("cd-1").was_connected is False

        # Connected, then explicitly disconnected: the race auto-warm loses.
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        await mgr.disconnect("cd-1")
        assert reg.get("cd-1").was_connected is False
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.DISCONNECTED
        assert (
            "cd-1" not in mgr._tunnels
        ), "a connected-only connect must not re-open a closed tunnel"
        assert (
            reg.get("cd-1").was_connected is False
        ), "a connected-only connect must not re-persist intent"

    @pytest.mark.asyncio
    async def test_connect_only_if_connected_answers_a_live_tunnel_like_plain_connect(
        self, tmp_path
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        first = await mgr.connect("cd-1")
        tunnel = mgr._tunnels["cd-1"]
        st = await mgr.connect("cd-1", only_if_connected=True)
        assert st.state == TunnelState.CONNECTED
        assert st.local_port == first.local_port
        assert mgr._tunnels["cd-1"] is tunnel, "idempotent on the live tunnel"
        assert mgr.get_token("cd-1") == "SECRET_TOK"

    @pytest.mark.asyncio
    async def test_connect_rebuild_and_only_if_connected_are_exclusive(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        with pytest.raises(ValueError):
            await mgr.connect("cd-1", rebuild=True, only_if_connected=True)

    @pytest.mark.asyncio
    async def test_connect_rebuild_with_no_tunnel_is_a_plain_connect(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        st = await mgr.connect("cd-1", rebuild=True)
        assert st.state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") == "SECRET_TOK"

    @pytest.mark.asyncio
    async def test_connect_resets_recover_attempts(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # Simulate a prior give-up that left the counter past the cap.
        mgr._recover_attempts["cd-1"] = 99
        await mgr.connect("cd-1")
        # A successful (re)connect clears the stale give-up counter so the next
        # unexpected drop gets a full fresh recovery budget.
        assert "cd-1" not in mgr._recover_attempts

    def test_unknown_instance_raises(self, tmp_path):
        _reg, mgr = self._mgr(tmp_path)
        with pytest.raises(KeyError):
            asyncio.run(mgr.connect("ghost"))

    def test_validation_failure(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")  # registry ok; manager rejects
        st = asyncio.run(mgr.connect("bad"))
        assert st.state == TunnelState.ERROR and "invalid ssh settings" in st.error
        assert mgr.status("bad") is None and mgr.get_token("bad") == ""

    def test_tunnel_failure(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        def failing(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        st = asyncio.run(mgr.connect("cd-1"))
        assert st.state == TunnelState.ERROR and mgr.get_token("cd-1") == ""

    def test_mint_failure_tears_down(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            raise TokenMintError("nope")

        reg, mgr = self._mgr(tmp_path, mint=bad_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        st = asyncio.run(mgr.connect("cd-1"))
        assert st.state == TunnelState.ERROR and "token mint failed" in st.error
        assert mgr.status("cd-1") is None

    @pytest.mark.asyncio
    async def test_disconnect_and_shutdown(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert await mgr.disconnect("cd-1") is True
        assert reg.get("cd-1").was_connected is False
        assert mgr.get_token("cd-1") == ""
        # shutdown preserves registry hints for lazy reconnect
        await mgr.connect("cd-1")
        await mgr.shutdown()
        assert mgr.status_all() == {}
        assert reg.get("cd-1").was_connected is True

    @pytest.mark.asyncio
    async def test_disconnect_clears_local_port(self, tmp_path):
        # Regression: connect() records the allocated local_port, but
        # disconnect() must reset it to the unallocated sentinel. Otherwise the
        # freed port stays recorded and reads as reserved forever.
        from kiro_crew.instances.registry import _UNALLOCATED_PORT

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")

        await mgr.connect("cd-1")
        allocated = reg.get("cd-1").local_port
        assert allocated >= mgr._allocator.base_port

        await mgr.disconnect("cd-1")
        inst = reg.get("cd-1")
        assert inst.local_port == _UNALLOCATED_PORT  # port hint cleared
        assert inst.was_connected is False
        # the cleared port is not counted as reserved
        assert allocated not in mgr._reserved_ports()

    @pytest.mark.asyncio
    async def test_disconnect_clears_stale_port_without_live_tunnel(self, tmp_path):
        # A port left recorded by an unclean prior exit (no live tunnel tracked)
        # must still be clearable via disconnect, so the user can recover.
        from kiro_crew.instances.registry import _UNALLOCATED_PORT

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.update("cd-1", local_port=7777, was_connected=True)  # simulate stale hint

        assert await mgr.disconnect("cd-1") is False  # no live tunnel existed
        inst = reg.get("cd-1")
        assert inst.local_port == _UNALLOCATED_PORT and inst.was_connected is False

    @pytest.mark.asyncio
    async def test_token_validates_status_mapping(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        class _Resp:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            status = 200

            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, params=None):
                return _Resp(_Sess.status)

        _reg, mgr = self._mgr(tmp_path)
        monkeypatch.setattr(m.aiohttp, "ClientSession", _Sess)
        _Sess.status = 200
        assert await mgr.token_validates(7778, "TOK") is True  # 2xx => valid
        _Sess.status = 403
        assert await mgr.token_validates(7778, "TOK") is False  # remote rejected => stale
        _Sess.status = 500
        assert await mgr.token_validates(7778, "TOK") is False  # non-2xx => not confirmed
        # missing token / unknown port => re-mint needed (no probe)
        assert await mgr.token_validates(7778, "") is False
        assert await mgr.token_validates(0, "TOK") is False

    @pytest.mark.asyncio
    async def test_token_validates_denies_on_error(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as m

        class _BoomSess:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, *a, **k):
                raise asyncio.TimeoutError()

        _reg, mgr = self._mgr(tmp_path)
        monkeypatch.setattr(m.aiohttp, "ClientSession", _BoomSess)
        # Probe inconclusive (timeout) => deny-by-default (force a re-mint).
        assert await mgr.token_validates(7778, "TOK") is False
