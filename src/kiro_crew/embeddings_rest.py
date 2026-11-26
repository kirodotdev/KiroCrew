"""Give an idle embedding model's weight pages back without unloading it.

The mapped GGUF weights are the largest part of a loaded model's memory (about
600 MiB resident for the shipped model). After a short idle period the embedder
asks the kernel to drop this process's copy of those pages (Linux). The model,
its context and the file mapping stay. A person showing up, or the next embed,
maps the pages back from the page cache in one pass on a side thread, so the
next embed reads them at its usual speed. The vectors are unchanged. The full
unload after ``memory.embedding_idle_evict_secs`` still follows.

Dropping the pages is safe at any time and from any thread: only read-only
mappings of the model file are advised, and their pages are clean copies of the
file, which the kernel may already drop and re-read under memory pressure.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.embeddings_llama import LlamaCppEmbedder

logger = logging.getLogger("kiro_crew.embeddings")

_MADV_WILLNEED = 3
_MADV_DONTNEED = 4
_MADV_POPULATE_READ = 22  # Linux 5.14+; an older kernel answers EINVAL


def _madvise() -> "object | None":
    """libc's ``madvise`` on Linux, else None."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        fn = ctypes.CDLL(None, use_errno=True).madvise
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn


def read_only_mappings(path: "str | os.PathLike[str]") -> "list[tuple[int, int]]":
    """Address ranges of this process's read-only mappings of *path* (Linux)."""
    if not sys.platform.startswith("linux"):
        return []
    target = os.path.realpath(path)
    ranges: list[tuple[int, int]] = []
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
            for line in maps:
                parts = line.rstrip("\n").split(maxsplit=5)
                if len(parts) < 6 or "w" in parts[1] or parts[5] != target:
                    continue
                lo, hi = (int(x, 16) for x in parts[0].split("-"))
                ranges.append((lo, hi))
    except (OSError, ValueError):
        return []
    return ranges


def _advise(ranges: "list[tuple[int, int]]", advice: int) -> bool:
    madvise = _madvise()
    if madvise is None:
        return False
    ok = True
    for lo, hi in ranges:
        if madvise(lo, hi - lo, advice) != 0:  # type: ignore[operator]
            ok = False
    return ok


def drop_weight_pages(path: "str | os.PathLike[str]") -> bool:
    """Drop this process's resident pages of *path*'s read-only mappings.

    False when the process maps no such page range (another platform, a model
    that is not a mapped file), so there is nothing to give back.
    """
    ranges = read_only_mappings(path)
    if not ranges:
        return False
    if not _advise(ranges, _MADV_DONTNEED):
        logger.debug("Could not drop the embedding model's weight pages")
    return True


def map_weight_pages(path: "str | os.PathLike[str]") -> bool:
    """Map *path*'s pages back; True only when every range was eagerly populated.

    False means there were no mappings or only best-effort readahead was requested.
    """
    ranges = read_only_mappings(path)
    if not ranges:
        return False
    if _advise(ranges, _MADV_POPULATE_READ):
        return True
    _advise(ranges, _MADV_WILLNEED)
    return False


def rest_if_idle(emb: "LlamaCppEmbedder", idle_secs: float) -> bool:
    """Give back *emb*'s weight pages when nothing used it for ``idle_secs``.

    Returns True if it did. The model stays loaded; :func:`wake_embedder` maps the
    pages back. Refused while work is queued, running or pacing, like
    ``evict_if_idle``, and done holding the inference lock so no embed is inside
    the model while its pages go. A child-process model is asked to do it itself.
    """
    llm = emb._llm
    if llm is None or emb._closed or emb._rested or emb._rest_unsupported:
        return False
    if time.monotonic() - emb._last_used < idle_secs or emb._jobs.qsize():
        return False
    if emb._inflight or time.monotonic() < emb._bulk_ready_at:
        return False
    if not emb._lock.acquire(blocking=False):
        return False  # inference running right now
    try:
        if llm is not emb._llm or emb._jobs.qsize() or emb._inflight:
            return False
        child_rest = getattr(llm, "rest", None)  # ChildLlama
        if callable(child_rest):
            rested = bool(child_rest())  # False while the child restarts: asked again later
        else:
            rested = drop_weight_pages(emb.model_path)
            # A model that maps no file pages here has nothing to give back.
            emb._rest_unsupported = not rested
        if not rested:
            return False
        emb._rested = True
    finally:
        emb._lock.release()
    logger.info(
        "Embedding model idle for %.0f s: gave back its weight pages; "
        "the next embed maps them in again",
        idle_secs,
    )
    return True


def wake_embedder(emb: "LlamaCppEmbedder") -> None:
    """Map a rested model's weight pages back in on a side thread; never waits."""
    if not emb._rested:
        return
    llm = emb._llm
    emb._rested = False
    if llm is None or emb._closed:
        return
    try:
        child_wake = getattr(llm, "wake", None)  # ChildLlama
        if callable(child_wake):
            child_wake()
        else:
            map_weight_pages_soon(emb.model_path)
    except Exception:  # noqa: BLE001 - the next embed maps the pages itself
        logger.debug("Waking the embedding model failed", exc_info=True)


def map_weight_pages_soon(path: "str | os.PathLike[str]") -> None:
    """:func:`map_weight_pages` on a daemon thread; returns at once.

    Safe beside a running decode: both only read the same clean file pages in.
    """
    threading.Thread(
        target=map_weight_pages, args=(os.fspath(path),), name="kc-wake-embed-pages", daemon=True
    ).start()
