"""The lock over each read-modify-write of ``migrated.json``, the Bot-move ledger.

The startup move and repair write the ledger on a worker thread, and two
gateways with their own data homes can share one Kiro home, so each repairs its
own Bots against the same file. An
unlocked pair of writers lets one write erase the other's, and an erased
``held_rules`` entry lets a Bot's held calls run without a person. The lock is a
thread lock plus a sidecar file lock, taken in that order; a nested transaction
in the same thread reuses both.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

from kiro_crew import platform_compat
from kiro_crew.config import paths as paths_mod

_THREAD_LOCK = threading.RLock()
#: How deep the thread holding :data:`_THREAD_LOCK` is nested; only that thread reads it.
_depth = 0

_F = TypeVar("_F", bound=Callable[..., Any])


@contextmanager
def ledger_lock() -> Iterator[None]:
    """Hold the ledger for one read-modify-write. Raises ``OSError`` past the file lock's ceiling."""
    global _depth
    with _THREAD_LOCK:
        if _depth:
            _depth += 1
            try:
                yield
            finally:
                _depth -= 1
            return
        path = paths_mod.mate_agents_dir().parent / "migrated.json.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with platform_compat.open_lock_file(path) as fd, platform_compat.flock_exclusive(fd):
            _depth = 1
            try:
                yield
            finally:
                _depth = 0


def ledger_transaction(fn: _F) -> _F:
    """Run *fn*, a read-modify-write of the ledger, under :func:`ledger_lock`."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        with ledger_lock():
            return fn(*args, **kwargs)

    return run  # type: ignore[return-value]
