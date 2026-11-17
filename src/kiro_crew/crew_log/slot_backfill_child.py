"""A slot fold's cold backfill, in a child process, handed back as a staged savepoint.

Why this is not a thread
------------------------
A slot fold with no warm cell and no usable savepoint reads every unit the slot ran
under from its first record. On a busy gateway that is the background prime
(:mod:`kiro_crew.crew_log.slot_prime`), and it was the largest GIL holder there. The
answer the gateway needs is small next to the work: a savepoint it can resume from. So
a child process folds the slot with the gateway's own read and writes the savepoint
that read would write, and the gateway resumes from it.

Why a staging directory
-----------------------
The child cannot write the savepoint where it belongs. :func:`checkpoint.save_slot`
takes the unit's lease, and the gateway's writers already hold it, so the child would
be refused and the fold wasted. In the child, ``checkpoint.slot_save_dir`` points the
write at ``<home>/crew-log/slot-staging/<unit dir>/<fold>.json`` instead, taking no
lease. That directory is outside the unit's own, so a unit removal never races a file
the child writes. The write is the store's own atomic write, so a half-written file
is never visible.

The gateway takes the file in :func:`admit`, called from
``projection_slot_memo._fold_slot_warm_locked`` with the fold lock held, only after both
savepoint resumes failed. Under the unit's lease it moves the file into the unit's
``slot-projections`` directory, then resumes from it exactly as from any savepoint:
``checkpoint.load_slot`` checks the identity block and the witness against the files as
they are now. A file that does not match (the unit was recreated, gained a unit, was
cut) is refused there and the read goes on as before. A unit being removed refuses the
lease, and the staged file is deleted.

Fail safe
---------
At most one child runs per process. :func:`backfill` answers ``False`` when it is off,
another backfill is running, or the child failed (could not start, timed out, exited
non-zero, wrote a bad reply), and the caller folds in the gateway as before. After
:data:`_MAX_FAILURES` failures in a row the child is not used again in this process.
OFF unless the gateway calls :func:`enable`; ``KIROCREW_SLOT_BACKFILL_CHILD=0`` keeps
it off there.

The child never calls ``read_budget.mark_gateway_process``, so its reads are not
counted against the gateway's read budget.

``python -m kiro_crew.crew_log.slot_backfill_child`` is the child's entry point, with
``{"home", "slot", "fold"}`` as one JSON object on stdin; nothing else runs it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

_CHILD_MODULE: Final[str] = "kiro_crew.crew_log.slot_backfill_child"
#: Turns the child off in a gateway when set to ``0``.
SWITCH_ENV: Final[str] = "KIROCREW_SLOT_BACKFILL_CHILD"
#: Longest one backfill may take, start-up included; past it the child is killed.
BACKFILL_TIMEOUT_SECS: Final[float] = 900.0
#: Child failures in a row after which this process stops using the child.
_MAX_FAILURES: Final[int] = 3
#: A staged file older than this was never admitted (its gateway restarted, or no read
#: came for it) and is deleted before the next backfill starts.
STALE_STAGED_SECS: Final[float] = 3600.0
#: Leaf under ``<home>/crew-log`` holding the staged savepoints.
STAGING_DIR: Final[str] = "slot-staging"
_CHILD_NICE: Final[int] = 10

_running = threading.Lock()
_state: "dict[str, Any]" = {"enabled": False, "failures": 0, "disabled": False}


def enable() -> None:
    """Use the child in this process. Called once, by the gateway's boot."""
    _state["enabled"] = True


def usable() -> bool:
    """Whether :func:`backfill` may run a child in this process right now."""
    return (
        bool(_state["enabled"])
        and not _state["disabled"]
        and os.environ.get(SWITCH_ENV, "").strip() != "0"
    )


def reset_for_tests() -> None:
    _state.update(enabled=False, failures=0, disabled=False)


def staging_root() -> Path:
    """``<home>/crew-log/slot-staging``. Does not create it."""
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(KIND_SESSION).parent / STAGING_DIR


