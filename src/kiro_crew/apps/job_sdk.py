"""Job SDK — app-scoped durable runs for long, user-initiated foreground work.

``CronSDK`` schedules work for later; this owns work a human started and is
watching NOW. The gap it closes is that the product had no server-side
representation of "a task of mine is running": the fact lived only in the React
component that started it, so navigating away destroyed the fact while the work
kept going, and the UI then reported the task as stopped. See
``docs/system-specs/features/app-sdk-durable-jobs-and-view-state.md``.

Five design points are load-bearing rather than incidental.

**P1 records that a run EXISTS and how it ended — nothing it produced.** There
is no ``params`` a caller passes in, and no ``progress`` or ``result`` a runner
reports out. A run's record holds its identity, its lifecycle and, if it failed,
one error string. That is deliberate and is the whole of what the originating
problem needs: the UI's question is "is my task still running", not "how far
along is it". The payload channels are structured data that must be sanitized
before it can be written or served, and P1 has NO consumer that reads them, so
they were carrying that cost for capability nobody calls. They return in P2,
designed against a real consumer, as types that are sanitized by construction
rather than by a rule each writer has to remember.

**A runner is REGISTERED, not passed per call.** ``register(kind, fn)`` binds a
kind to the callable that services it, once, at app init. ``start(kind, ...)``
then names the kind only. This is what lets a caller that cannot hold a Python
callable — the browser, and the startup reconciliation pass — address a run.

**Cancellation is cooperative and DECLARED.** A worker thread cannot be killed,
so ``cancel()`` can only ask. The SDK cannot inspect an arbitrary ``fn`` to find
out whether it ever checks, so cancellability is the app's assertion at
``register(..., cancellable=True)`` and defaults to False. A run recorded
``cancellable: false`` answers ``cancel()`` with ``False`` rather than
pretending, and the UI hides the control instead of offering a button that does
nothing.

**One writer per run file.** There is no lock helper beside ``atomic_write`` and
concurrent read-modify-write of one document is last-writer-wins, so each run is
its own file and writers never share a path: ``start`` writes the initial record
BEFORE handing off, the worker thread is the sole writer from then on, and
``cancel`` writes nothing at all (it sets an in-memory event; the worker records
the outcome at its next checkpoint). Reconciliation writes only records from a
process that is already gone. A requested cancel still has to be visible to a
reader before that checkpoint arrives, so it is DERIVED on read from the live
table -- ``cancelling_and_live_ids`` -- rather than written down. Reading it costs nothing
the rule protects; writing it would cost the rule.

**Claiming a run and LAUNCHING it are separate, and the interval between them is
named.** That interval contains a disk write, so it is not instantaneous, and the
run is already in the live table while its thread does not yet exist. Three
defects came from code that had no name for that state: cleanup joined a thread
that was never started, which raised and escaped the disable so the records were
never deleted; a start that had already claimed launched into an app being
disabled; and the record said ``running`` while no worker existed. So the record
starts at ``STARTING`` and the worker itself completes the transition to
``RUNNING``, the live entry carries ``started`` as the one authority on whether
there is a thread to join, and the launch is a GUARDED transition performed under
the same lock cleanup marks with -- not an unconditional call. Each of the three
questions is now answered by reading state rather than by assuming it.

That discipline settles writer-vs-WRITER, not reader-vs-writer: a reader still
races the one writer's temp-file-plus-rename. POSIX hides this because rename is
atomic for a reader, so the reads go through ``atomic_write``'s
``read_bytes_with_retry`` -- on Windows the same instant raises
``PermissionError`` at the reader, and a record read is not optional here
(``get`` answers the HTTP surface, and ``iter_runs`` feeds reconciliation).

Staleness is decided by ``_ORIGIN``, a token minted once per gateway process —
not by pid, which can be reused by the very process doing the reconciling.

That token is the GATEWAY process, which also bounds what this SDK describes, and
the bound is per RUNNER rather than per app. A registration only exists where the
callable lives: ``register`` binds a kind to a callable held here in the gateway,
so only runners registered from gateway-side code -- an app's hooks, its routes --
are ones this SDK can execute or reconcile. An app declaring ``backend.entryPoint``
is additionally spawned as its own OS process (``apps/backend.py``,
``start_app_backend``), and that does NOT remove its SDK: the context builder gates
``ctx.job`` on ``permissions.jobs`` alone, so such an app still receives a
``JobSDK`` and the gateway hooks still publish it behind the shared routes. What it
cannot do is register a runner from that separate process, so work executing there
is invisible here. The consequence is not merely that such work is unsupported: on
a restart that ``_reap_stale_app_backends`` leaves the backend alive through,
``reconcile`` marks any record it left behind ``INTERRUPTED``, a terminal state no
later pass revisits.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Identity of THIS gateway process. A run record carrying a different origin
#: belongs to a process that no longer exists, which is what makes staleness
#: decidable without trusting a pid (pids are reused, and the reconciling
#: process could hold the very pid a stale record names).
_ORIGIN = uuid.uuid4().hex

QUEUED = "queued"
#: Claimed, durable, and NOT yet executing: the record is on disk and the run
#: owns its dedupe key, but its worker thread has not been started. This state
#: exists because the interval between claiming a run and launching it is real --
#: it contains a disk write -- and three separate defects came from code that had
#: no way to name it. Cleanup joined a thread that was never started; a start that
#: had already claimed launched into an app that was being disabled; and the
#: record said ``running`` while no worker existed. A state that can be READ under
#: the same lock the transitions take makes each of those decidable instead of
#: guessable.
STARTING = "starting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

#: A run in a terminal state is never resumed and never reconciled.
TERMINAL_STATES = frozenset({DONE, FAILED, CANCELLED, INTERRUPTED})

#: Why a run was reconciled, on the record. A closed SET rather than a bool
#: because the not-registered case already covers two different events -- the app
#: was disabled, or the kind was removed -- so a boolean would freeze that
#: conflation on the day the field is introduced. Adding the third value later
#: costs one constant and one table entry.
CAUSE_PROCESS_GONE = "process_gone"
CAUSE_RUNNER_UNREGISTERED = "runner_unregistered"
#: The record was minted by THIS process and its worker is gone from the live
#: table without a terminal write -- ``_write_terminal`` spent both attempts and
#: said so. Distinct from the two above because ``origin == _ORIGIN`` is positive
#: evidence that no restart happened, so neither restart cause can be true.
#:
#: What this cause leaves open depends on the OTHER axis, so the message takes its
#: consequence clause from ``interrupted_from`` rather than asserting one: a
#: ``running`` record genuinely leaves the outcome unknown, while a ``starting`` one
#: does not (see ``_WRITE_LOST_BODY_NEVER_RAN``).
CAUSE_TERMINAL_WRITE_LOST = "terminal_write_lost"

#: How far a run had got, in words -- and deliberately COARSER than
#: ``interrupted_from`` itself. A consumer needs ``queued`` apart from
#: ``starting`` (only one of them owns a dedupe key), but a person reading a
#: message does not: both mean no line of the runner's body ran. Keeping the
#: field's granularity and the sentence's granularity separate is the reason the
#: message is derived here rather than being the only record of what happened.
_PROGRESS_PHRASE = {
    QUEUED: "had not started yet",
    STARTING: "had not started yet",
    RUNNING: "was running",
}

#: The ONE place an interruption's message is composed. A table keyed on cause,
#: not a nested conditional: with two axes the conditional form needs four arms
#: and grows multiplicatively, while this grows by one line per cause. The
#: ``process_gone`` + ``running`` cell reproduces the message this SDK shipped
#: with, byte for byte, because that cell was the only one that was ever true.
_INTERRUPT_MESSAGE = {
    CAUSE_PROCESS_GONE: "the gateway restarted while this {progress}",
    CAUSE_RUNNER_UNREGISTERED: (
        "the gateway restarted while this {progress}, and no runner is registered for {kind!r}"
    ),
    CAUSE_TERMINAL_WRITE_LOST: (
        "this run's final state could not be written while this {progress}, so {consequence}"
    ),
}

#: Statuses a ``terminal_write_lost`` record can carry that prove the body NEVER RAN,
#: rather than leaving the outcome unknown. Completing the ``starting`` -> ``running``
#: transition is a PRECONDITION for calling the runner: if that write fails the run is
#: marked failed and the runner is never invoked. So a record still at ``starting``
#: (or ``queued``) whose terminal write was also lost proves execution never began --
#: there is nothing unknown about it, and saying otherwise would be this module's own
#: defect, a record declining to state what it knows.
_WRITE_LOST_BODY_NEVER_RAN = frozenset({QUEUED, STARTING})
_WRITE_LOST_NEVER_RAN = (
    "its body never ran: the transition to running is a precondition for calling the "
    "runner, and this record never reached it"
)
_WRITE_LOST_OUTCOME_UNKNOWN = "whether its work finished is not known"

#: Used when a record carries a status this build does not know -- a file written
#: by a newer gateway, or hand-edited. The pass must still resolve it, and the
#: message must not claim a progress it cannot support.
_UNKNOWN_PROGRESS_PHRASE = "had not finished"

#: Length of an SDK-minted run id (``uuid.uuid4().hex``). Validated, not just
#: assumed: a caller reaches ``{run_id}`` on the HTTP surface directly, and a
#: hex string of any other length is not a run this SDK ever minted. Checking
#: only the ALPHABET let a very long id through, and the oversized filename it
#: built raised ``ENAMETOOLONG`` -- an ``OSError`` no read handler names -- which
#: surfaced as a 500 where a 404 is the honest answer.
_RUN_ID_LEN = 32

#: How long disable waits for one worker to notice its cancel signal. Bounded:
#: a runner that never polls its handle must not be able to block an app's
#: disable indefinitely, so it is reported instead.
_CLEANUP_JOIN_SECS = 5.0

#: Upper bound on a record file the disable scan will open. A ``JobRun`` is a
#: small flat JSON document (its one free-text field is clipped at ingest), so
#: a megabyte is generous headroom; anything larger in the runs directory is
#: not a record this SDK wrote and is refused unopened -- the scan's reads walk
#: agent-writable filenames, and the bound is what keeps a planted giant file
#: (or a device node reached some other way) from stalling disable.
_MAX_RECORD_BYTES = 1 << 20

_RUNS_DIRNAME = "jobs"


class JobError(RuntimeError):
    """Base class for Job SDK refusals."""


class UnknownJobKind(JobError):
    """Raised when starting a kind that has no registered runner."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _redact(text: str) -> str:
    """Scrub the ONE runner-produced string a record carries: its error.

    A failing runner's exception text can quote back a command line carrying a
    credential, so the same chain the app route boundaries apply runs here, at
    the point the text stops being local. Applied at INGEST rather than on the
    way out, so the record on disk is clean too.
    """
    try:
        out, _ = redact_credentials(text)
        out, _ = redact_exfiltration_urls(out)
        return out
    except Exception:  # noqa: BLE001 - redaction must never mask the error itself
        logger.debug("job text redaction failed", exc_info=True)
        return text


