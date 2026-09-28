"""The Pull+Build sync runner, as a real module instead of a string literal.

The alternative shape -- assembling this program from Python source and handing
it to ``[sys.executable, "-c", <string>]`` -- has one defect the code itself is
blameless for: no linter parses it, and tests can only string-match the source.
So the transaction that moves ``node_modules`` aside and puts it back on failure
-- the part whose whole point is not to lose a dependency tree -- is unreachable
by an executing test. A module makes every branch a real function a test can
drive against a real directory tree.

It mirrors :mod:`npm_preflight`'s discipline exactly, and for the same reasons:

* **Stdlib only.** It imports nothing from ``kiro_crew``. Everything that would
  otherwise come from the package -- the reserved exit codes, the one step label
  allowed to assert a diagnosis -- is passed IN, on the command line or the
  environment, never interpolated into source and never imported. That is what
  lets it be snapshotted out and run BY PATH: what it does cannot change with the
  revision being merged underneath it.

* **Run by path, never by module.** ``server`` copies this file's bytes into an
  ``mkdtemp`` snapshot and invokes ``[sys.executable, <snapshot_path>, ...]``.
  ``-m kiro_crew...sync_runner`` would import it from the working tree AFTER the
  merge has landed, dragging the whole package ``__init__`` chain in with it -- so
  a merged revision that raised the ``requires-python`` floor with newer syntax
  anywhere in that chain would ``SyntaxError`` while being parsed. Executing the
  copied file keeps the only parsed file one the launching interpreter already
  ran. The snapshot-not-import rule is an invariant, not a convenience.

The step list arrives as JSON from a FILE PATH argument, never embedded in the
source, so a non-ASCII checkout path in a step's argv or env cannot break the
program that reads it.

The exit code IS the diagnosis. The reserved codes come from :mod:`npm_preflight`
(passed in as ``--reserved``); this runner is the thing that enforces the
reservation -- a reserved code from any step other than the one trusted label is
DEMOTED to a plain failure, because every other step runs worktree-controlled
code that can exit any number it likes. stdout carries only human-readable log
text; nothing printed here is promoted into the authoritative diagnosis.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - running the sync's own steps is this module's purpose
import sys
import threading

#: Suffix appended to a stash path to name its backup. A ``node_modules`` moved
#: aside for the duration of a step lands at ``<stash>`` + this.
_BACKUP_SUFFIX = ".kirocrew-sync-backup"

#: How many trailing stderr lines of a FAILED step are re-emitted as
#: ``::steperr::`` markers. Four covers the shape git and pip use -- a headline,
#: the offending paths, the remedy sentence -- without turning the banner into a
#: log window; the full log is one click away in the dashboard either way.
_STEPERR_TAIL = 4

#: How long to wait for a step's stderr pump to reach EOF after the step exits.
#: Bounded rather than unbounded because a GRANDCHILD (npm spawns several)
#: inherits the write end of that pipe, so a survivor keeps it open and EOF never
#: arrives. Before stderr was piped such a survivor simply kept writing into the
#: inherited pipe and the runner exited anyway, so waiting forever here would
#: turn a cosmetic leak into a wedged Pull+Build. Late lines are dropped instead.
_STEPERR_DRAIN_S = 5.0

#: The gateway reads this runner's pipe with ``asyncio.StreamReader.readline()``,
#: whose limit is 64 KiB of BYTES (``runtime.py``). A line past it raises
#: ``LimitOverrunError`` there, and that handler reaps the whole process tree --
#: so the ceiling this pump forwards under is not ours to pick.
_GATEWAY_LINE_BYTES = 64 * 1024

#: Per-read ceiling on a step's stderr, in characters. DERIVED, not picked.
#:
#: A step runs worktree-controlled code, so it can write a newline-free blob of
#: any length, and ``for line in stream`` would allocate the whole blob inside
#: this runner. ``readline(cap)`` bounds each read instead: a longer run comes
#: back in cap-sized pieces and every piece is forwarded, so splitting is the only
#: effect and the allocation is fixed. That is the bounded-reader posture
#: ``test_jsonl_util.py::TestNoUnboundedHandleIteration`` requires; this module
#: cannot call the repo's own ``jsonl_util`` readers because it is stdlib-only
#: and executes from a snapshot by path.
#:
#: The cap counts CHARACTERS, because the stream is a text wrapper, while the
#: gateway's limit counts BYTES -- so the two units have to be reconciled here or
#: the bound is a bound in name only. A Python character encodes to at most 4
#: UTF-8 bytes and the pump appends one newline, so the worst-case encoded line
#: is ``cap * 4 + 1`` bytes; dividing the gateway's byte ceiling by that is the
#: whole derivation. Multibyte stderr (a non-ASCII checkout path, a localized
#: git message) is ordinary, not exotic, and a character cap chosen as a round
#: number holds only for ASCII.
_STEPERR_READ_CAP = (_GATEWAY_LINE_BYTES - 1) // 4

#: Ceiling on ONE remembered tail line, in characters. The tail names a failure
#: in a one-notice banner, not in a log, so a cap-sized piece has no business
#: being rendered whole. The full piece still reaches the log.
_STEPERR_LINE_CHARS = 500


def gone(path: str) -> bool:
    """Remove *path* and report whether it is now absent.

    ``rmtree(..., ignore_errors=True)`` alone is not safe HERE, even though it is
    the right default elsewhere: every deletion in the transaction decides what
    the next rename does, so a partial removal that is silently ignored leaves a
    directory in place, makes the following rename fail, and ends with the
    transaction restoring a PARTIAL tree over a good one. So the load-bearing
    deletions are CONFIRMED, and one that will not complete stops the step with
    both trees intact -- a refused sync is recoverable, a half-restored
    ``node_modules`` is not.

    ``rmtree`` REFUSES a symlink ("Cannot call rmtree on a symbolic link"), and
    ``ignore_errors=True`` swallows that refusal -- so a symlinked
    ``node_modules`` left its backup undeletable, the next sync saw both paths,
    and every Pull + Build from then on refused as ambiguous: a permanent wedge
    escapable only by hand. So unlink the link, and ``rmtree`` only real trees.

    ``lexists``, not ``exists``: a DANGLING symlink is still something at this
    path, and reporting it as gone would let the runner proceed as though the
    slot were clear.
    """
    if os.path.islink(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    else:
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def reconcile_leftovers(steps: list[dict], exit_tree_ambiguous: int) -> None:
    """Reconcile any leftover stash/backup state from an earlier run.

    Runs BEFORE any step, because both of its decisions are knowable from disk
    with nothing applied. Splitting them off onto the ``npm ci`` step was a
    defect: a run killed just after stashing left ``node_modules`` absent and its
    intact backup unclaimed, and the next run's recovery then sat behind every
    earlier step succeeding -- so a still-failing preflight meant the tree stayed
    missing with the copy right there.

    * BOTH tree and backup present is genuinely AMBIGUOUS -- the stash may be a
      partial tree ``npm ci`` was writing when killed (backup is the last good
      one), or the good tree after a successful sync whose backup cleanup failed
      (backup is stale). Nothing on disk tells those apart, so either choice
      destroys the good copy in one case. Touch NEITHER and exit
      ``exit_tree_ambiguous`` (the gateway-passed diagnosis code), naming both
      paths so the operator can act.
    * Backup only is unambiguous recovery: claim it now with a rename.

    ``lexists`` for every presence gate, never ``isdir``: ``isdir`` FOLLOWS a
    symlink, so a DANGLING ``node_modules`` reads as absent and the backup-only
    branch would then ``os.rename(<dir>, <dangling link>)``, which fails ENOTDIR
    and crashes the runner on every sync with the tree never recovered.
    """
    for st in steps:
        stash = st.get("stash")
        if not stash:
            continue
        backup = stash + _BACKUP_SUFFIX
        have_tree = os.path.lexists(stash)
        have_backup = os.path.lexists(backup)
        if have_tree and have_backup:
            # The paths are LOG text; the diagnosis is the exit code, which the
            # gateway maps. Nothing here is promoted out of stdout.
            emit("a previous sync left a dependency-tree backup beside the tree")
            emit("tree: %s" % stash)
            emit("backup: %s" % backup)
            sys.exit(exit_tree_ambiguous)
        if have_backup:
            emit("restoring a dependency tree left stashed by an earlier run")
            os.rename(backup, stash)


class NodeModulesTransaction:
    """Move a stash path aside for a step and restore it on failure.

    ``npm ci`` empties ``node_modules`` before it installs, so a tree it emptied
    is the one artifact of a failed sync that cannot be rebuilt without the
    registry -- exactly what is unavailable when that step fails. So the tree is
    moved aside on ``__enter__`` and, on ``__exit__``:

    * step succeeded (``rc == 0``): the backup is DROPPED. If it will not delete
      the sync still worked -- the tree on disk is the new good one and the next
      run's both-exist branch handles the leftover -- so this is a note, not a
      failure.
    * step failed and the (now clean) stash slot can be cleared: the backup is
      renamed back and the failure becomes a no-op.
    * step failed but the partial tree at the stash slot will NOT clear: forcing
      the rename is how a partial tree ends up installed over a good backup.
      Leave BOTH, name them, and REPLACE the exit code with
      the restore-failed code -- "the tree could not be put back" outranks
      whatever the step itself failed with, because it is the part the operator
      has to act on.

    Leftover state is reconciled by :func:`reconcile_leftovers` before the loop,
    so a backup cannot already exist on ``__enter__``. ``lexists`` throughout: a
    SYMLINKED ``node_modules`` must still be moved aside and restored, and a
    DANGLING backup is still something to put back -- ``isdir`` would skip both,
    which is the data loss the transaction exists to prevent.

    Used as a context manager whose ``rc`` attribute is read on exit; the caller
    pre-seeds it non-zero so an exception restores rather than discards.
    """

    def __init__(self, stash: str | None, exit_restore_failed: int):
        self.stash = stash
        self.exit_restore_failed = exit_restore_failed
        self.backup = (stash + _BACKUP_SUFFIX) if stash else None
        #: The step's result. Pre-seeded non-zero so an exception in the body
        #: takes the restore path, not the drop path.
        self.rc = 1

    def __enter__(self) -> "NodeModulesTransaction":
        if self.stash and self.backup and os.path.lexists(self.stash):
            os.rename(self.stash, self.backup)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        backup = self.backup
        stash = self.stash
        if not (backup and stash and os.path.lexists(backup)):
            return
        if self.rc == 0:
            if not gone(backup):
                emit(
                    "note: a dependency-tree backup could not be removed and "
                    "was left at %s" % backup
                )
        elif gone(stash):
            os.rename(backup, stash)
            emit("restored %s after a failed step" % stash)
        else:
            emit("partial: %s" % stash)
            emit("backup: %s" % backup)
            self.rc = self.exit_restore_failed
        # Never suppress an exception: the step body's rc handling is done before
        # exit, and an exception must still propagate after the tree is restored.
        # (Returning None -- always falsy -- is the no-suppression contract.)


#: Serializes every write this runner makes to its stdout pipe.
#:
#: The gateway reads that pipe with a LINE reader, so a line is only bounded if
#: nothing can splice into the middle of it. Two pumps plus the main thread's
#: marker prints are three writers; without one lock a step's newline-free run
#: could prepend to another's terminated line and the merged inter-newline run
#: would exceed :data:`_GATEWAY_LINE_BYTES` however tightly each writer capped
#: itself. Holding this for every write is what makes the bound true by
#: construction rather than by hope.
_WRITE_LOCK = threading.Lock()


def emit(line: str) -> None:
    """Write ONE whole line to the runner's stdout, under the shared lock.

    Every write to that pipe goes through here -- the pumps and the step markers
    alike. A caller that bypasses it reintroduces the splice this lock exists to
    prevent, so there is deliberately no second path.
    """
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _pump(stream, tail: "collections.deque[str] | None") -> None:
    """Relay one of a step's streams to our stdout, optionally keeping its tail.

    Runs on a thread for the step's whole lifetime, so a step that writes more
    than a pipe buffer's worth cannot block waiting for a reader. Each piece is
    written through IMMEDIATELY, so piping costs nothing in liveness: the
    dashboard's "current activity" line keeps advancing.

    Reads are bounded by :data:`_STEPERR_READ_CAP` rather than iterating the
    handle, so a newline-free blob from a worktree-controlled step cannot become
    one unbounded allocation here. A blob longer than the cap arrives as
    cap-sized pieces, each forwarded, so splitting it across log lines is the
    only effect.

    *tail* is a deque for the stream whose last lines name a failure (stderr) and
    ``None`` for the one that does not (stdout). Blank pieces are forwarded but
    never remembered -- they pad a diagnosis, they are never the diagnosis.
    """
    while True:
        chunk = stream.readline(_STEPERR_READ_CAP)
        if not chunk:
            break
        line = chunk.rstrip("\n")
        emit(line)
        if tail is not None and line.strip():
            tail.append(_trim_tail_line(line))


def _trim_tail_line(line: str) -> str:
    """Cut a remembered line to banner length, MARKED when it was cut.

    The tail is rendered in a one-line notice, so a cap is right; a silent cut is
    not. A diagnosis trimmed without a marker reads as the whole sentence, which
    is the same class of harm as naming a progress line -- the reader believes
    something the output does not support.
    """
    if len(line) <= _STEPERR_LINE_CHARS:
        return line
    return line[:_STEPERR_LINE_CHARS] + "..."


def run_step(st: dict, cwd: str) -> tuple[int, list[str]]:
    """Run one step's subprocess; return its exit code and its stderr tail.

    Each step is a separate process whose stdout AND stderr are piped to us, and
    which re-derives its encoding from the locale -- so a Python step (pip, the
    build-and-stage child) would encode a non-ASCII checkout path with the
    codepage and die on it. ``PYTHONIOENCODING`` is the only channel that reaches
    a child, so it is set here on every step's env -- non-Python steps (git, npm)
    ignore it and are unaffected. Assigned rather than defaulted: the reader's
    encoding is fixed, so a divergent inherited value would be the defect.

    **Both streams are piped, and stderr's tail is returned, because the ORDER of
    a failed step's output in one merged pipe is a lie.** With stdout and stderr
    sharing one descriptor, a child block-buffers stdout to a pipe while writing
    stderr unbuffered -- so the stdout buffer flushes at EXIT, after the
    diagnostic. ``git merge --ff-only`` refused for local changes emits, in pipe
    order::

        error: Your local changes to the following files would be overwritten ...
        Please commit your changes or stash them before you merge.
        Aborting
        Updating 2f9ed9724..bf09e50e5     <- stdout, flushed last

    The dashboard promotes the LAST output line when the exit code carries no
    reserved diagnosis, so a bare last-line rule names ``Updating <old>..<new>``
    -- a progress line -- as the reason Pull+Build failed. Returning the tail as
    data lets the failure be named from the stream that carries diagnostics,
    instead of from a position that depends on libc buffering.

    **The step writes to neither pipe directly; this runner is the sole writer.**
    That is what makes the byte bound real. Capping our own writes bounds nothing
    while a step also owns the descriptor: it can emit a newline-free blob that
    prepends to a terminated relay line, and the merged inter-newline run the
    gateway reads exceeds :data:`_GATEWAY_LINE_BYTES` however tightly each writer
    capped itself -- which reaps the process tree. With both streams piped and
    every write going through :func:`emit` under one lock, each line the gateway
    sees is whole and under the ceiling by construction.

    What this guarantees, exactly -- four statements, no more:

    1. Order WITHIN one stream is preserved.
    2. Order ACROSS the two is unspecified: two pumps relay independently, so
       they interleave by timing.
    3. Every forwarded line is WHOLE and under :data:`_GATEWAY_LINE_BYTES`. No
       writer can splice into another's line.
    4. Everything the STEP ITSELF wrote is relayed. The pumps read to EOF, and by
       the time ``wait()`` returns the step is gone and its bytes are already in
       the pipes. Output written AFTER the drain cutoff by something that outlived
       the step -- the surviving grandchild :data:`_STEPERR_DRAIN_S` exists for --
       is dropped, and that trade is the point of bounding the join.

    (2) is the premise of the change rather than a gap in it: a position in this
    stream was never evidence of what failed, which is exactly why the tail is
    labelled instead of located. Nothing is filtered or reordered within a stream.
    """
    env = dict(st["env"])
    env["PYTHONIOENCODING"] = "utf-8:replace"
    tail: collections.deque[str] = collections.deque(maxlen=_STEPERR_TAIL)
    proc = subprocess.Popen(  # nosec B603 - argv list, no shell
        st["argv"],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    # Daemon so an undrainable pipe (see _STEPERR_DRAIN_S) cannot hold up
    # interpreter exit either. stdout gets no tail: the tail names a failure, and
    # naming it from stdout is the defect this whole change exists to remove.
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, None), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, tail), daemon=True),
    ]
    for pump in pumps:
        pump.start()
    rc = proc.wait()
    # AFTER wait(): the child is gone, so the pumps are draining what is left in
    # the pipes and reach EOF unless a surviving grandchild holds one open.
    for pump in pumps:
        pump.join(timeout=_STEPERR_DRAIN_S)
    return rc, list(tail)


def demote_reserved(rc: int, label: str, reserved: frozenset[int], preflight_label: str) -> int:
    """Demote a reserved diagnosis code from an untrusted step to a plain failure.

    An exit code is only trustworthy from the step whose binary is OURS. Every
    other step runs worktree-controlled code -- an npm lifecycle script, a vite
    config -- and can exit any number it likes, so a forged 41 would make the
    dashboard assert a registry-credential failure, WITH a remedy, for what was
    actually a build error. A reserved code from any step but the preflight is
    therefore reported as a plain failure, with the true code kept in the log
    rather than believed. Pure: returns the code to use, printing a log line only
    when it demotes.
    """
    if rc in reserved and label != preflight_label:
        emit(
            "step %s exited %d, which is a reserved diagnosis code; reporting "
            "it as a plain failure because only the %s step may assert one"
            % (label, rc, preflight_label)
        )
        return 1
    return rc


def run_steps(
    steps: list[dict],
    cwd: str,
    reserved: frozenset[int],
    preflight_label: str,
    exit_restore_failed: int,
    exit_frontend_skip: int | None = None,
    frontend_labels: frozenset[str] = frozenset(),
) -> int:
    """Run the reconciled step list in order, fail-fast, with the transaction.

    Emits one ``::step::<idx>::<label>`` marker per step -- the run worker parses
    these to name the current step in the dashboard. A step that FAILS is
    followed by up to :data:`_STEPERR_TAIL` ``::steperr::<idx>::<line>`` markers
    carrying the tail of its stderr, which is what lets the dashboard name the
    failure without trusting output ORDER in a merged pipe (see
    :func:`run_step`). Returns the exit code to exit with: 0 if every step
    passed, otherwise the first non-zero code (after reserved-code demotion and
    any restore-failure override).

    The frontend-skip verdict is held HERE, in runner state, not on disk. When
    the trusted preflight step (identified by ``preflight_label``) exits
    ``exit_frontend_skip``, the incoming ref proved the frontend install and
    build are already present, so this treats that as success and skips every
    later step whose label is in ``frontend_labels`` -- WITHOUT running their
    node_modules transaction (a skipped step's transaction would move the tree
    aside and drop the backup on its no-op exit, deleting it). The verdict can
    come ONLY from the preflight: a worktree-run step (a pip lifecycle script)
    exiting the same code is demoted to a plain failure by
    :func:`demote_reserved`, so an untrusted step cannot forge a "skip the
    build" verdict and ship stale assets.
    """
    skip_frontend = False
    for i, st in enumerate(steps):
        emit("::step::%d::%s" % (i, st["label"]))
        if skip_frontend and st["label"] in frontend_labels:
            emit(
                "::skip::%d::%s -- backend-only sync, frontend unchanged and "
                "node_modules populated" % (i, st["label"])
            )
            continue
        with NodeModulesTransaction(st.get("stash"), exit_restore_failed) as txn:
            rc, steperr = run_step(st, cwd)
            rc = demote_reserved(rc, st["label"], reserved, preflight_label)
            # The preflight asserting the frontend is already built is a SUCCESS
            # that also suppresses the two frontend steps. Only the trusted
            # preflight label may assert it: demote_reserved above has already
            # turned this same code from any other step into a plain failure, so
            # a worktree-run step cannot forge the skip.
            if st["label"] == preflight_label and rc == exit_frontend_skip:
                skip_frontend = True
                rc = 0
            txn.rc = rc
        # The transaction may have overridden rc to its restore-failed code.
        rc = txn.rc
        if rc != 0:
            # Re-emit the failing step's stderr tail under its own marker, so the
            # dashboard can name the failure from the stream diagnostics arrive
            # on rather than from the last line in a merged pipe -- which for a
            # step that block-buffered its stdout is a stale progress line (see
            # `run_step`). Emitted only on failure and only for the step that
            # failed: on the success path there is nothing to name.
            #
            # This is still LOG TEXT, not a diagnosis. The authoritative cause
            # remains the exit code, mapped by the gateway, and these lines are
            # presented as the raw tail they are -- so a worktree-run step that
            # prints a plausible sentence to stderr gains exactly what it already
            # had: its output shown verbatim in the failure notice.
            for line in steperr:
                emit("::steperr::%d::%s" % (i, line))
            return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: the sync runs a SNAPSHOT of this module as one process.

    The steps come from a FILE PATH, never embedded in the source. The reserved
    exit codes and the one trusted step label come in on the command line so this
    module needs to import nothing from ``kiro_crew``.
    """
    # Align the writer with the reader. ``_start_run`` decodes this stream as
    # UTF-8, but a piped stdout on Windows encodes with the process locale
    # codepage -- so any non-ASCII that reached a print here would be mangled or
    # raise UnicodeEncodeError, killing the runner before its first step.
    # errors="replace" additionally guarantees no print can be fatal.
    # (getattr: typeshed's TextIO lacks reconfigure; the runtime object has it
    # on CPython >= 3.7, and a wrapped/absent stdout simply skips the tune-up.)
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Dev Fleet Pull+Build sync runner.")
    ap.add_argument("steps_json", help="path to a JSON file holding the step list")
    ap.add_argument("cwd", help="working directory every step runs in")
    ap.add_argument(
        "--reserved",
        required=True,
        help="comma-separated reserved diagnosis exit codes (from npm_preflight)",
    )
    ap.add_argument(
        "--preflight-label",
        required=True,
        help="the ONE step label whose reserved exit code may be trusted",
    )
    ap.add_argument(
        "--exit-tree-ambiguous",
        required=True,
        type=int,
        help="exit code for tree+backup both present (npm_preflight owns the value)",
    )
    ap.add_argument(
        "--exit-restore-failed",
        required=True,
        type=int,
        help="exit code for a failed post-step restore (npm_preflight owns the value)",
    )
    ap.add_argument(
        "--exit-frontend-skip",
        type=int,
        default=None,
        help=(
            "exit code the preflight uses to assert the frontend install/build "
            "is already present (npm_preflight owns the value); trusted only "
            "from the preflight step, demoted from any other. Omitted disables "
            "the suppression, so both frontend steps always run"
        ),
    )
    ap.add_argument(
        "--frontend-labels",
        default="",
        help=(
            "comma-separated step labels suppressed when the preflight asserts "
            "the frontend-skip verdict"
        ),
    )
    ap.add_argument(
        "--steps-sha256",
        required=True,
        help=(
            "hex SHA-256 the steps file's bytes must match. argv is fixed at "
            "exec, so this pins the manifest the gateway composed: a steps file "
            "rewritten after staging fails the check and nothing runs"
        ),
    )
    args = ap.parse_args(argv)

    with open(args.steps_json, "rb") as fh:
        raw = fh.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.steps_sha256:
        # The manifest is not the one the gateway staged. Refuse before parsing:
        # the pinned digest travels in argv (immutable once this process exists),
        # so a rewrite of the file between staging and startup cannot substitute
        # steps. Content is diagnosis; the refusal is the protection.
        emit(
            "sync runner: steps file does not match the staged manifest "
            "(sha256 %s != expected %s)" % (digest, args.steps_sha256)
        )
        return 1
    steps = json.loads(raw.decode("utf-8"))
    reserved = frozenset(int(c) for c in args.reserved.split(",") if c.strip())
    frontend_labels = frozenset(s for s in args.frontend_labels.split(",") if s.strip())

    reconcile_leftovers(steps, args.exit_tree_ambiguous)
    return run_steps(
        steps,
        args.cwd,
        reserved,
        args.preflight_label,
        args.exit_restore_failed,
        args.exit_frontend_skip,
        frontend_labels,
    )


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
