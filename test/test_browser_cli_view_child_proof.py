"""What the spawned ``playwright-cli show`` child itself proves when the lookup cannot.

On a host whose listener lookup cannot attribute owners, adoption rests on the child's own
evidence: the listener its spawned descendant owns, the root start identity captured at
spawn, and the binding report its stdout carries. A recycled or same-second reused
descendant is inconclusive, a root mismatch reaps, the port-zero banner authorizes the
resolved port and a structurally blind start publishes once. The bounded reader that
captures the report, the one guard every view thread start goes through, the squatter
checks reuse, status and stop run against the recorded child, and the child's entry in the
gateway-owned session registry are pinned here too. The shared helpers live in
``browser_cli_view_helpers.py``.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import os
import threading

import pytest
from browser_cli_view_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    FakeProc,
    _child_record,
    _FakeClock,
    _linux_shape,
    _recorded_child_port,
    _recorded_info,
    _recorded_proc,
    _stub_port_owner,
    _windows_shape,
    reset_state_fixture,
)

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod


def _blind_host_child(
    monkeypatch: pytest.MonkeyPatch,
    owns_listener: list[bool | None],
    spawned: list[FakeProc],
) -> FakeProc:
    """Start one child where the global listener lookup cannot attribute owners."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawned.append(proc) or proc)
    reports = iter((True,))
    monkeypatch.setattr(
        mod,
        "_child_reported_binding",
        lambda child, port: next(reports, False),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: owns_listener[0],
        raising=False,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)
    return proc


def test_blind_host_accepts_a_listener_owned_by_the_spawned_descendant(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    checked: list[int] = []
    _linux_shape(monkeypatch)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)

    def _owns_listener(pid: int, port: int) -> bool:
        checked.append(pid)
        return pid == 9931

    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )

    assert mod.ensure_running() is not None
    assert checked == [4242, 9931]
    assert reset_state == []


@pytest.mark.parametrize(
    "descendant_reads",
    [("new",), ("old", "new")],
    ids=["before-probe", "after-probe"],
)
def test_recycled_descendant_identity_is_inconclusive_around_listener_probe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reset_state: list[int],
    descendant_reads: tuple[str, ...],
) -> None:
    proc = FakeProc(pid=4242)
    reads = iter(descendant_reads)
    probed: list[int] = []
    _linux_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: [
            platform_compat.ProcessDescendantIdentity(
                9931,
                4242,
                "old",
                platform_compat.ProcessIdentitySource.LSTART,
            )
        ],
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: str(pid) if pid == proc.pid else pytest.fail("root-only fallback"),
    )

    def _source_recheck(pid: int, source: platform_compat.ProcessIdentitySource) -> str:
        if pid == proc.pid:
            assert source is platform_compat.ProcessIdentitySource.ATOMIC
            return str(pid)
        assert pid == 9931
        assert source is platform_compat.ProcessIdentitySource.LSTART
        return next(reads)

    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        _source_recheck,
    )

    def _owns_listener(pid: int, port: int) -> bool:
        probed.append(pid)
        return pid == 9931

    monkeypatch.setattr(platform_compat, "process_owns_loopback_listener", _owns_listener)
    mod._child = _child_record(proc, 45613)
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    with caplog.at_level("DEBUG"):
        assert mod.ensure_running() is None

    assert _recorded_proc() is proc
    assert reset_state == []
    assert any(
        "enumerated=(pid=9931, ppid=4242, start='old'), "
        "current=(pid=9931, start='new')" in record.message
        for record in caplog.records
    )
    if len(descendant_reads) == 1:
        assert probed == [4242]
    else:
        assert probed == [4242, 9931]


def test_same_second_descendant_reuse_with_a_different_start_id_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    probed: list[int] = []
    captured_start = "1700000000.000001"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: [platform_compat.ProcessDescendantIdentity(9931, 4242, captured_start)],
    )
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: captured_start,
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: "1700000000.000002" if pid == 9931 else str(pid),
    )

    def _owns_listener(pid: int, port: int) -> bool:
        probed.append(pid)
        return pid == 9931

    monkeypatch.setattr(platform_compat, "process_owns_loopback_listener", _owns_listener)

    result = mod._verify_child_listener(proc, 45613, allow_report=False)

    assert result == (None, False)
    assert probed == [4242]


