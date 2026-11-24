"""A child that answers ``initialize`` and exits before ``session/new`` answers ends the start with its stderr.

The adapter started, so the death comes after the handshake: during ``session/new``
or during the ``session/load`` of a resumed session. ``_initialize_session`` raises
the same non-transient ``AcpError`` an exit during ``initialize`` raises, naming the
stage, the exit code and the child's stderr, which the chat does not read as a lost
connection to retry. An error frame ``session/new`` answers keeps its error.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient, AcpError, AcpProcessDied
from kiro_crew.llm_helpers import acp_error_is_pipe_death, acp_error_is_transient
from kiro_crew.testing.wait import finished

#: Each test starts a real interpreter child; one worker runs them in turn.
pytestmark = pytest.mark.xdist_group(name="acp_session_new_death_children")

#: "not running" is a phrase the chat reads as a dead process in an error's text, so
#: the verdict must not come from the tail.
_STDERR_LINE = "error: engine not running: failed to load engine binary"


def _adapter(*, load_session: bool, then: str) -> str:
    """A stub that answers ``initialize``, reads the next request, then does ``then``."""
    return (
        "import json, sys\n"
        "init = json.loads(sys.stdin.readline())\n"
        "caps = {'loadSession': %r}\n"
        "sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': init['id'],"
        " 'result': {'protocolVersion': 1, 'agentCapabilities': caps}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "req = json.loads(sys.stdin.readline())\n"
        "%s\n"
    ) % (load_session, then)


_SAY_AND_EXIT = f"sys.stderr.write({_STDERR_LINE!r} + '\\n')\nsys.stderr.flush()\nsys.exit(3)"
_ERROR_FRAME = (
    "sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': req['id'],"
    " 'error': {'code': -32602, 'message': 'bad cwd'}}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.readline()"
)


async def _client_over(script: str, tmp_path: Path) -> AcpClient:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = AcpClient(work_dir=tmp_path)
    client._process = proc
    client._pid = proc.pid
    client._stderr_task = asyncio.ensure_future(client._drain_stderr(proc.stderr))
    return client


async def _close(client: AcpClient) -> None:
    proc = client._process
    assert proc is not None
    if proc.returncode is None:
        proc.kill()
    await finished(proc.wait(), "the child's reap")
    task = client._stderr_task
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_a_child_exiting_before_session_new_answers_ends_the_start_with_its_stderr(
    tmp_path: Path,
) -> None:
    client = await _client_over(_adapter(load_session=False, then=_SAY_AND_EXIT), tmp_path)
    try:
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
    exc = raised.value
    assert str(exc) == f"the agent exited during session/new (code=3) — {_STDERR_LINE}"
    assert not isinstance(exc, AcpProcessDied)
    assert acp_error_is_transient(exc) is False
    assert acp_error_is_pipe_death(exc) is False
    assert isinstance(exc.__cause__, AcpError)


@pytest.mark.asyncio
async def test_a_child_exiting_during_session_load_ends_the_start_without_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resumed session's transcript exists, so the start sends session/load.
    monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
    (tmp_path / "sid-1.json").write_text("{}")
    client = await _client_over(_adapter(load_session=True, then=_SAY_AND_EXIT), tmp_path)
    client._resume_session_id = "sid-1"
    try:
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
    assert str(raised.value) == (f"the agent exited during session/load (code=3) — {_STDERR_LINE}")
    assert acp_error_is_pipe_death(raised.value) is False


@pytest.mark.asyncio
async def test_an_error_frame_from_session_new_keeps_its_error(tmp_path: Path) -> None:
    client = await _client_over(_adapter(load_session=False, then=_ERROR_FRAME), tmp_path)
    try:
        with pytest.raises(AcpError) as raised:
            await finished(client._initialize_session(), "the handshake")
    finally:
        await _close(client)
    assert raised.value.code == -32602
    assert "exited during" not in str(raised.value)
