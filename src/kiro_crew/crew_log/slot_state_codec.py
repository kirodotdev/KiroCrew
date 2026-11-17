"""A slot fold's state as its savepoint file stores it: plain JSON, or compressed when large.

The kernel store refuses any savepoint file over ``MAX_PAYLOAD_BYTES`` (2 MiB), on write
and on read, and that one number must stay the same for both: a file written under a
larger cap is ignored by a build with the smaller one, and the slot then resumes from an
older, staler savepoint or folds cold. But a fold whose state is bounded can still
serialize past 2 MiB -- a busy board's ``work`` state is about 2.4 MB of JSON -- and such
a cell had no savepoint at all, so every restart re-read its unit's tail since the last
file that did fit.

So the state is stored compressed once its JSON passes half the cap, and the cap itself
does not move. The JSON string is compressed exactly as encoded, so the key order the
state's meaning depends on (``checkpoint._SLOT_STATE_JSON``) is kept. A build that
predates the compressed key finds no ``state_json`` in the payload, refuses the file,
and falls back to an older savepoint or a cold fold, which is slower and never wrong.
"""

from __future__ import annotations

import base64
import binascii
import zlib
from typing import Any, Final

from kiro_crew.projection import MAX_PAYLOAD_BYTES

#: The payload key holding the state as a plain JSON string.
STATE_JSON: Final[str] = "state_json"

#: The payload key holding the same JSON string, zlib-compressed and base64-encoded.
STATE_ZLIB: Final[str] = "state_zlib"

#: Encoded state above this many bytes is stored compressed. Half the cap, so the
#: identity block, the witness and the kernel's own escaping of the string fit beside a
#: plain state, and a compressed one has the same room.
COMPRESS_ABOVE_BYTES: Final[int] = MAX_PAYLOAD_BYTES // 2

#: Most bytes a compressed state may expand to on read. A bound on what one file can make
#: this process allocate, so a crafted payload that inflates without limit is refused
#: instead of decoded. Far above any bounded fold's state.
MAX_DECODED_BYTES: Final[int] = 32 * MAX_PAYLOAD_BYTES


def encode_state(encoded: str) -> "dict[str, str]":
    """The payload ``state`` mapping for a state already encoded as *encoded* JSON."""
    # ``surrogatepass``: the JSON is built with ``ensure_ascii=False``, so a lone
    # surrogate a writer accepted stays in it, and a strict encode would raise out of
    # the savepoint writer. Decoded the same way, so the string round-trips exactly.
    raw = encoded.encode("utf-8", "surrogatepass")
    if len(raw) <= COMPRESS_ABOVE_BYTES:
        return {STATE_JSON: encoded}
    return {STATE_ZLIB: base64.b64encode(zlib.compress(raw, 6)).decode("ascii")}


def decode_state(payload: Any) -> "str | None":
    """The JSON string inside a payload ``state`` mapping, or ``None`` if there is none.

    ``None`` for a payload with neither key, a value of the wrong type, damaged
    base64 or zlib data, a stream that does not end, and one that inflates past
    :data:`MAX_DECODED_BYTES`. Every one is the same answer as an absent file.
    """
    if not isinstance(payload, dict):
        return None
    plain = payload.get(STATE_JSON)
    if plain is not None:
        return plain if isinstance(plain, str) else None
    packed = payload.get(STATE_ZLIB)
    if not isinstance(packed, str):
        return None
    try:
        data = base64.b64decode(packed.encode("ascii"), validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(data, MAX_DECODED_BYTES)
        if inflater.unconsumed_tail or not inflater.eof:
            return None
        return raw.decode("utf-8", "surrogatepass")
    except (binascii.Error, ValueError, zlib.error, UnicodeError):
        return None