def test_root_identity_inconclusive_preserves_but_mismatch_reaps(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    starts: dict[int, str | None] = {4242: "4242", 4343: "4343"}
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_process_start_identity", lambda pid: starts[pid])
    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        lambda pid, source: starts[pid],
    )

    def _spawn(command: list[str], port: int) -> FakeProc:
        child = children.pop(0)
        spawned.append(child)
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn)

    first = mod.ensure_running()
    assert first is not None
    starts[4242] = None

    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": mod._OWNERSHIP_REASON,
    }
    assert mod.ensure_running() is None
    assert _recorded_proc() is spawned[0]
    assert _recorded_info() == first
    assert reset_state == []

    starts[4242] = "recycled"

    assert mod.ensure_running() is not None
    assert [child.pid for child in spawned] == [4242, 4343]
    assert _recorded_proc() is spawned[1]
    assert reset_state == [4242]


def test_blind_port_zero_banner_authorizes_the_resolved_startup_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    proof = proc.binding
    proof.record(42963)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda command, port: proc)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_binding",
        lambda child, port: mod._child_reported_port(child, port) is not None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)
    monkeypatch.setattr(mod, "time", _FakeClock())

    info = mod.ensure_running()

    assert proof.port == 0
    assert proof.reported_port() == 42963
    assert info == mod.ShowInfo("http://127.0.0.1:42963", 42963)
    assert _recorded_proc() is proc


def test_structurally_blind_start_publishes_once_then_withholds_a_squatter(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    spawn_ports: list[int] = []
    listener_owner = {"pid": proc.pid}
    reason = (
        "listener ownership cannot be re-proved on this host: "
        "lsof is absent or cannot attribute processes"
    )
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_spawn",
        lambda command, port: spawn_ports.append(port) or proc,
    )
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
        raising=False,
    )

    def _listener_verdict(
        child: FakeProc,
        port: int,
        *,
        allow_report: bool,
        proof_not_before: float | None = None,
    ) -> tuple[bool | None, bool]:
        if allow_report and listener_owner["pid"] == child.pid:
            return True, True
        return None, False

    monkeypatch.setattr(mod, "_verify_child_listener", _listener_verdict)

    first = mod.ensure_running()
    listener_owner["pid"] = 777
    later = mod.status()

    assert first == mod.ShowInfo("http://127.0.0.1:45613", 45613)
    assert later == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": reason,
    }
    assert mod.ensure_running() is None
    assert spawn_ports == [0]
    assert _recorded_proc() is proc
    assert _recorded_info() == first
    assert reset_state == []


def test_structurally_blind_dead_child_respawns_from_a_fresh_report(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    reported_by_pid = {4242: 45613, 4343: 45614}
    spawned: list[FakeProc] = []
    spawn_ports: list[int] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)

    def _spawn(command: list[str], port: int) -> FakeProc:
        spawn_ports.append(port)
        child = children.pop(0)
        spawned.append(child)
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: reported_by_pid.pop(child.pid, None),
        raising=False,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)

    first = mod.ensure_running()
    assert first == mod.ShowInfo("http://127.0.0.1:45613", 45613)
    spawned[0]._alive = False
    spawned[0].returncode = 1

    second = mod.ensure_running()
    status = mod.status()

    assert second == mod.ShowInfo("http://127.0.0.1:45614", 45614)
    assert status == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": (
            "listener ownership cannot be re-proved on this host: "
            "GetExtendedTcpTable is absent or cannot attribute processes"
        ),
    }
    assert spawn_ports == [0, 0]
    assert [child.pid for child in spawned] == [4242, 4343]
    assert _recorded_proc() is spawned[1]
    assert _recorded_info() == second
    assert reset_state == [4242]


def test_replacement_spawn_invalidates_listener_self_test_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    invalidations: list[None] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda command, port: children.pop(0))
    monkeypatch.setattr(
        mod,
        "_invalidate_listener_lookup_self_test_cache",
        lambda: invalidations.append(None),
        raising=False,
    )

    assert mod.ensure_running() is not None
    assert _recorded_proc() is not None
    _recorded_proc()._alive = False
    _recorded_proc().returncode = 1
    assert mod.ensure_running() is not None

    assert invalidations == [None, None]


