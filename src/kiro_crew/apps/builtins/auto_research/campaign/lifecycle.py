"""Campaign lifecycle: validation, status transitions, deletion and their order.

The single owner of which transitions are legal (terminal statuses, the source
states each user action accepts, the ``allowed_current`` fence a background
observer supplies) and of how one campaign's transitions serialize: the
per-campaign transition lock, the ``started_at`` run-generation fence that keeps
a stale observation from settling a newer run, and the settle-before-cancel
discipline for worker-thread writes. SSE fan-out and the SEL audit trail ride
with the transitions they report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
import weakref
from collections.abc import Callable
from typing import Any

from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME, storage, untrusted
from kiro_crew.apps.builtins.auto_research.campaign.storage import CampaignStatus

try:
    from kiro_crew.sel import sel
except ImportError:
    sel = None  # type: ignore[assignment]

logger = logging.getLogger(LOGGER_NAME)

MAX_CYCLES_HARD_CAP = 100
_MAX_PARALLEL_WORKERS = 5  # hard cap on parallel sub-agents per cycle
# Default seconds between cycles (until the next nudge fires). The watchdog's
# inactivity timeout is idle_secs * 2; the first cycle gets a longer startup
# grace (it can't produce anything until the first nudge + a full work turn).
DEFAULT_IDLE_SECS = 120

# Cap on a stored model id. Longest ids in the wild (fully-qualified Bedrock
# inference profiles) are ~60 chars; anything past this is not a model id.
_MAX_MODEL_LEN = 128

# Terminal statuses cannot transition to any other status.
_TERMINAL_STATUSES = (CampaignStatus.COMPLETE, CampaignStatus.STOPPED)

# The status each user action moves a campaign to (``fork`` creates a child
# campaign instead), and the source statuses it is allowed from: e.g. a start
# on a running campaign would reset started_at and relaunch a duplicate worker.
_ACTION_TARGET_STATUS = {
    "start": CampaignStatus.RUNNING,
    "pause": CampaignStatus.PAUSED,
    "resume": CampaignStatus.RUNNING,
    "stop": CampaignStatus.STOPPED,
}
_ACTION_SOURCE_STATUSES = {
    "start": {CampaignStatus.READY},
    "resume": {
        CampaignStatus.PAUSED,
        CampaignStatus.STAGNANT,
        CampaignStatus.NEEDS_INPUT,
        CampaignStatus.FAILED,
        CampaignStatus.COMPLETE,
        CampaignStatus.STOPPED,
    },
    "pause": {CampaignStatus.RUNNING},
    "stop": {
        CampaignStatus.READY,
        CampaignStatus.RUNNING,
        CampaignStatus.PAUSED,
        CampaignStatus.STAGNANT,
        CampaignStatus.NEEDS_INPUT,
    },
}


def _audit(operation: str, campaign_id: str, **extra: Any) -> None:
    """Emit SEL audit event for campaign lifecycle actions."""
    if sel is None:
        logger.warning(
            "SEL module unavailable — audit event for %s/%s not recorded",
            operation,
            campaign_id,
        )
        return
    try:
        sel().log_api_access(
            caller="auto_research",
            operation=operation,
            outcome="success",
            resources=campaign_id,
            **extra,
        )
    except Exception as exc:
        logger.warning("SEL audit failed for %s/%s: %s", operation, campaign_id, exc)


def _campaign_model(config: dict) -> str:
    """The campaign's explicit model pick from a create/fork config, normalized.

    '' means "no explicit pick" — the worker slot inherits the research agent's
    (and ultimately the backend's) default resolution. A concrete id is stored
    verbatim (trimmed); over-length ids are rejected in ``validate_campaign``
    rather than truncated, so a bad id gets a 400 that names the problem instead
    of being stored as a different string.

    Availability is NOT screened here: no advertised-model list exists outside a
    live session. If the pick stops being served, the session layer's withhold
    (``_pinned_model_verdict`` in chat_runner) KEEPS the pin, runs the worker on
    the backend default, and posts a notice card — but that card lands in the
    app-owned ``research-<cid>`` transcript, which the Research Lab page does not
    render, so the fallback is not visible on this app's own surfaces.
    """
    raw = config.get("model")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def validate_campaign(config: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if len(config.get("question", "")) < 20:
        errors.append("Question too vague — provide more context (min 20 characters)")
    if len(config.get("sub_questions", [])) < 2:
        warnings.append("Consider decomposing into sub-questions for better coverage")
    # RL v2: validate execution_mode against supported modes.
    if (
        config.get("execution_mode", storage.DEFAULT_EXECUTION_MODE)
        not in storage.VALID_EXECUTION_MODES
    ):
        errors.append("Execution mode must be 'agent' or 'workflow'")

    raw_model = config.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        errors.append("Model must be a string")
    elif isinstance(raw_model, str) and len(raw_model.strip()) > _MAX_MODEL_LEN:
        # Reject rather than truncate: a sliced id is a *different* string that
        # is never served, which would take the silent-fallback path instead of
        # a 400 that names the problem.
        errors.append(f"Model id too long (max {_MAX_MODEL_LEN} characters)")
    elif (
        _campaign_model(config)
        and config.get("execution_mode", storage.DEFAULT_EXECUTION_MODE) == "workflow"
    ):
        # The workflow engine resolves its own models per step; a campaign-level
        # pin would be silently ignored, which
        # docs/system-specs/common/model-selection.md forbids.
        errors.append(
            "Model selection requires agent mode — workflow mode runs on the default model"
        )

    max_cycles = config.get("max_cycles", 30)
    if max_cycles > MAX_CYCLES_HARD_CAP:
        errors.append(f"Max cycles cannot exceed {MAX_CYCLES_HARD_CAP}")
    elif max_cycles > 50:
        low, high = max_cycles * 0.10, max_cycles * 0.30
        warnings.append(
            f"High cycle count ({max_cycles}). " f"Estimated cost: ~${low:.2f}–${high:.2f}"
        )

    db = storage._get_db()
    active = db.execute(
        "SELECT id, name FROM campaigns WHERE status IN (?, ?, ?, ?)",
        (
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.STAGNANT,
            CampaignStatus.NEEDS_INPUT,
        ),
    ).fetchone()
    db.close()
    if active:
        clean_name = untrusted._redact_finding({"v": active["name"]})["v"]
        errors.append(f"Campaign '{clean_name}' is already active. Stop it first.")

    n = len(config.get("sub_questions", []))
    suggested_max_cycles = n + (n + 2) // 3 + 1 if n > 0 else 0
    return {
        "can_start": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "estimated_cycles": max_cycles,
        "estimated_duration_min": max_cycles * 2,
        "suggested_max_cycles": suggested_max_cycles,
    }


_FORK_NAME_PREFIX = "Forked: "


def _fork_name(source: str) -> str:
    """Build a forked campaign's display name with a clear 'Forked:' prefix.

    Mirrors create_campaign's 50-char name cap and avoids double-prefixing
    when the source already starts with the prefix (e.g. forking a fork).
    """
    base = (source or "").strip()
    if base.startswith(_FORK_NAME_PREFIX):
        base = base[len(_FORK_NAME_PREFIX) :].strip()
    return (_FORK_NAME_PREFIX + base[: 50 - len(_FORK_NAME_PREFIX)]).strip()


def create_campaign(config: dict) -> dict:
    campaign_id = uuid.uuid4().hex[:8]
    name = config.get("name") or config["question"][:50].strip()
    parent_id = config.get("parent_id") or None
    # RL v2: validate/clamp execution mode + recursive-exploration budget.
    exec_mode = config.get("execution_mode", storage.DEFAULT_EXECUTION_MODE)
    if exec_mode not in storage.VALID_EXECUTION_MODES:
        exec_mode = storage.DEFAULT_EXECUTION_MODE
    max_subq = max(
        0, int(config.get("max_subquestions_per_round", storage.DEFAULT_MAX_SUBQUESTIONS_PER_ROUND))
    )
    depth_decay = float(config.get("depth_decay", storage.DEFAULT_DEPTH_DECAY))
    if not 0.0 <= depth_decay <= 1.0:
        depth_decay = storage.DEFAULT_DEPTH_DECAY
    reserve_fraction = float(config.get("reserve_fraction", storage.DEFAULT_RESERVE_FRACTION))
    if not 0.0 <= reserve_fraction < 1.0:
        reserve_fraction = storage.DEFAULT_RESERVE_FRACTION
    db = storage._get_db()
    db.execute("BEGIN")
    db.execute(
        "INSERT INTO campaigns (id,name,question,sub_questions,sources,scope_constraints,"
        "max_cycles,idle_secs,success_criteria,auto_approve,parent_id,parallel_workers,"
        "execution_mode,max_subquestions_per_round,depth_decay,reserve_fraction,"
        "model,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            campaign_id,
            name,
            config["question"],
            json.dumps(config.get("sub_questions", [])),
            json.dumps(config.get("sources", [])),
            json.dumps(config.get("scope_constraints", [])),
            config.get("max_cycles", 30),
            config.get("idle_secs", DEFAULT_IDLE_SECS),
            config.get("success_criteria") or None,
            int(bool(config.get("auto_approve", False))),
            parent_id,
            min(int(config.get("parallel_workers", 1)), _MAX_PARALLEL_WORKERS),
            exec_mode,
            max_subq,
            depth_decay,
            reserve_fraction,
            _campaign_model(config),
            CampaignStatus.READY,
            time.time(),
        ),
    )
    db.commit()
    db.close()
    # Persist the grill tree if provided (full tree with clarifier answers,
    # pruned branches, origin tags — enables revisiting + challenge mode).
    grill_tree = config.get("grill_tree")
    if grill_tree and isinstance(grill_tree, list):
        d = storage._campaign_dir(campaign_id)
        d.mkdir(parents=True, exist_ok=True)
        d.joinpath("grill_tree.json").write_text(json.dumps(grill_tree, indent=2), encoding="utf-8")
    storage.write_status(campaign_id, CampaignStatus.READY)
    _audit("campaign_created", campaign_id)
    return {"id": campaign_id, "name": name, "status": CampaignStatus.READY}


def update_campaign_status(campaign_id: str, new_status: str, **kwargs: Any) -> dict:
    if not storage._validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    db = storage._get_db()
    row = db.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        db.close()
        return {"error": "campaign not found"}
    current = row["status"]
    if current in _TERMINAL_STATUSES and new_status not in (current, CampaignStatus.RUNNING):
        db.close()
        return {"error": f"invalid transition: {current} -> {new_status}"}
    sets: list[str] = ["status = ?"]
    vals: list[Any] = [new_status]
    if new_status == CampaignStatus.RUNNING:
        sets.append("started_at = ?")
        vals.append(time.time())
        # Clear the prior run's completed_at so resumed COMPLETE/STOPPED campaigns
        # don't end up with completed_at < started_at (breaks duration math/UI).
        sets.append("completed_at = ?")
        vals.append(None)
        kwargs.setdefault("error_message", None)  # clear stale failure on (re)start
    if new_status in (CampaignStatus.COMPLETE, CampaignStatus.STOPPED, CampaignStatus.FAILED):
        sets.append("completed_at = ?")
        vals.append(time.time())
    if "error_message" in kwargs:
        sets.append("error_message = ?")
        vals.append(kwargs["error_message"])
    vals.append(campaign_id)
    db.execute("BEGIN")
    db.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    db.close()
    storage.write_status(campaign_id, new_status, **kwargs)
    _audit(f"campaign_{new_status}", campaign_id)
    return {"id": campaign_id, "status": new_status}


def delete_campaign(campaign_id: str) -> dict:
    """Delete a campaign's research dir (findings + report), then its DB row.

    Directory cleanup runs BEFORE the DB delete, and the row is kept when
    cleanup fails, so a caller who retries the same id gets a real retry of
    the cleanup instead of ``{"error": "campaign not found"}`` against an
    already-vanished row. Windows refuses to unlink a file another process
    still holds open (POSIX allows it), so a live worker session's handle on
    a findings file can make this tree removal fail HALFWAY; the previous
    ``ignore_errors=True`` swallowed that and deleted the row anyway, leaving
    orphaned findings with no id left to retry them under.
    """
    if not storage._validate_campaign_id(campaign_id):
        return {"error": "invalid campaign_id"}
    d = storage._safe_campaign_dir(campaign_id)
    if d and d.exists():
        failures: list[str] = []

        def _on_error(_func: Any, path: Any, _exc: BaseException) -> None:
            failures.append(str(path))

        shutil.rmtree(d, onexc=_on_error)
        if failures:
            logger.warning(
                "auto_research: campaign %s directory cleanup left %d path(s) "
                "behind (a process may still hold them open); the database row "
                "is kept so retrying this delete will try cleanup again",
                campaign_id,
                len(failures),
            )
            return {"error": "cleanup incomplete", "residual": True}
    db = storage._get_db()
    db.execute("BEGIN")
    rows = db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,)).rowcount
    db.commit()
    db.close()
    if rows == 0:
        return {"error": "campaign not found"}
    return {"id": campaign_id, "deleted": True, "residual": False}


_SSE_QUEUE_MAXSIZE = 256
_sse_queues: list[asyncio.Queue] = []
_campaign_transition_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _campaign_transition_lock(campaign_id: str) -> asyncio.Lock:
    """Serialize one campaign's user and watchdog status transitions per loop."""
    event_loop = asyncio.get_running_loop()
    locks = _campaign_transition_locks.setdefault(event_loop, weakref.WeakValueDictionary())
    lock = locks.get(campaign_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[campaign_id] = lock
    return lock


async def _settle_before_cancellation(
    task: "asyncio.Task[Any]",
    *,
    on_settled: "Callable[[asyncio.Task[Any]], None] | None" = None,
) -> Any:
    """Await *task* and guarantee it SETTLES before cancellation propagates
    out of this coroutine.

    ``asyncio.to_thread`` keeps running after its awaiting task is cancelled,
    so a caller holding the campaign transition lock would release it while
    the filesystem mutation is still in flight — letting a lock-serialized
    DELETE interleave and have its directory resurrected by the worker's
    ``mkdir``. Mirrors the shield-and-settle discipline of the terminal
    settlement in the watchdog path.

    When *on_settled* is provided, it is invoked with the now-settled *task*
    on the cancellation path only — after the settle loop and before the
    original cancellation is re-raised — so a caller can retrieve and report
    the worker outcome without letting it replace the shutdown cancellation.
    When it is None the helper just re-raises the cancellation as before.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated shutdown cancellation must not cancel the worker
                # wait; the thread finishes regardless, so keep settling.
                continue
            except Exception:
                # The worker failed; the cancellation below still wins.
                break
        if on_settled is not None:
            on_settled(task)
        raise cancelled


def _guarded_txn(
    cid: str,
    new_status: str,
    allowed_current: tuple[str, ...],
    expected_started_at: float | None,
    **kwargs: Any,
) -> dict | None:
    """The fence check + write of :func:`_guarded_transition`, WITHOUT the lock.

    Runs off-loop. Callers must already hold the campaign's transition lock
    (directly, or via :func:`_guarded_transition`).
    """
    db = storage._get_db()
    try:
        row = db.execute("SELECT status, started_at FROM campaigns WHERE id = ?", (cid,)).fetchone()
        if row is None or row["status"] not in allowed_current:
            return None
        if expected_started_at is not None and row["started_at"] != expected_started_at:
            return None  # stale generation: a replacement run took over
    finally:
        db.close()
    return update_campaign_status(cid, new_status, **kwargs)


def _sse_from_thread(loop: asyncio.AbstractEventLoop, event: dict) -> None:
    """Deliver an SSE event from a worker thread (``_emit_sse`` is loop-affine)."""
    loop.call_soon_threadsafe(_emit_sse, event)


async def _guarded_transition(
    cid: str,
    new_status: str,
    *,
    allowed_current: tuple[str, ...],
    expected_started_at: float | None = None,
    on_commit: Any = None,
    **kwargs: Any,
) -> dict | None:
    """Serialize a background status transition against user actions.

    A background observer (watchdog / nudge / workflow poller) decides on a
    transition from state it read BEFORE a thread hop, so a user Stop/Pause
    that commits during the hop must win. This takes the same per-campaign
    lock ``_handle_action`` holds, re-reads the current status, and writes
    only while it is still one of ``allowed_current`` — refusing stale
    observations instead of resurrecting or overwriting the newer state.

    ``expected_started_at`` is the generation fence: ``started_at`` is minted
    on every RUNNING transition, so a Pause→Resume that recreates RUNNING
    yields a NEW generation and a status-only check would let the OLD run's
    verdict (COMPLETE/STAGNANT/NEEDS_INPUT) terminate the replacement run
    (ABA). Callers that observed a RUNNING row pass the ``started_at`` they
    read; the write then also requires the persisted generation to match
    (same equality contract as :func:`_campaign_run_has_status`).

    Returns the update result, or ``None`` when the transition was refused.
    The caller must NOT already hold the campaign's transition lock
    (``asyncio.Lock`` is not reentrant) — a frame that holds it offloads
    :func:`_guarded_txn` directly.

    ``on_commit`` (optional) runs IN THE WORKER THREAD immediately after the
    transition persists, before this coroutine resumes. Side effects that must
    accompany a persisted transition (SSE via :func:`_sse_from_thread`, audit,
    marker files) belong here: the awaiting frame can be CANCELLED at the
    ``to_thread`` suspension point AFTER the commit already landed, and a
    success-branch after ``await`` is silently skipped in that window (the
    watchdog's shutdown cancel made a persisted COMPLETE lose its SSE).
    """
    async with _campaign_transition_lock(cid):

        def _txn_and_notify() -> dict | None:
            result = _guarded_txn(cid, new_status, allowed_current, expected_started_at, **kwargs)
            if result and on_commit is not None:
                on_commit(result)
            return result

        return await asyncio.to_thread(_txn_and_notify)


def _emit_sse(event: dict) -> None:
    for q in _sse_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Drop events for slow consumers


def _campaign_run_has_status(
    campaign_id: str,
    observed_started_at: float | None,
    expected_status: str,
) -> bool:
    """Return whether one run generation has the expected persisted status."""
    if observed_started_at is None:
        return False
    db = storage._get_db()
    try:
        row = db.execute(
            "SELECT status, started_at FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    finally:
        db.close()
    return bool(
        row is not None
        and row["status"] == expected_status
        and row["started_at"] == observed_started_at
    )


def _campaign_run_is_current(campaign_id: str, observed_started_at: float | None) -> bool:
    """Return whether the watchdog observation still names the active run."""
    return _campaign_run_has_status(
        campaign_id,
        observed_started_at,
        CampaignStatus.RUNNING,
    )