def staged_path(unit_id: str, name: str) -> Path:
    """Where the child stages *unit_id*'s savepoint for fold *name*."""
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import crew_log_dir

    return staging_root() / crew_log_dir(KIND_SESSION, unit_id).name / f"{name}.json"


def _staging_dir_for(handle: Any) -> "Path | None":
    """The child's ``checkpoint.slot_save_dir``: the unit's staging directory, created."""
    try:
        from kiro_crew.owner_only_files import ensure_directory

        directory = staged_path(handle.id, "x").parent
        # Owner-only for slot-staging and the unit's directory alike: staged files
        # carry fold state, as the unit's own savepoints do.
        ensure_directory(directory)
        return directory
    except Exception:
        logger.debug("slot backfill staging directory unavailable for %s", handle.id)
        return None


def admit(name: str, unit_id: str) -> bool:
    """Move a staged savepoint for *name* into *unit_id*'s unit. ``True`` when moved.

    Never raises. Off in the child itself, which stages and never admits.
    """
    from kiro_crew.crew_log import checkpoint as savepoints
    from kiro_crew.crew_log.projection_session_log import open_session_log
    from kiro_crew.crew_log.store import log_exception_text

    if savepoints.slot_save_dir is not None:
        return False
    try:
        staged = staged_path(unit_id, name)
        if not staged.is_file():
            return False
        handle = open_session_log(unit_id)
        lease = savepoints._hold(handle) if handle is not None else None
        if handle is None or lease is None:
            # The unit is gone or being removed: nothing to admit it into.
            staged.unlink(missing_ok=True)
            return False
        try:
            directory = savepoints._ensure_dir(handle, savepoints.SLOT_CHECKPOINT_DIR)
            if directory is None:
                return False
            os.replace(staged, directory / f"{name}.json")
            return not savepoints._discard_if_unit_gone(handle, directory)
        finally:
            savepoints.release_lease(lease)
    except Exception:
        log_exception_text(
            logger, logging.DEBUG, "slot backfill for %s/%s not admitted", unit_id, name
        )
        return False


def backfill(
    slot: str, name: str, *, timeout: float = BACKFILL_TIMEOUT_SECS, argv: "list[str] | None" = None
) -> bool:
    """Run the child for *slot*'s *name*. ``True`` when it staged a savepoint.

    ``False`` without a child for a cell last refused as too big to keep
    (``slot_fold_over_ceiling``): its savepoint would be refused too, so the child would
    fold it for nothing. A child that folded but staged nothing (a state over the
    savepoint cap) also answers ``False``, and the caller folds once in the gateway.

    *argv* replaces the child command line (tests use it for a child that hangs,
    crashes or replies badly).
    """
    from kiro_crew.crew_log.projection_slot_memo import slot_fold_over_ceiling

    if not usable() or slot_fold_over_ceiling(slot, name) or not _can_earn_savepoint(slot, name):
        return False
    if not _running.acquire(blocking=False):
        return False
    try:
        from kiro_crew import platform_compat
        from kiro_crew.config.paths import data_home
        from kiro_crew.embeddings_child import _child_creationflags

        _sweep_stale()
        if argv is None:
            # Fixed: this interpreter, -P, this module. What to fold travels on stdin.
            argv = platform_compat.isolated_python_argv("-P", "-m", _CHILD_MODULE)
        request = json.dumps({"home": str(data_home()), "slot": slot, "fold": name})
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
                argv,
                input=request.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                timeout=timeout,
                check=False,
                # No console window and below-normal priority on Windows, where the
                # child cannot os.nice itself; 0 elsewhere.
                creationflags=_child_creationflags(),
            )
        except subprocess.TimeoutExpired:
            return _failed(slot, name, f"timed out after {timeout:.0f} s")
        except OSError as exc:
            return _failed(slot, name, f"could not start: {exc}")
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
            return _failed(slot, name, f"exited with code {proc.returncode}: {tail}")
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            return _failed(slot, name, "wrote a malformed reply")
        staged = reply.get("staged")
        if not isinstance(staged, bool):
            return _failed(slot, name, "wrote a malformed reply")
        _state["failures"] = 0
        return staged
    finally:
        _running.release()