def _interrupt_error(cause: str, interrupted_from: str, kind: str) -> str:
    """Compose an interruption's human-readable message. The ONLY site that does.

    Two facts go in, and they are independent because they describe different
    TIMES: ``interrupted_from`` is how far the run got before it stopped being
    accounted for, ``cause`` is why nobody can account for it now. NOT "before its
    process died" -- ``CAUSE_TERMINAL_WRITE_LOST`` exists precisely for a run whose
    process is still alive and only lost the write. Neither fact implies the other,
    so both are on the record; the sentence is derived from them here rather than
    being the place they are stored.

    An unrecognised ``cause`` falls back to the process-gone template instead of
    raising: this runs inside the pass that clears stuck ``running`` records, and
    a record with a cause this build does not know must still be resolved.
    """
    template = _INTERRUPT_MESSAGE.get(cause) or _INTERRUPT_MESSAGE[CAUSE_PROCESS_GONE]
    progress = _PROGRESS_PHRASE.get(interrupted_from, _UNKNOWN_PROGRESS_PHRASE)
    # Derived from the SECOND axis, not asserted: only `terminal_write_lost` has a
    # `{consequence}` slot, and the other templates ignore the extra keyword. An
    # unknown status is treated as outcome-unknown, which is the honest default when
    # the progress axis itself cannot be read.
    consequence = (
        _WRITE_LOST_NEVER_RAN
        if interrupted_from in _WRITE_LOST_BODY_NEVER_RAN
        else _WRITE_LOST_OUTCOME_UNKNOWN
    )
    return template.format(progress=progress, kind=kind, consequence=consequence)


#: The ONE verdict every returned suspendable gets, worded as the contract violation
#: it is. It deliberately does NOT say the body never ran: for a drained generator it
#: did, and this SDK cannot tell that case from a suspendable closed before it began.
#: Saying "never ran" would replace a false success with a false explanation, which is
#: the same defect one layer down.
_SUSPENDABLE_RETURNED = (
    "{name}, which a runner must not return: this SDK never drives what a runner "
    "hands back, and the object itself cannot say what it did -- one that ran to "
    "completion and one closed before it began are identical from here"
)

#: The suspendable carriers, matched by kind ALONE. There is deliberately no state
#: table: the state cannot answer the question the record needs answered.
#: ``CLOSED``/``frame is None`` covers BOTH a suspendable drained to completion and one
#: closed before it ever began, and those two are indistinguishable from the object --
#: verified, not assumed (see :func:`_undriven_result` for what was measured and
#: rejected). So the classifier stops inferring history and states a rule instead.
#:
#: Two rejected alternatives, both of which look more precise and are traps:
#:
#: * the ``cr_frame``/``gi_frame``/``ag_frame`` attribute with an ``f_lasti``
#:   comparison. ``f_lasti`` is ``-1`` for an unstarted frame up to 3.10 and an
#:   instruction offset (``0``) from 3.11, so a ``< 0`` test silently matches NOTHING
#:   on 3.12 -- every ``async def`` runner would be recorded ``done`` again, the very
#:   defect this module exists to remove.
#: * ``gc.get_referents``, which DOES separate the two closed histories on 3.12:
#:   ``('str', 'str')`` for a drained generator against ``('code', 'function', 'str',
#:   'str')`` for one closed unstarted, stable across trials, creation order and an
#:   explicit ``gc.collect()``. It is still refused, because it is an undocumented
#:   interpreter internal, generators were restructured in 3.11 with lazily-created
#:   frames, and CI runs 3.10 AND 3.12. A classifier that is right on one interpreter
#:   and wrong on another writes false records confidently, which is worse than
#:   declining to classify.
_SUSPENDABLE_KINDS: tuple[tuple[str, Any], ...] = (
    ("a coroutine", inspect.iscoroutine),
    ("a generator", inspect.isgenerator),
    ("an async generator", inspect.isasyncgen),
)


def _undriven_result(result: Any) -> str:
    """Name why a runner's return value is not a completed unit of work, else ``""``.

    The counterpart to :func:`_lazy_call_shape`, and the load-bearing half. That one
    inspects the callable's SHAPE at registration, which is a proxy: it can only
    refuse the spellings somebody thought of. This one reads the FACT, at the one
    moment it exists -- whatever the runner handed back, once.

    ONE PROTOCOL QUESTION WITH TWO CARRIERS, then allow. The question is "is this
    thing finished?", and two kinds of object answer it:

    * a SUSPENDABLE cannot answer it, so it is refused outright;
    * a FUTURE answers it by ``done()``, and then by how it settled.

    **Suspendables, ONE verdict, and this reverses an earlier decision in this same
    change set.** Earlier rounds read the frame state and allowed ``CLOSED`` on the
    reasoning that a closed frame ran to completion. That was wrong, and the reason is
    worth stating because it is the module's own thesis turned on itself: ``CLOSED``
    covers a suspendable drained to completion AND one closed before it ever began, so
    allowing it recorded a completion nobody observed -- exactly the class of claim
    this module exists to remove. The two cases were then MEASURED rather than assumed
    to differ: no public attribute, ``dir()`` entry, code object, ``gi_frame``,
    ``gi_running`` or ``gi_yieldfrom`` separates them.

    ``gc.get_referents`` does separate them on 3.12, reliably, and is refused anyway --
    see :data:`_SUSPENDABLE_KINDS` for the measurement and why depending on it would be
    a defect rather than a fix.

    So the rule replaced the inference: a returned suspendable is a CONTRACT VIOLATION
    whatever its state. The reason string says only what this SDK knows -- that it will
    not accept the object and cannot tell what it did. It deliberately does NOT say the
    body never ran, because for a drained generator it did; that wording would swap a
    false success for a false explanation. Nothing legitimate is refused: the SDK
    discards return values by design, so returning a suspendable cannot serve a runner
    even in principle.

    An async generator needs no separate case now. It used to have one because its
    state was not portably readable (``inspect.getasyncgenstate`` is 3.12+ while this
    module supports 3.10) -- and no state is read any more, so the special case that
    existed to avoid a version-dependent record dissolves into the general rule.

    **Futures, four verdicts, and deliberately NOT flattened.** Unlike a suspendable, a
    future does answer the question, so the distinctions it draws are kept. ``done()``
    False is pending, so unfinished. ``done()`` True splits, because ``done()`` answers
    "is it settled", not "did it succeed" -- and ``cancelled()`` MUST be asked before
    ``exception()``, since on a cancelled future ``exception()`` RAISES
    ``CancelledError`` rather than returning it. That is a ``BaseException``, so it
    would sail through ``_execute``'s ``except Exception`` and crash the function whose
    job is classifying failures.

    **An unsettled future is FAILED, and that verdict is true rather than
    convenient.** The runner's contract is to do its work before returning, so
    handing back something unfinished violates it -- ``failed`` is what this SDK
    observed about the RUN, not a guess about the work.

    What this SDK cannot do is STOP that work. An unsettled
    ``concurrent.futures.Future`` stands for a thread it does not own, and
    terminalizing the run releases the dedupe key -- so an owner who retries gets a
    second run overlapping work that never stopped. Relabelling the status would not
    change that: the overlap is a consequence of the contract violation, not
    something the record misstates. So the reason string SAYS the work may still be
    running, because the owner reading that record is the only party who can judge
    whether a retry is safe. The missing capability -- no way to stop work a runner
    spawned outside this thread -- is filed rather than pretended away.

    **Why this is complete, which after nine review rounds is a fair thing to ask.**
    Not because cases stopped being found, but because the two carriers are now
    answered on different grounds. Suspendables need no enumeration at all: every
    state, known or added by a future Python, reaches the same verdict, so there is no
    fourth state to discover and no state table to go stale. The future protocol offers
    pending, cancelled, exception and result, and this maps every one of those.

    **Default: allow.** Safe now in a way the old default was not. The old code asked
    ``isawaitable`` first, so a non-awaitable future reached the default and was
    called fine; the future carrier now catches it by ``done()``, so what still
    reaches the default is genuinely a plain return value.
    """
    # Carrier 1: a suspendable, refused by KIND -- its state is not consulted,
    # because no state it can report answers "did the work happen".
    for name, is_kind in _SUSPENDABLE_KINDS:
        if is_kind(result):
            return _SUSPENDABLE_RETURNED.format(name=name)

    # Carrier 2: a future, which answers by `done()` and then by how it settled.
    done = getattr(result, "done", None)
    if callable(done):
        if not done():
            return (
                "a future that is not settled, so the runner returned before its work "
                "finished; that work may still be running somewhere this SDK does not "
                "own and cannot stop, so a retry of this run can overlap it"
            )
        cancelled = getattr(result, "cancelled", None)
        if callable(cancelled) and cancelled():
            return "a cancelled future, so the work it stood for never completed"
        exception = getattr(result, "exception", None)
        if callable(exception):
            # Safe without a timeout because ``done()`` is established, so it cannot
            # block. Retrieving it also MARKS it retrieved, which suppresses the
            # "never retrieved" warning :func:`_close_quietly` handles for the
            # unsettled case -- classification and quiet retirement meet here.
            exc = exception()
            if exc is not None:
                return (
                    f"a future that failed with {type(exc).__name__}, "
                    "so the work it stood for ran and raised"
                )
        return ""

    # Carries neither answer and still needs driving: nothing here will do it.
    if inspect.isawaitable(result):
        return _SUSPENDABLE_RETURNED.format(name="a bare awaitable")

    return ""


