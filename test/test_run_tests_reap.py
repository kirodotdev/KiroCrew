"""Regression tests for task_executor.run_tests process-group reaping.

A test/subprocess run that exceeds TEST_TIMEOUT must not orphan the spawned
process (or its children): on timeout the whole process group is signalled so
nothing keeps holding CPU/memory/file handles across runs.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from kiro_crew import task_executor


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
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
    child_pid = int(line.decode().strip())

    await task_executor._reap_process_group(proc)

    # Parent (sh) reaped.
    assert proc.returncode is not None

    # The forked `sleep` child in the same group must be gone too.
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("child process in the group survived reaping")


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
