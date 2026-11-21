"""CBOR (RFC 8949), the subset Smithy ``rpcv2Cbor`` uses. No third-party codec ships here.

One codec for every Smithy RPC v2 CBOR client in the gateway: the Kiro Space listing
client (``apps/market_client.py``) and the Kiro calls an app makes through the
gateway (``apps/kiro_calls.py``).
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([(major << 5) | n])
    for info, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([(major << 5) | info]) + struct.pack(fmt, n)
    raise ValueError("integer too large for CBOR")


def cbor_encode(value: Any) -> bytes:
    """Encode None, bool, int, float, str, bytes, list/tuple and dict."""
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor_encode(k) + cbor_encode(v) for k, v in value.items()
        )
    raise TypeError(f"cannot CBOR-encode {type(value).__name__}")


_BREAK = object()
_MAX_DEPTH = 64
#: The CBOR tag Smithy puts on a timestamp: epoch seconds, an integer or a float.
_EPOCH_TAG = 1


class _Reader:
    def __init__(self, data: bytes, *, timestamps: bool = False) -> None:
        self.data = data
        self.pos = 0
        self.timestamps = timestamps

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("truncated CBOR")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def arg(self, info: int) -> int | None:
        if info < 24:
            return info
        if info == 31:
            return None
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise ValueError("invalid CBOR length")
        return int.from_bytes(self.take(sizes[info]), "big")

    def item(self, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("CBOR nested too deep")
        ib = self.take(1)[0]
        major, info = ib >> 5, ib & 0x1F
        if major == 7:
            return self._simple(info)
        n = self.arg(info)
        if major == 0:
            return self._definite(n)
        if major == 1:
            return -1 - self._definite(n)
        if major in (2, 3):
            if n is None:
                parts = []
                while (part := self.item(depth + 1)) is not _BREAK:
                    if not isinstance(part, bytes if major == 2 else str):
                        raise ValueError("bad CBOR string chunk")
                    parts.append(part)
                return (b"" if major == 2 else "").join(parts)
            raw = self.take(n)
            return raw if major == 2 else raw.decode("utf-8")
        if major == 4:
            out: list[Any] = []
            while n is None or len(out) < n:
                v = self.item(depth + 1)
                if v is _BREAK:
                    if n is not None:
                        raise ValueError("unexpected CBOR break")
                    break
                out.append(v)
            return out
        if major == 5:
            obj: dict[Any, Any] = {}
            count = 0
            while n is None or count < n:
                k = self.item(depth + 1)
                if k is _BREAK:
                    if n is not None:
                        raise ValueError("unexpected CBOR break")
                    break
                obj[k] = self.item(depth + 1)
                count += 1
            return obj
        # major 6: a tag. The tagged value is kept, except an epoch timestamp
        # when the caller asked for timestamps, which becomes a UTC datetime.
        tag = self._definite(n)
        value = self.item(depth + 1)
        if self.timestamps and tag == _EPOCH_TAG:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("bad CBOR timestamp")
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise ValueError("bad CBOR timestamp") from None
        return value

    @staticmethod
    def _definite(n: int | None) -> int:
        if n is None:
            raise ValueError("indefinite length not allowed here")
        return n

    def _simple(self, info: int) -> Any:
        if info == 20:
            return False
        if info == 21:
            return True
        if info in (22, 23):
            return None
        if info == 25:
            return _half(int.from_bytes(self.take(2), "big"))
        if info == 26:
            return struct.unpack(">f", self.take(4))[0]
        if info == 27:
            return struct.unpack(">d", self.take(8))[0]
        if info == 31:
            return _BREAK
        raise ValueError("unsupported CBOR simple value")


def _half(h: int) -> float:
    exp, frac = (h >> 10) & 0x1F, h & 0x3FF
    sign = -1.0 if h & 0x8000 else 1.0
    if exp == 0:
        return sign * frac * 2.0**-24
    if exp == 31:
        return sign * float("inf") if frac == 0 else float("nan")
    return sign * (1 + frac / 1024) * 2.0 ** (exp - 15)


def cbor_decode(data: bytes, *, timestamps: bool = False) -> Any:
    """Decode exactly one CBOR item; trailing bytes or truncation raise ValueError.

    ``timestamps`` turns a tag-1 epoch value into a UTC :class:`datetime`; without
    it the tag is dropped and the number kept.
    """
    reader = _Reader(data, timestamps=timestamps)
    value = reader.item()
    if value is _BREAK or reader.pos != len(data):
        raise ValueError("trailing CBOR bytes")
    return value


__all__ = ["cbor_decode", "cbor_encode"]
