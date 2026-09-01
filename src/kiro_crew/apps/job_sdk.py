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
table -- ``cancelling_ids`` -- rather than written down. Reading it costs nothing
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
"""

from __future__ import annotations

import asyncio
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

    def _persist(self, run: JobRun, handle: JobHandle | None = None) -> bool:
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

        Returns True when the record is on disk.
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
        """
        if not kind:
            raise ValueError("job kind must be a non-empty string")
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

        The runner's return value is DISCARDED. P1 records that a run finished,
        not what it produced: a return value is arbitrary nested data, and
        holding it is what required a recursive sanitizer over channels no P1
        consumer reads. It returns in P2 on a type that is safe by construction.
        """
        # Completing the STARTING -> RUNNING transition is the worker's first act,
        # because the worker is the only party that knows it is actually running.
        # Through the guarded writer, so a disable that landed between the launch
        # and this line refuses it rather than recreating a deleted record.
        run.status = RUNNING
        self._persist(run, handle)
        try:
            runner.fn(handle)
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

    def cancelling_ids(self) -> frozenset[str]:
        """Run ids whose cancel has been asked for and not yet recorded.

        The read side of :meth:`cancel`'s "writes nothing". A requested cancel
        has to outlive the tab that asked -- that is the whole point of the SDK --
        but persisting it from ``cancel`` would make a second writer on that
        run's file, and one-writer-per-run is what keeps ``atomic_write``'s
        crash-safety from silently dropping the loser of a concurrent update. So
        the fact is DERIVED from the live table on read instead, the same way
        :meth:`reconcile` already derives liveness from it. Nothing new is
        written and the single-writer rule is untouched.

        The window this covers is the gap between the request and the worker's
        next checkpoint, which for a runner that checkpoints minutes apart is
        minutes. It closes on its own: the worker records ``cancelled`` and
        ``_execute`` drops the entry, after which this returns nothing for that
        run and the status carries the answer.

        Deliberately NOT durable across a restart. A restarted gateway has no
        live table, and ``reconcile`` has already resolved the run to
        ``interrupted`` -- there is no pending cancel left to report.

        Call OFF the event loop. ``_persist`` holds this same lock across a disk
        write, so taking it on the loop can park the gateway for the length of
        that write. One acquisition serves a whole response, so a list endpoint
        renders every row from ONE snapshot rather than from N of them.
        """
        with self._lock:
            return frozenset(
                run_id for run_id, live in self._live.items() if live.handle.cancelled.is_set()
            )

    # ── Reconciliation ──

    def reconcile(self) -> int:
        """Resolve records left non-terminal by a process that is gone.

        A run must never be left ``running`` forever and must never silently
        vanish — the two directions the hand-rolled predecessors got wrong. The
        reason distinguishes a lost process from a kind whose runner is no
        longer registered (the app was disabled, or the kind was removed), which
        is why this runs only after every app has registered.

        This is the ONE path that consumes records it did not write -- a file
        left by an older build, or hand-edited during an incident -- so a single
        unusable record must cost only itself. ``JobStore._path`` raises
        ``ValueError`` on a run id it will not turn into a path, and letting that
        escape would abandon every remaining run of this app, leaving exactly the
        stuck-``running`` state the pass exists to clear.
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
            run.status = INTERRUPTED
            run.finished_at = _now()
            run.error = (
                "the gateway restarted while this was running"
                if known
                else f"the gateway restarted and no runner is registered for {run.kind!r}"
            )
            if not self._persist(run):
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
                "running with their records removed: %s",
                self._app_name,
                len(stubborn),
                _CLEANUP_JOIN_SECS,
                ", ".join(stubborn),
            )

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
        the loop.

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
                stubborn.append(entry.thread.name)
        return stubborn

    # ── Audit ──

    def _audit(self, operation: str, resources: str, outcome: str, *, error: str = "") -> None:
        try:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation=f"jobs.{operation}",
                outcome=outcome,
                source=self._app_name,
                resources=resources[:200],
                error=error[:200],
            )
        except Exception:  # noqa: BLE001 - an audit failure must not fail the job
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
