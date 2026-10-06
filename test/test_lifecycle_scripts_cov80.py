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
        # A hung child has no exit status until something kills it.
        self.returncode: int | None = None if hang else returncode
        self._output = output
        self._hang = hang
        self.killed = False
        self.waited = False
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        if self._hang and not self.killed:
            # Still hanging: both the site's own wait and the TERM grace
            # window time out until the SIGKILL escalation lands.
            raise asyncio.TimeoutError
        return self._output, None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

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
    # These tests exercise the POSIX run path (the `/bin/bash` spawn), so pin
    # IS_POSIX regardless of the host that runs them — otherwise the native-
    # Windows guard short-circuits every one of them on a Windows CI runner.
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)
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
    monkeypatch.setattr(lifecycle_scripts, "_TERM_GRACE_SECS", 0.01)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "sleep 99", timeout=7)
    assert result == {"output": "script timed out after 7s", "failed": True}
    # Graceful SIGTERM first with a bounded grace window (a script's cleanup
    # trap can run), then the shared helper escalates the whole tree to
    # SIGKILL when the child ignored the TERM.
    assert killed == [(proc.pid, platform_compat.SIGTERM), (proc.pid, platform_compat.SIGKILL)]
    # The pid-scoped kill is the helper's second barrel after the tree signal.
    assert proc.killed is True
    # The critical pin: every post-kill wait drains pipes via communicate();
    # a bare wait() on a killed child blocked writing into a full pipe would
    # hang the caller forever. Calls: the site's own wait, the TERM
    # grace, and the escalation reap.
    assert proc.communicate_calls == 3
    assert proc.waited is False


@pytest.mark.asyncio
async def test_timeout_grace_lets_a_term_trap_finish_without_sigkill(
    admit, monkeypatch, tmp_path
) -> None:
    """A script whose TERM trap exits within the grace window is never
    SIGKILLed — the graceful path stays graceful."""
    (tmp_path / "demo-app").mkdir()
    proc = _Process(hang=True)
    admit(proc)
    killed: list[tuple[int, Any]] = []

    async def _kill_tree(pid, sig):
        killed.append((pid, sig))
        # The script's TERM trap runs and the child exits inside the grace
        # window: the next communicate() returns instead of hanging.
        proc._hang = False
        proc.returncode = 0

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _kill_tree)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "sleep 99", timeout=7)
    assert result == {"output": "script timed out after 7s", "failed": True}
    assert killed == [(proc.pid, platform_compat.SIGTERM)]  # no SIGKILL escalation
    assert proc.killed is False
    assert proc.waited is False


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
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)

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
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)

    async def _spawn(*argv, **kwargs):
        return _Process(output=b"survived\n")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)
    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo survived")
    assert result == {"output": "survived", "failed": False}


@pytest.mark.asyncio
async def test_native_windows_refuses_hook_before_spawning(monkeypatch, tmp_path) -> None:
    """On a non-POSIX host a lifecycle hook is refused before any process is
    started: lifecycle scripts always run through ``/bin/bash``, which native
    Windows does not have. The runner returns its normal failed result carrying
    an unsupported-platform reason, logs one warning, and spawns nothing."""
    (tmp_path / "demo-app").mkdir()
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", False)

    async def _unexpected(*a, **k):
        pytest.fail("spawned a lifecycle script on native Windows")

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _unexpected)
    warnings: list[tuple] = []
    monkeypatch.setattr(lifecycle_scripts.logger, "warning", lambda *a, **k: warnings.append(a))

    result = await lifecycle_scripts.run_lifecycle_script("demo-app", "echo hi", action="on_enable")

    assert result["failed"] is True
    # The reason travels in the existing ``output`` field (the enable route's
    # ``script_output``); no extra result key is introduced for it.
    assert "unsupported_platform" not in result
    assert "native Windows" in result["output"]
    assert "/bin/bash" in result["output"]
    # Exactly one warning, naming the app and the action.
    assert len(warnings) == 1
    assert "demo-app" in warnings[0]
    assert "on_enable" in warnings[0]


@pytest.mark.asyncio
async def test_posix_host_still_runs_the_hook(admit, monkeypatch, tmp_path) -> None:
    """The guard is non-POSIX only — with IS_POSIX the hook runs unchanged. The
    `admit` fixture already pins IS_POSIX True, so this holds on any CI host."""
    (tmp_path / "demo-app").mkdir()
    assert lifecycle_scripts.platform_compat.IS_POSIX is True
    calls = admit(_Process(output=b"enabled\n"))
    result = await lifecycle_scripts.run_lifecycle_script(
        "demo-app", "echo enabled", action="on_enable"
    )
    assert result == {"output": "enabled", "failed": False}
    assert calls[0]["argv"][:2] == ["/bin/bash", "-c"]
