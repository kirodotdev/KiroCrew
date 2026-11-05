"""Tracked background jobs for the explicit "Update remote" action.

``POST /api/instances/{id}/update`` runs a remote crew's own ``kirocrew update``
and, when that succeeds, ``kirocrew restart``, then reads the crew's version
again to learn whether anything was installed. An update downloads and installs
a build, so it outlives any HTTP request: the route starts a job here and
returns at once, and the dashboard polls the job's state.

This module owns only the bookkeeping -- one latest job per instance, its phase
and outcome, and the task refs that keep a running job from being collected.
What the job runs, and on which transport, is
:meth:`SshTunnelManager._update_then_restart`'s business.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Budget for the remote ``kirocrew update`` itself: a release-feed check, a
#: wheel download and an install over a possibly slow link. Far above the 60s a
#: restart gets, and still bounded, so a remote that hangs reports a timeout.
REMOTE_UPDATE_TIMEOUT_SECS = 600.0

#: How long the job waits for the restarted crew to answer with its version,
#: and how often it asks. The tunnel self-heals after the restart, which takes
#: a probe interval or two.
REMOTE_UPDATE_VERIFY_SECS = 180.0
REMOTE_UPDATE_VERIFY_INTERVAL_SECS = 5.0

STATE_RUNNING = "running"
#: The crew came back on a different version than before the update.
STATE_SUCCEEDED = "succeeded"
#: ``update`` and ``restart`` both exited 0, but the crew came back on the
#: version it had: its updater installed nothing (already on its channel's
#: newest build, or an install it does not update itself).
STATE_UNCHANGED = "unchanged"
#: Both commands exited 0, but the crew did not answer with a version in time.
STATE_UNVERIFIED = "unverified"
STATE_FAILED = "failed"

PHASE_UPDATING = "updating"
#: The update is installed and the crew still runs work a restart would stop:
#: the job asks again until it is idle. ``waiting`` carries the last counts.
PHASE_WAITING_FOR_IDLE = "waiting_for_idle"
PHASE_RESTARTING = "restarting"
PHASE_VERIFYING = "verifying"
PHASE_DONE = "done"

#: How long a job waits for the crew to go idle before it gives up without
#: restarting: the ceiling the crew's own gateway gives a mandatory update it
#: holds back for in-flight work (``_MANDATORY_UPDATE_MAX_DEFER_SECS``). A test
#: pins the two equal.
REMOTE_UPDATE_IDLE_WAIT_SECS = 600.0
#: How often a busy crew is asked again. Each ask is one remote command.
REMOTE_UPDATE_IDLE_POLL_SECS = 15.0

#: ``code`` of a failed job that stopped short of the restart. ``still_busy``:
#: the crew was still running work when the wait ran out. ``peer_cannot_wait``:
#: the crew runs a build with no idle check. Both are retried with ``force``.
FAIL_STILL_BUSY = "still_busy"
FAIL_PEER_CANNOT_WAIT = "peer_cannot_wait"
#: The crew could not establish that it is idle (its check failed, or a restart
#: there would reach more than the gateway it checked). Retried with ``force``.
FAIL_IDLE_CHECK_FAILED = "idle_check_failed"

#: The ``code`` values with which ``restart_remote`` refuses without restarting;
#: the route answers them 409, each retried with ``force``.
RESTART_REFUSAL_CODES = frozenset({"busy", FAIL_PEER_CANNOT_WAIT, FAIL_IDLE_CHECK_FAILED})

#: The remote command that checks once and restarts only if the crew is idle.
RESTART_WHEN_IDLE_SUBCOMMAND = "restart --when-idle --max-wait 0"

#: What ``kirocrew restart --when-idle`` reports on the crew
#: (``kiro_crew.cli_restart_idle``; a test pins these to its own).
_GATE_EXIT_BUSY = 75
_GATE_EXIT_UNSUPPORTED = 69
_GATE_EXIT_REFUSED = 78
_GATE_REFUSED_MARKER = "kirocrew-restart-gate-refused"
#: The busy marker is this prefix, one space, then a JSON object with the
#: counts: ``kirocrew-restart-busy {"turns":2,"background":1}``.
_GATE_BUSY_MARKER = "kirocrew-restart-busy"
#: Where the runner joins the stdout tail onto the stderr head
#: (``token_mint._with_stdout_tail``).
_STDOUT_TAIL_JOIN = " | stdout tail: "
#: argparse's exit status for an unknown flag: a crew whose CLI predates
#: ``--when-idle``. Nothing else ``kirocrew restart`` does exits 2.
_ARGPARSE_USAGE_EXIT = 2


@dataclass(frozen=True)
class GateAnswer:
    """What one ``restart --when-idle --max-wait 0`` on the crew came back with."""

    #: ``restarted``, ``busy``, ``unsupported``, ``refused`` or ``failed``.
    outcome: str
    turns: int = 0
    background: int = 0


def _read_busy_counts(err: str) -> tuple[int, int] | None:
    """The ``(turns, background)`` of the busy marker ending *err*, or ``None``.

    The marker is the CLI's last line: the last stdout line when the runner
    added a stdout tail, else the last stderr line. That line must be exactly
    the marker, a space and a JSON object whose ``turns`` and ``background``
    are non-negative integers; anything else is unreadable.
    """
    lines = (err or "").strip().splitlines()
    if not lines:
        return None
    last = lines[-1].rpartition(_STDOUT_TAIL_JOIN)[2].strip()
    prefix = _GATE_BUSY_MARKER + " "
    if not last.startswith(prefix):
        return None
    try:
        body = json.loads(last[len(prefix) :])
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    turns = body.get("turns")
    background = body.get("background")
    # ``type(...) is int`` and not ``isinstance``: JSON ``true`` reads as a bool,
    # which is an int subclass and no count.
    if type(turns) is not int or type(background) is not int:
        return None
    if turns < 0 or background < 0:
        return None
    return turns, background


def read_gate_answer(rc: int, err: str) -> GateAnswer:
    """Classify the exit status and message of a remote idle-gated restart.

    *err* is the runner's message with its stdout tail (``stdout_tail=True``):
    the marker is the CLI's last stdout line, so an SSH banner that fills the
    head of stderr cannot hide it. A busy exit whose marker cannot be read is
    ``refused``: the crew restarted nothing, and its counts are unknown.
    """
    if rc == 0:
        return GateAnswer("restarted")
    if rc == _GATE_EXIT_BUSY:
        counts = _read_busy_counts(err)
        if counts is None:
            return GateAnswer("refused")
        return GateAnswer("busy", counts[0], counts[1])
    if rc in (_GATE_EXIT_UNSUPPORTED, _ARGPARSE_USAGE_EXIT):
        return GateAnswer("unsupported")
    if rc == _GATE_EXIT_REFUSED and _GATE_REFUSED_MARKER in (err or ""):
        return GateAnswer("refused")
    return GateAnswer("failed")


def read_force_flag(body: object) -> bool | None:
    """``force`` from an update or restart request body, or ``None`` if malformed.

    No body (``None``) and ``{}`` both mean ``False``. Only a JSON object with an
    optional boolean ``force`` is accepted; nothing in it reaches the remote
    command beyond choosing between two fixed literals.
    """
    if body is None:
        return False
    if not isinstance(body, dict) or set(body) - {"force"}:
        return None
    force = body.get("force", False)
    return force if isinstance(force, bool) else None


#: Longest error tail a job keeps. The runners already cap and redact their
#: own tails; this bounds the combined stderr + stdout message.
_ERROR_MAX_CHARS = 700

#: A version string a job may record: the shape a release or build id has.
#: Anything else from the peer is dropped rather than echoed.
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


def clean_version(value: object) -> str:
    """*value* if it has a version's shape, else ``""``."""
    if isinstance(value, str) and _VERSION_RE.fullmatch(value):
        return value
    return ""


