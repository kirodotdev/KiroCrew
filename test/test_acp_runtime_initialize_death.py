"""A child that dies during ``initialize`` leaves its exit status and stderr on the error.

The death fails the pending ``initialize`` at stdout EOF, often before the stderr
drain and the reap have run. ``_initialize_handshake`` waits for both, so the
text a user sees on the card names the code and the child's last words.
"""

import asyncio
import sys

import pytest

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead
from kiro_crew.testing.wait import await_until

# Reads the initialize frame, says why it cannot start, exits 3 without answering.
_CHILD_DIES_DURING_INITIALIZE = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "sys.stderr.write('error: failed to load engine binary\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(3)\n"
)


async def _die_during_initialize(rt: AcpRuntime) -> AcpRuntimeDead:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD_DIES_DURING_INITIALIZE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rt._process = proc
    rt._pid = proc.pid
    rt._stderr_task = asyncio.ensure_future(rt._drain_stderr())
    reader = asyncio.ensure_future(rt._reader_loop())
    try:
        with pytest.raises(AcpRuntimeDead) as raised:
            await await_until(
                asyncio.ensure_future(rt._initialize_handshake({})), what="initialize"
            )
        return raised.value
    finally:
        for task in (reader, rt._stderr_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if proc.returncode is None:
            proc.kill()
        await await_until(asyncio.ensure_future(proc.wait()), what="the child's reap")


@pytest.mark.asyncio
async def test_a_child_dying_during_initialize_names_its_code_and_stderr(tmp_path):
    exc = await _die_during_initialize(AcpRuntime(work_dir=str(tmp_path)))
    text = str(exc)
    assert text.startswith("process exited (")
    assert "during initialize [returncode=3]" in text
    assert "stderr_tail: error: failed to load engine binary" in text
    assert "rc=None" not in text


@pytest.mark.asyncio
async def test_a_restricted_session_keeps_the_code_but_not_the_stderr(tmp_path):
    exc = await _die_during_initialize(AcpRuntime(work_dir=str(tmp_path), memory_mode="incognito"))
    text = str(exc)
    assert "[returncode=3]" in text
    assert "stderr_tail: <not retained>" in text
    assert "failed to load engine binary" not in text
