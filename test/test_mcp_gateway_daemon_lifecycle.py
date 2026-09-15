"""The MCP gateway daemon has exactly one owner and one code revision.

The field case these pin: a daemon spawned on Sep 4 was still serving on Sep 8.
The gateway had restarted twice in between, each time with newer code, and each
time found "a healthy daemon already owns the socket" and ADOPTED it -- the
adoption gate compared target stems, which had not changed, and nothing else.
The daemon's pooled ``kirocrew mcp-core`` backend was a day older than the
control-frame shape the gateway read (``/api/session-directive`` moved from
``{kind,args}`` to ``{tool,raw_args}``), so every ``monitor_start`` for a day was
refused as ``not_derivable`` while the tool reported success.

Four properties close it, each pinned below:

1. **Fingerprint.** ``code_fingerprint()`` names the package tree's revision, so
   two processes from different checkouts disagree and two from one agree.
2. **Owner liveness.** A daemon spawned with ``--owner-pid`` exits on its own
   once that process is gone, PID-reuse-checked, so a SIGKILLed gateway leaves
   no daemon for the next one to adopt.
3. **Adoption gate.** A pong without a matching fingerprint is not adopted; the
   manager asks it to stand down on that ground and the daemon accepts it.
4. **Diagnosis.** A stale backend's ``{kind,args}`` body is answered
   ``stale_mcp_backend`` with the fix named, not the generic ``not_derivable``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew import code_fingerprint as cf
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="/proc and unix sockets")


# ── 1. fingerprint ───────────────────────────────────────────────────────


class TestCodeFingerprint:
    def test_is_stable_within_a_process(self) -> None:
        assert cf.code_fingerprint() == cf.code_fingerprint()
        assert cf.code_fingerprint()

    def test_a_git_tree_reports_head_and_a_digest_of_the_dirty_diff(self, tmp_path: Path) -> None:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(tmp_path)}
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(tmp_path), *args],
                check=True,
                capture_output=True,
                env=env,
                **UTF8_TEXT,
            )

        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            env=env,
            **UTF8_TEXT,
        ).stdout.strip()

        assert cf.fingerprint_of(pkg) == head
        (pkg / "a.py").write_text("x = 2\n", encoding="utf-8")
        first_edit = cf.fingerprint_of(pkg)
        assert first_edit.startswith(f"{head}+") and first_edit != head
        # A SECOND uncommitted edit on the same HEAD is different code, and
        # must not read as the same fingerprint -- a bare dirty bit did, and a
        # daemon from the first edit was then adopted by a gateway on the second.
        (pkg / "a.py").write_text("x = 3\n", encoding="utf-8")
        second_edit = cf.fingerprint_of(pkg)
        assert second_edit != first_edit
        assert second_edit.startswith(f"{head}+")

    def test_git_is_resolved_from_system_dirs_and_runs_with_no_planted_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shim on PATH or a diff driver in config must not execute.

        The binary comes from `trusted_git_bin` (fixed system directories, never
        PATH); global and system config are disabled; inherited `GIT_*`
        variables are dropped; the diff refuses external drivers and textconv.
        """
        seen: list[tuple[list[str], dict[str, str]]] = []

        class _R:
            returncode = 0
            stdout = "abc\n"

        def fake_run(argv, **kw):
            seen.append((argv, kw["env"]))
            r = _R()
            if "diff" in argv:
                r.stdout = b""
            return r

        monkeypatch.setattr(cf, "trusted_git_bin", lambda: "/usr/bin/git")
        monkeypatch.setattr(cf.subprocess, "run", fake_run)
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/tmp/evil")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'diff.external=/tmp/evil'")
        assert cf._git_fingerprint(tmp_path) == "abc"
        assert len(seen) == 2
        for argv, env in seen:
            assert argv[0] == "/usr/bin/git"
            assert env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert env["GIT_CONFIG_NOSYSTEM"] == "1"
            assert not any(
                k.startswith("GIT_")
                and k not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")
                for k in env
            )
            assert "core.pager=cat" in argv
        diff_argv = seen[1][0]
        assert "--no-ext-diff" in diff_argv and "--no-textconv" in diff_argv

    def test_no_trusted_git_means_the_mtime_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "trusted_git_bin", lambda: None)
        root = tmp_path / "p"
        root.mkdir()
        (root / "m.py").write_text("v = 0\n", encoding="utf-8")
        assert cf.fingerprint_of(root).startswith("mtime:")

    def test_two_checkouts_of_different_code_disagree(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        for root in (a, b):
            root.mkdir()
            (root / "m.py").write_text("v = 0\n", encoding="utf-8")
        # Not git trees, so the mtime rule applies: give them distinct mtimes.
        os.utime(a / "m.py", ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        os.utime(b / "m.py", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
        assert cf.fingerprint_of(a) != cf.fingerprint_of(b)
        assert cf.fingerprint_of(a).startswith("mtime:")

    @pytest.mark.asyncio
    async def test_warming_runs_off_the_loop_and_fills_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first computation runs git; an async caller must not pay that
        on its loop. Warming goes through a thread and every later sync read
        is the cached value."""
        import asyncio
        import threading

        cf.code_fingerprint.cache_clear()
        seen: list[str] = []
        real = cf._git_fingerprint

        def spy(root):
            seen.append(threading.current_thread().name)
            return real(root)

        monkeypatch.setattr(cf, "_git_fingerprint", spy)
        try:
            warmed = await cf.warm_code_fingerprint()
            assert seen and seen[0] != threading.main_thread().name
            assert cf.code_fingerprint() == warmed
            assert len(seen) == 1, "the sync read after warming is a cache hit"
        finally:
            cf.code_fingerprint.cache_clear()
        assert asyncio.get_running_loop() is not None

    def test_a_pycache_directory_does_not_change_the_answer(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        root.mkdir()
        (root / "m.py").write_text("v = 0\n", encoding="utf-8")
        before = cf.fingerprint_of(root)
        cache = root / "__pycache__"
        cache.mkdir()
        (cache / "m.cpython-312.pyc").write_bytes(b"\x00" * 8)
        os.utime(cache / "m.cpython-312.pyc", ns=(9_000_000_000_000_000_000,) * 2)
        assert cf.fingerprint_of(root) == before


# ── 2. owner liveness ────────────────────────────────────────────────────


class TestOwnerLivenessSweeper:
    @pytest.mark.asyncio
    async def test_exits_when_the_owner_process_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = asyncio.Event()
        monkeypatch.setattr(gw, "_process_start_time", lambda pid: "1000")
        alive = {"v": True}
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: alive["v"])
        task = asyncio.create_task(gw._owner_liveness_sweeper(4242, 0.01, stop))
        await asyncio.sleep(0.05)
        assert not stop.is_set(), "a live owner must not stop the daemon"
        alive["v"] = False
        await asyncio.wait_for(stop.wait(), timeout=5)
        await task

    @pytest.mark.asyncio
    async def test_a_recycled_pid_counts_as_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``pid_exists`` alone would keep serving whoever inherited the number."""
        stop = asyncio.Event()
        start = {"v": "1000"}
        monkeypatch.setattr(gw, "_process_start_time", lambda pid: start["v"])
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: True)
        task = asyncio.create_task(gw._owner_liveness_sweeper(4242, 0.01, stop))
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        start["v"] = "7777"  # same PID number, different process
        await asyncio.wait_for(stop.wait(), timeout=5)
        await task

    @pytest.mark.asyncio
    async def test_an_unreadable_probe_is_inconclusive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One EACCES between two good reads must not end a serving daemon."""
        stop = asyncio.Event()
        seq = iter(["1000", None, None, None, "1000", "1000"])
        monkeypatch.setattr(gw, "_process_start_time", lambda pid: next(seq, "1000"))
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: True)
        task = asyncio.create_task(gw._owner_liveness_sweeper(4242, 0.01, stop))
        await asyncio.sleep(0.15)
        assert not stop.is_set()
        stop.set()
        await task

    @pytest.mark.asyncio
    async def test_no_baseline_disables_the_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stop = asyncio.Event()
        monkeypatch.setattr(gw, "_process_start_time", lambda pid: None)
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: False)
        await asyncio.wait_for(gw._owner_liveness_sweeper(4242, 0.01, stop), timeout=5)
        assert not stop.is_set()

    def test_the_manager_names_itself_as_owner(self, tmp_path: Path) -> None:
        """The argv the manager builds carries this process's PID."""
        manager = mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))
        src = Path(mgr.__file__).read_text(encoding="utf-8")
        assert '"--owner-pid", str(os.getpid())' in src
        assert manager is not None


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_real_daemon_exits_when_its_owner_dies(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a throwaway owner process dies, the daemon follows it out.

    The owner is a ``sleep`` we kill, not the test process, so the daemon's
    exit is attributable to the owner check and nothing else.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.chdir(home)
    monkeypatch.setattr(gw, "_OWNER_LIVENESS_INTERVAL_SECS", 0.2)
    sock = short_sock_dir / "gw.sock"

    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(600)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    env = {**os.environ, "PYTHONPATH": str(Path(gw.__file__).resolve().parents[2])}
    daemon = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        # Same entry the manager uses, with the probe interval shortened so
        # the test does not wait the production 15 s.
        "import sys, asyncio\n"
        "from kiro_crew.mcp_gateway import gatewayd as g\n"
        "g._OWNER_LIVENESS_INTERVAL_SECS = 0.2\n"
        "sys.exit(asyncio.run(g._amain(sys.argv[1:])))",
        "--socket",
        str(sock),
        "--idle-timeout-secs",
        "60",
        "--max-backends",
        "1",
        "--owner-pid",
        str(owner.pid),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(home),
        start_new_session=True,
    )
    try:
        from kiro_crew.mcp_gateway import transport

        deadline = asyncio.get_running_loop().time() + 60
        while not await asyncio.to_thread(transport.endpoint_exists, sock):
            assert asyncio.get_running_loop().time() < deadline, "daemon never bound"
            await asyncio.sleep(0.05)
        from kiro_crew.mcp_gateway.daemon_control import describe_daemon

        info = await asyncio.to_thread(describe_daemon, sock)
        assert info is not None
        assert info.owner_pid == owner.pid
        assert info.pid == daemon.pid
        assert info.fingerprint == cf.code_fingerprint()
        from kiro_crew.platform_compat import process_start_time

        assert info.start_time == process_start_time(daemon.pid)

        owner.kill()
        await owner.wait()
        await asyncio.wait_for(daemon.wait(), timeout=60)
        assert daemon.returncode == 0, "owner death is the graceful path, not a crash"
    finally:
        for proc in (owner, daemon):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ── 3. adoption gate ─────────────────────────────────────────────────────


