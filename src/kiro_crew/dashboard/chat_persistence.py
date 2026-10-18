"""Session persistence — save, restore, history prefix.

A dashboard slot's conversation lives in one session JSONL file: a metadata line
followed by the transcript rows. This module is the facade every caller imports
from, and the one namespace the save and restore code runs on: the orchestration
runs on its globals and reports it as its ``__module__``, so a patch of a name here
reaches that code and its ``global`` writes land here, while its text lives in the
parts of :mod:`kiro_crew.dashboard.chat_persistence_parts`, which
:func:`kiro_crew._facade_compose.compose` composes here:
* ``save_transaction``: the save transaction, ``_save_slot_to_history`` (snapshot,
  refusals, the transcript lock, the atomic replace and the witness stamping), with
  its on-loop entry point ``save_slot_off_loop`` and the close fence it applies;
* ``restore_drivers``: ``restore_open_slots``, ``restore_recent_sessions`` and their
  async twins, with the open-tab plan, budget and tail;
* ``slot_builders``: ``_rehydrate_slot_from_history``, ``_apply_recent_session``, the
  open-tab slot build, the deferred window load and the prefetch reads they share;
* ``member_store``: the private member-store assignment;
* this module itself: process-wide state with the code that mutates it (the
  reasoning-effort allowlist, and the persisted-entry memo ``_build_message_entry``
  with its bounds), the request-side retired-mode coercion and the history prefix.

The rules those consult are composed from :mod:`kiro_crew.dashboard.slot_persistence`:
``write_guards`` (the save's refusals and witnesses), ``metadata_codec`` (every
slot field of the metadata line, written and read back through one table),
``metadata_line`` (what a save folds against the line on disk before it encodes),
``transcript_merge`` (frozen prefix, foreign appends, payload), ``message_entries``
(row projection), ``restore_inputs`` (restore-time reads and screens) and
``turn_marker`` (the turn-in-flight marker). The slot builders construct and fill a
slot through ``metadata_codec``; the window replay and the rollback stay with them. Every
project name this module bound before the owners were composed is still importable
from here, and the names tests rebind on this module are read through it at call
time, so such a patch reaches the code that moved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque  # noqa: F401
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence  # noqa: F401
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
from kiro_crew.dashboard import display_verdicts as _display_verdicts
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
    SlotOrigin,
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
    HUMAN_TURN_META_KEY,
    METADATA_LINE_CORRUPT,
    ROWS_ONLY_DEFERRED_META_KEYS,
    ROWS_ONLY_OWNED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    ConversationLog,
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


# ── Composed parts ─────────────────────────────────────────────────────────────
# The save transaction, the restore drivers, the slot builders and the member-store
# assignment run on THIS module's globals (:func:`kiro_crew._facade_compose.compose`):
# a patch of a name here reaches them, their ``global`` writes land here, and each
# reports this module as its ``__module__``; see ``kiro_crew.dashboard.chat_persistence_parts``.
# They are composed here, before the rule owners below rather than at the very foot,
# because test_chat_persistence_composition_contract keeps those owner imports the last
# statements of this module. Only module-level statements run when a part loads, and none
# reads a rule owner.
from kiro_crew._facade_compose import compose as _facade_compose  # noqa: E402
from kiro_crew.dashboard import chat_persistence_parts as _parts  # noqa: E402
from kiro_crew.dashboard.chat_persistence_parts.member_store import (  # noqa: E402,F401
    _member_private_selection,
    _member_restore_identity,
    _pin_private_agent_assignment,
    member_store_ownership_holds,
    pin_private_agent_store,
    release_prewarmed_session,
)
from kiro_crew.dashboard.chat_persistence_parts.restore_drivers import (  # noqa: E402,F401
    _PERSON_ORIGINS,
    _TAIL_CLS_BOUND,
    _TAIL_META_KEYS,
    _TAIL_SCALAR_BOUND,
    EAGER_WINDOW_RESTORES,
    LAZY_TAIL_ROWS,
    TAIL_TEXT_BOUND,
    _bounded_text,
    _carry_unread_slot_keys,
    _display_tail,
    _kept_past_budget,
    _may_restore_as_row,
    _open_slot_restore_plan,
    _over_restore_budget,
    _prefetch_open_slot_inputs,
    _record_unrestored_open_tabs,
    _recover_mcp_app_claims_async,
    _report_restore_budget,
    _report_restore_with_no_seed,
    _restore_open_slots_steps,
    _restore_recent_sessions_steps,
    _short_scalar,
    _window_can_wait,
    restore_open_slots,
    restore_open_slots_async,
    restore_recent_sessions,
    restore_recent_sessions_async,
)
from kiro_crew.dashboard.chat_persistence_parts.save_transaction import (  # noqa: E402,F401
    _save_slot_to_history,
    save_all_slots_to_history,
    save_slot_off_loop,
    wait_for_unwinding_turns,
)
from kiro_crew.dashboard.chat_persistence_parts.slot_builders import (  # noqa: E402,F401
    _IDENTITY_UNRESOLVED,
    _apply_recent_session,
    _apply_restored_open_slot,
    _load_rehydrated_window,
    _PendingWindow,
    _prefetch_recent_session,
    _prefetch_rehydrate_inputs,
    _rehydrate_slot_from_history,
    rehydrate_slot_from_history_async,
)

_facade_compose(
    globals(),
    (
        _parts.save_transaction,
        _parts.restore_drivers,
        _parts.slot_builders,
        _parts.member_store,
    ),
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
    remember_unknown_row_fields,
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
    _validate_turn_timeout_secs,
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
)
from kiro_crew.dashboard.slot_persistence.restore_inputs import (  # noqa: E402,F401
    _build_kiro_model_map,
    _deletion_during_read,
    _is_app_owned_channel_row,
    _load_restore_cfg,
    _read_mcp_app_claims,
    _read_open_slots_keys,
    _read_open_slots_snapshot,
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
    _keep_owed_after_refusal,
    _line_is_this_slots,
    _queue_snapshot_is_stale,
    _stable_durable_queue,
    register_guarded_history_write,
    session_transcript_remains,
    session_was_deleted,
)
