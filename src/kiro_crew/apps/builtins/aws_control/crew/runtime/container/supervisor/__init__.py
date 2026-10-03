"""Track S3: launch the Kiro Crew backend headless and supervise the process tree.

This package owns the container entrypoint. It runs the startup order the
contract makes a correctness requirement (`docs/system-specs/modules/aws-control.md`, "Four
processes, one task"): gate the environment and install the bundle, restore the authority
files, then the backend, then the front process, then the backup sidecar. It also owns
shutdown, which terminates process *groups* rather than pids because a `kiro-cli` worker is
a two-process tree and signalling only the launcher orphans a child that finishes its turn
anyway.

The restore is third for a reason the ordering rule states: the backend's periodic flush
persists its in-memory slot table, so a flush landing before the restore finishes writes an
empty set over the record of which conversations existed.

Public seams other tracks may call (do not import our internals otherwise):

- ``container.supervisor.backend:start_backend(settings)``
- ``container.supervisor.backend:wait_until_ready(settings, timeout)``
"""

from .backend import start_backend, wait_until_ready

__all__ = ["start_backend", "wait_until_ready"]