def _manager(tmp_path: Path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


class TestCodeDriftGate:
    def test_a_matching_fingerprint_is_not_drift(self, tmp_path: Path) -> None:
        assert _manager(tmp_path)._code_drift({"fingerprint": cf.code_fingerprint()}) is False

    def test_a_different_fingerprint_is_drift(self, tmp_path: Path) -> None:
        assert _manager(tmp_path)._code_drift({"fingerprint": "someone-else"}) is True

    def test_a_pong_without_a_fingerprint_is_drift(self, tmp_path: Path) -> None:
        """A pre-fingerprint daemon predates this code by construction."""
        assert _manager(tmp_path)._code_drift({"type": "pong", "targets": []}) is True

    @pytest.mark.asyncio
    async def test_a_fit_but_stale_incumbent_is_asked_to_stand_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Targets cover everything; only the code differs. That alone must act."""
        manager = _manager(tmp_path)
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": [], "fingerprint": "old"}),
        )
        asked = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)

        async def _spawn() -> dict[str, Any]:
            manager._process = _FakeProc()
            return {"type": "pong", "targets": [], "fingerprint": cf.code_fingerprint()}

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            asked.assert_awaited_once_with([], stale_code=True, orphaned=False)
            assert manager._adopted is False
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    def test_a_daemon_with_no_owner_is_not_someone_elses(self, tmp_path: Path) -> None:
        m = _manager(tmp_path)
        assert m._owned_by_a_live_other({"type": "pong"}) is False
        assert m._owned_by_a_live_other({"owner_pid": 0}) is False
        assert m._owned_by_a_live_other({"owner_pid": True}) is False
        assert m._owned_by_a_live_other({"owner_pid": os.getpid()}) is False

    def test_a_dead_owner_is_an_orphan_not_a_rival(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: False)
        assert _manager(tmp_path)._owned_by_a_live_other({"owner_pid": 4242}) is False

    def test_a_live_other_owner_is_a_rival(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: pid == 4242)
        assert _manager(tmp_path)._owned_by_a_live_other({"owner_pid": 4242}) is True

    @pytest.mark.asyncio
    async def test_a_daemon_owned_by_a_live_gateway_is_never_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Targets fit, code matches -- and it still belongs to someone else.

        Adopting it would tear the daemon out from under its owner, or on a
        restart racing the old gateway's exit, adopt a daemon whose owner
        sweeper is about to shut it down mid-traffic. The start refuses the
        broker instead; stubs fall back to per-session exec."""
        manager = _manager(tmp_path)
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: pid == 4242)
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(
                return_value={
                    "type": "pong",
                    "targets": [],
                    "fingerprint": cf.code_fingerprint(),
                    "owner_pid": 4242,
                }
            ),
        )
        asked = AsyncMock(side_effect=RuntimeError("must not stand down another owner's daemon"))
        monkeypatch.setattr(manager, "_request_stand_down", asked)
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn into a held socket"))
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawned)
        with caplog.at_level("ERROR", logger=mgr.__name__):
            assert await manager._start_locked() is False
        assert manager._adopted is False
        asked.assert_not_awaited()
        spawned.assert_not_awaited()
        assert any("owned by another LIVE gateway" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_orphan_is_replaced_not_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owner dead, same code, same stems: still not adopted.

        Its owner sweeper will end it within two probes; adopting hands every
        session a broker that exits under them. It is asked to stand down on
        the orphan ground and replaced with a daemon this process owns."""
        manager = _manager(tmp_path)
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: False)
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(
                return_value={
                    "type": "pong",
                    "targets": [],
                    "fingerprint": cf.code_fingerprint(),
                    "owner_pid": 4242,
                }
            ),
        )
        asked = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)

        async def _spawn() -> dict[str, Any]:
            manager._process = _FakeProc()
            return {
                "type": "pong",
                "targets": [],
                "fingerprint": cf.code_fingerprint(),
                "owner_pid": os.getpid(),
            }

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is False
            asked.assert_awaited_once_with([], stale_code=False, orphaned=True)
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    def test_orphan_detection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        m = _manager(tmp_path)
        assert m._orphaned({"type": "pong"}) is False
        assert m._orphaned({"owner_pid": 0}) is False
        assert m._orphaned({"owner_pid": os.getpid()}) is False
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: False)
        assert m._orphaned({"owner_pid": 4242}) is True
        monkeypatch.setattr(mgr.platform_compat, "pid_exists", lambda pid: True)
        assert m._orphaned({"owner_pid": 4242}) is False

    @pytest.mark.asyncio
    async def test_the_stand_down_frame_names_our_fingerprint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path)
        sent: list[dict[str, Any]] = []

        async def _rt(frame: dict[str, Any]) -> dict[str, Any]:
            sent.append(frame)
            return {"type": "standing-down", "missing": [], "stale_code": True}

        monkeypatch.setattr(manager, "_control_roundtrip", _rt)
        monkeypatch.setattr(mgr.transport, "singleton_lock_free", lambda p: True)
        assert await manager._request_stand_down([], stale_code=True) == mgr._RELEASED
        assert sent == [
            {"type": "stand-down", "need": [], "caller_fingerprint": cf.code_fingerprint()}
        ]

    @pytest.mark.asyncio
    async def test_a_refusing_stale_daemon_is_still_adopted_but_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail-open like target drift -- a refusing daemon is still serving."""
        manager = _manager(tmp_path)
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._REFUSED))
        with caplog.at_level("ERROR", logger=mgr.__name__):
            assert await manager._repair_or_adopt([], True) == mgr._ADOPT
        assert any("DIFFERENT CODE" in r.getMessage() for r in caplog.records)