def _close_quietly(result: Any) -> None:
    """Close an undriven result so it does not warn from the garbage collector.

    A coroutine that is never awaited emits ``RuntimeWarning: coroutine ... was
    never awaited`` when collected, at a point far from the run that caused it.
    The record already says what happened, so the warning is noise pointing at
    the wrong place.

    Two shapes, because they do not share a method. Coroutines and generators
    expose a synchronous ``close``. A future does NOT -- neither ``asyncio.Future``
    nor ``concurrent.futures.Future`` has ``close``, so an earlier version of this
    docstring named futures among the closable and was wrong about the one object
    the sentence was about. A pending future is retired with ``cancel`` instead,
    which is what stops it warning that its exception was never retrieved. An
    async generator's ``aclose`` is itself a coroutine needing a loop, so it is
    left to the interpreter rather than driven from this thread.
    """
    for method in ("close", "cancel"):
        handler = getattr(result, method, None)
        if not callable(handler):
            continue
        try:
            handler()
        except Exception:  # noqa: BLE001 - closing is hygiene, never the outcome
            logger.debug("could not retire an undriven job result", exc_info=True)
        return


def _lazy_call_shape(fn: Any) -> str:
    """Name the shape of a callable whose CALL does not run its body, else ``""``.

    Three shapes return a lazy object instead of doing the work: a coroutine
    function, an async generator function, and a plain generator function. The
    SDK calls its runner on a worker thread and discards the return value, so for
    all three the body never executes. Before the execution-side check existed
    such a run was then recorded ``done``; today :func:`_undriven_result` reads the
    returned object and records ``failed``, so this guard is a fast, friendly error
    at registration rather than the safety property.
    One defect with three spellings, so the check names the class rather than the
    one spelling that was reported.

    A callable OBJECT is examined through its ``__call__``, because that is what
    invocation actually reaches -- ``inspect`` reports the instance itself as an
    ordinary object, so a check written against bare functions passes it through.
    That was the gap two reviewers found in the first version of this guard.

    No legitimate runner is refused by this: a callable whose invocation returns a
    lazy object cannot do the work the SDK is calling it to do, whatever it is.
    """
    for target in (fn, getattr(fn, "__call__", None)):
        if target is None:
            continue
        if inspect.iscoroutinefunction(target):
            return "a coroutine function"
        if inspect.isasyncgenfunction(target):
            return "an async generator function"
        if inspect.isgeneratorfunction(target):
            return "a generator function"
    return ""


@dataclass
class JobRun:
    """One run's durable record. Serialized whole; never partially updated.

    ``error`` is the ONLY field a runner supplies. Everything else is minted by
    the SDK, which is what makes the sanitize rule one line rather than a list
    somebody has to remember to extend.
    """

    run_id: str
    app: str
    kind: str
    status: str = QUEUED
    origin: str = ""
    pid: int = 0
    dedupe_key: str = ""
    cancellable: bool = False
    created_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    error: str = ""
    #: Set only by ``reconcile``: the status this run held when a process that no
    #: longer exists was executing it. Structured because a CONSUMER acts on it --
    #: a run interrupted from ``running`` may have side effects already committed,
    #: while one interrupted from ``queued`` or ``starting`` provably has none, and
    #: those need different recovery. Before this field the pass overwrote
    #: ``status`` in place and chose ``error`` from whether a runner was
    #: registered, so all three collapsed into one record that read "while this was
    #: running" even for a run whose worker thread was never started.
    interrupted_from: str = ""
    #: Set only by ``reconcile``: why the run could not be resumed, from the
    #: ``CAUSE_*`` set. Orthogonal to ``interrupted_from`` -- that says how far the
    #: run got, this says whether the kind can be serviced now -- so a consumer
    #: deciding "offer a retry" reads this one and a consumer deciding "warn about
    #: partial work" reads the other.
    interrupt_cause: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRun:
        """Build a record from disk, tolerating a hand-edited or older file.

        Unknown keys are dropped rather than raising: a run record is data the
        gateway re-reads across upgrades, so one unexpected field must not make
        an app's whole run history unreadable.

        A body that is not an OBJECT breaks that promise from the other side. A
        file holding ``[]`` or ``"x"`` or ``5`` is valid JSON, so it survives the
        parse, and then ``.items()`` raises ``AttributeError`` -- which neither
        reader's handler names, so it escaped as a 500 from the route and
        ABANDONED the whole reconciliation scan, stranding every later record of
        that app. Refused as ``ValueError`` because that is the failure both
        readers already treat as "this one record is unusable", so the blast
        radius is the one file rather than the pass.

        A WRONG-TYPED field does the same damage one level in: a record whose
        ``error`` is a number reached ``_persist``, where the slice after the
        redaction raised ``TypeError`` outside that method's own try. So each
        known field is coerced to its declared type HERE, at the single point
        foreign data enters, and a value that cannot be coerced falls back to the
        field's default. Every consumer downstream then gets the type it is
        written against, instead of each one needing its own defence.
        """
        if not isinstance(data, dict):
            raise ValueError(f"job record is {type(data).__name__}, not an object")
        kwargs: dict[str, Any] = {}
        for name, spec in cls.__dataclass_fields__.items():
            if name not in data:
                continue
            value = data[name]
            want = spec.type
            # ``bool`` before ``int``: bool IS an int subclass, so testing int
            # first would silently accept True for a pid.
            if want == "bool":
                if isinstance(value, bool):
                    kwargs[name] = value
            elif want == "int":
                if isinstance(value, int) and not isinstance(value, bool):
                    kwargs[name] = value
            elif isinstance(value, str):
                kwargs[name] = value
        kwargs.setdefault("run_id", "")
        kwargs.setdefault("app", "")
        kwargs.setdefault("kind", "")
        return cls(**kwargs)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


class JobHandle:
    """What a runner is handed: the cancel signal it must poll.

    ``cancelled`` is a ``threading.Event`` and checking it is the runner's own
    responsibility — the SDK has no way to interrupt a thread that never looks.

    There is deliberately no ``progress()`` in P1. With no progress channel the
    worker writes its record exactly ONCE, terminally, so the record has a
    single writer over its whole life instead of a stream of mid-run mutations
    each needing the discard check to be right.
    """

    def __init__(self, run: JobRun) -> None:
        self.cancelled = threading.Event()
        #: Set when this run's record has been deliberately dropped (app
        #: disable). Checked INSIDE the guarded writer, under the same lock the
        #: discard is set with -- checking it here and writing afterwards was a
        #: check-then-act race: cleanup could delete the file in between and the
        #: write would recreate it, because ``JobStore.write`` mkdirs and writes
        #: unconditionally and cannot tell a first write from a resurrection.
        self.discarded = threading.Event()
        self._run = run

    @property
    def run_id(self) -> str:
        return self._run.run_id

    @property
    def kind(self) -> str:
        return self._run.kind

    @property
    def status(self) -> str:
        return self._run.status


