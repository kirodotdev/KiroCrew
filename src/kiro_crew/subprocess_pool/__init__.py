"""Run syscall-shaped work in a separate interpreter, behind an Executor face.

See :mod:`kiro_crew.subprocess_pool.executor` for why a thread pool cannot isolate
work that is syscall-shaped but Python-paced, and for the three steps to point a
second pool at this base.
"""

from __future__ import annotations

from kiro_crew.subprocess_pool.executor import (
    OP_REALPATH_SPELLINGS,
    SubprocessPoolExecutor,
    SubprocessPoolUnavailable,
    pack_strings,
    unpack_strings,
)

__all__ = [
    "OP_REALPATH_SPELLINGS",
    "SubprocessPoolUnavailable",
    "SubprocessPoolExecutor",
    "pack_strings",
    "unpack_strings",
]
