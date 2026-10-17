"""Regression tests for task_executor.run_tests process-group reaping.

A test/subprocess run that exceeds TEST_TIMEOUT must not orphan the spawned
process (or its children): on timeout the whole process group is signalled so
nothing keeps holding CPU/memory/file handles across runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from kiro_crew import platform_compat, task_executor


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_reap_process_group_kills_children() -> None:
    """_reap_process_group must terminate the whole group, not just the pid."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "sleep 300 & echo $!; wait",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    child_pid: int | None = None
    # Identity captured WITH the pid: a pid alone is not a handle, since the kernel
    # may reassign it the moment the process exits. `None` means unknown, and
    # unknown means do not signal.
    child_start_id: str | None = None
    # Set once the body has PROVEN the forked child is gone; see the finally.
    child_reaped = False
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        child_pid = int(line.decode().strip())
        child_start_id = platform_compat.get_process_start_id(child_pid)

        await task_executor._reap_process_group(proc)

        # Parent (sh) reaped.
        assert proc.returncode is not None

        # The forked `sleep` child in the same group must be gone too. Liveness
        # through the repo's own probe (AGENTS.md "Cross-platform"): a raw
        # ``os.kill(pid, 0)`` TERMINATES the target on Windows, and the sweep's
        # caller filter recognises only the sanctioned helper.
        for _ in range(50):
            if not platform_compat.pid_exists(child_pid):
                child_reaped = True
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("child process in the group survived reaping")
    finally:
        # Every failing exit above — the readline timeout, the int() parse, the
        # returncode assertion and the for-else pytest.fail — leaves a REAL
        # `sh` + `sleep 300` pair alive, and `start_new_session=True` put that
        # pair in its own session, i.e. in a group no run-level sweep can reach.
        # So a regression in _reap_process_group would park two live processes
        # in the host process table for the sleep's full five minutes on top of
        # reporting the failure. Kill the group first (that is the path the
        # readline timeout needs, where `sh` is still the group leader), then
        # the forked `sleep` by pid: once `sh` has been reaped, getpgid(proc.pid)
        # fails and the reparented child is reachable only by its own
        # pid. Routed through platform_compat rather than
        # os.killpg per the POSIX-call rule.
        with contextlib.suppress(ProcessLookupError, OSError):
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        # ONLY when the body did not already prove it dead: a pid is free for the
        # kernel to reassign the moment it exits, so a SIGKILL sent "just in case"
        # after a confirmed death is aimed at whatever process now holds the
        # number -- on every passing run.
        # Identity revalidated immediately before the signal: between the poll above
        # and here the child may have exited and its number been reassigned to an
        # unrelated host process, and this is a SIGKILL.
        if (
            child_pid is not None
            and not child_reaped
            and child_start_id is not None
            and platform_compat.get_process_start_id(child_pid) == child_start_id
        ):
            with contextlib.suppress(ProcessLookupError, OSError):
                platform_compat.kill_pid(child_pid, platform_compat.SIGKILL)
        if proc.returncode is None:
            # Bounded: SIGKILL cannot be blocked, so this returns at once — the
            # ceiling only exists so a wedged wait in a `finally` cannot replace
            # the real assertion failure with a hang.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=10)
        # Close the transport the test would otherwise leak: its __del__ emits
        # ResourceWarning and can raise "Event loop is closed" once the loop is
        # gone, attributed to whichever later test happens to trigger the GC.
        task_executor._close_proc_pipes(proc)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
async def test_run_tests_timeout_returns_and_reaps(monkeypatch, tmp_path) -> None:
    """A command exceeding TEST_TIMEOUT returns a timeout result without hanging."""
    monkeypatch.setattr(task_executor, "TEST_TIMEOUT", 1)

    try:
        success, output = await asyncio.wait_for(
            task_executor.run_tests(["sleep", "300"], tmp_path), timeout=20
        )
    except RuntimeError as exc:
        # No OS-level sandbox backend on this host at argv-build time (e.g. user
        # namespaces disabled) — the reaping logic itself is covered by the
        # helper test above, so skip the end-to-end spawn here.
        if "sandbox" in str(exc).lower():
            pytest.skip("no sandbox backend available on this host")
        raise

    # The sandbox wrapper can also fail at *runtime*: it re-execs and calls
    # unshare() itself, so on hosts where unprivileged user/mount namespaces are
    # blocked (many CI runners, incl. GitHub Actions) it aborts with a
    # 'sandbox: ...' message and a non-zero exit *before* the wrapped command
    # ever runs. That path can't exercise the timeout either, so skip it the
    # same way — the reap helper above already covers the signalling logic.
    if not success and output.lstrip().startswith("sandbox:"):
        pytest.skip("sandbox backend unavailable at runtime on this host")

    assert success is False
    assert "timed out" in output
