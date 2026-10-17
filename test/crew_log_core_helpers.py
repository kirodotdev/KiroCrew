"""Shared fixtures and helpers for the ``test_crew_log_core*.py`` suites.

``_isolated_home`` is autouse: each suite binds it by name so pytest registers it for
that module, and after every test it asserts that no write lease outlived the test.
``_raises`` asserts a refusal without retaining the exception (its docstring says why).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, CrewLogError, lease

CREW = "qa"
SESSION = "s-7f3a"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_crew_log_core.py and the suites split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield
    # The write lease is released when the handle that adopted it is dropped, and
    # the holder table is process-global, so a handle a test here keeps reachable
    # is a lease the next test on this worker -- in whatever file xdist hands it
    # -- reads as held. Read WITHOUT a ``gc.collect()``: release rides the handle's
    # refcount, so a lease still held here is a retention (a reference cycle
    # through the handle, typically a caught exception's traceback holding the
    # ``append`` frame whose ``self`` is the handle), not a frame that has not
    # finished unwinding. Reported here, where it is created, rather than in
    # ``test_crew_log_edge_exhaustion.py``'s teardown, which is where the fourth
    # Windows sweep first read it.
    assert not lease._held, f"a lease outlived its test on this worker: {sorted(lease._held)}"


def _crew(unit_id: str = CREW, **fields) -> CrewLog:
    return CrewLog.create(lg.KIND_CREW, unit_id, **fields)


def _session(unit_id: str = SESSION, **fields) -> CrewLog:
    fields.setdefault("owner", CREW)
    fields.setdefault("agent", "kirocrew")
    return CrewLog.create(lg.KIND_SESSION, unit_id, **fields)


class _Refusal:
    """What a refusal SAID, copied out of the exception at the moment it is caught.

    A plain record with the fields these tests read -- ``code``, ``field``,
    ``message``, ``written`` -- and ``str()`` as the exception's own. Deliberately
    NOT the exception: see :func:`_raises`.
    """

    __slots__ = ("code", "field", "message", "written")

    def __init__(self, exc: BaseException) -> None:
        self.code = getattr(exc, "code", None)
        self.field = getattr(exc, "field", None)
        self.message = str(exc)
        self.written = getattr(exc, "written", None)

    def __str__(self) -> str:
        return self.message


class _raises:
    """Assert the block raises *exc_type* -- with *code*, when one is given -- and RETAIN NOTHING.

    ``pytest.raises(...) as exc`` binds an ``ExceptionInfo`` in the test's frame,
    and that object holds the exception, whose traceback holds every frame it
    unwound through -- the test's own frame (a reference cycle only the cyclic
    collector breaks) and ``CrewLog.append``'s frame, whose ``self`` is the handle.
    The handle's write lease is released when the handle is dropped, so with the
    cycle standing the lease outlived the test by however many tests it took the
    collector to get round to it: the fourth Windows sweep read these tests' leases
    from ``test_crew_log_edge_exhaustion.py``'s teardown, 3 of 3 rounds.

    A CLASS, not a ``@contextlib.contextmanager``: an exception thrown into a
    generator is a second cycle of the same kind (the traceback grows the
    generator's frame, whose ``f_back`` is ``__exit__``'s frame, whose ``value``
    is the exception), measured here with ``gc.get_referrers`` -- 26 of the
    suite's tests still held their lease under that shape. Here ``__exit__`` reads
    the exception's fields into a :class:`_Refusal`, returns, and its frame is
    the only thing that ever named the exception; nothing outlives the ``with``
    but strings, so the handle -- and the lease -- go with the test's frame, by
    refcount. The autouse fixture's ``lease._held`` pin is what proves it, test
    by test.
    """

    def __init__(self, code: str | None, exc_type: type[BaseException] = CrewLogError) -> None:
        self.code = code
        self.exc_type = exc_type
        self.value: _Refusal | None = None

    def __enter__(self) -> "_raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            pytest.fail(f"expected {self.exc_type.__name__} {self.code!r}, nothing was raised")
        if not isinstance(exc, self.exc_type):
            return False
        self.value = _Refusal(exc)
        if self.code is not None and self.value.code != self.code:
            raise AssertionError(
                f"refused with code {self.value.code!r}, expected {self.code!r}: {self.value}"
            ) from None
        return True


def _code(caught: _raises) -> str:
    assert caught.value is not None
    return caught.value.code


def _log_bytes(kind: str = lg.KIND_SESSION, unit_id: str = SESSION) -> bytes:
    """The file exactly as it sits on disk, for asserting nothing was rewritten."""
    return lg.crew_log_path(kind, unit_id).read_bytes()


def _split_into_segments(kind: str, unit_id: str, at_seq: int) -> None:
    """Move every entry from *at_seq* onward into ``log.<at_seq>.jsonl``.

    Stands in for a writer that rotates, which nothing does yet: the reader side
    is what this commit freezes, so the fixture produces the on-disk shape a
    future rotation would leave.
    """
    head = lg.crew_log_path(kind, unit_id)
    lines = head.read_bytes().splitlines(keepends=True)
    header, entries = lines[0], lines[1:]
    kept, moved = [], []
    for raw in entries:
        (moved if json.loads(raw)["seq"] >= at_seq else kept).append(raw)
    head.write_bytes(header + b"".join(kept))
    (head.parent / f"log.{at_seq}.jsonl").write_bytes(header + b"".join(moved))
