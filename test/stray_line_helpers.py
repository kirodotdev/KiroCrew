"""Lines a child process may write that no line-oriented JSON reader can use.

One corpus for every reader's "a stray line costs that line" tests, so a new
bad-line shape is added once and every reader is held to it. Each entry is a
factory: the nested line probes the decoder depth, which is not free, so it is
built only when a test asks for it.
"""

from __future__ import annotations

import functools
import gc
import json
from typing import Callable

import pytest


@functools.cache
def too_deep_to_decode() -> str:
    """JSON text nested past THIS interpreter's DECODER ceiling.

    Probed rather than hardcoded: the ceiling depends on the interpreter's
    recursion limit and build, and the decoder's ceiling is higher than the
    encoder's, so text the encoder refuses is still ordinary input to
    ``json.loads``.
    """
    depth = 1000
    while depth <= 262144:
        text = "[" * depth + "]" * depth
        try:
            json.loads(text)
        except RecursionError:
            return text
        depth *= 2
    raise AssertionError("no nesting up to 2**18 is refused by json.loads")


def too_deep_line() -> bytes:
    """One newline-terminated line ``json.loads`` refuses with ``RecursionError``."""
    return too_deep_to_decode().encode("ascii") + b"\n"


def too_deep_result_line() -> bytes:
    """A response line whose ``result`` content is nested past the decoder."""
    return ('{"result": {"content": ' + too_deep_to_decode() + "}}\n").encode("ascii")


#: Every newline-terminated line that is not a usable JSON object. Each must be
#: dropped by a reader, never end its stream and never raise out of it.
STRAY_LINES: dict[str, Callable[[], bytes]] = {
    "not-json": lambda: b"not json\n",
    "undecodable": lambda: b"\x80\xff{}\n",
    "null": lambda: b"null\n",
    "scalar": lambda: b"42\n",
    "string": lambda: b'"a string"\n',
    "list": lambda: b"[1]\n",
    "truncated": lambda: b'{"unterminated": \n',
    "over-digit-limit": lambda: b'{"n": ' + b"9" * 5000 + b"}\n",
    "nested-past-the-decoder": too_deep_line,
}


@pytest.fixture
def cyclic_gc_quiesced():
    """Keep a cyclic-GC pass (and any finalizer it runs) out of a deep walk.

    A recursion-limit fixture drives a walker to the bottom of the stack; a
    collection firing there could run an inherited finalizer with no stack
    left. Drain first, disable for the walk, then restore and drain again.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()
