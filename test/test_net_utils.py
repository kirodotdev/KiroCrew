import pytest

from kiro_crew import net_utils


class DummyStream:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.idx = 0

    async def read(self, n: int = -1) -> bytes:
        if self.idx < len(self.chunks):
            chunk = self.chunks[self.idx]
            if n > 0 and len(chunk) > n:
                # return only n bytes, keep the rest for next time
                ret = chunk[:n]
                self.chunks[self.idx] = chunk[n:]
                return ret
            self.idx += 1
            return chunk
        return b""


@pytest.mark.asyncio
async def test_read_bounded_success():
    stream = DummyStream([b"hello", b" ", b"world"])
    result = await net_utils.read_bounded(stream, max_bytes=100, chunk_size=1024)
    assert result == b"hello world"


@pytest.mark.asyncio
async def test_read_bounded_overflow():
    stream = DummyStream([b"12345", b"67890", b"extra bytes"])
    result = await net_utils.read_bounded(stream, max_bytes=10, chunk_size=1024)
    # the function should return exactly when it exceeds max_bytes
    # first read: 5 bytes
    # second read: 5 bytes, total 10. len > 10 is False.
    # third read: 1 byte, total 11.
    assert len(result) > 10
    assert result == b"1234567890e"