def _can_earn_savepoint(slot: str, name: str) -> bool:
    """Whether a cold fold of *slot*'s *name* would write a savepoint at all.

    It writes one only when every unit's mark is known and the units hold at least
    ``checkpoint.MIN_ADVANCE_ENTRIES`` entries (``write_is_earned`` from 0); anything
    less stages nothing, and the child would fold for nothing. Never raises: any
    doubt answers ``False`` and the caller folds in the gateway as before.
    """
    try:
        from kiro_crew.config.paths import data_home
        from kiro_crew.crew_log.checkpoint import MIN_ADVANCE_ENTRIES
        from kiro_crew.crew_log.projection_readers import _slot_units_for_fold
        from kiro_crew.crew_log.projection_slot_memo import _unit_marks

        marks = _unit_marks(_slot_units_for_fold(slot, name), str(data_home()))
        if not marks or any(mark.origin is None for mark in marks):
            return False
        return sum(mark.last_seq for mark in marks) >= MIN_ADVANCE_ENTRIES
    except Exception:
        return False


def _failed(slot: str, name: str, why: str) -> bool:
    _state["failures"] += 1
    if _state["failures"] >= _MAX_FAILURES and not _state["disabled"]:
        _state["disabled"] = True
        logger.warning(
            "slot backfill child failed %d times in a row (last: %s/%s, %s); folding in the "
            "gateway from now on",
            _state["failures"],
            slot,
            name,
            why,
        )
    else:
        logger.info("slot backfill child failed for %s/%s: %s", slot, name, why)
    return False


def _sweep_stale() -> None:
    """Delete staged files older than :data:`STALE_STAGED_SECS`. Never raises."""
    cutoff = time.time() - STALE_STAGED_SECS
    try:
        for unit_dir in staging_root().iterdir():
            for staged in unit_dir.iterdir():
                if staged.stat().st_mtime < cutoff:
                    staged.unlink(missing_ok=True)
    except OSError:
        pass


def _staged_stamps(name: str) -> "dict[str, int]":
    """Every staged ``<name>.json`` under the staging root, with its mtime."""
    found: "dict[str, int]" = {}
    try:
        for unit_dir in staging_root().iterdir():
            path = unit_dir / f"{name}.json"
            try:
                found[str(path)] = path.stat().st_mtime_ns
            except OSError:
                continue
    except OSError:
        pass
    return found


def _parse_request(raw: bytes) -> "tuple[str, str, str] | None":
    """``(home, slot, fold)`` from the child's stdin, or ``None`` when it is not that."""
    try:
        request = json.loads(raw)
        home, slot, name = (str(request[key]) for key in ("home", "slot", "fold"))
    except (ValueError, KeyError, TypeError):
        return None
    return home, slot, name


def _stage(home: str, slot: str, name: str) -> bool:
    """Fold *slot*'s *name* with the gateway's read, staging its savepoint instead.

    ``True`` when a staged file was written or rewritten. A state over the savepoint
    cap writes nothing, and the gateway must not wait for one.
    """
    os.environ["KIROCREW_HOME"] = home
    from kiro_crew.crew_log import checkpoint as savepoints
    from kiro_crew.crew_log import projection

    savepoints.slot_save_dir = _staging_dir_for
    before = _staged_stamps(name)
    projection.read_slot_projection(slot, name)
    return any(stamp != before.get(path) for path, stamp in _staged_stamps(name).items())


def main(argv: "list[str] | None" = None) -> int:
    """Child entry: fold one slot with the gateway's read, staging its savepoint.

    The request is one JSON object on stdin: ``{"home", "slot", "fold"}``.
    """
    args = sys.argv[1:] if argv is None else argv
    request = _parse_request(sys.stdin.buffer.read())
    if args or request is None:
        print(f"usage: {_CHILD_MODULE} < {{home, slot, fold}} as JSON", file=sys.stderr)
        return 2
    # The reply owns the original stdout; anything else that prints goes to stderr.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if hasattr(os, "nice"):
        try:
            os.nice(_CHILD_NICE)
        except OSError:
            pass
    staged = _stage(*request)
    out.write(json.dumps({"ok": True, "staged": staged}).encode("utf-8") + b"\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