def test_transient_listener_probe_failure_still_degrades_without_reaping(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    owns_listener: list[bool | None] = [True]
    spawned: list[FakeProc] = []
    proc = _blind_host_child(monkeypatch, owns_listener, spawned)
    reports = iter((True, False, False))
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: next(reports))

    assert mod.ensure_running() is not None
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: "/usr/sbin/lsof" if name == "lsof" else None,
    )
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: None)
    owns_listener[0] = None
    assert mod.ensure_running() is None
    status = mod.status()
    assert status["url"] is None
    assert status["reason"] == (
        "lsof at /usr/sbin/lsof did not attribute the gateway's own control "
        "listener; check its permissions/namespace"
    )
    assert _recorded_proc() is proc
    assert spawned == [proc]
    assert reset_state == []


def test_every_view_thread_start_uses_one_guard() -> None:
    source = inspect.getsource(mod)
    guard = inspect.getsource(mod._start_daemon_thread)

    assert source.count(".start()") == guard.count(".start()") == 1


def test_relay_thread_start_failure_reaps_spawned_child(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    public_port = mod._free_port()
    real_start = threading.Thread.start
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "_spawn", lambda command, port: proc)
    monkeypatch.setattr(mod, "_child_reported_port", lambda child, port: 45613)
    monkeypatch.setattr(
        mod,
        "_healthy",
        lambda port: pytest.fail("relay failure must stop before health probing"),
    )

    def _fail_relay_start(thread: threading.Thread) -> None:
        if thread.name == "browser-view-relay":
            raise RuntimeError("thread capacity exhausted")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", _fail_relay_start)

    assert mod.ensure_running(public_port) is None
    assert mod.status()["reason"] == "Couldn't start the browser view's connection relay"
    assert proc.stdout.closed
    assert proc.pid in reset_state
    assert not proc._alive


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("linux-proc", id="linux-proc"),
        pytest.param("windows-table", id="windows-table"),
        pytest.param("ps-fallback", id="ps-fallback"),
    ],
)
def test_spawn_captures_root_start_identity(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    proc = FakeProc(pid=4242)
    source_reads: list[str] = []
    expected_start = {
        "linux-proc": "linux-start",
        "windows-table": "windows-start",
        "ps-fallback": "ps-start",
    }[shape]
    expected_source = {
        "linux-proc": platform_compat.ProcessIdentitySource.ATOMIC,
        "windows-table": platform_compat.ProcessIdentitySource.WINDOWS,
        "ps-fallback": platform_compat.ProcessIdentitySource.LSTART,
    }[shape]
    expected_capture_reads = {
        "linux-proc": ["atomic"],
        "windows-table": ["windows"],
        "ps-fallback": ["atomic", "lstart"],
    }[shape]
    expected_recheck_reads = {
        "linux-proc": ["atomic"],
        "windows-table": ["windows"],
        "ps-fallback": ["lstart"],
    }[shape]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", shape == "windows-table")
    monkeypatch.setattr(platform_compat, "IS_POSIX", shape != "windows-table")
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod, "ui_socket_env", lambda env: {})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: proc)

    def _atomic(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("atomic")
        return "linux-start" if shape == "linux-proc" else None

    def _windows(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("windows")
        return "windows-start" if shape == "windows-table" else None

    def _lstart(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("lstart")
        return "ps-start" if shape == "ps-fallback" else None

    monkeypatch.setattr(platform_compat, "get_process_start_id", _atomic)
    monkeypatch.setattr(platform_compat, "process_start_time", _windows)
    monkeypatch.setattr(platform_compat, "_process_lstart", _lstart)
    monkeypatch.setattr(mod, "_start_daemon_thread", lambda thread: True)

    child = mod._spawn(["/n/pw"], 45613)
    assert child is not None and child.proc is proc
    proof = child.binding
    assert proof.root_identity == platform_compat.ProcessDescendantIdentity(
        proc.pid,
        0,
        expected_start,
        expected_source,
    )
    assert source_reads == expected_capture_reads

    source_reads.clear()

    assert mod._root_process_identity_matches(child, "test") is True
    assert source_reads == expected_recheck_reads


def test_spawn_reaps_child_when_proof_reader_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod, "ui_socket_env", lambda env: {})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: proc)

    def _refuse_start(thread: threading.Thread) -> None:
        raise RuntimeError("thread capacity exhausted")

    monkeypatch.setattr(threading.Thread, "start", _refuse_start)

    assert mod._spawn(["/n/pw"], 45613) is None
    assert proc.stdout.closed
    assert proc.pid in reset_state
    assert not proc._alive


def test_binding_reader_discards_after_proof_budget_until_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty child cannot block after the bounded proof window closes."""

    class NeverEof(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.stop = threading.Event()
            self.budget_reached = threading.Event()
            self.reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self.reads >= 4:
                self.budget_reached.set()
            return b"" if self.stop.is_set() else b"not the binding report\n"

    stream = NeverEof()
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_LINES", 4)
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 4096)
    monkeypatch.setattr(mod, "_BINDING_PROOF_TIMEOUT_S", 1.0)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    reader.start()
    try:
        assert stream.budget_reached.wait(timeout=1)
        reader.join(timeout=0.05)
        discarding_after_budget = reader.is_alive()
        assert discarding_after_budget
    finally:
        stream.stop.set()
        reader.join(timeout=1)

    assert not reader.is_alive()
    assert not proof.reported.is_set()


def test_binding_reader_drops_proof_at_the_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"Listening on http://127.0.0.1:45613\n")
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 8)

    mod._drain_child_output(stream, 45613, proof)

    assert not proof.reported.is_set()


def test_binding_reader_retains_a_matched_banner_after_later_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ChunkedBytesIO(io.BytesIO):
        def __init__(self, chunks: tuple[bytes, ...]) -> None:
            super().__init__()
            self._chunks = iter(chunks)

        def read(self, size: int = -1) -> bytes:
            return next(self._chunks, b"")

    banner = b"Listening on http://127.0.0.1:45613\n"
    stream = ChunkedBytesIO((banner, b"overflow", b""))
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", len(banner) + 4)

    mod._drain_child_output(stream, 45613, proof)

    assert proof.reported.is_set()


def _assert_binding_reader_drains_real_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    payload = b"x" * (1024 * 1024)
    errors: list[OSError] = []
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 32)

    def _write_payload() -> None:
        remaining = memoryview(payload)
        try:
            while remaining:
                written = os.write(write_fd, remaining)
                remaining = remaining[written:]
        except OSError as exc:
            errors.append(exc)
        finally:
            os.close(write_fd)

    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    writer = threading.Thread(target=_write_payload, daemon=True)
    try:
        reader.start()
        writer.start()
        writer.join(timeout=2)
        write_completed = not writer.is_alive()
    finally:
        stream.close()
        writer.join(timeout=1)
        reader.join(timeout=1)

    assert write_completed, "the child writer blocked after the proof reader exited"
    assert errors == []
    assert not reader.is_alive()
    assert not proof.reported.is_set()


def test_binding_reader_drains_a_real_pipe_after_the_proof_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_binding_reader_drains_real_pipe(monkeypatch)


def test_binding_reader_owns_a_duplicate_across_original_fd_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read, original_write = os.pipe()
    stream = os.fdopen(original_read, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    read_started = threading.Event()
    allow_read = threading.Event()
    read_fds: list[int] = []
    closed_fds: list[int] = []
    real_read = os.read
    real_write = os.write
    real_close = os.close

    def _tracked_read(fd: int, size: int) -> bytes:
        read_fds.append(fd)
        read_started.set()
        assert allow_read.wait(timeout=1)
        return real_read(fd, size)

    def _tracked_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(mod.os, "read", _tracked_read)
    monkeypatch.setattr(mod.os, "close", _tracked_close)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    foreign_read = -1
    foreign_write = -1
    try:
        reader.start()
        assert read_started.wait(timeout=1)
        stream.close()
        foreign_read, foreign_write = os.pipe()
        if foreign_read != original_read:
            os.dup2(foreign_read, original_read)
            real_close(foreign_read)
            foreign_read = original_read
        real_write(original_write, b"Listening on http://127.0.0.1:45613\n")
        real_close(original_write)
        original_write = -1
        real_write(foreign_write, b"Listening on http://127.0.0.1:49999\n")
        real_close(foreign_write)
        foreign_write = -1
        allow_read.set()
        reader.join(timeout=1)
    finally:
        allow_read.set()
        reader.join(timeout=1)
        for fd in (original_write, foreign_write, foreign_read):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    real_close(fd)

    assert not reader.is_alive()
    assert proof.reported_port() == 45613
    assert read_fds
    assert set(read_fds) != {original_read}
    assert closed_fds == [read_fds[0]]


def test_binding_reader_falls_back_to_blocking_drain_when_nonblocking_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_nonblocking(fd: int, blocking: bool) -> None:
        raise OSError("nonblocking pipe reads unavailable")

    monkeypatch.setattr(mod.os, "set_blocking", _refuse_nonblocking)

    _assert_binding_reader_drains_real_pipe(monkeypatch)


def test_binding_reader_accepts_a_tolerant_listener_banner() -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"[playwright] Browser view is listening at http://127.0.0.1:45613/\n")

    mod._drain_child_output(stream, 45613, proof)

    assert proof.reported.is_set()


def test_binding_reader_logs_when_stdout_has_no_listener_banner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"Dashboard ready; no listener URL was reported\n")

    with caplog.at_level("WARNING"):
        mod._drain_child_output(stream, 45613, proof, cli_version="0.1.99")

    assert not proof.reported.is_set()
    assert any(
        "playwright-cli 0.1.99" in r.message and "Listening on http://127.0.0.1:45613" in r.message
        for r in caplog.records
    )


def test_binding_reader_switches_to_drain_at_the_time_budget_with_a_silent_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_TIMEOUT_S", 0.05)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    try:
        reader.start()
        reader.join(timeout=0.2)
        assert reader.is_alive(), "the reader exited instead of draining after the proof timeout"
        assert not proof.reported.is_set()
    finally:
        os.close(write_fd)
        reader.join(timeout=1)
        stream.close()
    assert not reader.is_alive()


def test_reuse_replaces_a_live_child_after_global_squatter_proof(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    listener_owner = {"pid": 0}
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == listener_owner["pid"],
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid, candidate_pids=None: [])
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    def _spawn_fresh(cli: str, port: int) -> FakeProc:
        child = children.pop(0)
        spawned.append(child)
        listener_owner["pid"] = child.pid
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn_fresh)
    first = mod.ensure_running()
    assert first is not None

    listener_owner["pid"] = 777

    assert mod.ensure_running() is not None
    assert [child.pid for child in spawned] == [4242, 4343]
    assert 4242 in reset_state


def test_status_does_not_report_a_squatter_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` withholds a URL after Windows ownership moves to a squatter."""
    proc = FakeProc(pid=4242)
    listener_owner = {4242}
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: set(listener_owner),
    )
    assert mod.ensure_running() is not None
    assert mod.status()["status"] == "running"

    listener_owner.clear()
    listener_owner.add(777)

    assert mod.status()["status"] == "stopped"


