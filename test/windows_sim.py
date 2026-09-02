"""Windows-condition simulators for POSIX dev machines.

KiroCrew is developed on macOS/Linux but must pass the ``Backend Tests
(Windows)`` matrix. Several classes of Windows-only behaviour never occur on
POSIX, so the buggy code path is never exercised by a local ``pytest`` run and
the failure only shows up in CI:

* **Coarse system clock (~15 ms tick).** ``datetime.now()`` on Windows advances
  in ~15.6 ms steps, so a burst of rapid appends gets an *identical* ``ts``.
  POSIX gives microsecond resolution, so the collision (and the bugs it exposes
  — history-dedup duplication, ambiguous cross-session sort) never reproduces
  locally.
* **File-sharing semantics.** Reading a file another process holds open for
  write raises a sharing violation (``PermissionError`` / ``WinError 32``), and
  ``os.replace()`` over an open handle fails likewise. POSIX permits both.

These context managers reproduce those conditions **deterministically on any
OS**, so the *logic* of Windows-relevant code can be unit-tested locally. They
do NOT reproduce the raw OS behaviour end-to-end (that still needs a real
Windows host) — they let a POSIX test drive the exact code path a Windows
machine would take.

Guidance:
    Any code that touches timestamps, cross-file/-process ordering, file locking
    or atomic replacement should get a test that wraps it in the matching
    simulator below, so a regression is caught on the Mac/Linux dev loop instead
    of a CI round-trip.

    from windows_sim import colliding_clock, read_sharing_violation

    def test_no_dupe_under_colliding_ts(tmp_path):
        with colliding_clock("kiro_crew.history"):
            ...  # every datetime.now() in kiro_crew.history returns ONE instant

Extending: keep each simulator small, name the real Windows behaviour it mimics
in its docstring, and add a self-test in ``test_windows_sim.py``.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
from contextlib import contextmanager
from typing import Iterator, Optional
from unittest import mock

__all__ = [
    "colliding_clock",
    "increasing_clock",
    "read_sharing_violation",
    "builtin_open_sharing_violation",
    "replace_sharing_violation",
    "open_sharing_violation",
    "unlink_sharing_violation",
    "nonatomic_write",
    "windows_text_mode_write",
]

_DEFAULT_INSTANT = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)


def _fixed_datetime_class(value: _dt.datetime) -> type:
    """A ``datetime`` subclass whose ``now()`` always returns *value*.

    Subclassing the real ``datetime`` (rather than a bare stub) keeps every other
    classmethod — ``fromisoformat``, ``strptime``, ``fromtimestamp``, … — working
    for code under test that uses them alongside ``now()``.
    """

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return value.astimezone(tz) if tz is not None else value

    return _FixedDatetime


def _increasing_datetime_class(start: _dt.datetime, step_seconds: float) -> type:
    """A ``datetime`` subclass whose ``now()`` advances by *step_seconds* on every
    call (strictly increasing, deterministic ordering)."""
    state = {"n": 0}

    class _IncreasingDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            state["n"] += 1
            value = start + _dt.timedelta(seconds=step_seconds * state["n"])
            return value.astimezone(tz) if tz is not None else value

    return _IncreasingDatetime


@contextmanager
def colliding_clock(
    module_path: str, *, at: Optional[_dt.datetime] = None
) -> Iterator[_dt.datetime]:
    """Freeze ``<module_path>.datetime.now()`` to a single instant.

    Mimics a burst of operations completing within one coarse Windows clock tick,
    so every ``datetime.now().isoformat()`` stamp COLLIDES — the condition behind
    the history colliding-timestamp bugs. *module_path* is the dotted module that
    does ``from datetime import datetime`` (e.g. ``"kiro_crew.history"``).

    Yields the fixed instant.
    """
    value = at if at is not None else _DEFAULT_INSTANT
    with mock.patch(f"{module_path}.datetime", _fixed_datetime_class(value)):
        yield value


@contextmanager
def increasing_clock(
    module_path: str,
    *,
    start: Optional[_dt.datetime] = None,
    step_seconds: float = 1.0,
) -> Iterator[None]:
    """Make ``<module_path>.datetime.now()`` strictly increasing.

    The opposite extreme of :func:`colliding_clock`: guarantees distinct, ordered
    timestamps regardless of how fast the calls happen, so a test that asserts a
    time-ordered result is deterministic on every OS (a coarse Windows clock would
    otherwise collide the stamps and leak the underlying merge order).
    """
    start = start if start is not None else _DEFAULT_INSTANT
    with mock.patch(
        f"{module_path}.datetime", _increasing_datetime_class(start, step_seconds)
    ):
        yield


@contextmanager
def read_sharing_violation(
    *, match: Optional[str] = None, times: int = 1
) -> Iterator[dict]:
    """Make ``Path.read_bytes()`` raise a Windows-style sharing violation.

    On Windows, reading a file another process holds open for write raises
    ``PermissionError`` (``WinError 32``). POSIX permits the read, so code that
    only guards ``FileNotFoundError`` (and treats every other ``OSError`` as
    fatal) misbehaves only on Windows. This raises ``PermissionError`` for the
    first *times* matching reads, then delegates to the real call.

    *match*: if given, only paths whose name equals it OR whose string contains it
    fault; otherwise every ``read_bytes`` faults. Yields a ``{"n": count}`` dict
    of how many reads were intercepted (faulted or passed the match), useful for
    asserting a retry happened.
    """
    real_read_bytes = pathlib.Path.read_bytes
    state = {"n": 0}

    def _patched(self: pathlib.Path):  # type: ignore[no-untyped-def]
        if match is None or self.name == match or match in str(self):
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(
                    f"[WinError 32] simulated sharing violation reading {self}"
                )
        return real_read_bytes(self)

    with mock.patch.object(pathlib.Path, "read_bytes", _patched):
        yield state


@contextmanager
def replace_sharing_violation(
    *, match: Optional[str] = None, times: int = 1
) -> Iterator[dict]:
    """Make ``os.replace()`` raise a Windows-style sharing violation.

    On Windows an atomic ``os.replace(src, dst)`` fails with ``PermissionError``
    (``WinError 32``) if *dst* is currently open by another handle — POSIX allows
    replacing an open file. Raises for the first *times* matching calls, then
    delegates. *match* filters on either path (name-equality or substring).
    Yields a ``{"n": count}`` dict.
    """
    real_replace = os.replace
    state = {"n": 0}

    def _patched(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if (
            match is None
            or os.path.basename(str(dst)) == match
            or match in str(dst)
            or match in str(src)
        ):
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(
                    f"[WinError 32] simulated sharing violation replacing {dst}"
                )
        return real_replace(src, dst, *args, **kwargs)

    with mock.patch("os.replace", _patched):
        yield state


@contextmanager
def builtin_open_sharing_violation(
    *, match: Optional[str] = None, times: int = 1
) -> Iterator[dict]:
    """Make the builtin ``open()`` raise a Windows-style sharing violation.

    The counterpart to :func:`read_sharing_violation` (which covers
    ``Path.read_bytes``) for the very common case of code that reads through
    ``open(path)`` directly -- e.g. a streaming ``readline()`` that must not slurp
    a large file. Note that patching ``os.open`` does NOT reach either of those:
    CPython's ``builtins.open`` and ``pathlib`` read paths go through the C
    ``_io`` layer and never call the ``os.open`` Python attribute, so
    :func:`open_sharing_violation` only intercepts explicit ``os.open`` callers.

    Raises ``PermissionError`` for the first *times* matching opens, then
    delegates to the real call -- so a caller that retries is expected to
    succeed. *match* filters on the path (basename-equality or substring);
    ``None`` faults every open. Yields a ``{"n": count}`` dict of how many
    matching opens were seen, useful for asserting a retry happened.
    """
    real_open = open
    state = {"n": 0}

    def _patched(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        p = str(file)
        if match is None or os.path.basename(p) == match or match in p:
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(
                    f"[WinError 32] simulated sharing violation opening {p}"
                )
        return real_open(file, *args, **kwargs)

    with mock.patch("builtins.open", _patched):
        yield state


@contextmanager
def open_sharing_violation(
    *, match: Optional[str] = None, times: int = 1, create_only: bool = True
) -> Iterator[dict]:
    """Make ``os.open()`` raise a Windows-style sharing violation.

    On Windows, opening a file another process holds open can raise
    ``PermissionError`` (``WinError 32``) — notably an exclusive create
    (``O_CREAT | O_EXCL``) racing a writer that still holds the just-created
    file open. Raises for the first *times* matching ``os.open`` calls, then
    delegates.

    *match* filters on the path (basename-equality or substring); ``None`` faults
    every ``os.open``. *create_only* (default ``True``) restricts faults to opens
    that include ``os.O_CREAT``, so only the exclusive create is exercised.

    Reaches ONLY code that calls ``os.open`` itself (e.g. ``atomic_write``'s
    ``O_CREAT | O_EXCL``). It does NOT intercept the builtin ``open()`` or the
    ``pathlib`` read helpers: those go through the C ``_io`` layer and never
    consult the ``os.open`` Python attribute. Use
    :func:`builtin_open_sharing_violation` or :func:`read_sharing_violation` for
    those. Yields a ``{"n": count}`` dict.
    """
    real_open = os.open
    state = {"n": 0}

    def _patched(path, flags, mode=0o777, *args, **kwargs):  # type: ignore[no-untyped-def]
        p = str(path)
        name_ok = match is None or os.path.basename(p) == match or match in p
        create_ok = (not create_only) or bool(flags & os.O_CREAT)
        if name_ok and create_ok:
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(
                    f"[WinError 32] simulated sharing violation opening {p}"
                )
        return real_open(path, flags, mode, *args, **kwargs)

    with mock.patch("os.open", _patched):
        yield state


@contextmanager
def unlink_sharing_violation(
    *, match: Optional[str] = None, times: int = 1
) -> Iterator[dict]:
    """Make deleting a file raise a Windows-style sharing violation.

    On Windows a file cannot be deleted while another handle holds it open
    WITHOUT ``FILE_SHARE_DELETE`` — which Python's ``open`` / ``os.open`` do NOT
    grant. ``os.unlink`` (and therefore ``Path.unlink``, which routes through it)
    raises ``PermissionError`` (``WinError 32``). POSIX permits deleting an open
    file, so an un-retried delete that races a concurrent reader (e.g. the
    credential poller mid-digest-read) passes locally yet fails on Windows.
    Raises for the first *times* matching deletes, then delegates to the real
    call — so a bounded retry loop (what an external credential deleter/rotator
    does) succeeds. *match* filters on the path (basename-equality or substring);
    ``None`` faults every delete. Yields a ``{"n": count}`` dict.

    Patches BOTH ``pathlib.Path.unlink`` (the method object itself) AND
    ``os.unlink``, sharing one counter. Patching ``os.unlink`` alone is NOT
    enough on every Python: on 3.10/3.11 ``Path.unlink`` calls
    ``self._accessor.unlink``, which binds ``os.unlink`` at class-definition time
    (before the patch), so a later ``mock.patch("os.unlink")`` never intercepts
    it. Replacing the ``Path.unlink`` method faults regardless of that internal
    routing on 3.10–3.12; the ``Path`` path deletes via the captured real
    ``os.unlink`` on the non-fault branch (bypassing the accessor), so the two
    entry points never nest and never double-count. ``os.remove`` is a distinct
    alias object and is left untouched — credential deletion uses ``unlink``.
    """
    real_unlink = os.unlink
    state = {"n": 0}

    def _maybe_fault(p: str) -> None:
        if match is None or os.path.basename(p) == match or match in p:
            state["n"] += 1
            if state["n"] <= times:
                raise PermissionError(
                    f"[WinError 32] simulated sharing violation deleting {p}"
                )

    def _patched_os_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        _maybe_fault(str(path))
        return real_unlink(path, *args, **kwargs)

    def _patched_path_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Fault-check, then delete via the REAL os.unlink directly — not through
        # real Path.unlink — so this never re-enters the os.unlink patch (no
        # double-count) and works identically across the 3.10 accessor binding
        # and the 3.12 dynamic os.unlink lookup.
        _maybe_fault(str(self))
        return real_unlink(self)

    with mock.patch("os.unlink", _patched_os_unlink), mock.patch.object(
        pathlib.Path, "unlink", _patched_path_unlink
    ):
        yield state


@contextmanager
def nonatomic_write(cred: pathlib.Path, data: bytes) -> Iterator[None]:
    """Reproduce a NON-ATOMIC ``write_bytes`` truncate window deterministically.

    ``Path.write_bytes`` opens the file in ``"wb"`` mode, which TRUNCATES it to
    zero bytes before the payload is written. A concurrent reader (e.g. the
    credential poller, hashing every ~10 ms in a worker thread) can observe that
    transient EMPTY/partial state as a distinct content revision — so an
    appearance/rotation done with a bare ``write_bytes`` fires the watcher an
    EXTRA, spurious time. On POSIX the truncate window is sub-microsecond and the
    poll almost never lands inside it, so the bug surfaces only on the (slower,
    under-load) native Windows matrix.

    This models that window as an EXPLICIT, awaitable two-phase sequence: on
    ENTER the file is left EMPTY (the truncate phase); the payload is written on
    EXIT (the completion phase). ``await``/sleep inside the ``with`` block to let
    a concurrent watcher observe the empty phase deterministically on any OS::

        with nonatomic_write(cred, b"secret-v1"):
            await asyncio.sleep(0.05)   # watcher observes the empty truncate window
        await asyncio.sleep(0.05)       # watcher observes the full payload

    Unlike the sharing-violation simulators this is NOT a ``mock.patch`` gate —
    the hazard is a concurrency INTERLEAVING, not an API raising, and a blocking
    patch inside the write call would freeze the event loop and starve the
    watcher. Driving the two phases around real ``await`` points is what makes it
    deterministic. A real refresh daemon writes atomically (temp + ``os.replace``),
    so the fix under test collapses the whole sequence into a single
    absent→present transition and the extra fire disappears.
    """
    cred.write_bytes(b"")  # truncate phase — file exists but is empty
    try:
        yield
    finally:
        cred.write_bytes(data)  # completion phase — full payload lands


# Real Windows uses 0x8000 for os.O_BINARY; reuse it so the simulated flag value
# matches a real Windows host. It is high enough not to collide with the small
# O_WRONLY/O_CREAT/O_EXCL flags the code under test ORs together.
_SIM_O_BINARY = 0x8000


@contextmanager
def windows_text_mode_write(*, match: Optional[str] = None) -> Iterator[dict]:
    """Mimic Windows ``os.open()`` defaulting to TEXT mode.

    On Windows a descriptor from ``os.open()`` WITHOUT ``os.O_BINARY`` is in
    text mode, so ``os.write()`` of bytes containing ``0x0A`` ('\\n') emits
    ``0x0D 0x0A`` ('\\r\\n') to disk — silently corrupting binary payloads
    (e.g. a random signing key). POSIX has no text mode and ``os.O_BINARY`` is
    ``0``, so the corruption never reproduces locally: a binary writer that
    forgets ``os.O_BINARY`` passes on Mac/Linux yet fails on Windows.

    This installs a NON-ZERO synthetic ``os.O_BINARY`` and CRLF-translates
    ``os.write`` for any fd opened WITHOUT that bit. To behave IDENTICALLY on
    POSIX and on a real Windows host, every underlying fd is opened GENUINELY
    BINARY: the real ``os.O_BINARY`` (captured before the patch below shadows
    it) is always OR'd back into the real ``os.open`` flags, so the synthetic
    translation here is the ONLY translation on any OS. Without this, on Windows
    the CRT would translate too — double-translating the text case and breaking
    the binary case — because the real flag and the synthetic sentinel share the
    value ``0x8000``.

    Code that ORs ``getattr(os, "O_BINARY", 0)`` into its open flags is
    exercised as binary (no translation); code that omits it gets the Windows
    corruption. ``os.write`` still returns the INPUT byte count (as Windows
    does), so a caller's short-write loop stays correct.

    *match* filters which paths are tracked (basename-equality or substring);
    ``None`` tracks every ``os.open``. Reads are left untouched
    (``Path.read_bytes`` is binary on Windows too). Yields
    ``{"translated": extra_bytes_written}``.
    """
    real_open = os.open
    real_write = os.write
    real_close = os.close
    # Capture the REAL os.O_BINARY BEFORE the mock.patch below shadows it with
    # the synthetic sentinel. POSIX -> 0 (no-op); Windows -> 0x8000, which MUST
    # be re-applied to every underlying fd so the CRT does not add its own
    # translation on top of the synthetic one.
    real_o_binary = getattr(os, "O_BINARY", 0)
    opened_binary: dict[int, bool] = {}
    state = {"translated": 0}

    def _match(p: str) -> bool:
        return match is None or os.path.basename(p) == match or match in p

    def _open(path, flags, mode=0o777, *args, **kwargs):  # type: ignore[no-untyped-def]
        want_binary = bool(flags & _SIM_O_BINARY)
        # Strip the synthetic bit but FORCE the underlying fd genuinely binary
        # (real_o_binary), so our synthetic translation is the only one on every
        # OS — including Windows, where the sentinel value IS the real flag.
        fd = real_open(
            path, (flags & ~_SIM_O_BINARY) | real_o_binary, mode, *args, **kwargs
        )
        if _match(str(path)):
            opened_binary[fd] = want_binary
        return fd

    def _write(fd, data):  # type: ignore[no-untyped-def]
        # Only text-mode (non-binary) tracked fds translate; everything else is
        # a straight passthrough.
        if fd in opened_binary and not opened_binary[fd]:
            raw = bytes(data)
            translated = raw.replace(b"\n", b"\r\n")
            # Write the whole translated buffer, tolerating short writes, then
            # report the INPUT byte count consumed (as Windows text mode does).
            mv = memoryview(translated)
            while mv:
                k = real_write(fd, mv)
                if k <= 0:
                    break
                mv = mv[k:]
            state["translated"] += len(translated) - len(raw)
            return len(raw)
        return real_write(fd, data)

    def _close(fd):  # type: ignore[no-untyped-def]
        opened_binary.pop(fd, None)
        return real_close(fd)

    with mock.patch("os.open", _open), mock.patch("os.write", _write), mock.patch(
        "os.close", _close
    ), mock.patch.object(os, "O_BINARY", _SIM_O_BINARY, create=True):
        yield state
