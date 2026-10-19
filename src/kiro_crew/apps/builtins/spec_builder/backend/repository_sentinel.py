"""Spec Builder's STOP sentinel and the handoff gate that arms it.

A Pause leaves a ``STOP`` file in the spec directory; the handoff gate clears it when a
run is armed, after re-checking the spec's identity under the index lock, and reports
whether ``tasks.md`` still holds an open task. Every write and unlink is pinned to a
verified directory descriptor. From the rest of the backend this module imports only
:mod:`repository` and :mod:`parsers`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .parsers import _has_open_task, _redact
from .repository import (
    _CAN_PIN_DIR,
    _INDEX_LOCK,
    _STOP_FILE,
    _load_index,
    _read_spec_text,
    _spec_file,
    _verified_spec_dir,
    logger,
)


def _write_stop_sentinel(spec_dir: Path) -> bool:
    """Write the STOP sentinel atomically, never following a symlink.

    The directory is PINNED with a non-following descriptor and both the temp
    create and the rename happen relative to it. Verifying the directory and then
    operating on PATHS left the same window the clear path had: an agent that
    swaps its verified directory for a symlink between the check and the open
    redirects both steps, so ANOTHER active spec receives the STOP file and halts.
    Pinning means the sentinel lands in the directory that was verified or not at
    all.

    ``os.replace`` swaps the link itself rather than writing through it, so a
    planted ``STOP`` symlink is destroyed instead of honoured.

    POSIX-only pinning (see ``_CAN_PIN_DIR``): Windows has neither ``O_NOFOLLOW``
    nor ``dir_fd``, so there it keeps the path-based form.

    BLOCKING -- call via ``asyncio.to_thread``. Every step touches the
    filesystem (``realpath``, ``is_sensitive_path``, ``open``, ``write``,
    ``close``, ``replace``), so on unresponsive storage this stalls whatever
    thread runs it; on the event loop that is the whole gateway.
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return False
    tmp_name = f".{_STOP_FILE}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if _CAN_PIN_DIR:
        try:
            dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, str(time.time()).encode())
            finally:
                os.close(fd)
            os.replace(tmp_name, _STOP_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return True
        except OSError:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass  # nothing to clean up
            return False
        finally:
            os.close(dir_fd)
    # No pinning available (Windows): FAIL CLOSED rather than writing by path. The
    # agent runs inside the user's project and can swap this directory for a junction
    # between the verification above and the write, and a path-based write then lands
    # the STOP file in whatever the replacement points at -- halting an unrelated
    # spec's run. Pause does not depend on this file: removing the nudge loop and
    # cancelling the in-flight turn are the authoritative stops, and both are
    # in-process. The sentinel only guards a FUTURE nudge, so its absence narrows
    # defense in depth instead of leaving the run going.
    logger.warning(
        "stop sentinel unavailable on this platform (no dir_fd pinning); "
        "relying on loop removal and turn cancellation to halt %s",
        _redact(str(real_dir)),
    )
    return False


def _clear_stop_sentinel(spec_dir: Path) -> None:
    """Remove a stale STOP sentinel belonging to THIS spec.

    Refuses a spec_dir that does not resolve to itself (see
    ``_verified_spec_dir``). Verification alone is not enough: between the check
    and the ``unlink`` the agent this app runs can replace the verified directory
    with a symlink, and a path-based unlink then resolves through the replacement
    and deletes a STOP file outside the spec. The directory is therefore PINNED
    with a non-following descriptor and the unlink is relative to it, so the
    delete lands in the directory that was verified or not at all.

    POSIX-only pinning: where ``dir_fd`` is unavailable (Windows) this does
    NOTHING and logs, because a path-based unlink can be redirected into another
    spec by a directory swapped under it.

    BLOCKING -- call via ``asyncio.to_thread`` (see ``_arm_stop_sentinel``).
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return
    if _CAN_PIN_DIR:
        try:
            dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return
        try:
            os.unlink(_STOP_FILE, dir_fd=dir_fd)
        except OSError:
            pass  # absent, or a directory in its place — nothing to clear
        finally:
            os.close(dir_fd)
        return
    # Same reasoning as _write_stop_sentinel: without pinning, a path-based unlink can
    # be redirected by a directory swapped underneath it, deleting another spec's STOP
    # file and letting THAT run resume. A stale sentinel of our own is the lesser
    # failure -- it makes this spec refuse to start until it is cleared, which is
    # visible and recoverable, rather than silently un-pausing someone else.
    logger.warning(
        "cannot clear the stop sentinel on this platform (no dir_fd pinning): %s",
        _redact(str(real_dir)),
    )


def _arm_stop_sentinel(spec_dir: Path) -> str:
    """Clear this spec's stale STOP sentinel and return the sentinel path.

    BLOCKING -- call via ``asyncio.to_thread``. Bundles the ``unlink`` with the
    path the autonudge arm needs so the handoff handler makes one thread hop
    instead of two filesystem round-trips on the event loop. Returns ``""`` when
    the spec dir does not verify, which the caller must treat as a refusal.
    """
    real_dir = _verified_spec_dir(spec_dir)
    if real_dir is None:
        return ""
    _clear_stop_sentinel(real_dir)
    return str(real_dir / _STOP_FILE)


def _write_stop_sentinel_for_spec(
    spec_dir: Path, name: str = "", expect_slot_key: str = ""
) -> bool:
    """``_write_stop_sentinel`` with the spec's identity pinned to the write.

    BLOCKING -- call via ``asyncio.to_thread``. The counterpart to the gate in
    ``_prepare_handoff``, for the opposite act: arming REMOVES a STOP, this one
    CREATES one, and both are destructive to whichever spec currently owns the
    directory. A same-name delete plus re-import between the caller's identity
    check and this write lands the STOP in the REPLACEMENT's directory, halting a
    run the user has only just started.

    Same critical-section reasoning, and the same safety argument, as
    ``_prepare_handoff``: identity and act inside one ``_INDEX_LOCK`` hold, in a
    worker thread, so the event loop never waits on it. Callers without an
    identity to pin (no *name* / *expect_slot_key*) still get the plain write --
    the gate cannot refuse what it cannot identify.
    """
    with _INDEX_LOCK:
        if name and expect_slot_key:
            current = _load_index().get(name) or {}
            if str(current.get("slot_key", "")) != expect_slot_key:
                return False
        return _write_stop_sentinel(spec_dir)


def _prepare_handoff(spec_dir: Path, name: str = "", expect_slot_key: str = "") -> tuple[bool, str]:
    """Everything the handoff endpoint needs off the filesystem, in one hop.

    BLOCKING -- call via ``asyncio.to_thread``. Returns ``(ready, sentinel
    path)``; ``ready`` is False both when ``tasks.md`` is missing AND when the
    spec dir fails verification, so a replaced-by-symlink directory cannot start
    a run (nor touch another spec's sentinel on the way).

    With *name* and *expect_slot_key*, the identity is re-checked under the index
    lock and the sentinel is armed WITHIN THE SAME critical section, and a
    mismatch refuses. Arming is destructive -- it removes the STOP that a Pause
    wrote -- so it must not happen for a spec this request does not refer to: a
    stale same-name, same-path execute would otherwise clear a REPLACEMENT's stop
    and let the persisted loop resume after a restart. Gating the act itself is
    what covers a request carrying no client claim, which no claim comparison can
    refuse.

    The check and the act are ONE critical section rather than two statements,
    because a same-name delete plus re-import landing between them leaves the
    check passing for the spec that is already gone while the arm lands on its
    replacement -- correct ordering alone does not close that window, only
    holding the lock across both does.

    Holding ``_INDEX_LOCK`` across filesystem work is safe HERE specifically
    because this function is BLOCKING by contract and only ever runs in a worker
    thread, so the critical section cannot stall the event loop. The lock is a
    plain non-reentrant ``threading.Lock`` and nothing reachable from
    ``_arm_stop_sentinel`` re-acquires it, so the wider section cannot deadlock.
    Do NOT widen it further into anything that awaits or that touches the index.
    """
    with _INDEX_LOCK:
        if name and expect_slot_key:
            current = _load_index().get(name) or {}
            if str(current.get("slot_key", "")) != expect_slot_key:
                return False, ""
        sentinel = _arm_stop_sentinel(spec_dir)
    if not sentinel:
        return False, ""
    # Through _spec_file, not a bare is_file(): is_file() FOLLOWS a symlink, so a
    # planted tasks.md -> <somewhere else> satisfied the gate and the autonomous
    # run then edited the link target outside the spec directory. _spec_file
    # refuses a symlink, a realpath that escapes the spec dir, and a sensitive
    # target; the extra is_file() keeps the "not written yet" case honest.
    tasks = _spec_file(spec_dir, "tasks.md")
    if tasks is None or not tasks.is_file():
        return False, sentinel
    # Existence is not a plan. The prompt this gate arms tells the agent to work
    # through each UNCHECKED task in order, so a zero-byte or half-written
    # tasks.md gave the autonomous loop nothing to act on while still reading as
    # a finished Tasks phase. Read through _read_spec_text rather than by name:
    # it validates the descriptor it read, and the agent writes into this very
    # directory, so the inode can change after the is_file() above.
    text = _read_spec_text(spec_dir, "tasks.md")
    return bool(text and _has_open_task(text)), sentinel
