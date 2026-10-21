"""How a run ends on disk.

The finished result, the tombstone (written exclusively, owed and retried when a write
fails), delivery marking and settling, the slow-command log, the folder's deletion, and
the orphans and leaked processes the registry lists.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Collection
from pathlib import Path
from typing import Callable, Protocol

from kiro_crew.atomic_write import fsync_dir
from kiro_crew.jsonl_util import rotate_jsonl_at
from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for
from kiro_crew.subagent_persistence_dismissals import _delete_panel_dismissal
from kiro_crew.subagent_persistence_layout import (
    _CLEANUP_IDENTITY_LOCK,
    _LIVE_CLEANUP_HINTS,
    _LIVE_CLEANUP_IDENTITIES,
    _agent_dir,
    _atomic_write,
    _delete_cleanup_identities_file,
    _live_cleanup_identities,
    _subagents_dir,
    logger,
    publish_live_cleanup_hint,
)
from kiro_crew.subagent_persistence_state import (
    _LIVE_RUN_STATES,
    _live_run_key,
    _read_tombstone_at,
    _replace_in_run_folder,
    read_state,
    update_state,
)

# ── tombstone ────────────────────────────────────────────────────────


def _check_result_available(path: Path) -> bool:
    """Check if result file exists and is non-empty (TOCTOU-safe)."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def result_is_whole(state: dict) -> bool:
    """Whether a run recorded that it finished with its whole answer.

    THE completeness fact. True means the run claimed its completed ending and
    rewrote ``result.txt`` whole from memory, durably, before recording it
    (:func:`write_finished_result`). Anything else means the bytes in
    ``result.txt``, if any, are a fragment: they are appended per streamed
    chunk, so they prove only that a token arrived. A run that finished without
    text is whole with no ``result.txt``. The orphan reconcile
    (``tombstone_recovery_action``) and the task queue's boot probe decide with
    this.
    """
    return bool(state.get("result_complete"))


def write_finished_result(
    agent_id: str,
    text: str | None,
    state_writer: Callable[..., bool] = update_state,
    /,
) -> bool:
    """Leave ``result.txt`` as a run's ending reads it, then record ``result_complete``.

    ONE worker job, so the executor finishes both halves even when its awaiting
    run gives up the drain. *text* is the whole answer of a run that claimed its
    completed ending: ``result.txt`` is rewritten from it, capped
    (``cap_result_bytes``), by temp file and rename, and fsynced with its folder,
    so the flag never vouches for a hole a failed append left or for bytes an OS
    crash could lose. An empty answer leaves no ``result.txt``, and removes a
    partial an earlier attempt left. A write that fails records
    ``result_complete=False`` instead, logged at WARNING. ``None`` is any other
    ending: the streamed file is capped in place (``cap_result_file``) and the
    flag recorded False; a whole rewrite that fails falls back to that same
    in-place cap, so the bound holds whatever refused the rename (a full disk,
    a Windows reader holding the file). *state_writer* is the caller's seam for
    ``update_state``. A transient run keeps its record in memory and its folder
    never exists, so it records the flag and touches no disk.
    """
    from kiro_crew.context_management import cap_result_bytes, cap_result_file

    whole = text is not None
    if _live_run_key(agent_id) not in _LIVE_RUN_STATES:
        try:
            path = _agent_dir(agent_id) / "result.txt"
            if text:
                try:
                    _replace_in_run_folder(path, cap_result_bytes(text.encode("utf-8")), fsync=True)
                    fsync_dir(path.parent)
                except OSError:
                    logger.warning(
                        "Could not write %s's finished result; it stays unflagged",
                        agent_id,
                        exc_info=True,
                    )
                    whole = False
                    cap_result_file(path)
            elif text is None:
                cap_result_file(path)
            else:
                path.unlink(missing_ok=True)
                fsync_dir(path.parent)
        except (OSError, ValueError):
            logger.warning(
                "Could not finish %s's result file; it stays unflagged", agent_id, exc_info=True
            )
            whole = False
    try:
        return bool(state_writer(agent_id, result_complete=whole))
    except (OSError, ValueError):
        logger.warning("Could not record result_complete for %s", agent_id, exc_info=True)
        return False


