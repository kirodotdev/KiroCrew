"""The per-job events a running cloud launch listens to: cancel and skip sign-in.

The launch routes set them; the worker thread running
:func:`kiro_crew.cloud.launch_job.run_launch` reads them. Kept on the long-lived
``DashboardState`` (never a module global), one dict per kind, keyed by job id.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from kiro_crew.cloud.launch_job import SigninSkip

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


def _cancels(state: "DashboardState") -> dict:
    cancels = getattr(state, "cloud_launch_cancels", None)
    if cancels is None:
        cancels = {}
        state.cloud_launch_cancels = cancels
    return cancels


def _skips(state: "DashboardState") -> dict:
    skips = getattr(state, "cloud_launch_skips", None)
    if skips is None:
        skips = {}
        state.cloud_launch_skips = skips
    return skips


#: Serialises the register/release pairs on ``_cancels`` and ``_skips``.
#: Registration happens on the event loop and release on a worker thread, so a
#: compare-then-delete without it could still read one worker's event and delete
#: the next one's.
_CANCELS_GUARD = threading.Lock()


def _register_cancel(state: "DashboardState", job_id: str, cancel: threading.Event) -> None:
    with _CANCELS_GUARD:
        _cancels(state)[job_id] = cancel


def _release_cancel(state: "DashboardState", job_id: str, cancel: threading.Event) -> None:
    """Drop the registered event for *job_id* only if it is still *cancel*.

    A worker releases its event AFTER persisting its terminal result -- and the
    restart route admits a new retry for the same job as soon as the file reads
    terminal, registering a new event under the same id. An unconditional pop in
    the finishing worker's ``finally`` therefore raced that registration and could
    delete the NEW worker's event: a cancel then found nothing to set, wrote
    CANCELLED, and the new worker overwrote it with its result -- the cancel was
    silently ignored. Releasing only our own event closes that.
    """
    with _CANCELS_GUARD:
        cancels = _cancels(state)
        if cancels.get(job_id) is cancel:
            del cancels[job_id]


def _register_skip(state: "DashboardState", job_id: str, skip: SigninSkip) -> None:
    with _CANCELS_GUARD:
        _skips(state)[job_id] = skip


def _release_skip(state: "DashboardState", job_id: str, skip: SigninSkip) -> None:
    """Drop *job_id*'s skip event only if it is still *skip* (see ``_release_cancel``)."""
    with _CANCELS_GUARD:
        skips = _skips(state)
        if skips.get(job_id) is skip:
            del skips[job_id]


def request_skip(state: "DashboardState", job_id: str) -> bool:
    """Ask the worker to skip sign-in; refuse if absent or past the sign-in step."""
    with _CANCELS_GUARD:
        skip: SigninSkip | None = _skips(state).get(job_id)
        return skip.request() if skip is not None else False
