"""A run's durable record, the states it moves through, and the handle its runner holds.

The lifecycle states and the interruption causes ``reconcile`` records, the bounds and
the redaction a caller's params and a runner's error text pass at ingest, ``JobRun`` --
one JSON file per run, serialized whole -- and ``JobHandle``, whose ``checkpoint``
reports progress and observes a cancel in one act. The Job SDK's refusals and the
runner's cooperative exit, ``JobCancelled``, are defined here too.

Served through :mod:`kiro_crew.apps.job_sdk`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

#: The Job SDK logs under its facade's name, so one logger covers the whole family.
logger = logging.getLogger("kiro_crew.apps.job_sdk")


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


#: Length of an SDK-minted run id (``uuid.uuid4().hex``). Validated, not just
#: assumed: a caller reaches ``{run_id}`` on the HTTP surface directly, and a
#: hex string of any other length is not a run this SDK ever minted. Checking
#: only the ALPHABET let a very long id through, and the oversized filename it
#: built raised ``ENAMETOOLONG`` -- an ``OSError`` no read handler names -- which
#: surfaced as a 500 where a 404 is the honest answer.
_RUN_ID_LEN = 32


#: Bounds on one run's parameter map, applied where the caller's map enters.
#: Parameters NAME the work -- an account, a target, a mode -- so the map is
#: small by construction: at most this many entries, and a key or value longer
#: than the lengths below is REFUSED rather than shortened to fit. The whole map
#: therefore cannot exceed roughly nine kilobytes, well inside
#: ``_MAX_RECORD_BYTES``, so no caller can grow a record past the size the
#: disable scan refuses to open.
_MAX_PARAMS = 16
_MAX_PARAM_KEY = 64
_MAX_PARAM_VALUE = 512


class JobError(RuntimeError):
    """Base class for Job SDK refusals."""


class UnknownJobKind(JobError):
    """Raised when starting a kind that has no registered runner."""


class JobCancelled(Exception):
    """Raised inside a runner by :meth:`JobHandle.checkpoint` when a cancel was
    requested, so the runner unwinds AT the checkpoint rather than running on.

    This is the fact that makes ``CANCELLED`` an observed outcome rather than an
    inference. The old ``CANCELLED`` verdict was chosen from the cancel flag being
    SET (see the terminal write in ``_execute``): that proves a cancel was
    REQUESTED, not that the work stopped, so a runner that polled the flag, saw it,
    and finished anyway was still recorded ``cancelled``. When a runner instead
    calls ``checkpoint`` and lets this propagate, its stack has unwound to the
    checkpoint before ``_execute`` records the run -- the record then states a stop
    the SDK watched happen at a named point in the runner's own body.

    A plain ``Exception`` and NOT a ``JobError``: it is not a refusal by the SDK
    but the runner's own cooperative exit, and ``_execute`` catches it BEFORE its
    generic ``except Exception`` so an acknowledged cancel is never miscounted as a
    failure. It is deliberately NOT a ``BaseException`` subclass like
    ``asyncio.CancelledError``: a runner is a plain function on a worker thread, so
    there is no cancellation machinery to cooperate with, and a ``BaseException``
    would sail through the ``except Exception`` guards a runner's own body may hold
    around its cleanup.
    """


# Named by the path callers import them from, the facade that serves them: a traceback or
# a log line names ``kiro_crew.apps.job_sdk.JobCancelled``, not this module.
JobError.__module__ = "kiro_crew.apps.job_sdk"
UnknownJobKind.__module__ = "kiro_crew.apps.job_sdk"
JobCancelled.__module__ = "kiro_crew.apps.job_sdk"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _redact(text: str) -> str:
    """Scrub a string a record will carry, and answer what a clean one looks like.

    Two callers, two uses. A runner's error is scrubbed with it, because the error
    still has to be readable afterwards. A caller's param is only COMPARED against
    it: a value it would change is refused instead, so nothing the caller wrote is
    quietly replaced.

    A failing runner's exception text can quote back a command line carrying a
    credential, so the same chain the app route boundaries apply runs here, at
    the point the text stops being local. Applied at INGEST rather than on the
    way out, so the record on disk is clean too.
    """
    try:
        from kiro_crew.security import redact

        out = redact(text)
        return out
    except Exception:  # noqa: BLE001 - redaction must never mask the error itself
        logger.debug("job text redaction failed", exc_info=True)
        return text


def _validated_params(params: Mapping[str, str] | None) -> dict[str, str]:
    """A caller's run parameters as a flat, bounded map of strings.

    THE ONE PLACE a caller's parameters enter. Everything the rest of the module
    assumes about them is established here, which is what keeps the record's
    "JSON-safe by construction" promise a property of the type rather than a rule
    the writer has to re-check: depth one, string keys, string values, a bounded
    count and bounded lengths.

    It REFUSES a wrong shape instead of coercing one, because each coercion it
    could perform is a wrong answer somewhere later. A key that is not a string
    round-trips through JSON as a DIFFERENT key, so a runner reading ``5`` back
    would miss the value stored under ``"5"``. A value that is not a string has no
    single right spelling -- ``True`` and ``"True"`` and ``"true"`` are three
    answers -- so the caller picks it, not this function. And a map that is merely
    clipped to the bound would hand the runner a silently incomplete set of
    parameters, which is worse than the call failing at the site that wrote it.

    Nothing here is rewritten, values included. A key is a name the app author
    writes in two places -- the ``start`` call and the runner reading it back --
    so rewriting it would break that lookup while looking like nothing happened.
    A value is data, and this channel is NOT a way to hand a runner a secret: the
    record is durable, so a credential-shaped value is REFUSED here, before
    either the handle or the record holds it. Refusing rather than scrubbing is
    what keeps this function's one rule whole -- every wrong input fails at the
    site that wrote it -- and it keeps the value the runner reads, the value on
    disk and the value the caller passed the same string. Scrubbing instead would
    carve out values as the single thing quietly changed, and hand the runner a
    string its caller never wrote with nothing raised anywhere.
    """
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise TypeError(f"job params must be a mapping, not {type(params).__name__}")
    if len(params) > _MAX_PARAMS:
        raise ValueError(f"a job takes at most {_MAX_PARAMS} params, got {len(params)}")
    out: dict[str, str] = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise TypeError(f"job param names must be strings, not {type(key).__name__}")
        if not key:
            raise ValueError("a job param name must not be empty")
        if len(key) > _MAX_PARAM_KEY:
            raise ValueError(
                f"job param name {key[:_MAX_PARAM_KEY]!r} is longer than "
                f"{_MAX_PARAM_KEY} characters"
            )
        if not isinstance(value, str):
            raise TypeError(f"job param {key!r} must be a string, not {type(value).__name__}")
        if len(value) > _MAX_PARAM_VALUE:
            raise ValueError(f"job param {key!r} is longer than {_MAX_PARAM_VALUE} characters")
        if _redact(value) != value:
            raise ValueError(
                f"job param {key!r} looks like a credential, and params are not a way "
                "to hand a runner a secret"
            )
        out[key] = value
    return out


@dataclass
class JobRun:
    """One run's durable record. Serialized whole; never partially updated.

    ``error`` is the ONLY field a RUNNER supplies, which is what makes the
    writer's sanitize rule one line rather than a list somebody has to remember
    to extend. Two fields come from the CALLER instead, ``dedupe_key`` and
    ``params``. ``params`` is the one that carries a runtime check:
    ``_validated_params`` admits only string keys and string values, within
    bounds, so the map is JSON-safe by construction. ``dedupe_key`` rests on its
    type annotation alone -- it reaches the record as the caller passed it.
    Everything else the SDK mints itself, and ``from_dict`` is what holds the
    line on a record read back off disk.
    """

    run_id: str
    app: str
    kind: str
    status: str = QUEUED
    origin: str = ""
    pid: int = 0
    dedupe_key: str = ""
    #: What the caller named this run's work with: a flat map of strings, at most
    #: ``_MAX_PARAMS`` of them, bounded and checked by ``_validated_params``
    #: where the caller's map enters. A runner reads it back off its handle, so
    #: the record is also what a resume reads: the two are the same strings.
    #:
    #: Caller-supplied, and still JSON-safe by construction -- which is the whole
    #: reason the shape is a depth-one map of strings rather than anything a
    #: caller hands over. Withheld from the served view for the same reason
    #: ``dedupe_key`` is: a caller chooses these strings, and the browser is not
    #: the party that chose them.
    params: dict[str, str] = field(default_factory=dict)
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
    #: Whether the runner reached at least one ``handle.checkpoint`` -- an OBSERVED
    #: fact, not the return-value classifier's inference. This is the record
    #: STATING that work happened rather than the SDK guessing it from what came
    #: back: a ``done`` run with this True was watched reaching a point of progress
    #: in the runner's own body. SDK-minted, never runner-supplied: the runner
    #: calls ``checkpoint`` (a method) and the SDK sets this bool from the handle
    #: at the terminal write, so it is not a payload channel the sanitize rule has
    #: to cover -- the same reason ``interrupted_from`` and ``interrupt_cause`` are
    #: on the record. Absence does NOT mean no work: a runner may do real work
    #: without checkpointing, so a False here with ``done`` means "not observed",
    #: which is why the classifier stays a backstop rather than being deleted.
    work_observed: bool = False

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

        ``params`` is the one field holding a map, and it is read back the same
        way: entries that are not string-to-string, or that break the bounds
        ``_validated_params`` writes them under, are DROPPED and the rest of the
        record survives -- one unusable entry costing the whole app's run history
        is exactly what this method exists to prevent. Dropped rather than
        clipped: a clipped name is a DIFFERENT name, so two long names sharing a
        prefix would land on one entry and a runner would read the wrong value.
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
            elif want == "dict[str, str]":
                if isinstance(value, dict):
                    # Filter, THEN cut to the bound: cutting first lets a broken
                    # entry near the front spend a slot, so a record carrying the
                    # bound's worth of valid entries behind it comes back one
                    # short -- and the reconciliation rewrite makes that loss
                    # durable. The bound is a limit on what SURVIVES.
                    kept = [
                        (k, v)
                        for k, v in value.items()
                        if isinstance(k, str)
                        and isinstance(v, str)
                        and 0 < len(k) <= _MAX_PARAM_KEY
                        and len(v) <= _MAX_PARAM_VALUE
                    ]
                    # The cut below drops the tail of an over-count record, and
                    # the reconciliation rewrite makes that loss permanent, so
                    # the overflow is counted out loud rather than dropped in
                    # silence. ``_validated_params`` refuses a map this large, so
                    # reaching this branch means something other than this SDK
                    # wrote the file -- which is the fact worth a line in the log.
                    #
                    # That same fact is why the id naming the record is redacted,
                    # clamped and repr-quoted the way the kill trace does it: the
                    # writer is foreign, the value is straight off disk, and this
                    # line is durable and served to an operator, so a raw value
                    # could carry a newline and forge whole log lines after it.
                    if len(kept) > _MAX_PARAMS:
                        logger.warning(
                            "job record %r: %s holds %d entries over the bound of %d; "
                            "dropping the excess",
                            _redact(str(data.get("run_id", "?")))[:_RUN_ID_LEN],
                            name,
                            len(kept) - _MAX_PARAMS,
                            _MAX_PARAMS,
                        )
                    kwargs[name] = dict(kept[:_MAX_PARAMS])
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
    """What a runner is handed: the cancel signal, and the checkpoint call that
    reports progress and observes that signal in one act.

    ``cancelled`` is a ``threading.Event`` the runner MAY still poll directly --
    that door stays open for a loop that wants to test-and-continue rather than
    unwind. The SDK cannot interrupt a thread that never looks, so cancellation
    remains cooperative either way.

    ``checkpoint`` is the P1 progress channel, and it is deliberately the SAME
    call that carries cancellation -- progress and cancellation are one design,
    not two. A runner that reports it reached a checkpoint necessarily
    observes the cancel signal AT that checkpoint, because ``checkpoint`` raises
    ``JobCancelled`` before it returns when a cancel is pending. There is no way
    to spell "report progress but ignore cancellation": the channel that answers
    "did work happen" is the channel that answers "should it stop", so a progress
    report that left cancellation unobservable -- a half-capability -- is not a
    state this API can represent.

    It writes NOTHING to disk. The observed fact lives on the handle and is read
    ONCE by ``_execute`` at the terminal write, so the record keeps its single
    writer over its whole life -- P1's reason for having no mid-run mutation
    stream, preserved. What changes is that the terminal ``done`` is now backed by
    an observation (a checkpoint was reached) with the return-value classifier
    kept as a backstop, rather than resting on the classifier alone.
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
        #: Whether the runner reached a checkpoint. A plain bool, not a guarded
        #: field: the ONLY writer is the runner thread (in ``checkpoint``) and the
        #: ONLY reader is ``_execute`` on that SAME thread after the runner
        #: returns, so there is no cross-thread read to make consistent and no
        #: lock to justify. A count and a note rode here in an earlier revision
        #: "for a future progress surface"; they had no reader, so by P1's own
        #: doctrine -- a payload stays out until the consumer that reads it exists
        #: -- they are gone and return in P2 with that consumer.
        self._reached_checkpoint = False

    @property
    def run_id(self) -> str:
        return self._run.run_id

    @property
    def kind(self) -> str:
        return self._run.kind

    @property
    def status(self) -> str:
        return self._run.status

    @property
    def params(self) -> dict[str, str]:
        """What the caller named this run's work with, already checked at the start
        call and carried through unchanged.

        A fresh COPY on each read, not the record's own map. The record has a
        single writer, and handing the runner the live mapping would let it change
        a field that writer is about to serialize -- a second writer through the
        back door. The copy also makes "the runner edited its parameters" a purely
        local act, which is the honest answer: parameters are what the run was
        STARTED with, and nothing later moves them.

        Empty when the caller named none, so a runner that needs one asks for it
        and handles absence like any other mapping.
        """
        return dict(self._run.params)

    def checkpoint(self) -> None:
        """Report that the runner reached a point of real progress, and stop here
        if a cancel is pending.

        Call this at each unit of work a runner completes. Two things happen, and
        they are one act on purpose -- progress and cancellation are one design,
        not two:

        * The handle records that a checkpoint was reached -- an OBSERVED fact the
          terminal write reads, so ``done`` rests on "this runner did work" rather
          than on the return-value classifier's inference about what it handed
          back.

        * If a cancel has been requested, this RAISES :class:`JobCancelled`,
          unwinding the runner at the checkpoint. ``_execute`` catches it and
          records ``cancelled`` -- an outcome it WATCHED, because the stack is
          already unwound to this call before the record is written. A runner that
          reaches its next checkpoint after a cancel therefore cannot run to a
          false ``done``; the stop is where the runner acknowledged it.

        The cancel check comes FIRST: a checkpoint reached in an already-cancelled
        run is not progress the record should credit, and testing progress before
        the signal would let one more unit be counted after the stop was asked
        for.

        It takes no argument. A progress NOTE (a stage name, a count) is a payload
        with no P1 consumer -- nothing reads it -- and the same module's doctrine
        keeps a rich per-checkpoint payload out until P2, where the surface that
        serves it is designed against a real reader. So P1 reports the one fact it
        can serve: that a checkpoint happened.
        """
        if self.cancelled.is_set():
            raise JobCancelled(
                f"job {self._run.kind!r} run {self._run.run_id} was cancelled at a checkpoint"
            )
        self._reached_checkpoint = True

    @property
    def work_observed(self) -> bool:
        """Whether any checkpoint was reached -- the fact the terminal write reads.

        Absence is NOT evidence of no work: a runner that does real work without
        calling ``checkpoint`` is legitimate, which is why the return-value
        classifier stays as a backstop rather than being replaced, and why the
        served value means "not observed", never "did no work".
        """
        return self._reached_checkpoint
