"""L1 turn-lock regression tests (Mesh _bg readuntil race).

The shared `_bg` ACP session is streamed by ~8 callers through ONE process's
single stdout StreamReader. asyncio's StreamReader permits exactly one waiting
reader; when a turn dies mid-readline and the next caller starts its own read on
the same reader, asyncio raises:

    RuntimeError: readuntil() called while another coroutine is already
    waiting for incoming data

L1 serializes whole turns with an asyncio.Lock inside AcpClient so two
coroutines can never read the same stdout concurrently.

`test_concurrent_turns_serialize` is the RED-FIRST test: it FAILS on current
(unmodified) client.py because the second turn's readline collides with the
first's parked read, and PASSES once L1 lands.

These use a REAL asyncio.StreamReader (not an AsyncMock) — only the real
StreamReader enforces the single-waiter rule that produces the race.

`test_aclosing_releases_lock_deterministically` covers the finalization edge:
the lock is held across _prompt_loop and released in its finally, but a consumer
that early-returns leaves the async-gen suspended (finally runs at deferred
finalization, not at the return). send_message_stream wraps the loop in
aclosing() for deterministic release; that test asserts it.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.client import AcpClient


def _client_with_real_reader(tmp_path):
    """An AcpClient whose _process.stdout is a real, never-fed StreamReader.

    With no data and no EOF, readline() parks — so turn A holds the reader's
    single waiter slot and turn B's readline collides (pre-L1) or blocks on the
    turn lock (post-L1).
    """
    client = AcpClient(work_dir=tmp_path)
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None  # _is_process_alive() -> True, so the loop reads
    client._process = proc
    return client, reader


@pytest.mark.asyncio
async def test_concurrent_turns_serialize(tmp_path):
    """Two concurrent _prompt_loop turns on one client must not raise the
    readuntil race. RED on current code, GREEN after L1."""
    client, _reader = _client_with_real_reader(tmp_path)

    async def drive(req_id):
        # Short per-turn deadline so an un-fed reader unblocks quickly.
        async for _action, _msg in client._prompt_loop(req_id, timeout=1.0):
            pass

    a = asyncio.create_task(drive(1))
    await asyncio.sleep(0.1)  # let turn A reach + park on readline
    b = asyncio.create_task(drive(2))

    results = await asyncio.gather(a, b, return_exceptions=True)

    readuntil = [
        r for r in results
        if isinstance(r, RuntimeError) and "readuntil" in str(r)
    ]
    assert not readuntil, (
        "concurrent _bg reads raced — L1 turn-lock missing or ineffective: "
        f"{results}"
    )


@pytest.mark.asyncio
async def test_turn_releases_on_normal_completion(tmp_path):
    """A turn that ends at its deadline must let a SUBSEQUENT turn run — i.e.
    the lock (post-L1) is released, no deadlock. Sequential, not concurrent."""
    client, _reader = _client_with_real_reader(tmp_path)

    async def drive(req_id):
        async for _action, _msg in client._prompt_loop(req_id, timeout=0.3):
            pass

    await drive(1)
    # If L1 held the lock past turn 1, this second turn would hang; bound it.
    await asyncio.wait_for(drive(2), timeout=5.0)


@pytest.mark.asyncio
async def test_aclosing_releases_lock_deterministically(tmp_path):
    """Document the _turn_lock finalization behavior and verify the deterministic
    hot path.

    A consumer that `return`s on "complete" without exhausting _prompt_loop
    leaves the async-gen SUSPENDED at the yield, holding _turn_lock. CPython
    runs the generator's finally via a DEFERRED scheduled athrow (next loop
    tick), not at the consumer's return — so on a bare early-return the lock is
    released promptly on a live loop, but NOT synchronously at the return point.

    send_message_stream avoids relying on that by wrapping the loop in
    `aclosing(...)`, which runs aclose() (and thus the finally) synchronously on
    block exit. This test asserts that deterministic release: iterate one event,
    leave the `async with aclosing(...)` block, and the lock is free with no
    pending finalization.
    """
    client, reader = _client_with_real_reader(tmp_path)

    # Feed one frame so the loop yields once before we exit the block.
    reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')

    from contextlib import aclosing

    async with aclosing(client._prompt_loop(1, timeout=5.0)) as loop:
        async for _action, _msg in loop:
            break  # early exit, as a consumer does on "complete"
    # aclosing ran the finally synchronously on block exit — lock is free NOW.
    assert not client._turn_lock.locked(), (
        "_turn_lock still held after aclosing() exit — aclose() did not run the "
        "_prompt_loop finally (deterministic release broken)"
    )

    # A subsequent turn must acquire promptly (bounded).
    async def drive(req_id):
        async for _a, _m in client._prompt_loop(req_id, timeout=0.3):
            pass

    await asyncio.wait_for(drive(2), timeout=5.0)
