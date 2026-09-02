"""The one process-wide lock that serializes every write to the dedup ledger file.

The ledger (``store.ledger_path()`` → ``<workspace>/ledger.jsonl``) is a single
append-only JSONL file, and it is written from THREE code paths that all run on
gateway worker threads in one process:

  * the loop's own filing — :meth:`spine.ledger.Ledger.record`, driven by
    :mod:`spine.pr_pipeline` on the active-run worker thread;
  * the operator's manual draft / commit — ``backend.ledger_admin.record_filed`` /
    ``record_committed``;
  * the operator's forget / purge — ``backend.ledger_admin.forget`` / ``purge``,
    which read the latest event, DECIDE on it, and append, all as one step.

``known()`` resolves a fingerprint by its LATEST event (last-write-wins). If any two
of those paths hold DIFFERENT locks, their read → decide → append sequences
interleave: a ``forget`` that read a ``QUEUED`` placeholder can append ``purged``
AFTER a concurrent path appended the real ``filed(<pr-url>)`` row, so ``purged``
wins, the pull request is hidden, and the loop drafts a second PR for a change
already up for review (#6716). One lock shared by all three writers makes each
sequence atomic against the others and removes the interleaving.

WHY A DEDICATED LEAF MODULE, not a lock defined in ``spine.ledger`` or
``backend.ledger_admin``: the lock must be the SAME object in both layers, but
``backend.ledger_admin`` is deliberately kept off the spine engine (importing
``spine`` eagerly loads the driver/agent-runner/PR-pipeline). This module imports
only :mod:`threading`, so either layer can import it for the shared object without
pulling the engine, and it belongs in ``spine`` because the ledger it guards is
spine-owned durable state.

WHY ``RLock``: :meth:`spine.ledger.Ledger.record` already serialized on the
instance's own lock; folding that into a re-entrant process lock means a future
caller that already holds it (a guarded helper calling another) cannot
self-deadlock. It is only ever acquired for a bounded file append, never around a
subprocess, network call, or a wait — so it cannot starve other writers.

LOCK ORDERING: this is an INNER lock. The manual-draft route holds
``commit.clone_lock`` (the clone-mutation lock) across its whole
materialize → commit → draft sequence and only briefly takes this lock at the
final ledger append; the reverse — taking ``clone_lock`` while holding this — never
happens (neither ``spine.ledger`` nor ``ledger_admin`` imports ``commit``), so the
two locks have a single consistent order and cannot deadlock. All acquisitions are
on worker threads (``asyncio.to_thread`` for the operator paths, the run thread for
the loop), never on the asyncio event loop.
"""

from __future__ import annotations

import threading

#: Serializes every read → decide → append against ``store.ledger_path()`` across
#: all three writer paths. Module-level because the ledger file is process-wide
#: shared state; the gateway runs these paths on worker threads.
LEDGER_WRITE_LOCK = threading.RLock()