#: A runner receives its handle and nothing else. P1 has no parameter channel:
#: caller-supplied ``params`` was the other half of what made a record hold
#: arbitrary nested data, and it returns in P2 with the payload channels.
JobFn = Callable[["JobHandle"], Any]


@dataclass
class CleanupResult:
    """What a disable actually achieved.

    ``still_running`` is a field rather than a log line because a cleanup that
    left app code executing must not be reportable as clean -- the caller has to
    be able to say so in the disable result.
    """

    removed: int = 0
    failed: int = 0
    still_running: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.failed and not self.still_running


@dataclass
class _Runner:
    fn: JobFn
    cancellable: bool


@dataclass
class _Live:
    handle: JobHandle
    thread: threading.Thread
    #: Whether ``thread.start()` has actually run. The ONE authority on "is there
    #: a thread to wait for", flipped only inside the lock, because cleanup makes
    #: a decision from it and a bool it has to infer is what it got wrong before:
    #: it joined every entry it snapshotted, and ``Thread.join()`` on a thread that
    #: was never started raises ``RuntimeError``, which escaped the disable and
    #: left the app's records in place with its worker running. Kept here rather
    #: than read off the record because cleanup never reads the record, and the
    #: two answers must not be able to disagree.
    started: bool = False


class JobStore:
    """One JSON file per run under ``<app data dir>/jobs/``.

    File-per-run rather than one document: ``atomic_write`` gives crash-safety
    (no reader sees a torn file) but not mutual exclusion, so two writers on one
    path would silently drop the loser's update. Separate paths remove the race
    instead of needing a lock the tree does not offer.
    """

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / _RUNS_DIRNAME

    def _path(self, run_id: str) -> Path:
        # run ids are SDK-minted hex; reject anything else rather than letting a
        # caller-supplied id become a path. LENGTH is checked as well as the
        # alphabet: `{run_id}` is reachable on the HTTP surface, and a very long
        # all-hex id passed the alphabet check, built a filename over the OS
        # limit, and raised ENAMETOOLONG -- an OSError no read handler names, so
        # it left as a 500 instead of the 404 an unknown id deserves.
        if len(run_id) != _RUN_ID_LEN or not all(c in "0123456789abcdef" for c in run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.dir / f"{run_id}.json"

    def write(self, run: JobRun) -> None:
        run.updated_at = _now()
        path = self._path(run.run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(run.to_dict(), indent=1))

    def read(self, run_id: str) -> JobRun | None:
        try:
            # ``read_bytes_with_retry``, not ``read_text``, for two reasons a
            # POSIX-only run never shows. A worker replacing this file races a
            # reader here, and on Windows ``os.replace`` makes that reader fail
            # with ``PermissionError`` while a plain rename does not -- the
            # read-side twin of the retry ``atomic_write`` already applies to the
            # write. And the decode is explicit because ``atomic_write`` emits
            # UTF-8 while ``read_text`` would decode in the host LOCALE, so a
            # non-UTF-8 Windows console would mis-read a redacted non-ASCII
            # string. Reached only off the loop (the routes offload every call),
            # which is what lets the helper's sleep apply at all.
            raw = read_bytes_with_retry(self._path(run_id)).decode("utf-8")
        # Only the damage that genuinely means "no such record": the file is not
        # there, its parent is not a directory, or its bytes are not the UTF-8
        # JSON this store writes (``ValueError`` covers the ``UnicodeDecodeError``
        # from the decode above and the malformed id ``_path`` rejects).
        #
        # ``PermissionError`` is deliberately NOT here, even though the comment
        # above is about it. On Windows a concurrent ``os.replace`` makes a reader
        # fail with a sharing violation that is TRANSIENT -- ``read_bytes_with_retry``
        # exists to outlast it, and re-raises only once its budget is spent or when
        # it is called on the event loop, where the budget cannot be spent at all.
        # Returning ``None`` there would answer "this run does not exist" about a
        # record that is present and intact, so a polling client reading 404 as
        # "gone" would drop live work. The same argument covers the rest of
        # ``OSError`` -- ``EIO``, ``EMFILE``, ``EBUSY`` are facts about the host,
        # not about whether this run was ever started. They belong to the caller,
        # which is why ``get`` lets them out and the route answers 500: an honest
        # "ask again" instead of a confident wrong answer.
        #
        # This is narrower than the sibling ``iter_runs`` scan below on purpose.
        # That scan is a partial result -- skipping one damaged file still returns
        # the others -- while this is a targeted lookup whose ``None`` asserts
        # absence, and absence is not something an unreadable file establishes.
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return None
        try:
            return JobRun.from_dict(json.loads(raw))
        except (TypeError, ValueError):
            logger.warning("unreadable job run record: %s", run_id)
            return None

    def iter_runs(self) -> Iterator[JobRun]:
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.glob("*.json")):
            try:
                # Same Windows window as ``read``, but here it failed SILENTLY
                # rather than loudly: ``PermissionError`` subclasses ``OSError``,
                # so a record being replaced by its own worker was skipped as
                # "unreadable" and simply vanished from this listing -- and a
                # record missed here is a record reconciliation does not resolve,
                # so a stale ``running`` would survive the boot meant to clear it.
                raw = read_bytes_with_retry(path).decode("utf-8")
                run = JobRun.from_dict(json.loads(raw))
            except (OSError, TypeError, ValueError):
                logger.warning("skipping unreadable job record: %s", path.name)
                continue
            yield run

    def remove_all(self) -> tuple[int, int]:
        """Delete every record. Returns ``(removed, failed)``.

        The failure count is returned rather than swallowed: reporting only the
        successes let a partial delete read as a clean one, so disable would
        claim the app's runs were gone while records remained. The cron contract
        this mirrors reports a failed cleanup, and so does this.
        """
        removed = 0
        failed = 0
        if not self.dir.is_dir():
            return 0, 0
        for path in list(self.dir.glob("*.json")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                failed += 1
                logger.warning("could not remove job record: %s", path.name)
        return removed, failed


class JobSDK:
    """App-scoped durable runs. One instance per app, for the gateway's life."""

    def __init__(self, app_name: str, data_dir: Path) -> None:
        self._app_name = app_name
        self._store = JobStore(data_dir)
        self._runners: dict[str, _Runner] = {}
        self._live: dict[str, _Live] = {}
        #: (kind, dedupe_key) -> run_id for runs live in THIS process. Held in
        #: memory rather than derived from the store so the dedupe check and the
        #: claim can happen in ONE critical section: the previous version read
        #: the disk between them, so two near-simultaneous starts both saw no
        #: owner and both ran -- exactly the double-click case dedupe exists to
        #: stop.
        self._keys: dict[tuple[str, str], str] = {}
        # A plain threading lock: it guards two small dicts with no awaits
        # inside, and an asyncio primitive here would bind this SDK to the loop
        # that happened to construct it.
        self._lock = threading.Lock()
        #: Set by ``remove_all_async``, under the lock, BEFORE it snapshots the
        #: live table. Disable has to be terminal for this SDK instance: the
        #: route guard re-reads the manifest, but an app's own code holds a
        #: reference to this object, so without this flag a ``start`` racing
        #: cleanup could claim and spawn a worker after the snapshot was taken
        #: and leave a disabled app doing real work with no record. Checked in
        #: the SAME critical section as the dedupe claim, so there is no window
        #: between the check and the claim.
        self._closed = False

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def store(self) -> JobStore:
        return self._store

    # ── The one writer ──

    def _persist(
        self,
        run: JobRun,
        handle: JobHandle | None = None,
        *,
        only_if_not_terminal: bool = False,
    ) -> bool:
        """Write a run's record. The ONLY path that writes one.

        Three callers used to write directly -- start, the worker's terminal
        write, and reconcile (a fourth, progress, is gone with the progress
        channel) -- and each had to remember the same three rules. Two review
        rounds found a different one missed each time, so the rules live here
        instead:

        * the discard check and the write happen under ONE lock acquisition, the
          same lock ``remove_all_async`` sets ``discarded`` with, so cleanup can
          no longer land between a caller's check and its write and have the
          record recreated;
        * a serialization or I/O failure returns ``False`` instead of raising, so
          a caller's bookkeeping (the live table, the dedupe claim) can never be
          skipped by an exception escaping mid-cleanup;
        * the record is JSON-safe by construction: every field except ``error``
          is minted by the SDK from a str, an int or a bool.

        ``only_if_not_terminal`` ENFORCES THE MODULE'S OWN INVARIANT that a record
        in :data:`TERMINAL_STATES` is never revisited. It is not a new rule and not
        a heuristic: :meth:`reconcile` decides from a snapshot and writes later, so
        a worker that finished in between had its true terminal record overwritten
        by a false one. That is why the re-read is HERE rather than next to the
        decision -- ``self._lock`` is not reentrant, so this is the only place the
        re-read and the write are one atomic step. A check near the decision would
        narrow the window; it would not close it.

        The re-read costs one file read per non-terminal record, on the boot pass
        only, inside a lock this method already holds across a file write.

        Returns True when the record is on disk. A refusal -- discarded, already
        terminal, or a failed write -- returns False, and every caller treats that
        the same way: do not count it, do not audit it as done.
        """
        # INVARIANT: nothing a runner supplied reaches disk unsanitized. This is
        # ONE line because ``error`` is the only field a runner supplies, and
        # that is the point: the earlier backstop scrubbed a hand-written LIST
        # (step, error, lines, params, result), and four rounds each found a
        # different member missing. A funnel with one input cannot.
        run.error = _redact(run.error)[:2000]
        with self._lock:
            if handle is not None and handle.discarded.is_set():
                return False
            if only_if_not_terminal:
                # Re-read INSIDE the write's lock: the caller's snapshot is stale by
                # construction, and the worker's own terminal write goes through
                # this same lock, so a record that reads terminal here is finished
                # and must not be revisited.
                latest = self._store.read(run.run_id)
                if latest is not None and latest.is_terminal:
                    return False
            try:
                self._store.write(run)
                return True
            except Exception:  # noqa: BLE001 - a write failure is a result, not a crash
                logger.exception("could not persist job record %s", run.run_id)
                return False

    # ── Registration ──

    def register(self, kind: str, fn: JobFn, *, cancellable: bool = False) -> None:
        """Bind ``kind`` to the callable that services it.

        Call from the app's ``on_startup`` hook. ``cancellable=True`` is the
        app's assertion that ``fn`` polls ``handle.cancelled`` at checkpoints;
        the SDK cannot verify it, so the consumer's migration checklist has to
        name those checkpoints.

        A CALLABLE WHOSE INVOCATION DOES NOT RUN ITS BODY IS REFUSED HERE, at
        registration, rather than failing at run time. ``_execute`` calls
        ``fn(handle)`` on a worker thread and discards the return value by design,
        and three shapes return a lazy object instead of doing the work: a
        coroutine function, an async generator function, and a plain generator
        function. For each, the object is dropped and no line of the runner's body
        ever runs. Such a run USED to be recorded ``done`` -- a run reporting
        success having executed nothing, and silent, since an un-awaited coroutine
        warning goes to the gateway log at most. That is now caught at execution by
        ``_undriven_result`` and recorded ``failed``, so refusing here is the early
        friendly error and not the thing that makes the record honest. The check
        goes through ``__call__`` as well as the object itself, so a callable
        instance with an ``async def __call__`` is caught too.

        Refusing is deliberately not the same as supporting: driving a coroutine
        correctly means deciding which loop owns it, how cancellation crosses
        the thread boundary, and what a blocking runner does to that loop, and
        those are design questions this refusal does not answer. It answers only
        the question a caller cannot currently ask -- "did my runner run" -- at
        the one point where the answer is still cheap.
        """
        if not kind:
            raise ValueError("job kind must be a non-empty string")
        lazy = _lazy_call_shape(fn)
        if lazy:
            raise ValueError(
                f"job kind {kind!r} was registered with {lazy}; the runner is called on "
                "a worker thread and its return value is discarded, so its body would "
                "never execute and the run would still be recorded as done. Register a "
                "callable that does the work when called (it may drive its own loop "
                "with asyncio.run)."
            )
        with self._lock:
            self._runners[kind] = _Runner(fn=fn, cancellable=cancellable)
        logger.info("App %s registered job kind: %s", self._app_name, kind)

    def kinds(self) -> list[str]:
        with self._lock:
            return sorted(self._runners)

    def is_cancellable(self, kind: str) -> bool:
        with self._lock:
            runner = self._runners.get(kind)
        return bool(runner and runner.cancellable)

    # ── Start ──

    def start(self, kind: str, *, dedupe_key: str = "") -> str:
        """Start a run of ``kind`` and return its run id.

        With a ``dedupe_key``, a second start while a run of the same kind and
        key is still in flight ADOPTS that run instead of beginning another —
        which is what stops a double click, or two tabs, from doing the paid
        work twice.

        There is no ``params`` in P1: a runner takes its handle and nothing
        else. Caller-supplied arguments are structured data that has to be
        sanitized before it can be written or served, and that channel returns
        in P2 together with the progress and result channels.

        Synchronous, and safe on the event loop: the only blocking work is one
        small ``atomic_write``. Unlike ``CronSDK``'s mutators there is no
        bounded store-lock spin to park the loop on, so this does not refuse an
        on-loop caller. :meth:`start_async` exists for callers who would rather
        not touch the disk from the loop thread at all.
        """
        with self._lock:
            runner = self._runners.get(kind)
        if runner is None:
            raise UnknownJobKind(
                f"app {self._app_name} has no registered runner for job kind {kind!r}"
            )

        run = JobRun(
            run_id=uuid.uuid4().hex,
            app=self._app_name,
            kind=kind,
            # STARTING, not RUNNING: no worker exists yet, and the initial write
            # below happens before one does. Claiming `running` here made the
            # record assert something untrue for the length of a disk write, which
            # `list_active` then served. The worker flips it as its first act.
            status=STARTING,
            origin=_ORIGIN,
            pid=os.getpid(),
            dedupe_key=dedupe_key,
            cancellable=runner.cancellable,
            created_at=_now(),
        )
        handle = JobHandle(run)
        thread = threading.Thread(
            target=self._execute,
            args=(run, runner, handle),
            name=f"job:{self._app_name}:{run.kind}",
            daemon=True,
        )

        # CHECK AND CLAIM IN ONE CRITICAL SECTION. Building the record, handle
        # and (unstarted) thread first keeps every await-free line above out of
        # the lock, so the section below holds no I/O at all -- which is what
        # makes it safe to be atomic. Splitting the check from the claim is what
        # let two concurrent starts both win. The closed check belongs in this
        # same section for the same reason: cleanup sets the flag under this lock
        # before snapshotting, so a start that gets here after that cannot claim.
        key = (kind, dedupe_key) if dedupe_key else None
        with self._lock:
            if self._closed:
                raise JobError(
                    f"app {self._app_name} is no longer accepting jobs; its job "
                    "runtime was shut down"
                )
            if key is not None:
                existing = self._keys.get(key)
                if existing is not None:
                    # The key itself is NOT logged: it is caller-supplied and can
                    # carry a credential or an account id, and the gateway log is
                    # durable and served by /api/logs. The run id and the kind
                    # identify the adoption without quoting the caller's string.
                    logger.info(
                        "App %s adopted in-flight job %s for kind=%s",
                        self._app_name,
                        existing,
                        kind,
                    )
                    return existing
                self._keys[key] = run.run_id
            self._live[run.run_id] = _Live(handle=handle, thread=thread)

        # Outside the lock, and BEFORE the worker exists, so this write still has
        # no competing writer. If it fails the claim must not leak, or the kind's
        # dedupe key would stay owned by a run that never started.
        if not self._persist(run):
            with self._lock:
                self._live.pop(run.run_id, None)
                if key is not None:
                    self._keys.pop(key, None)
            raise JobError(f"could not persist the initial record for job kind {kind!r}")

        # LAUNCH AS A GUARDED TRANSITION, not as an unconditional call. Everything
        # above happened outside the lock, including a disk write, so by now the
        # app may have been disabled -- and this run is ALREADY in `_live`, which
        # is the case `_closed` alone cannot cover: that flag refuses a start that
        # reaches the claim section after cleanup, and this one got there first.
        # Cleanup marks the handle discarded under this same lock, so re-reading
        # both here is what makes "may I start" answerable rather than assumed.
        with self._lock:
            entry = self._live.get(run.run_id)
            if self._closed or handle.discarded.is_set() or entry is None:
                # Refused: unwind the claim so the key is not owned by a run that
                # will never exist.
                self._live.pop(run.run_id, None)
                if key is not None:
                    self._keys.pop(key, None)
                refused = True
                start_error: Exception | None = None
            else:
                refused = False
                try:
                    thread.start()
                except RuntimeError as exc:
                    # The OS refused a thread. Unwind under the lock we hold; the
                    # record is dealt with below, outside it.
                    self._live.pop(run.run_id, None)
                    if key is not None:
                        self._keys.pop(key, None)
                    start_error = exc
                else:
                    # The transition, recorded where cleanup will read it.
                    entry.started = True
                    start_error = None

        if refused:
            # The record is the discarded handle's problem, not ours: `_persist`
            # refuses a discarded run by design, and writing one anyway is how a
            # record cleanup had already deleted came back. Cleanup's own
            # `remove_all` owns the file.
            raise JobError(
                f"app {self._app_name} stopped accepting jobs while {kind!r} was starting"
            )
        if start_error is not None:
            # A terminal record so this is not a ghost nothing will ever resolve:
            # its origin is ours and reconcile spares a live entry, so without
            # this it would sit non-terminal for the process's whole life. Routed
            # through the handle so a cleanup that landed first still refuses it
            # rather than having the file recreated.
            run.status = FAILED
            run.error = "the host refused a new thread for this job"
            run.finished_at = _now()
            self._persist(run, handle)
            self._audit("job_start", run.run_id, "failed", error=str(start_error))
            raise JobError(
                f"could not start a worker for job kind {kind!r}: {start_error}"
            ) from start_error
        self._audit("job_start", run.run_id, "ok")
        return run.run_id

    async def start_async(self, kind: str, *, dedupe_key: str = "") -> str:
        """Loop-native :meth:`start` — the initial record write is offloaded so
        an on-loop caller never touches the disk on the loop thread."""
        return await asyncio.to_thread(self.start, kind, dedupe_key=dedupe_key)

    def _execute(self, run: JobRun, runner: _Runner, handle: JobHandle) -> None:
        """The worker body. Sole writer of this run's record from here on.

        The runner's return value is DISCARDED, but it is INSPECTED first. P1
        records that a run finished, not what it produced: a return value is
        arbitrary nested data, and holding it is what required a recursive
        sanitizer over channels no P1 consumer reads. It returns in P2 on a type
        that is safe by construction.

        One thing is read off it before it is dropped, and it is the fact this
        method could not otherwise state honestly: whether the runner actually did
        the work. If the call handed back something that still needs to be driven
        -- an awaitable, or an async generator -- then the body did not run to
        completion and ``done`` would be a false record. ``register`` refuses the
        callable SHAPES that produce this, but a shape check is an adjacent-state
        proxy and cannot see every route to it: a plain ``def run(h): return
        _do_it(h)`` over an ``async def _do_it`` passes every registration check
        and still returns an unperformed coroutine. Observing the result is the
        only place the fact itself is available, which makes this the safety
        property and the registration guard a fast, friendly error.

        A returned suspendable is refused BY KIND, not by what its state seems to
        say. Generator FUNCTIONS are still refused at registration; a returned
        generator, coroutine or async generator is failed whatever it has done,
        because the object cannot say what it did -- a drained generator and one
        closed before it ever began are identical from here. So the SDK refuses the
        shape rather than guessing the history. This does NOT assert the body never
        ran: for a drained generator it did, and that run is still failed because
        the rule is stated rather than inferred. Earlier forms of this docstring
        read the frame state and allowed the closed one; see
        :func:`_undriven_result` for the reversal and what was measured.
        """
        # Completing the STARTING -> RUNNING transition is the worker's first act,
        # because the worker is the only party that knows it is actually running.
        # Through the guarded writer, so a disable that landed between the launch
        # and this line refuses it rather than recreating a deleted record.
        run.status = RUNNING
        try:
            # The transition is a PRECONDITION for running the body, not a
            # notification alongside it. This return used to be discarded, so a
            # failed write left the record at `starting` while the runner went on
            # to commit real work -- and if the process then died before the
            # terminal write, `reconcile` read that record and stated the run had
            # never begun. There is no way to correct that claim after the fact,
            # because the knowledge that the write failed dies with the process.
            # So the run does not begin unless the record can say it began.
            #
            # The cost is deliberate: a transient store error now fails a run that
            # might have succeeded. For an SDK whose product is a durable record,
            # a run that is not recorded is worse than a run not attempted.
            if not self._persist(run, handle):
                run.status = FAILED
                run.error = (
                    f"the store could not record run {run.run_id} as running, so its "
                    f"body was never started; a run this SDK cannot say began is one "
                    f"it does not begin"
                )
                logger.warning(
                    "App %s job %s (%s) not started: the running transition did not persist",
                    self._app_name,
                    run.run_id,
                    run.kind,
                )
            else:
                result = runner.fn(handle)
                undriven = _undriven_result(result)
                if undriven:
                    # Inside the SAME try, so the terminal write below is still the
                    # one write site. A second write path here is what leaked a
                    # dedupe claim before, and `_write_terminal` already owns the
                    # discard check.
                    run.status = FAILED
                    run.error = (
                        f"the runner for {run.kind!r} returned {undriven}, so this "
                        "run is recorded as failed rather than done"
                    )
                    _close_quietly(result)
                    logger.warning(
                        "App %s job %s (%s) returned %s and did no work",
                        self._app_name,
                        run.run_id,
                        run.kind,
                        undriven,
                    )
                else:
                    run.status = CANCELLED if handle.cancelled.is_set() else DONE
        except Exception as exc:  # noqa: BLE001 - a runner's failure is data, not a crash
            run.status = FAILED
            run.error = _redact(str(exc))[:2000]
            logger.warning(
                "App %s job %s (%s) failed: %s",
                self._app_name,
                run.run_id,
                run.kind,
                run.error,
            )
        finally:
            run.finished_at = _now()
            # The guarded writer owns the discard check, so a cleanup landing
            # mid-write cannot have this record recreated, and a failure comes
            # back as False rather than as an exception that would skip the
            # bookkeeping below. That skip is what leaked a dedupe claim no
            # later start could release.
            self._write_terminal(run, handle)
            with self._lock:
                self._live.pop(run.run_id, None)
                if run.dedupe_key:
                    self._keys.pop((run.kind, run.dedupe_key), None)
            self._audit(f"job_{run.status}", run.run_id, "ok")

    def _write_terminal(self, run: JobRun, handle: JobHandle) -> None:
        """Persist a run's final state, retrying once.

        A lost terminal write is not cosmetic: the record stays ``running`` and
        the UI keeps reporting work that has finished. One retry covers a
        transient failure. If it still fails the run has already been dropped
        from the live table, so ``reconcile`` resolves it -- immediately if
        anything calls it, and at the next gateway start regardless, since by
        then the record's origin is foreign. That residue is bounded but real,
        and a periodic sweep is deliberately out of scope here.

        A discarded run is not retried: ``_persist`` refuses it by design, and
        retrying would only burn the delay before refusing again.
        """
        if handle.discarded.is_set():
            return
        for attempt in (1, 2):
            if self._persist(run, handle):
                return
            if attempt == 1:
                time.sleep(0.05)
        logger.error(
            "could not persist terminal state for job %s; it will be reconciled "
            "as interrupted rather than left running",
            run.run_id,
        )

    # ── Read ──

    def get(self, run_id: str) -> JobRun | None:
        return self._store.read(run_id)

    def list_active(self, kind: str = "") -> list[JobRun]:
        """Runs that are not in a terminal state — what a fresh mount adopts."""
        return [
            r for r in self._store.iter_runs() if not r.is_terminal and (not kind or r.kind == kind)
        ]

    def list_recent(self, kind: str = "", limit: int = 20) -> list[JobRun]:
        """Most recently updated runs first, terminal ones included."""
        runs = [r for r in self._store.iter_runs() if not kind or r.kind == kind]
        runs.sort(key=lambda r: (r.updated_at, r.created_at), reverse=True)
        return runs[: max(0, limit)]

    def cancelling_and_live_ids(self) -> tuple[frozenset[str], frozenset[str]]:
        """One-acquisition snapshot of the ``(cancelling, live)`` run id sets.

        ``live`` is live-table membership: whether THIS process holds the run
        -- claimed at ``start`` just before the record is persisted and the
        worker thread launched, dropped only after the terminal write lands.
        That ownership reading is deliberate: it is the same authority
        :meth:`cancel` and :meth:`reconcile` already consult (``reconcile``
        spares exactly the runs reported live here). A durable record can say
        ``running`` while nothing owns it -- a terminal write that failed
        twice, or a record minted by another gateway process -- and both are
        absent from ``live``, which is what distinguishes a stale record from
        work this process is executing. Derived from memory at read time;
        nothing new is persisted, so it stays correct under any later
        heartbeat or lease design layered on top.

        ``cancelling`` is the read side of :meth:`cancel`'s "writes nothing".
        A requested cancel has to outlive the tab that asked -- that is the
        whole point of the SDK -- but persisting it from ``cancel`` would make
        a second writer on that run's file, and one-writer-per-run is what
        keeps ``atomic_write``'s crash-safety from silently dropping the loser
        of a concurrent update. So the fact is DERIVED from the live table on
        read instead. The window it covers is the gap between the request and
        the worker's next checkpoint, and it closes on its own: the worker
        records ``cancelled`` and ``_execute`` drops the entry, after which
        the status carries the answer. Deliberately NOT durable across a
        restart: a restarted gateway has no live table, and ``reconcile`` has
        already resolved the run to ``interrupted`` -- there is no pending
        cancel left to report.

        Both sets come from a SINGLE lock acquisition so a response renders
        them from one instant. Taken as two separate snapshots, a worker
        finishing between the acquisitions could serve ``cancelling: true``
        and ``live: false`` together for a non-terminal record -- a cancel
        pending on a run nothing owns. Within one snapshot ``cancelling`` is a
        subset of ``live`` by construction, and one acquisition serves a whole
        response, so a list endpoint renders every row from ONE snapshot
        rather than N of them.

        Call OFF the event loop: ``_persist`` holds this same lock across a
        disk write, so taking it on the loop can park the gateway for the
        length of that write.
        """
        with self._lock:
            return (
                frozenset(
                    run_id for run_id, live in self._live.items() if live.handle.cancelled.is_set()
                ),
                frozenset(self._live),
            )

    # ── Cancel ──

    def cancel(self, run_id: str) -> bool:
        """Ask a live, cancellable run to stop. Writes nothing.

        Returns False — rather than pretending — when the run is not live in
        this process or was never declared cancellable. The worker records the
        outcome itself at its next checkpoint, which keeps this run's file to a
        single writer.
        """
        with self._lock:
            live = self._live.get(run_id)
        if live is None:
            return False
        run = self._store.read(run_id)
        if run is None or not run.cancellable or run.is_terminal:
            return False
        live.handle.cancelled.set()
        self._audit("job_cancel", run_id, "ok")
        return True

    async def cancel_async(self, run_id: str) -> bool:
        """Loop-native :meth:`cancel`. Present so an on-loop caller does not
        have to know which methods happen to touch the disk."""
        return await asyncio.to_thread(self.cancel, run_id)

    # ── Reconciliation ──

    def reconcile(self) -> int:
        """Resolve records left non-terminal by a process that is gone.

        A run must never be left ``running`` forever and must never silently
        vanish — the two directions the hand-rolled predecessors got wrong. Each
        resolved record carries TWO facts rather than a sentence: how far the run
        had got (``interrupted_from``) and why it cannot be resumed
        (``interrupt_cause``). They are independent because they describe
        different times -- past progress, present capability -- and a consumer
        acts on them separately: a run interrupted from ``running`` may have
        committed side effects, one interrupted from ``queued`` or ``starting``
        provably has not, and only the cause says whether offering a retry makes
        sense. ``error`` is composed from both by :func:`_interrupt_error`, which
        is why the pass no longer has to choose one of two strings from the runner
        table alone. This runs only after every app has registered, so a missing
        runner means the kind is gone rather than not yet loaded.

        This is the ONE path that consumes records it did not write -- a file
        left by an older build, or hand-edited during an incident -- so a single
        unusable record must cost only itself. ``JobStore._path`` raises
        ``ValueError`` on a run id it will not turn into a path, and letting that
        escape would abandon every remaining run of this app, leaving exactly the
        stuck-``running`` state the pass exists to clear.

        **This pass decides from a snapshot, so it must not trust it at write
        time.** ``iter_runs`` yields a record decoded from disk; by the time this
        loop writes, a worker may have completed and left ``_live``. The write
        therefore goes through ``_persist(only_if_not_terminal=True)``, which
        re-reads under the same lock it writes with. That enforces the invariant
        this module already states -- a record in :data:`TERMINAL_STATES` is never
        revisited -- rather than adding a new rule: without it this pass overwrote
        a true terminal record with a false one, which is a worse failure than any
        unverified claim, because the truth was on disk and this pass deleted it.
        """
        flipped = 0
        for run in self._store.iter_runs():
            if run.is_terminal:
                continue
            with self._lock:
                live = run.run_id in self._live
                known = run.kind in self._runners
            # Skip only a run this process is ACTUALLY executing. Matching on
            # origin alone would spare a record this process wrote and then lost
            # (a terminal write that failed twice), which is the stuck-`running`
            # state the pass exists to clear.
            if run.origin == _ORIGIN and live:
                continue
            # BOTH axes are recorded, and the message is DERIVED from them. The
            # previous form assigned `status` over the top of the only evidence of
            # how far the run got, then picked one of two strings from `known`
            # alone -- so a run whose worker thread was never started was written
            # a record claiming it "was running", and a consumer could not tell a
            # run that committed side effects from one that provably had not.
            run.interrupted_from = run.status
            # Own origin is tested FIRST because it is the one VERIFIED fact here:
            # this record was minted by this process, so the process cannot have
            # restarted -- and both restart causes would say it did. `known` is a
            # proxy (is a runner registered) and a proxy must not outrank the fact
            # sitting next to it. Reaching this line with a matching origin means
            # the guard above found no live worker, which is `_write_terminal`
            # having spent both of its attempts.
            #
            # Composed into a local and assigned ONCE, so the field keeps its
            # single assignment site from a constant -- the ratchet that turns
            # "SDK-minted" from a claim into something checkable.
            if run.origin == _ORIGIN:
                cause = CAUSE_TERMINAL_WRITE_LOST
            elif known:
                cause = CAUSE_PROCESS_GONE
            else:
                cause = CAUSE_RUNNER_UNREGISTERED
            run.interrupt_cause = cause
            run.status = INTERRUPTED
            run.finished_at = _now()
            run.error = _interrupt_error(run.interrupt_cause, run.interrupted_from, run.kind)
            # `only_if_not_terminal` re-reads under the write's lock. The
            # `is_terminal` check at the top of this loop reads a SNAPSHOT: a
            # worker can finish between that read and this write, and the earlier
            # `live` check does not close the window because a worker leaves
            # `_live` only AFTER its terminal write has landed. Without the
            # re-read this pass overwrote a true `done` with `interrupted`, and
            # the cause it wrote was `terminal_write_lost` -- claiming the
            # terminal write was lost when it had in fact succeeded and this pass
            # destroyed it.
            if not self._persist(run, only_if_not_terminal=True):
                continue
            flipped += 1
            self._audit("job_interrupted", run.run_id, "ok")
        if flipped:
            logger.info("App %s: reconciled %d interrupted job run(s)", self._app_name, flipped)
        return flipped

    # ── Cleanup ──

    async def remove_all_async(self) -> CleanupResult:
        """Stop this app's runs and drop their records.

        Called on disable, mirroring ``CronSDK.remove_all_async``, and it now
        does what "disable" implies: every live handle is marked discarded and
        cancelled under the lock the guarded writer checks, and then each worker
        is **bounded-joined**. Signalling alone left the threads running -- a
        disabled app kept doing real, side-effecting work with its records
        already deleted. Waiting is the only correct answer: a thread cannot be
        killed, and abandoning it is the defect rather than the fix.

        A worker that outlives the deadline is reported, not waited on forever.
        Every run this call kills leaves one SEL line (``job_killed_on_disable``)
        carrying its id and kind before the record is deleted, so "which runs
        did the disable kill" stays answerable after the records are gone. The
        trace is fail-closed: if it cannot be written durably, the deletion is
        refused and reported as a failed cleanup; the surviving records are
        retried by the next cleanup on a fresh instance (disable also forgets
        the SDK, so that means after a re-enable, or by ``reconcile`` at the
        next start).
        Gateway shutdown is a separate case left as accepted residue: these are
        daemon threads, so the interpreter reaps them at exit without a chance
        to finish, and draining every app's runs there would delay shutdown for
        work nobody is waiting on.
        """
        with self._lock:
            # Set BEFORE the snapshot, under the lock ``start``'s claim section
            # takes. Marking and snapshotting were previously the whole of this
            # section, so a start that had already read the runner table could
            # still claim afterwards and spawn a worker this cleanup would never
            # see -- a disabled app doing real work with its records deleted.
            # Disable is terminal for this instance; a re-enable builds a new one.
            self._closed = True
            live = list(self._live.values())
            # Marked and cleared under the SAME lock the guarded writer takes,
            # so a worker cannot slip a write in between this and the delete.
            # The dedupe index goes too, or a key would stay owned by a run
            # whose record no longer exists and the next start would adopt a
            # ghost.
            for entry in live:
                entry.handle.discarded.set()
                entry.handle.cancelled.set()
            self._live.clear()
            self._keys.clear()

        # Joined OFF THE LOOP and outside the lock. Off the loop because a
        # blocking join in an async method parks the whole gateway for its
        # deadline -- the exact hazard CronSDK's docstring spells out, which this
        # method walked into while fixing the previous round. Outside the lock
        # because a worker's final write needs that lock, so holding it here
        # would deadlock against the thread being waited on.
        stubborn = await asyncio.to_thread(self._join_workers, live)
        if stubborn:
            logger.warning(
                "App %s: %d job worker(s) did not stop within %.0fs and are still "
                "running with their records removed; run id(s): %s",
                self._app_name,
                len(stubborn),
                _CLEANUP_JOIN_SECS,
                ", ".join(stubborn),
            )

        # One durable SEL line per run this disable kills, BEFORE the records
        # are deleted -- deletion is the last moment the identities exist, and
        # the count-only summary below cannot answer "which runs were killed".
        # Mirrors the per-run precedent ``reconcile`` set with
        # ``job_interrupted``. The marker comes from the SAME observation that
        # feeds the summary's stubborn count -- ``_join_workers`` captures the
        # run ids alive at their join deadline -- so the summary can never say
        # "one was stubborn" while no line names it. Stale records are read
        # once, off the loop; a live entry is skipped only on its IN-MEMORY
        # terminal status (its worker finished on its own before the discard
        # landed, so nothing was killed) -- never on its disk record, which an
        # agent can write.
        stubborn_ids = set(stubborn)
        live_ids = {entry.handle.run_id for entry in live}

        def _safe_read(run_id: str) -> JobRun | None:
            # The scan walks *.json names in an app-writable directory, so a
            # path here is agent-influenced input: a planted symlink
            # ``<id>.json -> /dev/zero`` would hang an unbounded follow and
            # make disable unreachable, and one pointed at a sensitive file
            # would pull its bytes into a parse attempt. The read goes through
            # the repository's centralized funnel rather than a hand-rolled
            # check: ``safe_read_file_bytes_nolink`` opens with ``O_NOFOLLOW``,
            # validates the OPENED descriptor (regular, non-hardlinked, real
            # path contained in ``within_root`` -- which also refuses a PARENT
            # directory swapped for a symlink, the case a final-component
            # check alone misses), and bounds the read. Every refusal --
            # permission denied, a Windows sharing violation, a refused link,
            # an oversized record -- classifies the record unreadable rather
            # than propagating: an unreadable record must not abort the
            # disable, and its identity still leaves an ``unreadable`` line.
            path = self._store.dir / f"{run_id}.json"
            try:
                raw = safe_read_file_bytes_nolink(
                    str(path),
                    within_root=str(self._store.dir),
                    max_bytes=_MAX_RECORD_BYTES,
                )
            except FileTooLargeError:
                return None
            if raw is None:
                return None
            try:
                return JobRun.from_dict(json.loads(raw.decode("utf-8")))
            except (TypeError, ValueError):
                return None

        def _scan() -> tuple[list[tuple[str, JobRun]], list[str]]:
            # Every record is read by CANONICAL FILENAME -- never from an
            # id-keyed index over parsed bodies: a record file whose BODY
            # claims another run's id would shadow the real record in such an
            # index and suppress that run's kill line. A file the guarded
            # read refuses (symlink, non-file, oversized, unparseable, or a
            # name that is not a valid run id) still gets deleted by
            # ``remove_all`` below, so its FILENAME is returned for an
            # ``unreadable`` line -- an identity must not vanish just because
            # its record went hostile or corrupt. Live runs' records are
            # deliberately NOT read at all: the record files sit in an
            # app-writable directory, so a planted ``status=done`` body would
            # be an agent-writable veto over its own run's kill line. Whether
            # a live run was killed is decided purely from in-process state
            # below.
            stale: list[tuple[str, JobRun]] = []
            unreadable: list[str] = []
            if self._store.dir.is_dir():
                for path in sorted(self._store.dir.glob("*.json")):
                    stem = path.stem
                    if stem in live_ids:
                        continue
                    record = _safe_read(stem)
                    if record is None:
                        unreadable.append(stem)
                    elif not record.is_terminal:
                        stale.append((stem, record))
            return stale, unreadable

        stale_runs, unreadable_stems = await asyncio.to_thread(_scan)
        killed: list[str] = []
        for entry in live:
            # Whether this run finished on its own is read ONLY from the
            # in-memory handle -- never from its disk record, which lives in
            # an app-writable directory where a planted terminal status would
            # silently suppress the run's kill line. The in-memory status is
            # set by the worker wrapper itself, post-join, so it is the one
            # observation an agent cannot forge. A worker
            # that computed DONE/FAILED before the cancel landed, but whose
            # terminal write the discard guard then refused, has already
            # audited its own completion (``job_done``/``job_failed``) while
            # its record stays non-terminal on disk. Reading the in-memory
            # status closes that window -- the SEL trail must not carry both a
            # completion and a kill for one run. Two deliberate exceptions:
            # CANCELLED means the disable's own signal stopped the worker,
            # which is exactly a killed run; and a run observed STUBBORN at its
            # join deadline keeps its line no matter what its status became
            # afterwards (a post-deadline raise flips it to FAILED), because
            # the summary already counted it from that same observation and
            # the count must never name a stubborn run no line identifies.
            if entry.handle.run_id not in stubborn_ids and entry.handle.status in (DONE, FAILED):
                continue
            # An entry whose thread never started is NOT skipped -- its record
            # is deleted by the same call and skipping would lose the identity
            # with no line at all (the stale scan excludes live ids). But no
            # worker existed, so "killed" must not imply one: the line carries
            # ``never_started`` instead of a kill-shaped claim.
            if not entry.started:
                marker = " never_started"
            elif entry.handle.run_id in stubborn_ids:
                marker = " still_running"
            else:
                marker = ""
            # The kind is clipped BEFORE the marker is appended: ``_audit``
            # truncates long lines from the right, and an oversized kind
            # would push ``still_running``/``never_started`` off the end --
            # the summary would then count a stubborn run no line marks.
            killed.append(f"{entry.handle.run_id} kind={entry.handle.kind[:32]}{marker}")
        # A non-terminal record with no live worker (a foreign process's run
        # that never got reconciled) vanishes in the same delete, so it gets
        # the same line, marked ``stale`` because nothing was killed: there was
        # no worker to kill. Identity comes from the FILENAME stem (the
        # canonical id), and ``kind`` comes off disk -- the one input here not
        # minted by this process -- so both are redacted, bounded, or
        # repr-quoted rather than interpolated raw into a durable SEL field.
        for stem, record in stale_runs:
            killed.append(f"{stem[:_RUN_ID_LEN]} kind={_redact(record.kind)[:32]!r} stale")
        # A file the store cannot parse is deleted by the same ``remove_all``,
        # and "which file was that" must survive the delete too.
        for stem in unreadable_stems:
            killed.append(f"{_redact(stem)[:_RUN_ID_LEN]!r} unreadable stale")
        if killed:
            # The kill trace is the ONLY durable answer to "which runs did this
            # disable kill", so it is fail-closed: each line is written
            # synchronously (``critical=True``) and a write failure REFUSES the
            # record deletion below. The records stay on disk -- still
            # non-terminal, since their handles are discarded -- so a later
            # cleanup on a fresh instance (after a re-enable, or ``reconcile``
            # at the next start) resolves them instead of deleting identities
            # nothing recorded. Reported through the existing partial-cleanup
            # contract instead of raising into the disable route.
            def _emit() -> None:
                for line in killed:
                    self._audit("job_killed_on_disable", line, "ok", critical=True)

            try:
                await asyncio.to_thread(_emit)
            except Exception:  # noqa: BLE001 - refusing the delete IS the handling
                logger.exception(
                    "App %s: could not durably audit killed job run(s); record "
                    "deletion refused so their identities are not lost",
                    self._app_name,
                )
                return CleanupResult(removed=0, failed=len(killed), still_running=len(stubborn))

        removed, failed = await asyncio.to_thread(self._store.remove_all)
        if removed or failed:
            self._audit(
                "job_remove_all",
                f"removed={removed} failed={failed} stubborn={len(stubborn)}",
                "ok" if not failed and not stubborn else "partial",
            )
            logger.info(
                "App %s removed %d job record(s), %d failed", self._app_name, removed, failed
            )
        return CleanupResult(removed=removed, failed=failed, still_running=len(stubborn))

    def _join_workers(self, live: list[_Live]) -> list[str]:
        """Wait for each STARTED worker, bounded. Runs on a worker thread, never
        the loop. Returns the RUN IDS of workers still alive at their deadline:
        run ids are unique where thread names are minted per kind, and this one
        observation feeds the count, the log line, and the per-run SEL marker --
        two observations taken at different times could disagree, reporting a
        stubborn count with no line saying which run it was.

        An entry whose thread never started is skipped rather than joined, and it
        cannot be stubborn: no code of the app is executing, so there is nothing
        to outlive a deadline. Joining it instead raised ``RuntimeError``, which
        escaped the disable entirely -- so the records were never deleted and any
        worker that HAD started kept going. Reading ``started`` is what removes
        the guess; the flag is set under the same lock the snapshot is taken with,
        so this list cannot contain an entry whose state changed underneath it.
        """
        stubborn = []
        for entry in live:
            if not entry.started:
                continue
            entry.thread.join(timeout=_CLEANUP_JOIN_SECS)
            if entry.thread.is_alive():
                stubborn.append(entry.handle.run_id)
        return stubborn

    # ── Audit ──

    def _audit(
        self,
        operation: str,
        resources: str,
        outcome: str,
        *,
        error: str = "",
        critical: bool = False,
    ) -> None:
        try:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation=f"jobs.{operation}",
                outcome=outcome,
                source=self._app_name,
                resources=resources[:200],
                error=error[:200],
                critical=critical,
            )
        except Exception:  # noqa: BLE001 - an audit failure must not fail the job
            # ... unless the caller said it must: ``critical`` re-raises so a
            # fail-closed audit can refuse the action it was auditing.
            if critical:
                raise
            logger.debug("job SEL audit failed", exc_info=True)


