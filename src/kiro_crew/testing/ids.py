"""Deterministic ids, seeds and fake PIDs for tests: rules D3, D4 and D6.

``docs/system-specs/common/testing-conventions.md`` § Determinism contract
asks a test to generate ids whose string order is their creation order, to
seed every random generator that feeds an assertion, and to give a fake PID a
number no OS can allocate. The module imports only the standard library.

Import the members (``from kiro_crew.testing.ids import seq_ids``) rather than
the module: ``ids`` is also ``pytest.mark.parametrize``'s keyword.
"""

from __future__ import annotations

import hashlib
import random
import threading

__all__ = [
    "UNALLOCATABLE_PID",
    "SeqIds",
    "seed_for",
    "seeded_rng",
    "seq_ids",
    "unallocatable_pids",
]

#: A PID above ``2**32``, so above every ``pid_max``, and ``3`` modulo 4 even
#: after a ctypes DWORD truncation, so never a PID Windows hands out.
UNALLOCATABLE_PID = 99_999_999_999

#: How many distinct values :func:`unallocatable_pids` hands out.
_PID_BAND = 25


class SeqIds:
    """``prefix-0001``, ``prefix-0002``, ...: ids whose sorted order is creation order.

    Zero padding to ``width`` digits makes string order equal numeric order, so
    a list sorted by id is in creation order (D4, class 7). A number that no
    longer fits ``width`` raises ``OverflowError`` instead of silently breaking
    that order. ``next()`` and calling the instance are safe across threads.

    These ids repeat in every run and on every xdist worker, so never use one to
    name a resource the host shares (a lock file in the system temp directory,
    a port): name those after ``tmp_path`` or the worker id.
    """

    def __init__(self, prefix: str, *, start: int = 1, width: int = 4, sep: str = "-") -> None:
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise ValueError(f"width must be a positive int, got {width!r}")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError(f"start must be a non-negative int, got {start!r}")
        self._prefix = f"{prefix}{sep}"
        self._width = width
        self._next = start
        self._lock = threading.Lock()

    def __iter__(self) -> SeqIds:
        return self

    def __next__(self) -> str:
        with self._lock:
            number = self._next
            self._next += 1
        digits = str(number).zfill(self._width)
        if len(digits) > self._width:
            raise OverflowError(
                f"id number {number} does not fit width={self._width}; string order would "
                "no longer be creation order"
            )
        return self._prefix + digits

    __call__ = __next__


def seq_ids(prefix: str, *, start: int = 1, width: int = 4, sep: str = "-") -> SeqIds:
    """A :class:`SeqIds` generator; usable as an ``id_factory=`` seam (it is callable)."""
    return SeqIds(prefix, start=start, width=width, sep=sep)


def seed_for(nodeid: str) -> int:
    """A seed derived from a pytest node id, the same in every run and on every host.

    Taken from SHA-256, never ``hash()``, which changes with ``PYTHONHASHSEED``.
    pytest writes node ids with ``/`` on every OS, so the seed matches across
    platforms.
    """
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def seeded_rng(nodeid: str, *, seed: int | None = None) -> random.Random:
    """A ``random.Random`` seeded from ``nodeid`` (or from ``seed`` when given).

    CPython promises only that ``random()`` repeats across versions, so assert
    properties of what is drawn, never hard-coded drawn values. The rootdir
    conftest's ``seeded_rng`` fixture calls this with the running test's node
    id and prints the seed when the test fails.
    """
    return random.Random(seed_for(nodeid) if seed is None else seed)


def unallocatable_pids(n: int = 1) -> tuple[int, ...]:
    """``n`` distinct fake PIDs no OS can allocate, starting at :data:`UNALLOCATABLE_PID`.

    Each is above ``2**32`` and ``3`` modulo 4, and stays ``3`` modulo 4 after a
    32-bit truncation (ctypes ``DWORD``), while Windows hands out multiples of 4.
    On POSIX ``os.kill(pid, 0)`` raises ``OverflowError`` for these before any
    system call, not ``ProcessLookupError``: a subject that range-checks a PID
    first takes another branch, and needs a PID the test spawned and reaped.
    Never probe one with ``os.kill`` on Windows, where it terminates the target.
    """
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= _PID_BAND:
        raise ValueError(f"n must be an int from 1 to {_PID_BAND}, got {n!r}")
    return tuple(UNALLOCATABLE_PID - 4 * i for i in range(n))
