from typing import Any


async def read_bounded(stream: Any, max_bytes: int, chunk_size: int = 65536) -> bytes:
    """Read from an aiohttp stream until EOF or max_bytes is exceeded.

    Returns the accumulated bytes. If the stream is larger than `max_bytes`,
    the returned bytes will have length > `max_bytes`, allowing callers to
    detect the overflow.
    """
    body = bytearray()
    while True:
        remaining = max_bytes + 1 - len(body)
        if remaining <= 0:
            return bytes(body)
        chunk = await stream.read(min(chunk_size, remaining))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