# ── Process-wide registry ──
#
# The shared ``_jobs/*`` route family is mounted ONCE for every app and resolves
# the app from the URL, so it needs a name -> SDK lookup; startup reconciliation
# needs the same table. That makes this registry part of the design rather than
# a shortcut around passing the SDK around.

_SDKS: dict[str, JobSDK] = {}
_SDKS_LOCK = threading.Lock()


def register_sdk(sdk: JobSDK) -> None:
    with _SDKS_LOCK:
        _SDKS[sdk.app_name] = sdk


def get_sdk(app_name: str) -> JobSDK | None:
    with _SDKS_LOCK:
        return _SDKS.get(app_name)


def forget_sdk(app_name: str) -> None:
    with _SDKS_LOCK:
        _SDKS.pop(app_name, None)


def registered_apps() -> list[str]:
    with _SDKS_LOCK:
        return sorted(_SDKS)


def reconcile_all() -> int:
    """Reconcile every registered app's runs. Call once, after startup.

    Placed after the enable loop deliberately: before it, a kind with no runner
    is indistinguishable from an app that has not loaded yet, so an early pass
    would blame the app for the gateway's own boot order.
    """
    with _SDKS_LOCK:
        sdks = list(_SDKS.values())
    total = 0
    for sdk in sdks:
        try:
            total += sdk.reconcile()
        except Exception:  # noqa: BLE001 - one app's bad store must not stop the rest
            logger.exception("job reconciliation failed for app %s", sdk.app_name)
    return total
