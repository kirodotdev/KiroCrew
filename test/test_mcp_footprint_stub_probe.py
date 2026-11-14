"""The stub reading the MCP page prices "with sharing" by when no stub runs.

With sharing off no stub runs, so ``footprint.measure`` has no stub to read and
could not forecast at all. ``footprint_probe`` starts one idle stub, reads its
PSS, stops it and keeps the reading for the gateway run; ``measure`` asks it
only when copies need pricing and no stub is running. Still a reading, never a
constant.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from mcp_resource_subscription_helpers import INIT_PARAMS, StubProcess, running_daemon, stub_argv
from test_mcp_footprint import KIB, SPECS, _proc, _stub, _two_chats
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

import kiro_crew
from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import daemon_control, footprint, footprint_probe
from kiro_crew.platform_compat_metrics import _proc_pss_kb


def _no_stub(root: Path) -> Path:
    for name in ("cmdline", "smaps_rollup", "stat"):
        (root / "104" / name).unlink()
    (root / "104").rmdir()
    return root


def _never(*_a: Any) -> int:
    raise AssertionError("the stub probe must not run here")


def test_with_no_stub_running_the_probe_reading_prices_each_stub(tmp_path: Path) -> None:
    root = _no_stub(_two_chats(tmp_path))
    calls: list[int] = []

    def reading() -> int:
        calls.append(1)
        return 12 * KIB

    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root, stub_reading=reading)
    assert r is not None
    assert r.using_now_bytes == (200 + 80) * KIB
    # One docs copy (mean 100) plus one probed stub per chat that had a copy;
    # creds never shares.
    assert r.with_sharing_bytes == (100 + 2 * 12 + 40 + 40) * KIB
    assert calls == [1]


def test_a_running_stub_is_read_and_the_probe_is_not_asked(tmp_path: Path) -> None:
    root = _two_chats(tmp_path)
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root, stub_reading=_never)
    assert r is not None
    assert r.with_sharing_bytes == (100 + 2 * 10 + 40 + 40 + 10) * KIB


def test_no_copy_to_price_never_asks_the_probe(tmp_path: Path) -> None:
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 103, 100, ["/usr/bin/creds-agent", "mcp"], 40)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path, stub_reading=_never)
    assert r is not None
    assert r.using_now_bytes == r.with_sharing_bytes == 40 * KIB


def test_a_probe_with_no_reading_keeps_with_sharing_hidden(tmp_path: Path) -> None:
    root = _no_stub(_two_chats(tmp_path))
    r = footprint.measure(
        [("a", 100), ("b", 200)], SPECS, proc_root=root, stub_reading=lambda: None
    )
    assert r is not None
    assert r.using_now_bytes == (200 + 80) * KIB
    assert r.with_sharing_bytes is None


def test_an_unreadable_process_does_not_start_the_probe(tmp_path: Path) -> None:
    root = _no_stub(_two_chats(tmp_path))
    (root / "102" / "smaps_rollup").unlink()
    r = footprint.measure([("a", 100), ("b", 200)], SPECS, proc_root=root, stub_reading=_never)
    assert r is not None and r.with_sharing_bytes is None


def test_a_stub_server_name_does_not_change_the_probe_reading(tmp_path: Path) -> None:
    # The probe prices a stub attached to a backend; a stub running its server
    # below it is a copy and is never the stub price.
    _proc(tmp_path, 100, 1, ["kiro-cli", "acp"], 50)
    _proc(tmp_path, 101, 100, _stub("docs"), 12)
    _proc(tmp_path, 102, 101, ["/opt/docs/bin/docs-mcp", "serve"], 88)
    r = footprint.measure([("a", 100)], SPECS, proc_root=tmp_path, stub_reading=lambda: 9 * KIB)
    assert r is not None
    assert r.with_sharing_bytes == (100 + 9) * KIB


class TestTheReadingIsKeptForTheGatewayRun:
    def setup_method(self) -> None:
        footprint_probe._reset_for_tests()

    def teardown_method(self) -> None:
        footprint_probe._reset_for_tests()

    def test_a_second_ask_does_not_start_a_second_stub(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawns: list[int] = []
        monkeypatch.setattr(footprint_probe, "_probe", lambda: spawns.append(1) or 7 * KIB)
        assert footprint_probe.idle_stub_bytes() == 7 * KIB
        assert footprint_probe.idle_stub_bytes() == 7 * KIB
        assert spawns == [1]

    def test_a_failed_probe_is_not_retried_on_every_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawns: list[int] = []
        monkeypatch.setattr(footprint_probe, "_probe", lambda: spawns.append(1) and None)
        assert footprint_probe.idle_stub_bytes() is None
        assert footprint_probe.idle_stub_bytes() is None
        assert spawns == [1]

    def test_off_linux_no_stub_is_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(footprint_probe.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(footprint_probe, "popen_limited", _never)
        assert footprint_probe._probe() is None


def test_probe_uses_the_rewriters_config_read_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.mcp_gateway import rewriter, rewriter_spec, stub

    ceiling = 8 * 1024 * 1024
    monkeypatch.setattr(
        loader, "_raw_config", lambda: {"mcp_gateway": {"read_buffer_limit_bytes": ceiling}}
    )
    # The parent must stamp config, not bake its per-process override into argv.
    monkeypatch.setenv("KIROCREW_MCP_READ_LIMIT", str(2 * ceiling))
    monkeypatch.setattr(footprint_probe.shutil, "which", lambda _name: sys.executable)
    socket_path = tmp_path / "gateway.sock"
    entry = rewriter_spec._build_stub_entry(
        stubs_dir=tmp_path / "stubs",
        server_name="probe",
        agent_name="probe",
        original={},
        env_pairs={},
        target_command=sys.executable,
        socket_path=socket_path,
        work_dir=tmp_path,
        sandbox_mode="off",
        approval_mode="auto",
        read_buffer_limit=rewriter.config_read_buffer_limit(),
    )
    rewritten = stub._parse_args(
        entry["args"][entry["args"].index(footprint_probe.STUB_MODULE) + 1 :]
    )
    argv = footprint_probe._probe_argv(str(socket_path))
    assert argv is not None
    assert "--read-limit" in argv
    assert stub._parse_args(argv[3:]).read_limit == rewritten.read_limit == str(ceiling)


@pytest.fixture
def isolated_stub_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    # The real wrap needs a sandbox backend the test host may not have; the
    # probe's own spawn, reading and reap are what is under test here. The
    # child imports this checkout's package, installed or not.
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": src, "KIROCREW_HOME": str(tmp_path / "home")}

    def unsandboxed(argv: list[str], **_kw: Any) -> tuple[list[str], dict[str, str], None]:
        return list(argv), env, None

    # _probe_on inherits cwd; --work-dir describes the target, not the stub's cwd.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(footprint_probe, "sandboxed_spawn_argv", unsandboxed)
    return env


def test_probe_children_use_the_test_home_and_work_dir(
    isolated_stub_env: dict[str, str], tmp_path: Path
) -> None:
    _, env, _ = footprint_probe.sandboxed_spawn_argv([], strip_python_env=True)
    assert Path(env["KIROCREW_HOME"]) == tmp_path / "home"
    assert Path.cwd() == tmp_path


@pytest.mark.skipif(not platform_compat.IS_LINUX, reason="smaps_rollup is Linux only")
class TestARealIdleStub:
    @pytest.fixture(autouse=True)
    def _spawns(
        self,
        _floor_monkeypatch: pytest.MonkeyPatch,
        isolated_stub_env: dict[str, str],
        tmp_path: Path,
    ) -> list[subprocess.Popen[bytes]]:
        started: list[subprocess.Popen[bytes]] = []
        real = footprint_probe.popen_limited

        def record(argv: Any, **kw: Any) -> subprocess.Popen[bytes]:
            assert kw["env"]["KIROCREW_HOME"] == str(tmp_path / "home")
            assert Path.cwd() == tmp_path
            proc = real(argv, **kw)
            started.append(proc)
            return proc

        _floor_monkeypatch.setattr(footprint_probe, "popen_limited", record)
        yield started
        for proc in started:
            platform_compat.kill_popen_tree(proc)
            proc.wait(timeout=10)

    def test_measures_an_attached_idle_stub_then_reaps_it(
        self, _spawns: list[subprocess.Popen[bytes]]
    ) -> None:
        reading = footprint_probe._probe(socket_dir=short_tmp_base())
        assert isinstance(reading, int) and reading > 0
        assert len(_spawns) == 1
        assert _spawns[0].returncode is not None

    def test_a_stub_that_never_registers_is_reaped_with_no_reading(
        self, monkeypatch: pytest.MonkeyPatch, _spawns: list[subprocess.Popen[bytes]]
    ) -> None:
        silent = [sys.executable, "-c", "import time; time.sleep(120)"]
        monkeypatch.setattr(footprint_probe, "_probe_argv", lambda _socket: silent)
        base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
        try:
            verdict = footprint_probe._probe(
                timeout=1.0,  # flake-ok: the child never connects; the wait length does not matter
                socket_dir=str(base),
            )
            assert verdict is None
            # The timeout removes the socket and its directory too.
            assert list(base.iterdir()) == []
        finally:
            shutil.rmtree(base, ignore_errors=True)
        assert len(_spawns) == 1
        assert _spawns[0].returncode is not None

    def test_the_socket_is_private_and_in_the_gateway_runtime_dir(
        self, monkeypatch: pytest.MonkeyPatch, _spawns: list[subprocess.Popen[bytes]]
    ) -> None:
        base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
        seen: dict[str, Any] = {}
        real_attach = footprint_probe._attach

        def attach(proc: Any, server: socket.socket, deadline: float) -> Any:
            path = Path(server.getsockname())
            seen["dir"] = path.parent.parent
            seen["dir_mode"] = stat.S_IMODE(path.parent.stat().st_mode)
            seen["sock_mode"] = stat.S_IMODE(path.stat().st_mode)
            seen["family"] = server.family
            return real_attach(proc, server, deadline)

        monkeypatch.setattr(daemon_control, "configured_socket_path", lambda: base / "gateway.sock")
        monkeypatch.setattr(footprint_probe, "_attach", attach)
        try:
            assert isinstance(footprint_probe._probe(), int)
            assert seen == {
                "dir": base,
                "dir_mode": 0o700,
                "sock_mode": 0o600,
                "family": socket.AF_UNIX,
            }
            assert list(base.iterdir()) == []
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_the_private_socket_directory_is_removed(
        self, tmp_path: Path, _spawns: list[subprocess.Popen[bytes]]
    ) -> None:
        base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
        try:
            footprint_probe._probe(socket_dir=str(base))
            assert list(base.iterdir()) == []
        finally:
            shutil.rmtree(base, ignore_errors=True)


def _pss_readable() -> bool:
    return platform_compat.IS_LINUX and _proc_pss_kb(os.getpid()) >= 0


@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.skipif(not _pss_readable(), reason="PSS (smaps_rollup) is not readable here")
@pytest.mark.asyncio
async def test_the_probe_reads_within_15_percent_of_a_connected_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_sock_dir: Path,
    isolated_stub_env: dict[str, str],
) -> None:
    """The probe's idle stub costs what a stub attached to a real gatewayd costs.

    A stub that only answered as an empty server read about 40% low (16.5 MB
    against 27 MB), so the bound is what keeps the probe honest. Both stubs run
    this interpreter and this checkout, one after the other.
    """
    work = tmp_path / "ws"
    work.mkdir()
    async with running_daemon(
        monkeypatch,
        short_sock_dir / "gw.sock",
        work,
        tmp_path / "launches.txt",
        tmp_path / "subscribes.txt",
        tmp_path / "callers.txt",
    ):
        # The probe stub gets the scrubbed environment a routed spawn gets, so
        # the connected one runs with the same small one.
        env = isolated_stub_env
        assert Path(env["KIROCREW_HOME"]) == tmp_path / "home"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            footprint_probe.STUB_MODULE,
            *stub_argv(short_sock_dir / "gw.sock", work),
            "--read-limit",
            str(footprint_probe.config_read_buffer_limit()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=work,
            env=env,
        )
        stub = StubProcess(proc)
        try:
            reply = await stub.request(1, "initialize", INIT_PARAMS)
            assert "result" in reply, reply
            connected_kb = _proc_pss_kb(stub.proc.pid)
        finally:
            await stub.close()
    assert connected_kb > 0

    assert Path.cwd() == tmp_path
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
    try:
        probed = await asyncio.to_thread(footprint_probe._probe, socket_dir=str(base))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert isinstance(probed, int)
    probed_kb = probed // 1024
    assert abs(probed_kb - connected_kb) <= 0.15 * connected_kb, (probed_kb, connected_kb)