class _FakeProc:
    pid = 12345
    returncode = None


class TestDaemonAcceptsAStaleCodeStandDown:
    def test_a_caller_on_different_code_is_honoured(self) -> None:
        stop = asyncio.Event()
        reply = gw._apply_stand_down(
            {"type": "stand-down", "need": [], "caller_fingerprint": "not-this-code"}, stop
        )
        assert reply["type"] == "standing-down"
        assert reply["stale_code"] is True
        assert stop.is_set()

    def test_a_caller_on_the_same_code_is_refused(self) -> None:
        """Nothing to gain from cycling a daemon that IS the caller's code."""
        stop = asyncio.Event()
        reply = gw._apply_stand_down(
            {"type": "stand-down", "need": [], "caller_fingerprint": cf.code_fingerprint()},
            stop,
        )
        assert reply["type"] == "stand-down-rejected"
        assert not stop.is_set()

    def test_an_orphan_claim_is_honoured_only_when_the_owner_really_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "_OWNER_PID", 4242)
        # Owner alive: the claim is false, nothing yields.
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: True)
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": [], "orphaned": True}, stop)
        assert reply["type"] == "stand-down-rejected"
        assert not stop.is_set()
        # Owner gone: yields on that ground alone.
        monkeypatch.setattr(gw, "_pid_exists", lambda pid: False)
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": [], "orphaned": True}, stop)
        assert reply["type"] == "standing-down"
        assert reply["orphaned"] is True
        assert stop.is_set()

    def test_an_orphan_claim_against_an_unowned_daemon_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "_OWNER_PID", 0)
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": [], "orphaned": True}, stop)
        assert reply["type"] == "stand-down-rejected"

    def test_the_old_frame_shape_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A manager without the fingerprint field asks by stems alone, as before."""
        for key in [
            k for k in os.environ if k.startswith(("KIROCREW_MCP_TARGET_", "MC_MCP_TARGET_"))
        ]:
            monkeypatch.delenv(key, raising=False)
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": ["CORE"]}, stop)
        assert reply["type"] == "standing-down"
        assert reply["missing"] == ["CORE"]

    def test_the_pong_carries_fingerprint_owner_and_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gw, "_OWNER_PID", 777)
        pong = gw._pong_payload()
        assert pong["type"] == "pong"
        assert pong["fingerprint"] == cf.code_fingerprint()
        assert pong["owner_pid"] == 777
        assert pong["pid"] == os.getpid()
        assert isinstance(pong["targets"], list)
        # Published from the token computed once at startup, never a syscall
        # (a ``ps`` on macOS) inside the connection handler.
        monkeypatch.setattr(gw, "_OWN_START_TIME", "12345")
        monkeypatch.setattr(
            gw,
            "_process_start_time",
            lambda pid: (_ for _ in ()).throw(AssertionError("pong must not stat")),
        )
        assert gw._pong_payload()["start_time"] == "12345"


# ── stub pool key ────────────────────────────────────────────────────────


class TestStubBinaryVersion:
    def test_kirocrew_servers_fold_in_the_code_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.mcp_gateway import stub

        monkeypatch.setattr(stub, "_binary_version", lambda cmd: "shimhash")
        assert (
            stub.pool_binary_version("kirocrew", ["mcp-core"])
            == f"shimhash+{cf.code_fingerprint()}"
        )

    def test_third_party_servers_are_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A git pull of Kiro Crew must not cold-start every npx server."""
        from kiro_crew.mcp_gateway import stub

        monkeypatch.setattr(stub, "_binary_version", lambda cmd: "shimhash")
        assert stub.pool_binary_version("npx", ["-y", "server-pdf"]) == "shimhash"
