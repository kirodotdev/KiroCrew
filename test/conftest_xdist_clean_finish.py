"""The INTERNALERROR report for an xdist worker that finished while still assigned work.

xdist's ``DSession.worker_workerfinished`` then fails ``assert not crashitem`` on an
item that never started; ``test/conftest.py``'s ``pytest_internalerror`` prints this.
"""

from __future__ import annotations


def format_clean_finish_report(excinfo) -> str | None:
    """The report for xdist's ``assert not crashitem``; ``None`` for any other error."""
    if excinfo is None or not excinfo.errisinstance(AssertionError):
        return None
    entry = excinfo.traceback[-1]
    if entry.name != "worker_workerfinished":
        return None
    local = entry.frame.f_locals
    worker = getattr(getattr(local.get("node"), "gateway", None), "id", "<unknown worker>")
    # The loadscope/loadgroup scheduler's queued units; other schedulers have none.
    queue = getattr(getattr(local.get("self"), "sched", None), "workqueue", None)
    queued = "?" if queue is None else sum(not d for u in queue.values() for d in u.values())
    return "\n".join(
        [
            "",
            "=" * 72,
            f"xdist run ABANDONED: worker {worker} finished while still assigned work",
            "=" * 72,
            f"Assigned to it but never started: {local.get('crashitem')}",
            f"The scheduler still queued {queued} selected items (that one may be among",
            "them); none of them ran.",
            "The worker's loop ended before that item reached it. Re-run the shard.",
            f"Worker {worker}'s session_end progress record says whether its channel had",
            "closed (channel_closed: true) or was still open (false: a shutdown command).",
            "=" * 72,
        ]
    )
