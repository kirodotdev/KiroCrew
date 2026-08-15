"""Coverage for ``apps/lifecycle_scripts.run_lifecycle_script``.

The happy path is exercised elsewhere (``test_app_execution.py``); what is pinned
here are the guard and failure paths that decide whether a lifecycle script can
leave anything behind: the execution-boundary denial, a missing app directory,
the timeout tree-kill (including a kill that fails), and the sandbox wrapper's
temp-file cleanup in ``finally``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps import lifecycle_scripts


class _Process:
    """Minimal stand-in for the spawned lifecycle-script process."""

    def __init__(self, *, output: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self.pid = 4242
        self.returncode = returncode
        self._output = output
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            raise asyncio.TimeoutError
        return self._output, None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


@pytest.fixture
def admit(monkeypatch, tmp_path):
    """Neutralize the sandbox/admission layer and return the spawn recorder."""
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", lambda argv, **k: (argv, None))
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    calls: list[dict[str, Any]] = []

    def _install(proc: _Process) -> list[dict[str, Any]]:
        async def _spawn(*argv, **kwargs):
            calls.append({"argv": list(argv), "kwargs": kwargs})
            return proc

        monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
        return calls

    return _install


@pytest.mark.asyncio
async def test_denied_action_never_spawns(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(
        lifecycle_scripts, "app_execution_denied", lambda *a, **k: "execution not admitted"
    )

    async def _unexpected(*a, **k):
        pytest.fail("spawned a lifecycle script after denial")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _unexpected)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo hi")
    assert result == {"output": "execution not admitted", "failed": True, "denied": True}


@pytest.mark.asyncio
async def test_missing_app_directory_fails_without_spawning(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)

    async def _unexpected(*a, **k):
        pytest.fail("spawned a lifecycle script for a nonexistent app dir")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _unexpected)
    result = await lifecycle_scripts.run_lifecycle_script("absent-app", "echo hi")
    assert result["failed"] is True
    assert "app directory not found" in result["output"]
    assert "denied" not in result


@pytest.mark.asyncio
async def test_extra_env_is_merged_into_the_minimal_env(admit, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    calls = admit(_Process(output=b"ok\n"))
    result = await lifecycle_scripts.run_lifecycle_script(
        "demo-app", "echo ok", extra_env={"APP_TOKEN_SLOT": "slot-1"}
    )
    assert result == {"output": "ok", "failed": False}
    env = calls[0]["kwargs"]["env"]
    assert env["APP_TOKEN_SLOT"] == "slot-1"
    assert env["NONINTERACTIVE"] == "1"
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path / "demo-app")
    assert calls[0]["argv"][:2] == ["/bin/bash", "-c"]
    assert calls[0]["argv"][2].startswith("set -euo pipefail\n")


@pytest.mark.asyncio
async def test_output_is_tail_trimmed_to_twenty_lines(admit, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    admit(_Process(output=("\n".join(f"line{i}" for i in range(30)) + "\n").encode()))
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "seq 30")
    lines = result["output"].split("\n")
    assert len(lines) == 20
    assert lines[0] == "line10"
    assert lines[-1] == "line29"


@pytest.mark.asyncio
async def test_nonzero_exit_marks_the_run_failed(admit, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    admit(_Process(output=b"boom\n", returncode=3))
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "exit 3")
    assert result == {"output": "boom", "failed": True}


@pytest.mark.asyncio
async def test_timeout_kills_the_process_tree(admit, monkeypatch, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    proc = _Process(hang=True)
    admit(proc)
    killed: list[tuple[int, Any]] = []

    async def _kill_tree(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _kill_tree)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "sleep 99", timeout=7)
    assert result == {"output": "script timed out after 7s", "failed": True}
    assert killed == [(proc.pid, platform_compat.SIGTERM)]
    assert proc.waited is True
    assert proc.killed is False


@pytest.mark.asyncio
async def test_timeout_falls_back_to_kill_when_tree_kill_fails(
    admit, monkeypatch, tmp_path
) -> None:
    (tmp_path / "demo-app").mkdir()
    proc = _Process(hang=True)
    admit(proc)

    async def _kill_tree(pid, sig):
        raise OSError("no such process group")

    async def _wait_boom():
        raise RuntimeError("reap failed")

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _kill_tree)
    monkeypatch.setattr(proc, "wait", _wait_boom)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "sleep 99", timeout=1)
    assert result == {"output": "script timed out after 1s", "failed": True}
    assert proc.killed is True


@pytest.mark.asyncio
async def test_sandbox_temp_file_is_removed_after_the_run(monkeypatch, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    scratch = tmp_path / "sandbox-profile"
    scratch.write_text("profile", encoding="utf-8")
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", lambda argv, **k: (argv, str(scratch)))
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)

    async def _spawn(*argv, **kwargs):
        return _Process(output=b"done\n")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo done")
    assert result["failed"] is False
    assert not scratch.exists()


@pytest.mark.asyncio
async def test_temp_file_cleanup_failure_does_not_break_the_result(monkeypatch, tmp_path) -> None:
    (tmp_path / "demo-app").mkdir()
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle_scripts,
        "wrap_argv",
        lambda argv, **k: (argv, str(tmp_path / "never-written")),
    )
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)

    async def _spawn(*argv, **kwargs):
        return _Process(output=b"survived\n")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo survived")
    assert result == {"output": "survived", "failed": False}
