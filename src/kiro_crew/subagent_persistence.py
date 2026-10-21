"""Subagent persistence — disk I/O for agent folders.

Each subagent gets a folder at ``~/.kiro/crew/subagents/{id}/`` containing:
- ``state.json``   — running state (task, PID, turns, last_tool)
- ``result.txt``   — streamed result text
- ``tombstone.json`` — written on abnormal exit only
"""

from __future__ import annotations

import asyncio
import heapq
import importlib as _importlib
import json
import logging
import os
import re
import shutil
import sys as _sys
import tempfile
import threading
import time
import weakref
from collections.abc import Collection
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple, Protocol

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.execution_context import ExecutionContext
from kiro_crew.jsonl_util import rotate_jsonl_at
from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for
from kiro_crew.providers.cleanup import _is_safe_path
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.subagent_persistence_dismissals import (
    _PANEL_APP_CAP,
    _PANEL_ID_CAP,
    _PANEL_PARENT_SESSION_CAP,
    DISMISSAL_FAILED,
    DISMISSAL_NO_FOLDER,
    DISMISSAL_RECORDED,
    _delete_panel_dismissal,
    dismissed_panel_ids,
    panel_dismissal_recorded,
    prune_orphan_panel_dismissals,
    record_panel_dismissal,
    record_panel_dismissal_outcome,
)
from kiro_crew.subagent_persistence_layout import (
    _CLEANUP_IDENTITY_LOCK,
    _LIVE_CLEANUP_HINTS,
    _LIVE_CLEANUP_IDENTITIES,
    SUBAGENT_CONVERSATION_PREFIX,
    _agent_dir,
    _delete_cleanup_identities_file,
    _live_cleanup_identities,
    _merge_cleanup_identity_records,
    _read_cleanup_identities_file,
    _subagents_dir,
    agent_dir_for_display,
    has_live_cleanup_identity,
    logger,
    publish_live_cleanup_hint,
    publish_live_cleanup_identity,
    remember_live_cleanup_identity,
    subagent_id_from_conversation_key,
    trusted_cleanup_identity_record,
)
from kiro_crew.subagent_persistence_state import (
    RetentionPromotionResult,
    _acquire_retention_locks,
    _read_tombstone_at,
    _release_retention_locks,
    bind_session_memory_mode,
    create_agent_folder,
    promote_retention,
    read_run_agent_selection,
    read_run_app,
    read_run_execution,
    read_run_memory_mode,
    read_run_memory_store,
    read_session_memory_mode,
    read_state,
    read_tombstone,
    tighten_run_memory_mode,
    update_execution_context,
    update_state,
    write_result_chunk,
    write_run_agent,
)
from kiro_crew.subagent_persistence_tombstones import (
    _finite_time,
    clear_leaked_process,
    clear_tombstone,
    delete_agent_folder,
    forget_live_run_state,
    has_owed_tombstones,
    list_leaked_processes,
    list_orphans,
    mark_delivered,
    record_slow_command,
    result_is_whole,
    retry_owed_tombstones,
    settle_delivered_batch,
    write_finished_result,
    write_tombstone,
)

#: The one tombstone cause that marks a run whose result reached its parent.
_DELIVERED_CAUSE = "delivered"

#: Named caps on every string a panel record RETAINS. ``keep`` bounds the ROW
#: count only, and a bounded number of unbounded rows is unbounded, so each
#: retained field carries its own limit and a cut field says that it was cut.
_PANEL_TASK_CAP = 2000
_PANEL_AGENT_CAP = 200
_PANEL_ERROR_CAP = 500
_PANEL_RESULT_CAP = 3000
_PANEL_TRUNC_MARKER = " ...(truncated)"

#: Extra bytes read past ``_PANEL_RESULT_CAP`` before redaction runs, so a
#: credential straddling the cut is present whole when it is matched. Bounded on
#: purpose: the alternative is reading the file whole, which the cap exists to
#: refuse. Wide enough for any single token the redactors recognise.
_PANEL_REDACT_MARGIN = 1024


def _panel_keys_plausible(agent_id: str, app: str, parent_session: str) -> bool:
    """Whether every equality key on a record is a plausible size.

    One predicate over all three, so a key added to the record is added here too
    rather than acquiring its own scattered guard.
    """
    return all(
        len(value) <= cap
        for value, cap in (
            (agent_id, _PANEL_ID_CAP),
            (app, _PANEL_APP_CAP),
            (parent_session, _PANEL_PARENT_SESSION_CAP),
        )
    )


