"""Minimal CBOR (RFC 8949) codec for the managed-environment service's ``rpcv2Cbor`` surface.

Stdlib only, so the ProxyCommand needs no installs.
Encodes dict/list/str/bytes/int/float/bool/None. Decodes the same plus tags
(the tag is dropped, the value kept: Smithy timestamps arrive as tag 1) and
indefinite-length strings, arrays and maps. Checked against RFC 8949
Appendix A vectors in the tests.
"""

from __future__ import annotations

import struct


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([major << 5 | info]) + struct.pack(fmt, n)
    raise ValueError("integer too large for CBOR")


def dumps(obj) -> bytes:
    if obj is False:
        return b"\xf4"
    if obj is True:
        return b"\xf5"
    if obj is None:
        return b"\xf6"
    if isinstance(obj, int):
        return _head(0, obj) if obj >= 0 else _head(1, -1 - obj)
    if isinstance(obj, float):
        return b"\xfb" + struct.pack(">d", obj)
    if isinstance(obj, bytes):
        return _head(2, len(obj)) + obj
    if isinstance(obj, str):
        raw = obj.encode()
        return _head(3, len(raw)) + raw
    if isinstance(obj, (list, tuple)):
        return _head(4, len(obj)) + b"".join(dumps(x) for x in obj)
    if isinstance(obj, dict):
        return _head(5, len(obj)) + b"".join(dumps(k) + dumps(v) for k, v in obj.items())
    raise TypeError(f"cannot CBOR-encode {type(obj).__name__}")


_BREAK = object()


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

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
            return None  # indefinite length
        size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
        if size is None:
            raise ValueError(f"bad CBOR additional info {info}")
        return int.from_bytes(self.take(size), "big")

    def item(self):
        ib = self.take(1)[0]
        major, info = ib >> 5, ib & 31
        if major == 7:
            return self._simple(info)
        n = self.arg(info)
        if n is None and major in (0, 1, 6):
            raise ValueError(f"CBOR major type {major} has no indefinite length")
        if major == 0:
            return n
        if major == 1:
            assert n is not None
            return -1 - n
        if major in (2, 3):
            if n is None:
                chunks = []
                while (c := self.item()) is not _BREAK:
                    chunks.append(c)
                return (b"" if major == 2 else "").join(chunks)
            raw = self.take(n)
            return raw if major == 2 else raw.decode()
        if major == 4:
            if n is None:
                out = []
                while (x := self.item()) is not _BREAK:
                    out.append(x)
                return out
            return [self.item() for _ in range(n)]
        if major == 5:
            pairs: dict = {}
            while n is None or len(pairs) < n:
                k = self.item()
                if k is _BREAK:
                    break
                pairs[k] = self.item()
            return pairs
        return self.item()  # major 6: tag -> keep the value

    def _simple(self, info: int):
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
        raise ValueError(f"unsupported CBOR simple value {info}")


def _half(h: int) -> float:
    exp, mant = (h >> 10) & 0x1F, h & 0x3FF
    if exp == 0:
        val = mant * 2.0**-24
    elif exp == 31:
        val = float("inf") if mant == 0 else float("nan")
    else:
        val = (mant + 1024) * 2.0 ** (exp - 25)
    return -val if h & 0x8000 else val


def loads(data: bytes):
    if not data:
        return {}
    r = _Reader(data)
    out = r.item()
    if r.pos != len(data):
        raise ValueError("trailing bytes after CBOR item")
    return out