def test_stop_clears_the_recorded_child_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale child port would be proved against a reaped tree."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=4242))
    _stub_port_owner(monkeypatch, listener_pids=(4242,))
    assert mod.ensure_running() is not None

    mod.stop()

    assert _recorded_child_port() is None


@pytest.mark.parametrize(
    ("shape", "expected_source"),
    [
        pytest.param(
            "posix",
            platform_compat.ProcessIdentitySource.ATOMIC,
            id="posix",
        ),
        pytest.param(
            "windows",
            platform_compat.ProcessIdentitySource.WINDOWS,
            id="windows",
        ),
    ],
)
def test_show_child_registers_in_the_gateway_owned_session_registry(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected_source: platform_compat.ProcessIdentitySource,
) -> None:
    """FP item 6: the ``show`` grid still lists the panel sessions.

    The grid (the documented CAPTCHA/2FA takeover surface) lists whatever is in
    the CLI session registry the ``show`` child runs against. The launcher
    registers each ``panel-`` session under the gateway-owned registry that
    :func:`ui_socket_env` pins (``<root>/ui/d``); this test proves ``_spawn``
    hands the ``show`` child that SAME env, so the grid and the launched
    sessions share one registry and the grid lists what a launch created.
    """
    if shape == "windows":
        _windows_shape(monkeypatch)
    else:
        _linux_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: str(pid))
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs["env"])
        assert kwargs["stdout"] is mod.subprocess.PIPE
        return FakeProc(stdout=b"Listening on http://127.0.0.1:7777\n")

    monkeypatch.setattr(mod, "cli_env", lambda: {"PATH": "/n"})
    monkeypatch.setattr(
        mod,
        "ui_socket_env",
        lambda env: {"PWTEST_SOCKETS_DIR": "/root/ui/s", "PWTEST_DAEMON_SESSION_DIR": "/root/ui/d"},
    )
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

    proc = mod._spawn(["/n/pw"], 7777)

    assert proc is not None
    proof = proc.binding
    assert proof.reported.wait(timeout=1)
    assert proof.port == 7777
    assert proof.root_identity.source is expected_source
    assert captured["env"]["PWTEST_SOCKETS_DIR"] == "/root/ui/s"
    assert captured["env"]["PWTEST_DAEMON_SESSION_DIR"] == "/root/ui/d"
