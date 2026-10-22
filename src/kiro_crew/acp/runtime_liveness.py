"""Whether this runtime is alive, busy, dead or due to recycle.

The exit watch, the stderr drain and its latches, death attribution and its summary, reap
tracking, the session occupancy checks and the age and RSS staleness checks.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead``, ``_death_expected``, ``_death_label``,
``_death_reason``, ``_death_summary``, ``_death_tail``, ``_pending_init_notifications``,
``_pending_requests``, ``_routed_requests``, ``_saw_auth_failure``,
``_saw_delegate_launch_failure``, ``_saw_sandbox_init_failure``,
``_stall_turn_sessions``, ``_stderr_lines``,
``_stdin_stall_death``.

Instance fields read: ``_first_session_ready``, ``_last_activity``, ``_max_age_secs``,
``_max_rss_depth``, ``_max_rss_mb``, ``_pid``, ``_process``, ``_session_inits_in_flight``,
``_session_queues``, ``_spawn_monotonic``, ``_spawn_start_token``, ``_stderr_task``,
``_turn_active_sessions``, ``recording_allowed``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import TYPE_CHECKING

from kiro_crew import platform_compat, runtime_death
from kiro_crew.acp import runtime_process_tree
from kiro_crew.acp.session_handle import AcpRuntimeDead
from kiro_crew.acp.transport_errors import (
    is_auth_failure_output,
    is_delegate_launch_failure_output,
    is_sandbox_init_failure_output,
    registration_throttle_line,
)
from kiro_crew.acp.transport_framing import settle_drain
from kiro_crew.executors import subprocess_executor
from kiro_crew.runtime_ownership import outstanding_leases
from kiro_crew.session_pid import (
    _pgroup_has_member_besides,
    _untrack_root_by_identity,
    unregister_protected_pid,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


# Cap on the stderr text folded into a process-exit death reason. The reason
# is what the chat error card shows, so it must stay one readable line: the
# LAST non-empty stderr line the drain captured, truncated to this many
# characters -- roughly one card line. The 20-line ring itself is unchanged.
_STDERR_REASON_TAIL_CHARS = 200
# The one stderr signature that names a host fault rather than a kiro-cli
# fault: every sandboxed spawn fails with it once the runtime tmpfs the
# launcher stages mount sources on runs out of space or inodes. Point the
# operator at the doctor check that measures that filesystem.
_ENOSPC_MARKER = "no space left on device"
_ENOSPC_HINT = (
    "the runtime tmp filesystem is out of space or inodes; run `kirocrew doctor` "
    "(Runtime tmpfs section)"
)


def _proven_death_cause(tail: str) -> tuple[str, str] | None:
    """The line in *tail* that EXPLAINS a death and its operator hint, or None.

    Returns ``(evidence, hint)`` -- the matched line, and any pointer that line
    earns -- rather than one joined string, because the caller caps what it shows.
    Joining first would let a long line push its own hint past the cap, which is
    precisely backwards: the hint is the short part the operator needs and the
    evidence is the long part that can afford to be trimmed. ``hint`` is empty
    when the signature carries no pointer.

    A child's last stderr line is not evidence of why it died, and reporting it
    as the cause sends the reader after the wrong thing. Measured over one
    gateway's fleet: every sampled runtime death carried ``rc=-15``, an ordinary
    SIGTERM teardown, while the ``HTTP 404`` registration line that gets pasted
    as the reason appears in 3 of 41 deaths and once on a runtime that does not
    die at all. So the cause slot is reserved for a shape that
    describes a death, and an unexplained one is reported as the exit status it
    is, with the tail kept for whoever asks the log for it.

    Both signatures are matched per LINE by their own helpers and searched over
    the WHOLE retained tail rather than only its last line, because which line a
    child printed last is a race with its own flushing and says nothing about
    which line matters.
    """
    throttled = registration_throttle_line(tail)
    if throttled:
        return throttled, ""
    for line in tail.splitlines():
        if _ENOSPC_MARKER in line.lower():
            return line.strip(), _ENOSPC_HINT
    return None


def _rc_phrase(rc: object) -> str:
    """``rc=N``, and for a signal death the SIGNAL's name beside it.

    A negative returncode is POSIX's ``-signum``, which is the whole exit status
    of a killed child -- and the number alone is the part an operator has to look
    up. Naming it is what makes the ordinary teardown legible as one:
    ``rc=-15 (signal SIGTERM)`` reads as "something asked it to stop", where a
    bare ``rc=-15`` beside a stray stderr line reads as a crash.

    Anything that is not a negative int -- a real exit code, the ``?`` a
    never-spawned process yields -- is rendered unchanged, so this only ever adds
    a name it can prove.
    """
    if isinstance(rc, int) and not isinstance(rc, bool) and rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            return f"rc={rc}"
        return f"rc={rc} (signal {name})"
    return f"rc={rc}"


# Default recycling thresholds for long-lived multiplexed runtimes (see
# _is_stale()). These are conservative defaults chosen to recycle well before
# the unbounded growth observed in production (multi-GB RSS after ~24h of
# uptime with no per-turn compaction) while still amortizing process-spawn
# cost across many background prompts.
_DEFAULT_MAX_AGE_SECS = 6 * 3600  # 6 hours
_DEFAULT_MAX_RSS_MB = 500.0  # 500 MiB

# Below this uptime the RSS staleness probe is skipped entirely (see
# _is_stale()). A freshly-(re)used runtime has not had time to grow, so this
# keeps the hot get_bg_session reuse path — which holds _bg_runtime_lock —
# CPU-only for young runtimes and only pays the offloaded RSS probe once a
# runtime has lived long enough to plausibly have ballooned.
_RSS_PROBE_MIN_AGE_SECS = 300.0  # 5 minutes


def is_alive(self: AcpRuntime) -> bool:
    """True if the underlying process exists and has not exited."""
    return self._process is not None and self._process.returncode is None and not self._dead


def stdin_reader_may_live(self: AcpRuntime) -> bool:
    """Whether a process that can still read this runtime's stdin may run.

    Blocking (a process-group scan), so call it off the loop. The pipe is
    inherited inside the root's process group -- the root may be a sandbox
    launcher whose child reads it -- while stdio MCP servers lead their own
    groups, so the answer is the root (proven by identity, which holds
    whether or not this handle still references it) or any live member of
    its group. Conservative on an unreadable group: True.
    """
    pid = self._pid  # pid-owner-ok: the runtime reads its own root
    if pid is None:
        return False
    if self._root_identity() == "holds":
        return True
    if platform_compat.IS_WINDOWS:
        return False
    return _pgroup_has_member_besides(pid, pid)


def turn_active_at_stall(self: AcpRuntime, session_id: str) -> bool:
    """Whether *session_id* had a turn running when a stdin stall killed this runtime.

    Frozen at the death, so a reader that runs after the turn's own teardown
    (the provider's exception translation) still gets the answer.
    """
    return session_id in self._stall_turn_sessions


def death_summary(self: AcpRuntime) -> str | None:
    """One-line death attribution, or None while alive.

    Composed once by ``_mark_dead`` (reason + returncode + stderr tail).
    Lets a consumer that only observes the death through a poisoned
    session queue — e.g. a turn's frame wait — report WHO/WHY instead
    of a bare "process died".
    """
    return self._death_summary


def _stale_by_age(self: AcpRuntime) -> bool:
    """True if uptime exceeds max_age_secs. Cheap, no I/O — safe to call
    under a lock. Does NOT consider RSS (see _is_stale for that).

    NOT a recycle predicate: RSS, not age, is the growth mode this class
    was observed failing on, so a reuse decision MUST ask _is_stale().
    Reaching for this one because it is cheaper is what left the shared
    background runtime unbounded. No production caller today.
    """
    if self._pid is None or self._spawn_monotonic is None:
        return False
    return (time.monotonic() - self._spawn_monotonic) > self._max_age_secs


async def _is_stale(self: AcpRuntime) -> str | None:
    """Return the recycle reason ('age' or 'rss'), or None if not stale.

    Distinct from is_alive(): a runtime can be perfectly healthy (process
    running, protocol responsive) yet still be "stale" — e.g. the
    kirocrew-lite background runtime observed growing unbounded (multi-GB
    RSS) over ~24h of uptime because the multiplexed design has no per-turn
    compaction or lifetime cap. Callers should check this alongside
    is_alive() and stop reusing a stale process: kill() and respawn when the
    active session count is 0, and otherwise DETACH it (park it to drain,
    respawn for new callers, reap on its last unregister) rather than
    deferring. Waiting for an idle window is not a bound — a multiplexed
    runtime under sustained background load never has one, which is how the
    multi-GB growth above went unchecked.

    RSS is measured across the whole descendant tree (_get_rss_tree_mb):
    under the Linux namespace sandbox self._pid is the launcher parent, and
    the real kiro-cli child is what grows. The RSS probe shells out / reads
    /proc, so it is offloaded to subprocess_executor() to keep the event
    loop free.

    The RSS probe is gated behind _RSS_PROBE_MIN_AGE_SECS: a freshly-(re)used
    runtime returns None without any executor round-trip, so the hot reuse
    path in get_bg_session (which holds _bg_runtime_lock) stays CPU-only for
    young runtimes. The lock IS deliberately held across the probe for older
    runtimes, busy or idle; the age gate bounds how often that happens, and a
    runtime that answers "stale" is displaced rather than re-probed.
    """
    if self._pid is None:
        return None

    if self._spawn_monotonic is not None:
        age = time.monotonic() - self._spawn_monotonic
        if age > self._max_age_secs:
            return "age"
        if age < _RSS_PROBE_MIN_AGE_SECS:
            # Too young to have grown — skip the offloaded RSS probe.
            return None

    rss_mb = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        runtime_process_tree._get_rss_tree_mb,
        self._pid,
        self._max_rss_depth,
    )
    if self._max_rss_depth is not None:
        # A bounded scope rests on a structural fact about the host's process
        # tree (which generation the resident process sits at). Record what the
        # bound captured against the ceiling it is judged by, so a host release
        # that moves that process out of scope is attributable from the log
        # rather than showing up only as a ceiling that never trips.
        logger.debug(
            "rss probe: backend=%r pid=%s depth=%s rss_mb=%s ceiling_mb=%s",
            self.acp_backend,
            self._pid,
            self._max_rss_depth,
            rss_mb,
            self._max_rss_mb,
        )
    if rss_mb is not None and rss_mb > self._max_rss_mb:
        return "rss"

    return None


def has_active_sessions(self: AcpRuntime) -> bool:
    """True if any session is currently registered on this runtime.

    Killing a runtime while a co-tenant session is registered drops that
    session's in-flight prompt/response. Every recycle path now asks
    ``has_active_or_initializing_sessions`` instead, which closes the
    registration window this one leaves open; no production caller remains.
    """
    return bool(self._session_queues)


def has_active_or_initializing_sessions(self: AcpRuntime) -> bool:
    """True if any session is registered OR still being created.

    ``has_active_sessions`` sees only REGISTERED queues, and
    ``create_session`` registers outside the runtime lock -- so a co-tenant
    whose ``session/new`` is in flight is momentarily invisible to it, and
    killing the runtime under it surfaces as ``AcpRuntimeDead`` on work the
    user never connected to whatever prompted the kill.

    This is therefore the predicate every recycle and displacement decision
    asks, because it also counts ``_session_inits_in_flight``: a runtime with
    an initializing session is treated as busy and parked to drain rather
    than killed, so no caller has to absorb that window with a respawn.
    The init scope opens before ``create_session``'s admission gate, so a
    claim still queued behind the gate already counts.
    """

    return bool(self._session_queues) or self._session_inits_in_flight > 0


def saw_not_logged_in(self: AcpRuntime) -> bool:
    """True if kiro-cli reported an auth failure on stderr.

    Lets callers translate a runtime death into AcpAuthRequired (an
    actionable login prompt) instead of a generic process-death error —
    parity with AcpClient, which inspects stderr the same way.

    Recognises the full auth vocabulary rather than the literal banner
    ``not logged in``: a real expired bearer token writes
    ``AccessDeniedException: "Invalid token"`` and ``the bearer token
    included in the request is invalid`` and says ``not logged in`` nowhere,
    so a single-banner regex answers False on exactly the state this exists
    to detect, and the operator is shown a ``session/new`` timeout instead.

    Reads the latch, not the ring buffer: see ``_saw_auth_failure``.
    """
    return self._saw_auth_failure


async def settle_stderr(self: AcpRuntime, timeout: float = 0.5) -> None:
    """Give the stderr drain a bounded chance to finish before its latches are read.

    The latches (:meth:`saw_sandbox_init_failure`, :meth:`saw_not_logged_in`)
    are written by the ``_drain_stderr`` TASK, while a death is discovered on
    the stdout side: ``_reader_loop`` sees EOF and ``_mark_dead`` fails the
    pending ``initialize`` future SYNCHRONOUSLY. Both tasks become runnable
    together -- which is the ordinary shape of a real refusal, since the child
    writes its signature and closes stdout at once -- so a caller that reads a
    latch straight off that failure can win the race and see ``False`` for a
    line already in the pipe.

    Bounded and swallowing, because the caller is already on a failure path:
    the worst case of not settling is the generic error it would have produced
    anyway, and no failure here may become a second failure. The same budget
    as ``AcpClient._read_message``'s own EOF drain, but shielded: this child
    may still be alive and the drain is still the latches' only writer. A
    cancel of the caller propagates; see :func:`settle_drain`.
    """
    await settle_drain(self._stderr_task, timeout)


def saw_sandbox_init_failure(self: AcpRuntime) -> bool:
    """True if an OS sandbox told this runtime's child it could not initialize.

    Lets callers translate a runtime death into ``AcpSandboxInitFailed`` -- a
    non-retryable error naming the layer that refused and its switch --
    instead of a generic process-death error the retry ladder then reproduces
    on a host that will refuse identically. Parity with
    :meth:`saw_not_logged_in`, including reading the latch rather than the
    ring buffer.
    """
    return self._saw_sandbox_init_failure


def saw_delegate_launch_failure(self: AcpRuntime) -> bool:
    """True if this runtime's child died because ``kiro-cli-chat`` would not launch.

    Lets callers translate the death into a non-retryable, actionable error
    naming the install fix instead of the generic "process exited" card the
    retry ladder then reproduces on a host whose delegate will fail to launch
    identically. Parity with :meth:`saw_sandbox_init_failure` and
    :meth:`saw_not_logged_in`, including reading the latch rather than the ring
    buffer.
    """
    return self._saw_delegate_launch_failure


def redacted_stderr_tail(self: AcpRuntime) -> str:
    """The retained stderr lines, newline-joined and redacted.

    A SEPARATE reader from :meth:`death_summary`, which exists to be read by a
    person: it folds the same lines onto one line behind a ``returncode``
    prefix. Anything that classifies PER LINE -- the launcher's own refusal
    test -- sees nothing in that shape, so it needs the lines as lines.

    Redacted like every other path out of this buffer: child stderr is
    untrusted subprocess output that can echo a credential. Empty for a
    restricted session, which retains nothing.
    """
    if not self._stderr_lines:
        return ""
    tail = "\n".join(self._stderr_lines)
    from kiro_crew.security import redact

    tail = redact(tail)
    return tail


async def _exit_watch_loop(self: AcpRuntime) -> None:
    """Fail the runtime on the root process's OWN exit, not on stdout EOF.

    The reader loop observes a death only as EOF on stdout. A descendant
    that inherited the root's write end holds that pipe open after the root
    is gone, so EOF does not arrive and the reader -- parked on ``readuntil``
    while nothing is being written -- cannot notice the broken pipe either.
    The death is then invisible until the survivor exits (the pipe finally
    closing) or idle expiry force-reaps the session, on the order of an hour.

    This watcher keys on the exit itself, by polling ``process.returncode``.
    ``returncode`` is set by the event loop's ``_process_exited`` -- the
    SIGCHLD/waitpid path -- the instant the root is reaped, independent of
    whether the pipes have disconnected. ``process.wait()`` cannot serve
    here: its waiters are woken only from ``_call_connection_lost``, after
    ALL pipe transports disconnect, so a descendant holding stdout open
    parks ``wait()`` exactly as long as it parks the reader -- the watcher
    would never fire for the very case it exists to catch. The poll sees a
    non-``None`` returncode as soon as the root is gone, whatever the pipes
    are doing. A still-``None`` returncode is NOT an exit (a closed pipe the
    module deliberately refuses to treat as death), so the watcher keeps
    looping rather than marking dead.

    On the confirmed exit it first gives the reader a bounded window to
    drain any final frame still readable on stdout -- a completed turn's
    response the backend wrote just before exiting -- then marks the runtime
    dead, which fails every pending request and poisons every session queue,
    so a waiting turn surfaces ``AcpProcessDied`` in seconds instead of
    waiting for a backstop timer. The drain yields in short slices while the
    reader keeps routing frames (it stamps ``_last_activity`` per frame) and
    ends the moment the reader goes quiet with an empty buffer, so it costs
    the fail-fast path at most one idle slice on a pipe with nothing left.

    The reader keeps priority: the drain window lets an EOF that arrives in
    the same breath (the ordinary same-second close) run the reader's own
    death+retire path first, and ``_mark_dead`` is idempotent, so the loser
    of that race is a no-op. The exit reason is read AFTER the returncode is
    observed, so it carries the real code rather than ``<not reaped>``.
    """
    process = self._process
    if process is None:
        return
    try:
        while process.returncode is None:
            await asyncio.sleep(self._EXIT_WATCH_POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A returncode read that cannot complete (a mock without the
        # attribute, an OS reap race) leaves death detection to the reader's
        # EOF path. It must never crash the runtime.
        logger.debug("AcpRuntime: exit watcher could not poll the runtime process", exc_info=True)
        return
    # Let the reader drain whatever is already readable before the mark.
    # The backend can write a final response and exit; those bytes sit in
    # the pipe the reader has not pulled yet. _mark_dead clears pending
    # routes and poisons the session queues, so marking before that frame is
    # routed loses a completed turn's response. Yield in short slices while
    # the reader keeps making progress (it stamps _last_activity per frame),
    # and stop as soon as it goes quiet for one slice -- nothing left to pull
    # -- or the total cap is reached. This drains readable frames WITHOUT
    # waiting for EOF, which the surviving descendant holding stdout open may
    # never deliver. An EOF observed in the same breath still wins: the
    # reader's own death+retire path runs during the grace, and _mark_dead is
    # idempotent so the slower observer here is a no-op.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + self._EXIT_DRAIN_MAX
    while not self._dead and loop.time() < deadline:
        before = self._last_activity
        await asyncio.sleep(self._EXIT_DRAIN_GRACE)
        if self._dead:
            break
        if self._last_activity == before and not self._reader_has_buffered_input():
            # The reader processed nothing this slice and nothing is waiting
            # in its buffer: the pipe is drained, stop waiting.
            break
    # Mark dead only once -- a reader-crash, broken-pipe or write-stall path
    # may already have set it before the root exited. But those paths mark
    # dead WITHOUT retiring the registry, so the confirmed exit still owes
    # the retirement: run it whether or not this watcher is the one that
    # marked. _retire_tracking_after_exit is identity-guarded and idempotent,
    # so a second call once the reader's EOF branch has already retired is a
    # no-op, and a stale PID-ledger line a non-retiring death mark left
    # standing is cleared the moment the exit is confirmed.
    if not self._dead:
        rc = process.returncode
        self._mark_dead(self._exit_reason(rc))
    # The reader is still parked on a stdout that never reaches EOF, so it
    # will not run the retire path the EOF branch owns; run it here instead.
    await self._retire_tracking_after_exit()


def _reader_has_buffered_input(self: AcpRuntime) -> bool:
    """Whether stdout still holds bytes the reader has not consumed.

    ``StreamReader`` keeps already-read bytes in a private ``_buffer`` until
    a ``readuntil``/``read`` consumes them. A non-empty buffer means a frame
    (or part of one) is still waiting to be routed, so the exit-drain window
    must keep yielding to the reader rather than mark the runtime dead. The
    private attribute is read defensively: any failure reports "nothing
    buffered" so the drain ends on its cap rather than crashing.
    """
    process = self._process
    stdout = process.stdout if process is not None else None
    if stdout is None:
        return False
    try:
        return bool(stdout._buffer)  # type: ignore[attr-defined]
    except Exception:
        return False


def _exit_reason(self: AcpRuntime, rc: object) -> str:
    """The death reason for a process that exited: its exit status, and a CAUSE only if proven.

    A bare ``rc=1`` told the operator nothing when every tool started failing
    because the runtime tmpfs had run out of inodes, so a cause belongs here
    when there is one. What does NOT belong is the child's last stderr line
    used AS the cause: it is whatever the child happened to flush last, which
    on a measured fleet put ``HTTP 404 Not Found`` on the card for deaths
    whose actual exit status was ``rc=-15`` -- a plain SIGTERM teardown. So
    :func:`_proven_death_cause` decides, over the whole retained tail, and an
    unexplained death reads as the signal or code it was.

    The tail is not lost, it is demoted: the last line goes to the debug log,
    redacted, for a reader who is already looking at this runtime. The card
    and the death summary keep their own separate ``stderr_tail`` field, so
    the evidence is still one hop away from anyone who needs it.
    """
    reason = f"process exited ({_rc_phrase(rc)})"
    if not self.recording_allowed:
        return reason
    tail = "\n".join(self._stderr_lines)
    if not tail.strip():
        return reason
    # A child's stderr is untrusted text that can echo a token or an
    # authority-bearing URL (a failed login prints the header it sent), and
    # both sinks below are real -- the reason travels to the session card and
    # the SEL, the debug line lands in the gateway log -- so redact before
    # either, and before any cut, which then cannot split a secret into a
    # half the redactor fails to recognise.
    from kiro_crew.security import redact

    tail = redact(tail)
    last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
    if last:
        logger.debug(
            "AcpRuntime (PID %s) exit stderr tail: %s",
            self._pid,
            last[:_STDERR_REASON_TAIL_CHARS],
        )
    cause = _proven_death_cause(tail)
    if cause is None:
        return reason
    evidence, hint = cause
    # The signature was matched on the whole line, so a cause past the cap
    # still earns its slot; only the EVIDENCE is cut. The hint is appended
    # after the cut so a long line cannot push the operator's pointer out of
    # the message it is the whole point of.
    if len(evidence) > _STDERR_REASON_TAIL_CHARS:
        evidence = evidence[:_STDERR_REASON_TAIL_CHARS] + "…"
    reason = f"{reason}: {evidence}"
    if hint:
        reason = f"{reason} — {hint}"
    return reason


def _returncode_label(self: AcpRuntime) -> str:
    """This runtime's exit status for the death log, or why there is none.

    ``_kill_inner`` marks the death BEFORE it signals and reaps (pending
    waiters must learn of the death first), so every kill of a live
    runtime reads ``returncode is None`` here. So does a reader crash or a
    broken pipe on a child that is still running, and a never-spawned
    runtime has no process to ask at all. None of the three is "died by
    signal, status unknown", so none of them prints a bare ``None``.
    """
    if self._process is None:
        return self._RC_NO_PROCESS
    rc = self._process.returncode
    return self._RC_NOT_REAPED if rc is None else str(rc)


def _compose_death_summary(self: AcpRuntime, reason: str, rc: str, tail: str) -> None:
    """Retain the one-line attribution ``death_summary()`` hands out.

    The parts are kept alongside it so the post-reap amendment can
    RECOMPOSE the line from them. Rewriting the composed text instead
    would search the child's stderr tail as well, and a tail that happened
    to carry this method's own ``[returncode=...]`` shape would be edited
    into an exit status -- the diagnostic corrupting the evidence it exists
    to carry.
    """
    self._death_reason = reason
    self._death_tail = tail
    self._death_summary = f"{reason} [returncode={rc}] stderr_tail: {tail}"


def _note_reaped_after_kill(self: AcpRuntime, rc: object) -> None:
    """Fill in the exit status the kill path's death line could not know.

    Called once the reap has completed, while the process handle is still
    held. The retained summary is amended because it OUTLIVES the log --
    it rides ``AcpProcessDied`` into a turn's error and a cron's
    ``last_error`` -- and one line is logged at the death's own severity
    so the gateway log holds the code too. Silent when the status is still
    unknown (both waits timed out): ``<not reaped>`` is then still true.
    """
    self._note_reaped(rc, after_kill=True)


def _note_reaped(self: AcpRuntime, rc: object, *, after_kill: bool) -> None:
    """Amend the death summary with *rc*; log it with the death's true origin.

    ``after_kill`` selects the wording, nothing else. The observed-exit path
    (the reader hitting EOF on a root that died from OUTSIDE -- an OOM kill,
    an operator's ``pkill``) must not log "reaped after kill": ``kill()``'s
    own contract is that a line saying "killed" names WHO killed, and a kill
    Kiro Crew never issued has no such name. Attribution is the whole reason
    the two paths log differently.
    """
    if rc is None or self._death_summary is None:
        return
    if self._death_label != self._RC_NOT_REAPED:
        return
    self._death_label = str(rc)
    self._compose_death_summary(self._death_reason, self._death_label, self._death_tail)
    log = logger.info if self._death_expected else logger.warning
    # Two LITERAL templates rather than one with the origin interpolated:
    # the raw template is what log filters and the tests select on, and
    # "reaped after kill" is the string they already match.
    if after_kill:
        log("AcpRuntime reaped after kill (PID %s): returncode=%s", self._pid, rc)
    else:
        log("AcpRuntime reaped after an observed exit (PID %s): returncode=%s", self._pid, rc)


def _mark_dead(
    self: AcpRuntime, reason: str, *, expected: bool = False, stdin_stalled: bool = False
) -> None:
    """Mark runtime dead, fail all pending requests, poison all session queues.

    ``stdin_stalled`` records that the death is a stdin stall with the child
    still alive (see ``stdin_stall_death``), BEFORE the queues are poisoned,
    so every session woken by the poison reads it.

    ``expected`` selects only the log severity: a deliberate teardown (a
    warm-pool TTL recycle, a session shutdown) logs at INFO, while every
    genuine death path (process exit, reader crash, broken pipe, ...) keeps
    today's WARNING. The default is False so any death path added later is
    a WARNING without having to opt in. Everything else — the ``_dead``
    early return, PID unshielding, failing pending futures, poisoning
    session queues — is identical on both paths.
    """
    if self._dead:
        return
    self._stdin_stall_death = stdin_stalled
    if stdin_stalled:
        self._stall_turn_sessions = frozenset(self._turn_active_sessions)
    # Read co-tenancy BEFORE the flag flips. Both readings go through
    # liveness -- ``outstanding_leases`` excludes a dead runtime's leases by
    # design, and ``is_alive()`` consults ``_dead`` -- so a count taken after
    # the assignment below is zero for every death, which would report every
    # shared process as single-tenant and defeat the attribution entirely.
    _leases_at_death = outstanding_leases(self)
    _acp_sessions_at_death = len(self._session_queues)
    self._dead = True
    # A process that already exited on its own is a genuine death being
    # reaped, not a teardown this caller initiated — refuse the downgrade
    # regardless of call site. This closes the race where a replacement
    # path observes is_alive() == False (returncode set by the child
    # watcher) and kill()s before the reader loop has marked the death.
    if expected and self._process is not None and self._process.returncode is not None:
        expected = False
    self._death_expected = expected
    # Release the sweep-protection shield on ANY death path (EOF, rc!=0,
    # stdout overrun, reader crash, broken pipe) — not just kill(). Otherwise
    # the dead PID lingers in _PROTECTED_PIDS forever and, after PID reuse,
    # could shield a genuinely-orphaned process from the orphan sweep.
    if self._pid:
        try:
            unregister_protected_pid(self._pid)
        except Exception:
            logger.debug(
                "AcpRuntime: unregister protected pid failed for %s", self._pid, exc_info=True
            )
    # Diagnostic context: process returncode + tail of captured stderr so
    # operators can tell an OOM/crash from a clean exit without DEBUG logs.
    rc = self._returncode_label()
    if self.recording_allowed:
        tail = " | ".join(self._stderr_lines[-5:]) if self._stderr_lines else "<none>"
    else:
        # Reader exceptions and child stderr can echo session content.
        # Restricted runs retain lifecycle facts, including the exit code.
        reason = "runtime stopped" if expected else "runtime failed"
        tail = "<not retained>"
        self._stderr_lines.clear()
    # Redact BEFORE composing: the summary outlives this method — it is
    # retained for death_summary(), appended to AcpProcessDied, and a
    # cron turn's failure stringifies that exception into job.last_error,
    # which persists to sandbox-visible crons.json. Child stderr is
    # external-subprocess output that can carry credential material
    # (same treatment as the send-path's 'ACP process exited' detail).
    from kiro_crew.security import redact

    tail = redact(tail)
    # Retain the summary for death_summary(): consumers that learn of the
    # death only through a poisoned queue (a live turn's frame wait) can
    # then attach WHO/WHY to their own error instead of raising bare.
    self._death_label = rc
    self._compose_death_summary(reason, rc, tail)
    # Classify the death ONCE, here, where it is detected -- and before the
    # pending futures are failed and the queues poisoned just below, so a
    # tenant woken by its poison sentinel can already read why. Every tenant
    # then reads this one record instead of forming its own account of a
    # process event it only saw one session's corner of.
    runtime_death.announce(
        self,
        reason=reason,
        expected=expected,
        leases=_leases_at_death,
        acp_sessions=_acp_sessions_at_death,
    )
    log = logger.info if expected else logger.warning
    log(
        "AcpRuntime dead (PID %s): %s [returncode=%s] stderr_tail: %s",
        self._pid,
        reason,
        rc,
        tail,
    )

    exc = AcpRuntimeDead(reason)
    for future in self._pending_requests.values():
        if not future.done():
            future.set_exception(exc)
    self._pending_requests.clear()
    self._pending_init_notifications.clear()
    # Also drop routed-request correlations: on death no reader will pop
    # them, and if a session is never destroyed the entry would otherwise
    # linger. unregister_session() also sweeps these per-session; this is
    # belt-and-suspenders for the process-death-before-response case.
    self._routed_requests.clear()

    for queue in list(self._session_queues.values()):
        try:
            queue.put_nowait(None)  # poison sentinel
        except asyncio.QueueFull:
            pass


async def _reap_after_cancelled_tracking(self: AcpRuntime, tracking: asyncio.Future[None]) -> None:
    """Let the tracking worker finish, then reap the child. Never raises.

    Run as its own task by the spawn cancellation guard, which shields it,
    so a cancellation aimed at the spawn cannot interrupt either step.
    ``asyncio.wait`` neither cancels *tracking* nor raises its exception; its
    outcome does not matter here, only that it is over before the untrack
    the reap performs.
    """
    await asyncio.wait({tracking})
    try:
        await self.kill(reason="reap after cancelled spawn tracking")
    except Exception:
        logger.debug(
            "AcpRuntime: kill after cancelled PID tracking failed for %s",
            self._pid,
            exc_info=True,
        )


async def _retire_tracking_after_exit(self: AcpRuntime) -> None:
    """Drop this root's registry entries once its exit is CONFIRMED, not inferred.

    Called from the two death paths that observe an exit instead of causing
    one: the reader loop's EOF branch, and the ``_exit_watch_loop`` that
    polls ``process.returncode`` so a death with stdout held open by a
    survivor is still seen. ``_kill_inner`` untracks after it
    reaps; a root killed from outside (an OOM kill, a ``pkill``, an operator)
    reached no such step, so its ``kiro_session_pids.txt`` / ``kiro_pids.txt``
    lines survived until the periodic sweep's next tick, bounded only by
    ``SessionCleanup.MAX_TICK_INTERVAL_SECS`` (300s). A registry line naming a
    dead pid is the hazard the start token exists to blunt, not one to leave
    standing for five minutes when the process that owns the line has already
    watched it die.

    A closed stdout is NOT an exit: a backend can close its pipe and keep
    running, and untracking a live process would hide it from every reaper
    for the host's uptime. So the exit is waited for, bounded by the same
    window the kill path gives a SIGKILLed child to be reaped, and the pid is
    re-probed after the wait. Any doubt -- the wait timed out, the pid still
    answers, the probe raised -- RETAINS the entries, which is the direction
    every reaper in ``session_pid`` already fails toward.

    The write itself is bound to the process, not the number: once the root
    is gone its number is free, and a replacement root this gateway spawns
    can be handed it before the write lands, so a prefix-matched untrack
    could erase the successor's only durable record. ``_untrack_root_by_identity``
    removes the session line only when its recorded start token is the one
    read at THIS spawn, and the bare root line only when the number is dead
    at the moment of a probe taken under the bare file's own lock -- a live
    holder of the number keeps its line. The root's own lines only: a
    descendant that outlived the root is reparented and still running, and
    its own ``kiro_pids.txt`` line is what the sweep reaps it by.

    Offloaded to a thread because the untrack takes the file locks the sweep
    contends for, and this runs on the event loop.
    """
    process = self._process
    if process is None:
        return
    pid = process.pid
    try:
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), timeout=self._KILL_REAP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.debug(
            "AcpRuntime: stdout closed but PID %s has not exited within %.1fs; "
            "leaving it tracked for the sweep",
            pid,
            self._KILL_REAP_TIMEOUT,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "AcpRuntime: could not confirm the exit of PID %s; leaving it tracked",
            pid,
            exc_info=True,
        )
        return
    # The wait just measured the status ``_mark_dead`` could not know (it ran
    # on EOF, before the reap): amend the retained summary the same way the
    # kill path does, so the ``AcpProcessDied`` a turn or a cron records
    # carries the real code instead of ``<not reaped>``. Worded as an
    # OBSERVED exit: nothing here issued a kill, and a log line that says
    # "killed" without naming who killed would defeat the attribution the
    # kill path's own docstring requires.
    self._note_reaped(process.returncode, after_kill=False)
    if platform_compat.pid_exists(pid):
        return
    try:
        retired = await asyncio.to_thread(_untrack_root_by_identity, pid, self._spawn_start_token)
    except Exception:
        logger.debug("AcpRuntime: PID untracking after exit failed for %s", pid, exc_info=True)
        return
    if not retired:
        logger.debug(
            "AcpRuntime: registry lines for PID %s were not provably this runtime's; "
            "leaving them for the sweep",
            pid,
        )
        return
    logger.info("AcpRuntime: retired registry tracking for PID %d after it exited", pid)


async def _drain_stderr(self: AcpRuntime) -> None:
    """Drain stderr to prevent subprocess blocking."""
    assert self._process and self._process.stderr
    stderr = self._process.stderr
    try:
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                if self.recording_allowed:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 20:
                        self._stderr_lines = self._stderr_lines[-20:]
                # Latch here, at the sink, because this is the only point at
                # which every line is guaranteed to have been seen. The
                # trim above is what makes it necessary: nobody asks about
                # auth until a request has already timed out, by which time a
                # chatty startup can have pushed the auth line out of the ring.
                # Deliberately does not log: the line below already emits this
                # text at debug, so a second record here would add no content
                # and only raise arbitrary matched stderr to a default-visible
                # level, against this sink's own convention. The condition is
                # surfaced where it is actionable instead -- as AcpAuthRequired.
                if not self._saw_auth_failure and is_auth_failure_output(text):
                    self._saw_auth_failure = True
                # Same sink, same reason as the auth latch: matched
                # unconditionally (not only when ``recording_allowed``), so a
                # restricted session that retains no stderr still gets the
                # actionable classification rather than a bare exit code.
                #
                # SCOPED TO THE PRE-INITIALIZE WINDOW, unlike the auth latch,
                # and the asymmetry is the point. A rejected credential does
                # not un-reject itself, so latching it for the runtime's life
                # is correct. This signature is not that: the child's OWN
                # sandbox can refuse mid-life when the HARNESS spawns a tool
                # subprocess, which says nothing about whether the agent
                # process can start. A life-long latch would turn the next
                # unrelated death -- a broken pipe, an OOM kill -- into a
                # permanent "your sandbox is broken", and permanently is
                # exactly how long the wrong verdict would last. A refusal
                # that genuinely stops the child from starting is always
                # printed before ``initialize`` completes, so the window that
                # matters closes there.
                if (
                    not self._first_session_ready
                    and not self._saw_sandbox_init_failure
                    and is_sandbox_init_failure_output(text)
                ):
                    self._saw_sandbox_init_failure = True
                # Same sink and the SAME pre-initialize window as the sandbox-init
                # latch, for the same reason: a delegate that will not exec fails
                # before startup completes, and a ``failed to launch kiro-cli-chat``
                # line after a healthy start would belong to some later unrelated
                # death, not to the agent process's own startup.
                if (
                    not self._first_session_ready
                    and not self._saw_delegate_launch_failure
                    and is_delegate_launch_failure_output(text)
                ):
                    self._saw_delegate_launch_failure = True
                if self.recording_allowed:
                    logger.debug("stderr: %s", text[:200])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # An overlong stderr line (ValueError / LimitOverrunError from
        # readline when no newline fits the buffer) or a low-level read
        # error must not kill this task with an unhandled exception. Log and
        # exit the drain cleanly rather than leaving a dead task behind.
        logger.debug(
            "stderr drain task exiting on error: %s",
            exc if self.recording_allowed else type(exc).__name__,
            exc_info=self.recording_allowed,
        )
