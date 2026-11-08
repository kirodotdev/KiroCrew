"""The growing tail of a sub-agent result file, read one poll at a time.

``api_session_agent_stream`` polls the result file once a second for up to
twenty minutes while a sub-agent appends to it. Reading the whole file on every
poll makes the stream's total work grow with the square of the file size, and
doing it on the event loop stalls every other session while it runs. This owner
keeps a byte offset instead, so each poll reads only the bytes appended since
the last one, and the caller runs :meth:`ResultTail.read_new` in a thread.

The decoded text matches what ``Path.read_text(encoding="utf-8")`` yields for the
same bytes: strict UTF-8, with ``\\r\\n`` and ``\\r`` translated to ``\\n``. The
incremental decoders hold back a multi-byte character or a ``\\r`` split across
two polls until the next poll completes it, so the text is never cut mid-way.

A file that shrinks below the offset (``write_result`` rewrites it in place) or
is replaced by a different file is read again from its start.
"""

from __future__ import annotations

import codecs
import io
import os
from pathlib import Path


def _decoder() -> io.IncrementalNewlineDecoder:
    return io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), translate=True)


class ResultTail:
    """The text appended to one result file since the previous read."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._decode = _decoder()

    def read_new(self) -> str:
        """Return the text appended since the last call; ``""`` when there is none.

        Blocking: call it through ``asyncio.to_thread``. A missing file reads as
        empty, so a stream opened before the sub-agent writes anything waits.
        """
        try:
            handle = open(self._path, "rb")
        except FileNotFoundError:
            return ""
        with handle:
            info = os.fstat(handle.fileno())
            identity = (info.st_dev, info.st_ino)
            if identity != self._identity or info.st_size < self._offset:
                self._identity = identity
                self._offset = 0
                self._decode = _decoder()
            handle.seek(self._offset)
            data = handle.read()
        self._offset += len(data)
        return self._decode.decode(data)

    def finish(self) -> str:
        """Return what the decoder still holds back, for the stream's last write.

        A ``\\r`` at the very end is held in case a ``\\n`` follows, so a stream
        that ends without this would drop the newline ``read_text`` gives. Reads
        no file. An incomplete UTF-8 sequence at the end is dropped rather than
        raised, since the stream's response is already on the wire.
        """
        try:
            return self._decode.decode(b"", final=True)
        except UnicodeDecodeError:
            return ""