#: Tombstone writes that failed (a full disk, say), by run id, with the arguments
#: the write was given. A missing tombstone leaves the folder to the next boot's
#: orphan reconciliation, which announces the run again, so the reaper retries
#: these off the event loop each tick and graceful shutdown once more. Bounded:
#: past the cap, or for an entry larger than ``_OWED_TOMBSTONE_MAX_BYTES``, the
#: write is not retried and the folder stays an orphan -- announced again at the
#: next boot, never lost.
_OWED_TOMBSTONES_MAX = 4096
_OWED_TOMBSTONE_MAX_BYTES = 16_384
_owed_tombstones: dict[str, dict[str, object]] = {}
#: Held for a whole retry, so two retries never write at once (see
#: :func:`retry_owed_tombstones`).
_owed_retry_lock = threading.Lock()
_owed_tombstones_lock = threading.Lock()


def _owe_tombstone(
    agent_id: str, d: Path, cause: str, recovery_action: str, extra: dict[str, object]
) -> None:
    """Record a failed tombstone write for :func:`retry_owed_tombstones`.

    Not recorded when a tombstone is already on disk: the folder is then out of
    orphan reconciliation whatever the failed write would have replaced.
    """
    if (d / "tombstone.json").exists():
        return
    entry: dict[str, object] = {
        "cause": cause,
        "recovery_action": recovery_action,
        "extra": dict(extra),
    }
    try:
        size = len(json.dumps(entry, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        size = _OWED_TOMBSTONE_MAX_BYTES + 1
    with _owed_tombstones_lock:
        # The size bound applies to every write, a replacement included; the
        # count bound only to a run not recorded yet. A record that does not fit
        # also drops the run's earlier one, which describes an older ending.
        if size > _OWED_TOMBSTONE_MAX_BYTES or (
            agent_id not in _owed_tombstones and len(_owed_tombstones) >= _OWED_TOMBSTONES_MAX
        ):
            _owed_tombstones.pop(agent_id, None)
            dropped = True
        else:
            _owed_tombstones[agent_id] = entry
            dropped = False
    if dropped:
        logger.warning(
            "write_tombstone for %s is not retried (%d writes already waiting, or the "
            "entry is too large); the next start reports the run again",
            agent_id,
            len(_owed_tombstones),
        )


def has_owed_tombstones() -> bool:
    """Whether a failed tombstone write is waiting for its retry. No I/O."""
    return bool(_owed_tombstones)


def _create_exclusive(path: Path, data: dict) -> None:
    """Create *path* holding *data* only if it is absent and its directory exists.

    The content becomes visible whole (a temp file hard-linked into place), an
    existing file is never replaced (``FileExistsError``) and a removed directory is
    never recreated (``FileNotFoundError``). The temp file's name is unique to the
    call, so one left behind by a writer that died never blocks a later write.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".owed")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    fsync_dir(path.parent, best_effort=True)


def retry_owed_tombstones(
    *, stop: Callable[[], bool] | None = None, budget: float | None = None
) -> int:
    """Retry every failed tombstone write; return how many landed.

    Blocking file I/O: run it off the event loop. An entry is dropped without a
    write when its run folder is gone or a tombstone has appeared since (another
    path recorded the run's ending, which a retry must not replace). A write that
    fails again keeps its entry and logs at DEBUG, so a full disk does not log a
    WARNING on every tick.

    One retry runs at a time. *stop* is read before each write, and a true
    reading ends the retry there: the reaper passes the manager's shutdown flag,
    so a retry it began before shutdown ends after the write it is in, and the
    shutdown retry, which waits for it, is the last one to write. *budget*, in
    seconds, bounds that wait and the writes after it; entries still owed at the
    deadline stay owed, so their runs are reported again at the next start.
    """
    deadline = None if budget is None else time.monotonic() + budget
    if not _owed_retry_lock.acquire(timeout=-1 if budget is None else budget):
        logger.debug("Owed tombstone retry skipped: an earlier retry is still writing")
        return 0
    try:
        return _retry_owed_tombstones_locked(stop, deadline)
    finally:
        _owed_retry_lock.release()


def _retry_owed_tombstones_locked(stop: Callable[[], bool] | None, deadline: float | None) -> int:
    with _owed_tombstones_lock:
        pending = list(_owed_tombstones.items())
    landed = 0
    for agent_id, entry in pending:
        if stop is not None and stop():
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        extra = entry["extra"]
        if not isinstance(extra, dict):
            continue
        try:
            _write_tombstone_file(
                agent_id,
                str(entry["cause"]),
                str(entry["recovery_action"]),
                extra,
                owed_retry=True,
            )
        except (FileExistsError, FileNotFoundError):
            logger.debug(
                "Dropping the owed tombstone for %s: folder gone or tombstone written", agent_id
            )
        except OSError:
            logger.debug("Owed tombstone for %s still cannot be written", agent_id, exc_info=True)
            continue
        else:
            if (_agent_dir(agent_id) / "tombstone.json").exists():
                landed += 1
                logger.info(
                    "Wrote the %s tombstone for %s after an earlier write failed",
                    entry["cause"],
                    agent_id,
                )
        with _owed_tombstones_lock:
            if _owed_tombstones.get(agent_id) is entry:
                del _owed_tombstones[agent_id]
    return landed


def write_tombstone(
    agent_id: str,
    *,
    cause: str,
    recovery_action: str,
    **extra: object,
) -> None:
    """Write ``tombstone.json`` for an abnormally exited agent."""
    _write_tombstone_file(agent_id, cause, recovery_action, extra, owed_retry=False)


def _write_tombstone_file(
    agent_id: str,
    cause: str,
    recovery_action: str,
    extra: dict[str, object],
    *,
    owed_retry: bool,
) -> None:
    """:func:`write_tombstone`'s body; *owed_retry* marks a retry of a failed write."""
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        return
    d = _agent_dir(agent_id)
    state = read_state(agent_id)
    # A delayed delivery acknowledgement cannot recreate a released transient
    # run. Abnormal-exit diagnostics may still record missing state.
    if state is None and cause == "delivered":
        return
    state = state or {}
    execution = state.get("execution_context")
    if isinstance(execution, dict) and execution.get("memory_mode") in ("incognito", "temporary"):
        return
    cleanup_identity = {
        key: state[key] for key in ("session_id", "provider", "cwd") if state.get(key)
    }
    live_cleanup_identities = _live_cleanup_identities(agent_id)
    latest_live_identity = live_cleanup_identities[-1] if live_cleanup_identities else {}
    generation_metadata: dict[str, object] = {}
    if live_cleanup_identities:
        generation_metadata["cleanup_identities"] = live_cleanup_identities
    # A terminal outcome, once recorded, is not erased by a later write that does
    # not carry one. This write REPLACES the file, and only ``extra`` can supply
    # ``outcome``, so a caller that has no opinion about the outcome -- a delivery
    # acknowledgement is the ordinary one -- would otherwise drop the outcome an
    # earlier write recorded, and the reader would fall back to deriving one from
    # ``cause``. A user stop then reads as a completion. Carried forward here, at
    # the single write, rather than left for each call site to remember.
    #
    # ``died`` is carried through the SAME read, on the same grounds, with one
    # difference: every write stamps it, so the carry is what keeps it meaning the
    # run's ENDING rather than the moment of the most recent bookkeeping write. A
    # delivery acknowledgement lands whenever the parent got round to the result,
    # and the panel reads this field as the end of the run, so an uncarried stamp
    # shows the delivery wait as runtime.
    #
    # The read is taken on exactly one condition, ``outcome`` absent, and that is
    # deliberate rather than incidental: a caller supplying an outcome is RECORDING
    # an ending, so its own fresh stamp IS that ending and there is nothing to
    # carry. Widening the condition to ``died`` absent as well would take the read
    # on every such caller -- including sync writers reached from a coroutine, where
    # this function's synchronous file I/O must not land.
    carried: dict[str, object] = {}
    if "outcome" not in extra:
        prior, _present = _read_tombstone_at(d / "tombstone.json")
        if isinstance(prior, dict):
            if prior.get("outcome"):
                carried["outcome"] = prior["outcome"]
            if "died" not in extra and _finite_time(prior.get("died")):
                carried["died"] = prior["died"]
            # A process the kill left standing is still standing after a later
            # bookkeeping write (a delivery acknowledgement), so the record of
            # it is carried the same way rather than erased by that write.
            if "leaked_process" not in extra and prior.get("leaked_process") is True:
                carried["leaked_process"] = True
    tombstone = {
        "id": agent_id,
        "task": state.get("task", ""),
        "agent": state.get("agent", ""),
        "parent_session": state.get("parent_session", ""),
        "started": state.get("started"),
        "died": time.time(),
        "cause": cause,
        "recovery_action": recovery_action,
        "result_available": _check_result_available(d / "result.txt"),
        "result_path": str(d / "result.txt"),
        **cleanup_identity,
        **latest_live_identity,
        **generation_metadata,
        **carried,
        **extra,
    }
    try:
        if owed_retry:
            _create_exclusive(d / "tombstone.json", tombstone)
        else:
            _atomic_write(d / "tombstone.json", tombstone)
    except OSError:
        if owed_retry:
            raise
        logger.warning("write_tombstone failed for %s", agent_id, exc_info=True)
        # Retried with the same fields and the same ``died``: the tombstone a
        # retry writes describes this ending, not the moment space came back.
        _owe_tombstone(agent_id, d, cause, recovery_action, {**extra, "died": tombstone["died"]})
    state_sid = cleanup_identity.get("session_id")
    if not live_cleanup_identities and isinstance(state_sid, str) and state_sid:
        # The run folder is agent-writable. Preserve only the retention/exemption
        # signal here; provider-deletion authority requires gateway-published live
        # identity or the protected durable record.
        publish_live_cleanup_hint(agent_id)


def mark_delivered(
    agent_id: str,
    *,
    elapsed: float | None = None,
    credits: float | None = None,
    leaked_process: bool = False,
    outcome: str | None = None,
    detail: str | None = None,
) -> None:
    """Mark a successfully-delivered subagent for deferred TTL cleanup.

    ``leaked_process`` marks a delivered run whose kill left its process
    standing: the next start ends that process without a notice. Such a run
    has no earlier tombstone to carry its ending forward, so its ``outcome``
    and ``detail`` (the kill failure the parent was told) are passed here.

    Writes a ``cause="delivered"`` tombstone instead of deleting the folder
    immediately, so (a) orphan reconciliation skips it on restart and (b) the
    reaper prunes it after the (short) delivered TTL — giving the parent a grace
    window to read ``result.txt`` via ``spawn_status`` / read / grep after the
    completion event, rather than re-running the subagent.
    """
    terminal: dict[str, float] = {}
    if elapsed is not None:
        terminal["elapsed"] = elapsed
    if credits is not None:
        terminal["credits"] = credits
    record: dict[str, object] = dict(terminal)
    if leaked_process:
        record["leaked_process"] = True
    if outcome:
        record["outcome"] = outcome
    if detail:
        record["detail"] = detail
    write_tombstone(agent_id, cause="delivered", recovery_action="delivered", **record)


class _SettleableDelivery(Protocol):
    """The shape a held delivery has, without importing the class that defines it.

    ``SubagentDelivery`` lives in ``subagent``, which imports this module, so naming
    it here would close a cycle. The three fields are the whole contract.

    Declared as read-only properties, not as attributes: ``SubagentDelivery`` is a
    frozen dataclass, so its fields do not satisfy a protocol that asks for
    settable ones -- and settable is not what this asks for anyway, since the batch
    only ever reads them.
    """

    @property
    def agent_id(self) -> str: ...

    @property
    def elapsed(self) -> float: ...

    @property
    def credits(self) -> float: ...


def settle_delivered_batch(
    deliveries: "Collection[_SettleableDelivery]",
    *,
    writer: "Callable[..., None] | None" = None,
) -> None:
    """Mark every delivery in *deliveries* delivered, as ONE indivisible operation.

    Takes the held DELIVERIES rather than bare ids because the tombstone records
    the run's terminal usage: settling by id alone would write a delivered
    tombstone with no ``elapsed`` or ``credits``, and the panel then shows a
    finished wave member with no usage while its siblings carry theirs.

    For a caller that has already detached its ids from the state that held
    them: awaiting :func:`mark_delivered` once per id makes each write a
    cancellation point, and ``CancelledError`` is not an ``Exception``, so a
    shutdown or a cancel arriving mid-batch discards the ids it has not reached
    with nothing left holding them. Each unwritten tombstone leaves a folder that
    restart reconciliation admits, which replays as a duplicate completion.
    Offloaded whole by such a caller, this runs to the end of the batch whether or
    not its waiter is still waiting.

    ``writer`` is the write to perform per id, defaulting to
    :func:`mark_delivered`. A caller inside the subagent manager passes its OWN
    resolved ``mark_delivered`` instead, because the manager's modules resolve
    that name through the facade and the facade is the single point anything
    substituting the write replaces -- a batch that reached past it would be the
    one delivery path that ignored the substitution.

    A failing write is logged and skipped rather than raised, so one unwritable
    run folder cannot strand the rest of the batch.
    """
    write = mark_delivered if writer is None else writer
    for delivery in deliveries:
        try:
            write(delivery.agent_id, elapsed=delivery.elapsed, credits=delivery.credits)
        except Exception:
            logger.debug("Failed to settle delivered subagent %s", delivery.agent_id, exc_info=True)


def clear_tombstone(agent_id: str) -> bool:
    """Remove ``tombstone.json`` so the agent is visible to orphan recovery again.

    A tombstone is the marker :func:`list_orphans` uses to EXCLUDE a folder from
    restart reconciliation. That is correct once the outcome has reached the
    parent, but the terminal record is written BEFORE delivery is attempted — so
    if delivery is then abandoned (gateway shutdown cancelling a still-pending
    terminal report), the tombstone would suppress the one mechanism that could
    still hand the result to the parent, losing it permanently.

    Clearing it re-admits the folder to the next start's reconciliation, which
    sees ``result.txt`` and re-delivers. Returns True if a tombstone was
    removed. Best-effort: never raises to the caller.
    """
    try:
        p = _agent_dir(agent_id) / "tombstone.json"
        existed = p.exists()
        p.unlink(missing_ok=True)
        return existed
    except OSError:
        logger.warning("clear_tombstone failed for %s", agent_id, exc_info=True)
        return False


# ── slow-command record (stalled but STILL RUNNING) ──────────────────


# Rotate ``slow_commands.jsonl`` once it exceeds this size, keeping ONE
# previous generation (``.jsonl.1``) — the same 1 MiB cap / ~2 MiB total
# shape as ``mcp_gateway.stub._FALLBACK_LOG_MAX_BYTES``. The log lives at
# the subagents-dir root so it survives per-agent folder cleanup, which
# also keeps it outside ``prune_stale_tombstones``'s sweep (that prune
# skips non-directories) — this cap is its only bound.
_SLOW_LOG_MAX_BYTES = 1024 * 1024


def record_slow_command(agent_id: str, **fields: object) -> None:
    """Append a stalled subagent's slow command to ``slow_commands.jsonl``.

    Unlike :func:`write_tombstone`, this does NOT mark the agent dead — a
    stalled subagent is still running; the record is purely for later analysis
    of which commands run slow. At the subagents-dir root so it survives
    per-agent folder cleanup; rotated at :data:`_SLOW_LOG_MAX_BYTES` keeping
    one previous generation. Best-effort: never raises to the caller.

    Bounded via rotate-by-rename (``os.replace``, O(1)) rather than a
    read-and-rewrite trim: this is invoked synchronously from the async
    stall detector (``subagent._maybe_flag_stall``), so whole-file work
    here would stall the gateway event loop.
    """
    entry = {"id": agent_id, "flagged": time.time(), **fields}
    base = _subagents_dir()
    try:
        ensure_directory(base)
        log_path = base / "slow_commands.jsonl"
        # Rotation (shared helper): O(1) rotate-by-rename at the cap, guarded
        # by a non-blocking try-lock so two writers hitting the cap together
        # cannot both rotate, and a loser never waits — no call can stall the
        # gateway event loop. The helper is best-effort by contract: ANY of
        # its failures — the lock file unopenable (fd exhaustion, read-only or
        # ACL-restricted dir), a fresh-boot missing log, a Windows sharing
        # violation rejecting the rename — degrades to appending without
        # rotating. Fd/disk exhaustion is a leading cause of the very stalls
        # this log diagnoses, so a rotation failure must never cost the
        # record; only a failure of the append itself may.
        rotate_jsonl_at(log_path, _SLOW_LOG_MAX_BYTES)
        with open(log_path, "a", encoding="utf-8", opener=owner_only_opener_for(log_path)) as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.warning("record_slow_command failed for %s", agent_id, exc_info=True)


# ── delete ───────────────────────────────────────────────────────────


def forget_live_run_state(agent_id: str) -> None:
    """Release transient run state after its lifecycle writers have settled."""
    _LIVE_RUN_STATES.pop(_live_run_key(agent_id), None)


def delete_agent_folder(agent_id: str) -> None:
    """Remove the entire agent directory and its in-process identity fallback."""
    d = _agent_dir(agent_id)
    forget_live_run_state(agent_id)
    with _CLEANUP_IDENTITY_LOCK:
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            _delete_cleanup_identities_file(agent_id)
            _delete_panel_dismissal(agent_id)
            _LIVE_CLEANUP_IDENTITIES.pop(agent_id, None)
            _LIVE_CLEANUP_HINTS.discard(agent_id)


# ── list orphans ─────────────────────────────────────────────────────


def list_orphans() -> list[dict]:
    """Return parsed state for all non-tombstoned agent folders."""
    results: list[dict] = []
    try:
        dirs = sorted(_subagents_dir().iterdir())
    except (FileNotFoundError, OSError):
        return results
    for d in dirs:
        if not d.is_dir():
            continue
        if (d / "tombstone.json").exists():
            continue
        state = read_state(d.name)
        if state is None:
            logger.debug("list_orphans: skipping corrupt state in %s", d.name)
            continue
        results.append(state)
    return results


def list_leaked_processes() -> list[dict]:
    """Return state for every tombstoned run whose kill left its process standing.

    The tombstone ended the run, so :func:`list_orphans` skips it and the parent
    is not told about it again. Its ``leaked_process`` flag says only that a
    process may still be running, which is what the next start's reconciliation
    reads this for: to end the survivor without sending a notice.

    A flagged folder whose ``state.json`` is missing or unreadable is returned
    as ``{"id": <folder>}``: with no pid on record nothing can be ended, and
    leaving it out would keep the flag -- and so the folder, which the pruner
    skips while flagged -- forever.
    """
    results: list[dict] = []
    try:
        dirs = sorted(_subagents_dir().iterdir())
    except (FileNotFoundError, OSError):
        return results
    for d in dirs:
        if not d.is_dir():
            continue
        tombstone, _present = _read_tombstone_at(d / "tombstone.json")
        if not isinstance(tombstone, dict) or tombstone.get("leaked_process") is not True:
            continue
        state = read_state(d.name)
        results.append(state if state is not None else {"id": d.name})
    return results


def clear_leaked_process(agent_id: str) -> None:
    """Drop the ``leaked_process`` flag from *agent_id*'s tombstone, keeping the rest.

    Rewritten in place rather than through :func:`write_tombstone`, which would
    replace the cause, the detail and the terminal usage the run recorded.
    Best-effort: never raises.
    """
    path = _agent_dir(agent_id) / "tombstone.json"
    tombstone, _present = _read_tombstone_at(path)
    if not isinstance(tombstone, dict) or "leaked_process" not in tombstone:
        return
    tombstone.pop("leaked_process", None)
    try:
        _atomic_write(path, tombstone)
    except OSError:
        logger.warning("clear_leaked_process failed for %s", agent_id, exc_info=True)


def _finite_time(value: object) -> float:
    """A finite, non-negative timestamp, or ``0.0`` for anything unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return 0.0
    return number
