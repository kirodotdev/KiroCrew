"""Session persistence — save, restore, history prefix.

A dashboard slot's conversation lives in one session JSONL file: a metadata line
followed by the transcript rows. This module is the facade every caller imports
from, and it keeps the orchestration that has to stay together:

* the save transaction, ``_save_slot_to_history``: snapshot, refusals, the
  transcript lock, the atomic replace and the witness stamping, with its on-loop
  entry point ``save_slot_off_loop`` and the close fence it applies;
* the restore drivers and slot builders: ``restore_open_slots``,
  ``restore_recent_sessions``, their async twins, ``_rehydrate_slot_from_history``,
  ``_apply_recent_session`` and the prefetch reads they share;
* process-wide state with the code that mutates it: the reasoning-effort
  allowlist, and the persisted-entry memo ``_build_message_entry`` with its bounds;
* the private member-store assignment and the request-side retired-mode coercion.

The rules those consult are composed from :mod:`kiro_crew.dashboard.slot_persistence`:
``write_guards`` (the save's refusals and witnesses), ``metadata_codec`` (every
slot field of the metadata line, written and read back through one table),
``metadata_line`` (what a save folds against the line on disk before it encodes),
``transcript_merge`` (frozen prefix, foreign appends, payload), ``message_entries``
(row projection), ``restore_inputs`` (restore-time reads and screens) and
``turn_marker`` (the turn-in-flight marker). The slot builders construct and fill a
slot through ``metadata_codec``; the window replay and the rollback stay here. Every
project name this module bound before the owners were composed is still importable
from here, and the names tests rebind on this module are read through it at call
time, so such a patch reaches the code that moved.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque  # noqa: F401
from collections.abc import Callable, Iterable, Iterator, Mapping  # noqa: F401
from itertools import chain, islice  # noqa: F401
from pathlib import Path
from typing import Any

from kiro_crew import mcp_apps_render, model_registry  # noqa: F401
from kiro_crew.agent import kiro_agents_dir_path  # noqa: F401
from kiro_crew.agent_discovery import agent_model_map  # noqa: F401
from kiro_crew.atomic_write import atomic_write
from kiro_crew.chat_attachments import (  # noqa: F401
    ImageBudget,
    persist_inline_images,
    same_text_modulo_images,
)
from kiro_crew.config.loader import (  # noqa: F401
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
    KiroCrewConfig,
    config_dir,
)
from kiro_crew.dashboard.channel_slots import slot_closed_since
from kiro_crew.dashboard.chat_delivery import (  # noqa: F401
    ATTACHMENT_LIST_MAX_ITEMS,
    ATTACHMENT_PATH_MAX_LEN,
)
from kiro_crew.dashboard.chat_title import _TITLE_ORIGINS, _rehydrated_refresh_mark  # noqa: F401
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _normalize_model,
    _redact_meta_for_role,
    _sync_dashboard_slots,
    apply_pending_slot_memory_mode,
    drop_records_without_placeholders,
    effective_session_key,
    redact_display_content,
    session_key_for,
    slot_history_key,
    slot_transcript_key,
    with_bounded_redaction_records,
)
from kiro_crew.dashboard.slot_buffers import (  # noqa: F401
    committed_filtered_note_ids,
    drop_committed_restored_notes,
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
    union_deferred_notes,
)
from kiro_crew.dashboard.slot_queue_repository import (  # noqa: F401
    queue_persist_signature,
    sanitize_restored_queue,
)
from kiro_crew.dashboard.slot_registry import SlotRegistry, TakeoverBasis, TruncationClaim
from kiro_crew.dashboard.slot_retention import (
    RESTORE_SLOT_BUDGET,
    crew_bound_on_disk,
    live_loop_slot_keys,
    notify_left_in_history,
    stored_loop_slot_keys,
)
from kiro_crew.dashboard.state import (  # noqa: F401
    _MAX_DISMISSED_SOURCE_LINKS,
    _TRANSIENT_ROLES,
    DashboardState,
    _ChatSlot,
    _normalize_slot_key,
    _note_authorized_elsewhere,
    durable_row_count,
    is_stop_event_row,
    is_turn_interrupted,
    row_mid,
)
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.execution_context import (  # noqa: F401
    EXECUTION_CONTEXT_KEY,
    MEMORY_MODES,
    STRICTEST_MEMORY_MODE,
    canonical_memory_mode,
    read_session_execution,
    stricter_memory_mode,
)
from kiro_crew.history import (  # noqa: F401
    _FLOCK_ACQUIRE_TIMEOUT_S,
    HUMAN_TURN_META_KEY,
    METADATA_LINE_CORRUPT,
    ROWS_ONLY_DEFERRED_META_KEYS,
    ROWS_ONLY_OWNED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    ConversationLog,
    HistoryLockTimeout,
    _archive_lines,
    carry_provenance,
    carry_unowned_metadata,
    latest_transcript_ts,
    transcript_sort_key,
    update_metadata_off_loop,
)
from kiro_crew.memory_stores import UnknownMemoryStore, named_store_or_empty  # noqa: F401
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import file_lock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F401
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.session_agent_selection import session_agent_selection_name  # noqa: F401
from kiro_crew.validation import ARTIFACT_SLUG_RE  # noqa: F401

logger = logging.getLogger(__name__)


#: Env flag the container supervisor sets when it restored this boot's
#: ``open_slots.json`` from a committed REMOTE snapshot and did NOT restore the
#: transcripts (they arrive lazily, per turn). It tells the open-slot restore
#: that a listed slot whose transcript is absent from local disk is a
#: remote-only slot to keep as a reopen seed, not a dead tab to prune — the two
#: are indistinguishable from local disk alone, and dropping a remote-only slot
#: lets the next flush write an empty table the sidecar then commits. Unset on
#: an ordinary boot (a developer gateway restart), where an absent transcript is
#: a genuine answer and its key is pruned so dead tabs do not resurrect forever.
ENV_AUTHORITY_RESTORED: str = "KIROCREW_CONTAINER_AUTHORITY_RESTORED"


def _transcripts_may_be_remote_only() -> bool:
    """True when absent-local transcripts may live in a not-yet-fetched remote.

    Reads :data:`ENV_AUTHORITY_RESTORED`. Kept a function rather than a
    module-level constant so a test can set the env and observe the branch
    without a reimport, and so the value is read at restore time rather than at
    import time (the supervisor sets it before the backend process starts).
    """
    return os.environ.get(ENV_AUTHORITY_RESTORED) == "1"


#: Sentinel: the slot is a member key whose binding is missing/unreadable —
#: skip publishing it (the transcript stays on disk; the member-thread
#: endpoint re-creates and re-binds the slot on the next page open).
_SKIP_MEMBER_RESTORE: tuple[str, str] = ("", "__skip__")

#: Sentinel default for the ``member_identity`` parameters below: the caller
#: did not prefetch, resolve inline. Distinct from ``None`` (an ordinary,
#: non-member key) — defaulting to ``None`` would silently unpin every member
#: slot restored by a caller that forgot to prefetch.
_IDENTITY_UNRESOLVED: tuple[str, str] = ("", "__unresolved__")


_MAX_HISTORY_CHARS = 8000


# Fallback effort levels — used when no ACP session has reported its config
# yet (cold start). Sourced from the shared ``effort.py`` vocabulary so every
# provider agrees on the levels (incl. "xhigh") and there is a single source of
# truth; ACP overrides these at runtime via update_reasoning_effort_values().
# Order matches natural escalation (low→max) for display purposes.
_REASONING_EFFORT_FALLBACK_ORDER: list[str] = list(EFFORT_LEVELS)
_REASONING_EFFORT_FALLBACK = EFFORT_VALUES

# Runtime state: validation set + ordered list (ACP order preserved).
# Persisted JSON is untrusted input — values flow into a subprocess CLI arg
# and the ACP /effort slash command, so set-membership validation applies on
# the read path too, not just the API.
_reasoning_effort_values: set[str] = set(_REASONING_EFFORT_FALLBACK)
_reasoning_effort_ordered: list[str] = list(_REASONING_EFFORT_FALLBACK_ORDER)
# Levels backed by gateway-owned markers. At the advertised-level cap, a
# marked restore outranks a peer level that no owner has selected.
_reasoning_effort_marked: set[str] = set()

# Re-exported (back-compat) for any caller importing the static allowlist.
_REASONING_EFFORT_VALUES = EFFORT_VALUES


def get_reasoning_effort_values() -> frozenset[str]:
    """Return currently valid effort levels (ACP-dynamic + fallback)."""
    return frozenset(_reasoning_effort_values)


def get_reasoning_effort_ordered() -> list[str]:
    """Return effort levels in ACP-reported order (excludes empty/default)."""
    return list(_reasoning_effort_ordered)


# Anchored with ``\Z`` (not ``$``) so a value with a trailing newline such as
# "low\n" is rejected — ``$`` would match before the newline and let it through
# to the persistence/subprocess boundary.
_SAFE_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,20}\Z")
MAX_EFFORT_LEVELS_PER_CAPABILITY = 32
MAX_RETAINED_REASONING_EFFORT_VALUES = 256
# Crew panels are gateway-owned, precreated, and masked under both ordinary and
# relocated data homes. Their record reader uses flat *.json names, so this
# separate subdirectory cannot be mistaken for a panel.
_VALIDATED_EFFORT_DIR = "crew-panels/validated_effort_levels"


def _effort_marker_path(directory: Path, level: str) -> Path:
    """Use a portable basename, including for Windows device names like con."""
    return directory / hashlib.sha256(level.encode("utf-8")).hexdigest()


def cap_effort_capability_levels(levels: Iterable[object], *, source: str) -> list[str]:
    """Validate before applying the per-response cap, preserving safe input order."""
    accepted: list[str] = []
    dropped = 0
    for level in levels:
        if not isinstance(level, str) or not _SAFE_EFFORT_RE.fullmatch(level):
            continue
        if len(accepted) < MAX_EFFORT_LEVELS_PER_CAPABILITY:
            accepted.append(level)
        else:
            dropped += 1
    if dropped:
        logger.warning(
            "Dropped %d %s effort capability level(s): per-response limit %d reached",
            dropped,
            source,
            MAX_EFFORT_LEVELS_PER_CAPABILITY,
        )
    return accepted


def _retain_reasoning_effort_values(acp_levels: list[str], *, source: str) -> list[str]:
    """Keep safe levels up to the process-wide cap, preserving input order."""
    global _reasoning_effort_values
    retained = set(_reasoning_effort_values)
    accepted: list[str] = []
    accepted_set: set[str] = set()
    dropped = 0
    for level in acp_levels:
        if (
            not isinstance(level, str)
            or not _SAFE_EFFORT_RE.fullmatch(level)
            or level in accepted_set
        ):
            continue
        if level not in retained:
            if len(retained) >= MAX_RETAINED_REASONING_EFFORT_VALUES:
                dropped += 1
                continue
            retained.add(level)
        accepted.append(level)
        accepted_set.add(level)
    if dropped:
        logger.warning(
            "Dropped %d %s effort level(s): retained limit %d reached",
            dropped,
            source,
            MAX_RETAINED_REASONING_EFFORT_VALUES,
        )
    if retained != _reasoning_effort_values:
        _reasoning_effort_values = retained
    return accepted


def register_reasoning_effort_values(acp_levels: list[str]) -> list[str]:
    """Allow peer-advertised levels without replacing the local display order.

    A remote crew's options can reach the composer before its first turn. They
    must pass the hub's POST allowlist, but their order belongs to that slot,
    not the process-global fallback for unrelated local sessions. Return only
    retained levels so an over-cap peer option is never offered by the picker.
    """
    return _retain_reasoning_effort_values(acp_levels, source="peer")


def update_reasoning_effort_values(acp_levels: list[str]) -> None:
    """Update valid effort levels from ACP session config.

    Preserves ACP order for display. The validation set grows monotonically —
    it UNIONS new levels onto the existing set (and the fallback) up to a named
    total cap, and never shrinks. A retained level that a slot persisted stays
    valid after another session reports a narrower config.

    Sanitizes input: only safe lowercase effort names pass through
    (defense-in-depth for subprocess boundary).

    Note: ``_reasoning_effort_ordered`` is a process-global *fallback* display
    list only. The dropdown resolves levels per-slot from the slot's live ACP
    provider (see ``api_effort_levels``); this global is served only when no
    live provider is available.
    """
    global _reasoning_effort_ordered
    ordered = _retain_reasoning_effort_values(acp_levels, source="ACP")
    if acp_levels and not ordered:
        # Every advertised level was rejected at the cap. Keep the prior
        # fallback menu instead of replacing it with an empty one.
        return
    if ordered != _reasoning_effort_ordered:
        logger.info("Effort levels updated from ACP: %s", ordered)
        _reasoning_effort_ordered = ordered


def _remember_reasoning_effort_for_restore(level: str) -> None:
    """Durably retain a selected ACP level before writing it to a transcript.

    The transcript is agent-writable, so its value alone cannot expand the
    restore allowlist. Each non-fallback level gets a separate owner-only marker
    after ACP or a peer has validated it. Separate files avoid a lost update
    when two slot-save workers select different levels concurrently.
    """
    if level in _REASONING_EFFORT_FALLBACK or level not in _reasoning_effort_values:
        return
    if not _SAFE_EFFORT_RE.fullmatch(level):
        return
    directory = config_dir() / _VALIDATED_EFFORT_DIR
    if directory.parent.is_symlink():
        raise OSError("validated effort parent is a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise OSError("validated effort directory is a symlink")
    path = _effort_marker_path(directory, level)
    # Old raw-name markers remain readable after upgrade. Avoid creating a
    # duplicate that would count twice against the durable bound.
    lock_fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+b") as lock_file, file_lock(lock_file.fileno(), required=True):
        if path.is_symlink():
            raise OSError("validated effort marker is a symlink")
        if not _has_validated_effort_marker(level):
            max_markers = max(
                0, MAX_RETAINED_REASONING_EFFORT_VALUES - len(_REASONING_EFFORT_FALLBACK)
            )
            markers = sum(
                1
                for entry in directory.iterdir()
                if entry.name != ".lock" and not entry.is_symlink() and entry.is_file()
            )
            if markers >= max_markers:
                raise ValueError("validated effort marker limit reached")
            atomic_write(path, "", fsync=True, restrict_to_owner=True)
    _reasoning_effort_marked.add(level)


def _has_validated_effort_marker(raw: object) -> bool:
    """Read a gateway-owned marker during the off-loop restore prefetch."""
    if not isinstance(raw, str) or not _SAFE_EFFORT_RE.fullmatch(raw):
        return False
    directory = config_dir() / _VALIDATED_EFFORT_DIR
    if directory.parent.is_symlink() or directory.is_symlink():
        return False
    for marker in (_effort_marker_path(directory, raw), directory / raw):
        try:
            if not marker.is_symlink() and marker.is_file():
                return True
        except OSError:
            continue
    return False


def _validate_reasoning_effort(raw: object, *, persisted_marker: bool = False) -> str:
    """Return *raw* if it's a valid reasoning_effort string, else "".

    Used by the persistence restore paths so a tampered/corrupted
    metadata file cannot smuggle an arbitrary string into the CC
    ``--effort`` subprocess argument.
    """
    global _reasoning_effort_values
    if isinstance(raw, str) and raw in _reasoning_effort_values:
        if persisted_marker:
            _reasoning_effort_marked.add(raw)
        return raw
    if persisted_marker and isinstance(raw, str) and _SAFE_EFFORT_RE.fullmatch(raw):
        retained = set(_reasoning_effort_values)
        if len(retained) >= MAX_RETAINED_REASONING_EFFORT_VALUES:
            unselected = retained - _REASONING_EFFORT_FALLBACK - _reasoning_effort_marked
            if unselected:
                retained.remove(min(unselected))
            else:
                logger.warning("Discarding persisted reasoning_effort at retained limit: %r", raw)
                return ""
        retained.add(raw)
        _reasoning_effort_values = retained
        _reasoning_effort_marked.add(raw)
        return raw
    if raw:
        logger.warning("Discarding invalid persisted reasoning_effort: %r", raw)
    return ""


#: Retired modes a client may still SEND. An older dashboard build can still
#: ask for one on create, switch or fork, so the request runs as plain chat
#: instead of failing. ``crew`` is not here: its refusal predates this set.
_COERCED_REQUEST_MODES: frozenset[str] = frozenset({"orchestrator"})


def _coerce_requested_mode(raw: Any) -> Any:
    """Map a retired mode a request still names to "" (plain chat).

    Anything else is returned unchanged for the caller's own allowlist check.
    """
    if isinstance(raw, str) and raw in _COERCED_REQUEST_MODES:
        return ""
    return raw


def save_all_slots_to_history(state: DashboardState) -> None:
    """Save all active slots to history. Called on gateway shutdown."""
    for key in list(state._slots.keys()):
        # Re-fetch the occupant at each turn: this sequential pass takes real
        # disk time per slot while the loop can still recreate keys, and the
        # replacement's state — not a stale snapshot entry's — is what the
        # next startup must restore. The watch opens before the occupancy
        # re-read for the same reason the flush pass orders them that way: a
        # takeover before the open changes the occupant, one after it moves
        # the watch count past the basis, so neither goes unobserved by a
        # truncating (``_pending_rewrite``) save.
        # A refusal is not a commit, and shutdown has no later pass to lean
        # on: the process exits after this loop, so a save that lost its
        # ordering claim to a takeover must be answered HERE by saving the
        # occupant that won — its unsaved rows are what the next startup
        # restores. Two attempts bound the revisit: a takeover of the
        # takeover inside one shutdown pass is logged and yielded rather
        # than chased.
        for _attempt in range(2):
            slot = state._slots.get(key)
            if slot is None:
                break
            basis = SlotRegistry.open_takeover_basis(state, key)
            try:
                if state._slots.get(key) is not slot:
                    continue
                try:
                    committed = _save_slot_to_history(state, slot, force=True, takeover_basis=basis)
                except Exception:
                    logger.error("Shutdown: failed to save slot %s", slot.key, exc_info=True)
                    break
            finally:
                SlotRegistry.close_takeover_basis(state, basis)
            if committed or state._slots.get(key) is slot:
                break
            logger.warning(
                "Shutdown: save for slot %s lost its key to a takeover; saving the "
                "current occupant instead",
                key,
            )
        else:
            logger.warning(
                "Shutdown: slot %s changed hands twice during the pass; the newest "
                "occupant's window is restored from disk state at next startup",
                key,
            )
    # Snapshot the open-tab set so the next startup restores them. This is
    # belt-and-braces vs the periodic flush snapshot — it ensures graceful
    # shutdown captures the very latest state, including tabs whose
    # _dirty was False but were still visually present in the sidebar.
    try:
        state._persist_open_slots()
    except Exception:
        logger.debug("Shutdown: open_slots snapshot failed", exc_info=True)
    # Same reasoning for the context-meter readings: a graceful restart is the
    # case the reopen seed exists to serve, so the last reading must reach disk
    # rather than waiting for a periodic flush that will not come.
    try:
        state._persist_context_snapshots()
    except Exception:
        logger.debug("Shutdown: context snapshot flush failed", exc_info=True)


def _prefetch_rehydrate_inputs(
    conv_log: ConversationLog,
    history_key: str,
    *,
    adopt_closed: bool = False,
    kiro_model_map: dict[str, str] | None = None,
    with_status: bool = False,
) -> tuple[
    dict, bool, list[dict] | None, dict[str, str] | None, tuple[str, str] | None, str | None, bool
]:
    """Read everything :func:`_rehydrate_slot_from_history` needs, off the loop.

    The one prefetch seam shared by every async restore path — the metadata line,
    the chained message walk and (when the caller has no shared copy) the
    agent→model map. All three are blocking disk work; none of them touches slot
    state, so the whole function is safe to hand to ``asyncio.to_thread`` while
    the loop-affine slot mutation stays on the event loop. See
    :func:`rehydrate_slot_from_history_async` for why that split is mandatory
    rather than merely nice.

    *with_status* selects ``get_metadata_status`` over ``get_metadata`` so the
    open-tab restore keeps the readability signal it needs: ``get_metadata``
    reports ``{}`` for both "never persisted" and "could not be read after
    retries", and treating the second as the first is what silently discards a
    live tab.

    Returns ``(meta, readable, messages, model_map, member_identity, agent,
    effort_marker)``. The marker check is filesystem work too, so it belongs
    in this prefetch rather than the loop-affine apply phase.
    *messages* and *model_map* are ``None`` when there is nothing to build — no
    metadata, an unreadable read, or a session closed with ✕ that the caller did
    not opt to adopt — so a caller can decide without a second disk round trip.
    *member_identity* is the prefetched ``_member_restore_identity`` answer
    (dm.json is file IO too, and the apply half is loop-affine); it is resolved
    only when there is something to build.
    """
    if with_status:
        meta, readable = conv_log.get_metadata_status(history_key)
    else:
        meta, readable = conv_log.get_metadata(history_key), True
    if not readable or not meta or (meta.get("closed") and not adopt_closed):
        return meta or {}, readable, None, None, None, None, False
    return (
        meta,
        readable,
        conv_log.read_messages_chained(history_key),
        kiro_model_map if kiro_model_map is not None else _build_kiro_model_map(),
        # The transcript key is "dashboard:" + slot name; identity is a
        # property of the slot name.
        _member_restore_identity(history_key.removeprefix("dashboard:")),
        _restored_agent_name(str(meta.get("linked_session_key") or history_key), meta),
        _has_validated_effort_marker(meta.get("reasoning_effort")),
    )


def _open_slot_restore_plan(
    conv_log: ConversationLog, keys: list[object]
) -> tuple[list[object], set[str] | None]:
    """Order the open-tab keys newest first and read which tabs a loop drives. BLOCKING.

    Newest is the transcript's mtime, which advances on every message, so the
    tabs the restore budget keeps are the ones most recently used. The second
    value is the set of slot keys an armed auto-nudge loop drives (always
    restored), or ``None`` when the loop store cannot be read; the restore then
    applies no budget rather than leave a loop's tab unbuilt, which ends the loop.
    """

    def _mtime(raw: object) -> float:
        key = _sanitize_open_slot_key(raw)
        if key is None:
            return 0.0
        mtime = conv_log.session_mtime(slot_transcript_key(key))
        return float(mtime) if isinstance(mtime, (int, float)) else 0.0

    looped = live_loop_slot_keys()
    if looped is None:
        looped = stored_loop_slot_keys()
    return sorted(keys, key=_mtime, reverse=True), looped


def _over_restore_budget(state: DashboardState, key: str, looped: set[str] | None) -> bool:
    """Whether *key* is past the restore budget and not driven by a loop."""
    if looped is None or key in looped:
        return False
    return state.live_slot_count() >= RESTORE_SLOT_BUDGET


def _kept_past_budget(conv_log: ConversationLog, key: str) -> bool:
    """Whether slot *key* takes the normal path past the budget. BLOCKING.

    True for a pinned tab, for a tab in a folder (the recent-sessions restore
    and the channel reconciler rebuild every foldered tab whatever its age, so
    leaving one out here would neither hold the budget nor keep it in History),
    for a crew worker tab bound to an open work item (the wake gate reads a
    worker with no slot as closed), and for one with no
    readable local metadata: an unreadable read or an absent transcript costs
    the normal path no transcript read, and that path's own guards decide
    whether the key stays in the reopen seed (unreadable, or remote-only after a
    remote authority restore) or goes.
    """
    meta, readable = conv_log.get_metadata_status(slot_transcript_key(key))
    if not readable or not meta or bool(meta.get("pinned")) or bool(meta.get("folder_id")):
        return True
    return crew_bound_on_disk(key)


def _report_restore_budget(state: DashboardState, skipped: int) -> None:
    if not skipped:
        return
    logger.info(
        "restore_open_slots: left %d older tab(s) in history; the restore budget "
        "is %d open sessions",
        skipped,
        RESTORE_SLOT_BUDGET,
    )
    notify_left_in_history(
        state,
        skipped,
        f"{skipped} older open tab(s) were left in History at startup to keep "
        f"{RESTORE_SLOT_BUDGET} sessions open at most.",
    )


def _restore_open_slots_steps(state: DashboardState) -> "Iterator[int]":
    """Drive the open-tab restore one tab at a time, yielding the running count.

    Exposed as a generator so a plain synchronous caller
    (:func:`restore_open_slots`) can spin through it. The event-loop path does
    NOT drive this generator: it needs each per-tab disk read hoisted into a
    worker thread, which a synchronous generator cannot express, so
    :func:`restore_open_slots_async` runs its own prefetch-then-apply loop over
    the same shared helpers (:func:`_read_open_slots_keys`,
    :func:`_sanitize_open_slot_key`, :func:`_prefetch_rehydrate_inputs`). See
    :func:`restore_open_slots` for the behavioural contract.
    """
    if not state.conversation_log:
        return
    keys = _read_open_slots_keys()
    if not keys:
        return
    restored = 0
    # Rebound each pass so it reflects only THIS restore: a key that becomes
    # readable later must stop being carried, and a fresh set() keeps mutation
    # off the class-level frozenset baseline.
    unrestored: set[str] = set()
    state.unrestored_slot_keys = unrestored
    # Read once per restore: whether this boot's authority pair came from a
    # remote snapshot whose transcripts are fetched lazily (see
    # _transcripts_may_be_remote_only).
    preserve_remote_only = _transcripts_may_be_remote_only()
    # Built once and shared across every tab — it is identical per slot.
    kiro_model_map = _build_kiro_model_map()
    keys, looped = _open_slot_restore_plan(state.conversation_log, keys)
    skipped = 0
    for raw in keys:
        key = _sanitize_open_slot_key(raw)
        if key is None or key in state._slots:
            continue
        # Past the budget only a pinned or crew-bound tab is still built. The
        # rest stay in history, out of the reopen seed: they were not shown to be
        # unreadable.
        if _over_restore_budget(state, key, looped):
            try:
                pinned = _kept_past_budget(state.conversation_log, key)
            except Exception:
                logger.debug("restore_open_slots: pin read failed for %s", key, exc_info=True)
                pinned = True
            if not pinned:
                skipped += 1
                yield restored
                continue
        try:
            # Ask whether the metadata READ succeeded, not just whether it came
            # back empty (``with_status``) — see _prefetch_rehydrate_inputs.
            #
            # These reads MUST stay inside the per-tab guard. The async driver
            # has no except at its call site either, so anything escaping here
            # aborts dashboard startup and costs every LATER tab too.
            meta, readable, messages, model_map, member_identity, agent, effort_marker = (
                _prefetch_rehydrate_inputs(
                    state.conversation_log,
                    slot_transcript_key(key),
                    kiro_model_map=kiro_model_map,
                    with_status=True,
                )
            )
            restored += _apply_restored_open_slot(
                state,
                key,
                meta=meta,
                readable=readable,
                messages=messages,
                model_map=model_map,
                member_identity=member_identity,
                agent=agent,
                effort_marker=effort_marker,
                unrestored=unrestored,
                preserve_remote_only=preserve_remote_only,
            )
        except Exception:
            logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
            # Same epistemic position as an unreadable read: the session was not
            # shown to be gone, so keep its key rather than erasing the seed.
            unrestored.add(key)
            # No rollback here: _rehydrate_slot_from_history undoes its own
            # partial slot and restricted key, so every caller gets it rather
            # than only the ones that remembered to compensate.
        # Restore-time recovery of an app flag whose claim outlived it, after the
        # handler above rather than inside it: a failure here must not mark the
        # tab unrestored over a display flag. This driver's reads are inline by
        # construction, so the spool read is too.
        _recovered_slot = state._slots.get(key)
        if _recovered_slot is not None:
            _recover_mcp_app_claims(_recovered_slot)
        # One yield point per tab, reached on EVERY outcome. A failing tab still
        # costs real I/O, so a run of failing tabs that skipped the yield would
        # monopolise the loop and feed the stall watchdog. The sync driver just
        # spins through it; the async driver has its own per-tab yield.
        yield restored
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)
    _report_restore_budget(state, skipped)


def _apply_restored_open_slot(
    state: DashboardState,
    key: str,
    *,
    meta: dict,
    readable: bool,
    messages: list[dict] | None,
    model_map: dict[str, str] | None,
    unrestored: set[str],
    member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    agent: str | None = None,
    effort_marker: bool = False,
    conv_log: ConversationLog | None = None,
    started: float | None = None,
    preserve_remote_only: bool = False,
) -> int:
    """Turn one prefetched open-tab read into a slot; return 1 if it restored.

    LOOP-AFFINE — slot construction broadcasts through
    ``asyncio.Queue.put_nowait`` / ``Event.set``, so this half must run on the
    event loop even when the read that fed it did not. Shared by both drivers so
    the "unreadable metadata keeps its reopen seed" rule has one definition.

    *conv_log* and *started* together opt into the POST-HOP re-checks and are
    passed only by the async driver, whose read happened in a worker thread. The
    synchronous generator reads inline with no suspension point, so its pre-read
    answers cannot have gone stale and it would only pay for redundant work.

    *preserve_remote_only* carries :func:`_transcripts_may_be_remote_only`: when
    the authority pair was restored from a remote snapshot without the
    transcripts, a listed slot with no local transcript is a remote-only reopen
    seed to keep, not a dead tab to prune.
    """
    if not readable:
        unrestored.add(key)
        logger.warning(
            "restore_open_slots: metadata unreadable for %s; keeping it "
            "in the reopen seed for the next restore instead of dropping it",
            key,
        )
        return 0
    if messages is None:
        if preserve_remote_only and not meta.get("closed"):
            # The authority pair was restored from a remote snapshot without the
            # transcripts (they arrive lazily, per turn), so an absent LOCAL
            # transcript does not mean the slot is gone — its transcript lives in
            # a remote this task has not fetched yet. Keep the key as a reopen
            # seed. Dropping it lets the next 5s flush write an empty
            # open_slots.json that the sidecar commits, silently losing the
            # restored slot table on an ordinary task replacement. A tab the user
            # closed with ✕ still carries ``meta.closed`` and is NOT kept.
            unrestored.add(key)
            logger.info(
                "restore_open_slots: %s has no local transcript yet after a "
                "remote authority restore; keeping it in the reopen seed until "
                "lazy retrieval completes",
                key,
            )
            return 0
        # No metadata (never persisted) or the user closed the tab with ✕. Both
        # are confident answers, so the key is NOT carried as unrestored — the
        # synchronous helper's own guards reached the same verdict before.
        return 0
    if started is not None and slot_closed_since(state, key, started):
        # TAB-CLOSE RACE, same window ``rehydrate_slot_from_history_async`` and
        # the recent-sessions driver already guard. The user can click ✕ while
        # the transcript is in flight: the close pops the slot and records the
        # tombstone synchronously on the loop, but persists the ``closed`` flag
        # only after its own awaits — so the metadata read above still says open.
        # Rebuilding from it re-creates a tab the user dismissed, and the restored
        # slot's next flush writes metadata WITHOUT ``closed``, erasing the close
        # itself. The tombstone is the authoritative signal in this window.
        logger.info(
            "restore_open_slots: session %s was closed while its transcript "
            "loaded; not restoring a tab the user dismissed",
            key,
        )
        # A confident answer, like closed-on-disk — do NOT carry the key.
        return 0
    if conv_log is not None:
        # Synchronous and immediately before the build: no await may separate the
        # two (see _deletion_during_read).
        gone = _deletion_during_read(conv_log, slot_transcript_key(key), meta, messages)
        if gone is not None:
            logger.info(
                "restore_open_slots: session %s was %s while its transcript "
                "loaded; refusing to restore a tab whose flush would rewrite it",
                key,
                gone,
            )
            # A confident answer, like closed/absent — do NOT carry the key.
            return 0
    slot = _rehydrate_slot_from_history(
        state,
        key,
        kiro_model_map=model_map,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
        _prefetched_member_identity=member_identity,
        _prefetched_agent=agent,
        _prefetched_effort_marker=effort_marker,
    )
    return 1 if slot is not None else 0


def restore_open_slots(state: DashboardState) -> int:
    """Restore the tabs the user had open at the previous shutdown.

    Reads ``<config_dir>/open_slots.json`` (written by
    ``DashboardState._persist_open_slots`` on every flush) and rehydrates
    each listed key from on-disk session metadata so it shows up in the
    Sessions sidebar exactly as it did before the restart — independent of
    the ``restore_window_minutes`` mtime cutoff used by
    ``restore_recent_sessions``.

    Path resolves through ``config_dir()`` (honors ``KIROCREW_HOME``) so
    dev/test instances with non-default homes don't read the production
    ``~/.kiro/crew`` snapshot.

    Returns the number of slots restored. Missing / malformed file is a
    no-op (returns 0). Sessions that have been explicitly closed
    (``meta.closed``) are skipped via _rehydrate_slot_from_history's own
    guard, so closing a tab and then restarting still loses the tab.

    Tabs are built newest first. Once ``live_slot_count()`` reaches
    ``RESTORE_SLOT_BUDGET`` only pinned tabs, tabs an armed auto-nudge loop
    drives and crew worker tabs bound to an open work item are still built; the
    rest stay in history, unrestored and undeleted, and one notification says so.

    Blocking: restores every tab without yielding. Startup on the event loop must
    use :func:`restore_open_slots_async` instead — see the note there.
    """
    restored = 0
    try:
        for restored in _restore_open_slots_steps(state):
            pass
    finally:
        # The open-tab restore has run this boot (even for a missing/empty
        # file): an empty ``_slots`` from here on is authoritative, so the
        # periodic flush may snapshot open_slots.json again — see
        # DashboardState._persist_open_slots.
        state.open_slots_restored = True
    return restored


async def restore_open_slots_async(state: DashboardState) -> int:
    """:func:`restore_open_slots`, with the disk reads off the loop.

    Restoring a tab reads and redacts a transcript, so a user with many large
    tabs can spend tens of seconds in here. Doing that synchronously monopolizes
    the event loop — and because ``_loop_heartbeat`` pets the
    ``LoopStallWatchdog`` *from a coroutine*, a blocked loop cannot pet it. The
    watchdog's 25s ``exit_after`` timer then fires, dumps thread stacks and
    ``_exit``s the gateway, which is exactly the observed startup crash-loop: the
    app never finished booting.

    Yielding between tabs (the ``sleep(0)`` below) was the first fix and is kept:
    it bounds how long any ONE tab can hold the loop. But it only ever moved the
    boundary — the whole per-tab read still ran ON the loop, so a single large
    transcript could stall it for seconds before the next yield arrived. So the
    reads themselves now move: ``open_slots.json``, the agent→model map, and each
    tab's metadata + chained transcript walk are hoisted into
    ``asyncio.to_thread``, and only the slot mutation stays here.

    That mutation cannot follow them. Creating a slot broadcasts via
    ``asyncio.Queue.put_nowait`` / ``asyncio.Event.set``, neither of which is
    thread-safe, and ``_spawn_ws_send``'s ``ensure_future`` raises off-loop into
    a broad ``except`` that marks every connected dashboard client dead and drops
    it *without a close frame* — browsers then never reconnect. So this driver
    runs its own prefetch-then-apply loop (the shape
    ``rehydrate_slot_from_history_async`` established) rather than driving
    :func:`_restore_open_slots_steps`, whose reads are inline by construction.
    The generator stays for the synchronous callers.

    Because this yields, the 5s periodic flush (already running by this point)
    can interleave — so ``restoring_open_slots`` is held for the duration to stop
    it snapshotting a half-restored slot set over open_slots.json.
    """
    restored = 0
    state.restoring_open_slots = True
    try:
        # No conversation log means persistence is a no-op this process, but the
        # restore has still "run": fall through to the finally so the latch
        # flips and the persist writers resume pruning instead of staying in the
        # permanent pre-restore merge mode.
        if not state.conversation_log:
            return 0
        keys = await asyncio.to_thread(_read_open_slots_keys)
        if not keys:
            return 0
        # Rebound only once there is a snapshot to restore FROM, matching the
        # generator: a missing/malformed file must not clear a carried set.
        unrestored: set[str] = set()
        state.unrestored_slot_keys = unrestored
        conv_log = state.conversation_log
        # Read once per restore (see _transcripts_may_be_remote_only).
        preserve_remote_only = _transcripts_may_be_remote_only()
        kiro_model_map = await asyncio.to_thread(_build_kiro_model_map)
        keys, looped = await asyncio.to_thread(_open_slot_restore_plan, conv_log, keys)
        skipped = 0
        for raw in keys:
            key = _sanitize_open_slot_key(raw)
            if key is None or key in state._slots:
                continue
            # Same budget as the inline driver, with the pin read off the loop.
            if _over_restore_budget(state, key, looped):
                try:
                    pinned = await asyncio.to_thread(_kept_past_budget, conv_log, key)
                except Exception:
                    logger.debug("restore_open_slots: pin read failed for %s", key, exc_info=True)
                    pinned = True
                if not pinned:
                    skipped += 1
                    await asyncio.sleep(0)
                    continue
            try:
                started = time.time()
                meta, readable, messages, model_map, member_identity, agent, effort_marker = (
                    await asyncio.to_thread(
                        _prefetch_rehydrate_inputs,
                        conv_log,
                        slot_transcript_key(key),
                        kiro_model_map=kiro_model_map,
                        with_status=True,
                    )
                )
                restored += _apply_restored_open_slot(
                    state,
                    key,
                    meta=meta,
                    readable=readable,
                    messages=messages,
                    model_map=model_map,
                    member_identity=member_identity,
                    agent=agent,
                    effort_marker=effort_marker,
                    unrestored=unrestored,
                    # Opts into the post-hop re-checks (close tombstone +
                    # deletion): this driver's read ran in a worker thread, so
                    # its answers can have gone stale.
                    conv_log=conv_log,
                    started=started,
                    preserve_remote_only=preserve_remote_only,
                )
            except Exception:
                logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
                unrestored.add(key)
            # Same recovery as the inline driver, with the spool read awaited:
            # this driver runs on the loop, where a scan would stall the gateway.
            _recovered_slot = state._slots.get(key)
            if _recovered_slot is not None:
                await _recover_mcp_app_claims_async(_recovered_slot)
            # sleep(0) yields to the ready queue without adding wall-clock delay.
            # Reached on EVERY outcome, including a failing tab (see the
            # generator's note) — and still needed with the reads offloaded,
            # because the apply half above runs here.
            await asyncio.sleep(0)
        if restored:
            logger.info("Restored %d open tab(s) from open_slots.json", restored)
        _report_restore_budget(state, skipped)
    finally:
        # Always clear, even if a rehydrate raises — a stuck flag would silently
        # disable open-tab persistence for the rest of the process's life.
        state.restoring_open_slots = False
        # The open-tab restore has run this boot (even on an early return or a
        # raise): an empty ``_slots`` is authoritative from here, so a periodic
        # flush may snapshot open_slots.json again — see
        # DashboardState._persist_open_slots.
        state.open_slots_restored = True
    return restored


async def _recover_mcp_app_claims_async(slot: _ChatSlot) -> None:
    """:func:`_recover_mcp_app_claims`, with the spool read off the event loop.

    Same prefetch-then-apply split the async restore drivers already use for
    their transcript reads: the scan runs on a worker thread, the flagging is
    pure memory and stays here.
    """
    _reconcile_mcp_app_claims(
        slot, await asyncio.to_thread(_read_mcp_app_claims, effective_session_key(slot))
    )


def _pin_private_agent_assignment(
    session_key: str,
    agent: str,
    config: KiroCrewConfig,
    *,
    conversation_log=None,
    native_context: bool = False,
    authorized_store: str | None = None,
    memory_mode: str = "persistent",
    validate_only: bool = False,
) -> str:
    """Pin an authorized member selection, never a name recovered from history.

    Callers must authorize the owner's request or its session-control creation
    before using this helper. Restricted sessions cannot create persistent child
    sessions through aggregate controls. Legacy members keep declared V1 memory.

    ``authorized_store`` is passed through to :func:`_member_private_selection`,
    which owns both the store classification and that fence.
    """
    selected, store = _member_private_selection(agent, config, authorized_store=authorized_store)
    if not store:
        return ""
    from kiro_crew.execution_context import (  # noqa: F811
        bind_session_execution,
        read_session_execution,
        resolve_member_execution,
    )
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F811

    execution = resolve_member_execution(
        config, selected, memory_mode=memory_mode, validate_memory_files=False
    )
    log = conversation_log if conversation_log is not None else ConversationLog()
    # ``has_messages``, not ``has_log``: the transcript file already exists once
    # the slot's metadata (title, agent, model) was flushed, and an agent pick
    # on an empty chat must not read as "this chat has V1 history". It fails
    # closed: a transcript that exists but cannot be read is not provably
    # empty, so the member cannot change.
    previous = read_session_execution(session_key)
    if (
        previous is not None
        and previous.member_id is not None
        and previous.member_id != execution.member_id
    ):
        raise UnknownMemoryStore(
            "This conversation belongs to another member. Open a new conversation."
        )
    if previous is None or previous.member_id is None:
        try:
            has_history = native_context or log.has_messages(session_key)
        except OSError as exc:
            raise UnknownMemoryStore(
                "This conversation's transcript is unreadable, so its history cannot be "
                "verified; its member selection was not changed."
            ) from exc
        if has_history:
            raise UnknownMemoryStore(
                "This conversation retains its existing history. Open a new conversation for member memory."
            )
    if previous is not None:
        if previous.member_id == execution.member_id and previous.store == execution.store:
            execution = previous.with_mode(memory_mode)
        else:
            execution = execution.with_mode(previous.memory_mode)
    if validate_only:
        return store
    # Establishing, so it vouches. Every caller of this helper has already
    # authorized the owner's own request (see the docstring), and the store being
    # published is the one CONFIG resolves for the selected member: `execution`
    # comes from `resolve_member_execution`, and `previous` is reused above only
    # when its member AND store equal that config-resolved pair. So the vouched
    # store is never one the session's own record chose. Without the vouch, a
    # dashboard tab the owner bound to a member is published but unvouched, and
    # its own-store `session_create` is refused as `memory_delegation_denied`
    # ("this process holds no vouched identity for the caller") for its whole life.
    bind_session_execution(
        session_key,
        execution,
        replace_existing=previous is not None,
        expected=previous,
        vouch=True,
    )
    return store


def _member_private_selection(
    agent: str, config: KiroCrewConfig, *, authorized_store: str | None = None
) -> tuple[str, str]:
    """Resolve an explicit member choice without opening its learned database.

    The selection and prewarm-release paths share this classification. An
    optional captured store limits the choice to the caller's admitted routing.
    """
    selected = agent or config.default_agent
    if not selected or selected == "default":
        return "", ""
    store = getattr(config.agents.get(selected), "memory_store", "")
    if not isinstance(store, str) or not store:
        return "", ""
    if authorized_store is not None and named_store_or_empty(store) != named_store_or_empty(
        authorized_store
    ):
        return "", ""
    record = config.memory_stores.get(store)
    if record is None or record.memory_version != 2:
        return "", ""
    return selected, store


async def release_prewarmed_session(
    state: DashboardState, session_key: str, agent: str, config: KiroCrewConfig
) -> bool:
    """Drop a speculative pre-warm's resume pointer before member context capture.

    ``session.eager_spawn`` is on by default, so opening a new chat pre-creates
    a session for it while the chat is still on the default agent. That
    allocation publishes the ACP session id into ``SessionMap``, and the agent
    switch's own reset preserves the persistence entry
    (``SessionManager.reset`` clears the SID only when a live session is still
    registered). Read as a live or resumable runtime, that surviving pointer
    stands for V1 context the transcript does not show -- but on a chat the
    user has never sent a message in there is no such context, and the member choice
    the owner asked for is refused for a runtime nobody is using.

    The pointer is discarded rather than ignored, so the invariant the guard
    protects is satisfied in fact: nothing can resume the default agent's
    pre-warmed process into the member context, and the first member
    turn cold-starts under the store it validated. Returns whether a pointer
    was dropped.

    Every condition fails CLOSED, because a wrong "yes" here throws away a
    conversation the user can still resume:

    * a pick that is not a private V2 member keeps its pre-warm — a V1 chat
      has nothing to grant and must not lose its resumable session;
    * a live provider means the caller has not torn its session down, so this
      is not the settled post-reset window this helper is written for;
    * any transcript row, or a transcript that cannot be read at all, means
      the chat is not provably empty. That is the same probe, and the same
      unreadable-is-not-empty rule, that :func:`_pin_private_agent_assignment`
      applies before it grants.

    Ordered cheapest-first, and the no-pointer case returns before the
    transcript read: a chat with nothing to drop must not pay a file read or a
    map write on every private pick.
    """
    if not _member_private_selection(agent, config)[1]:
        return False
    sessions = getattr(state, "sessions", None)
    log = state.conversation_log
    if sessions is None or log is None:
        return False
    if not sessions.resumable_sid(session_key):
        return False
    if sessions.get_provider(session_key) is not None:
        return False
    try:
        if await asyncio.to_thread(log.has_messages, session_key):
            return False
    except OSError:
        return False
    return bool(await asyncio.to_thread(sessions.forget_conversation, session_key))


def member_store_ownership_holds(config: KiroCrewConfig, member: str, entry_store: str) -> bool:
    """Whether *member* still privately owns *entry_store* according to *config*.

    The one spelling of that question, shared by the reopen path in
    ``handlers/members.py`` and the grant below, so the check that returns a
    thread and the check that publishes authority over it cannot drift apart.

    :func:`require_member_memory_store` answers identity -- the member exists,
    its store resolves, nothing else owns that store, and the store's own
    recorded identity matches the member's immutable id -- and comparing its
    answer with *entry_store* is the continuity check: a member repointed while
    the request waited owns something other than what the caller decided about. It is
    deliberately blind to the record's ``owner_member`` NAME, which is what
    :func:`session_control._store_is_member_owned` authorizes admission on, so
    that field is compared here too: a reopen tolerating a stale name would
    serve a thread the admission gate refuses.

    Blocking: the identity check reads the store's database, so call it in a
    thread.
    """
    from kiro_crew.memory_stores import (  # noqa: F811
        UnknownMemoryStore,
        require_member_memory_store,
    )

    try:
        if require_member_memory_store(config, member) != entry_store:
            return False
    except UnknownMemoryStore:
        return False
    record = config.memory_stores.get(entry_store)
    return record is not None and record.owner_member == member


async def pin_private_agent_store(
    state: DashboardState,
    session_key: str,
    agent: str,
    config: KiroCrewConfig,
    *,
    memory_mode: str = "persistent",
    validate_only: bool = False,
) -> str:
    """Pin one dashboard selection from current private ownership off the loop.

    ``native_context`` is whether the session already has a live or resumable
    provider: such a session carries V1 context no transcript row shows yet.
    Callers snapshot the slot before awaiting and re-compare afterwards.

    Every selection is resolved and published from config read under the store
    namespace lock, and that lock is held through immutable binding publication,
    so member deletion, recreation, and store retirement cannot land between
    validation and publication. The request-entry member/store pair is kept only
    to compare against: a selection whose privateness or store changed while the
    request waited is refused rather than published, in either direction. A
    grant downgraded to non-private would write member content to the shared
    store, and one upgraded to private would publish authority the caller never
    asked for. This wrapper deliberately does not take the async config lock:
    callers may already hold a slot lock, while config writers serialize private
    ownership changes through the namespace lock.
    """
    selected = agent or config.default_agent
    _entry_selected, entry_store = _member_private_selection(selected, config)
    native_context = state.sessions.get_provider(session_key) is not None or bool(
        state.sessions.resumable_sid(session_key)
    )

    from kiro_crew.memory_stores import (  # noqa: F811
        UnknownMemoryStore,
        memory_store_namespace_lock,
    )

    changed = (
        f"Crew Member {selected!r} changed during private memory assignment; "
        "no private memory was granted"
    )

    @memory_store_namespace_lock()
    def pin_current_assignment() -> str:
        current = KiroCrewConfig.load()
        # The classification the selection path already uses, asked of current
        # config: an empty store means the selection is not private, so one
        # comparison covers both a changed store and a changed privateness.
        _current_selected, current_store = _member_private_selection(selected, current)
        if current_store != entry_store:
            raise UnknownMemoryStore(changed)
        if current_store and not member_store_ownership_holds(current, selected, current_store):
            raise UnknownMemoryStore(changed)
        return _pin_private_agent_assignment(
            session_key,
            selected,
            current,
            conversation_log=state.conversation_log,
            native_context=native_context,
            memory_mode=memory_mode,
            validate_only=validate_only,
        )

    return await asyncio.to_thread(pin_current_assignment)


def _member_restore_identity(slot_name: str) -> tuple[str, str] | None:
    """Resolve a member slot's restore identity from its binding.

    Returns ``None`` for ordinary keys (caller restores normally),
    ``(member, "member")`` when ``dm.json`` names a dispatchable crew, and
    :data:`_SKIP_MEMBER_RESTORE` when the key has no binding or its stored
    member name cannot be dispatched. The BINDING is the authority — transcript
    metadata lives in the same operator-editable JSONL it would otherwise re-pin
    from, so it is never consulted for a member key's agent or mode.
    """
    # Function-local ON PURPOSE: kiro_crew.members imports kiro_crew.artifacts
    # (slugify), and importing that at module scope closes the
    # artifacts -> ... -> webhooks -> validation -> artifacts cycle when this
    # module is imported outside the dashboard handler tree.
    from kiro_crew import members as members_mod

    prefix = members_mod.DM_SLOT_KEY_PREFIX
    if not slot_name.startswith(prefix):
        return None
    binding = members_mod.read_dm_binding_for_slot(slot_name)
    member = (binding or {}).get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        logger.warning(
            "restore: member slot %r has no dispatchable dm binding; leaving it "
            "unpublished until the Crew Member config or binding is repaired",
            redact_log_via_context(slot_name),
        )
        return _SKIP_MEMBER_RESTORE
    return member, members_mod.DM_SLOT_MODE


def _rehydrate_slot_from_history(
    state: DashboardState,
    slot_name: str,
    *,
    kiro_model_map: dict[str, str] | None = None,
    adopt_closed: bool = False,
    _prefetched_meta: dict | None = None,
    _prefetched_messages: list[dict] | None = None,
    _prefetched_member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    _prefetched_agent: str | None = None,
    _prefetched_effort_marker: bool = False,
) -> _ChatSlot | None:
    """Rehydrate a single dashboard slot from persisted history.

    *kiro_model_map* lets a bulk caller build the agent→model map once and share
    it across every slot instead of paying a fresh directory glob + JSON parse
    per tab; omit it and one is built on demand for single-slot callers.

    Unlike ``state.get_or_create_slot`` (which creates a fresh, empty slot with
    default ``memory_mode='persistent'``), this helper reads the session's
    metadata and messages from ``conversation_log`` so the restored slot has
    the original title/agent/model/memory_mode and its message history
    populated. Returns ``None`` if the session does not exist on disk (so
    callers can fall through to other delivery paths without creating a
    phantom empty tab).

    Intended for targeted resume paths (e.g. cron→origin injection after
    gateway restart). Bulk startup restore still uses ``restore_recent_sessions``.
    """
    if not state.conversation_log:
        return None
    # Canonicalize to the filename-charset key (idempotent) so callers holding
    # a stale raw display-style key (e.g. a cron's caller_session recorded
    # before slot-key normalization) resolve to the same slot the restore
    # paths create — get_or_create_slot() below applies the same fold.
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    # ``_prefetched_*`` let an async caller hoist the two disk reads (this
    # metadata line and the chained message walk further down) into a worker
    # thread and then run the REST of this function on the event loop — see
    # ``rehydrate_slot_from_history_async``. Slot construction below must stay
    # loop-affine: it broadcasts through ``asyncio.Queue.put_nowait`` and
    # ``Event.set``, neither of which is thread-safe. Omit them and the reads
    # happen inline, which is what the synchronous callers want.
    meta = (
        _prefetched_meta
        if _prefetched_meta is not None
        else (state.conversation_log.get_metadata(history_key))
    )
    # No metadata → session was never persisted. Don't create a phantom slot.
    if not meta:
        return None
    if _is_app_owned_channel_row(meta, history_key):
        return None
    # ``adopt_closed`` restores a session that was archived with ``closed``.
    # Off by default so a session the user closed stays closed; app-owned worker
    # slots pass it, because their lifecycle belongs to the app (their own delete
    # path ends them) and idle-slot cleanup marks them closed without the user
    # ever asking for that.
    if meta.get("closed") and not adopt_closed:
        return None
    _restore_cfg = _load_restore_cfg()
    # Same kiro-agent model map as restore_recent_sessions so legacy sessions
    # without a persisted `model` still resolve correctly. Reuse the caller's
    # when bulk-restoring — it is identical for every slot.
    if kiro_model_map is None:
        kiro_model_map = _build_kiro_model_map()
    # Captured BEFORE the slot is created so the rollback below can tell what
    # this call actually added from what was already there. Only the restricted
    # key needs the test: the early return above means the slot itself is always
    # this call's own creation.
    restricted_key = f"dashboard:{slot_name}"
    preexisting_restricted = restricted_key in state._restricted_keys
    # Member keys resolve their pin from dm.json BEFORE construction (the
    # constructor's member-* reservation refuses a bare member key); a member
    # key without a binding is skipped, not published — the member-thread
    # endpoint re-creates and re-binds it on the next open. Async callers
    # prefetch the binding read in their worker-thread step (dm.json is file
    # IO and this half is loop-affine); the inline resolve serves the
    # synchronous callers, whose reads already happen inline.
    _member_identity = (
        _member_restore_identity(slot_name)
        if _prefetched_member_identity is _IDENTITY_UNRESOLVED
        else _prefetched_member_identity
    )
    if _member_identity is _SKIP_MEMBER_RESTORE:
        return None
    purpose = _metadata_codec.Restore(
        name=slot_name,
        member=_member_identity,
        agent=_prefetched_agent,
        cfg=_restore_cfg,
        model_map=kiro_model_map,
        effort_marker=_prefetched_effort_marker,
    )
    try:
        slot = state.get_or_create_slot(slot_name, **_metadata_codec.slot_args(meta, purpose))
        # Every field the line carries is applied through the one field table
        # (``slot_persistence.metadata_codec``), from the metadata line already
        # read above. The title deliberately does not come from
        # ``list_sessions()``: that globs, stats and reads the first line of EVERY
        # session file, once per restored slot -- a boot with many open tabs then
        # blocks the event loop long enough to trip the LoopStallWatchdog.
        applied = _metadata_codec.apply(state, slot, meta, purpose)
        # Use read_messages_chained (not read_messages) so the loaded window walks
        # the tab_id ancestry across forks, matching restore_recent_sessions.
        # read_messages alone caps visible history at 200 lines from THIS file and
        # drops the ancestor chain — long-running forked sessions would lose 200+
        # messages of context on every gateway restart.
        messages = (
            _prefetched_messages
            if _prefetched_messages is not None
            else state.conversation_log.read_messages_chained(history_key)
        )
        if applied.minted_tab_id is not None:
            # Persist the freshly-minted tab_id AFTER reading the transcript above,
            # never before. update_metadata_off_loop dispatches an os.replace() of
            # THIS session file to a worker thread; scheduling it before the read
            # let that replace race the loop-thread transcript read of the very
            # same file. On Windows a concurrent replace makes the reader's open()
            # fail with a sharing violation (PermissionError, an OSError subclass),
            # and the on-loop read retry cannot pause (a loop sleep would starve the
            # LoopStallWatchdog heartbeat), so the immediate retries expire while the
            # replace is still in flight, _read_messages re-raises, and the
            # except-BaseException arm below rolls the whole tab back — the
            # intermittent `restored == N-1` open-tabs drop on restart
            # (test_restore_open_slots_async_yields_between_tabs, Windows shard).
            # Reading first removes the self-inflicted race: the file is quiescent
            # for the read, and the backfill lands once nothing is reading it. The
            # id is freshly minted with no on-disk siblings, so read_messages_chained
            # returns the identical window whether it is written before or after.
            # Kept off the loop because update_metadata enters _locked (flock +
            # os.close), a blocking-on-loop-prohibited op.
            update_metadata_off_loop(
                state.conversation_log, history_key, {"tab_id": applied.minted_tab_id}
            )
        # Only the recent window is loaded into memory; older on-disk lines become
        # the FROZEN PREFIX that saves never rewrite. _disk_older_count must
        # therefore count those older lines so the save model preserves them.
        older_cut = max(0, len(messages) - 500)
        slot._disk_older_count = older_cut
        # Recomputed from the on-disk rows on every load (never trusted from any
        # stored value): the durable-only view of the same prefix, which is what
        # absolute message positions are built over. See _ChatSlot.__init__.
        # ``islice``, not ``messages[:older_cut]`` — this can run on the event
        # loop for a large transcript, and a slice would copy the whole prefix.
        slot._disk_older_durable_count = durable_row_count(islice(messages, older_cut))
        for m in messages[-500:]:
            role = m.get("role", "assistant")
            cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
            content = m.get("content", "")
            # Neither content nor meta is redacted here. Redaction happens where the
            # data is EMITTED (chat_utils._prepare_messages for the slot detail
            # endpoint, _ChatSlot.to_dict for the sidebar payload,
            # _build_history_prefix for the ACP prompt) — every path a client or model
            # can observe.
            #
            # CONTENT, however, is redacted right here, on load. That split is
            # deliberate and measured, and it is the crux of this change:
            #
            #   field    | read sites | share of the ~7s load cost
            #   ---------|------------|---------------------------
            #   content  |    ~204    | ~0.4s  (6%)
            #   meta     |     31     | ~5.5s  (79%)
            #
            # `meta.tool_input` carries the large tool payloads, so meta is where the
            # boot cost actually lives — and its 31 readers are tractable: outside the
            # emit sites (which redact and are covered by
            # test_display_time_redaction.py) every one reads only CONTROL fields
            # (`done`, `tool_call_id`), never payload text. Deferring meta to display
            # time is therefore both where the win is and safely enumerable.
            #
            # `content` is the opposite on both axes: it is cheap (0.4s) and it has
            # ~204 readers across the dashboard, so "every reader must remember to
            # redact" is not an invariant anyone can hold. Three separate egress paths
            # (the side-chat prompt, the orchestrator stage-result file, and the
            # title-model prompt) can each leak restored content if a reader forgets.
            # Paying 0.4s here restores the single chokepoint — any present or FUTURE
            # reader of `m["content"]` gets clean bytes — instead of relying on an
            # enumeration of every reader.
            # `role != "user"`, never `not in ("user", "system")`: user-authored text
            # stays raw because its author is its only reader, but `system` MUST be
            # redacted — the write path excludes it, so system bytes reach disk raw.
            if role != "user":
                content = redact_display_content(content)
            slot.append(
                role,
                content,
                cls,
                ts=m.get("ts", ""),
                # broadcast=False: replaying history must not emit N `chat_message`
                # events. _broadcast_chat_message redacts non-user content (parity
                # with _prepare_messages) but deliberately not meta, and this
                # helper also runs for on-demand cold-slot rehydrates while clients
                # ARE connected, so broadcasting here would push unredacted meta
                # straight to them. Clients get the transcript from the slot detail
                # endpoint (redacted) and the sidebar from the coalesced slots push.
                broadcast=False,
                # meta is NOT redacted here — same reasoning as content, and
                # it is where the cost actually was: tool `meta.tool_input` carries
                # the large payloads, so meta redaction was ~5.5s of a ~7s restore
                # while content redaction was only ~0.4s. Redacted at emit instead
                # (chat_utils._prepare_messages), which is the only path that returns
                # meta to a client. Blocked-link records are the exception: they are
                # BOUNDED here because this is where the slot retains them, and the
                # bound is a no-op for a row that carries none.
                meta=(
                    with_bounded_redaction_records(m["meta"])
                    if isinstance(m.get("meta"), dict)
                    else None
                ),
                mint_mid=False,
            )
            # Provenance is not a slot.append() argument, so carry it onto the
            # message the append just created. Without this the window loses where
            # each turn came from and the next flush restamps it "dashboard".
            carry_provenance(slot.messages[-1], m)
            _attach_variants(slot, m)
        slot.drain()
        slot._resumed_count = len(slot.messages)
        # The whole in-memory window is already on disk → it is the on-disk window
        # region. Saves re-serialize the window in place; the frozen prefix (older
        # turns counted above) is never rewritten.
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        # A local turn admitted by the previous process and never torn down, the
        # held notes the window already delivered, and the title refresh mark
        # re-based against the rows the window holds -- the fields whose read
        # needs the window.
        applied.settle(messages)
        logger.info("Rehydrated session %s (%s) from history", slot_name, slot.title)
        return slot
    except BaseException:
        # Undo what THIS call added.
        #
        # Owned here rather than in each caller: get_or_create_slot runs before
        # the fallible work (the transcript read, redaction, slot.append), so a
        # failure leaves an empty slot registered in state._slots. A caller that
        # forgets to compensate leaves restore_recent_sessions to hit its
        # `if slot_name in state._slots: continue` dedup guard and skip the
        # proper restore -- the user then sees a tab with the right title and
        # agent but empty or wrong history.
        #
        # Unconditional pop: the function returns early when the slot already
        # exists, so reaching here means this call created it.
        state._slots.pop(slot_name, None)
        if not preexisting_restricted:
            # Otherwise a later get_or_create_slot (default memory_mode
            # 'persistent') silently inherits restricted status, blocking
            # consolidation and lessons for what should be a normal session.
            state._restricted_keys.discard(restricted_key)
        raise


def _await_takeover_ordering_settled(state: DashboardState, slot_name: str) -> bool:
    """Block (off the loop) until no takeover-losing save is unsettled on *slot_name*.

    The read half of the takeover-ordering contract. A claim the takeover note
    marked overtaken belongs to a save the commit gate refuses — and, for a
    hand-over write (``_pending_rewrite``, no snapshot), one that lands the
    popped slot's unsaved rows append-safely under its per-session lock before
    releasing. The claim is retired only after that lock exits, so its absence
    is one half of the signal that the rows a takeover's read must observe are
    on disk. The other half is the key's takeover WATCH: a save still in
    dispatch has no claim to wait on, but the takeover note that preceded this
    wait bumped the open watch, so that save's claim is born overtaken at
    registration and the same refused-save append follows — waiting through
    watch retirement covers the dispatch gap a claims-only wait leaves open
    (a resume could otherwise outrun the save it overtook and hydrate a window
    the fallback appends behind). The lock itself cannot give this ordering:
    the transcript projection serves mtime-cached reads and takes its fill
    lock best-effort, so a read can return pre-append bytes while the refused
    save is still inside its region.

    Bounded by the same deadline the save's own cross-process acquire polls to
    (an overtaken save can sit queued behind another writer for that long
    before it appends), plus write headroom. Returns ``True`` once settled and
    ``False`` on expiry — and expiry must fail CLOSED at the caller: a resume
    that reads anyway hydrates a window whose missing tail a later truncating
    rewrite by the hydrated replacement silently deletes, so the callers
    refuse with a retryable error instead of serving unsettled history.

    Enter this OFF the event loop; it sleeps.
    """
    deadline = time.monotonic() + _FLOCK_ACQUIRE_TIMEOUT_S + 2.0
    while SlotRegistry.takeover_ordering_pending(state, slot_name):
        if time.monotonic() >= deadline:
            logger.warning(
                "Resume of %s refused: a takeover-losing save is still in flight "
                "after %.1fs, and reading now would hydrate a window the refused "
                "save appends behind",
                slot_name,
                _FLOCK_ACQUIRE_TIMEOUT_S + 2.0,
            )
            return False
        time.sleep(0.002)
    return True


async def rehydrate_slot_from_history_async(
    state: DashboardState,
    slot_name: str,
    *,
    kiro_model_map: dict[str, str] | None = None,
    adopt_closed: bool = False,
) -> _ChatSlot | None:
    """:func:`_rehydrate_slot_from_history` with the disk reads off the loop.

    Same contract and return values as the synchronous form, including
    returning ``None`` for a session the user closed with ✕.

    Why split rather than simply wrapping the whole thing in
    ``asyncio.to_thread``: slot construction is loop-affine. It reaches
    ``get_or_create_slot`` → ``push_slots_update`` → ``_broadcast``, which uses
    ``asyncio.Queue.put_nowait`` and ``Event.set`` — neither thread-safe — and
    ``_spawn_ws_send``'s ``ensure_future`` raises off-loop. That raise lands in
    a broad ``except`` that marks every connected dashboard client dead and
    drops it *without a close frame*, so browsers never reconnect and stop
    receiving frames until a manual reload. ``restore_open_slots_async``
    documents the same invariant.

    So only the reads move: the metadata line, the chained message walk (tens of
    MB of read plus JSON parse on a large session) and the agent→model map.
    Everything that touches slot state runs on the loop, as the synchronous
    callers do. The reads are shared with the two bulk restore drivers via
    :func:`_prefetch_rehydrate_inputs`, so all three prefetch identically.

    Because the read is offloaded, the state it observed can be stale by the time
    the build runs. Both post-hop windows are re-checked below, synchronously and
    immediately before the build so no await can reopen them: the close tombstone
    (a ✕ during the read) and :func:`_deletion_during_read` (the session deleted,
    or deleted and recreated, during the read). Returning ``None`` for either is
    part of the contract — the callers already handle a ``None`` result.

    A rebuilt slot also gets its MCP-app claim recovery here, so every caller has
    it without asking; see the comment at that call for why it is not each
    caller's job.
    """
    if not state.conversation_log:
        return None
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    conv_log = state.conversation_log

    # Same-key takeover note, BEFORE the read is dispatched. A truncating save
    # for this key can be mid-flight on a worker thread, and the prefetch below
    # serializes behind the per-session lock it holds — so an unannounced
    # resume would block through the save's file replace and then adopt the
    # truncated window as its live transcript. Marking the claim first makes
    # the save's commit gate refuse instead, and the read (however the lock is
    # granted) finds the transcript the truncation never changed. A claim that
    # already committed stays committed: this resume then knowingly reads the
    # post-rewrite transcript, the sequential "rewrite, then reopen" order.
    # Fire-and-forget — no branch on the outcome, the ordering does the work.
    # The transcript this resume is about to hydrate rides along: a teardown
    # whose own write routes to a LINKED transcript cannot see this resume in
    # the map while the read below is pending, and the declared target is what
    # lets it keep its ordering claim exactly when this resume reads its file.
    if not SlotRegistry.note_same_key_takeover(state, slot_name, transcript=history_key):
        # Refused at admission: the key's teardown watch already retains as
        # many declared transcripts as one teardown admits, so this takeover's
        # target could not be recorded and nothing about it was. The same
        # retryable refusal an unsettled ordering answers with -- the watch
        # retires when the saves holding it settle.
        raise HistoryLockTimeout(
            f"resume of {slot_name!r} refused: its takeover note could not be recorded "
            "against the key's teardown watch"
        )
    # The note decides the ordering; this wait makes the read HONOR it. A save
    # the note just overtook still holds the per-session lock, and its refusal
    # lands the popped slot's unsaved rows append-safely under that lock before
    # releasing (the commit gate's fallback) — rows this resume's window must
    # carry, or the replacement publishes without them and its own later
    # truncating rewrite drops them from the transcript for good. The lock
    # cannot order the read by itself (the projection serves cached and
    # best-effort-locked reads), so the read waits for the claim table instead:
    # an overtaken claim retires only after its save exits the lock, appends
    # included. Off the loop, bounded — and fail-CLOSED on expiry: reading
    # anyway would hydrate exactly the unsettled window the wait exists to
    # prevent, so an expired wait raises the retryable lock-contention error
    # the read layer already speaks rather than serving that window.
    if SlotRegistry.takeover_ordering_pending(state, slot_name):
        if not await asyncio.to_thread(_await_takeover_ordering_settled, state, slot_name):
            raise HistoryLockTimeout(
                f"resume of {slot_name!r} would hydrate an unsettled transcript: "
                "a takeover-losing save is still in flight for its key"
            )

    started = time.time()
    _meta, _readable, messages, model_map, _member_id, agent, effort_marker = (
        await asyncio.to_thread(
            _prefetch_rehydrate_inputs,
            conv_log,
            history_key,
            adopt_closed=adopt_closed,
            kiro_model_map=kiro_model_map,
        )
    )
    # ``messages is None`` covers both "never persisted" and "closed with ✕"
    # — the prefetch already applied the same guards the synchronous form does.
    if messages is None:
        return None
    meta = _meta
    # Read the spool HERE, in the awaiting phase, not after the build. The two
    # race re-checks below are placed synchronously and immediately before the
    # build precisely so no await can reopen the windows they close, and an await
    # AFTER the build reopens the deletion one a step later: the slot is published
    # by then, so a deletion landing during that await leaves the caller holding a
    # slot for a session that has been deleted, and the delete-won save discards
    # what it accepts. The remedy is the split this module uses everywhere else --
    # filesystem work in the prefetch phase, the apply synchronous.
    #
    # The key has to be derived rather than asked of a slot, because there is no
    # slot yet: ``session_key_for`` is the same rule ``effective_session_key``
    # applies, and the builder below sets ``linked_session_key`` from this very
    # metadata field, so the two answer alike. ``history_key`` is NOT the fallback
    # -- it resolves the transcript FILE and its own docstring says the session key
    # cannot be recovered that way, so a channel-born slot would be addressed as a
    # ``dashboard:`` session that does not exist.
    _claims = await asyncio.to_thread(
        _read_mcp_app_claims,
        session_key_for(slot_name, str(meta.get("linked_session_key") or "")),
    )
    # Tab-close race. The user can click ✕ while the read above is in flight.
    # The close pops the slot and records a tombstone synchronously on the loop,
    # but persists the ``closed`` flag only after its own awaits — so the
    # metadata just read still says open, and rebuilding from it would re-create
    # a tab the user dismissed and then fire a nudge turn into it. The tombstone
    # is the authoritative signal in that window; the surface reconciler
    # consults it after its own awaits for the same reason.
    # Skipped for ``adopt_closed`` callers: those are app-owned worker slots
    # whose lifecycle belongs to the app rather than the user, and they have
    # already opted into restoring a session carrying the closed flag.
    if not adopt_closed and slot_closed_since(state, slot_name, started):
        logger.info(
            "Rehydration abandoned: session %s was closed while its transcript loaded",
            slot_name,
        )
        return None
    # DELETION race, the same window and the same remedy the two bulk restore
    # drivers apply. This wrapper's read is offloaded too, so it carries the same
    # window — the guard is folded in here rather than left as the one uncovered
    # instance, because a class of defect handled at two of three call sites
    # simply returns through the third.
    #
    # NOT gated on ``adopt_closed``: that opt-in is about the ``closed`` FLAG (an
    # app-owned worker slot whose lifecycle belongs to the app), not about the
    # session having been permanently deleted. Nothing wants to resurrect a
    # deleted transcript.
    #
    # Synchronous and immediately before the build, so no await can reopen the
    # window it closes.
    gone = _deletion_during_read(conv_log, history_key, meta, messages)
    if gone is not None:
        logger.info(
            "Rehydration abandoned: session %s was %s while its transcript "
            "loaded; refusing to rebuild a slot whose flush would rewrite it",
            slot_name,
            gone,
        )
        return None
    _restored = _rehydrate_slot_from_history(
        state,
        slot_name,
        kiro_model_map=model_map,
        adopt_closed=adopt_closed,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
        _prefetched_member_identity=_member_id,
        _prefetched_agent=agent,
        _prefetched_effort_marker=effort_marker,
    )
    if _restored is not None:
        # Claim recovery belongs HERE, not in each caller: this function is how
        # anything outside this module rebuilds one slot from disk by TARGETED
        # history rehydration -- `channel_slots.surface_channel_session` reaches a
        # slot by its own route, so "the only way" would overstate it -- and it has
        # eight such callers (members, messaging, files, two in the Slack
        # gateway, Issue Radar, Spec Builder). Hooking them one at a time is how
        # the bulk drivers were first wired and two of four were missed; a caller
        # added later would silently restore a row whose flag the crash lost.
        #
        # SYNCHRONOUS, and after the build so the slot the recovery may mark dirty
        # is the one the caller receives. No await may separate the deletion
        # re-check above from this line, which is why the spool read happened back
        # in the prefetch phase -- the apply itself only touches memory.
        #
        # The live-slot early return above is deliberately NOT covered: nothing was
        # read from disk there, its flags are already in memory, and messaging's
        # cache hit would pay a spool scan per lookup.
        _reconcile_mcp_app_claims(_restored, _claims)
    return _restored


def _prefetch_recent_session(
    conv_log: ConversationLog,
    key: str,
    session: dict,
    *,
    folders_only: bool,
    cutoff: float | None,
) -> tuple[dict | None, list[dict] | None, tuple[str, str] | None, str | None, bool]:
    """Read one candidate session's metadata + transcript, off the loop.

    Applies the selection filters BETWEEN the two reads so a session that is
    going to be skipped never pays for its transcript walk — the metadata read is
    what the filters need, and it is the cheap one.

    Returns ``(None, None, None, None, False)`` for a session this pass must skip (not
    folder'd / pinned under ``folders_only``, closed with ✕, or outside the
    mtime window). The third element is the prefetched
    ``_member_restore_identity`` answer — dm.json is file IO too, and the apply
    half is loop-affine. Pure disk work: no slot state is touched, so the whole
    function is safe in ``asyncio.to_thread`` while the loop-affine apply half
    stays on the loop.
    """
    meta = conv_log.get_metadata(key)
    if not meta:
        # No metadata line at all. ``list_sessions()`` is a SNAPSHOT taken one
        # thread hop before this read, so a session can be
        # deleted in between and still appear in the list — or the read itself
        # came back empty. Either way there is nothing to build from, and
        # building anyway is destructive rather than merely useless: an empty
        # ``meta`` sails past the folder/pin/closed/cutoff filters below (all of
        # which read falsy), reaches ``get_or_create_slot``, and registers a
        # PHANTOM slot whose next flush RECREATES the transcript the user
        # deleted. ``_rehydrate_slot_from_history`` already refuses on empty
        # metadata for exactly this reason ("don't create a phantom slot"); this
        # makes the recent-sessions path agree with it.
        return None, None, None, None, False
    has_folder = bool(meta.get("folder_id"))
    has_pin = bool(meta.get("pinned"))
    if folders_only and not has_folder and not has_pin:
        return None, None, None, None, False
    if meta.get("closed"):
        return None, None, None, None, False
    if not has_folder and not has_pin:
        if cutoff is not None and session.get("modified", 0) < cutoff:
            return None, None, None, None, False
    return (
        meta,
        conv_log.read_messages_chained(key),
        _member_restore_identity(_recent_session_slot_name(key) or ""),
        _restored_agent_name(
            str(
                meta.get("linked_session_key")
                or slot_transcript_key(_recent_session_slot_name(key) or key)
            ),
            meta,
        ),
        _has_validated_effort_marker(meta.get("reasoning_effort")),
    )


def _apply_recent_session(
    state: DashboardState,
    key: str,
    slot_name: str,
    session: dict,
    meta: dict,
    messages: list[dict],
    *,
    conv_log: "ConversationLog",
    kiro_model_map: dict[str, str],
    restore_cfg: "KiroCrewConfig | None",
    member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    agent: str | None = None,
    effort_marker: bool = False,
) -> None:
    """Build the slot for one prefetched recent session.

    LOOP-AFFINE — everything here mutates slot state, and slot creation
    broadcasts through ``asyncio.Queue.put_nowait`` / ``Event.set`` (neither
    thread-safe). Split out of :func:`_restore_recent_sessions_steps` so the
    synchronous generator and :func:`restore_recent_sessions_async` share one
    definition of the build while differing only in where the reads that feed it
    happen.
    """
    _restore_cfg = restore_cfg
    # Member keys resolve their pin from dm.json BEFORE construction (the
    # constructor's member-* reservation refuses a bare member key); a member
    # key without a binding is skipped, not published. Async callers prefetch
    # the binding read in their worker-thread step (this half is loop-affine);
    # the inline resolve serves synchronous callers.
    _member_identity = (
        _member_restore_identity(slot_name)
        if member_identity is _IDENTITY_UNRESOLVED
        else member_identity
    )
    if _member_identity is _SKIP_MEMBER_RESTORE:
        return
    if _is_app_owned_channel_row(meta, key):
        return
    purpose = _metadata_codec.Recent(
        name=slot_name,
        member=_member_identity,
        agent=agent,
        cfg=_restore_cfg,
        model_map=kiro_model_map,
        effort_marker=effort_marker,
        listing=session,
        history_key=key,
    )
    # No channel origin here: the caller skips every non-dashboard key, so a
    # channel-born session never reaches this — ``channel_slot_reconciler`` owns
    # surfacing those.
    slot = state.get_or_create_slot(slot_name, **_metadata_codec.slot_args(meta, purpose))
    # The title is the session list's (it names an untitled session after its
    # first message); every other field comes from the line, through the one
    # field table (``slot_persistence.metadata_codec``).
    applied = _metadata_codec.apply(state, slot, meta, purpose)
    if applied.minted_tab_id is not None:
        # restore_recent_sessions runs during on_startup (event loop live) — keep
        # the _locked flock/os.close off the loop via the off-loop backfill
        # helper. Dispatched AFTER the transcript read (the caller prefetches
        # messages first) so its os.replace() cannot race the read of the same
        # file — see the equivalent note in _rehydrate_slot_from_history.
        update_metadata_off_loop(conv_log, key, {"tab_id": applied.minted_tab_id})
    older_cut = max(0, len(messages) - 500)
    slot._disk_older_count = older_cut
    # Durable-only view of the same prefix, recomputed from disk on every load —
    # see the equivalent line (and the islice rationale) in
    # _rehydrate_slot_from_history.
    slot._disk_older_durable_count = durable_row_count(islice(messages, older_cut))
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        # CONTENT is redacted on load; META is deferred to the emit sites.
        # See the equivalent loop in _rehydrate_slot_from_history for the
        # measured rationale (content ~0.4s / ~204 readers, meta ~5.5s /
        # 31 readers that touch only control fields outside the emit sites).
        if role != "user":
            content = redact_display_content(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            broadcast=False,
            # Blocked-link records bounded where the slot retains them; see the
            # equivalent append in _rehydrate_slot_from_history.
            meta=(
                with_bounded_redaction_records(m["meta"])
                if isinstance(m.get("meta"), dict)
                else None
            ),
            mint_mid=False,
        )
        # See the equivalent call in _rehydrate_slot_from_history.
        carry_provenance(slot.messages[-1], m)
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (counted in
    # _disk_older_count above) are the frozen prefix saves never rewrite.
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    # The held notes the window already delivered, the local-turn marker and
    # the title refresh mark: the fields whose read needs the loaded window.
    applied.settle(messages)
    logger.info("Restored session %s (%s)", slot_name, slot.title)


def _restore_recent_sessions_steps(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> "Iterator[int]":
    """Drive :func:`restore_recent_sessions` one session at a time.

    Generator so a plain synchronous caller can spin through it. The event-loop
    path does NOT drive this one either: it needs ``list_sessions()`` and each
    per-session read hoisted into a worker thread, which a synchronous generator
    cannot express, so :func:`restore_recent_sessions_async` runs its own
    prefetch-then-apply loop over the same shared helpers. This path restores
    every folder'd/pinned session regardless of the mtime window, so it can be
    just as slow as the open-tab restore — measured at 13.6s for 76 sessions.
    """
    if not state.conversation_log:
        return
    conv_log = state.conversation_log
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    restored = 0

    kiro_model_map = _build_kiro_model_map()
    _restore_cfg = _load_restore_cfg()
    for s in conv_log.list_sessions():
        key = s.get("key", "")
        slot_name = _recent_session_slot_name(key)
        if slot_name is None or slot_name in state._slots:
            continue
        meta, messages, _member_id, agent, effort_marker = _prefetch_recent_session(
            conv_log, key, s, folders_only=folders_only, cutoff=cutoff
        )
        if meta is None or messages is None:
            continue
        _apply_recent_session(
            state,
            key,
            slot_name,
            s,
            meta,
            messages,
            conv_log=conv_log,
            kiro_model_map=kiro_model_map,
            restore_cfg=_restore_cfg,
            member_identity=_member_id,
            agent=agent,
            effort_marker=effort_marker,
        )
        restored += 1
        # Recover an app flag whose claim outlived its row, as the open-slots
        # driver does. This driver's reads are inline by construction.
        _recovered_slot = state._slots.get(slot_name)
        if _recovered_slot is not None:
            _recover_mcp_app_claims(_recovered_slot)
        # One yield point per restored session (see _restore_open_slots_steps).
        yield restored
    _sync_dashboard_slots(state)


def restore_recent_sessions(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """Restore sessions as chat slots.

    Blocking: see :func:`restore_recent_sessions_async` for the startup path.
    """
    restored = 0
    for restored in _restore_recent_sessions_steps(
        state, window_minutes, folders_only=folders_only
    ):
        pass
    return restored


async def restore_recent_sessions_async(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """:func:`restore_recent_sessions`, with the disk reads off the loop.

    Same rationale as :func:`restore_open_slots_async` — keeps the stall-watchdog
    heartbeat alive while a large restore proceeds, and holds
    ``restoring_open_slots`` so an interleaved flush cannot snapshot a partial
    slot set (this path adds slots to the same sidebar).

    Everything blocking is hoisted into ``asyncio.to_thread``: ``list_sessions()``
    (which globs + stats + reads the first line of EVERY session file), the
    agent→model map, the config load, and each candidate's metadata + chained
    transcript walk. Only :func:`_apply_recent_session` stays on the loop,
    because slot construction is loop-affine for the reasons
    :func:`rehydrate_slot_from_history_async` documents.

    Yielding per session is kept alongside the offload: the apply half still runs
    here, so the loop must get a turn between sessions.
    """
    if not state.conversation_log:
        return 0
    restored = 0
    state.restoring_open_slots = True
    try:
        conv_log = state.conversation_log
        cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
        sessions = await asyncio.to_thread(conv_log.list_sessions)
        kiro_model_map = await asyncio.to_thread(_build_kiro_model_map)
        _restore_cfg = await asyncio.to_thread(_load_restore_cfg)
        for s in sessions:
            key = s.get("key", "")
            slot_name = _recent_session_slot_name(key)
            if slot_name is None or slot_name in state._slots:
                continue
            started = time.time()
            meta, messages, _member_id, agent, effort_marker = await asyncio.to_thread(
                _prefetch_recent_session,
                conv_log,
                key,
                s,
                folders_only=folders_only,
                cutoff=cutoff,
            )
            if meta is None or messages is None:
                continue
            # POST-HOP REVALIDATION. Before the reads were offloaded, each item's
            # check-then-apply ran atomically on the loop — nothing could get
            # between them. The read window is now seconds wide, so the pre-hop
            # answers are stale and BOTH must be asked again here, on the loop,
            # before anything mutates slot state:
            #
            #   * the slot may now EXIST (a resume, a nudge, or the user opening
            #     the tab published it while the transcript loaded).
            #     ``_apply_recent_session`` calls ``get_or_create_slot``, which
            #     returns that live slot, and the replay below would then append
            #     500 on-disk messages onto state that already has them and
            #     persist the duplicates.
            #   * the tab may have been CLOSED with ✕. The close pops the slot and
            #     records a tombstone synchronously, but persists the ``closed``
            #     flag only after its own awaits — so the metadata just read still
            #     says open, and rebuilding from it would resurrect a dismissed
            #     tab and then fire a nudge turn into it. The tombstone is the
            #     authoritative signal in that window.
            #
            # ``rehydrate_slot_from_history_async`` guards the same two windows
            # after its own hop, and the open-tab driver inherits the ``_slots``
            # half from ``_rehydrate_slot_from_history``'s internal re-check. This
            # is the one converted surface that has to spell both out.
            if slot_name in state._slots:
                logger.debug(
                    "Restore skipped: session %s was published while its " "transcript loaded",
                    slot_name,
                )
                continue
            if slot_closed_since(state, slot_name, started):
                logger.info(
                    "Restore abandoned: session %s was closed while its " "transcript loaded",
                    slot_name,
                )
                continue
            # Third window: the session may have been permanently DELETED (or
            # deleted and recreated) during the read. Synchronous and last, so no
            # await separates it from the build it gates.
            gone = _deletion_during_read(conv_log, key, meta, messages)
            if gone is not None:
                logger.info(
                    "Restore abandoned: session %s was %s while its transcript "
                    "loaded; refusing to restore a slot whose flush would "
                    "rewrite it",
                    slot_name,
                    gone,
                )
                continue
            _apply_recent_session(
                state,
                key,
                slot_name,
                s,
                meta,
                messages,
                conv_log=conv_log,
                kiro_model_map=kiro_model_map,
                restore_cfg=_restore_cfg,
                member_identity=_member_id,
                agent=agent,
                effort_marker=effort_marker,
            )
            restored += 1
            # Same recovery, with the spool read awaited: this driver is
            # loop-affine, so a scan here would stall the gateway.
            _recovered_slot = state._slots.get(slot_name)
            if _recovered_slot is not None:
                await _recover_mcp_app_claims_async(_recovered_slot)
            await asyncio.sleep(0)
        _sync_dashboard_slots(state)
    finally:
        state.restoring_open_slots = False
    return restored


# Memoisation for :func:`_build_message_entry`. ``_save_slot_to_history``
# re-serializes the WHOLE in-memory window on every flush (see the comment inside
# the uncached builder), so each save re-runs redaction over every message in the
# window -- including the overwhelming majority that have not changed since the
# previous flush. Redaction is the expensive part: two passes over the content,
# the same two passes again over EACH variant, plus a meta pass.
#
# The key is a content hash of the WHOLE message rather than an identity or a
# field subset, which is what makes invalidation automatic and total: the slot
# mutates messages in place (a stop event resolving, a file-change chip landing,
# a banner completing), and any such edit changes the digest, so the next call
# misses and recomputes instead of serving a stale entry. There is deliberately
# no explicit invalidation hook to forget.
#
# Bound sizing: a save re-serializes one slot's entire window, so the live
# working set is roughly ``active_slots x window_size`` and the failure mode past
# the bound is a cliff rather than a slope -- each save walks its window in
# order, so with several slots taking turns the LRU evicts each window just
# before its next save and the hit rate collapses to zero instead of degrading.
# The default entry bound holds several concurrent slot windows; because the
# right size is host-dependent (a gateway with many active slots overflows the
# entry bound while the byte bound still has headroom), both the entry bound and
# the byte ceiling are configurable
# (``dashboard.chat_entry_cache_max_entries`` / ``chat_entry_cache_max_bytes``).
#
# The flush site skips the cache for a window longer than the entry bound, which
# closes that cliff for ONE oversized window and nothing more. Several slots
# whose COMBINED windows exceed the bound each stay under it individually, so
# they take the cached path and hit the same zero-hit cliff unguarded. Detecting
# that needs a live view across slots, which no single save has; the mitigation
# is the configurable entry bound above -- an operator whose host shows the
# multi-slot cliff raises it -- while the cost of the residual case is only the
# key derivation on a miss.
#
# The entry count alone does NOT bound memory, because an entry is as large as
# its message: a cache full of megabyte-sized messages would retain gigabytes.
# That retention outlives the slot, since the entry holds the SAME content string
# object as the message rather than a copy, so a closed slot's window can be
# freed while the cache keeps its content alive. Hence two further bounds: a
# per-entry ceiling above which an entry is computed but never stored (so one
# huge message cannot evict the whole cache), and a total-byte ceiling evicted
# alongside the entry count. Worst-case retention is the lesser of
# ``max_entries x _ENTRY_MAX_CACHEABLE_BYTES`` and the configured byte ceiling.
#
# Size is measured as the length of the key payload, which the front door has
# already built for hashing, so it costs nothing extra. It measures the input
# rather than the built entry, but the entry is derived from it and the two track
# each other within a small factor -- accurate enough for a memory ceiling.
#
# Two properties work in our favour: entries are content-keyed, so two slots
# holding identical message content share one entry; and a cached ``None`` (a
# transient role) is a legitimate value, so membership -- not truthiness -- is
# what distinguishes a hit from a miss.
#
# ``_ENTRY_MAX_CACHEABLE_BYTES`` stays a module constant: it guards against ONE
# huge message evicting the whole cache, a shape that does not vary by host the
# way the working-set bounds do.
_ENTRY_MAX_CACHEABLE_BYTES = 256 * 1024
_entry_cache_lock = threading.Lock()
_entry_cache: OrderedDict[str, tuple[dict | None, int]] = OrderedDict()
_entry_cache_bytes = 0

# Lazily resolved ``(max_entries, max_bytes)`` for the entry cache. Resolved
# once and then served from this module global: the builder runs on every
# message of every flush, so it must not stat or parse ``config.json`` per call,
# and the memo keeps the hot path free of config I/O the way the loader's push
# pattern does for the event loop. The memo is INVALIDATED on a config write
# (see ``_on_config_change``), so a changed bound takes effect on the next flush
# rather than on the next gateway restart. ``None`` means "not resolved yet";
# tests reset it via the autouse cache-isolation fixture in ``test/conftest.py``.
_entry_cache_bounds_cached: tuple[int, int] | None = None
_entry_cache_bounds_read_warned = False

#: The registered bounds applier, kept so ``watch_config`` stays idempotent.
_entry_cache_config_sub: object = None


def _on_config_change(change: object) -> None:
    """Drop the memoised entry-cache bounds so the next read re-resolves them.

    Invalidation rather than a push, because the memo is read on the flush hot
    path and a config write is rare: clearing it costs one assignment and the
    next flush pays a single fingerprint-cached load. The read-failure warning
    latch is cleared with it, so a bound that starts working again warns once
    more instead of staying silent about a NEW failure.
    """
    del change  # either bound changing invalidates the same pair
    global _entry_cache_bounds_cached, _entry_cache_bounds_read_warned
    _entry_cache_bounds_cached = None
    _entry_cache_bounds_read_warned = False


def watch_config() -> None:
    """Register the entry-cache bounds invalidator on the process config watcher."""
    global _entry_cache_config_sub
    if _entry_cache_config_sub is not None:
        return
    from kiro_crew.config import live

    _entry_cache_config_sub = live.subscribe(
        "dashboard.chat_entry_cache_max_entries",
        "dashboard.chat_entry_cache_max_bytes",
        callback=_on_config_change,
        name="chat-entry-cache-bounds",
    )


def _entry_cache_bounds() -> tuple[int, int]:
    """Configured ``(max_entries, max_bytes)`` bounds for the entry cache.

    Reads the validated config once (loader-clamped to the documented ranges)
    and memoises the pair until a config write invalidates it (see
    ``_on_config_change``). Falls back to the built-in defaults when the loaded
    values are not real integers (a stubbed config
    object would otherwise flow a non-numeric value into the eviction
    comparison) -- that shape does not change without a config write, so it
    memoises like any other resolved pair. A config
    read that RAISES falls back to the defaults for this call WITHOUT
    latching, so a transient failure retries on the next call instead of
    discarding an operator's setting for the process lifetime; ``load()``
    degrades to defaults internally rather than raising, so a persistently
    raising read is not a realistic hot-path cost. The memo is written only
    after a successful read, which also makes concurrent first calls resolve
    toward the config value: two successful readers store the same pair, and a
    failing reader stores nothing.
    """
    global _entry_cache_bounds_cached, _entry_cache_bounds_read_warned
    bounds = _entry_cache_bounds_cached
    if bounds is None:
        bounds = (CHAT_ENTRY_CACHE_ENTRIES_DEFAULT, CHAT_ENTRY_CACHE_BYTES_DEFAULT)
        try:
            dashboard = KiroCrewConfig.load().dashboard
            max_entries = dashboard.chat_entry_cache_max_entries
            max_bytes = dashboard.chat_entry_cache_max_bytes
            if (
                isinstance(max_entries, int)
                and not isinstance(max_entries, bool)
                and isinstance(max_bytes, int)
                and not isinstance(max_bytes, bool)
            ):
                bounds = (max_entries, max_bytes)
        except Exception:
            # Log once per process: silently discarding a configured bound
            # reproduces the exact symptom (a thrashing cache) the config
            # exists to fix, with nothing to diagnose from.
            if not _entry_cache_bounds_read_warned:
                _entry_cache_bounds_read_warned = True
                logger.warning(
                    "chat entry-cache bounds config read failed; using defaults "
                    "until a read succeeds",
                    exc_info=True,
                )
            return bounds
        _entry_cache_bounds_cached = bounds
    return bounds


def _build_message_entry(m: dict, *, attachments: tuple[Path, str] | None = None) -> dict | None:
    """Memoised front door to :func:`_build_message_entry_uncached`.

    The cached value is the POST-redaction entry, never the raw input, so a hit
    can never hand a caller unredacted bytes -- that property is the one whose
    failure would be a security regression rather than a missed optimisation.

    The returned dict is the cached object itself, not a copy: every current
    caller treats the entry as read-only (it is serialized with ``json.dumps``
    and read for ordering keys), and copying on every hit would give back the
    cost the cache exists to avoid. A future caller that mutates an entry in
    place would need to copy first.
    """
    global _entry_cache_bytes
    try:
        payload = json.dumps(m, sort_keys=True, default=str)
    except Exception:
        # An unserializable message must still persist; fall back to computing it.
        return _build_message_entry_uncached(m, attachments=attachments)
    key = hashlib.sha256(payload.encode()).hexdigest()
    # *attachments* is part of the cache KEY, not just of the computation: the
    # entry it produces names a path inside ONE session's attachment directory,
    # so serving it to another session would point that session's transcript at
    # a file its own delete will never reclaim. Folded in AFTER the digest rather
    # than into its input, so the message payload keeps exactly one hashing site.
    key = f"{attachments}\x00{key}"
    size = len(payload)
    with _entry_cache_lock:
        if key in _entry_cache:
            _entry_cache.move_to_end(key)
            return _entry_cache[key][0]
    entry = _build_message_entry_uncached(m, attachments=attachments)
    if size > _ENTRY_MAX_CACHEABLE_BYTES:
        return entry
    # Refuse to STORE a pairing whose key and entry may describe different states.
    # The flush thread shares message dicts with the event loop, so a variant
    # switch landing between the two reads above would file the new entry under
    # the old state's key; because a switch restores content AND ts from the
    # stored variant, switching back reproduces that key exactly and would serve
    # the wrong variant. Re-reading m here costs one dump on a miss only.
    try:
        if json.dumps(m, sort_keys=True, default=str) != payload:
            return entry
    except Exception:
        return entry
    # Resolve the configured bounds BEFORE taking the cache lock: the first call
    # in the process reads config from disk, and that read must not run under a
    # lock the flush path contends on.
    max_entries, max_bytes = _entry_cache_bounds()
    with _entry_cache_lock:
        previous = _entry_cache.pop(key, None)
        if previous is not None:
            _entry_cache_bytes -= previous[1]
        _entry_cache[key] = (entry, size)
        _entry_cache_bytes += size
        while _entry_cache and (len(_entry_cache) > max_entries or _entry_cache_bytes > max_bytes):
            _, (_evicted_entry, evicted_size) = _entry_cache.popitem(last=False)
            _entry_cache_bytes -= evicted_size
    return entry


# Transient/streaming roles that are never persisted (mirrors
# ``_build_message_entry``). A window-region disk line carrying one of these is
# not a real message and is never treated as a cross-process append to preserve.
# Canonically defined in ``state`` (imported above) so the trim path that must
# count durable rows shares the same set; re-exported here for this module's
# other readers (session_control, chat_handlers).


@contextlib.contextmanager
def _truncation_claim_scope(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    rewrite: bool,
    expected_slot_name: str | None,
    takeover_basis: TakeoverBasis | None,
) -> Iterator[TruncationClaim | None]:
    """Hold a truncating save's publication-ordering claim across its write.

    The claim (see :class:`~kiro_crew.dashboard.slot_registry.TruncationClaim`)
    is the contract that orders this save's file replace against same-key slot
    publication: registered here, marked by every same-key takeover, and
    decided atomically at the commit gate immediately before ``atomic_write``.
    Entered BEFORE the per-session lock, so a takeover that lands while this
    save is still queued behind another writer wins too — the takeover's own
    read serializes behind the same lock, so whichever of the two acquires it
    first, the claim decision (not lock order) settles who the transcript
    belongs to.

    ``takeover_basis`` extends the coverage backwards to the caller's dispatch:
    a takeover firing before this registration has no claim to mark, so the
    registration compares the key's open takeover watch against the snapshot
    the dispatcher took at its last synchronous instant, and a moved count
    means the claim is born already overtaken. The dispatcher that opened the
    basis closes it; this scope only reads it.

    Registered only for truncating writes: an append-shaped save is a superset
    of the file, so a replacement reading around it can lose nothing. Keyed by
    the caller-checked map key when one was supplied, else by the slot's own
    key — a rewrite retried from ``_pending_rewrite`` carries no
    ``expected_slot_name``, and its window is exactly as stale to a takeover
    as a guarded caller's.
    """
    if not rewrite:
        yield None
        return
    name = expected_slot_name if expected_slot_name is not None else slot.key
    claim = SlotRegistry.begin_truncation_claim(state, name, basis=takeover_basis)
    try:
        yield claim
    finally:
        SlotRegistry.retire_truncation_claim(state, name, claim)


def _save_slot_to_history(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    closed_at: float | None = None,
    force: bool = False,
    rewrite: bool = False,
    expected_history_key: str | None = None,
    expected_disk_older_count: int | None = None,
    expected_slot_name: str | None = None,
    rows_only: bool = False,
    pending_mode_slot: _ChatSlot | None = None,
    mutes_opened_override: bool | None = None,
    after_commit_under_lock: Callable[[], None] | None = None,
    refuse_stale_empty_merge: bool = False,
    takeover_basis: TakeoverBasis | None = None,
) -> bool:
    """Persist slot messages to JSONL history (append-safe).

    ``refuse_stale_empty_merge``: let the empty-window merge of a message-less
    slot refuse a stale queue snapshot or a replaced slot, as the full save
    does. Only the periodic flush passes it, because it re-runs a refused pass
    on its next tick. The forced route saves (tag, folder, pin, transfer) keep
    committing their merge unconditionally, so none of them can newly report an
    acknowledged edit as refused.

    The session file is modeled as **frozen prefix + live window**:

    - The **frozen prefix** is the first ``slot._disk_older_count`` on-disk
      message lines — the turns OLDER than the in-memory window (set at
      restore/resume). These bytes are read verbatim and NEVER rewritten, so a
      restart that loaded only a recent window can no longer destroy older
      history.
    - The **live window** is ``slot.messages`` (small, ~500 messages). It is
      re-serialized in full on every save. Re-serializing the whole window means
      in-place edits to already-shown messages (stop-event resolution, file-change
      chips, mcp_oauth banner completion) and any reordering done by
      ``_flush_segment`` all persist correctly — there is no position counter to
      get out of sync.

    The default save writes ``meta + frozen_prefix + serialize(window)``.

    Pass ``rewrite=True`` (or an explicit *messages* snapshot, which implies it)
    for operations that INTENTIONALLY truncate the window (rewind/regenerate/
    fork): the file is rebuilt as ``meta + frozen_prefix + serialize(snapshot)``
    and the dropped window tail is archived first via ``_archive_dropped_lines``.

    Concurrency: ``_flush_dirty_slots`` runs this in an executor thread
    while ``_run_chat`` mutates ``slot.messages`` on the event loop. We snapshot
    ``list(slot.messages)`` (a single GIL-atomic attribute read) and the matching
    ``slot._disk_older_count`` up front, then operate only on that snapshot, so a
    concurrent ``_flush_segment`` reassigning ``slot.messages`` cannot interleave
    with the read-serialize-write and skip/duplicate a message.

    Operates ONLY on this slot's own single session file (``_path(history_key)``);
    tab_id chaining is 1:1 (a slot's tab_id maps to exactly one file — fork makes
    a fresh slot with its own file), so this never reads/writes a sibling and
    legacy no-tab_id sessions stay isolated.

    ``rows_only``: persist the window but leave the metadata line's slot-owned
    fields as they are on disk, keeping authority over only
    :data:`~kiro_crew.history.ROWS_ONLY_OWNED_META_KEYS`. It exists for the one
    caller whose slot is not the transcript's only writer: the close/cleanup
    hand-over drain, which writes a popped slot's unsaved rows onto a transcript a
    concurrent same-key replacement now holds. The default rebuild would revert
    whatever that replacement had already published (a folder or a pinned title
    from ``POST /api/chat/slots``, a tag, a pin), so the rows move and the line does
    not. The deferred set is
    :data:`~kiro_crew.history.ROWS_ONLY_DEFERRED_META_KEYS`, which is wider than the
    owned fields alone: a title's provenance and refresh budget describe the title
    and travel with it. It includes ``closed``/``closed_at``, so an open-shaped
    rows-only write does not erase a dismissal the replacement committed while this
    one was in flight.

    The deferral is conditional on there being another writer to defer to, decided
    from the line's ``tab_id``: a line this slot published itself, or no line at
    all, gets the ordinary rebuild. Otherwise the flag would cost the popped slot
    its own uncommitted metadata — an edit is acknowledged when it lands in memory
    and persists on a later flush, and after the pop no flush ever visits that slot
    again.

    ``expected_disk_older_count`` pairs an explicit *messages* snapshot with the
    ``slot._disk_older_count`` the caller observed in the SAME synchronous stretch
    it froze that snapshot in. A snapshot is internally consistent by
    construction, but the frozen-prefix boundary it must be written against is
    not: a concurrent ``append`` at the window cap trims the front and credits
    the trimmed rows to ``_disk_older_count``, so a snapshot frozen before that
    trim, written against the counter read after it, emits those rows twice —
    once in the frozen prefix and once at the head of the snapshot. Supplying the
    paired count makes that drift refuse the save (``False``, nothing written)
    instead of committing a duplicated transcript; a caller that reads the live
    counter itself cannot detect the drift at all. Ignored without *messages*,
    where the bounded retry below already takes both halves together.

    ``takeover_basis`` anchors a truncating write's publication-ordering claim
    to the caller's dispatch instant: a takeover watch opened while the caller
    could still vouch that this slot was the live occupant of its key. A
    takeover firing between that instant and the claim's registration has no
    claim to mark, so the registration compares the watch's count against the
    basis snapshot and a moved value makes the claim be born overtaken — the
    commit gate then refuses exactly as it does for a takeover that marks the
    claim mid-write. The dispatcher that opened the basis closes it once this
    save's outcome is in hand. ``None`` means the claim observes takeovers
    only from its registration on. Ignored for non-truncating saves.

    Returns ``False`` when the delete-won guard aborted the save because the
    session was permanently deleted while this save awaited the lock, when
    the slot's routing moved off ``expected_history_key``, when
    ``expected_disk_older_count`` drifted, or when a truncating write lost its
    publication-ordering claim because a same-name takeover began before the
    commit gate — the in-memory window was NOT persisted and must not be
    treated as durable. Every other completion (including the benign no-op
    skips) returns ``True``.
    """
    if not state.conversation_log:
        return True
    pending_mode_target = pending_mode_slot or slot
    # An explicit message snapshot always means "this is the full authoritative
    # window state" → rewrite. Edit paths (rewind/regenerate/fork) pass a snapshot.
    # A slot left in _pending_rewrite by a failed inline rewrite also takes
    # the archive-safe rewrite path until it succeeds.
    if messages is not None or slot._pending_rewrite:
        rewrite = True
    # The committed-queue witness as it stands BEFORE this save reads the queue,
    # so the guard inside the lock can tell "another writer committed since" from
    # "the queue moved since". Taken first, which is the conservative order: a
    # writer that commits between here and the read costs a refused pass, never a
    # committed value this save could not prove.
    queue_write_basis = slot._queue_persisted_sig
    snapshot = _write_guards.paired_window_snapshot(slot, messages, expected_disk_older_count)
    if snapshot is None:
        return False
    window, queue_snapshot, queue_candidates, disk_older = snapshot
    note_auth_key, history_key = _write_guards.routing_snapshot(slot)
    if expected_history_key is not None and history_key != expected_history_key:
        # The caller authorized a write against a specific transcript and the
        # slot's routing moved before this snapshot (a rebind on the event
        # loop wins any race with this worker). Writing would land the
        # caller's mutation on a transcript it never authorized -- refuse the
        # whole save instead, exactly like the delete-won guard: return False
        # with nothing written, and let the caller roll back and re-decide.
        logger.warning(
            "Slot %s save refused: routing moved from %s to %s during the write",
            slot.key,
            expected_history_key,
            history_key,
        )
        return False
    window = _write_guards.drop_notes_authorized_elsewhere(slot, window, note_auth_key)

    # The two retryable refusals, evaluated under the transcript lock by both the
    # empty-window merge and the full save below. Each returns the reason to log,
    # or ``None`` to proceed.
    def _queue_line_is_ours(meta: dict) -> bool:
        # A rows-only write over another holder's line defers the queue key to
        # that line, so it neither decides nor commits the queue.
        return not (rows_only and meta and not _line_is_this_slots(slot, meta))

    def _stale_queue_refusal(meta: dict) -> str | None:
        # The lock orders the queue writers' commits, not their reads, so a
        # writer holding an older queue can arrive here second.
        if _queue_line_is_ours(meta) and _queue_snapshot_is_stale(slot, queue_write_basis):
            return (
                "another writer committed a newer queued-prompt value "
                "while this save held an older snapshot"
            )
        return None

    def _replaced_refusal() -> str | None:
        if expected_slot_name is not None and state._slots.get(expected_slot_name) is not slot:
            return f"slot {expected_slot_name} was replaced before the write committed"
        return None

    if not window:
        if force or closed:

            def _refusal_under_lock(meta: dict) -> str | None:
                return _stale_queue_refusal(meta) or _replaced_refusal()

            refusal = _metadata_line.merge_empty_window(
                state.conversation_log,
                slot,
                history_key,
                live_session=note_auth_key,
                queue_snapshot=queue_snapshot,
                closed=closed,
                closed_at=closed_at,
                pending_mode_target=pending_mode_target,
                # Only the periodic flush opts in. A close commits
                # unconditionally, as it does on main: its callers ignore a
                # refused save, so a refusal would leave an open-shaped line
                # that a restart resurrects.
                refusal_under_lock=(
                    _refusal_under_lock if refuse_stale_empty_merge and not closed else None
                ),
                # A route save (tag, folder, pin, transfer) is not refused, but
                # an older queue snapshot must not overwrite a newer committed
                # queue either: the route's edit commits and the queue key is
                # left as it is on disk, still owed to the next save.
                queue_deferred_under_lock=(
                    (lambda meta: _stale_queue_refusal(meta) is not None)
                    if not refuse_stale_empty_merge and not closed
                    else None
                ),
                mutes_opened_override=mutes_opened_override,
                after_commit_under_lock=after_commit_under_lock,
            )
            if refusal is not None:
                logger.warning("Slot %s empty-window save refused: %s", slot.key, refusal)
                _keep_owed_after_refusal(slot)
                return False
        return True
    # Skip a pure no-op: a freshly resumed slot with no new AND no edited
    # messages. ``slot._dirty`` is set by both append and in-place edits
    # (update_message / _resolve_stop_event / file-change + mcp_oauth patches),
    # so a dirty slot whose length merely equals the resumed count still falls
    # through and re-serializes the window — otherwise an in-place edit after
    # resume would never reach disk. closed/force/rewrite always proceed.
    if (
        slot._resumed_count > 0
        and len(window) <= slot._resumed_count
        and not slot._dirty
        # A queued prompt lives on the metadata line, so a slot whose window has
        # not grown since resume can still owe one. Skipping here would leave
        # that prompt with no durable copy for as long as the slot stays quiet.
        and not slot.queue_persist_pending
        and not closed
        and not force
        and not rewrite
    ):
        return True
    try:
        # Hold the SAME per-session cross-process lock that ``append`` /
        # ``append_off_loop`` / rotate / rewrite / metadata mutations take, across
        # the whole read-modify-atomic_write below (metadata read, frozen-prefix
        # read, archive-diff read, and the file-replacing ``atomic_write``).
        # Without it, a concurrent ``append_off_loop`` (e.g. a workflow/cron
        # result appended to the originating dashboard session) can land between
        # this save's snapshot of the file and its ``atomic_write`` — the save
        # then replaces the file with meta+frozen+window and silently deletes the
        # acknowledged append. ``_locked`` serializes the two so neither is lost.
        # On the event loop ``_locked`` makes ONE non-blocking acquire and raises
        # ``HistoryLockTimeout`` under contention (never blocking the loop); the
        # ``save_slot_off_loop`` helper routes on-loop callers to a worker thread
        # so they take the patient acquire path instead of dropping the save.
        #
        # The claim scope opens BEFORE the lock and closes after it: the lock
        # serializes writers on the transcript, while the claim orders this
        # truncating write against same-key slot publication, which never takes
        # the lock (the cleanup pops ``state._slots[name]`` and the facade
        # republishes through ``SlotRegistry.put_slot`` on the event loop). The
        # decision the claim carries is consumed at the commit gate directly
        # above the payload composition, whose dropped-line archive is the
        # rewrite's first mutation.
        with (
            _truncation_claim_scope(
                state,
                slot,
                rewrite=rewrite,
                expected_slot_name=expected_slot_name,
                takeover_basis=takeover_basis,
            ) as _claim,
            state.conversation_log._locked(history_key),
        ):
            existing_meta, _meta_readable, _line_corrupt = _write_guards.read_line_for_save(
                state.conversation_log, slot, history_key
            )

            # ── Stale-queue guard ───────────────────────────────────────────
            # Refuse rather than put an older queue back: nothing is written, the
            # queue stays owed by the drift check, and the next pass re-decides
            # against the state that exists.
            refusal = _stale_queue_refusal(existing_meta)
            if refusal is not None:
                logger.warning("Slot %s save refused: %s", slot.key, refusal)
                _keep_owed_after_refusal(slot)
                return False
            queue_line_is_ours = _queue_line_is_ours(existing_meta)

            path = state.conversation_log._path(history_key)
            if _write_guards.delete_won(
                slot,
                path,
                existing_meta,
                meta_readable=_meta_readable,
                line_corrupt=_line_corrupt,
                history_key=history_key,
            ):
                return False
            # ── Recreate-won guard ──────────────────────────────────────────
            # Re-read the live occupant of the slot's map key INSIDE the lock,
            # after the patient off-loop acquire. A truncating caller checks
            # object identity before dispatching this write, but the executor
            # wait between that check and here frees the event loop, and a
            # same-name close-and-recreate is not serialized against the slot's
            # own lock (the cleanup pops ``state._slots[name]`` and
            # ``get_or_create_slot`` re-inserts, neither taking it). A recreate
            # that resumes the SAME transcript keeps ``history_key`` identical,
            # so the routing guard above waves it through. Confirming the map
            # still holds THIS slot object, at the commit boundary with no await
            # before the write, is what catches it: if the map now holds a
            # replacement the original slot is being torn down and its
            # truncation has no future, so refuse the whole save (``False``,
            # nothing written) rather than land the stale snapshot on the
            # replacement's transcript. This check covers takeovers that are
            # already PUBLISHED; a takeover still between its pop and its
            # publication — or between publication and its transcript read —
            # is what the truncation claim's commit gate below decides, since
            # no map re-read can observe a replacement that does not exist yet.
            refusal = _replaced_refusal()
            if refusal is not None:
                logger.warning("Slot %s save refused: %s", history_key, refusal)
                # A refusal is not a commit, and the periodic writer cannot tell
                # the difference: it clears ``_dirty`` on any return that did not
                # raise. Keeping the state owed is what makes this guard safe for
                # a caller whose edit lives only in memory -- the same treatment
                # the other retryable refusals in this function already apply.
                _keep_owed_after_refusal(slot)
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            meta_line, _mode, _durable_queue, retired_drop_ids, tab_id = (
                _metadata_line.build_full_line(
                    slot,
                    existing_meta,
                    live_session=note_auth_key,
                    closed=closed,
                    closed_at=closed_at,
                    window=window,
                    queue_snapshot=queue_snapshot,
                    queue_candidates=queue_candidates,
                    rewrite=rewrite,
                    rows_only=rows_only,
                    mutes_opened_override=mutes_opened_override,
                )
            )
            meta_str = json.dumps(meta_line) + "\n"

            # ── Commit gate ─────────────────────────────────────────────────
            # The single atomic decision that orders this truncating write
            # against same-key slot publication. The recreate-won guard above
            # re-reads the map at the commit boundary, but a same-name
            # close-and-recreate is not serialized against the per-session
            # lock, so the event loop can still take the key over between that
            # check and the replace below — and shrinking the gap cannot help,
            # because the replacement's harm is READING the file after the
            # replace, not being published before the check. The claim makes
            # the ordering total instead: every takeover (publication, resume
            # read start) marks the claim, and this one mutex-guarded
            # transition decides the winner. Losing here means a replacement
            # began first — its read, serialized behind the lock this save
            # holds, must find the transcript the truncation never touched, so
            # refuse the whole save with nothing written. Winning means any
            # later takeover observes ``committed`` and knowingly resumes the
            # post-rewrite transcript.
            #
            # The gate sits above EVERY mutation the rewrite performs — the
            # payload composition below archives the dropped tail, which is
            # the first one — so a refusal leaves no trace: an archive written
            # before a refusal would record still-live rows as dropped. Between
            # the decision and the file replace only this save runs (the
            # per-session lock is held throughout), so nothing can observe the
            # file inside that stretch, and a takeover arriving there reads
            # the post-rewrite transcript its ``committed`` answer promised.
            if _claim is not None and not SlotRegistry.commit_truncation_claim(state, _claim):
                logger.warning(
                    "Slot %s save refused: a same-name replacement took over key %s "
                    "before this truncating write committed",
                    history_key,
                    expected_slot_name if expected_slot_name is not None else slot.key,
                )
                # ── Append-safe fallback, under the lock the takeover reads
                # behind ─────────────────────────────────────────────────────
                # The refusal is right, but only for the TRUNCATION: a
                # ``_pending_rewrite`` retry riding a teardown save also
                # carries window rows appended AFTER the failed inline
                # rewrite — real conversation whose last writer is this frame
                # (the slot is out of the map, so no flush retries it). The
                # takeover's own transcript read serializes behind the
                # per-session lock THIS save still holds, so landing those
                # rows here — id-deduped appends, nothing rebuilt — is what
                # makes the replacement hydrate a window that already carries
                # them. Deferring the append past the release (the drain's
                # loop-side arm) lets the queued read acquire the lock first
                # and publish a window without the rows; a later truncating
                # rewrite by that replacement rebuilds the file without
                # collecting foreign appends (``collect_foreign=not rewrite``),
                # which drops the rows the drain appended too late.
                #
                # Scoped to the hand-over shape only: ``slot._pending_rewrite``
                # with no explicit snapshot. An edit path's snapshot
                # (``messages is not None``) is the truncation itself — its
                # regenerated rows presuppose the truncation the gate just
                # refused, so they lose with it (the recreate-won rule).
                # ``_locked`` is reentrant for the same key on this thread, so
                # the per-row locks inside ``append_if_absent`` reuse the lock
                # held here. Best-effort: a failed append leaves the refusal
                # as it was, and the drain's own arm retries it after release.
                #
                # The rows travel as the fully built persisted dicts this save
                # would have written (the ``entry`` passthrough), so the
                # appended rows carry the same ``ts``, provenance, ``variants``
                # and ``meta`` the committed payload would have; a (role,
                # content) reconstruction here would silently strip every one
                # of those fields from the only durable copy these rows get.
                # The attachment pair scopes the builder's inline-image rewrite
                # to this transcript, exactly as the composed payload does.
                if messages is None and slot._pending_rewrite and state.conversation_log:
                    try:
                        # The same replay the drain's own arm runs, under this
                        # lock: header settled first (an unconfirmed contract
                        # raises and appends nothing, the state staying owed
                        # below), rows built by the full save's builder,
                        # appended id-deduped against the whole durable history.
                        appended = _write_guards.append_window_rows(
                            state.conversation_log,
                            slot,
                            history_key,
                            window,
                            live_session=note_auth_key,
                        )
                        if appended:
                            logger.info(
                                "Slot %s: %d unsaved row(s) appended to %s under the "
                                "refused save's lock, ahead of the takeover's read",
                                slot.key,
                                appended,
                                history_key,
                            )
                    except Exception:
                        logger.warning(
                            "Slot %s: append-safe fallback failed under the refused "
                            "save's lock for %s; the hand-over drain retries it",
                            slot.key,
                            history_key,
                            exc_info=True,
                        )
                # A refusal is not a commit, and the periodic writer cannot tell
                # the difference: it clears ``_dirty`` on any return that did
                # not raise. The window this save did not write stays owed --
                # the same treatment the other retryable refusals above apply
                # -- so a flush that lost its claim leaves the slot dirty for
                # the next pass, and a hand-over drain whose fallback did not
                # land still holds the tail as unsaved.
                _keep_owed_after_refusal(slot)
                return False

            payload, frozen_prefix, foreign_lines = _transcript_merge.compose_payload(
                state, slot, path, history_key, window, disk_older, meta_str, rewrite=rewrite
            )

            _preserve_mtime: float | None = None
            if closed and (slot.linked_session_key or is_channel_session_key(history_key)):
                # This slot shares its transcript with a channel, and the
                # reconciler decides whether a close still stands by comparing
                # the file's mtime against ``closed_at``: activity newer than the
                # close means the conversation moved on and the tab comes back.
                # Writing the close flag IS a write, so it would advance mtime
                # past ``closed_at`` and make the close outrun itself — the tab
                # would reopen on the next pass. Restore the pre-close mtime so
                # only a genuine channel append can outrun the close.
                #
                # Gated on the TRANSCRIPT, not on ``linked_session_key``: an
                # UNBOUND channel tab (the session map could not resolve its
                # stem) writes this very same shared file, so testing the binding
                # left exactly that tab unprotected — its close bumped the
                # channel file's mtime and ``_close_stands`` then rejected the
                # close, resurfacing the tab on the next reconcile. Keeping the
                # ``linked_session_key`` arm makes this strictly additive for
                # cron- and workflow-linked slots, whose keys are not channel
                # keys but which also share a transcript.
                try:
                    _preserve_mtime = path.stat().st_mtime
                except OSError:
                    _preserve_mtime = None

            atomic_write(path, payload, fsync=True)
            # The staged value is now durable. Flip the live flag HERE, still
            # inside the transcript ``_locked`` block, so a concurrent dirty
            # flush -- which needs this same lock to serialize the slot -- cannot
            # observe the still-prior flag and overwrite the committed value.
            # This is the full-save twin of the empty-window merge's
            # ``after_commit_under_lock`` hook; one or the other runs, never both.
            if after_commit_under_lock is not None:
                after_commit_under_lock()
            _record_pending_memory_mode(pending_mode_target, _mode)
            # The write committed: the deferred-note drop records it retired
            # are now safe to consume (see ``build_full_line``). Discard is
            # idempotent, and a record consumed here can no longer be needed —
            # the retired entry left the durable hold in the same file replace.
            for retired_id in retired_drop_ids:
                slot._dropped_note_ids.discard(retired_id)
            if _preserve_mtime is not None:
                try:
                    os.utime(path, (_preserve_mtime, _preserve_mtime))
                except OSError:
                    # Best-effort: a failure only costs a resurfaced tab on the
                    # next pass, never data.
                    logger.debug(
                        "could not restore pre-close mtime for %s", history_key, exc_info=True
                    )
            # The witnesses below all describe THIS file. They live on the live
            # slot, so they may only be stamped while the slot still routes to
            # the transcript this save wrote. The event loop can rebind the slot
            # (a cron injection re-linking it) after the routing snapshot above
            # and while this worker writes: the write itself stays correct (it
            # lands on the authorized transcript), but stamping would then
            # describe the OLD file on a slot that now writes the NEW one —
            # clearing ``_pending_rewrite`` the new transcript still owes,
            # over-claiming ``_disk_window_len`` rows as persisted, and handing
            # the delete-won guard another file's identity. Skipping leaves every
            # witness at its pre-save value, which is the conservative side of
            # each one: the next save re-reads the prefix, re-takes the
            # archive-safe path, and re-observes the file. The cache
            # invalidations after this block are keyed on the file that WAS
            # written, so they stay unconditional.
            # Everything the stamping needs is computed BEFORE the routing
            # re-check, so the stamped region is assignments only: this runs in a
            # worker thread, and a syscall between the check and the last
            # assignment is the realistic point at which the event loop gets to
            # rebind the slot underneath a half-applied stamp. It cannot be made
            # atomic against the loop from here (``slot._lock`` is an asyncio lock
            # and no undo is right once the rebind path has recomputed these for
            # its own transcript) -- collapsing the five fields into one
            # assignable record carrying the key it describes is the real fix, and
            # belongs with that record rather than here.
            #
            # The frozen-prefix cache records the post-write mtime (even when
            # there is no frozen prefix, ``disk_older == 0``). It doubles as the
            # "did another process touch this file since we last wrote it?"
            # signal: a matching mtime on the next save proves THIS slot was the
            # last writer, so the frozen prefix is reusable and no NEW
            # cross-process append can have landed — letting the foreign-append
            # scan take the O(window) fast path instead of re-reading the whole
            # file. The foreign lines this save just preserved are cached
            # alongside so the fast path re-emits them verbatim: they now live in
            # the on-disk window region (after the frozen prefix), and because
            # ``disk_older`` is unchanged a bare frozen+window rebuild on the next
            # save would otherwise silently delete them.
            _post_write_cache: tuple[float, int, int, str, list[str]] | None
            try:
                _st = path.stat()
            except OSError:
                _post_write_cache = None
            else:
                _post_write_cache = (
                    _st.st_mtime,
                    _st.st_size,
                    disk_older,
                    frozen_prefix,
                    foreign_lines,
                )
            # The disk identity this save just wrote (carried forward from
            # ``existing_meta`` when present), so the delete-won guard can
            # recognize a file recreated by another writer after a permanent
            # delete on the NEXT save.
            _post_write_created_at = str(meta_line.get("created_at") or "")
            if slot_history_key(slot) == history_key:
                # A rewrite (archive-safe) save succeeded → clear the pending-rewrite
                # flag so later saves return to the cheap default path.
                if rewrite:
                    slot._pending_rewrite = False
                # How many window messages are now on disk, so memory trimming can
                # safely fold leading window messages into the frozen prefix.
                slot._disk_window_len = len(window)
                slot._disk_meta_created_at = _post_write_created_at
                # A committed save is a direct observation of the file this slot
                # writes — even when the carried-forward metadata is legacy and
                # has no ``created_at`` for the identity string above.
                slot._disk_meta_observed = True
                slot._frozen_prefix_cache = _post_write_cache
                if queue_line_is_ours:
                    # The queued prompts are now on disk, so the flush's drift
                    # check stops reporting them as owed until the queue moves
                    # again.
                    slot._queue_persisted_sig = queue_persist_signature(_durable_queue)
            else:
                logger.warning(
                    "Slot %s was rebound from %s while its save was in flight; "
                    "leaving the persistence witnesses at their pre-save values",
                    slot.key,
                    history_key,
                )
            state.conversation_log._invalidate_cache(history_key)
            state.conversation_log.note_tab_id(history_key, tab_id)
            return True
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


def _transfer_basis_to_worker(
    state: DashboardState,
    basis: TakeoverBasis | None,
    future: "asyncio.Future[bool] | None",
) -> None:
    """Hand a caller-opened basis to its still-running worker on cancellation.

    The shield in :func:`save_slot_off_loop` keeps the worker running through a
    caller's cancellation, but the CALLER's unwind reaches its own ``finally``
    close immediately — and the worker may still be pre-registration, so
    retiring the watch there leaves a takeover in that gap unrecorded and the
    orphaned truncation commits over the takeover's transcript. Transferring
    makes the caller's close a no-op and releases the holder from the worker
    future's settlement instead — the same worker-owns-the-close rule ``_do``
    already applies to the basis that function opens itself. A future already
    settled needs no transfer: the caller's close is then correctly ordered
    after the write.
    """
    if basis is None or future is None or future.done():
        return
    if not SlotRegistry.defer_basis_close(state, basis):
        return
    future.add_done_callback(lambda _f: SlotRegistry.close_transferred_basis(state, basis))


async def save_slot_off_loop(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    closed_at: float | None = None,
    force: bool = False,
    rewrite: bool = False,
    best_effort: bool = True,
    expected_history_key: str | None = None,
    expected_slot_name: str | None = None,
    rows_only: bool = False,
    issued_by_the_retraction: bool = False,
    mutes_opened_override: bool | None = None,
    after_commit_under_lock: Callable[[], None] | None = None,
    takeover_basis: TakeoverBasis | None = None,
) -> bool:
    """Persist a slot from the event loop without blocking or dropping the save.

    :func:`_save_slot_to_history` holds the per-session cross-process
    ``_locked`` across its read-modify-``atomic_write``. That lock, invoked on
    the gateway event loop, makes a single
    non-blocking acquire and raises :class:`~kiro_crew.history.HistoryLockTimeout`
    under any concurrent holder (e.g. a workflow/cron result appending via
    :func:`~kiro_crew.history.append_off_loop`) — so calling the save inline on
    the loop would both risk a disk write on the loop and drop the save under
    benign contention, or surface the timeout into the aiohttp handler.

    This helper mirrors :func:`~kiro_crew.history.append_off_loop`: on a running
    loop it dispatches the save to a worker thread so it takes the *patient*
    off-loop acquire path; off the loop it saves inline.

    ``best_effort`` (default ``True``): a lock timeout / I/O error is logged and
    the slot is marked ``_dirty`` so the periodic flush retries the write — the
    in-memory slot is the source of truth. This retry re-arm matters for the
    metadata mutation endpoints (pin / folder / tag / mode), which call this with
    ``force=True`` but do not otherwise mark the slot dirty: without it a
    swallowed failure would drop an acknowledged edit with no retry, losing it
    after a restart. Pass ``best_effort=False`` for archival paths (session
    close/cleanup) that must CONFIRM the durable write succeeded before removing
    the session: the save still runs off-loop (patient acquire), but any
    exception propagates so the caller can roll back and keep the slot.

    ``expected_history_key``: the transcript key the caller authorized its
    mutation against. The save refuses (returns ``False``, nothing written)
    when the slot's routing no longer resolves to that key at write time -- a
    rebind on the event loop can land between the caller's authorization and
    the worker's routing snapshot, and without this pin the durable write
    would target a transcript the caller never authorized.

    ``expected_slot_name``: the ``state._slots`` map key the caller checked its
    slot object against before dispatching. The save refuses (returns ``False``,
    nothing written) when the map holds a different slot object at the locked
    commit boundary -- a same-name close-and-recreate that resumes the
    same transcript keeps ``expected_history_key`` identical and slips past the
    routing pin, so this object-identity recheck under the lock stops the
    truncating snapshot from landing on the replacement's transcript. The map
    recheck only sees replacements already published; the window it cannot
    cover -- a takeover that begins after the recheck and reads the file after
    the replace -- is closed by the truncating write's publication-ordering
    claim, whose commit gate refuses the write when any same-key takeover
    (publication or resume read start) marked the claim first.

    ``rows_only``: write the window but leave the metadata line's slot-owned
    fields as they stand on disk when the line was published by ANOTHER slot --
    for a caller persisting a slot's rows onto a transcript another live slot now
    holds. See :func:`_save_slot_to_history` for the full contract, including the
    ``tab_id`` test that keeps the flag from deferring to the caller's own line.

    ``takeover_basis``: a caller-opened takeover watch, for dispatchers whose
    authorization instant precedes this call (the close/cleanup paths open one
    before they pop the slot, so a resume beginning during their teardown
    awaits is observed). The caller that opened it closes it; when it is
    absent, this helper opens its own for rewrite-shaped saves and its worker
    closes it once the save settles.

    Returns ``False`` only when the save was skipped WITHOUT writing: the
    session was permanently deleted while the save awaited the lock (the
    delete-won guard in :func:`_save_slot_to_history`), the routing moved
    off ``expected_history_key``, or a same-key takeover won the ordering
    (at the dispatch verify or at the commit gate). A guarded write is also
    skipped when the slot is already fenced for close, because the retraction
    of its name has passed the point where it can wait for this write --
    unless the retraction itself issued it (``issued_by_the_retraction``),
    which the handover drain does and nothing else does. Neither skip raises,
    for either ``best_effort`` mode, so a clean return does NOT prove a
    committed write. Callers that go on to republish the slot's content
    elsewhere (fork, the transfer export) must check the return. Archival
    callers (close/cleanup) may ignore a delete-won ``False`` -- the delete
    already disposed of what they were archiving -- but must not read a
    takeover refusal as success: they hold the basis they opened, and
    ``SlotRegistry.basis_moved`` is the discriminator.
    """

    # ── Dispatch verify ─────────────────────────────────────────────────
    # For callers that pin the map key, re-read the live occupant HERE, in the
    # caller's own synchronous tick. A takeover whose resume read is already in
    # flight fired its takeover note before this save could open a watch, so
    # neither the claim nor the basis below can observe it — but its pop is
    # observable, and a guarded caller's slot not being the live occupant at
    # dispatch means the key already changed hands. Refusing now is the same
    # recreate-won outcome the commit-boundary recheck delivers, one hop
    # earlier. Unguarded callers (the close/cleanup paths write popped slots
    # by design) are deliberately not checked.
    if expected_slot_name is not None and state._slots.get(expected_slot_name) is not slot:
        logger.warning(
            "Slot %s save refused at dispatch: slot %s is not the live occupant of its key",
            slot.key,
            expected_slot_name,
        )
        # The same refusal the in-lock recreate-won guard delivers, so it keeps
        # the state owed the same way: nothing was written, and the caller's
        # window must not read as persisted to the next flush pass.
        _keep_owed_after_refusal(slot)
        return False

    pending_mode_slot = slot
    current = state._slots.get(slot.key)
    if current is not None and slot_history_key(current) == slot_history_key(slot):
        pending_mode_slot = current

    # Opened at the last synchronous instant before the save leaves this tick,
    # for EVERY save the caller did not already open one for. The shape the
    # worker writes is decided there, not here: ``_save_slot_to_history`` reads
    # ``slot._pending_rewrite`` on the worker, so a save dispatched
    # append-shaped can become a truncating rewrite in the gap between this
    # tick and that read (a regenerate truncates the window and its inline
    # save fails, leaving the flag set), and a takeover landing in the same
    # gap would then be unobservable to a claim registered with no basis. A
    # basis is one dict read under the claim mutex; the claim scope consults
    # it only for a rewrite, so an append-shaped save pays the open and the
    # close and nothing else. An owned basis is closed by ``_do`` itself once
    # the save settles — closing it from this side on a cancelled await would
    # retire the watch while the executor thread is still working under it —
    # with the loop-side ``finally`` closing only when the worker provably
    # never ran (close is idempotent per basis, so the two sides cannot
    # double-release a holder).
    _owned_basis: TakeoverBasis | None = None
    _basis = takeover_basis
    if _basis is None:
        _owned_basis = SlotRegistry.open_takeover_basis(
            state, expected_slot_name if expected_slot_name is not None else slot.key
        )
        _basis = _owned_basis

    def _do() -> bool:
        try:
            return _save_slot_to_history(
                state,
                slot,
                messages,
                closed=closed,
                closed_at=closed_at,
                force=force,
                rewrite=rewrite,
                expected_history_key=expected_history_key,
                expected_slot_name=expected_slot_name,
                rows_only=rows_only,
                pending_mode_slot=pending_mode_slot,
                mutes_opened_override=mutes_opened_override,
                after_commit_under_lock=after_commit_under_lock,
                takeover_basis=_basis,
            )
        finally:
            SlotRegistry.close_takeover_basis(state, _owned_basis)

    def _begin_guarded_metadata_write() -> None:
        inflight = getattr(slot, "_metadata_persist_inflight", 0)
        # Production slots initialize this counter, while compatibility callers
        # may provide a mock that synthesizes missing attributes. Treat a
        # non-integer value as an absent counter rather than leaking it into the
        # write's cleanup path.
        slot._metadata_persist_inflight = inflight + 1 if type(inflight) is int else 1

    def _finish_guarded_metadata_write() -> None:
        inflight = getattr(slot, "_metadata_persist_inflight", 0)
        slot._metadata_persist_inflight = (
            inflight - 1 if type(inflight) is int and inflight > 0 else 0
        )

    def _register_guarded_write(save: "asyncio.Future[bool]") -> None:
        """Hold a guarded write's executor future until the WORKER completes.

        ``_finish_guarded_metadata_write`` runs in THIS coroutine's ``finally``,
        so a caller cancelled while the executor is running releases the count
        with the worker thread still on its way to the rename. The future does
        not share that fate: cancelling the awaiter either cancels the work
        before it starts, in which case nothing is written, or fails to cancel a
        thread already running and the future still completes when that thread
        returns. A retraction of this slot's name orders itself after that
        completion, which is a guarantee the count cannot give.
        """
        register_guarded_history_write(slot, save)

    # The executor future once one is submitted: ``_do`` owns the owned basis
    # close from that point on, and the outer ``finally`` reads this to tell a
    # dispatch that never submitted (nothing ran, so this side closes) from
    # one that did.
    _submitted_future: "asyncio.Future[bool] | None" = None

    async def _dispatch_to_worker(active_loop: asyncio.AbstractEventLoop) -> bool:
        if guarded_metadata and not issued_by_the_retraction and getattr(slot, "is_closing", False):
            # A retraction of this slot's name is already past its own wait for
            # the guarded writes registered below, so dispatching now would put a
            # worker thread on its way to the rename with nothing left to order
            # against it. The caller that reached here checked the same fence
            # before its own awaits, and those awaits are where the fence went
            # up; re-reading it HERE is what makes the pair decidable. There is
            # no suspension between this read and the registration below, so the
            # two possible interleavings are the only ones: fence first and this
            # write refuses, or registration first and the retraction waits.
            #
            # The fence asks whether this write races a retraction. A write the
            # retraction ITSELF issues does not: the handover drain runs inside
            # the close that raised the fence, is sequenced by it, and is the
            # last chance the original's unsaved rows have to reach disk.
            # ``issued_by_the_retraction`` is how that caller says so, and only
            # that caller passes it.
            #
            # Refusing writes nothing, which is the ``False`` contract this
            # function already documents for a write its own guards decline. The
            # close's archival save is unaffected for a second reason: it carries
            # no authorized transcript key, so it is not a guarded write.
            logger.warning(
                "Refusing a guarded history write for %s: the conversation is being closed",
                getattr(slot, "key", "?"),
            )
            # Refusing must not be silently FINAL. A metadata-only mutation
            # (recreate title, folder filing, tag, pin, mode) calls this with
            # ``force=True``, does not otherwise set ``_dirty``, and its callers
            # publish on the strength of the acknowledged edit without reading
            # this return. A close can raise the fence and then leave the slot
            # live -- it refuses on a breached wait, and the sweep raises and
            # releases the fence around a deferral -- so an edit landing in that
            # window would be dropped and the old value would come back after a
            # restart. Arming the flush is the same remedy this function's own
            # exception arms use below, and it converges once the fence is down.
            try:
                slot._dirty = True
            except Exception:  # noqa: BLE001 - a slot double may not accept it
                pass
            return False
        nonlocal _submitted_future
        save = active_loop.run_in_executor(None, _do)
        _submitted_future = save
        # ``run_in_executor`` returns an asyncio Future bound to ``active_loop``;
        # its callbacks therefore already run on that loop. Register before any
        # await so adoption follows the worker's completion even when the
        # awaiting task is cancelled, and runs before a normal awaiter resumes.
        save.add_done_callback(
            lambda _completed: apply_pending_slot_memory_mode(state, pending_mode_slot)
        )
        if guarded_metadata:
            _register_guarded_write(save)
        # Shield the FUTURE from this caller's cancellation. Cancelling an
        # asyncio future returned by ``run_in_executor`` succeeds whatever the
        # worker thread is doing -- the chained cancel of the underlying
        # concurrent future cannot stop a thread already running -- so an
        # unshielded await would resolve the registration above while the write
        # is still on its way to the rename, which is the exact blindness the
        # registration exists to remove. The thread runs on either way; shielding
        # only keeps it observable. Every executor save is shielded, not only
        # the registered ones: the worker owns the takeover-watch close, and an
        # authorized durable write completes rather than being silently dropped
        # from the queue. The cancellation still reaches the caller from the
        # shield itself.
        try:
            return await asyncio.shield(save)
        except asyncio.CancelledError:
            # The caller's unwind closes ITS basis now, while the shielded
            # worker may still be pre-registration under it — transfer the
            # close to the worker's settlement so the watch outlives every
            # takeover the running write must still observe.
            _transfer_basis_to_worker(state, takeover_basis, save)
            raise

    guarded_metadata = expected_history_key is not None
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            if best_effort:
                try:
                    return _do()
                except Exception:  # noqa: BLE001 - best-effort durable copy
                    # A swallowed failure must NOT be silently final: mark the slot
                    # dirty so the periodic flush retries the write. Metadata-only
                    # mutations (pin / folder / tag / mode) call this with
                    # ``force=True`` but do not otherwise set ``_dirty``; without this
                    # a lock timeout / I/O error would drop the change and the flush
                    # would never retry it, losing an acknowledged edit after restart.
                    slot._dirty = True
                    logger.warning(
                        "save_slot_off_loop: inline save failed slot=%s", slot.key, exc_info=True
                    )
                    return True
            return _do()
        if best_effort:
            if guarded_metadata:
                _begin_guarded_metadata_write()
            try:
                return await _dispatch_to_worker(loop)
            except Exception:  # noqa: BLE001 - best-effort durable copy
                # See the inline branch above: re-arm the periodic flush so a
                # swallowed metadata/message save is retried rather than lost.
                slot._dirty = True
                logger.warning(
                    "save_slot_off_loop: offloaded save failed slot=%s", slot.key, exc_info=True
                )
                return True
            finally:
                if guarded_metadata:
                    _finish_guarded_metadata_write()
        # Non-best-effort: propagate so the caller can roll back (do NOT remove the
        # session until the durable write is confirmed).
        if guarded_metadata:
            _begin_guarded_metadata_write()
        try:
            return await _dispatch_to_worker(loop)
        finally:
            if guarded_metadata:
                _finish_guarded_metadata_write()
    finally:
        # ``_do`` owns the close once it is submitted (the shield above keeps
        # the worker running through a caller's cancellation, so submission
        # means ``_do`` runs and its ``finally`` closes). This side releases
        # the holder only when the dispatch never submitted anything -- the
        # fence refusal, or a dispatch that raised before ``run_in_executor``;
        # the inline no-loop paths run ``_do`` synchronously, so their close
        # already happened and the idempotent close makes this a no-op there.
        if _submitted_future is None:
            SlotRegistry.close_takeover_basis(state, _owned_basis)


def _build_history_prefix(
    slot: _ChatSlot,
    *,
    conversation_log: ConversationLog | None = None,
    current_message: dict | None = None,
    model_window: int | None = None,
) -> str:
    """Legacy no-builder entry point; share the canonical merge and budget."""
    from kiro_crew.context import build_session_replay

    replay = build_session_replay(
        conversation_log,
        slot_history_key(slot),
        pending_messages=list(slot.messages),
        current_message=current_message,
        model_window=model_window,
    )
    if not replay:
        return ""
    return (
        "[Previous chat history for this tab — session was reset after stop]\n"
        + replay
        + "\n[End of history]\n\n"
    )


# ── Composed owners ─────────────────────────────────────────────────────────────
# Imported last, once every binding above exists, so loading them leaves the
# facade's own import order as it was: each owner imports only modules this file
# has already loaded, and reaches back into it only at call time. Every name
# below kept its import path here when it moved to its owner, and this file's own
# code calls them through these bindings. The owners read every name a test
# rebinds on this module through it at call time, so such a patch reaches them
# too (test_chat_persistence_composition_contract derives that set from the tests).
from kiro_crew.dashboard.slot_persistence import metadata_codec as _metadata_codec  # noqa: E402
from kiro_crew.dashboard.slot_persistence import metadata_line as _metadata_line  # noqa: E402
from kiro_crew.dashboard.slot_persistence import transcript_merge as _transcript_merge  # noqa: E402
from kiro_crew.dashboard.slot_persistence import write_guards as _write_guards  # noqa: E402
from kiro_crew.dashboard.slot_persistence.message_entries import (  # noqa: E402,F401
    _approx_window_payload_bytes,
    _attach_variants,
    _build_message_entry_uncached,
)
from kiro_crew.dashboard.slot_persistence.metadata_codec import (  # noqa: E402,F401
    _RETIRED_MODES,
    COLOR_HEX_RE,
    _rebase_rehydrated_refresh_mark,
    _rehydrate_slot_title,
    _rehydrate_title_low_signal,
    _rehydrate_title_origin,
    _rehydrate_title_refresh_mark,
    _restore_dismissed_source_links,
    _restore_model_fields,
    _restored_mode,
    _validate_autocompact_pct,
)
from kiro_crew.dashboard.slot_persistence.metadata_line import (  # noqa: E402,F401
    _META_LAST_USER_AT,
    _PENDING_MEMORY_MODE_LOCK,
    _capped_dismissed_line,
    _latest_stamp,
    _newest_human_turn_ts,
    _record_pending_memory_mode,
    _tighten_carried_execution,
    pending_slot_memory_mode,
    tighten_line_for_appends,
)
from kiro_crew.dashboard.slot_persistence.restore_inputs import (  # noqa: E402,F401
    _build_kiro_model_map,
    _deletion_during_read,
    _is_app_owned_channel_row,
    _load_restore_cfg,
    _read_mcp_app_claims,
    _read_open_slots_keys,
    _recent_session_slot_name,
    _reconcile_mcp_app_claims,
    _recover_mcp_app_claims,
    _restored_agent_name,
    _sanitize_open_slot_key,
)
from kiro_crew.dashboard.slot_persistence.transcript_merge import (  # noqa: E402,F401
    _archive_dropped_lines,
    _diff_dropped_message_lines,
    _foreign_tail_ts,
    _frozen_prefix_and_foreign_appends,
    _interleave_foreign_lines,
)
from kiro_crew.dashboard.slot_persistence.turn_marker import (  # noqa: E402,F401
    _LOCAL_TURN_PROMPT_MAX_ATTACHMENTS,
    _LOCAL_TURN_PROMPT_MAX_BYTES,
    _LOCAL_TURN_PROMPT_MAX_FIELD_CHARS,
    _LOCAL_TURN_PROMPT_META_KEYS,
    _LOCAL_TURN_PROMPT_ROLES,
    _RESTART_INTERRUPTION_KIND,
    _RESTART_INTERRUPTION_MSG,
    _latest_turn_was_deliberately_stopped,
    _local_turn_generation,
    _local_turn_prompt,
    _reconcile_local_turn_marker,
    _window_holds_row,
    local_turn_prompt_within_bounds,
)
from kiro_crew.dashboard.slot_persistence.write_guards import (  # noqa: E402,F401
    _FLUSH_SNAPSHOT_RETRIES,
    HeaderUnconfirmed,
    _keep_owed_after_refusal,
    _line_is_this_slots,
    _queue_snapshot_is_stale,
    _stable_durable_queue,
    append_window_rows,
    drop_notes_authorized_elsewhere,
    register_guarded_history_write,
    session_deletion_confirmed,
    session_transcript_remains,
    session_was_deleted,
)
