"""Track S2: the asynchronous backup sidecar and the restore path.

This is what makes a conversation survive its container. Two public seams,
named in ``container/CONTRACT.md``:

* ``container.backup.sidecar:run_sidecar(settings)`` — the long-running copier.
* ``container.backup.restore:run_restore(settings)`` — runs to completion before
  the backend starts; the supervisor (Track S3) calls it.

``backup_status(settings)`` exposes the accepted-but-visible backup lag as a
metric the owner can read.
"""

from __future__ import annotations

from .restore import RestoreResult, run_restore
from .sidecar import CycleResult, backup_status, run_backup_cycle, run_sidecar

__all__ = [
    "run_sidecar",
    "run_restore",
    "run_backup_cycle",
    "backup_status",
    "CycleResult",
    "RestoreResult",
]
