"""Session persistence — save, restore, history prefix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator, Mapping
from itertools import islice

from kiro_crew import model_registry
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import agent_model_map
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
    KiroCrewConfig,
    config_dir,
)
from kiro_crew.dashboard.channel_slots import slot_closed_since
from kiro_crew.dashboard.chat_utils import (
    _normalize_model,
    _redact_meta_for_role,
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
    slot_transcript_key,
)
from kiro_crew.dashboard.state import (
    _TRANSIENT_ROLES,
    DashboardState,
    _ChatSlot,
    _normalize_slot_key,
    _note_authorized_elsewhere,
    durable_row_count,
    row_mid,
)
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.history import (
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
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import ARTIFACT_SLUG_RE

logger = logging.getLogger(__name__)

# Custom session color contract: lowercase-normalized #rrggbb only. Canonical
# home of the regex (chat_handlers imports it from here to avoid an import
# cycle). Every persistence read site below re-validates against it because
# the JSONL metadata line is attacker-writable and this string reaches every
# client's inline style.
COLOR_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Sentinel: the slot is a member key whose binding is missing/unreadable —
#: skip publishing it (the transcript stays on disk; the member-thread
#: endpoint re-creates and re-binds the slot on the next page open).
_SKIP_MEMBER_RESTORE: tuple[str, str] = ("", "__skip__")

#: Sentinel default for the ``member_identity`` parameters below: the caller
#: did not prefetch, resolve inline. Distinct from ``None`` (an ordinary,
#: non-member key) — defaulting to ``None`` would silently unpin every member
#: slot restored by a caller that forgot to prefetch.
_IDENTITY_UNRESOLVED: tuple[str, str] = ("", "__unresolved__")


# Recognized title-origin values (mirrors chat_title._TITLE_ORIGINS; duplicated
# here rather than imported to avoid dragging the chat_title import graph into
# the persistence module's load path).
_TITLE_ORIGINS = ("auto", "user")


def _rehydrate_title_origin(titled: bool, stored: object) -> str:
    """Resolve a rehydrated slot's title origin from persisted metadata.

    Mirrors how ``_titled`` is derived from the presence of a persisted title,
    so "a manual rename is final" survives a reload. A recognized stored
    ``title_origin`` is used verbatim. A titled slot with NO stored origin is a
    LEGACY session written before the field existed: treat it conservatively as
    ``"user"`` so the background title refresh never rewrites what might be a
    manual rename. An untitled slot has no origin.
    """
    if not titled:
        return ""
    if isinstance(stored, str) and stored in _TITLE_ORIGINS:
        return stored
    return "user"


def _rehydrate_title_refresh_mark(stored: object) -> int:
    """Resolve the persisted refresh mark; unknown/invalid values mean 0."""
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
        return stored
    return 0


def _rehydrate_slot_title(
    slot: _ChatSlot,
    raw_title: str,
    *,
    titled: bool,
    metadata: Mapping[str, object],
) -> None:
    """Restore the complete persisted title state through one contract.

    Titles may be model-authored, so display redaction must happen before the
    value reaches the slot. Keeping provenance and refresh-budget restoration
    beside that assignment prevents hydration paths from restoring only part
    of the title state.
    """
    safe_title, _ = redact_exfiltration_urls(raw_title)
    safe_title, _ = redact_credentials(safe_title)
    slot.title = safe_title
    slot._titled = titled
    slot._title_origin = _rehydrate_title_origin(titled, metadata.get("title_origin"))
    slot._title_refresh_mark = _rehydrate_title_refresh_mark(metadata.get("title_refresh_mark"))


_MAX_HISTORY_CHARS = 8000

# Bounded retries for taking a consistent (window, _disk_older_count) snapshot
# when _save_slot_to_history runs in the flush executor thread concurrently with
# event-loop mutations. A handful suffices — the only racing mutation is the
# rare >10000-message trim; retries just re-read until the two reads agree.
_FLUSH_SNAPSHOT_RETRIES = 4

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


def update_reasoning_effort_values(acp_levels: list[str]) -> None:
    """Update valid effort levels from ACP session config.

    Preserves ACP order for display. The validation set grows monotonically —
    it UNIONS the new levels onto the existing set (and the fallback) and never
    shrinks, so a level that a prior session reported (and that a slot may have
    persisted) stays valid even after another session reports a narrower config.

    Sanitizes input: only lowercase alphanumeric strings pass through
    (defense-in-depth for subprocess boundary).

    Note: ``_reasoning_effort_ordered`` is a process-global *fallback* display
    list only. The dropdown resolves levels per-slot from the slot's live ACP
    provider (see ``api_effort_levels``); this global is served only when no
    live provider is available.
    """
    global _reasoning_effort_values, _reasoning_effort_ordered
    safe_levels = [
        level for level in acp_levels if isinstance(level, str) and _SAFE_EFFORT_RE.match(level)
    ]
    level_set = set(safe_levels)
    # Union-only: never drop a previously-valid level (persistence safety).
    merged = _reasoning_effort_values | set(_REASONING_EFFORT_FALLBACK) | level_set | {""}
    ordered = [level for level in safe_levels if level]
    if merged != _reasoning_effort_values or ordered != _reasoning_effort_ordered:
        logger.info("Effort levels updated from ACP: %s", ordered)
        _reasoning_effort_values = merged
        _reasoning_effort_ordered = ordered


def _validate_reasoning_effort(raw: object) -> str:
    """Return *raw* if it's a valid reasoning_effort string, else "".

    Used by the persistence restore paths so a tampered/corrupted
    metadata file cannot smuggle an arbitrary string into the CC
    ``--effort`` subprocess argument.
    """
    if isinstance(raw, str) and raw in _reasoning_effort_values:
        return raw
    if raw:
        logger.warning("Discarding invalid persisted reasoning_effort: %r", raw)
    return ""


def _validate_autocompact_pct(raw: object) -> float | None:
    """Return *raw* as a threshold percent within the documented range, else None.

    Restore-path twin of the endpoint validation: a tampered or corrupted
    metadata file must not seed an override that can never fire (over the max)
    or that thrashes compaction (under the min). Out-of-range finite values
    clamp — matching how the loader treats the global knob — while
    non-numeric/NaN values are discarded.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            value = float(raw)
        except OverflowError:
            # An int too large for a float; a corrupted metadata file must not
            # abort the restore path.
            logger.warning("Discarding oversized persisted autocompact_pct")
            return None
        if value != value:  # NaN
            logger.warning("Discarding NaN persisted autocompact_pct")
            return None
        return min(max(value, AUTOCOMPACT_PCT_MIN), AUTOCOMPACT_PCT_MAX)
    if raw is not None:
        logger.warning("Discarding invalid persisted autocompact_pct: %r", raw)
    return None


def save_all_slots_to_history(state: DashboardState) -> None:
    """Save all active slots to history. Called on gateway shutdown."""
    for slot in list(state._slots.values()):
        try:
            _save_slot_to_history(state, slot, force=True)
        except Exception:
            logger.error("Shutdown: failed to save slot %s", slot.key, exc_info=True)
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


def _build_kiro_model_map() -> dict[str, str]:
    """Map kiro-agent name/stem -> configured model, for legacy sessions.

    Sessions persisted before ``model`` was written into their metadata resolve
    their model by agent name instead, so both restore paths need this map.
    Factored out of ``_rehydrate_slot_from_history`` and
    ``restore_recent_sessions`` because the former rebuilt it *per restored
    slot* — re-globbing and re-parsing every agent JSON on each of N tabs to
    produce a byte-identical dict. Callers restoring many slots should build it
    once and pass it down (see ``kiro_model_map`` params below).
    """
    try:
        return agent_model_map(
            agents_dir=kiro_agents_dir_path(),
            operation="chat_persistence",
            source="unknown",
        )
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
        return {}


def _load_restore_cfg() -> "KiroCrewConfig | None":
    """Load the config the restore paths read, tolerating a broken file.

    Factored out so the async drivers can hoist it into a worker thread —
    ``KiroCrewConfig.load()`` reads and parses ``config.json`` from disk, which
    is exactly the kind of blocking I/O that must not run on the event loop
    during startup restore.
    """
    try:
        return KiroCrewConfig.load()
    except Exception:
        return None


def _read_open_slots_keys() -> list[object]:
    """Read and parse ``open_slots.json``, returning its raw ``keys`` list.

    Pure disk work with no slot state touched, so the async driver can hoist the
    whole thing into ``asyncio.to_thread``: the read plus the JSON parse were
    running on the event loop during startup (#895).

    Entries are returned UNVALIDATED — the file is attacker-writable, so every
    caller must pass each one through :func:`_sanitize_open_slot_key` before it
    reaches path construction. Returns ``[]`` for a missing, unreadable or
    malformed file, which is the documented no-op.
    """
    path = config_dir() / "open_slots.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("open_slots.json unreadable; skipping", exc_info=True)
        return []
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return []
    return list(keys)


