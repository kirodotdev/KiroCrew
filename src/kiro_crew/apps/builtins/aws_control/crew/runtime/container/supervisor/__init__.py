"""Track S3: launch the Kiro Crew backend headless and supervise the process tree.

This package owns the container entrypoint. It runs the startup order the
contract makes a correctness requirement (`container/CONTRACT.md`, "Startup
order"): restore to completion, then the backend, then the front process and the
sidecar. It also owns shutdown, which terminates process *groups* rather than
pids because a `kiro-cli` worker is a two-process tree and signalling only the
launcher orphans a child that finishes its turn anyway.

Public seams other tracks may call (do not import our internals otherwise):

- ``container.supervisor.backend:start_backend(settings)``
- ``container.supervisor.backend:wait_until_ready(settings, timeout)``
"""

from .backend import start_backend, wait_until_ready

__all__ = ["start_backend", "wait_until_ready"]
