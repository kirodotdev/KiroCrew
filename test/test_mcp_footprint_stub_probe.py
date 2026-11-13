"""The stub reading the MCP page prices "with sharing" by when no stub runs.

With sharing off no stub runs, so ``footprint.measure`` has no stub to read and
could not forecast at all. ``footprint_probe`` starts one idle stub, reads its
PSS, stops it and keeps the reading for the gateway run; ``measure`` asks it
only when copies need pricing and no stub is running. Still a reading, never a
constant.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from test_mcp_footprint import KIB, SPECS, _proc, _stub, _two_chats
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

import kiro_crew
from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import footprint, footprint_probe


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


def _unsandboxed(argv: list[str], *_a: Any, **_k: Any) -> tuple[list[str], dict[str, str], None]:
    # The real wrap needs a sandbox backend the test host may not have; the
    # probe's own spawn, reading and reap are what is under test here. The
    # child imports this checkout's package, installed or not.
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    return list(argv), {"PATH": "/usr/bin:/bin", "PYTHONPATH": src}, None


@pytest.mark.skipif(not platform_compat.IS_LINUX, reason="smaps_rollup is Linux only")
class TestARealIdleStub:
    @pytest.fixture(autouse=True)
    def _spawns(self, _floor_monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
        started: list[subprocess.Popen[bytes]] = []
        real = footprint_probe.popen_limited

        def record(argv: Any, **kw: Any) -> subprocess.Popen[bytes]:
            proc = real(argv, **kw)
            started.append(proc)
            return proc

        _floor_monkeypatch.setattr(footprint_probe, "sandboxed_spawn_argv", _unsandboxed)
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
        verdict = footprint_probe._probe(
            timeout=1.0,  # flake-ok: the child never connects; the wait length does not matter
            socket_dir=short_tmp_base(),
        )
        assert verdict is None
        assert len(_spawns) == 1
        assert _spawns[0].returncode is not None

    def test_the_private_socket_directory_is_removed(
        self, tmp_path: Path, _spawns: list[subprocess.Popen[bytes]]
    ) -> None:
        base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
        try:
            footprint_probe._probe(socket_dir=str(base))
            assert list(base.iterdir()) == []
        finally:
            shutil.rmtree(base, ignore_errors=True)
