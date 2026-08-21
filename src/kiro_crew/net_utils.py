from typing import Any


async def read_bounded(stream: Any, max_bytes: int, chunk_size: int = 65536) -> bytes:
    """Read from an aiohttp stream until EOF or max_bytes is exceeded.
    
    Returns the accumulated bytes. If the stream is larger than `max_bytes`,
    the returned bytes will have length > `max_bytes`, allowing callers to
    detect the overflow.
    """
    body = bytearray()
    async for chunk in stream.iter_chunked(chunk_size):
        body.extend(chunk)
        if len(body) > max_bytes:
            break
    return bytes(body)
