"""Papyrus — bounded reads from a child process's pipes.

One helper, shared by :mod:`.latex` and :mod:`.gitops`, because both spawn a child whose
output volume is decided by something outside our control and neither may buffer it
without a bound:

* the compiler prints whatever the ``.tex`` tells it to (``\\message`` in a loop,
  ``\\tracingall``, a package erroring once per line);
* ``git`` relays sideband progress from a REMOTE, so a hostile server chooses how much
  arrives.

Both had a display cap applied *after* the buffering (``MAX_LOG_CHARS``,
``MAX_OUTPUT_CHARS``), which bounds what is shown and not what is held — and with a
120s timeout that is a long window to write to memory at pipe speed, inside the
gateway's own process.

Lives in its own module rather than being imported across the two: ``gitops`` and
``latex`` are siblings with no dependency between them today, and adding one so they can
share a private function would be the wrong trade.
"""

from __future__ import annotations

import asyncio

#: Hard cap on bytes RETAINED from each pipe.
#:
#: Generous relative to either caller's display cap on purpose: this is an OOM backstop,
#: not a display limit, and a diagnostics parser wants a real tail to work with. 4MB is
#: far more than any legitimate build or clone emits and small enough that several
#: concurrent ones cannot matter.
MAX_CAPTURED_OUTPUT_BYTES = 4 * 1024 * 1024

#: Pipe read size. Large enough that a chatty child is not read a line at a time, small
#: enough to bound one iteration's allocation.
_PIPE_CHUNK_BYTES = 64 * 1024


async def read_capped(
    proc: asyncio.subprocess.Process, cap: int = MAX_CAPTURED_OUTPUT_BYTES
) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently, keeping at most *cap* bytes from each.

    Replaces ``proc.communicate()``, which buffers BOTH streams with no bound.

    Three properties are load-bearing:

    * **Both pipes are read concurrently.** That is what ``communicate()`` provides and a
      sequential read destroys: draining stdout to EOF first deadlocks the moment the
      child fills the stderr pipe buffer and blocks.
    * **The excess is DISCARDED, not the pipe closed.** Closing early would deliver
      ``SIGPIPE``/``EPIPE`` to the child and turn a noisy-but-valid run into a failed
      one.
    * **``read(n)``, not ``readline()``.** A single line has no length bound either, so a
      line-oriented cap is defeated by one enormous line.

    Falls back to ``communicate()`` for any stdio shape that is not a real
    ``StreamReader``. That is a genuine robustness guard, not only a test affordance:
    without it, an object whose ``read()`` never signals EOF spins the drain loop forever
    inside the gateway.
    """
    if not isinstance(proc.stdout, asyncio.StreamReader) and not isinstance(
        proc.stderr, asyncio.StreamReader
    ):
        return await proc.communicate()

    async def _drain(stream: asyncio.StreamReader | None) -> bytes:
        if not isinstance(stream, asyncio.StreamReader):
            return b""
        chunks: list[bytes] = []
        kept = 0
        while True:
            chunk = await stream.read(_PIPE_CHUNK_BYTES)
            if not chunk:
                break
            if kept < cap:
                chunks.append(chunk[: cap - kept])
                kept += len(chunks[-1])
            # Past the cap the bytes are read and dropped, so the child keeps draining
            # and never sees a broken pipe.
        return b"".join(chunks)

    out, err = await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
    # Reap: `communicate()` waited, so this must too, or the child is left a zombie
    # holding its pipes.
    await proc.wait()
    return out, err
