"""Regression test for stub run_bridge stdout writer-thread death.

If the dedicated stdout writer thread dies (write raised), producers must get
an error instead of blocking forever on a full queue while the bridge hangs
waiting on output that will never be flushed.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew.mcp_gateway import stub


class _RaisingBuffer:
    """A sys.stdout.buffer replacement whose write always fails."""

    def write(self, data: bytes) -> int:  # noqa: D401
        raise OSError("simulated broken stdout")

    def flush(self) -> None:
        pass


class _FakeStdout:
    buffer = _RaisingBuffer()


@pytest.mark.asyncio
async def test_bridge_terminates_when_writer_thread_dies(monkeypatch) -> None:
    """A dead writer thread must tear down the bridge, not hang it."""
    monkeypatch.setattr(sys, "stdout", _FakeStdout())

    # Socket-side reader feeds output lines to the stdout pump.
    reader = asyncio.StreamReader()
    for _ in range(400):  # more than the queue's maxsize (256)
        reader.feed_data(b"line\n")

    # stdin reader is never fed and stop_event never set, so the stdout pump is
    # the branch that must complete on writer death.
    stdin = asyncio.StreamReader()
    stop_event = asyncio.Event()
    writer = MagicMock()

    # Falls back to the real writer-thread path (stdout_writer=None).
    await asyncio.wait_for(
        stub.run_bridge(reader, writer, stop_event, stdin=stdin, stdout_writer=None),
        timeout=10,
    )


@pytest.mark.asyncio
async def test_bridge_terminates_when_writer_dies_and_upstream_silent(
    monkeypatch,
) -> None:
    """Writer dies after one line, then upstream goes quiet — bridge must still
    tear down.

    The producer-side check only fires while _emit is processing a NEW line, so
    if the writer thread dies and no further upstream line arrives, stdout_pump
    would park in reader.readuntil() forever. The loop-level writer-failure
    event must wake a dedicated bridge task and complete the bridge anyway.
    """
    monkeypatch.setattr(sys, "stdout", _FakeStdout())

    # Exactly one line: it is enqueued successfully, the writer thread then
    # fails writing it, and NOTHING more is fed (no EOF) so the read side
    # blocks. Only the loop-level failure signal can end the bridge here.
    reader = asyncio.StreamReader()
    reader.feed_data(b"only-line\n")

    stdin = asyncio.StreamReader()  # never fed
    stop_event = asyncio.Event()  # never set
    writer = MagicMock()

    await asyncio.wait_for(
        stub.run_bridge(reader, writer, stop_event, stdin=stdin, stdout_writer=None),
        timeout=10,
    )
