"""Reclaiming a hard-kill-orphaned forwarder: pid hint plus exact-argv guard."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest
from instances_helpers import _FakeTunnel

from kiro_crew.testing.wait import async_wait_until


def _route_pinned_kill_to_kill_pid(monkeypatch):
    """Send the Windows pinned kill through the test's ``kill_pid`` double.

    The real ``kill_pid_pinned`` opens a handle to the (fake) pid on Windows and
    refuses, so the signal-sequence tests below would see no signal there.
    """
    from kiro_crew import platform_compat as pc

    monkeypatch.setattr(pc, "kill_pid_pinned", lambda pid, start, sig: pc.kill_pid(pid, sig))


class TestOrphanForwarderReclaim:
    """End-to-end reclaim behavior against REAL processes holding REAL ports.

    The reclaim path (``_reclaim_orphan_forwarder``) is keyed on the recorded
    pid behind a strict exact-argv identity check. These tests spawn a real
    child bound to a real loopback port and drive a real ``connect()``:

    * the leaked-forwarder case proves the child is terminated and its port
      released (identity confirmed via the real /proc//ps argv read);
    * the reclaim-guard cases prove a process this manager did not spawn is
      NEVER signalled — whether its pid is recorded (pid recycled), unrecorded,
      or its port is not even occupied.

    ``_is_port_free`` stays REAL here (unlike the fake-port manager tests):
    occupancy of the holder's port is the trigger under test.
    """

    @staticmethod
    def _os_assigned_base_port():
        """A base port the OS just handed out, so no two cases share one.

        These tests keep the probe REAL, and the probe is a bind. A fixed base
        port shared by the parametrized cases of one test lets xdist workers
        probe the same port at the same moment; on Windows the exclusive bind
        makes one of them see the port as taken and ``connect()`` fail.
        """
        # flake-ok: bind-close OS port only seeds the allocator probe scan, which skips a taken port; moved verbatim
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _mgr(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        base_port = self._os_assigned_base_port()
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
            reg, base_port=base_port, mint_token=ok_mint, tunnel_factory=_FakeTunnel
        )

    def _spawn_port_holder(self):
        """Spawn a real child LISTENing on a free loopback port.

        Returns ``(proc, port, argv)``. Readiness is signalled over stdout so
        the bind (and /proc argv population) cannot be raced. A daemon reaper
        thread ``wait()``s the child so that, once signalled, it disappears
        immediately instead of lingering as a zombie — mirroring production,
        where the leaked forwarder's parent is dead and init reaps it.

        The child's parent is the TEST process, i.e. a LIVE parent: the
        reclaim's orphan gate refuses it as-is (which the live-parent test
        pins with the real ``get_ppid``). Tests that exercise the gates
        BEHIND the orphan gate monkeypatch ``get_ppid`` to 1 for their own
        scope — a real double-fork orphan would leave an unsupervised process
        behind on a test crash and is exactly the residue the suite forbids.
        """
        code = (
            "import socket, sys, time\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "sys.stdout.write('%d\\n' % s.getsockname()[1])\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            # flake-ok: pre-existing read of the child's ready line returns on its port print or its EOF; moved verbatim
            line = proc.stdout.readline()
            port = int(line.strip())
        except Exception:
            proc.kill()
            raise
        # flake-ok: daemon reaper's untimed wait never blocks the test, and the finally cleanup kills a live child; moved verbatim
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc, port, argv

    @staticmethod
    def _fake_orphan(monkeypatch):
        """Make the orphan gate see every pid as init-reparented (test scope)."""
        from kiro_crew import platform_compat as pc

        monkeypatch.setattr(pc, "get_ppid", lambda pid: 1)

    _TEST_IDENTITY_KEY = b"k" * 32

    @classmethod
    def _pin_identity_key(cls, monkeypatch):
        """Pin the reclaim signing key and return a sig factory.

        The real key derives from the SEL trust root, which a test home does
        not initialize; pinning a fixed key keeps the MAC math (and its
        compare_digest gate) fully real while making signatures computable by
        the test exactly the way the pre-kill gateway would have.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: cls._TEST_IDENTITY_KEY)

        def sign(instance_id, pid, start, port, argv_sig="", transport="ssh"):
            return stm._forwarder_identity_sig(
                cls._TEST_IDENTITY_KEY, instance_id, pid, start, port, argv_sig, transport
            )

        return sign

    @staticmethod
    def _cleanup(proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="positive reclaim needs the python stand-in's kernel argv to equal "
        "the spawn argv; macOS framework python re-execs (rewriting argv[0]) and "
        "Windows fails closed by design — production targets (ssh/aws binaries) "
        "do not re-exec",
    )
    @pytest.mark.asyncio
    async def test_hard_kill_leaked_forwarder_is_reclaimed_by_pid(self, tmp_path, monkeypatch):
        """hard-kill -> restart -> reconnect: the recorded child is terminated
        and its port released; connect then succeeds."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.port_allocator import _is_port_free
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            # The identity check compares the recorded pid's REAL argv against
            # the command line the manager would construct. The manager builds
            # ssh argv; the leaked stand-in is a python child — point the
            # builder at the stand-in's exact argv so the real /proc read and
            # the element-wise comparison are exercised end to end. The orphan
            # gate is faked open (the stand-in's parent is this test).
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid)
            assert start, "test needs a readable start-time identity"
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            # What the pre-kill gateway persisted: port + signed child identity.
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            # (a) the old forwarder process is not alive…
            assert proc.wait(timeout=10) is not None
            # …and its port is released.
            await async_wait_until(
                lambda: _is_port_free(port),
                off_loop=True,
                describe=lambda: f"port {port} is still bound",
            )
            assert _is_port_free(port), "reclaimed forwarder's port was not released"
            # The connect succeeds regardless of which loopback port it lands on.
            # It asserts only that a valid port was recorded, not a DIFFERENT one:
            # the recorded port is PREFERRED for origin stability, so reclaiming
            # this crew's own orphaned forwarder and then reconnecting onto the
            # very port it freed is a valid outcome — and whether the freed port
            # is already probe-free at allocation time races the OS reap, so
            # neither "same" nor "different" is a stable assertion.
            inst = reg.get("cd-1")
            assert inst.local_port >= mgr._allocator.base_port
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_reclaim_compares_the_resolved_ssh_head_the_tunnel_spawned(
        self, tmp_path, monkeypatch
    ):
        """The ssh tunnel spawns an ABSOLUTE head, so the reclaim must compare one.

        The builder returns the bare ``"ssh"`` and ``ssh_spawn_argv_env``
        resolves it at spawn, so the kernel records the resolved path. A reclaim
        that rebuilt the bare head compared ``ssh`` with ``/usr/bin/ssh``
        element-exactly and leaked every genuine orphan. Runs on every platform:
        the port is held by a real listener, every gate before the identity
        check is real, and the argv handed to the identity check is compared
        with the one ``_SshTunnel`` spawns for the same instance.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew.instances import token_mint

        head = str(tmp_path / "bin" / "ssh")
        monkeypatch.setattr(token_mint, "resolve_ssh_bin", lambda: head)
        self._fake_orphan(monkeypatch)
        sign = self._pin_identity_key(monkeypatch)
        seen: dict = {}

        def capture(pid, start, expected_argv, port, tree, audit, expected_argv_sig=""):
            seen["argv"] = list(expected_argv)
            return "identity_mismatch"

        monkeypatch.setattr(stm, "_verify_and_reclaim_forwarder", capture)
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            pid, start = 424242, "12345"
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", pid, start, port),
                was_connected=True,
            )
            await mgr.connect("cd-1")
        finally:
            holder.close()

        spawned = stm._SshTunnel(
            "cd-1", "cd-1-alias", port, reg.get("cd-1").remote_port
        )._build_argv()
        assert seen["argv"][0] == head
        assert seen["argv"] == spawned

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="positive reclaim needs the python stand-in's kernel argv to equal "
        "the spawn argv; macOS framework python re-execs and Windows fails closed",
    )
    @pytest.mark.asyncio
    async def test_sigterm_ignoring_forwarder_is_sigkill_escalated(self, tmp_path, monkeypatch):
        """A verified leaked forwarder that ignores SIGTERM is SIGKILLed after
        the grace (identity re-confirmed before the destructive escalation)."""
        import signal as signal_mod

        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        code = (
            "import signal, socket, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "sys.stdout.write('%d\\n' % s.getsockname()[1])\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            # flake-ok: pre-existing read of the child's ready line returns on its port print or its EOF; moved verbatim
            port = int(proc.stdout.readline().strip())
            # flake-ok: daemon reaper's untimed wait never blocks the test, and the finally cleanup kills a live child; moved verbatim
            threading.Thread(target=proc.wait, daemon=True).start()
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            # Keep the TERM grace short so the escalation happens quickly.
            monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.3)
            start = pc.process_start_time(proc.pid)
            assert start
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            rc = proc.wait(timeout=10)
            assert rc is not None
            # SIGTERM was ignored by the child, so death proves the SIGKILL
            # escalation ran (exit by signal reports negative on POSIX).
            assert rc == -signal_mod.SIGKILL
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_unsigned_or_forged_record_is_never_signalled(self, tmp_path, monkeypatch):
        """Forged-registry regression (agent-writable instances.json): a record
        with fully matching process attributes but WITHOUT the gateway's MAC —
        missing or wrong — authorizes nothing, even with the orphan gate open
        and the argv matching. Only the gateway can mint the signature (its
        key derives from the deny-listed SEL trust root)."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            # "" = unsigned; hex garbage = wrong MAC; non-ASCII and a lone
            # surrogate = the malformed-text shapes that must read as
            # verification failure, never crash the connect path.
            for forged_sig in ("", "deadbeef" * 8, "签名不对", "\udc80bad"):
                reg, mgr = self._mgr(tmp_path)
                reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
                reg.update(
                    "cd-1",
                    local_port=port,
                    forwarder_pid=proc.pid,
                    forwarder_start=start,
                    forwarder_sig=forged_sig,
                    was_connected=True,
                )
                st = await mgr.connect("cd-1")
                assert st.state == TunnelState.CONNECTED
                assert proc.poll() is None, f"a record with sig={forged_sig!r} was honored"
                reg.remove("cd-1")
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_live_parent_forwarder_is_never_signalled(self, tmp_path, monkeypatch):
        """Forged-registry regression: the registry is agent-writable, so a
        record can truthfully describe a LIVE sibling gateway's forwarder
        (real pid, real start time, matching argv). The orphan gate must
        refuse it: a process whose parent is alive belongs to a running
        gateway and is never a hard-kill leak."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # Direct child of the test process == a live parent (this process).
        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a live-parented forwarder was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(os.name == "nt", reason="ppid probe of a posix-spawned stand-in")
    @pytest.mark.asyncio
    async def test_recorded_start_mismatch_is_never_signalled(self, tmp_path, monkeypatch):
        """Pid-recycling regression: the recorded pid now belongs to a process
        whose argv happens to match, but its start-time identity does not.
        Nothing may be signalled — the start-time pin is what defeats a
        recycled pid running an identical command line."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start="not-the-recorded-identity",
                forwarder_sig=sign("cd-1", proc.pid, "not-the-recorded-identity", port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a start-time-mismatched pid was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_recorded_pid_with_foreign_argv_is_never_signalled(self, tmp_path, monkeypatch):
        """The recorded pid is recycled onto a process this
        manager did not spawn (its argv is not the forward command line). It
        must be left alone even with a matching start time and an open orphan
        gate."""
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, _argv = self._spawn_port_holder()
        try:
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            # Even with a CORRECT start-time identity the argv gate must still
            # refuse: the real argv builder expects `ssh …`, not python.
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")  # real argv builder: expects ssh …

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a foreign process holding the port was signalled"
            inst = reg.get("cd-1")
            assert inst.local_port != port  # allocated around the occupied port
        finally:
            self._cleanup(proc)

    def _spawn_shebang_port_holder(self, tmp_path):
        """Like :meth:`_spawn_port_holder`, but run through a ``#!`` script.

        The kernel rewrites a shebang child's argv to ``<interpreter> <script>``,
        the same thing it does to an ``aws`` CLI v1 entrypoint. So the command
        line anyone would rebuild (just the script) never equals what the
        kernel reports for the running child.
        """
        script = tmp_path / "fake-forwarder"
        script.write_text(
            f"#!{sys.executable}\n"
            "import socket, sys, time\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "sys.stdout.write('%d\\n' % s.getsockname()[1])\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        script.chmod(0o700)
        proc = subprocess.Popen(
            [str(script)],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            # flake-ok: pre-existing read of the child's ready line returns on its port print or its EOF; moved verbatim
            port = int(proc.stdout.readline().strip())
        except Exception:
            proc.kill()
            raise
        # flake-ok: daemon reaper's untimed wait never blocks the test, and the finally cleanup kills a live child; moved verbatim
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc, port, [str(script)]

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows records no argv fingerprint; a shebang child is POSIX-only",
    )
    @pytest.mark.parametrize("flip_compression", [False, True])
    @pytest.mark.asyncio
    async def test_recorded_argv_fingerprint_survives_command_line_drift(
        self, tmp_path, monkeypatch, flip_compression
    ):
        """A forwarder whose rebuilt command line differs from its live argv
        is still reclaimed when its spawn-time fingerprint matches.

        The child runs through a shebang, so its kernel argv carries the
        interpreter the rebuilt argv lacks; with ``flip_compression`` the
        compression setting changes between spawn and reclaim as well. The
        rebuild is pointed at the script alone (what a v1 ``aws`` rebuild looks
        like), so only the recorded fingerprint can confirm the identity.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.port_allocator import _is_port_free
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, rebuilt = self._spawn_shebang_port_holder(tmp_path)
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(rebuilt),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid)
            argv_sig = pc.process_argv_fingerprint(proc.pid)
            assert start and argv_sig, "test needs a readable spawn-time identity"
            # The drift this test is about: the rebuild is not the live argv.
            assert not pc.process_argv_matches_exact(proc.pid, rebuilt)
            reg, mgr = self._mgr(tmp_path)
            if flip_compression:
                mgr._ssh_compression = not mgr._ssh_compression
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_argv_sig=argv_sig,
                forwarder_sig=sign("cd-1", proc.pid, start, port, argv_sig),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.wait(timeout=10) is not None, "the drifted orphan was not reclaimed"
            # "reclaimed" is only reported once the port let go.
            # flake-ok: real two-loopback probe of the reaped child's released port; moved verbatim, tracked for a de-flake pass
            assert _is_port_free(port)
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(os.name == "nt", reason="ppid probe of a posix-spawned stand-in")
    @pytest.mark.asyncio
    async def test_recorded_argv_fingerprint_mismatch_is_never_signalled(
        self, tmp_path, monkeypatch
    ):
        """A signed record whose fingerprint is not the live process's argv is
        left alone, even when the rebuilt command line would have matched."""
        import hashlib

        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            # The rebuild matches, so only the fingerprint can refuse.
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            other = hashlib.sha256(b"ssh\0-N\0some-other-forward").hexdigest()
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_argv_sig=other,
                forwarder_sig=sign("cd-1", proc.pid, start, port, other),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a process with a different argv was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(os.name == "nt", reason="ppid probe of a posix-spawned stand-in")
    @pytest.mark.asyncio
    async def test_dropping_the_signed_fingerprint_does_not_fall_back(self, tmp_path, monkeypatch):
        """Erasing ``forwarder_argv_sig`` from a record signed with it fails the
        MAC, so the record cannot be pushed back onto the rebuilt-argv check."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        seen: list = []
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            monkeypatch.setattr(
                stm, "_verify_and_reclaim_forwarder", lambda *a, **k: seen.append(a) or "x"
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_argv_sig="",  # erased after the gateway signed it
                forwarder_sig=sign("cd-1", proc.pid, start, port, "ab" * 32),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert seen == [], "a record with a broken signature reached the identity check"
            assert proc.poll() is None
        finally:
            self._cleanup(proc)

    def test_forwarder_sig_message_keeps_the_older_four_field_form(self):
        """An empty fingerprint signs exactly what older gateways signed, so
        their records still verify; a fingerprint changes the signature."""
        import hashlib
        import hmac

        import kiro_crew.instances.ssh_tunnel_manager as stm

        key = b"k" * 32
        old = hmac.new(key, b"cd-1\x004242\x00S1\x0054300", hashlib.sha256).hexdigest()
        assert stm._forwarder_identity_sig(key, "cd-1", 4242, "S1", 54300) == old
        assert stm._forwarder_identity_sig(key, "cd-1", 4242, "S1", 54300, "") == old
        assert stm._forwarder_identity_sig(key, "cd-1", 4242, "S1", 54300, "ab" * 32) != old
        # The spawn transport is signed with the fingerprint.
        assert stm._forwarder_identity_sig(
            key, "cd-1", 4242, "S1", 54300, "ab" * 32, "ssh"
        ) != stm._forwarder_identity_sig(key, "cd-1", 4242, "S1", 54300, "ab" * 32, "ssm")

    @pytest.mark.skipif(os.name == "nt", reason="ppid probe of a posix-spawned stand-in")
    @pytest.mark.asyncio
    async def test_switched_transport_is_refused_before_any_signal(self, tmp_path, monkeypatch):
        """A record signed for an ssh child is refused once the instance's
        settings say SSM, so the SSM group signal never reaches a child that
        shares the dead gateway's process group."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        proc, port, _argv = self._spawn_port_holder()
        seen: list = []
        try:
            monkeypatch.setattr(
                stm, "_verify_and_reclaim_forwarder", lambda *a, **k: seen.append(a) or "x"
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            argv_sig = "ab" * 32
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            params = stm._TransportParams(method="ssm", ssm_target="i-0123456789abcdef0")
            assert params.forwards_over_ssm
            reg.update(
                "cd-1",
                local_port=port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_argv_sig=argv_sig,
                forwarder_sig=sign("cd-1", proc.pid, start, port, argv_sig, "ssh"),
            )

            await mgr._reclaim_orphan_forwarder(reg.get("cd-1"), params)
            assert seen == [], "a transport-switched record reached the signal path"

            # Control: the same record signed for SSM gets past the MAC.
            reg.update("cd-1", forwarder_sig=sign("cd-1", proc.pid, start, port, argv_sig, "ssm"))
            await mgr._reclaim_orphan_forwarder(reg.get("cd-1"), params)
            assert len(seen) == 1
            assert proc.poll() is None
        finally:
            self._cleanup(proc)

    # A pid no test host runs: stands in for the recorded spawning gateway.
    _FAKE_PARENT_PID = 3_999_991

    @pytest.mark.parametrize("tree", [False, True])
    @pytest.mark.parametrize("windows", [True, False])
    def test_windows_kill_is_pinned_to_the_recorded_start_time(self, monkeypatch, windows, tree):
        """Every pid signal goes through ``kill_pid_pinned`` with the recorded start
        (on POSIX that delegates straight to ``kill_pid``). A Windows SSM forwarder
        is not signalled at all: the group signal there is an unpinned
        ``taskkill /T``, and ending the wrapper alone strands its plugin child."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        pinned: list = []
        bare: list = []
        monkeypatch.setattr(pc, "process_start_time", lambda pid: "S1")
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: not (pinned or bare))
        monkeypatch.setattr(stm, "_is_addr_free", lambda port, host: bool(pinned or bare))
        monkeypatch.setattr(
            pc, "kill_pid_pinned", lambda pid, start, sig: pinned.append((pid, start, sig)) or True
        )
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: bare.append((pid, sig)) or True)
        monkeypatch.setattr(
            stm._SshTunnel,
            "_signal_group",
            staticmethod(lambda pid, sig: bare.append(("tree", pid, sig)) or True),
        )
        monkeypatch.setattr(pc, "pgroup_exists", lambda pgid: not (pinned or bare))
        monkeypatch.setattr(stm, "_RECLAIM_POLL_INTERVAL_SECS", 0.001)
        monkeypatch.setattr(pc, "IS_WINDOWS", windows)

        outcome = stm._verify_and_reclaim_forwarder(4242, "S1", ["ssh", "h"], 7778, tree, "t")

        if tree and windows:
            assert outcome == "windows_group_unsupported"
            assert pinned == [] and bare == [], "a Windows SSM forwarder was signalled"
        elif tree:
            assert outcome == "reclaimed"
            assert pinned == []
            assert bare == [("tree", 4242, pc.SIGTERM)]
        else:
            assert outcome == "reclaimed"
            assert pinned == [(4242, "S1", pc.SIGTERM)]
            assert bare == []

    def _windows_orphan_gate(self, monkeypatch, *, parent):
        """Run the REAL orphan test on its Windows branch, for that call only.

        *parent* is the state of the forwarder's recorded parent pid:
        ``"gone"`` (nothing there), ``"older"`` (alive, started before the
        forwarder) or ``"later"`` (pid reused by a process started after it).
        Only the parent's facts are faked, as Windows creation FILETIMEs ordered
        around a fixed forwarder token, so the order is settled the same way on
        every host whatever its native start-token format. The recorded identity
        and argv stay real, so every gate behind this one runs for real.
        ``IS_WINDOWS`` is flipped only inside the wrapped call, so nothing else
        in ``connect`` takes a Windows code path on a POSIX host.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        real_state = stm._forwarder_orphan_state
        fake_ppid = self._FAKE_PARENT_PID

        forwarder_filetime = "133000000000000000"
        parent_filetime = {
            "gone": None,
            "older": "132999999999999999",
            "later": "133000000000000001",
        }[parent]

        def gate(pid, start):
            real_start_time = pc.process_start_time
            real_exists = pc.pid_exists
            saved = (pc.IS_WINDOWS, pc.get_ppid, pc.process_start_time, pc.pid_exists)
            # flake-ok: in-call swap of platform_compat globals in the to_thread worker, restored in finally; moved verbatim
            pc.IS_WINDOWS = True
            pc.get_ppid = lambda p: fake_ppid if p == pid else -1
            pc.process_start_time = lambda p: (
                parent_filetime if p == fake_ppid else real_start_time(p)
            )
            pc.pid_exists = lambda p: parent != "gone" if p == fake_ppid else real_exists(p)
            try:
                return real_state(pid, forwarder_filetime)
            finally:
                pc.IS_WINDOWS, pc.get_ppid, pc.process_start_time, pc.pid_exists = saved

        monkeypatch.setattr(stm, "_forwarder_orphan_state", gate)

    def _record(self, reg, *, port, pid, start, sig):
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.update(
            "cd-1",
            local_port=port,
            forwarder_pid=pid,
            forwarder_start=start,
            forwarder_sig=sig,
            was_connected=True,
        )

    @pytest.mark.parametrize("parent", ["gone", "later"])
    @pytest.mark.asyncio
    async def test_windows_orphan_with_dead_or_reused_parent_is_reclaimed(
        self, tmp_path, monkeypatch, parent
    ):
        """Windows never re-parents to pid 1. A forwarder whose recorded parent
        is gone, or whose parent pid now names a LATER process, is a leak: the
        reclaim gets past the orphan test and hands the verify-and-kill worker
        the recorded pid, start time and the forward argv.

        The worker itself is replaced by a recorder, so this runs on every
        platform: the kill and the exact-argv read are pinned by their own tests
        (``_verify_and_reclaim_forwarder`` and ``TestProcessArgvMatchesExact``)."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._windows_orphan_gate(monkeypatch, parent=parent)
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid)
            assert start, "test needs a readable start-time identity"
            reg, mgr = self._mgr(tmp_path)
            self._record(
                reg, port=port, pid=proc.pid, start=start, sig=sign("cd-1", proc.pid, start, port)
            )

            reached: list = []
            monkeypatch.setattr(
                stm,
                "_verify_and_reclaim_forwarder",
                lambda pid, start, argv_, port_, tree, audit, argv_sig="": reached.append(
                    (pid, start, list(argv_), port_, tree)
                )
                or "identity_mismatch",
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert reached, "the orphan test refused a dead or reused parent"
            r_pid, r_start, r_argv, r_port, r_tree = reached[0]
            assert (r_pid, r_start, r_port, r_tree) == (proc.pid, start, port, False)
            assert r_argv == list(argv)
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_windows_forwarder_with_live_older_parent_is_refused_and_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        """A parent that is alive and older is the forwarder's real spawner --
        this gateway or a second one on the box. Refused, and the miss is logged."""
        import logging

        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, argv = self._spawn_port_holder()
        try:
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._windows_orphan_gate(monkeypatch, parent="older")
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path)
            self._record(
                reg, port=port, pid=proc.pid, start=start, sig=sign("cd-1", proc.pid, start, port)
            )

            with caplog.at_level(logging.INFO, logger=stm.logger.name):
                st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "a live-parented forwarder was signalled"
            assert any(
                "Not reclaiming recorded" in r.getMessage()
                and f"parent pid {self._FAKE_PARENT_PID}" in r.getMessage()
                for r in caplog.records
            )
        finally:
            self._cleanup(proc)

    @pytest.mark.parametrize("orphan_test", ["posix", "windows"])
    @pytest.mark.parametrize("mismatch", ["sig", "start", "argv"])
    @pytest.mark.asyncio
    async def test_identity_mismatch_is_refused_behind_either_orphan_test(
        self, tmp_path, monkeypatch, orphan_test, mismatch
    ):
        """With the orphan gate OPEN on either platform's test, a forged MAC, a
        start-time mismatch or a foreign argv still signals nothing.

        Reclaim needs the recorded listener, not a replacement tunnel: allocating
        a replacement adds a probe-to-bind race unrelated to identity refusal.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        proc, port, argv = self._spawn_port_holder()
        try:
            if mismatch != "argv":
                monkeypatch.setattr(
                    stm,
                    "_build_ssh_tunnel_argv",
                    lambda host, lp, rp, compression=True: list(argv),
                )
            if orphan_test == "posix":
                self._fake_orphan(monkeypatch)
            else:
                self._windows_orphan_gate(monkeypatch, parent="gone")
            sign = self._pin_identity_key(monkeypatch)
            start = pc.process_start_time(proc.pid) or "x"
            recorded = "not-the-recorded-identity" if mismatch == "start" else start
            sig = "deadbeef" * 8 if mismatch == "sig" else sign("cd-1", proc.pid, recorded, port)
            reg, mgr = self._mgr(tmp_path)
            self._record(reg, port=port, pid=proc.pid, start=recorded, sig=sig)

            params = stm._TransportParams(method="ssh", ssh_host="cd-1-alias")
            async with mgr._lock:
                await mgr._reclaim_orphan_forwarder(reg.get("cd-1"), params)

            assert proc.poll() is None, f"a {mismatch}-mismatched forwarder was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_unrecorded_port_holder_is_never_signalled(self, tmp_path):
        """No recorded identity -> no candidate: the reclaim never scans the
        process table for whoever holds the port."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        proc, port, _argv = self._spawn_port_holder()
        try:
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update("cd-1", local_port=port, was_connected=True)  # identity stays unset

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "an unrecorded port holder was signalled"
        finally:
            self._cleanup(proc)

    @pytest.mark.asyncio
    async def test_free_port_short_circuits_before_the_identity_check(self, tmp_path, monkeypatch):
        """A free recorded port means nothing leaked: the recorded pid is not
        signalled even when its identity WOULD match (e.g. the pid was
        recycled onto an innocent process while nothing holds the port)."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        code = "import sys, time; sys.stdout.write('R'); sys.stdout.flush(); time.sleep(120)"
        argv = [sys.executable, "-c", code]
        proc = subprocess.Popen(
            argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            # flake-ok: pre-existing read of the child's one-byte ready mark returns on the print or its EOF; moved verbatim
            assert proc.stdout.read(1) == b"R"
            # Even a would-be-exact identity must not matter: the port gates.
            monkeypatch.setattr(
                stm,
                "_build_ssh_tunnel_argv",
                lambda host, lp, rp, compression=True: list(argv),
            )
            self._fake_orphan(monkeypatch)
            sign = self._pin_identity_key(monkeypatch)
            # A port nothing listens on: bind(0) to reserve one, then close it.
            # flake-ok: bind(0)-then-close port stands in for a free recorded port; moved verbatim, tracked for a de-flake pass
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
            s.close()

            start = pc.process_start_time(proc.pid) or "x"
            reg, mgr = self._mgr(tmp_path)
            reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
            reg.update(
                "cd-1",
                local_port=free_port,
                forwarder_pid=proc.pid,
                forwarder_start=start,
                forwarder_sig=sign("cd-1", proc.pid, start, free_port),
                was_connected=True,
            )

            st = await mgr.connect("cd-1")

            assert st.state == TunnelState.CONNECTED
            assert proc.poll() is None, "reclaim signalled a pid while its port was free"
        finally:
            self._cleanup(proc)

    @pytest.mark.skipif(not socket.has_ipv6, reason="host has no IPv6 support")
    def test_reclaim_ignores_a_foreign_ipv6_listener_on_the_same_port(self, monkeypatch):
        """A reclaim asks whether OUR forwarder released ITS port, not whether the
        port is free for a new one.

        An ``ssh -L`` child binds 127.0.0.1 alone, so an unrelated listener on
        ``::1`` says nothing about whether the orphan let go. Probing both
        families here would report a fully reclaimed forwarder as ``not_gone``
        and record that wrong outcome in the SEL audit.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        squatter = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        squatter.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            squatter.bind(("::1", 0))
        except OSError:
            squatter.close()
            pytest.skip("::1 is not assignable here")
        squatter.listen(1)
        port = squatter.getsockname()[1]

        monkeypatch.setattr(pc, "process_start_time", lambda pid: "identity-A")
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: False)  # child already exited
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: True)
        _route_pinned_kill_to_kill_pid(monkeypatch)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        try:
            # flake-ok: real 127.0.0.1 bind probe on the ::1 listener's port number; moved verbatim, tracked for a de-flake pass
            outcome = stm._verify_and_reclaim_forwarder(
                4242, "identity-A", ["ssh", "-N"], port, False, "instance=t pid=4242"
            )
        finally:
            squatter.close()

        assert (
            outcome == "reclaimed"
        ), "a foreign ::1 listener must not make a released IPv4 forward read as not_gone"

    def test_sigkill_withheld_when_identity_changes_during_grace(self, monkeypatch):
        """Open-box guard test: if the pid stops matching its recorded
        start-time identity during the TERM grace (exit + recycle inside a
        poll gap, which pid_exists cannot observe), the destructive SIGKILL is
        withheld — leak-not-mis-kill."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        starts = iter(["identity-A", "identity-B"])  # pre-TERM, then re-check

        monkeypatch.setattr(pc, "process_start_time", lambda pid: next(starts))
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: True)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        _route_pinned_kill_to_kill_pid(monkeypatch)
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["ssh", "-N"], 50505, False, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld on identity change"

    def test_sigkill_withheld_when_pid_vanishes_but_port_lingers(self, monkeypatch):
        """Open-box guard test: a pid that does not exist while the port is
        still held (SSM wrapper gone, plugin lingering — or a recycle inside a
        poll gap) is NOT a safe SIGKILL fall-through: getpgid on a recycled
        pid would resolve the REPLACEMENT process. No verified identity, no
        SIGKILL."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        starts = iter(["identity-A", None])  # pre-TERM ok; re-check: pid gone
        # The POSIX group-signal path: a Windows SSM forwarder is never signalled
        # (test_windows_kill_is_pinned_to_the_recorded_start_time[windows-tree]).
        monkeypatch.setattr(pc, "IS_WINDOWS", False)

        monkeypatch.setattr(pc, "process_start_time", lambda pid: next(starts))
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: True)
        monkeypatch.setattr(pc, "pid_exists", lambda pid: False)
        monkeypatch.setattr(pc, "pgroup_exists", lambda pgid: True)  # plugin lingers
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        _route_pinned_kill_to_kill_pid(monkeypatch)
        monkeypatch.setattr(
            stm._SshTunnel,
            "_signal_group",
            staticmethod(lambda pid, sig: delivered.append(sig) or True),
        )
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["aws", "ssm"], 50506, True, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld when the pid is gone"

    def test_sigkill_withheld_when_argv_stops_matching_during_grace(self, monkeypatch):
        """Open-box guard test: on macOS the start token is 1s-granular, so a
        same-second pid reuse can keep it matching — the argv half of the
        re-check is what breaks the tie. argv mismatch at escalation time
        withholds the SIGKILL."""
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        delivered: list[int] = []
        argv_answers = iter([True, False])  # pre-TERM ok; re-check: different argv

        monkeypatch.setattr(pc, "process_start_time", lambda pid: "identity-A")
        monkeypatch.setattr(pc, "process_argv_matches_exact", lambda pid, argv: next(argv_answers))
        monkeypatch.setattr(pc, "pid_exists", lambda pid: True)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: delivered.append(sig) or True)
        _route_pinned_kill_to_kill_pid(monkeypatch)
        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": False)
        monkeypatch.setattr(stm, "_RECLAIM_TERM_GRACE_SECS", 0.05)

        outcome = stm._verify_and_reclaim_forwarder(
            4242, "identity-A", ["ssh", "-N"], 50507, False, "instance=t pid=4242"
        )

        assert outcome == "recycled_during_grace"
        assert delivered == [pc.SIGTERM], "SIGKILL must be withheld on argv change"