def _sanitize_open_slot_key(raw: object) -> str | None:
    """Fold one ``open_slots.json`` entry to a canonical slot key, or reject it.

    Single home for the screen both restore drivers apply, so the sync and async
    paths cannot drift on the security check.

    Defense-in-depth: slot keys flow into ``_history_key_for()`` -> filesystem
    path construction. ``open_slots.json`` is 0o600 so the threat is small, but a
    key smuggled in (symlink attack at write time or a separate vuln) could
    escape the sessions directory (e.g. ``"../../etc/passwd"``). Live-gateway
    slot keys never contain path separators; reject any that do and warn so an
    attempted breakout is visible, leaving the caller to restore the rest.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if "/" in raw or "\\" in raw:
        logger.warning("restore_open_slots: rejecting key with path separators: %r", raw)
        return None
    # Fold to the canonical (filename-charset) key. Snapshots written before
    # slot-key normalization landed may carry a raw display-style key (e.g.
    # "Artifact: My Doc") alongside its sanitized twin — after folding, the
    # second form hits the caller's dedup guard instead of restoring a duplicate
    # sidebar session backed by the same transcript.
    return _normalize_slot_key(raw)


def _prefetch_rehydrate_inputs(
    conv_log: ConversationLog,
    history_key: str,
    *,
    adopt_closed: bool = False,
    kiro_model_map: dict[str, str] | None = None,
    with_status: bool = False,
) -> tuple[dict, bool, list[dict] | None, dict[str, str] | None, tuple[str, str] | None]:
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

    Returns ``(meta, readable, messages, model_map, member_identity)``.
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
        return meta or {}, readable, None, None, None
    return (
        meta,
        readable,
        conv_log.read_messages_chained(history_key),
        kiro_model_map if kiro_model_map is not None else _build_kiro_model_map(),
        # The transcript key is "dashboard:" + slot name; identity is a
        # property of the slot name.
        _member_restore_identity(history_key.removeprefix("dashboard:")),
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
    # Built once and shared across every tab — it is identical per slot.
    kiro_model_map = _build_kiro_model_map()
    for raw in keys:
        key = _sanitize_open_slot_key(raw)
        if key is None or key in state._slots:
            continue
        try:
            # Ask whether the metadata READ succeeded, not just whether it came
            # back empty (``with_status``) — see _prefetch_rehydrate_inputs.
            #
            # These reads MUST stay inside the per-tab guard. The async driver
            # has no except at its call site either, so anything escaping here
            # aborts dashboard startup and costs every LATER tab too.
            meta, readable, messages, model_map, member_identity = _prefetch_rehydrate_inputs(
                state.conversation_log,
                slot_transcript_key(key),
                kiro_model_map=kiro_model_map,
                with_status=True,
            )
            restored += _apply_restored_open_slot(
                state,
                key,
                meta=meta,
                readable=readable,
                messages=messages,
                model_map=model_map,
                member_identity=member_identity,
                unrestored=unrestored,
            )
        except Exception:
            logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
            # Same epistemic position as an unreadable read: the session was not
            # shown to be gone, so keep its key rather than erasing the seed.
            unrestored.add(key)
            # No rollback here: _rehydrate_slot_from_history undoes its own
            # partial slot and restricted key, so every caller gets it rather
            # than only the ones that remembered to compensate.
        # One yield point per tab, reached on EVERY outcome. A failing tab still
        # costs real I/O, so a run of failing tabs that skipped the yield would
        # monopolise the loop and feed the stall watchdog. The sync driver just
        # spins through it; the async driver has its own per-tab yield.
        yield restored
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)


def _deletion_during_read(
    conv_log: ConversationLog,
    history_key: str,
    pre_meta: dict,
    pre_messages: list[dict] | None,
) -> str | None:
    """Was *history_key* deleted (or deleted-and-recreated) during a prefetch?

    Returns a short reason for logging, or ``None`` when it is safe to build.

    Offloading a transcript read (#895) opened a window that did not exist when
    read-then-build ran atomically on the loop: ``ConversationLog.delete_session``
    leaves **no tombstone** — its own docstring notes that once the delete
    releases the lock "a concurrent writer can recreate the session" — so a slot
    published from content we already hold rewrites, on its next flush, a file
    the user permanently deleted. The dashboard's HTTP listener is bound before
    startup restore runs (``_start_site`` precedes it in ``start_dashboard``), so
    a user delete really can land inside this window.

    This is the guard the chat-resume handler already applies after its own read
    for exactly this reason; the logic is mirrored here rather than reinvented, so
    both surfaces refuse on the same evidence.

    MUST be called synchronously, on the loop, with no suspension point between
    it and the build it gates — an await in between would reopen the window it
    closes.

    Two arms, and the asymmetry in each is deliberate:

    * **absence** — ``get_metadata_status``, never ``get_metadata``: the latter
      returns ``{}`` for both "deleted" and "unreadable", and reading an
      unreadable metadata line as a deletion would discard a LIVE session. On an
      unreadable read this returns ``None`` (build), because refusing is the
      destructive direction here.
    * **identity** — the delete leaves no tombstone, so a delete-then-RECREATE
      inside the window leaves a NON-EMPTY metadata dict belonging to a NEW
      conversation. Existence alone reads that as "still here" and would publish
      a slot holding the OLD transcript, whose flush overwrites a session the
      user is actively using — worse than the first arm, because the data
      destroyed is live. ``created_at`` is the discriminator: every path that
      MINTS a metadata line stamps it, while a rewrite/compaction carries it
      through verbatim, so this does not fire on a legitimate rewrite.

    ``created_at`` ABSENT on either side falls through to building rather than
    refusing: refusing would reject every transcript whose metadata predates the
    field, a visible break for real users, to close a narrow race.

    The existence witness is the UNION of the pre-read metadata and the
    transcript, so a metadata-only session (a metadata line with no messages,
    which ``update_metadata`` creates on upsert) is not silently unguarded.
    """
    post_meta, readable = conv_log.get_metadata_status(history_key)
    if not readable:
        return None
    if not (pre_meta or pre_messages):
        # Never existed when we looked — an absent key is a new conversation,
        # not a deletion.
        return None
    if not post_meta:
        return "deleted"
    pre_identity = pre_meta.get("created_at")
    post_identity = post_meta.get("created_at")
    if pre_identity and post_identity and pre_identity != post_identity:
        return "deleted and recreated"
    return None


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
    conv_log: ConversationLog | None = None,
    started: float | None = None,
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

    Blocking: restores every tab without yielding. Startup on the event loop must
    use :func:`restore_open_slots_async` instead — see the note there.
    """
    restored = 0
    for restored in _restore_open_slots_steps(state):
        pass
    return restored


async def restore_open_slots_async(state: DashboardState) -> int:
    """:func:`restore_open_slots`, with the disk reads off the loop (#895).

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
    if not state.conversation_log:
        return 0
    restored = 0
    state.restoring_open_slots = True
    try:
        keys = await asyncio.to_thread(_read_open_slots_keys)
        if not keys:
            return 0
        # Rebound only once there is a snapshot to restore FROM, matching the
        # generator: a missing/malformed file must not clear a carried set.
        unrestored: set[str] = set()
        state.unrestored_slot_keys = unrestored
        conv_log = state.conversation_log
        kiro_model_map = await asyncio.to_thread(_build_kiro_model_map)
        for raw in keys:
            key = _sanitize_open_slot_key(raw)
            if key is None or key in state._slots:
                continue
            try:
                started = time.time()
                meta, readable, messages, model_map, member_identity = await asyncio.to_thread(
                    _prefetch_rehydrate_inputs,
                    conv_log,
                    slot_transcript_key(key),
                    kiro_model_map=kiro_model_map,
                    with_status=True,
                )
                restored += _apply_restored_open_slot(
                    state,
                    key,
                    meta=meta,
                    readable=readable,
                    messages=messages,
                    model_map=model_map,
                    member_identity=member_identity,
                    unrestored=unrestored,
                    # Opts into the post-hop re-checks (close tombstone +
                    # deletion): this driver's read ran in a worker thread, so
                    # its answers can have gone stale.
                    conv_log=conv_log,
                    started=started,
                )
            except Exception:
                logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
                unrestored.add(key)
            # sleep(0) yields to the ready queue without adding wall-clock delay.
            # Reached on EVERY outcome, including a failing tab (see the
            # generator's note) — and still needed with the reads offloaded,
            # because the apply half above runs here.
            await asyncio.sleep(0)
        if restored:
            logger.info("Restored %d open tab(s) from open_slots.json", restored)
    finally:
        # Always clear, even if a rehydrate raises — a stuck flag would silently
        # disable open-tab persistence for the rest of the process's life.
        state.restoring_open_slots = False
    return restored


def _attach_variants(slot: _ChatSlot, m: dict) -> None:
    """Copy variant history from a persisted message onto the slot's last message, with redaction."""
    if m.get("variants"):
        slot.messages[-1]["variants"] = [  # type: ignore[assignment]
            {
                **v,
                "content": redact_credentials(redact_exfiltration_urls(v.get("content", ""))[0])[0],
            }
            for v in m["variants"]
            if isinstance(v, dict)
        ]
        slot.messages[-1]["variant_idx"] = m.get("variant_idx", 0)


def _member_restore_identity(slot_name: str) -> tuple[str, str] | None:
    """Resolve a member slot's restore identity from its binding.

    Returns ``None`` for ordinary keys (caller restores normally),
    ``(member, "member")`` when ``dm.json`` names the thread's crew, and
    :data:`_SKIP_MEMBER_RESTORE` when the key is a member key without a
    usable binding. The BINDING is the authority — transcript metadata lives
    in the same operator-editable JSONL it would otherwise re-pin from, so it
    is never consulted for a member key's agent or mode.
    """
    # Function-local ON PURPOSE: kiro_crew.members imports kiro_crew.artifacts
    # (slugify), and importing that at module scope closes the
    # artifacts -> ... -> webhooks -> validation -> artifacts cycle when this
    # module loads inside crew_chat's import graph
    # (test_crew_chat_does_not_import_the_dashboard_handler_tree pins this).
    from kiro_crew import members as members_mod

    prefix = members_mod.DM_SLOT_KEY_PREFIX
    if not slot_name.startswith(prefix):
        return None
    binding = members_mod.read_dm_binding(slot_name[len(prefix) :])
    member = (binding or {}).get("member", "")
    if not member:
        logger.warning(
            "restore: member slot %r has no usable dm binding; leaving it "
            "unpublished (the member-thread endpoint re-creates it on open)",
            slot_name,
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
    try:
        slot = state.get_or_create_slot(
            slot_name,
            agent=_member_identity[0] if _member_identity else "",
            mode=_member_identity[1] if _member_identity else "",
            app=meta.get("app", ""),
            # PERSISTED provenance only. A name is not evidence: main supports a
            # dashboard slot a caller happened to name ``slack_notes`` (see
            # test_slack_dashboard_live_sync's "the guard must not be a name
            # heuristic"), so inferring channel origin from the stem would let a
            # fresh dashboard conversation adopt a real thread's transcript.
            # A legacy channel transcript carrying neither marker is surfaced by
            # ``channel_slot_reconciler`` instead, which sets the flag -- and the
            # first save then persists it, so later boots need no inference.
            channel_origin=(
                bool(meta.get("channel_origin")) or bool(meta.get("linked_session_key"))
            ),
            # Restore the persisted origin. Re-deriving it here would relabel
            # every rehydrated slot on restart, so a cron slot would come back
            # as USER (leak) and a real user slot as untagged (silently
            # dropping `slots:user` for apps that legitimately hold it).
            origin=str(meta.get("origin", "")),
        )
        # Title comes from the metadata line we already read above. We deliberately
        # do NOT consult ``list_sessions()`` here: that call globbed + stat'd + read
        # the first line of EVERY session file in the history dir (O(all sessions))
        # to look up one title, and it ran once per restored slot — so a boot with N
        # open tabs did N full directory scans. With 77 tabs over 455 session files
        # that measured ~13s of pure event-loop block, which alone can trip the
        # 25s LoopStallWatchdog and crash-loop the gateway before it ever serves.
        #
        # It was also dead code: ``list_sessions()`` keys are FILENAME STEMS
        # (``dashboard_chat-1-...``, because history's ``_safe_key()`` folds ``:``
        # to ``_``), while ``history_key`` here is the canonical colon form
        # (``dashboard:chat-1-...``). The two never compared equal, so the lookup
        # always yielded ``{}`` and the title always fell through to ``meta``.
        # Dropping it is therefore behaviour-identical as well as O(N) cheaper.
        #
        # Titles may have been auto-generated by an LLM (_generate_title_via_kiro)
        # and are surfaced on the dashboard, so apply the same redaction passes
        # used on assistant content before setting. Defence-in-depth — the title
        # author is trusted-ish (our own kiro process), but the generation input
        # is user content, so a prompt injection could craft a title with an
        # exfiltration URL or leaked credential.
        raw_title = meta.get("title") or slot_name
        _rehydrate_slot_title(
            slot,
            raw_title,
            titled=bool(meta.get("title")),
            metadata=meta,
        )
        if meta.get("created_at"):
            slot.created_at = meta["created_at"]
        # The identity of the file this restore just read — lets a later save
        # recognize a file recreated by another writer after a permanent
        # delete (delete-won guard).
        slot._disk_meta_created_at = str(meta.get("created_at") or "")
        # Legacy metadata has no ``created_at``: record the observation
        # itself so the guard's missing-file witness still fires for it.
        slot._disk_meta_observed = bool(meta)
        # Member keys keep the binding-derived agent/mode: transcript metadata
        # is the operator-editable file the pin must not re-derive from.
        if meta.get("agent") and _member_identity is None:
            slot.agent = meta["agent"]
        if meta.get("model"):
            # _normalize_model handles deprecation renames. For claude_code sessions,
            # also map a pre-migration raw provider id back to the canonical key so it
            # matches the canonical-keyed dropdown (no-op for other providers). Reuse
            # the already-loaded _restore_cfg provider — no second config load.
            _prov = _restore_cfg.agent.provider if _restore_cfg else ""
            slot.model = model_registry.canonicalize_for_provider(
                _normalize_model(meta["model"]), _prov
            )
        elif slot.agent:
            try:
                mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
                kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
                slot.model = kiro_model_map.get(kiro_name, "")
            except Exception:
                logger.debug(
                    "Failed to resolve model for rehydrated slot %s", slot_name, exc_info=True
                )
        if meta.get("reasoning_effort"):
            slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
        if meta.get("autocompact_pct") is not None:
            slot.autocompact_pct = _validate_autocompact_pct(meta["autocompact_pct"])
        if meta.get("workspace"):
            slot.workspace = meta["workspace"]
        if meta.get("project"):
            slot.project = meta["project"]
        # Restore the remote executor marker INDEPENDENTLY of its target fields.
        # history JSONL is a file on disk, so a truncated write or a hand-edit can
        # leave the ``executor="remote"`` marker without a valid instance_id /
        # remote_slot. Dropping the marker in that case (the old behaviour) failed
        # OPEN: the session came back as an ordinary local slot and its next send
        # ran the crew's turn on THIS machine — the wrong-host execution the remote
        # binding exists to prevent (GPT #7693). Fail CLOSED instead: keep the
        # marker, populate only the target fields that are valid, and let the
        # incomplete-binding guard in ``api_chat`` (``slot.executor == "remote" and
        # not slot.is_remote`` -> 409 ``remote_binding_incomplete``) plus the
        # ``_run_chat`` chokepoint (keyed on ``executor``, not ``is_remote``) refuse
        # the send with a message the user can act on, rather than run local.
        _relay_was_in_flight = False
        _executor_meta = meta.get("executor")
        _instance_meta = meta.get("instance_id")
        _remote_slot_meta = meta.get("remote_slot")
        if _executor_meta == "remote":
            slot.executor = "remote"
            if isinstance(_instance_meta, str) and _instance_meta:
                slot.instance_id = _instance_meta
            if isinstance(_remote_slot_meta, str) and _remote_slot_meta:
                slot.remote_slot = _remote_slot_meta
            # Deferred to AFTER the window is loaded (see below): the metadata line
            # is read before the transcript rows, so appending here would land the
            # notice ahead of the conversation instead of at its tail. Only a
            # COMPLETE binding can have been mid-relay; an incomplete one never
            # dispatched, so there is no in-flight tail to recover.
            if slot.is_remote:
                _relay_was_in_flight = bool(meta.get("relay_in_flight"))
        if meta.get("mode") and _member_identity is None:
            slot.mode = meta["mode"]
        if meta.get("created_by"):
            # Creator attribution restored so the member ownership boundary in
            # session-control authorization survives a restart: without it every
            # worker a member dispatched would come back unowned and the
            # fail-closed `not_creator` check would strand them.
            slot._created_by = str(meta["created_by"])
        if meta.get("folder_id"):
            slot.folder_id = meta["folder_id"]
        if meta.get("channel_folder_filed"):
            slot._channel_folder_filed = True
        if meta.get("app"):
            slot._app = meta["app"]
        # Re-validate the companion binding against the slug grammar on restore
        # (same gate as slot create) — history JSONL is a file an attacker with
        # disk access could tamper, and this value flows into to_dict()/WS
        # broadcasts to every connected dashboard client.
        _artifact_meta = meta.get("artifact")
        if isinstance(_artifact_meta, str) and ARTIFACT_SLUG_RE.match(_artifact_meta):
            slot._artifact = _artifact_meta
        if meta.get("pinned"):
            slot.pinned = True
        if meta.get("color_index") is not None:
            slot.color_index = meta["color_index"]
        _ch = meta.get("color_hex")
        if isinstance(_ch, str) and COLOR_HEX_RE.match(_ch):
            slot.color_hex = _ch.lower()
        raw_tags = meta.get("tags")
        if isinstance(raw_tags, list):
            slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
            # Prune ids missing from the vocabulary: tag deletion commits the
            # vocab write first (crash-atomic), so a crash mid-delete can
            # leave dangling ids on the persisted slot line. load_tags() runs
            # before any slot restore, so state._tags is authoritative here.
            # FAIL-OPEN only when the vocabulary is UNKNOWN (tags.json parse
            # or I/O failure): pruning then would wipe EVERY assignment and
            # the next save persists the loss. A legitimately-empty vocabulary
            # (user deleted the last tag) IS authoritative and must prune —
            # otherwise a crash mid-delete resurrects the dangling id forever.
            if getattr(state, "_tags_authoritative", True):
                known = {t.get("id") for t in state._tags}
                slot.tags = [t for t in slot.tags if t in known]
        if meta.get("auto_tagged"):
            slot._auto_tagged = True
        if meta.get("human_seen"):
            # Attendance survives the restart, so an app-owned tab a person has
            # been working in keeps the full approval window instead of silently
            # dropping to the unattended deny-fast (state._ChatSlot.unattended).
            slot._human_seen = True
        mm = meta.get("memory_mode", "persistent")
        slot.memory_mode = mm
        if mm != "persistent":
            state._restricted_keys.add(f"dashboard:{slot_name}")
        if meta.get("forked_from") is not None:
            slot.forked_from = meta["forked_from"]
        if meta.get("linked_session_key"):
            # Rebind the slot to the session its conversation actually runs on.
            # Skipped, the slot would answer from a dashboard-only session and the
            # channel thread would stop seeing its replies.
            slot.linked_session_key = str(meta["linked_session_key"])
        # Re-seed the live compaction threshold. The SessionManager's override
        # map is process-local, so a rehydrated slot must push its persisted
        # value back or the session silently compacts at the global threshold.
        # After the link assignment above, so a channel-born slot seeds the
        # session its turns actually run on.
        if slot.autocompact_pct is not None and state.sessions:
            state.sessions.set_autocompact_pct(effective_session_key(slot), slot.autocompact_pct)
        # Restore the persisted tab_id so cross-restart fork chaining survives.
        # get_or_create_slot (called by our caller) assigns a fresh random uuid to
        # slot._tab_id; if we don't overwrite it here, the next _flush_dirty_slots
        # persists that uuid back into meta, severing the tab_id ancestry that
        # read_messages_chained walks across forks — one restart + one flush
        # permanently loses forked-session history. Mirrors restore_recent_sessions.
        tab_id = meta.get("tab_id")
        if not tab_id:
            tab_id = uuid.uuid4().hex[:12]
            needs_tab_id_backfill = True
        else:
            needs_tab_id_backfill = False
        slot._tab_id = tab_id
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
        if needs_tab_id_backfill:
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
            update_metadata_off_loop(state.conversation_log, history_key, {"tab_id": tab_id})
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
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
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
                # meta to a client.
                meta=(m["meta"] if isinstance(m.get("meta"), dict) else None),
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
        if _relay_was_in_flight:
            # The gateway crashed while this slot's turn was executing on the peer
            # (flagged in the binding block above). The relay reader died with it
            # and the turn's tail was never mirrored here, so the loaded window
            # stops mid-turn. Append an explicit notice at the TAIL rather than
            # resurrect a silently truncated conversation — the peer may well have
            # finished, and the next send re-synchronises the visible history.
            # ``broadcast=False`` is the sanctioned replay door (fork / transfer /
            # window-rebuild use it): no clients exist at boot, and it appends a
            # schema-correct row with a minted id. Placed AFTER ``_disk_window_len``
            # so the new row is not miscounted as already-persisted, with ``_dirty``
            # re-armed so the next flush writes it. The runtime ``_relay_in_flight``
            # stays False, so that flush clears the on-disk marker and a second
            # restart cannot append the notice twice.
            slot.append(
                "error",
                "This turn was interrupted when the app restarted. The crew may "
                "have finished it — send again to pick the conversation back up.",
                "msg msg-err",
                broadcast=False,
            )
            slot._dirty = True
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
    """
    if not state.conversation_log:
        return None
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    conv_log = state.conversation_log

    started = time.time()
    _meta, _readable, messages, model_map, _member_id = await asyncio.to_thread(
        _prefetch_rehydrate_inputs,
        conv_log,
        history_key,
        adopt_closed=adopt_closed,
        kiro_model_map=kiro_model_map,
    )
    # ``messages is None`` covers both "never persisted" and "closed with ✕"
    # — the prefetch already applied the same guards the synchronous form does.
    if messages is None:
        return None
    meta = _meta
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
    # drivers apply. This wrapper's read has always been offloaded, so the window
    # predates #895 — the guard is folded in here anyway rather than left as the
    # one uncovered instance, because a class of defect fixed at two of three
    # call sites simply returns through the third.
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
    return _rehydrate_slot_from_history(
        state,
        slot_name,
        kiro_model_map=model_map,
        adopt_closed=adopt_closed,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
        _prefetched_member_identity=_member_id,
    )


def _recent_session_slot_name(key: str) -> str | None:
    """Map a ``list_sessions()`` key to its dashboard slot name, or skip it.

    Returns ``None`` for a key this restore path does not own. Channel-born
    sessions are restored by ``channel_slot_reconciler``, which reads their
    transcripts in an executor — pulling them in here would put a large
    transcript's read in front of the whole gateway at startup.
    """
    if key.startswith("dashboard:"):
        return key.removeprefix("dashboard:")
    if key.startswith("dashboard_"):
        return key.removeprefix("dashboard_")
    return None


def _prefetch_recent_session(
    conv_log: ConversationLog,
    key: str,
    session: dict,
    *,
    folders_only: bool,
    cutoff: float | None,
) -> tuple[dict | None, list[dict] | None, tuple[str, str] | None]:
    """Read one candidate session's metadata + transcript, off the loop (#895).

    Applies the selection filters BETWEEN the two reads so a session that is
    going to be skipped never pays for its transcript walk — the metadata read is
    what the filters need, and it is the cheap one.

    Returns ``(None, None, None)`` for a session this pass must skip (not
    folder'd / pinned under ``folders_only``, closed with ✕, or outside the
    mtime window). The third element is the prefetched
    ``_member_restore_identity`` answer — dm.json is file IO too, and the apply
    half is loop-affine. Pure disk work: no slot state is touched, so the whole
    function is safe in ``asyncio.to_thread`` while the loop-affine apply half
    stays on the loop.
    """
    meta = conv_log.get_metadata(key)
    if not meta:
        # No metadata line at all. ``list_sessions()`` is a SNAPSHOT, and since
        # #895 it is taken one thread hop before this read, so a session can be
        # deleted in between and still appear in the list — or the read itself
        # came back empty. Either way there is nothing to build from, and
        # building anyway is destructive rather than merely useless: an empty
        # ``meta`` sails past the folder/pin/closed/cutoff filters below (all of
        # which read falsy), reaches ``get_or_create_slot``, and registers a
        # PHANTOM slot whose next flush RECREATES the transcript the user
        # deleted. ``_rehydrate_slot_from_history`` already refuses on empty
        # metadata for exactly this reason ("don't create a phantom slot"); this
        # makes the recent-sessions path agree with it.
        return None, None, None
    has_folder = bool(meta.get("folder_id"))
    has_pin = bool(meta.get("pinned"))
    if folders_only and not has_folder and not has_pin:
        return None, None, None
    if meta.get("closed"):
        return None, None, None
    if not has_folder and not has_pin:
        if cutoff is not None and session.get("modified", 0) < cutoff:
            return None, None, None
    return (
        meta,
        conv_log.read_messages_chained(key),
        _member_restore_identity(_recent_session_slot_name(key) or ""),
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
    slot = state.get_or_create_slot(
        slot_name,
        agent=_member_identity[0] if _member_identity else "",
        mode=_member_identity[1] if _member_identity else "",
        app=meta.get("app", ""),
        # No channel_origin here: the caller skips every non-dashboard key, so a
        # channel-born session never reaches this — ``channel_slot_reconciler``
        # owns surfacing those.
        # Restore the persisted origin. Re-deriving it here would relabel
        # every rehydrated slot on restart, so a cron slot would come back
        # as USER (leak) and a real user slot as untagged (silently
        # dropping `slots:user` for apps that legitimately hold it).
        origin=str(meta.get("origin", "")),
    )
    # Titles can be LLM-generated (auto-title) and are surfaced on the
    # dashboard — apply the same redaction as assistant content. Matches
    # the treatment in _rehydrate_slot_from_history above.
    raw_title = session.get("title", slot_name)
    _rehydrate_slot_title(
        slot,
        raw_title,
        titled=bool(session.get("title")),
        metadata=meta,
    )
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    # The identity of the file this restore just read — lets a later save
    # recognize a file recreated by another writer after a permanent delete
    # (delete-won guard).
    slot._disk_meta_created_at = str(meta.get("created_at") or "")
    # Legacy metadata has no ``created_at``: record the observation itself so
    # the guard's missing-file witness still fires for it.
    slot._disk_meta_observed = bool(meta)
    # Member keys keep the binding-derived agent/mode: transcript metadata is
    # the operator-editable file the pin must not re-derive from.
    if meta.get("agent") and _member_identity is None:
        slot.agent = meta["agent"]
    if meta.get("model"):
        # Canonicalize a pre-migration claude_code provider id to the
        # canonical dropdown key (no-op for other providers); reuse the
        # already-loaded _restore_cfg provider.
        _prov = _restore_cfg.agent.provider if _restore_cfg else ""
        slot.model = model_registry.canonicalize_for_provider(
            _normalize_model(meta["model"]), _prov
        )
    elif slot.agent:
        try:
            mc = _restore_cfg.agents.get(slot.agent) if _restore_cfg else None
            kiro_name = mc.kiro_agent if mc and mc.kiro_agent else slot.agent
            slot.model = kiro_model_map.get(kiro_name, "")
        except Exception:
            logger.debug("Failed to resolve model for restored slot %s", slot_name, exc_info=True)
    if meta.get("reasoning_effort"):
        slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
    if meta.get("autocompact_pct") is not None:
        slot.autocompact_pct = _validate_autocompact_pct(meta["autocompact_pct"])
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode") and _member_identity is None:
        slot.mode = meta["mode"]
    if meta.get("created_by"):
        # Same rehydration as _rehydrate_slot_from_history: without it a
        # member-created worker restored through the recent-session path
        # loses its creator binding and authorize_target refuses the
        # legitimate member with not_creator.
        slot._created_by = str(meta["created_by"])
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
    if meta.get("channel_folder_filed"):
        slot._channel_folder_filed = True
    if meta.get("app"):
        slot._app = meta["app"]
    # Same tamper gate as _rehydrate_slot_from_history: re-validate the
    # companion binding against the slug grammar before it reaches
    # to_dict()/WS broadcasts.
    _artifact_meta = meta.get("artifact")
    if isinstance(_artifact_meta, str) and ARTIFACT_SLUG_RE.match(_artifact_meta):
        slot._artifact = _artifact_meta
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    _ch = meta.get("color_hex")
    if isinstance(_ch, str) and COLOR_HEX_RE.match(_ch):
        slot.color_hex = _ch.lower()
    if meta.get("color_theme"):
        slot.color_theme = meta["color_theme"]
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
        # Prune ids missing from the vocabulary: tag deletion commits the
        # vocab write first (crash-atomic), so a crash mid-delete can
        # leave dangling ids on the persisted slot line. load_tags() runs
        # before any slot restore, so state._tags is authoritative here.
        # FAIL-OPEN only when the vocabulary is UNKNOWN (tags.json parse
        # or I/O failure): pruning then would wipe EVERY assignment and
        # the next save persists the loss. A legitimately-empty vocabulary
        # (user deleted the last tag) IS authoritative and must prune —
        # otherwise a crash mid-delete resurrects the dangling id forever.
        if getattr(state, "_tags_authoritative", True):
            known = {t.get("id") for t in state._tags}
            slot.tags = [t for t in slot.tags if t in known]
    if meta.get("auto_tagged"):
        slot._auto_tagged = True
    if meta.get("human_seen"):
        # Attendance survives the restart, so an app-owned tab a person has
        # been working in keeps the full approval window instead of silently
        # dropping to the unattended deny-fast (state._ChatSlot.unattended).
        slot._human_seen = True
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{slot_name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    if meta.get("linked_session_key"):
        slot.linked_session_key = str(meta["linked_session_key"])
    elif is_channel_session_key(key) and state.sessions:
        # First time this thread is surfaced: bind it to the session the
        # channel itself runs. Resolved from the session map, never derived
        # from the filename — the ``:``-to-``_`` fold is not reversible, so
        # a guess could point the tab at a session the channel never reads.
        real_key = state.sessions.channel_key_for_stem(key)
        if real_key:
            slot.linked_session_key = real_key
    # Re-seed the live compaction threshold (see _rehydrate_slot_from_history).
    if slot.autocompact_pct is not None and state.sessions:
        state.sessions.set_autocompact_pct(effective_session_key(slot), slot.autocompact_pct)
    tab_id = meta.get("tab_id")
    if not tab_id:
        tab_id = uuid.uuid4().hex[:12]
        # restore_recent_sessions runs during on_startup (event loop live)
        # — keep the _locked flock/os.close off the loop via the off-loop
        # backfill helper. Dispatched AFTER the transcript read above (the
        # caller prefetches messages first) so its os.replace() cannot race
        # the read of the same file — see the equivalent note in
        # _rehydrate_slot_from_history.
        update_metadata_off_loop(conv_log, key, {"tab_id": tab_id})
    slot._tab_id = tab_id
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
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            broadcast=False,
            meta=(m["meta"] if isinstance(m.get("meta"), dict) else None),
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
        meta, messages, _member_id = _prefetch_recent_session(
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
        )
        restored += 1
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
    """:func:`restore_recent_sessions`, with the disk reads off the loop (#895).

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
            meta, messages, _member_id = await asyncio.to_thread(
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
            )
            restored += 1
            await asyncio.sleep(0)
        _sync_dashboard_slots(state)
    finally:
        state.restoring_open_slots = False
    return restored


def _diff_dropped_message_lines(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Return existing message lines that *new_lines* would drop.

    Both inputs are full file-line lists (metadata line at index 0, which is
    skipped on both sides). Compares by normalized JSON (``sort_keys``, so a
    key-order change is not a spurious drop). Corrupted/unparseable old lines
    are treated as dropped (archived). This is the same drop-detection rule
    ``ConversationLog.rewrite_session`` applies; it is factored out here so the
    dashboard rewrite path and ``rewrite_session`` share one definition.
    """
    if old_lines and '"_type"' in old_lines[0]:
        old_lines = old_lines[1:]
    kept_serialized: set[str] = set()
    for ln in new_lines[1:]:
        if not ln.strip():
            continue
        try:
            kept_serialized.add(json.dumps(json.loads(ln), sort_keys=True))
        except ValueError:
            continue
    dropped: list[str] = []
    for ln in old_lines:
        if not ln.strip():
            continue
        try:
            normalized = json.dumps(json.loads(ln), sort_keys=True)
        except ValueError:
            dropped.append(ln)  # corrupted line → archive it
            continue
        if normalized not in kept_serialized:
            dropped.append(ln)
    return dropped


def _archive_dropped_lines(
    state: DashboardState, history_key: str, old_lines: list[str], new_lines: list[str]
) -> None:
    """Archive on-disk message lines that *new_lines* (full file) would drop.

    Used only by the rewrite path (rewind/regenerate/fork), which intentionally
    truncates the in-memory window. The frozen prefix is present unchanged in
    both *old_lines* and *new_lines*, so it is never archived — only the dropped
    window tail is. No-op in the steady-state superset case.
    """
    dropped = _diff_dropped_message_lines(old_lines, new_lines)
    if not dropped:
        return
    base = state.conversation_log._dir if state.conversation_log else None
    _archive_lines(history_key, dropped, reason="compact", base=base)


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
# once per process and then served from this module global: the builder runs on
# every message of every flush, so it must not stat or parse ``config.json``
# per call, and the one-time read keeps the hot path free of config I/O the way
# the loader's push pattern does for the event loop. A changed value therefore
# takes effect on the next gateway restart, which the config field descriptions
# state. ``None`` means "not resolved yet"; tests reset it via the autouse
# cache-isolation fixture in ``test/conftest.py``.
_entry_cache_bounds_cached: tuple[int, int] | None = None
_entry_cache_bounds_read_warned = False


def _entry_cache_bounds() -> tuple[int, int]:
    """Configured ``(max_entries, max_bytes)`` bounds for the entry cache.

    Reads the validated config once (loader-clamped to the documented ranges)
    and memoises the pair for the process lifetime. Falls back to the built-in
    defaults when the loaded values are not real integers (a stubbed config
    object would otherwise flow a non-numeric value into the eviction
    comparison) -- that shape is process-permanent, so it latches. A config
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


def _approx_window_payload_bytes(window: list[dict]) -> int:
    """Cheap LOWER BOUND on what a window would serialize to, in bytes.

    Sums only string ``content`` on each message and on its variants, ignoring
    keys, meta and JSON escaping, and never serializes anything -- serializing to
    measure would pay the very cost the caller is deciding whether to avoid.

    Being a lower bound is what makes it safe to gate on: an estimate above the
    ceiling proves the real payload is above it too, so the bypass it triggers is
    always justified, while an underestimate merely forgoes the bypass and pays
    the hashing cost. Either way correctness is unaffected -- only throughput.
    """
    total = 0
    for m in window:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        variants = m.get("variants")
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict):
                    vc = v.get("content")
                    if isinstance(vc, str):
                        total += len(vc)
    return total


def _build_message_entry(m: dict) -> dict | None:
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
        return _build_message_entry_uncached(m)
    key = hashlib.sha256(payload.encode()).hexdigest()
    size = len(payload)
    with _entry_cache_lock:
        if key in _entry_cache:
            _entry_cache.move_to_end(key)
            return _entry_cache[key][0]
    entry = _build_message_entry_uncached(m)
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


def _build_message_entry_uncached(m: dict) -> dict | None:
    """Build one persisted JSONL message dict from an in-memory slot message.

    Returns None for transient roles that are never persisted. Applies the
    same redaction the overwrite path used so append and rewrite produce
    byte-identical lines for the same message.
    """
    role = m.get("role", "assistant")
    if role in ("chunk", "done", "streaming", "queued", "permission"):
        return None
    content = m.get("content", "")
    # Gate is `!= "user"`, NOT `not in ("user", "system")`. _save_slot_to_history
    # re-serializes the WHOLE in-memory window on every flush, so this is the
    # write-back boundary. `system` must be included: the load path does not
    # redact `system` on the way in, so excluding it here would let unredacted
    # bytes from a legacy or foreign writer survive the rewrite indefinitely.
    if role != "user":
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
    entry: dict = {
        "role": role,
        "content": content,
        "ts": m.get("ts", ""),
        # "dashboard" is the fallback, not the answer. A channel tab shares the
        # channel's transcript, so the window this re-serializes can hold turns
        # that arrived FROM Slack or Discord with their own recorded origin; the
        # load paths carry that origin onto the in-memory message so it survives
        # the round trip. Hardcoding "dashboard" flattened it on the next flush,
        # making the audit trail claim inbound channel traffic was typed into
        # the dashboard. A message with no recorded origin genuinely IS a
        # dashboard-authored turn, so it keeps these defaults.
        "source_thread": "dashboard",
        "source_user": "dashboard",
    }
    carry_provenance(entry, m)
    if m.get("variants"):
        redacted_variants: list[dict] = []
        for v in m["variants"]:
            if not isinstance(v, dict):
                continue
            vc = v.get("content", "")
            vc, _ = redact_exfiltration_urls(vc)
            vc, _ = redact_credentials(vc)
            redacted_variants.append({**v, "content": vc})
        entry["variants"] = redacted_variants
        entry["variant_idx"] = m.get("variant_idx", 0)
    cls_val = m.get("cls", "")
    if role == "system" and cls_val:
        entry["cls"] = cls_val
    if isinstance(m.get("meta"), dict):
        entry["meta"] = _redact_meta_for_role(role, m["meta"])
    return entry


# Transient/streaming roles that are never persisted (mirrors
# ``_build_message_entry``). A window-region disk line carrying one of these is
# not a real message and is never treated as a cross-process append to preserve.
# Canonically defined in ``state`` (imported above) so the trim path that must
# count durable rows shares the same set; re-exported here unchanged for this
# module's historical readers (session_control, chat_handlers).


def _foreign_tail_ts(foreign_lines: list[str]) -> str | None:
    """The newest parseable ``ts`` among *foreign_lines*, or ``None``.

    Named and single-sourced so "how a slot learns the disk tail" is one thing a
    reader can find, rather than a loop inlined in the save. Sits beside
    :func:`_interleave_foreign_lines` because they consume the same input: those
    lines are on-disk rows this slot never observed, which is exactly why they are
    the rows its ordering floor would otherwise miss.

    Malformed lines are skipped rather than propagated -- a corrupt row must not
    become the floor (``latest_transcript_ts`` refuses unparseable candidates for
    the same reason).
    """
    tail: str | None = None
    for line in foreign_lines:
        try:
            row_ts = json.loads(line).get("ts")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(row_ts, str):
            tail = latest_transcript_ts(tail, row_ts)
    return tail


def _interleave_foreign_lines(
    window_entries: list[dict],
    window_lines: list[str],
    foreign_lines: list[str],
) -> list[str]:
    """Merge this save's window with another writer's lines, in time order.

    A bare ``window + foreign`` concatenation preserves both sets but not the
    conversation: it parks every foreign line after the newest window line. That
    was harmless while foreign appends were rare end-of-file arrivals (a cron
    result landing in a dashboard-only transcript). Once a channel tab shares the
    channel's transcript, foreign lines are ordinary turns of the SAME
    conversation that genuinely happened BETWEEN the window's turns — a channel
    reply that arrived before the user's next dashboard message would be filed
    after it, and the reordered file is what the next turn reads back as context.

    Both sequences are individually already chronological, so this is a two-way
    merge rather than a re-sort: neither side's internal order can change, and a
    line with no parseable ``ts`` inherits the previous key from its own sequence
    so it stays beside the line it was written next to. Exact ties keep the
    window's line first, making the result deterministic.
    """
    if not foreign_lines:
        return window_lines

    def keyed(entries, lines):
        out, last = [], (0, 0.0)
        for entry, line in zip(entries, lines):
            key = transcript_sort_key(entry.get("ts") or "")
            if key[0]:  # unparseable — stay adjacent to the previous line
                key = last
            last = key
            out.append((key, line))
        return out

    parsed_foreign = []
    for line in foreign_lines:
        try:
            parsed_foreign.append(json.loads(line))
        except (ValueError, TypeError):
            # Unparseable bytes are still somebody's acknowledged append: keep
            # them rather than dropping them on the floor.
            parsed_foreign.append({})

    left = keyed(window_entries, window_lines)
    right = keyed(parsed_foreign, foreign_lines)
    merged: list[str] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if right[j][0] < left[i][0]:
            merged.append(right[j][1])
            j += 1
        else:
            merged.append(left[i][1])
            i += 1
    merged.extend(line for _, line in left[i:])
    merged.extend(line for _, line in right[j:])
    return merged


def _frozen_prefix_and_foreign_appends(
    slot: _ChatSlot,
    path,
    disk_older: int,
    window_entries: list[dict],
    *,
    collect_foreign: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Return ``(frozen_prefix, foreign_lines, dedup_dropped)`` for a save.

    ``frozen_prefix`` is the verbatim bytes of the first *disk_older* on-disk
    message lines — the turns OLDER than the in-memory window. They are never
    rewritten, so older history survives a restart that only loaded a recent
    window. The bytes are cached on the slot keyed by ``(mtime, size,
    disk_older)`` so a steady 5s flush is O(window) rather than O(file size).

    ``foreign_lines`` are on-disk message lines in the WINDOW region (the bytes
    after the frozen prefix) that this slot's in-memory *window_entries* do NOT
    represent — i.e. acknowledged appends made by ANOTHER process (subagent /
    cron / CLI) that this slot never saw. ``_save_slot_to_history`` captures its
    ``window`` snapshot BEFORE taking ``_locked``, so a cross-process writer can
    fully append + release between the snapshot and this save acquiring the lock;
    a bare ``meta + frozen + window`` replace would then silently delete that
    acknowledged message. Carrying these lines into the payload makes the save
    non-destructive against cross-process appends. Identity is **id-first**: a
    disk line whose ``meta.mid`` (read via :func:`row_mid`) matches a window
    entry's id, corroborated by body or ``ts`` (same ``(role, content)`` — a
    durable copy — or same ``ts`` — an in-place edit), IS that entry's
    persisted copy, so it is dropped silently (the window re-serializes it)
    and never archived. The corroboration is required because ``meta.mid`` is
    caller-suppliable (``_ChatSlot.append`` preserves a pre-existing id), so a
    bare id equality could pair two genuinely distinct messages; an id match
    with NO corroborating entry falls back to the legacy ladder as if id-less,
    which typically preserves the line. A disk line whose ``meta.mid`` matches
    NO available window entry is a foreign append regardless of body equality,
    which is what tells two genuinely distinct identical-content messages
    apart — it still keeps its ``ts`` group ambiguous for the ts-only tier, so
    its presence can never convert a contested group into a silent id-less
    fold. Only an **id-less** disk line resolves
    through the legacy timestamp-first ladder, unchanged: it is treated as
    ours when
    its ``ts`` matches a window entry (covers in-place edits, which keep ``ts``
    but change content) OR — as a COUNT-BOUNDED tiebreak — its
    ``(role, content)`` matches an as-yet-unconsumed window entry (covers a
    same-process ``append_if_absent`` copy persisted with a FRESH ``ts``
    distinct from the window entry's in-memory ``ts``). The tiebreak is bounded
    so each window entry absorbs AT MOST ONE disk copy: if the on-disk window
    region holds two id-less lines with identical ``(role, content)`` but
    distinct timestamps — the window's own persisted copy PLUS a genuinely
    distinct event from another process (e.g. a repeated identical cron /
    workflow result) — only the first is folded into the window and the second
    is preserved as a foreign append. A plain ``(role, content)`` set collapsed
    those two real events into one; the bounded, timestamp-first identity
    fixes it for id-less lines, and the ``meta.mid`` tier resolves it exactly
    for stamped lines (see also ``docs/system-specs/modules/history.md``).
    ``dedup_dropped`` returns any fresh-``ts`` content-tiebreak drops so the
    caller can route them through the archive — even the residual ambiguous
    case (an id-less distinct message indistinguishable from an
    ``append_if_absent`` copy without a stable id) then loses no data
    permanently. A corroborated id-matched fold is NOT such a drop: the ids
    plus body/``ts`` agreeing makes it unambiguous, so it does not churn the
    archive.

    Fast path: when BOTH the on-disk mtime AND size match the frozen-prefix
    cache, THIS slot was the last writer and nothing has landed since, so the
    prefix is served from cache and the foreign lines preserved by the previous
    save are re-emitted verbatim from cache — the O(file) read/scan runs ONLY
    when the file changed on disk since our last write. Size is part of the key
    because an append always grows the file even inside a single coarse mtime
    tick, so mtime alone is not a safe change signal for a data-loss guard.
    Re-emitting the cached foreign lines (rather than assuming there are none)
    is what makes the fast path non-destructive: a previous save may have
    preserved a cross-process append INTO the on-disk window region, and since
    ``disk_older`` is unchanged those preserved lines would otherwise be dropped
    by a bare frozen-prefix + in-memory-window rebuild on the very next save.

    Returns ``("", [])`` when the file is missing/unreadable/has no metadata line.
    """
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return ("", [], [])
    cache = slot._frozen_prefix_cache
    if cache is not None and cache[0] == mtime and cache[1] == size and cache[2] == disk_older:
        # File is byte-identical to our last write → prefix AND the foreign
        # lines that write preserved are both served from cache. Returning the
        # cached foreign lines (a copy, so the caller cannot mutate the cache)
        # keeps the fast path non-destructive: the previously-preserved
        # cross-process append is re-emitted instead of silently dropped. No
        # scan runs, so there are no fresh dedup drops to archive.
        return (cache[3], list(cache[4]), [])
    try:
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return ("", [], [])
    if not existing or '"_type"' not in existing[0]:
        return ("", [], [])
    body = existing[1:]  # message lines only (metadata excluded)
    prefix = "".join(body[:disk_older]) if disk_older > 0 else ""
    if not collect_foreign:
        # Rewrite (rewind / regenerate / fork) INTENTIONALLY truncates the
        # window, so a disk window-region line absent from the (truncated) window
        # is ambiguous between a rewound tail (must drop) and a cross-process
        # append (must keep). Those edits are same-session/same-process (not the
        # cross-process loss this scan guards), so skip the scan and let the
        # rewrite's archive-diff handle the dropped tail. Cache with no foreign
        # lines so a subsequent fast path re-emits nothing extra.
        slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, [])
        return (prefix, [], [])
    # Scan the on-disk window region for lines the in-memory window does not
    # carry — those are cross-process appends we must preserve. Identity is
    # id-first (``meta.mid``, the stable per-message id every window append
    # mints and every durable-copy writer carries through), with the legacy
    # timestamp-first ladder — exact triple, ts, then a COUNT-BOUNDED
    # (role, content) tiebreak — retained unchanged for id-less lines (see the
    # module docstring / history.md).
    #
    # Build COUNT-BOUNDED consumption budgets over the window entries so each
    # on-disk window-region line is matched to AT MOST ONE window entry and each
    # window entry absorbs AT MOST ONE disk line. Identity is checked in four
    # tiers of decreasing confidence:
    #   (0) ``meta.mid`` — the stable id stamped at append time and carried onto
    #       durable copies (PR #5133); an id match IS the same message, resolved
    #       first across ALL disk lines so no heuristic tier can steal the
    #       entry, and an id-carrying line whose id matches NO entry is foreign
    #       regardless of body (two distinct identical-content messages carry
    #       distinct ids);
    #   (a) exact (ts, role, content) — an unchanged re-serialization (the common
    #       steady-save case), resolved before the ts/rc passes so a greedy
    #       edit/tiebreak match can never steal an entry a later exact line needs;
    #   (b) ts only — an in-place edit (same ``ts``, changed content: window wins);
    #   (c) (role, content) only — a same-content copy persisted with a FRESH
    #       ``ts`` (the ``append_if_absent`` case), routed to the archive.
    # Tiers (a)-(c) see only id-less disk lines, but every window entry stays
    # indexed in all of them regardless of whether it carries an id: a legacy
    # (pre-id) disk line must still fold into its window row even though the
    # restore minted that row a fresh id.
    # Keying every tier by COUNT (deques of entry indices guarded by a shared
    # ``consumed`` flag) — rather than a ``ts -> entry`` dict plus a per-``ts``
    # ``set`` — is what makes this correct when several messages share one ``ts``.
    # Coarse system clocks (notably Windows' ~15ms tick) can stamp a burst of
    # rapid appends with an IDENTICAL ``datetime.now().isoformat()``; the old
    # dict/set collapsed those colliding-``ts`` entries to a single slot, so a
    # genuine window line was mis-classified as a foreign append and DUPLICATED on
    # disk. The bounded multiset below matches them one-for-one regardless of
    # ``ts`` collisions.
    mid_idx: dict[str, "deque[int]"] = {}
    exact_idx: dict[tuple[object, object, object], "deque[int]"] = {}
    ts_idx: dict[object, "deque[int]"] = {}
    rc_idx: dict[tuple[object, object], "deque[int]"] = {}
    for _i, e in enumerate(window_entries):
        _ets = e.get("ts")
        _erole = e.get("role")
        _econtent = e.get("content", "")
        _emid = row_mid(e)
        if _emid:
            mid_idx.setdefault(_emid, deque()).append(_i)
        if _ets:
            exact_idx.setdefault((_ets, _erole, _econtent), deque()).append(_i)
            ts_idx.setdefault(_ets, deque()).append(_i)
        rc_idx.setdefault((_erole, _econtent), deque()).append(_i)
    consumed = [False] * len(window_entries)

    def _take(dq: "deque[int] | None") -> bool:
        """Consume the first not-yet-consumed entry index in ``dq`` (if any)."""
        if not dq:
            return False
        while dq:
            _idx = dq.popleft()
            if not consumed[_idx]:
                consumed[_idx] = True
                return True
        return False

    # Parse the on-disk window-region lines once (skipping blank/corrupt/transient
    # lines exactly as before), so the matching passes share one parse.
    disk_msgs: list[tuple[str, object, object, object, str | None]] = (
        []
    )  # (norm, ts, role, content, mid)
    for ln in body[disk_older:]:
        if not ln.strip():
            continue
        try:
            entry = json.loads(ln)
        except ValueError:
            continue  # corrupt window-region line — not a preservable message
        if not isinstance(entry, dict) or entry.get("_type") == "metadata":
            continue
        role = entry.get("role")
        if role is None or role in _TRANSIENT_ROLES:
            continue
        norm = ln if ln.endswith("\n") else ln + "\n"
        disk_msgs.append((norm, entry.get("ts"), role, entry.get("content", ""), row_mid(entry)))

    # Pass 0 — ``meta.mid``: id-first identity, resolved across ALL disk lines
    # before any heuristic tier so a greedy lower-confidence match can never
    # steal a window entry whose persisted copy is identified by id. An id
    # match folds ONLY when corroborated by body or ``ts`` — ``meta.mid`` is
    # caller-suppliable (``_ChatSlot.append`` preserves a pre-existing id, and
    # the ``/api/chat`` meta rides through), so a bare id equality is not proof
    # of sameness the way a minted-uuid contract would suggest:
    #   * corroborated (same (role, content) — a durable copy — or same ``ts``
    #     — an in-place edit): consume the entry and drop the line (the window
    #     re-serializes it). The ids matching exactly makes this NOT a dedup
    #     drop, so it is not routed to the ``foreign-dedup`` archive.
    #   * id matches an unconsumed entry but NEITHER body nor ``ts`` agrees
    #     (an id reused across two genuinely distinct messages): leave the
    #     entry unconsumed and let the line fall through to the legacy tiers
    #     as if id-less — typically preserved as foreign, so the distinct
    #     message stays in the transcript rather than being silently folded.
    #   * id matches NO available window entry (unknown id, or every same-id
    #     entry already absorbed its one copy): FOREIGN regardless of body
    #     equality — two genuinely distinct identical-content messages (e.g. a
    #     cron reporting the same status text twice) carry distinct ids, which
    #     is exactly what the body tiebreak could never tell apart — so it is
    #     excluded from the heuristic tiers below (``mid_foreign``) and
    #     preserved, in disk order, by pass 2.
    # Id-less lines fall through with tier (a)-(c) behaviour unchanged.
    handled = [False] * len(disk_msgs)
    mid_foreign = [False] * len(disk_msgs)
    for _j, (_norm, _ts, _role, _content, mid) in enumerate(disk_msgs):
        if mid is None:
            continue
        _live = [_i for _i in mid_idx.get(mid, ()) if not consumed[_i]]
        if not _live:
            mid_foreign[_j] = True
            continue
        for _i in _live:
            _e = window_entries[_i]
            if (_role, _content) == (_e.get("role"), _e.get("content", "")) or (
                _ts and _ts == _e.get("ts")
            ):
                consumed[_i] = True
                handled[_j] = True
                break
        # No corroborated entry → deliberate fallthrough to the legacy tiers.

    # Pass 1 — exact (ts, role, content) over id-less lines: unambiguously our
    # own unchanged re-serialization. Resolving these before the ts/rc passes
    # makes the result independent of the disk-line order (an earlier
    # edit/tiebreak match can no longer consume an entry that a later exact
    # line requires).
    for _j, (_norm, ts, role, content, _mid) in enumerate(disk_msgs):
        if handled[_j] or mid_foreign[_j]:
            continue
        if ts and _take(exact_idx.get((ts, role, content))):
            handled[_j] = True

    foreign: list[str] = []
    dedup_dropped: list[str] = []
    # After the exact pass, an in-place EDIT (same ``ts``, changed content) is the
    # only legitimate reason to drop a still-unmatched disk line by ``ts`` alone.
    # But under COLLIDING timestamps a ts-only match is AMBIGUOUS: a foreign
    # cross-process append that happens to share the ``ts`` is indistinguishable
    # from an edited window entry, and greedily consuming the ts budget would
    # silently DROP that acknowledged foreign line (data loss) — the exact guard
    # this scan exists to uphold. So restrict ts-only matching to the UNAMBIGUOUS
    # singleton case: a ``ts`` carried by EXACTLY ONE still-unmatched window entry
    # AND EXACTLY ONE still-unmatched disk line. Any ts group with more than one
    # unmatched line on either side is ambiguous, so its disk lines fall through
    # to the content tiebreak / foreign preservation below (favouring a rare
    # duplicate over irreversible data loss). Counts are taken from the
    # post-exact-pass state and are static for pass 2 (the ``consumed`` guard in
    # ``_take`` still prevents any double-consumption).
    w_unmatched_ts: dict[object, int] = {}
    for _i, e in enumerate(window_entries):
        _wt = e.get("ts")
        if _wt and not consumed[_i]:
            w_unmatched_ts[_wt] = w_unmatched_ts.get(_wt, 0) + 1
    d_unmatched_ts: dict[object, int] = {}
    for _j, (_norm, ts, _role, _content, _mid) in enumerate(disk_msgs):
        if ts and not handled[_j]:
            d_unmatched_ts[ts] = d_unmatched_ts.get(ts, 0) + 1

    # Pass 2 — for still-unmatched disk lines: ts-only (UNAMBIGUOUS in-place edit)
    # then the bounded (role, content) tiebreak, else genuinely foreign. A line
    # pass 0 already ruled foreign by id bypasses both heuristics (its identity
    # is settled) but is emitted HERE so ``foreign`` keeps disk order — the
    # interleave that re-merges these lines breaks ts ties by adjacency, so
    # reordering them relative to other foreign lines is not harmless. Such a
    # line still counts in ``d_unmatched_ts`` above: it keeps its ``ts`` group
    # ambiguous exactly as it did before the id tier existed, so an id-less
    # line sharing the ``ts`` is preserved (a rare stale duplicate) rather
    # than silently ts-folded into an entry the id-foreign line proves
    # contested (an irreversible drop of an acknowledged append).
    for _j, (norm, ts, role, content, _mid) in enumerate(disk_msgs):
        if handled[_j]:
            continue
        if mid_foreign[_j]:
            foreign.append(norm)
            continue
        # ts-match: an in-place edit keeps the ``ts`` but changes content, so the
        # window's version wins and the disk line is dropped silently — but ONLY
        # when the ``ts`` group is an unambiguous 1:1 (else a colliding foreign
        # append could be mistaken for the edit and lost).
        if (
            ts
            and w_unmatched_ts.get(ts, 0) == 1
            and d_unmatched_ts.get(ts, 0) == 1
            and _take(ts_idx.get(ts))
        ):
            continue
        # content tiebreak (bounded): a window entry with this exact
        # (role, content) that no match already consumed absorbs this disk copy —
        # the ``append_if_absent`` fresh-``ts`` case. A drop carrying a DISTINCT
        # non-empty ``ts`` is the genuinely ambiguous case (it could be a distinct
        # message we cannot tell apart without a stable id), so route it through
        # the archive; a ts-less / matching re-serialization is a plain window
        # copy and is dropped silently to avoid archive spam.
        if _take(rc_idx.get((role, content))):
            if ts:
                dedup_dropped.append(norm)
            continue
        # genuinely foreign → preserve verbatim.
        foreign.append(norm)
    # Cache the frozen prefix AND the foreign lines together, keyed on the
    # as-read (mtime, size). If this save's atomic_write later fails, the file
    # on disk is unchanged, so a subsequent save that re-reads the same
    # (mtime, size) must re-emit these same preserved foreign lines rather than
    # drop them — hence they are cached here, not just at the post-write site.
    slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, foreign)
    return (prefix, foreign, dedup_dropped)


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
    rows_only: bool = False,
) -> bool:
    """Persist slot messages to JSONL history (append-safe).

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

    Returns ``False`` only when the delete-won guard aborted the save because
    the session was permanently deleted while this save awaited the lock — the
    in-memory window was NOT persisted and must not be treated as durable.
    Every other completion (including the benign no-op skips) returns ``True``.
    """
    if not state.conversation_log:
        return True
    # An explicit message snapshot always means "this is the full authoritative
    # window state" → rewrite. Edit paths (rewind/regenerate/fork) pass a snapshot.
    # A slot left in _pending_rewrite by a failed inline rewrite also takes
    # the archive-safe rewrite path until it succeeds.
    if messages is not None or slot._pending_rewrite:
        rewrite = True
    # Snapshot the window and its disk-older count CONSISTENTLY. The save
    # may run in the flush executor thread while _flush_segment (reassigns
    # slot.messages) or append (trims the front AND bumps _disk_older_count)
    # run on the event loop. A trim is the only mutation that changes the
    # window/_disk_older_count relationship, so we read _disk_older_count,
    # snapshot the window, then confirm _disk_older_count is unchanged; a small
    # bounded retry closes the race without locks (slot._lock is an asyncio.Lock
    # and so cannot be acquired from this thread). An explicit snapshot is
    # already consistent by construction.
    if messages is not None:
        window = list(messages)
        disk_older = slot._disk_older_count
    else:
        for _ in range(_FLUSH_SNAPSHOT_RETRIES):
            disk_older = slot._disk_older_count
            window = list(slot.messages)
            if slot._disk_older_count == disk_older:
                break
        else:
            disk_older = slot._disk_older_count
            window = list(slot.messages)
    # Filter the SNAPSHOT, never slot.messages: this may run in the flush
    # executor thread, where mutating the live window is exactly the race the
    # snapshot above exists to avoid. A note row whose slot was rebound after
    # the write must not be persisted into the session it now routes to; the
    # drain drops it from the live window on the event loop.
    #
    # Authorization and the write target must come from ONE observation of the
    # routing. Both keys derive from ``slot.linked_session_key``, which the event
    # loop rebinds with no running gate, so reading it per row -- or again when
    # the write target is resolved -- authorizes rows against one session and
    # then writes the file of another. Snapshot-then-confirm with the same
    # bounded retry this function already uses for the window pair. The two keys
    # stay DISTINCT: collapsing them would send a channel-born slot the
    # dashboard could not bind to the phantom file ``slot_history_key`` exists
    # to avoid.
    for _ in range(_FLUSH_SNAPSHOT_RETRIES):
        routing = getattr(slot, "linked_session_key", "")
        note_auth_key = effective_session_key(slot)
        history_key = slot_history_key(slot)
        if getattr(slot, "linked_session_key", "") == routing:
            break
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
    kept = [m for m in window if not _note_authorized_elsewhere(m.get("meta"), note_auth_key)]
    dropped_notes = len(window) - len(kept)
    window = kept
    if dropped_notes:
        # Count-gated exactly like the drain's own denial at state.py:2320. This is
        # the PERIODIC save path, so an ungated emit would record a denial on every
        # save of every slot, and the same row would re-emit on each one until the
        # loop-side drain removes it from slot.messages. ``critical`` stays default
        # False: a denial must never be able to fail a save that is otherwise
        # correct. Nothing raises or returns early here either, so the authorized
        # remainder still persists. Called directly rather than through loop
        # plumbing because sel is pure threading -- unlike the asyncio lock named
        # above, it is safe from this executor thread. Only the slot key and a count
        # are recorded; note content never enters the audit line.
        sel().log_api_access(
            caller="dashboard",
            operation="note_save_drop",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key} dropped={dropped_notes}",
            error="slot was rebound to another session after the note was written",
        )
        logger.warning(
            "Slot %s dropped %d note row(s) from a save: authorized elsewhere, "
            "slot now routes to %s",
            slot.key,
            dropped_notes,
            note_auth_key,
        )
    if not window:
        if force or closed:
            # A FORCED (or closing) save of a message-less slot is a metadata
            # mutation (folder filing/unfiling, a tag assignment, a pin, a
            # pinned title, a mode switch, a close) -- the full save below has no window to
            # write, but the mutation still has to reach disk. This became
            # reachable when `session_create` started persisting `folder_id`
            # at birth (#6118): an empty newborn HAS a metadata line, so any
            # acknowledged metadata change before its first message must
            # overwrite that line, or a restart resurrects the birth state the
            # user already changed. The merge carries every slot-owned field a
            # force/closed save is responsible for -- not just `folder_id`:
            # the tag routes, the pin route, the recreate PATCH (folder or
            # pinned title) and the close path all persist ONLY through this
            # save, so a folder-only merge would silently drop their
            # acknowledged writes on restart. Merged ONLY into an existing
            # line: a slot with no line at all does not survive a restart, so
            # there is nothing to reconcile, and materializing files for every
            # empty tab here would create transcripts nothing else expects.
            # The existence guard runs INSIDE the same cross-process lock as
            # the merge (`update_metadata_if`): the plain update is an upsert,
            # so a checked-then-written pair would let a permanent deletion
            # land between the two and be resurrected as a fresh file.
            # Clearable fields are written even when empty -- the merge cannot
            # delete a key, and rehydrate treats a falsy value as cleared
            # (unfiled / untagged / unpinned / untitled / default mode). Fails
            # closed on an unreadable record, per `update_metadata_if`'s own
            # contract.
            def _fresh_fields() -> dict:
                # Mirrors the FULL save's slot-owned enumeration (the
                # ``meta_line`` construction below), so a forced save of an
                # empty slot persists exactly what a forced save of a
                # non-empty slot would persist for the metadata line -- the
                # invariant that keeps this branch from silently dropping
                # whichever acknowledged mutation a route happens to persist
                # through it (folder, tags, pin, title, mode, project,
                # artifact binding, ...). Two write classes, matching
                # rehydrate's semantics:
                # - CLEARABLE fields are written even when empty (the merge
                #   cannot delete a key; rehydrate treats a falsy value as
                #   cleared: unfiled / untagged / unpinned / untitled /
                #   default mode / unbound artifact / uncolored).
                # - IDENTITY and MONOTONIC fields are written only when
                #   truthy, exactly like the full save (origin's fail-closed
                #   sentinel and the once-flags must never be erased by a
                #   writer that has not learned them).
                fields: dict = {
                    "folder_id": slot.folder_id or "",
                    "tags": list(slot.tags),
                    "pinned": bool(slot.pinned),
                    "mode": slot.mode or "",
                    "artifact": slot._artifact or "",
                    "reasoning_effort": slot.reasoning_effort or "",
                    "color_index": slot.color_index,
                    "color_hex": slot.color_hex or "",
                    "color_theme": slot.color_theme or "",
                    "memory_mode": slot.memory_mode,
                    "model": slot.model,
                    # None means "follow the global threshold" and is the
                    # cleared value (rehydrate reads it with ``is not None``),
                    # so the override is CLEARABLE: written even when None,
                    # like the other clearable fields above.
                    "autocompact_pct": slot.autocompact_pct,
                }
                if slot.title and slot.title != slot.key:
                    fields["title"] = slot.title
                    # Persist the title's provenance next to it (mirrors the
                    # full save): without it rehydration conservatively
                    # re-classifies an auto title as "user" and locks the
                    # refresh out.
                    _origin = getattr(slot, "_title_origin", "")
                    if _origin:
                        fields["title_origin"] = _origin
                    _mark = getattr(slot, "_title_refresh_mark", 0)
                    if _mark:
                        fields["title_refresh_mark"] = _mark
                else:
                    fields["title"] = ""
                if slot.agent:
                    fields["agent"] = slot.agent
                if slot.workspace:
                    fields["workspace"] = slot.workspace
                if slot.project:
                    fields["project"] = slot.project
                if slot._app:
                    fields["app"] = slot._app
                if slot._origin:
                    fields["origin"] = slot._origin
                if getattr(slot, "_created_by", ""):
                    # Creator attribution: the member ownership boundary in
                    # session-control authorization reads it, so dropping it here
                    # would orphan a member's workers on the next restart.
                    fields["created_by"] = slot._created_by
                if slot.linked_session_key:
                    fields["linked_session_key"] = slot.linked_session_key
                if getattr(slot, "channel_origin", False):
                    fields["channel_origin"] = True
                if slot.forked_from is not None:
                    fields["forked_from"] = slot.forked_from
                if slot.executor == "remote" and slot.instance_id and slot.remote_slot:
                    # All three or none, exactly like the full save: a newborn
                    # bound to a peer has an EMPTY window until the first relayed
                    # row lands, so this merge is the only writer its binding
                    # ever sees. Dropping it here means a restart in that window
                    # brings the session back as an ordinary local one and the
                    # next turn runs on this machine instead of the crew the user
                    # picked. The completeness guard keeps the fail-closed
                    # invariant: a half-binding is never written, so rehydration
                    # never has to repair one.
                    fields["executor"] = "remote"
                    fields["instance_id"] = slot.instance_id
                    fields["remote_slot"] = slot.remote_slot
                    if getattr(slot, "_relay_in_flight", False):
                        # Only ever written while a turn is mid-flight; the relay
                        # clears it when the turn ends, so a persisted True means
                        # "crashed mid-turn" on reload. Nested under the binding
                        # because it is meaningless without one.
                        fields["relay_in_flight"] = True
                if getattr(slot, "_tab_id", None):
                    fields["tab_id"] = slot._tab_id
                if getattr(slot, "_auto_tagged", False):
                    # Once-flag, monotonic (see the full save): written when
                    # set, never cleared.
                    fields["auto_tagged"] = True
                if getattr(slot, "_human_seen", False):
                    fields["human_seen"] = True
                if slot._channel_folder_filed:
                    # Sticky like the full save; the disk-carry half is
                    # inherent here since a merge never deletes a key.
                    fields["channel_folder_filed"] = True
                if closed:
                    # Without this a closed empty newborn's line stays
                    # open-shaped and the next restart resurrects a tab the
                    # user dismissed.
                    fields["closed"] = True
                    fields["closed_at"] = closed_at if closed_at is not None else time.time()
                return fields

            # The slot fields are read INSIDE the guard, which
            # `update_metadata_if` evaluates under the cross-process lock at
            # write time -- exactly the contract that method exists for ("the
            # decision is re-made here rather than trusted from before the
            # lock"). A dict snapshotted before the lock could commit out of
            # order: a tag save that snapshotted `pinned=False` before a
            # concurrent pin request committed `pinned=True` would land its
            # stale aggregate second and silently revert the acknowledged pin.
            # The full save has the same shape -- it builds its metadata line
            # from slot state inside the locked block -- so whichever writer
            # commits last writes the newest slot state.
            merged_fields: dict = {}
            guard_state = {"ran": False}

            def _refresh_under_lock(meta: dict) -> bool:
                guard_state["ran"] = True
                if not meta:
                    return False
                merged_fields.clear()
                merged_fields.update(_fresh_fields())
                return True

            applied = state.conversation_log.update_metadata_if(
                history_key,
                merged_fields,
                _refresh_under_lock,
            )
            if not applied and not guard_state["ran"]:
                # `update_metadata_if` fails CLOSED on an unreadable record
                # WITHOUT invoking the guard -- that is a failed write, not the
                # by-design skip for a line-less tab (where the guard runs and
                # sees an empty record). Returning True here would report a
                # merge that never happened as durable: a close would remove
                # the tab while the on-disk line stays open-shaped and the
                # next restart resurrects it. Raise instead, matching the save
                # contract: best-effort callers log + mark the slot dirty, and
                # archival callers (close, best_effort=False) roll back and
                # keep the slot.
                raise OSError(
                    f"empty-window metadata merge skipped: record unreadable for {history_key}"
                )
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
        with state.conversation_log._locked(history_key):
            # Status form, not bare ``get_metadata``: the delete-won identity
            # comparison below is exactly the "empty result triggers something
            # destructive" case that ``get_metadata_status`` exists for — a
            # transient read failure returns ``{}`` from the bare getter,
            # indistinguishable from "no metadata", which would blank the
            # identity check and let a pending save overwrite a replacement
            # session with deleted content.
            existing_meta, _meta_readable = state.conversation_log.get_metadata_status(history_key)

            path = state.conversation_log._path(history_key)
            # ── Delete-won guard ────────────────────────────────────────────
            # ``delete_session`` unlinks the session file under the SAME
            # ``_locked`` region and deliberately leaves no tombstone (its
            # docstring notes a concurrent writer can recreate the session
            # once it releases the lock). ``save_slot_off_loop`` routes
            # on-loop callers to a worker thread that takes the PATIENT
            # acquire, so this save can legitimately sit waiting while a
            # permanent delete runs to completion ahead of it — writing now
            # would silently undo a delete that already reported success,
            # resurrecting the conversation in Older Sessions. A missing
            # file alone is NOT that signal: a brand-new slot's first save
            # also starts with no file. The abort therefore requires
            # evidence that this slot's session HAS been on disk before —
            # it was resumed from history (``_resumed_count``), its window
            # has older lines on disk (``disk_older``), or one of this
            # slot's own saves already committed (``_disk_window_len``).
            # A fresh slot has none of these and proceeds with a normal
            # first create. (``path`` is resolved after the delete, so for a
            # legacy-aliased Slack key it may name the canonical file rather
            # than the legacy one the delete unlinked — both are gone, so
            # the answer is the same.) Returning cleanly (no mkdir, no
            # write, no raise) lets the flush loop clear ``_dirty`` so the
            # delete's reported success stands; the ``False`` return lets
            # callers that must CONFIRM durability (fork, transfer export)
            # distinguish this skip from a committed write. Only a missing
            # file counts as the delete witness — any other ``stat`` failure
            # (permissions, device not ready) propagates to the outer
            # handler, which re-raises and leaves the retry armed.
            _delete_won = False
            # Evidence is the slot having OBSERVED its file on disk, and
            # ``_disk_meta_created_at`` is that observation: recorded exactly
            # at the hydrate sites and at each committed save, nowhere else.
            # Identity ALONE is the gate. The window counters take no part in
            # it, in either direction: fork/transfer set ``_resumed_count``
            # optimistically after a best-effort first save (a transient
            # failure would read as "was on disk, now gone" and eat the retry
            # best-effort re-armed), and a restored ZERO-message session has
            # all-zero counters while its delete must still win against the
            # save of its first message.
            _known = slot._disk_meta_created_at
            # ``created_at`` is the identity, but legacy metadata carries none
            # — the observation BIT is the evidence there, so a save racing a
            # permanent delete cannot recreate a legacy transcript through the
            # "no identity recorded" gap. The missing-file witness needs only
            # the observation; the identity COMPARISON below still needs the
            # recorded ``created_at``.
            if _known or slot._disk_meta_observed:
                try:
                    path.stat()
                except FileNotFoundError:
                    _delete_won = True
                else:
                    # The file EXISTS but may not be the one this slot knows: a
                    # permanent delete followed by another writer's append (a
                    # channel/cron ``append_off_loop``) creates a FRESH file
                    # with a new metadata ``created_at``. Merging this slot's
                    # window into that file would restore the deleted
                    # conversation into the new transcript. ``created_at`` is
                    # the file's identity — the save always carries the on-disk
                    # value forward, so for a continuously-existing file it
                    # never changes. An UNREADABLE metadata line fails CLOSED:
                    # the identity cannot be verified, so the save must not
                    # proceed — raising (rather than returning False) leaves
                    # ``_dirty`` armed via the outer handler, so the flush
                    # retries once the transient read failure clears, instead
                    # of the delete-won path discarding the content. A
                    # readable-but-absent ``created_at`` (legacy meta) fails
                    # open for an EXISTING file only — a missing file is the
                    # legacy delete witness via the observation bit above.
                    if not _meta_readable:
                        raise OSError(
                            f"history metadata for {history_key} is transiently "
                            "unreadable; cannot verify the session's identity "
                            "before writing — save deferred for retry"
                        )
                    _current = str(existing_meta.get("created_at") or "")
                    # Compare identities only when one was RECORDED: a legacy
                    # observation (``_known`` empty) cannot distinguish "the
                    # same legacy file, stamped with a ``created_at`` by a
                    # sibling's save since" from "a fresh incarnation born
                    # after a delete" — fail open for the existing file,
                    # matching the documented legacy behavior above. The
                    # missing-file witness is the legacy delete evidence.
                    if _known and _current and _current != _known:
                        _delete_won = True
            if _delete_won:
                # WARNING, with the slot key: for a slot the delete's cleanup
                # could not pop (e.g. a cron-linked tab whose slot key does not
                # match any spelling the cleanup probes), every later save of
                # new activity aborts here — the slot's in-memory content is
                # no longer durable, and this line is the only operator-visible
                # evidence of that.
                logger.warning(
                    "Skipping history save for %s (slot=%s): the session was "
                    "permanently deleted while this save awaited the lock",
                    history_key,
                    slot.key,
                )
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            meta_line: dict = {
                "_type": "metadata",
                "created_at": existing_meta.get("created_at") or slot.created_at,
                "last_consolidated": existing_meta.get("last_consolidated", 0),
            }
            # Preserve history-layer-owned metadata this dashboard save does NOT
            # manage. The save is authoritative only for the slot fields it writes
            # (SLOT_OWNED_META_KEYS), where an absent field means "cleared"; every
            # other key is another layer's durable state, and reconstructing the
            # subset deletes it. That is not hypothetical: it erased the rotation
            # generation (re-opening the consolidation race the generation check
            # closed) and then the consolidation retry accounting (resetting the
            # backoff so billed retries resumed). Carrying unowned keys through by
            # default closes the class instead of enumerating one more field to
            # rescue. Applied after the slot fields below so an inherited value can
            # never shadow the slot's own state.
            if closed:
                meta_line["closed"] = True
                # Epoch stamp of WHEN the tab was closed. The channel-slot
                # reconciler compares channel-side activity against this to
                # decide whether a close still stands: a Discord/Slack message
                # arriving after the close re-surfaces the conversation, while
                # a conversation that stayed idle stays closed.
                #
                # Prefer the caller-supplied instant (captured by
                # note_slot_closed at the moment the user acted): this save
                # runs only after the close handler's awaits (task
                # cancellation, patient lock acquire), and stamping save time
                # here would make channel activity that landed during that
                # teardown window compare as OLDER than the close — hiding a
                # conversation the reactivation rule should surface. The
                # save-time fallback covers callers with no user gesture to
                # anchor to (and legacy call sites).
                meta_line["closed_at"] = closed_at if closed_at is not None else time.time()
            meta_line["memory_mode"] = slot.memory_mode
            if slot.title and slot.title != slot.key:
                meta_line["title"] = slot.title
                # Persist the title's provenance next to it (mirrors
                # _persist_title): without this, the canonical full save would
                # strip the field and rehydration would conservatively
                # re-classify an auto title as "user" after restart —
                # permanently locking the background refresh out.
                _origin = getattr(slot, "_title_origin", "")
                if _origin:
                    meta_line["title_origin"] = _origin
                _mark = getattr(slot, "_title_refresh_mark", 0)
                if _mark:
                    meta_line["title_refresh_mark"] = _mark
            if slot.agent:
                meta_line["agent"] = slot.agent
            meta_line["model"] = slot.model
            if slot.reasoning_effort:
                meta_line["reasoning_effort"] = slot.reasoning_effort
            # Unconditional, matching the empty-window merge mirror: None is
            # the cleared "follow the global" value, not an absent field.
            meta_line["autocompact_pct"] = slot.autocompact_pct
            if slot.mode:
                meta_line["mode"] = slot.mode
            if slot.workspace and slot.workspace != "default":
                meta_line["workspace"] = slot.workspace
            if slot.project:
                meta_line["project"] = slot.project
            # Remote-execution binding. All three are written together or not at
            # all: a half-restored binding (executor="remote" with no peer slot)
            # is the fail-closed refusal case, so persisting the marker without
            # its target would resurrect a session that can never run. Written
            # only when the whole binding is present, and read back the same way.
            if slot.executor == "remote" and slot.instance_id and slot.remote_slot:
                meta_line["executor"] = "remote"
                meta_line["instance_id"] = slot.instance_id
                meta_line["remote_slot"] = slot.remote_slot
                if getattr(slot, "_relay_in_flight", False):
                    # See the merge-save site: written only while a turn is
                    # in-flight, so a True read back on reload is the crash signal
                    # that triggers the interrupted-turn row.
                    meta_line["relay_in_flight"] = True
            if slot.folder_id:
                meta_line["folder_id"] = slot.folder_id
            if slot._channel_folder_filed or existing_meta.get("channel_folder_filed"):
                # Sticky, and carried forward from disk rather than only from the
                # slot: this function rebuilds the metadata line from scratch, so
                # a restore path that failed to set the in-memory flag would
                # otherwise ERASE the marker on the next save and the
                # conversation would be re-filed. Preserving the on-disk value
                # makes that whole class of omission harmless — same reason
                # rotation_generation is carried forward above.
                meta_line["channel_folder_filed"] = True
            if slot._app:
                meta_line["app"] = slot._app
            # Slot ORIGIN (user / app / cron) must round-trip with ``app``:
            # the rehydrate paths restore ``origin=meta.get("origin", "")`` and an
            # untagged restore falls back to the fail-closed empty sentinel. Without
            # this write every slot would come back unattributed after a restart —
            # ``slots:user`` subscribers would stop seeing user slots, and a cron
            # slot would lose the CRON tag that keeps it out of ``slots:user``.
            if slot._origin:
                meta_line["origin"] = slot._origin
            if getattr(slot, "_created_by", ""):
                # Creator attribution — read by the member ownership boundary in
                # session-control authorization; see the partial-save mirror above.
                meta_line["created_by"] = slot._created_by
            # Artifact companion binding — persisted so a bound
            # session restored after a gateway restart (or resumed from the
            # History page) comes back as the artifact's active bound session.
            if slot._artifact:
                meta_line["artifact"] = slot._artifact
            if slot.pinned:
                meta_line["pinned"] = True
            if slot.color_index is not None:
                meta_line["color_index"] = slot.color_index
            if slot.color_hex:
                meta_line["color_hex"] = slot.color_hex
            if slot.color_theme:
                meta_line["color_theme"] = slot.color_theme
            if slot.tags:
                meta_line["tags"] = list(slot.tags)
            if getattr(slot, "_auto_tagged", False):
                # Once-flag for project auto-tagging: without it a restart
                # re-runs maybe_auto_tag and silently re-adds a tag the user
                # removed (see chat_auto_tag.maybe_auto_tag).
                meta_line["auto_tagged"] = True
            if getattr(slot, "_human_seen", False):
                # Once-flag for attendance (state._ChatSlot.unattended). Without
                # it a restart drops an app-owned tab a person is working in from
                # the 2h approval window to the 180s deny-fast — a gateway
                # restart happens on every upgrade and is not evidence the person
                # left. Monotonic like auto_tagged above, so it is written when
                # set and never cleared; both are therefore absent from
                # SLOT_OWNED_META_KEYS and survive via carry_unowned_metadata
                # even on a save by a slot that has not learned the flag yet.
                meta_line["human_seen"] = True
            if slot.forked_from is not None:
                meta_line["forked_from"] = slot.forked_from
            if slot.linked_session_key:
                # The slot's conversation lives on another session (a channel
                # thread, a cron job). Nothing recreates that binding on
                # restart for a channel slot — no injection re-fires — so
                # without persisting it the slot rehydrates unbound and
                # silently reverts to a dashboard-only copy of the thread.
                meta_line["linked_session_key"] = slot.linked_session_key
            if getattr(slot, "channel_origin", False):
                # Durable provenance. Without it the restore has only the slot
                # name to go on, and a name is not evidence -- persisting the
                # flag is what lets a later boot know this tab was adopted from
                # a channel conversation rather than merely named like one.
                meta_line["channel_origin"] = True
            tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
            if tab_id:
                meta_line["tab_id"] = tab_id
            # ``rewrite`` is the structural signal for "this save EDITS the
            # conversation": the regenerate / rewind / fork paths pass an explicit
            # window snapshot (or leave ``_pending_rewrite`` set), while a steady
            # flush re-serializes the same window it already persisted.
            #
            # An edit swaps the live window's tail for content no consolidation
            # turn has read, so it advances the rotation generation — the
            # session's content-identity counter. That single write covers both
            # halves of the invariant that a consolidation marker and its retry
            # budget are bound to the content they measured:
            #
            # * An attempt already IN FLIGHT snapshotted the pre-edit generation,
            #   so its ``mark_consolidated`` write is rejected as stale
            #   (``ConversationLog.mark_consolidated``) instead of marking the
            #   REPLACEMENT tail consolidated without ever extracting it. A
            #   regenerate lands at the same message count, the same generation
            #   and the same marker, so nothing else about the save distinguishes
            #   it and the completion write would otherwise apply.
            # * A charged (or capped) budget stamped against the pre-edit
            #   generation stops describing the current span, so the replacement
            #   content earns a fresh budget rather than inheriting an exhausted
            #   one (``ConversationLog._attempts_describe_current_span``).
            #
            # This is the same release a rotation gets, and deliberately the same
            # in both directions: the armed backoff deadline survives, so a user
            # repeatedly regenerating a reply cannot re-bill a failing
            # consolidation turn on each gesture.
            if rewrite:
                meta_line["rotation_generation"] = (
                    int(existing_meta.get("rotation_generation", 0) or 0) + 1
                )
            # ``rows_only`` DEFERS to the line on disk, so it owes evidence that the
            # line is somebody ELSE's. ``tab_id`` is that evidence and the only
            # per-writer mark the line carries: it is minted per slot OBJECT
            # (``get_or_create_slot`` assigns a fresh uuid; a rehydrate adopts the
            # file's), and every save stamps the writer's own onto the line. A line
            # still carrying THIS slot's id was published by this slot and describes
            # nothing that needs protecting, so the ordinary rebuild must run —
            # metadata edits are acknowledged to the user the instant they land in
            # memory (``_dirty``, persisted by a later flush), and the slot a
            # rows-only write carries has been popped, so deferring here would drop
            # a title, folder, tag set or pin the user already saw applied with
            # nothing left to retry it.
            #
            # Unprovable ownership defers, because the two errors cost differently.
            # Deferring this slot's own edit loses fields that were never committed;
            # rebuilding over a live holder's committed line reverts fields it
            # already published, and for a replacement nobody types in again nothing
            # rewrites them, so that loss is permanent.
            own_tab_id = getattr(slot, "_tab_id", "") or ""
            line_is_this_slots = bool(own_tab_id) and existing_meta.get("tab_id") == own_tab_id
            if rows_only and existing_meta and not line_is_this_slots:
                # A rows-only write does not own the slot-owned fields: the line
                # describes whichever OTHER live slot published it, and this one is
                # only here to get its messages down. Drop the rebuild for every
                # field outside the file-identity subset and let the carry below
                # restore the on-disk value verbatim, so a title, folder, tag set or
                # pin another holder acknowledged is not reverted by a write that was
                # never about it. ``closed``/``closed_at`` are deferred with the
                # rest: on this line they are the other holder's own dismissal, and
                # an open-shaped write that erased them would resurface a tab the
                # user put away with that holder already popped. Gated on an existing
                # line because with none there is no other writer to defer to and
                # the slot's own state is all there is — and that is the branch below,
                # where the open-shaped write still clears a stale ``closed``.
                for meta_key in ROWS_ONLY_DEFERRED_META_KEYS:
                    meta_line.pop(meta_key, None)
                carry_unowned_metadata(meta_line, existing_meta, ROWS_ONLY_OWNED_META_KEYS)
            else:
                carry_unowned_metadata(meta_line, existing_meta, SLOT_OWNED_META_KEYS)
            meta_str = json.dumps(meta_line) + "\n"

            # ── Frozen prefix (never rewritten) + freshly serialized window ──
            # Read the verbatim bytes of the on-disk lines OLDER than the
            # in-memory window (cached, O(window) on a steady flush — #5), AND
            # detect any cross-process appends that landed in the on-disk window
            # region since our last write. Then re-serialize the ENTIRE window
            # snapshot so in-place edits and reordering persist, and append the
            # foreign lines so a concurrent cross-process append (landed between
            # this save's pre-lock ``window`` snapshot and the lock) is preserved
            # rather than clobbered by the meta+frozen+window replace.
            # A window longer than the entry cache cannot hit it: this save walks
            # the window in order, so the LRU evicts each entry before the next
            # save reaches it again. Building such a window through the cache
            # would pay the key-hashing cost for a guaranteed 0% hit rate, so the
            # largest windows -- where flush cost hurts most -- go uncached. A
            # window whose payload exceeds the BYTE ceiling self-evicts the same
            # way at a far smaller message count, so it is gated too, on a cheap
            # lower-bound estimate rather than on a measurement that would itself
            # cost what the bypass saves. Gated on the same configured bounds the
            # cache evicts by, so raising them widens the cached path in step.
            cache_max_entries, cache_max_bytes = _entry_cache_bounds()
            build_entry = (
                _build_message_entry_uncached
                if len(window) > cache_max_entries
                or _approx_window_payload_bytes(window) > cache_max_bytes
                else _build_message_entry
            )
            window_entries = [e for m in window if (e := build_entry(m)) is not None]
            window_lines = [json.dumps(e) + "\n" for e in window_entries]
            frozen_prefix, foreign_lines, dedup_dropped = _frozen_prefix_and_foreign_appends(
                slot, path, disk_older, window_entries, collect_foreign=not rewrite
            )
            # A fresh-``ts`` disk copy folded into the window by the bounded
            # (role, content) tiebreak is redundant with a window entry, so the
            # payload does not carry it. It is nonetheless the genuinely ambiguous
            # case (indistinguishable from a distinct same-content message without
            # a stable per-message id), so archive it before the atomic replace so
            # the trade-off loses no data permanently.
            if dedup_dropped:
                try:
                    base = state.conversation_log._dir if state.conversation_log else None
                    _archive_lines(history_key, dedup_dropped, reason="foreign-dedup", base=base)
                except Exception:
                    logger.warning(
                        "Failed to archive foreign-dedup drops for %s",
                        history_key,
                        exc_info=True,
                    )
            payload = (
                meta_str
                + frozen_prefix
                + "".join(_interleave_foreign_lines(window_entries, window_lines, foreign_lines))
            )

            # Refresh the slot's ordering floor from what is actually going to
            # disk, foreign rows included. This is the only place the slot can
            # learn about a row it never observed: the lock is already held and
            # the foreign lines are already in hand, whereas reading the tail per
            # append would put file I/O on the event loop. It does not make the
            # slot fully symmetric with ConversationLog.append -- a foreign row
            # arriving BETWEEN two saves stays invisible until the next one -- but
            # it closes the reachable shape, where a subagent/cron append is
            # observed at the next flush. The monotone rule itself lives on the
            # slot (note_disk_tail), so this cannot move the floor backwards.
            slot.note_disk_tail(
                _foreign_tail_ts(foreign_lines),
                window_entries[-1].get("ts") if window_entries else None,
            )

            # Rewrite paths (rewind/regenerate/fork) intentionally TRUNCATE the
            # window, so the dropped tail must be archived first to stay
            # recoverable. The default save is a superset of what's on disk
            # (frozen prefix unchanged + same-or-grown window), so it drops
            # nothing — and we skip the O(file) archive-diff read there to keep a
            # steady flush O(window). Both sides are passed as proper
            # per-line lists so the normalized-JSON diff matches the
            # frozen-prefix lines (never archived).
            if rewrite and path.exists():
                try:
                    old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    new_lines = payload.splitlines(keepends=True)
                    _archive_dropped_lines(state, history_key, old_lines, new_lines)
                except Exception:
                    logger.warning(
                        "Failed to archive dropped lines for %s", history_key, exc_info=True
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
            if _preserve_mtime is not None:
                try:
                    os.utime(path, (_preserve_mtime, _preserve_mtime))
                except OSError:
                    # Best-effort: a failure only costs a resurfaced tab on the
                    # next pass, never data.
                    logger.debug(
                        "could not restore pre-close mtime for %s", history_key, exc_info=True
                    )
            # A rewrite (archive-safe) save succeeded → clear the pending-rewrite
            # flag so later saves return to the cheap default path.
            if rewrite:
                slot._pending_rewrite = False
            # Record how many window messages are now on disk so memory trimming
            # can safely fold leading window messages into the frozen prefix.
            slot._disk_window_len = len(window)
            # Record the disk identity this save just wrote (carried forward
            # from ``existing_meta`` when present), so the delete-won guard can
            # recognize a file recreated by another writer after a permanent
            # delete on the NEXT save.
            slot._disk_meta_created_at = str(meta_line.get("created_at") or "")
            # A committed save is a direct observation of the file this slot
            # writes — even when the carried-forward metadata is legacy and
            # has no ``created_at`` for the identity string above.
            slot._disk_meta_observed = True
            # Record the post-write mtime in the frozen-prefix cache (even when
            # there is no frozen prefix, ``disk_older == 0``). The cache doubles
            # as the "did another process touch this file since we last wrote
            # it?" signal: a matching mtime on the next save proves THIS slot was
            # the last writer, so the frozen prefix is reusable and no NEW
            # cross-process append can have landed — letting the foreign-append
            # scan take the O(window) fast path instead of re-reading the
            # whole file. The foreign lines this save just preserved are cached
            # alongside so the fast path re-emits them verbatim: they now live in
            # the on-disk window region (after the frozen prefix), and because
            # ``disk_older`` is unchanged a bare frozen+window rebuild on the next
            # save would otherwise silently delete them.
            try:
                _st = path.stat()
                slot._frozen_prefix_cache = (
                    _st.st_mtime,
                    _st.st_size,
                    disk_older,
                    frozen_prefix,
                    foreign_lines,
                )
            except OSError:
                slot._frozen_prefix_cache = None
            state.conversation_log._invalidate_cache(history_key)
            state.conversation_log.note_tab_id(history_key, tab_id)
            return True
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


def session_was_deleted(state: DashboardState, slot: _ChatSlot) -> bool:
    """True when this slot's session was permanently deleted out from under it.

    The same delete witness as the delete-won guard in
    :func:`_save_slot_to_history`, exposed for callers that REPUBLISH a slot's
    content (fork, transfer export) and cannot rely on observing the guard's
    ``False`` return: the periodic 5s flush can hit the guard first and clear
    ``_dirty``, after which those callers skip their own flush arm entirely and
    would copy from the in-memory window. This probe answers directly, however
    the flush ordering fell out. Same evidence rule: the slot must have
    OBSERVED its file on disk (``_disk_meta_created_at`` non-empty, or the
    ``_disk_meta_observed`` bit for legacy metadata that records no
    ``created_at`` — both recorded
    at the hydrate sites and at committed saves, nowhere else), so a fresh
    slot is never "deleted". Witnesses, in order: a missing file
    (``FileNotFoundError``; any other ``stat`` failure also refuses — the
    file's existence is unverifiable, same fail-closed rule as the metadata
    read), and an on-disk ``created_at`` that no longer matches the observed one (a
    fresh incarnation created after the delete). An UNREADABLE metadata line
    returns True — identity unverifiable, so the copy is refused (fork 409 /
    transfer ``SnapshotUnstable``, both retryable) rather than republishing.
    An EMPTY ``created_at`` is re-stated before it is trusted: being lock-free,
    this probe can have the delete land between its stat and its metadata read,
    and a file that has just vanished reads back as a genuine ``({}, True)``,
    so the empty answer alone cannot tell "legacy metadata" (fails open) from
    "deleted a moment ago" (must refuse).
    Lock-free: a permanent delete never un-happens, so a True is stable; a
    False can race a delete landing right after, which is the same residual
    as a delete landing right after the copy itself completed.
    """
    if not state.conversation_log:
        return False
    # Same evidence rule as the guard: the slot must have OBSERVED its file on
    # disk, and ``_disk_meta_created_at`` is that observation (recorded at the
    # hydrate sites and at committed saves, nowhere else). Identity alone is
    # the gate — the window counters take no part (fork/transfer set
    # ``_resumed_count`` optimistically after a best-effort save that may have
    # failed, and a restored zero-message session has all-zero counters).
    known = str(getattr(slot, "_disk_meta_created_at", "") or "")
    # Same widening as the guard: legacy metadata records no ``created_at``,
    # so the observation BIT carries the evidence there — the missing-file
    # stat below is the legacy delete witness, while the identity comparison
    # at the tail still requires the recorded ``known``.
    if not known and not bool(getattr(slot, "_disk_meta_observed", False)):
        return False
    path_fn = getattr(state.conversation_log, "_path", None)
    if path_fn is None:
        # A log without a path resolver (stub/alternate store) cannot witness a
        # delete — same fail-open-is-fail-safe rule as the OSError arm below.
        return False
    try:
        path_fn(slot_history_key(slot)).stat()
    except FileNotFoundError:
        return True
    except OSError:
        # Any other stat failure fails CLOSED, like the metadata read below:
        # the file's existence cannot be verified, so the copy is refused
        # (retryably) rather than republishing what may be a deleted
        # conversation from the surviving in-memory window.
        return True
    # The file exists but may be a fresh incarnation created by another writer
    # AFTER the delete (e.g. a channel/cron append) — same identity rule as the
    # save's own guard: the observed ``created_at`` no longer matching the
    # on-disk one means this slot's session was deleted and the file belongs
    # to a new one. Status form for the same reason as the guard: a transient
    # metadata read failure must not blank the comparison. UNREADABLE fails
    # CLOSED here too — the copy is refused (fork 409 / transfer
    # SnapshotUnstable, both retryable) rather than republishing content whose
    # identity cannot be verified.
    meta_fn = getattr(state.conversation_log, "get_metadata_status", None)
    if meta_fn is not None:
        try:
            current_meta, readable = meta_fn(slot_history_key(slot))
        except Exception:
            return True  # cannot verify identity — refuse the copy
        if not readable:
            return True
        current = str((current_meta or {}).get("created_at") or "")
        if not current:
            # An empty ``created_at`` is ambiguous, and this probe is
            # deliberately lock-free, so the delete can land BETWEEN the stat
            # above and this read: ``get_metadata_status`` reports a file that
            # no longer exists as ``({}, True)`` -- by its own contract a
            # GENUINE empty answer, not an unreadable one -- which would blank
            # the comparison below and answer "not deleted" for a session that
            # is gone. Re-stat to tell the two empties apart. The save's own
            # guard needs no equivalent: it reads the metadata and stats the
            # path inside ``_locked``, the lock ``delete_session`` unlinks
            # under, so no delete can interleave between its two reads.
            try:
                path_fn(slot_history_key(slot)).stat()
            except OSError:
                # Gone (``FileNotFoundError``) is the delete witness; any other
                # stat failure leaves existence unverifiable. Both refuse the
                # copy, exactly as the first stat's arms do.
                return True
            # Still there, so the empty ``created_at`` is a genuine legacy-
            # metadata answer, which fails OPEN by the documented rule.
            return False
        # Compare identities only when one was RECORDED (same rule as the
        # guard): a legacy observation cannot tell "the same legacy file,
        # stamped since by a sibling's save" from a fresh incarnation.
        if known and current != known:
            return True
    return False


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
    rows_only: bool = False,
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

    ``rows_only``: write the window but leave the metadata line's slot-owned
    fields as they stand on disk when the line was published by ANOTHER slot --
    for a caller persisting a slot's rows onto a transcript another live slot now
    holds. See :func:`_save_slot_to_history` for the full contract, including the
    ``tab_id`` test that keeps the flag from deferring to the caller's own line.

    Returns ``False`` only when the save was skipped WITHOUT writing: the
    session was permanently deleted while the save awaited the lock (the
    delete-won guard in :func:`_save_slot_to_history`), or the routing moved
    off ``expected_history_key``. Neither skip raises, for either
    ``best_effort`` mode, so a clean return NO LONGER proves a committed write.
    Callers that go on to republish the slot's content elsewhere (fork, the
    transfer export) must check the return; archival callers (close/cleanup)
    may ignore it — the delete already disposed of what they were archiving.
    """

    def _do() -> bool:
        return _save_slot_to_history(
            state,
            slot,
            messages,
            closed=closed,
            closed_at=closed_at,
            force=force,
            rewrite=rewrite,
            expected_history_key=expected_history_key,
            rows_only=rows_only,
        )

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

    guarded_metadata = expected_history_key is not None
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
            return await loop.run_in_executor(None, _do)
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
        return await loop.run_in_executor(None, _do)
    finally:
        if guarded_metadata:
            _finish_guarded_metadata_write()


def _build_history_prefix(slot: _ChatSlot) -> str:
    """Build a condensed history prefix from slot messages for session re-injection.

    Redacts here as defence in depth. The returned prefix is prepended to the ACP
    prompt, so it leaves the dashboard's own storage and is persisted by kiro-cli
    into its session file — an egress path, not an internal read, so it does not
    rely solely on the load-time content pass upstream. Redaction is idempotent,
    so the common case is a no-op.
    """
    lines: list[str] = []
    total = 0
    for m in slot.messages:
        role = m.get("role", "")
        if role in ("chunk", "done", "streaming", "queued", "permission", "error", "tool"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = m.get("content", "")[:500]
        if role != "user":
            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
        line = f"{label}: {text}"
        if total + len(line) > _MAX_HISTORY_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "[Previous chat history for this tab — session was reset after stop]\n"
        + "\n".join(lines)
        + "\n[End of history]\n\n"
    )
