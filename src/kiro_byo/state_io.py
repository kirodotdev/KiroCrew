"""Cross-process JSON ledger transactions for BYO control-plane state.

No external locking dependency; advisory locks require every writer to comply.
Never claim remote-tool exactly-once semantics based on this store.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

@contextmanager
def _file_transaction_lock(target: Path, *, timeout: float = 15.0):
    """Cross-process advisory lock on a *stable* sidecar, not the replaced JSON.

    Uses stdlib primitives on Windows and POSIX. The lock file is deliberately
    never unlinked: removing it could allow two processes to lock different
    inodes and both believe they own a run. A timeout fails closed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        acquired = False
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring durable state lock: {lock_path.name}")
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace whole JSON only after the new bytes have reached the OS.

    POSIX directory fsync also syncs the rename metadata where supported.
    Windows supports file fsync but not portable directory fsync via stdlib.
    A remote network drive may provide weaker semantics; no exactly-once claim.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            # Directory fsync is unsupported on some filesystems. Do not
            # misrepresent that as an unperformed rename (which already ran).
            fd = None
            try:
                fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                os.fsync(fd)
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


