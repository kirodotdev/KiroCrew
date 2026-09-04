"""The Pull+Build sync runner, as a real module instead of a string literal.

This is the program that :func:`server._sync_start_locked` used to assemble from
Python source and hand to ``[sys.executable, "-c", <string>]``. That form had one
defect the code itself was blameless for: no linter parsed it, and its only tests
string-matched the source. So the transaction that moves ``node_modules`` aside
and puts it back on failure -- the part whose whole point is not to lose a
dependency tree -- was unreachable by an executing test. Extracting it here makes
every branch a real function a test can drive against a real directory tree.

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
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - running the sync's own steps is this module's purpose
import sys

#: Suffix appended to a stash path to name its backup. A ``node_modules`` moved
#: aside for the duration of a step lands at ``<stash>`` + this.
_BACKUP_SUFFIX = ".kirocrew-sync-backup"


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
            print(
                "a previous sync left a dependency-tree backup beside the tree",
                flush=True,
            )
            print("tree: %s" % stash, flush=True)
            print("backup: %s" % backup, flush=True)
            sys.exit(exit_tree_ambiguous)
        if have_backup:
            print(
                "restoring a dependency tree left stashed by an earlier run",
                flush=True,
            )
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
                print(
                    "note: a dependency-tree backup could not be removed and "
                    "was left at %s" % backup,
                    flush=True,
                )
        elif gone(stash):
            os.rename(backup, stash)
            print("restored %s after a failed step" % stash, flush=True)
        else:
            print("partial: %s" % stash, flush=True)
            print("backup: %s" % backup, flush=True)
            self.rc = self.exit_restore_failed
        # Never suppress an exception: the step body's rc handling is done before
        # exit, and an exception must still propagate after the tree is restored.
        # (Returning None -- always falsy -- is the no-suppression contract.)


def run_step(st: dict, cwd: str) -> int:
    """Run one step's subprocess and return its exit code.

    Each step is a separate process that inherits the runner's stdout pipe but
    re-derives its encoding from the locale, so a Python step (pip, the
    build-and-stage child) would encode a non-ASCII checkout path with the
    codepage and die on it. ``PYTHONIOENCODING`` is the only channel that reaches
    a child, so it is set here on every step's env -- non-Python steps (git, npm)
    ignore it and are unaffected. Assigned rather than defaulted: the reader's
    encoding is fixed, so a divergent inherited value would be the defect.
    """
    env = dict(st["env"])
    env["PYTHONIOENCODING"] = "utf-8:replace"
    return subprocess.run(  # nosec B603 - argv list, no shell
        st["argv"], cwd=cwd, env=env
    ).returncode


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
        print(
            "step %s exited %d, which is a reserved diagnosis code; reporting "
            "it as a plain failure because only the %s step may assert one"
            % (label, rc, preflight_label),
            flush=True,
        )
        return 1
    return rc


def run_steps(
    steps: list[dict],
    cwd: str,
    reserved: frozenset[int],
    preflight_label: str,
    exit_restore_failed: int,
) -> int:
    """Run the reconciled step list in order, fail-fast, with the transaction.

    Emits one ``::step::<idx>::<label>`` marker per step -- the run worker parses
    these to name the current step in the dashboard. Returns the exit code to
    exit with: 0 if every step passed, otherwise the first non-zero code (after
    reserved-code demotion and any restore-failure override).
    """
    for i, st in enumerate(steps):
        print("::step::%d::%s" % (i, st["label"]), flush=True)
        with NodeModulesTransaction(st.get("stash"), exit_restore_failed) as txn:
            rc = run_step(st, cwd)
            rc = demote_reserved(rc, st["label"], reserved, preflight_label)
            txn.rc = rc
        # The transaction may have overridden rc to its restore-failed code.
        rc = txn.rc
        if rc != 0:
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
        print(
            "sync runner: steps file does not match the staged manifest "
            "(sha256 %s != expected %s)" % (digest, args.steps_sha256),
            flush=True,
        )
        return 1
    steps = json.loads(raw.decode("utf-8"))
    reserved = frozenset(int(c) for c in args.reserved.split(",") if c.strip())

    reconcile_leftovers(steps, args.exit_tree_ambiguous)
    return run_steps(steps, args.cwd, reserved, args.preflight_label, args.exit_restore_failed)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
