"""The driver supervisor against a fake stdio driver process."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod
from kiro_crew.computer_use.driver_runtime.supervisor import (
    DriverSupervisor,
    DriverUnavailable,
    OutcomeUnknown,
    ToolNotAllowed,
)
from kiro_crew.testing.wait import default_timeout, wait_until

FAKE = Path(__file__).with_name("computer_use_fake_driver.py")


def _make(tmp_path: Path, *extra: str, **kwargs) -> tuple[DriverSupervisor, Path]:  # type: ignore[no-untyped-def]
    log = tmp_path / "calls.jsonl"
    sup = DriverSupervisor(
        tmp_path / "home",
        resolve_binary=lambda: (Path(sys.executable), ""),
        mcp_args=(str(FAKE), "mcp", "--log", str(log), *extra),
        backoff=(0.0,),
        **kwargs,
    )
    return sup, log


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
def cleanup():  # type: ignore[no-untyped-def]
    made: list[DriverSupervisor] = []
    yield made
    for sup in made:
        sup.stop()


class TestAllowlist:
    def test_forbidden_tools_are_not_allowed(self) -> None:
        assert not (sup_mod.ALLOWED_TOOLS & sup_mod.FORBIDDEN_TOOLS)
        for name in (
            "install_ffmpeg",
            "install_extension",
            "check_for_update",
            "run_script",
            "kill_app",
            "launch_app",
        ):
            assert name not in sup_mod.ALLOWED_TOOLS

    def test_disallowed_tool_is_refused_before_anything_starts(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, log = _make(tmp_path)
        cleanup.append(sup)
        with pytest.raises(ToolNotAllowed):
            sup.call("run_script", {"script": "x"})
        assert not sup.is_running()
        assert _calls(log) == []


class TestCalls:
    def test_call_round_trip_and_handshake(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, log = _make(tmp_path)
        cleanup.append(sup)
        result = sup.call("list_windows", {"pid": 1})
        assert result["structuredContent"]["tool"] == "list_windows"
        assert sup.status()["state"] == sup_mod.STATE_RUNNING
        assert _calls(log) == [
            {
                "pid": result["structuredContent"]["pid"],
                "name": "list_windows",
                "arguments": {"pid": 1},
            }
        ]

    def test_env_fence_reaches_the_child(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        env_out = tmp_path / "env.json"
        base = {
            "PATH": "/usr/bin:/bin",
            "AWS_SECRET_ACCESS_KEY": "x",
            "DO_NOT_TRACK": "0",
            "HTTPS_PROXY": "http://p",
        }
        sup, _ = _make(tmp_path, "--env-out", str(env_out), base_env=base)
        cleanup.append(sup)
        sup.call("check_permissions")
        env = json.loads(env_out.read_text())
        assert env["DO_NOT_TRACK"] == "1"
        assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
        assert env["CUA_DRIVER_RS_UPDATE_CHECK"] == "false"
        assert "AWS_SECRET_ACCESS_KEY" not in env and "HTTPS_PROXY" not in env
        assert env["CUA_DRIVER_RS_HOME"].startswith(str(tmp_path / "home"))

    def test_concurrent_callers_are_serialized(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, log = _make(tmp_path)
        cleanup.append(sup)
        sup.ensure()
        in_flight = {"now": 0, "max": 0, "started": 0}
        lock_was_free: list[bool] = []
        guard = threading.Lock()
        real_request = sup._request

        def counting(child, method, params, timeout):  # type: ignore[no-untyped-def]
            with guard:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
                first = in_flight["max"] == 1 and not lock_was_free
            # The call lock is held for the whole request, never only around it.
            free = sup._call_lock.acquire(blocking=False)
            if free:
                sup._call_lock.release()
            lock_was_free.append(free)
            try:
                if first:
                    # Hold the first request until every caller has started, so
                    # the others are queued behind it rather than run after it.
                    wait_until(lambda: in_flight["started"] == 8, timeout=default_timeout())
                return real_request(child, method, params, timeout)
            finally:
                with guard:
                    in_flight["now"] -= 1

        sup._request = counting  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            with guard:
                in_flight["started"] += 1
            try:
                sup.call("click", {"element_token": f"s00000001:{i}"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        assert in_flight["max"] == 1
        assert lock_was_free == [False] * 8
        assert len(_calls(log)) == 8


class TestFailures:
    def test_crash_mid_call_is_unknown_outcome_and_never_replayed(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, log = _make(tmp_path, "--crash-on", "click")
        cleanup.append(sup)
        with pytest.raises(OutcomeUnknown, match="Re-read"):
            sup.call("click", {"element_token": "s00000001:5"})
        assert [c["name"] for c in _calls(log)] == ["click"]
        assert not sup.is_running()

    def test_restart_reinitializes_with_a_new_generation(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        marker = tmp_path / "crashed"
        sup, log = _make(tmp_path, "--crash-once-on", "click", "--marker", str(marker))
        cleanup.append(sup)
        sup.call("list_windows")
        first = sup.generation
        with pytest.raises(OutcomeUnknown):
            sup.call("click", {"element_token": "s00000001:5"})
        result = sup.call("list_windows")
        assert sup.generation == first + 1
        pids = {c["pid"] for c in _calls(log)}
        assert len(pids) == 2 and result["structuredContent"]["pid"] in pids
        # The crashed click was delivered once and not again on the new child.
        assert [c["name"] for c in _calls(log)].count("click") == 1

    def test_idempotent_read_is_retried_once_on_a_fresh_child(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        marker = tmp_path / "crashed"
        sup, log = _make(tmp_path, "--crash-once-on", "get_window_state", "--marker", str(marker))
        cleanup.append(sup)
        result = sup.call("get_window_state", {"pid": 1}, idempotent=True)
        assert result["structuredContent"]["tool"] == "get_window_state"
        assert [c["name"] for c in _calls(log)] == ["get_window_state", "get_window_state"]

    def test_stall_is_killed_and_reported_unknown(self, tmp_path: Path, cleanup, caplog) -> None:  # type: ignore[no-untyped-def]
        import logging

        caplog.set_level(logging.DEBUG)
        sup, log = _make(tmp_path, "--stall-on", "type_text")
        cleanup.append(sup)
        sup.ensure()
        pid = sup.status()["pid"]
        with pytest.raises(OutcomeUnknown, match="did not answer"):
            sup.call(
                "type_text",
                {"text": "hunter2-secret"},
                timeout=1.0,  # flake-ok: the fake never answers, so the deadline is the trigger
            )
        assert "hunter2-secret" not in caplog.text
        assert "hunter2-secret" not in json.dumps(sup.status())
        assert not sup.is_running()
        from kiro_crew import platform_compat

        wait_until(lambda: not platform_compat.pid_exists(pid), timeout=default_timeout())

    def test_a_child_that_stops_reading_cannot_hold_the_call_lock(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        # A payload larger than the pipe buffer blocks the write until the child
        # reads; the call deadline must cover that write, kill the child and
        # free the lock for the next caller.
        import threading

        sup, log = _make(tmp_path, "--stop-reading-on", "list_windows")
        cleanup.append(sup)
        sup.call("list_windows", {"pid": 1})
        first_pid = sup.status()["pid"]
        outcome: list[BaseException] = []

        def big_write() -> None:
            try:
                sup.call(
                    "type_text",
                    {"text": "é" * 2_000_000},
                    timeout=1.0,  # flake-ok: the fake stopped reading, so the deadline is the trigger
                )
            except BaseException as exc:  # noqa: BLE001 - the test inspects it
                outcome.append(exc)

        worker = threading.Thread(target=big_write, daemon=True)
        worker.start()
        worker.join(60)
        assert not worker.is_alive(), "the blocked write held the call past its deadline"
        assert len(outcome) == 1 and isinstance(outcome[0], OutcomeUnknown)
        assert "unknown" in str(outcome[0])
        # The lock is free and the next call runs on a fresh child.
        sup.call("check_permissions")
        assert sup.status()["pid"] != first_pid
        assert "type_text" not in [c["name"] for c in _calls(log)]

    def test_crash_loop_latches_until_restart(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, _ = _make(tmp_path, "--crash-on", "click")
        cleanup.append(sup)
        for _ in range(sup_mod.MAX_RESTARTS_IN_WINDOW):
            with pytest.raises(OutcomeUnknown):
                sup.call("click")
        with pytest.raises(DriverUnavailable, match="keeps exiting"):
            sup.call("list_windows")
        sup.restart()
        assert sup.call("list_windows")["structuredContent"]["tool"] == "list_windows"

    def test_unverified_binary_never_starts(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        spawned: list[object] = []
        sup = DriverSupervisor(
            tmp_path / "home",
            resolve_binary=lambda: (
                None,
                "the driver binary does not match the hash recorded at install",
            ),
            spawn=lambda *a: spawned.append(a),  # type: ignore[arg-type,return-value]
        )
        cleanup.append(sup)
        with pytest.raises(DriverUnavailable, match="hash"):
            sup.call("list_windows")
        assert spawned == []
        assert sup.status()["state"] == sup_mod.STATE_UNAVAILABLE


class TestShutdown:
    def test_stop_leaves_no_child(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        sup, _log = _make(tmp_path)
        cleanup.append(sup)
        sup.ensure()
        proc = sup._child.proc  # type: ignore[union-attr]
        sup.stop()
        assert proc.poll() is not None and not sup.is_running()

    def test_a_child_whose_gateway_died_exits_by_itself(self, tmp_path: Path, cleanup) -> None:  # type: ignore[no-untyped-def]
        # No pid is ever killed by number: a gateway that dies without stopping
        # closes the child's stdin, and the driver exits on end-of-file.
        sup, _log = _make(tmp_path)
        cleanup.append(sup)
        sup.ensure()
        proc = sup._child.proc  # type: ignore[union-attr]
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=default_timeout()) is not None

    def test_exit_before_the_handshake_names_the_exit_and_the_reason(self, tmp_path: Path) -> None:
        # What `cua-driver mcp` does on a Mac with no reachable app daemon: it prints
        # why and exits before answering initialize. No action was in flight.
        script = "import sys; sys.stderr.write('the app daemon is not reachable\\n'); sys.exit(2)"
        sup = DriverSupervisor(
            tmp_path / "home",
            resolve_binary=lambda: (Path(sys.executable), ""),
            mcp_args=("-c", script),
            backoff=(0.0,),
        )
        try:
            with pytest.raises(DriverUnavailable) as caught:
                sup.call("list_windows", {}, timeout=default_timeout(), idempotent=True)
        finally:
            sup.stop()
        message = str(caught.value)
        assert "exited during startup (exit code 2)" in message
        assert "the app daemon is not reachable" in message
        assert "whether the action happened" not in message
        assert "exited during startup" in sup.status()["last_error"]
