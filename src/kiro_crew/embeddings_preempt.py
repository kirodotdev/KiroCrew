"""Stop a background embed early, between two steps of the model, so a person's embed runs.

The process runs one embedding model on one inference thread, and llama.cpp
offers no way to stop a ``llama_decode`` from Python once it has started. It does
offer an abort callback: the CPU backend calls it between the nodes of the
compute graph it is running and ends the decode with status 2 (aborted) when it
returns true. llama.cpp then removes the cache cells that decode wrote, and the
vendored ``Llama.embed`` clears the cache before every input, so the next embed
of the same text starts from nothing and returns the vector an uninterrupted
call returns, byte for byte.

The callback is a C function, never a Python one. llama.cpp calls it from its
compute thread at every graph node, and a Python callback would take the
interpreter lock each time, slowing every embed and every Python thread beside
it. ``strlen`` over a two-byte buffer is the predicate: the buffer is ``b"\\0\\0"``
(length 0, false) or ``b"\\1\\0"`` (length 1, true), and the platform calling
conventions return that length in the register a C ``bool`` is read from. No
other code runs on the compute thread and nothing takes a lock.

:class:`PreemptFlag` is the buffer. The inference worker arms it only while a
background job is inside the model and a person's job is waiting, so a person's
own embed is never stopped.
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger("kiro_crew.embeddings")


class PreemptFlag:
    """The byte llama.cpp's abort callback reads between graph nodes."""

    __slots__ = ("_buf", "_callback")

    def __init__(self, buf: "ctypes.Array[ctypes.c_char]", callback: object) -> None:
        self._buf = buf
        # Kept so the function-pointer object outlives every decode that may call it.
        self._callback = callback

    def arm(self) -> None:
        """Ask the decode now running to stop at its next graph node."""
        self._buf[0] = b"\x01"

    def clear(self) -> None:
        self._buf[0] = b"\x00"

    def fired(self) -> bool:
        return self._buf[0] != b"\x00"


def install_preempt_flag(llm: object) -> "PreemptFlag | None":
    """Give a loaded in-process model a :class:`PreemptFlag`; None when it cannot have one.

    Only a vendored ``Llama`` with a live native context qualifies: a child-process
    model, a test fake, or a runtime without ``llama_set_abort_callback`` or a C
    ``strlen`` keeps running every job to its end, as before. The flag is stored on
    the model (``kiro_crew_preempt``), so it lives exactly as long as the context
    whose callback points into it.
    """
    raw_ctx = getattr(getattr(llm, "_ctx", None), "ctx", None)
    if raw_ctx is None:
        return None
    try:
        from llama_cpp import llama_cpp as native  # vendored copy, already imported

        from kiro_crew.platform_compat import c_strlen_address
    except Exception:  # noqa: BLE001 - no binding, nothing to install
        return None
    setter = getattr(native, "llama_set_abort_callback", None)
    callback_type = getattr(native, "ggml_abort_callback", None)
    address = c_strlen_address()
    if not callable(setter) or callback_type is None or not address:
        return None
    try:
        buf = ctypes.create_string_buffer(2)
        flag = PreemptFlag(buf, callback_type(address))
        # Stored on the model BEFORE native code holds a pointer into the buffer,
        # so the buffer can never be freed while the context may still read it.
        llm.kiro_crew_preempt = flag  # type: ignore[attr-defined]
        setter(raw_ctx, flag._callback, ctypes.cast(buf, ctypes.c_void_p))
    except Exception:  # noqa: BLE001 - losing preemption must never lose the model
        logger.debug("Could not install the embedding abort callback", exc_info=True)
        return None
    return flag