@dataclass
class RemoteUpdateJob:
    """One update of one remote crew, from the click to its outcome."""

    started_at: float
    state: str = STATE_RUNNING
    phase: str = PHASE_UPDATING
    finished_at: float | None = None
    error: str = ""
    version_before: str = ""
    version_after: str = ""
    #: Why a failed job stopped (``FAIL_*``), or ``""``.
    code: str = ""
    #: The crew's last in-flight counts while ``waiting_for_idle``.
    waiting_turns: int = 0
    waiting_background: int = 0
    #: Whether this job restarts without waiting for the crew to go idle.
    force: bool = False

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    def advance(self, phase: str) -> None:
        if self.running:
            self.phase = phase

    def wait_for_idle(self, turns: int, background: int) -> None:
        """Record that the crew is still busy, with what it reported."""
        if self.running:
            self.phase = PHASE_WAITING_FOR_IDLE
            self.waiting_turns = max(0, turns)
            self.waiting_background = max(0, background)

    def finish(self, state: str, *, version_after: str = "") -> None:
        """End the job in a non-failed outcome (succeeded/unchanged/unverified)."""
        if self.running:
            self.state = state
            self.phase = PHASE_DONE
            self.version_after = version_after
            self.finished_at = time.time()

    def fail(self, error: str, *, code: str = "") -> None:
        """End the job failed, keeping a clamped error tail.

        Every *error* is a literal from the job body or a tail the transport
        runner already redacted (:func:`run_remote_kirocrew`,
        :func:`run_remote_kirocrew_ssm`), so this only bounds its length. The
        phase is left where the failure happened, so the dashboard can say
        whether the update or the restart after it failed.
        """
        if self.running:
            self.state = STATE_FAILED
            self.error = (error or "")[-_ERROR_MAX_CHARS:]
            self.code = code
            self.finished_at = time.time()

    def to_dict(self) -> dict:
        end = self.finished_at if self.finished_at is not None else time.time()
        return {
            "state": self.state,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_secs": max(0, round(end - self.started_at)),
            "error": self.error,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "code": self.code,
            "waiting": {"turns": self.waiting_turns, "background": self.waiting_background},
            "force": self.force,
        }


