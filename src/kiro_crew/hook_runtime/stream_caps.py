"""A script hook's output caps: the bounded drain that never lets the child block
on a full pipe, the decode that marks truncation, and the concurrent
stdin/stdout/stderr exchange.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _HOOK_TRUNCATION_MARKER,
    )


async def _read_capped_stream(
    reader: "asyncio.StreamReader | None", cap: int
) -> tuple[bytes, bool]:
    """Drain *reader* fully, retaining at most *cap* bytes.

    Returns ``(retained_bytes, truncated)``. Bytes beyond *cap* are read and
    discarded so the child never blocks on a full OS pipe buffer (the deadlock
    ``communicate`` avoided by buffering everything — we avoid it by consuming
    everything, but only *keeping* a bounded prefix). Chunked reads keep peak
    memory at roughly ``cap`` regardless of how much the child writes.
    """
    if reader is None:
        return b"", False
    retained = bytearray()
    truncated = False
    while True:
        # A fixed read size bounds a single chunk; the loop bounds the total.
        chunk = await reader.read(65536)
        if not chunk:
            break
        if len(retained) < cap:
            room = cap - len(retained)
            retained.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        else:
            # Already at cap — keep draining so the pipe drains, drop the bytes.
            truncated = True
    return bytes(retained), truncated


def _decode_capped(raw: bytes, truncated: bool) -> str:
    """Decode capped raw bytes, appending the truncation marker when clipped.

    ``errors="replace"`` handles a multibyte sequence severed at the cap
    boundary: the trailing partial code point becomes U+FFFD rather than raising
    or silently dropping, so a UTF-8 stream clipped mid-character still decodes
    to a stable, safe string.
    """
    text = raw.decode(errors="replace")
    if truncated:
        text += _HOOK_TRUNCATION_MARKER
    return text


async def _communicate_capped(
    proc: "asyncio.subprocess.Process", stdin_data: bytes, cap: int
) -> tuple[bytes, bool, bytes, bool]:
    """Write *stdin_data*, then drain stdout and stderr concurrently under a cap.

    Concurrent draining (vs. sequential) is required for the same reason
    ``communicate`` reads both pipes at once: a child that fills stderr while we
    are still reading stdout would deadlock if we did not consume stderr in
    parallel. Returns ``(stdout, stdout_truncated, stderr, stderr_truncated)``.
    """

    async def _feed_stdin() -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            stdin.write(stdin_data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The hook may exit without reading stdin; that is not our error.
            pass
        finally:
            try:
                stdin.close()
            except Exception:
                pass

    stdin_task = asyncio.ensure_future(_feed_stdin())
    stdout_task = asyncio.ensure_future(_read_capped_stream(proc.stdout, cap))
    stderr_task = asyncio.ensure_future(_read_capped_stream(proc.stderr, cap))
    try:
        (stdout_b, stdout_trunc), (stderr_b, stderr_trunc) = await asyncio.gather(
            stdout_task, stderr_task
        )
        await stdin_task
        await proc.wait()
    except BaseException:
        # On timeout (CancelledError from wait_for) or any failure, cancel and
        # OBSERVE every helper before returning control to the reap path. Merely
        # calling cancel() leaves the StreamReader with an active waiter, so a
        # cleanup read can raise "read() called while another coroutine is
        # already waiting" and leak the process.
        for task in (stdin_task, stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdin_task, stdout_task, stderr_task, return_exceptions=True)
        raise
    return stdout_b, stdout_trunc, stderr_b, stderr_trunc
