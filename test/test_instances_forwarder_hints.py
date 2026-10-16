"""The forwarder pid hints a connect records and the orphan-state predicate."""

from __future__ import annotations

import os

import pytest
from instances_helpers import _FakeTunnel


class TestForwarderPidHints:
    """The registry pid hint lifecycle: recorded on connect, moved by recovery,
    cleared by disconnect. Fake tunnels; no real processes or ports."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-free stub on the shared monkeypatch, moved verbatim; no test here calls undo()
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)

    def _mgr(self, tmp_path, *, factory=_FakeTunnel):
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

        # flake-ok: pre-existing literal base port feeds the real allocator's bind probe, which skips a busy port; moved verbatim
        return reg, SshTunnelManager(
            reg, base_port=54200, mint_token=ok_mint, tunnel_factory=factory
        )

    def test_registry_field_default_roundtrip_and_validation(self, tmp_path):
        from kiro_crew.instances.registry import Instance, InvalidInstanceError

        # Older registry files have no keys -> sentinel defaults.
        old = Instance.from_dict({"id": "a", "name": "A"})
        assert old.forwarder_pid == 0
        assert old.forwarder_start == ""
        # A hand-edited negative pid normalizes to the sentinel instead of
        # poisoning every later update() with a validation error.
        assert Instance.from_dict({"id": "a", "name": "A", "forwarder_pid": -7}).forwarder_pid == 0
        inst = Instance(
            id="a", name="A", ssh_host="host-a", forwarder_pid=4321, forwarder_start="12345"
        )
        d = inst.to_dict()
        assert d["forwarder_pid"] == 4321
        assert d["forwarder_start"] == "12345"
        loaded = Instance.from_dict(d)
        assert loaded.forwarder_pid == 4321
        assert loaded.forwarder_start == "12345"
        # A pid can never be negative; the sentinel 0 is the floor.
        bad = Instance(id="a", name="A", ssh_host="host-a", forwarder_pid=-1)
        with pytest.raises(InvalidInstanceError):
            bad.validate()
        bad_start = Instance(id="a", name="A", ssh_host="host-a", forwarder_start=123)  # type: ignore[arg-type]
        with pytest.raises(InvalidInstanceError):
            bad_start.validate()

    @pytest.mark.asyncio
    async def test_connect_persists_forwarder_identity(self, tmp_path, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # A REAL pid (our own), so the recorded start-time identity is the
        # genuine platform value rather than "".
        my_pid = os.getpid()
        key = b"k" * 32
        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: key)

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = my_pid
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        inst = reg.get("cd-1")
        assert inst.forwarder_pid == my_pid
        assert inst.forwarder_start == (pc.process_start_time(my_pid) or "")
        assert inst.forwarder_start != ""  # readable for a live process we own
        assert inst.local_port > 0
        # The argv the kernel reports for the child is recorded at spawn.
        assert inst.forwarder_argv_sig == (pc.process_argv_fingerprint(my_pid) or "")
        # The identity is signed with the gateway's key, bound to this
        # instance, pid, start, port, and argv fingerprint.
        assert inst.forwarder_sig == stm._forwarder_identity_sig(
            key,
            "cd-1",
            my_pid,
            inst.forwarder_start,
            inst.local_port,
            inst.forwarder_argv_sig,
            "ssh",
        )

    @pytest.mark.asyncio
    async def test_disconnect_clears_forwarder_identity_with_local_port(self, tmp_path):
        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = os.getpid()
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert reg.get("cd-1").forwarder_pid == os.getpid()
        await mgr.disconnect("cd-1")
        inst = reg.get("cd-1")
        # One atomic reset: a freed port is not reserved forever, and a stale
        # identity never even reaches a later reclaim's checks.
        assert inst.local_port == 0
        assert inst.forwarder_pid == 0
        assert inst.forwarder_start == ""
        assert inst.forwarder_sig == ""
        assert inst.forwarder_argv_sig == ""

    @pytest.mark.asyncio
    async def test_mark_recovered_refreshes_forwarder_identity(self, tmp_path):
        """A rebuild replaces the child; the recorded identity must move with
        it, or the replacement leaks unrecorded at the next hard-kill."""
        from kiro_crew import platform_compat as pc

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            # flake-ok: pre-existing literal stand-in pid for a dead child, never signalled; moved verbatim, tracked for a de-flake pass
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a fake child, never signalled; moved verbatim
            t.pid = 54321  # dead pid: start-time identity records as ""
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert reg.get("cd-1").forwarder_pid == 54321
        assert reg.get("cd-1").forwarder_start == ""
        mgr._tunnels["cd-1"].pid = os.getpid()  # the rebuilt child's pid
        await mgr._mark_recovered("cd-1", mgr._tunnels["cd-1"], mgr._tunnel_epoch["cd-1"])
        inst = reg.get("cd-1")
        assert inst.forwarder_pid == os.getpid()
        assert inst.forwarder_start == (pc.process_start_time(os.getpid()) or "")
        assert inst.was_connected is True
        # The port is part of that identity: it is what forwarder_sig is
        # signed over, so it tracks the live tunnel in the same write.
        assert inst.local_port == mgr._tunnels["cd-1"].status.local_port

    @pytest.mark.asyncio
    async def test_mark_recovered_persists_the_rebuilt_port(self, tmp_path, monkeypatch):
        """A rebuild landing on a different local port records THAT port.

        ``_recover`` rebuilds on the LIVE tunnel's port, so the port a recovery
        settles on can differ from the one ``connect`` assigned. Leaving the
        registry's ``local_port`` behind points every consumer that reads it
        (the pane URL, ``diagnose``'s fallback) at a port with no listener, and
        desyncs ``forwarder_sig`` — a MAC over the port — so the orphan reclaim
        refuses the very child this write exists to record.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        my_pid = os.getpid()
        key = b"k" * 32
        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: key)
        # Pin the start time: on a host where reading it fails (a sandbox that
        # denies the process query) it records as "" and the signing branch is
        # skipped, which would let the signature assertion below pass without
        # ever computing a signature.
        monkeypatch.setattr(pc, "process_start_time", lambda pid: "424242")

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = my_pid
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assigned = reg.get("cd-1").local_port
        assert assigned > 0

        # The replacement child bound a different loopback port.
        rebuilt = assigned + 1
        mgr._tunnels["cd-1"].status.local_port = rebuilt
        await mgr._mark_recovered("cd-1", mgr._tunnels["cd-1"], mgr._tunnel_epoch["cd-1"])

        inst = reg.get("cd-1")
        assert inst.local_port == rebuilt
        # The identity and the port it is signed with stay consistent, so a
        # later reclaim can still authenticate this child.
        assert inst.forwarder_sig == stm._forwarder_identity_sig(
            key, "cd-1", my_pid, "424242", rebuilt, inst.forwarder_argv_sig, "ssh"
        )
        assert inst.forwarder_sig != ""

    @pytest.mark.asyncio
    async def test_both_identity_writes_carry_the_same_fields(self, tmp_path, monkeypatch):
        """``connect`` and ``_mark_recovered`` write ONE record, from one helper.

        Both sites persist the same identity fields, and a field present in one
        write but absent from the other breaks the record: an identity without
        the ``local_port`` that ``forwarder_sig`` signs over fails its own
        verification, so the reclaim refuses the very child the write exists to
        record. Every other test here checks recorded VALUES, which a missing
        field can slip past; comparing the recorded KEY SETS is what fails when
        the two writes disagree.
        """
        import kiro_crew.instances.ssh_tunnel_manager as stm
        from kiro_crew import platform_compat as pc

        monkeypatch.setattr(stm, "_reclaim_identity_key", lambda: b"k" * 32)
        # Pin the start time: on a host where reading it fails (a sandbox that
        # denies the process query) it records as "" and the signing branch is
        # skipped, so the key-set comparison below would run against a record
        # whose signature was never computed.
        monkeypatch.setattr(pc, "process_start_time", lambda pid: "424242")

        def factory(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.pid = os.getpid()  # live pid: the signing branch actually runs
            return t

        reg, mgr = self._mgr(tmp_path, factory=factory)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        writes: list[dict] = []
        real_update = reg.update

        def recording_update(instance_id, **kwargs):
            writes.append(kwargs)
            return real_update(instance_id, **kwargs)

        # The manager resolves self._registry.update per call, so an instance
        # attribute is enough to observe both writes.
        monkeypatch.setattr(reg, "update", recording_update)

        await mgr.connect("cd-1")
        await mgr._mark_recovered("cd-1", mgr._tunnels["cd-1"], mgr._tunnel_epoch["cd-1"])

        identity_writes = [w for w in writes if "forwarder_sig" in w]
        assert len(identity_writes) == 2, writes
        connect_kwargs, recovered_kwargs = identity_writes

        # Pinned literally rather than only compared to each other: a field
        # dropped from BOTH sites is a regression, not agreement.
        identity_fields = {
            "local_port",
            "forwarder_pid",
            "forwarder_start",
            "forwarder_sig",
            "forwarder_argv_sig",
            "was_connected",
        }
        assert set(recovered_kwargs) == identity_fields
        # connect adds its own extra and nothing else.
        assert set(connect_kwargs) == identity_fields | {"mark_last_active"}
        assert connect_kwargs["mark_last_active"] is True
        # Same live tunnel both times, so the values agree as well as the keys.
        assert {k: connect_kwargs[k] for k in identity_fields} == recovered_kwargs
        assert recovered_kwargs["forwarder_sig"] != ""


class TestForwarderOrphanState:
    """``_forwarder_orphan_state``: POSIX keeps ``ppid == 1``; Windows asks
    whether the recorded parent is gone or was replaced by a later process."""

    FWD = 4242
    PARENT = 777
    START = "1000"

    def _patch(self, monkeypatch, *, windows, ppid, parent_start=None, parent_exists=True):
        from kiro_crew import platform_compat as pc

        seen: list = []
        monkeypatch.setattr(pc, "IS_WINDOWS", windows)
        monkeypatch.setattr(pc, "get_ppid", lambda pid: ppid if pid == self.FWD else -1)

        def start_time(pid):
            seen.append(("start", pid))
            return parent_start

        def exists(pid):
            seen.append(("exists", pid))
            return parent_exists

        monkeypatch.setattr(pc, "process_start_time", start_time)
        monkeypatch.setattr(pc, "pid_exists", exists)
        return seen

    @pytest.mark.parametrize(("ppid", "orphaned"), [(1, True), (777, False), (-1, False)])
    def test_posix_is_exactly_reparented_to_init(self, monkeypatch, ppid, orphaned):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        seen = self._patch(
            monkeypatch, windows=False, ppid=ppid, parent_start="1", parent_exists=False
        )

        assert _forwarder_orphan_state(self.FWD, self.START) == (orphaned, ppid)
        # POSIX never consults the parent's liveness or start time.
        assert seen == []

    def test_windows_parent_gone_is_orphaned(self, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        self._patch(
            monkeypatch, windows=True, ppid=self.PARENT, parent_start=None, parent_exists=False
        )

        assert _forwarder_orphan_state(self.FWD, self.START) == (True, self.PARENT)

    def test_windows_parent_alive_and_older_is_refused(self, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        self._patch(monkeypatch, windows=True, ppid=self.PARENT, parent_start="999")

        assert _forwarder_orphan_state(self.FWD, self.START) == (False, self.PARENT)

    def test_windows_parent_pid_reused_by_later_process_is_orphaned(self, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        self._patch(monkeypatch, windows=True, ppid=self.PARENT, parent_start="1001")

        assert _forwarder_orphan_state(self.FWD, self.START) == (True, self.PARENT)

    @pytest.mark.parametrize("parent_start", ["1000", "garbage", "Mon Oct  5 05:56:00 2026"])
    def test_windows_unsettled_order_is_refused(self, monkeypatch, parent_start):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        self._patch(monkeypatch, windows=True, ppid=self.PARENT, parent_start=parent_start)

        assert _forwarder_orphan_state(self.FWD, self.START) == (False, self.PARENT)

    def test_windows_parent_exists_but_unreadable_is_refused(self, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        self._patch(
            monkeypatch, windows=True, ppid=self.PARENT, parent_start=None, parent_exists=True
        )

        assert _forwarder_orphan_state(self.FWD, self.START) == (False, self.PARENT)

    @pytest.mark.parametrize("ppid", [-1, 0])
    def test_windows_unreadable_parent_pid_is_refused(self, monkeypatch, ppid):
        from kiro_crew.instances.ssh_tunnel_manager import _forwarder_orphan_state

        seen = self._patch(monkeypatch, windows=True, ppid=ppid, parent_exists=False)

        assert _forwarder_orphan_state(self.FWD, self.START) == (False, ppid)
        assert seen == []