#: Ceiling on the scan's own working set, as a multiple of ``keep``. The walk
#: retains at most this many folder candidates at a time, so a registry holding
#: hundreds of thousands of folders costs what one holding a hundred costs.
#: Above ``keep`` so a run of skipped folders still leaves depth to fill a page.
_PANEL_CANDIDATE_MULTIPLE = 4

#: Terminal values a tombstone may record, and which of them is a user stop.
_PANEL_OUTCOMES = ("completed", "failed", "stopped")


def _clamp(text: str, cap: int) -> str:
    """Return *text* within *cap*, ending in the marker when it had to be cut.

    The marker IS the truncation report: it travels inside the value to every
    reader, so no separate flag has to be carried and kept in step.
    """
    if len(text) <= cap:
        return text
    return text[:cap] + _PANEL_TRUNC_MARKER


def _redact_then_clamp(text: str, cap: int) -> str:
    """Redact the WHOLE value, then clamp it.

    Order matters and this is the order the native card path already requires: a
    credential cut at the cap first loses the tail its pattern needs, so the
    consumers' own redaction cannot match the fragment that survives. Redacting
    before the cut leaves nothing for the boundary to split.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return _clamp(text, cap)


def _record_memory_mode(state: dict) -> str:
    """The run's memory mode, from the top-level field or the execution record.

    Answers ``""`` when neither spells one, which every caller treats as "not
    persistent". Failing closed here costs at most one card; failing open would
    put an incognito run's task text on screen.
    """
    mode = state.get("memory_mode")
    if isinstance(mode, str) and mode:
        return mode
    execution = state.get("execution_context")
    if isinstance(execution, dict):
        nested = execution.get("memory_mode")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _record_app(state: dict) -> str:
    """The app that executed the run, from the top level or the execution record.

    Answers ``""`` for a run no app owns, which is a real value and not a
    fallback: a slot a person created has no app owner either, so the two compare
    equal and a human run replays to a human slot.
    """
    app = state.get("app")
    if isinstance(app, str) and app:
        return app
    execution = state.get("execution_context")
    if isinstance(execution, dict):
        nested = execution.get("app")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def classify_persisted_ending(agent_dir: Path) -> tuple[str, str, bool]:
    """How a run on disk ended, as ``(outcome, error, stopped)``.

    The ONE classification site for a persisted run, shared by the panel list
    and the single-card read, so one folder cannot read "completed" in a list
    and "Orphaned: delivered" when it is opened.

    Takes the run's resolved folder rather than its id, so a caller that already
    resolved it -- every caller does, to reach ``result.txt`` -- resolves it once
    for the whole response instead of once per field.

    The tombstone's own ``outcome`` is preferred over its ``cause``, because the
    writer records the outcome directly: a user stop is ``stopped`` there, while
    deriving from ``cause`` alone reports that routine stop as a failure.
    ``detail`` carries the specific reason where ``cause`` is a coarse bucket.

    An absent tombstone means NOTHING recorded an ending, which is not an outcome
    and must not be reported as one: calling it ``completed`` would show a green
    terminal card for a run the restart killed, contradicting the orphan notice
    injected for that same run. It answers ``""`` instead, and a caller that
    needs an outcome skips the folder until an ending is on disk. The orphan
    reconciler writes one, but it yields between folders, so a tab reconnecting
    mid-scan observes this state rather than racing past it. A tombstone that is
    PRESENT but unreadable is the opposite case: an ending was recorded and
    cannot be read, which is reported as an unknown cause.
    """
    tombstone, present = _read_tombstone_at(agent_dir / "tombstone.json")
    if tombstone is None:
        if present:
            return "failed", "Orphaned (unknown cause)", False
        return "", "", False
    recorded = tombstone.get("outcome")
    cause = str(tombstone.get("cause") or "")
    if recorded in _PANEL_OUTCOMES:
        outcome = str(recorded)
    elif cause == _DELIVERED_CAUSE:
        outcome = "completed"
    else:
        outcome = "failed"
    if outcome == "stopped":
        # A stop is not an error, and the consumers drop error text for it.
        return outcome, "", True
    if outcome == "completed":
        return outcome, "", False
    detail = str(tombstone.get("detail") or "")
    return outcome, detail or f"Orphaned: {cause or 'unknown'}", False


def _panel_result_text(agent_dir: Path) -> str:
    """Capped result text for a panel record, or ``""`` when unreadable."""
    path = agent_dir / "result.txt"
    if is_sensitive_path(str(path)):
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            # Read a bounded MARGIN past the cap so a credential straddling the
            # cut is present whole when redaction runs, and one byte past that so
            # a value exactly at the cap is not marked as cut. The margin keeps
            # the read bounded; nothing here reads the file whole.
            text = handle.read(_PANEL_RESULT_CAP + _PANEL_REDACT_MARGIN + 1)
    except OSError:
        return ""
    return _redact_then_clamp(text, _PANEL_RESULT_CAP)


def _result_written_at(agent_dir: Path) -> float:
    """When the run's own output last reached disk, or ``0.0`` when it never did.

    The run writes ``result.txt`` itself as its output streams, and its ending
    may rewrite it once more just before recording ``result_complete`` (whole
    from memory, or capped in place when it outgrew the cap), so its last write
    is when the run stopped producing, give or take the ending's
    post-processing, which this may include. Delivery and retention only read it
    or remove it, which is what makes the timestamp the run's and not a later
    caller's. A run that produced no output has no file, and the caller falls
    back to the tombstone.
    """
    try:
        return _finite_time((agent_dir / "result.txt").stat().st_mtime)
    except OSError:
        return 0.0


def _panel_record(agent_id: str, *, cutoff: float, include_result: bool) -> dict | None:
    """One panel record, or ``None`` when the folder supplies no usable one."""
    state = read_state(agent_id)
    if state is None:
        return None
    if _record_memory_mode(state) != "persistent":
        return None
    parent_session = str(state.get("parent_session") or "")
    if not parent_session:
        # Without an owning conversation there is no slot to route a frame to,
        # and an empty slot is read by older clients as the active one.
        return None
    try:
        agent_dir = _agent_dir(agent_id)
    except ValueError:
        return None
    app = _record_app(state)
    if not _panel_keys_plausible(agent_id, app, parent_session):
        # An equality key this size is refused outright: clamping one would make
        # it compare unequal while still reading as a value.
        return None
    started = _finite_time(state.get("started"))
    tombstone, _present = _read_tombstone_at(agent_dir / "tombstone.json")
    ended = _finite_time((tombstone or {}).get("died")) or _finite_time(state.get("updated_at"))
    # The end of the RUN, not of the bookkeeping about it. A tombstone's ``died``
    # is carried across writes, so where a write happened at the ending it is the
    # ending. The queued and digest-held paths have no write at completion at all
    # -- the delivery acknowledgement is their FIRST tombstone -- and it lands
    # whenever the parent got round to the result, so on those the stamp is a wait
    # this panel would otherwise show as runtime. The run's own output file is the
    # completion evidence that exists on every path that produced output, and the
    # earlier of the two is the one the run itself wrote. Only the reported
    # duration uses it: the retention window below stays on the latest timestamp,
    # because a run whose result reached its parent moments ago belongs in the
    # panel whenever it happens to have finished.
    written = _result_written_at(agent_dir)
    ran_until = min(ended, written) if (ended and written) else (ended or written)
    if max(started, ended) < cutoff:
        return None
    outcome, error, stopped = classify_persisted_ending(agent_dir)
    if not outcome:
        # No ending is recorded yet, so this run has no outcome to show. A card
        # now could only carry an invented one; the folder is picked up on a
        # later read, once the reconciler has written its ending.
        return None
    record: dict = {
        "id": agent_id,
        "task": _redact_then_clamp(str(state.get("task") or ""), _PANEL_TASK_CAP),
        "agent": _redact_then_clamp(str(state.get("agent") or ""), _PANEL_AGENT_CAP),
        "app": app,
        "parent_session": parent_session,
        "started": started,
        "elapsed": ran_until - started if ran_until > started else 0.0,
        "outcome": outcome,
        "error": _redact_then_clamp(error, _PANEL_ERROR_CAP),
        "stopped": stopped,
    }
    if include_result:
        record["result"] = _panel_result_text(agent_dir)
    return record


class PanelRecords(NamedTuple):
    """One panel rebuild's records, plus what the count bound left out.

    The overflow is carried out with the records rather than dropped, because a
    truncated tail is otherwise indistinguishable from a population that never
    held those runs -- a panel showing 50 of 51 looks exactly like a panel showing
    all 50 there were. Every consumer surfaces it once per rebuild.

    ``overflow_is_lower_bound`` says the scan's own candidate window filled up, so
    more admissible folders may exist past it and the count is a floor.
    """

    records: list[dict]
    overflow: int
    overflow_is_lower_bound: bool


def read_panel_records(
    *,
    keep: int,
    max_age_secs: float,
    exclude_ids: Collection[str] = (),
    include_result: bool = False,
    admit: "Callable[[dict], bool] | None" = None,
) -> PanelRecords:
    """Bounded, newest-first run records for rebuilding a subagent panel.

    A panel's live source is gateway memory, so a replacement gateway process
    has nothing to show for the runs it never tracked. These records are the
    durable half of that answer: one entry per run folder, carrying the fields a
    terminal panel frame needs.

    ``exclude_ids`` carries the ids the caller's live manager already holds.
    Those folders are skipped, never merged: a live entry is the authority on its
    own run, and a record here describes only a run nobody is tracking. Runs the
    user DISMISSED are skipped on the same terms, from the durable record the
    delete route leaves -- without it, dropping a run from the manager hid its
    card only until this reader found the folder again.

    ``admit`` is the caller's OWN visibility filter, and it runs before the cap
    rather than after it. Filtering afterwards lets a record the caller may not
    see occupy a slot its own runs need, and makes the overflow count describe a
    population that is not the caller's -- which discloses how many foreign runs
    exist. Applied here, both the cap and the count are the caller's own.

    ``keep`` and ``max_age_secs`` bound different things -- the size of the burst
    a caller must deliver at once, and how far back a rebuild reaches. Ages are
    measured against the times the run itself recorded. Because a row count is
    not a memory bound, every retained string is clamped to its own named cap and
    the three equality keys refuse an oversized record outright. What ``keep``
    cut is COUNTED and returned, never silently dropped.

    A run whose memory mode is not ``persistent`` keeps its state in memory and
    writes no folder, so it cannot be discovered here; a folder whose record
    spells a non-persistent mode is skipped explicitly too, so the exclusion
    holds whichever way the folder came to exist.

    Blocking. A caller on the event loop must wrap this in
    :func:`asyncio.to_thread`.
    """
    if keep <= 0:
        return PanelRecords([], 0, False)
    cutoff = time.time() - max_age_secs if max_age_secs > 0 else float("-inf")
    # The caller's own exclusions. Dismissals are NOT unioned in here: that read
    # retains one entry per dismissal record, so the registry's size would set this
    # function's memory -- inside the one function whose contract is a bounded
    # working set. They are probed per scanned candidate instead, below, which is
    # still BEFORE the heap: a dismissed folder must not spend a candidate slot a
    # visible run needs, which a filter after the walk would let it do.
    skip = set(exclude_ids)
    # At least one candidate BEYOND ``keep`` has to fit, or the window could not
    # see the very overflow it has to report: a heap sized exactly ``keep``
    # admits every entry as a record and truncates in silence.
    candidate_cap = max(keep + 1, keep * _PANEL_CANDIDATE_MULTIPLE)
    # A bounded min-heap on mtime, so the walk's own working set is capped
    # whatever the registry holds. Folders outside the age window and ids the
    # caller already knows are dropped HERE, before they can occupy the heap or
    # cost a state read.
    newest: list[tuple[float, str]] = []
    saturated = False
    try:
        scan = os.scandir(_subagents_dir())
    except OSError:
        return PanelRecords([], 0, False)
    with scan:
        for entry in scan:
            try:
                if not entry.is_dir():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff or entry.name in skip:
                continue
            # Ordered after the cheap tests on purpose: this one costs a stat, and
            # the two above reject most entries without it.
            if panel_dismissal_recorded(entry.name):
                continue
            if len(newest) < candidate_cap:
                heapq.heappush(newest, (mtime, entry.name))
            else:
                saturated = True
                if mtime > newest[0][0]:
                    heapq.heapreplace(newest, (mtime, entry.name))
    records: list[dict] = []
    overflow = 0
    for _mtime, agent_id in sorted(newest, reverse=True):
        at_cap = len(records) >= keep
        # Past the cap the record itself is not wanted, only the answer to
        # "would this one have been shown", so the result read is skipped.
        record = _panel_record(
            agent_id, cutoff=cutoff, include_result=include_result and not at_cap
        )
        if record is None:
            continue
        if admit is not None and not admit(record):
            # The caller's OWN filter runs before the cap, not after it. A record
            # this caller may not see must not occupy a slot its own runs need,
            # and must not be counted as something the cap withheld from it --
            # that count would disclose how many foreign runs exist.
            continue
        if at_cap:
            overflow += 1
            continue
        records.append(record)
    # ``saturated`` stands on its own, NOT ANDed with the count. The candidate
    # window is a second bound and it closes on mtime BEFORE validity and
    # ``admit`` run, so a window filled entirely by records that are then
    # rejected yields a zero count while admissible older folders were never
    # inspected. ANDing the two would report that case as "nothing was cut",
    # which is the silence this bound is required not to keep.
    return PanelRecords(records, overflow, saturated)


# ── prune ────────────────────────────────────────────────────────────

# Preserve unknown retention state long enough to outlive the six-hour
# continuable-conversation window, then reclaim it using tombstone metadata.
_UNREADABLE_STATE_GRACE_SECS = 24 * 3600
_UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS = 90 * 86400


def _tombstone_died(ts: dict[str, object], path: Path, now: float) -> int | float:
    """Return a finite, positive, non-future death time with bounded fallback."""
    died = ts.get("died")
    if isinstance(died, (int, float)) and not isinstance(died, bool) and 0 < died <= now:
        return died
    try:
        fallback = path.stat().st_mtime
    except OSError:
        return now
    if 0 < fallback <= now:
        return fallback
    return now


def _should_defer_tombstone_cleanup(
    *,
    retention_state: dict[str, object],
    retention_unknown: bool,
    cleanup_session_id: object,
    died: object,
    cutoff: float,
    now: float,
) -> bool:
    """Return whether prune must preserve provider files and identity folder."""
    if retention_unknown:
        if not cleanup_session_id or not (
            isinstance(died, (int, float)) and not isinstance(died, bool) and 0 < died <= now
        ):
            return False
        return died >= cutoff - _UNREADABLE_STATE_GRACE_SECS
    return retention_state.get("keep") is True


def _cleanup_identity_fallback_record(
    agent_id: str, session_id: object
) -> dict[str, object] | None:
    """Return the matching generation, or latest when top-level SID is absent."""
    records = _merge_cleanup_identity_records(
        _read_cleanup_identities_file(agent_id),
        _live_cleanup_identities(agent_id),
    )
    if not records:
        return None
    if isinstance(session_id, str) and session_id:
        for record in reversed(records):
            if record.get("session_id") == session_id:
                return record
        # Agent-writable state cannot suppress protected retention authority by
        # naming an unrelated SID. Fall back to the latest trusted generation;
        # this record can preserve ownership but never expands deletion authority.
    return records[-1]


def _cleanup_retention_fallback(agent_id: str, session_id: object) -> tuple[bool | None, str, str]:
    """Return trusted fallback retention, owner, and SID."""
    record = _cleanup_identity_fallback_record(agent_id, session_id)
    if record is None:
        return None, "", ""
    keep = record.get("keep")
    conversation_key = record.get("conversation_key")
    record_sid = record.get("session_id")
    return (
        keep if isinstance(keep, bool) else None,
        conversation_key if isinstance(conversation_key, str) else "",
        record_sid if isinstance(record_sid, str) else "",
    )


def _tombstone_cleanup_identities(agent_id: str) -> list[tuple[str, str, str]]:
    """Return gateway-authorized cleanup generations only.

    Tombstone and state identity fields live in the agent-writable run folder.
    They remain compatibility/display hints, but cannot expand the set of provider
    transcripts prune may delete. Durable protected records and synchronous live
    gateway publication are the only deletion authorities.
    """
    records = _merge_cleanup_identity_records(
        _read_cleanup_identities_file(agent_id),
        _live_cleanup_identities(agent_id),
    )
    identities: list[tuple[str, str, str]] = []
    for identity_record in records:
        sid = identity_record.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        record_provider = identity_record.get("provider")
        record_cwd = identity_record.get("cwd")
        identities.append(
            (
                sid,
                (
                    record_provider
                    if isinstance(record_provider, str) and record_provider
                    else PROVIDER_LABEL_DEFAULT
                ),
                record_cwd if isinstance(record_cwd, str) else "",
            )
        )
    return identities


def prune_stale_tombstones(max_age_days: int = 7, delivered_ttl_secs: int = 3600) -> int:
    """Delete tombstoned folders past their retention window. Returns count pruned.

    Two windows: abnormal-exit tombstones (timeout / error / orphan) are kept for
    *max_age_days* for post-mortem diagnostics; ``cause="delivered"`` tombstones
    (successful deliveries retained so the parent can read the full transcript)
    are pruned after the shorter *delivered_ttl_secs* to bound disk growth.
    """
    now = time.time()
    default_cutoff = now - (max_age_days * 86400)
    delivered_cutoff = now - max(0, delivered_ttl_secs)
    pruned = 0
    # Once per cycle, and before the walk: a record whose folder left by some
    # other route has nothing left to suppress, and this sweep is what reclaims
    # it. Not added to ``pruned``, which counts folders.
    prune_orphan_panel_dismissals()
    try:
        dirs = sorted(_subagents_dir().iterdir())
    except (FileNotFoundError, OSError):
        return 0
    for d in dirs:
        if not d.is_dir():
            continue
        ts_path = d / "tombstone.json"
        if not ts_path.exists():
            continue
        try:
            ts = read_tombstone(d.name)
            if ts is None:
                logger.debug("prune: skipping corrupt tombstone in %s", d.name)
                continue
            if ts.get("leaked_process") is True:
                # The folder is the only record of a process still owed its end
                # (``list_leaked_processes``); it is pruned once that flag clears.
                continue
            cutoff = delivered_cutoff if ts.get("cause") == "delivered" else default_cutoff
            died = _tombstone_died(ts, ts_path, now)
            if died <= cutoff:
                tombstone_session_id = ts.get("session_id", "")
                state_hint = read_state(d.name)
                claim_hint = d.name
                hinted_key = ""
                if isinstance(state_hint, dict):
                    hinted_session_id = state_hint.get("session_id", "")
                    _, fallback_key, fallback_sid = _cleanup_retention_fallback(
                        d.name,
                        hinted_session_id,
                    )
                    # Protected acquisition-time ownership outranks all
                    # agent-writable state, including an empty or conflicting
                    # conversation key. Legacy runs without a generation retain
                    # the compatibility hint from state.
                    hinted_key = (
                        fallback_key
                        if fallback_sid
                        else str(state_hint.get("conversation_key") or "")
                    )
                else:
                    hinted_record = _cleanup_identity_fallback_record(d.name, "")
                    if hinted_record is not None:
                        hinted_key = str(hinted_record.get("conversation_key") or "")
                hinted_owner = subagent_id_from_conversation_key(hinted_key)
                if hinted_owner:
                    try:
                        _agent_dir(hinted_owner)
                    except ValueError:
                        pass
                    else:
                        claim_hint = hinted_owner
                locked_agent_ids = {d.name, claim_hint}
                retention_holders = _acquire_retention_locks(*locked_agent_ids)
                try:
                    state = read_state(d.name)
                    claim_agent_id = d.name
                    lookup_session_id = tombstone_session_id
                    if isinstance(state, dict):
                        lookup_session_id = state.get("session_id") or tombstone_session_id
                        retention_unknown = False
                        retention_state: dict[str, object] = state
                        session_id = state.get("session_id", "")
                        # Every completed plain run records keep=False. A continuation
                        # follows readable original-owner state; unreadable owner
                        # intent receives bounded grace.
                        state_key = str(state.get("conversation_key") or "")
                        (
                            fallback_keep,
                            fallback_key,
                            fallback_sid,
                        ) = _cleanup_retention_fallback(
                            d.name,
                            session_id,
                        )
                        if fallback_sid:
                            # State is agent-writable. Once gateway-published
                            # identity exists, it cannot erase or redirect the
                            # continuation owner used for retention arbitration.
                            conversation_key = fallback_key
                            if not session_id:
                                session_id = fallback_sid
                            if "keep" not in state and fallback_keep is not None:
                                retention_state = dict(state)
                                retention_state["keep"] = fallback_keep
                        else:
                            conversation_key = state_key
                        owner_id = subagent_id_from_conversation_key(conversation_key)
                        if owner_id and owner_id != d.name:
                            try:
                                _agent_dir(owner_id)
                            except ValueError:
                                retention_unknown = True
                            else:
                                claim_agent_id = owner_id
                                owner_state = read_state(owner_id)
                                if isinstance(owner_state, dict):
                                    retention_state = owner_state
                                else:
                                    retention_unknown = True
                    else:
                        fallback_record = _cleanup_identity_fallback_record(d.name, "")
                        retention_state = {}
                        retention_unknown = True
                        session_id = ""
                        conversation_key = ""
                        if fallback_record is not None:
                            session_id = fallback_record.get("session_id", session_id)
                            sidecar_keep = fallback_record.get("keep")
                            if isinstance(sidecar_keep, bool):
                                retention_state["keep"] = sidecar_keep
                                # A durable false predates any later promotion
                                # that landed only in now-unreadable state. It
                                # cannot authorize immediate deletion; keep the
                                # bounded unknown-retention grace. True is safe
                                # to honor because it only preserves material.
                                if sidecar_keep:
                                    retention_unknown = False
                            conversation_key = str(fallback_record.get("conversation_key") or "")
                        owner_id = subagent_id_from_conversation_key(conversation_key)
                        if owner_id and owner_id != d.name:
                            try:
                                _agent_dir(owner_id)
                            except ValueError:
                                retention_unknown = True
                            else:
                                claim_agent_id = owner_id
                                owner_state = read_state(owner_id)
                                if isinstance(owner_state, dict):
                                    retention_state = owner_state
                                    retention_unknown = False
                                else:
                                    retention_unknown = True
                    if claim_agent_id not in locked_agent_ids:
                        continue
                    defer_cleanup = _should_defer_tombstone_cleanup(
                        retention_state=retention_state,
                        retention_unknown=retention_unknown,
                        cleanup_session_id=session_id,
                        died=died,
                        cutoff=cutoff,
                        now=now,
                    )
                    if defer_cleanup:
                        continue
                    # Hold arbitration through cleanup and rmtree. A promotion
                    # arriving after the keep=False decision returns retryable
                    # instead of writing keep=True just before deletion.
                    cleanup_identities = _tombstone_cleanup_identities(d.name)
                    within_retry_window = died >= now - _UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS
                    # Legacy/pre-upgrade runs can carry a SID only in the
                    # agent-writable state/tombstone. It is not safe deletion
                    # authority, but the folder is useful for a later trusted
                    # migration. Bound that lookup window so an unavailable
                    # migration cannot accumulate private run folders forever.
                    if lookup_session_id and not cleanup_identities and within_retry_window:
                        continue
                    cleanup_succeeded = True
                    for cleanup_sid, cleanup_provider, cleanup_cwd in cleanup_identities:
                        try:
                            if (
                                _cleanup_session_files_sync(
                                    cleanup_sid,
                                    cleanup_provider,
                                    cwd=cleanup_cwd,
                                )
                                is False
                            ):
                                cleanup_succeeded = False
                        except Exception:
                            cleanup_succeeded = False
                            logger.debug(
                                "prune: session cleanup failed for %s (%s)",
                                d.name,
                                cleanup_sid,
                                exc_info=True,
                            )
                    if not cleanup_succeeded and within_retry_window:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    if not d.exists():
                        with _CLEANUP_IDENTITY_LOCK:
                            _delete_cleanup_identities_file(d.name)
                            _delete_panel_dismissal(d.name)
                            _LIVE_CLEANUP_IDENTITIES.pop(d.name, None)
                            _LIVE_CLEANUP_HINTS.discard(d.name)
                    pruned += 1
                finally:
                    _release_retention_locks(retention_holders)
        except (OSError, ValueError, RecursionError):
            logger.debug("prune: tombstone processing failed for %s", d.name)
    return pruned


# ── session file cleanup ──────────────────────────────────────────────


def _cleanup_session_files_sync(
    session_id: str, provider: str = PROVIDER_LABEL_DEFAULT, *, cwd: str = ""
) -> bool:
    """Delete provider session files, returning whether cleanup completed.

    Synchronous — used during tombstone pruning (which runs in the reaper loop).
    A false result preserves the run folder and protected identity record so a
    future provider cleanup implementation or transient filesystem recovery can
    retry instead of making the leaked transcript permanently unreachable.

    Only the kiro-cli backend stores transcripts where this function can reach
    them. Any other *provider* is logged and its files are left in place, since
    reporting success without deleting anything hides the leak.
    """
    if not session_id:
        return True
    if session_id in (".", ".."):
        return False
    try:
        if provider == PROVIDER_LABEL_DEFAULT:
            sessions_dir = kiro_sessions_dir()
            succeeded = True
            for suffix in (".json", ".jsonl"):
                target = sessions_dir / f"{session_id}{suffix}"
                if not _is_safe_path(target, sessions_dir):
                    logger.error(
                        "_cleanup_session_files_sync: path traversal blocked for %s",
                        target,
                    )
                    return False
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    succeeded = False
                    logger.warning(
                        "_cleanup_session_files_sync: failed to delete %s",
                        target,
                        exc_info=True,
                    )
            return succeeded
        # Every other backend owns its own session storage, which this function
        # has no route to. Report failure so prune retains retry metadata.
        logger.debug(
            "_cleanup_session_files_sync: no cleanup route for provider %s; "
            "session %s files retained",
            provider,
            session_id,
        )
        return False
    except Exception:
        logger.warning(
            "_cleanup_session_files_sync: unexpected error cleaning session %s",
            session_id,
            exc_info=True,
        )
        return False


# This module keeps two of the readers that sweep every run folder: the panel records
# (:func:`read_panel_records`, which redacts and clamps what a record retains) and the
# stale-tombstone prune with its session-file cleanup. Every name its owners define still
# resolves as ``kiro_crew.subagent_persistence.<name>``. A write through this module
# (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the name
# (``kiro_crew._facade_mirror``). No owner imports this module; an owner imports only the
# owners listed before it.
# The owners log as ``kiro_crew.subagent_persistence``. New work goes to the owner of its
# responsibility:
#   ``subagent_persistence_layout`` -- run-folder paths, the registry root, the cleanup
#     identities, the owner-only record write
#   ``subagent_persistence_dismissals`` -- panel dismissals and the record-key ceilings
#   ``subagent_persistence_state`` -- creating the folder, its execution context, state.json
#     reads and locked writes, the tombstone reads, retention promotion, result streaming,
#     a session's memory mode
#   ``subagent_persistence_tombstones`` -- the finished result, the tombstone (owed and
#     retried), delivery, the slow-command log, deletion, orphans and leaked processes
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.subagent_persistence_layout",
    "kiro_crew.subagent_persistence_dismissals",
    "kiro_crew.subagent_persistence_state",
    "kiro_crew.subagent_persistence_tombstones",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the unsplit module's public names, and TYPE_CHECKING.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.subagent_persistence_dismissals import (  # noqa: F401
        _PANEL_DISMISSAL_LEAF,
        _panel_dismissal_path,
        _panel_dismissals_dir,
    )
    from kiro_crew.subagent_persistence_layout import (  # noqa: F401
        _CLEANUP_IDENTITIES_FILE,
        _CLEANUP_IDENTITIES_TRUST_DIR,
        _SUBAGENTS_DIR,
        _atomic_write,
        _cleanup_identities_path,
        _cleanup_identity_record,
        _protect_cleanup_identities_path,
    )
    from kiro_crew.subagent_persistence_state import (  # noqa: F401
        _LIVE_RUN_STATES,
        _RETENTION_LOCKS,
        _RETENTION_LOCKS_GUARD,
        _STATE_LOCKS,
        _STATE_LOCKS_GUARD,
        _AgentLock,
        _live_run_key,
        _lock_for_agent,
        _lock_for_registry,
        _on_event_loop,
        _read_state_at,
        _replace_in_run_folder,
        _retention_lock_for_agent,
        _try_acquire_registry_lock,
        _try_acquire_retention_lock,
        _try_acquire_state_lock,
    )
    from kiro_crew.subagent_persistence_tombstones import (  # noqa: F401
        _OWED_TOMBSTONE_MAX_BYTES,
        _OWED_TOMBSTONES_MAX,
        _SLOW_LOG_MAX_BYTES,
        _check_result_available,
        _create_exclusive,
        _owe_tombstone,
        _owed_retry_lock,
        _owed_tombstones,
        _owed_tombstones_lock,
        _retry_owed_tombstones_locked,
        _SettleableDelivery,
        _write_tombstone_file,
    )