class RemoteUpdateJobs:
    """The latest update job per instance, and the tasks running them."""

    def __init__(self) -> None:
        self._jobs: dict[str, RemoteUpdateJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, instance_id: str) -> RemoteUpdateJob | None:
        return self._jobs.get(instance_id)

    def start(
        self,
        instance_id: str,
        body: Callable[[RemoteUpdateJob], Awaitable[None]],
    ) -> RemoteUpdateJob | None:
        """Start *body* as *instance_id*'s job, or return ``None`` if one is running.

        Must be called on the event loop. *body* advances and ends the job it
        is handed; an exception it raises fails the job with the exception's
        type name only, so no message text it carries reaches the dashboard.
        """
        current = self._jobs.get(instance_id)
        if current is not None and current.running:
            return None
        job = RemoteUpdateJob(started_at=time.time())
        self._jobs[instance_id] = job

        async def _run() -> None:
            try:
                await body(job)
            except asyncio.CancelledError:
                job.fail("update cancelled: the gateway shut down")
                raise
            except Exception as e:  # the job reports it; nothing awaits this task
                logger.warning("Remote update job for %s raised", instance_id, exc_info=True)
                job.fail(f"update failed: {type(e).__name__}")
            if job.running:
                job.fail("update ended without an outcome")

        task = asyncio.get_running_loop().create_task(_run())
        self._tasks.add(task)

        def _on_done(t: "asyncio.Task[None]") -> None:
            self._tasks.discard(t)
            # A cancel delivered before ``_run``'s first step -- e.g.
            # ``cancel_all`` during shutdown while the task is still queued --
            # raises ``CancelledError`` into a coroutine that never entered its
            # ``try``, so the handler above never runs and the job would stay
            # ``running`` forever, wedging the instance (``start`` refuses while
            # a job is running). Settle it here. On every other path the body
            # has already ended the job, so ``job.running`` is False and this is
            # a no-op -- no double-fail.
            if t.cancelled() and job.running:
                job.fail("update cancelled: the gateway shut down")

        task.add_done_callback(_on_done)
        return job

    def cancel_all(self) -> None:
        """Cancel every running job's task (gateway shutdown)."""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
