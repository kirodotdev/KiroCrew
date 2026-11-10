"""Local machine: one process at a time runs this computer's connectors.

Two processes can host the local controller: the local gateway, and the desktop
app's own host (:mod:`.desktop_host`) for a window on a remote gateway with no
local gateway running. Both read the same grant store, so without this lock a
computer could dial the same remote twice and run each tool server twice.
Whichever process takes the lock first runs the connectors; the other one serves
status, revoke and Off (they only narrow, and act on the shared store) and
starts nothing until the lock is free.

The lock is an OS file lock (``platform_compat.try_acquire_lock``), so it ends
with its process however that process ends. It is not a security boundary: a
process that holds it can only stop this computer's connectors from running.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat

LOCK_NAME = "connected-computers-controller.lock"


class ControllerLock:
    """A non-blocking, re-entrant-per-instance exclusive lock on one file."""

    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None
        self._mutex = threading.Lock()

    @classmethod
    def for_this_machine(cls) -> "ControllerLock":
        from kiro_crew.config.paths import config_dir

        return cls(Path(config_dir()) / "run" / LOCK_NAME)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock if free; True when this instance holds it (already or now)."""
        with self._mutex:
            if self._fd is not None:
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = platform_compat.open_create_or_existing(self.path, os.O_RDWR, 0o600)
            except OSError:
                return False
            if not platform_compat.try_acquire_lock(fd, exclusive=True):
                os.close(fd)
                return False
            self._fd = fd
            return True

    def release(self) -> None:
        with self._mutex:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            platform_compat.release_lock(fd)
        except OSError:
            pass  # closing the descriptor releases it as well
        finally:
            os.close(fd)
