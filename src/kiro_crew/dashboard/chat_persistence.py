"""Session persistence — save, restore, history prefix."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Iterator

from kiro_crew import model_registry
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dashboard.channel_slots import slot_closed_since
from kiro_crew.dashboard.chat_utils import (
    _normalize_model,
    _redact_meta_for_role,
    _sync_dashboard_slots,
    slot_history_key,
    slot_transcript_key,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _normalize_slot_key
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.history import (
    SLOT_OWNED_META_KEYS,
    _archive_lines,
    carry_provenance,
    carry_unowned_metadata,
    latest_transcript_ts,
    transcript_sort_key,
    update_metadata_off_loop,
)
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import ARTIFACT_SLUG_RE

logger = logging.getLogger(__name__)


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
    out: dict[str, str] = {}
    try:
        for f in kiro_agents_dir_path().glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    out[data["name"]] = model
                out[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
    return out


def _restore_open_slots_steps(state: DashboardState) -> "Iterator[int]":
    """Drive the open-tab restore one tab at a time, yielding the running count.

    Exposed as a generator so the same logic serves both a plain synchronous
    caller (:func:`restore_open_slots`) and an event-loop-friendly one
    (:func:`restore_open_slots_async`) that awaits between tabs. See
    :func:`restore_open_slots` for the behavioural contract.
    """
    if not state.conversation_log:
        return
    path = config_dir() / "open_slots.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("open_slots.json unreadable; skipping", exc_info=True)
        return
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
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
        if not isinstance(raw, str) or not raw:
            continue
        # Defense-in-depth: slot keys flow into _history_key_for() ->
        # filesystem path construction. open_slots.json is 0o600 so the threat
        # is small, but a key smuggled in (symlink attack at write time or a
        # separate vuln) could escape the sessions directory (e.g.
        # "../../etc/passwd"). Live-gateway slot keys never contain path
        # separators; reject any that do, warn so an attempted breakout is
        # visible, and keep restoring the rest.
        if "/" in raw or "\\" in raw:
            logger.warning(
                "restore_open_slots: rejecting key with path separators: %r", raw
            )
            continue
        # Fold to the canonical (filename-charset) key. Snapshots written
        # before slot-key normalization landed may carry a raw display-style
        # key (e.g. "Artifact: My Doc") alongside its sanitized twin — after
        # folding, the second form hits the dedup guard below instead of
        # restoring a duplicate sidebar session backed by the same transcript.
        raw = _normalize_slot_key(raw)
        if raw in state._slots:
            continue
        try:
            # Ask whether the metadata READ succeeded, not just whether it came
            # back empty. get_metadata() reports {} for both "never persisted"
            # and "could not be read after retries", and treating the second as
            # the first is what silently discards a live tab. Key it exactly as
            # _rehydrate_slot_from_history does, so the prefetch below is a hit.
            #
            # This read MUST stay inside the per-tab guard. restore_open_slots_async
            # has no except at its call site, so anything escaping here aborts
            # dashboard startup and costs every LATER tab too, not just this one.
            meta, readable = state.conversation_log.get_metadata_status(
                slot_transcript_key(raw)
            )
            if readable:
                slot = _rehydrate_slot_from_history(
                    state, raw, kiro_model_map=kiro_model_map, _prefetched_meta=meta
                )
                if slot is not None:
                    restored += 1
            else:
                unrestored.add(raw)
                logger.warning(
                    "restore_open_slots: metadata unreadable for %s; keeping it "
                    "in the reopen seed for the next restore instead of "
                    "dropping it",
                    raw,
                )
        except Exception:
            logger.debug("restore_open_slots: rehydrate failed for %s", raw, exc_info=True)
            # Same epistemic position as an unreadable read: the session was not
            # shown to be gone, so keep its key rather than erasing the seed.
            unrestored.add(raw)
            # No rollback here: _rehydrate_slot_from_history undoes its own
            # partial slot and restricted key, so every caller gets it rather
            # than only the ones that remembered to compensate.
        # One yield point per tab, reached on EVERY outcome. A failing tab still
        # costs real I/O (the metadata read retries, and _pause_for_transient_retry
        # deliberately does not sleep while on the loop), so a run of failing tabs
        # that skipped the yield would monopolise the loop and feed the stall
        # watchdog. The async driver turns this into a real event-loop yield; the
        # sync driver just spins through it.
        yield restored
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)


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
    """:func:`restore_open_slots`, yielding to the event loop between tabs.

    Restoring a tab reads and redacts a transcript, so a user with many large
    tabs can spend tens of seconds in here. Doing that synchronously monopolizes
    the event loop — and because ``_loop_heartbeat`` pets the
    ``LoopStallWatchdog`` *from a coroutine*, a blocked loop cannot pet it. The
    watchdog's 25s ``exit_after`` timer then fires, dumps thread stacks and
    ``_exit``s the gateway, which is exactly the observed startup crash-loop: the
    app never finished booting. Yielding per tab lets the heartbeat run, so a slow
    restore degrades into "the sidebar fills in progressively" rather than "the
    gateway dies".

    Stays ON the loop rather than moving to a worker thread because creating a
    slot broadcasts via ``asyncio.Queue.put_nowait`` / ``asyncio.Event.set``,
    neither of which is thread-safe.

    Because this yields, the 5s periodic flush (already running by this point)
    can interleave — so ``restoring_open_slots`` is held for the duration to stop
    it snapshotting a half-restored slot set over open_slots.json.
    """
    restored = 0
    state.restoring_open_slots = True
    try:
        for restored in _restore_open_slots_steps(state):
            # sleep(0) yields to the ready queue without adding wall-clock delay.
            await asyncio.sleep(0)
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


def _rehydrate_slot_from_history(
    state: DashboardState,
    slot_name: str,
    *,
    kiro_model_map: dict[str, str] | None = None,
    adopt_closed: bool = False,
    _prefetched_meta: dict | None = None,
    _prefetched_messages: list[dict] | None = None,
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
    meta = _prefetched_meta if _prefetched_meta is not None else (
        state.conversation_log.get_metadata(history_key)
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
    try:
        _restore_cfg = KiroCrewConfig.load()
    except Exception:
        _restore_cfg = None
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
    try:
        slot = state.get_or_create_slot(
            slot_name,
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
                bool(meta.get("channel_origin"))
                or bool(meta.get("linked_session_key"))
            ),
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
        raw_title, _ = redact_exfiltration_urls(raw_title)
        raw_title, _ = redact_credentials(raw_title)
        slot.title = raw_title
        slot._titled = bool(meta.get("title"))
        if meta.get("created_at"):
            slot.created_at = meta["created_at"]
        if meta.get("agent"):
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
                logger.debug("Failed to resolve model for rehydrated slot %s", slot_name, exc_info=True)
        if meta.get("reasoning_effort"):
            slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
        if meta.get("workspace"):
            slot.workspace = meta["workspace"]
        if meta.get("project"):
            slot.project = meta["project"]
        if meta.get("mode"):
            slot.mode = meta["mode"]
        if meta.get("folder_id"):
            slot.folder_id = meta["folder_id"]
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
            update_metadata_off_loop(
                state.conversation_log, history_key, {"tab_id": tab_id}
            )
        # Only the recent window is loaded into memory; older on-disk lines become
        # the FROZEN PREFIX that saves never rewrite. _disk_older_count must
        # therefore count those older lines so the save model preserves them.
        slot._disk_older_count = max(0, len(messages) - 500)
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
                # events. _broadcast_chat_message ships content verbatim, and this
                # helper also runs for on-demand cold-slot rehydrates while clients
                # ARE connected, so broadcasting here would push unredacted history
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

    So only the two reads move: the metadata line and the chained message walk,
    which on a large session are tens of MB of read plus JSON parse. Everything
    that touches slot state runs on the loop, as the synchronous callers do.
    """
    if not state.conversation_log:
        return None
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    conv_log = state.conversation_log

    def _read() -> tuple[dict | None, list[dict] | None, dict[str, str] | None]:
        meta = conv_log.get_metadata(history_key)
        if not meta or (meta.get("closed") and not adopt_closed):
            return None, None, None
        return (
            meta,
            conv_log.read_messages_chained(history_key),
            kiro_model_map if kiro_model_map is not None else _build_kiro_model_map(),
        )

    started = time.time()
    meta, messages, model_map = await asyncio.to_thread(_read)
    if meta is None or messages is None:
        return None
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
    return _rehydrate_slot_from_history(
        state,
        slot_name,
        kiro_model_map=model_map,
        adopt_closed=adopt_closed,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
    )


def _restore_recent_sessions_steps(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> "Iterator[int]":
    """Drive :func:`restore_recent_sessions` one session at a time.

    Generator for the same reason as :func:`_restore_open_slots_steps`: it lets
    the startup path yield to the event loop between sessions. This path restores
    every folder'd/pinned session regardless of the mtime window, so it can be
    just as slow as the open-tab restore — measured at 13.6s for 76 sessions.
    """
    if not state.conversation_log:
        return
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    restored = 0

    kiro_model_map = _build_kiro_model_map()
    try:
        _restore_cfg = KiroCrewConfig.load()
    except Exception:
        _restore_cfg = None
    for s in state.conversation_log.list_sessions():
        key = s.get("key", "")
        if key.startswith("dashboard:"):
            slot_name = key.removeprefix("dashboard:")
        elif key.startswith("dashboard_"):
            slot_name = key.removeprefix("dashboard_")
        else:
            # Channel-born sessions are restored by ``channel_slot_reconciler``,
            # which reads their transcripts in an executor. This loop runs ON the
            # event loop between yields, so pulling them in here would put a
            # large transcript's read in front of the whole gateway at startup.
            continue
        if slot_name in state._slots:
            continue
        meta = state.conversation_log.get_metadata(key)
        has_folder = bool(meta.get("folder_id"))
        has_pin = bool(meta.get("pinned"))
        if folders_only and not has_folder and not has_pin:
            continue
        if meta.get("closed"):
            continue
        if not has_folder and not has_pin:
            if cutoff is not None and s.get("modified", 0) < cutoff:
                continue
        slot = state.get_or_create_slot(
            slot_name,
            app=meta.get("app", ""),
            # No channel_origin here: this loop `continue`s above for every
            # non-dashboard key, so a channel-born session never reaches it --
            # ``channel_slot_reconciler`` owns surfacing those.
        )
        # Titles can be LLM-generated (auto-title) and are surfaced on the
        # dashboard — apply the same redaction as assistant content. Matches
        # the treatment in _rehydrate_slot_from_history above.
        raw_title = s.get("title", slot_name)
        raw_title, _ = redact_exfiltration_urls(raw_title)
        raw_title, _ = redact_credentials(raw_title)
        slot.title = raw_title
        slot._titled = bool(s.get("title"))
        if meta.get("created_at"):
            slot.created_at = meta["created_at"]
        if meta.get("agent"):
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
                logger.debug(
                    "Failed to resolve model for restored slot %s", slot_name, exc_info=True
                )
        if meta.get("reasoning_effort"):
            slot.reasoning_effort = _validate_reasoning_effort(meta["reasoning_effort"])
        if meta.get("workspace"):
            slot.workspace = meta["workspace"]
        if meta.get("project"):
            slot.project = meta["project"]
        if meta.get("mode"):
            slot.mode = meta["mode"]
        if meta.get("folder_id"):
            slot.folder_id = meta["folder_id"]
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
        tab_id = meta.get("tab_id")
        if not tab_id:
            tab_id = uuid.uuid4().hex[:12]
            # restore_recent_sessions runs during on_startup (event loop live)
            # — keep the _locked flock/os.close off the loop via the off-loop
            # backfill helper.
            update_metadata_off_loop(
                state.conversation_log, key, {"tab_id": tab_id}
            )
        slot._tab_id = tab_id
        messages = state.conversation_log.read_messages_chained(key)
        slot._disk_older_count = max(0, len(messages) - 500)
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
        restored += 1
        logger.info("Restored session %s (%s)", slot_name, slot.title)
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
    """:func:`restore_recent_sessions`, yielding to the event loop per session.

    Same rationale as :func:`restore_open_slots_async` — keeps the stall-watchdog
    heartbeat alive while a large restore proceeds, and holds
    ``restoring_open_slots`` so an interleaved flush cannot snapshot a partial
    slot set (this path adds slots to the same sidebar).
    """
    restored = 0
    state.restoring_open_slots = True
    try:
        for restored in _restore_recent_sessions_steps(
            state, window_minutes, folders_only=folders_only
        ):
            await asyncio.sleep(0)
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
        except (json.JSONDecodeError, ValueError):
            continue
    dropped: list[str] = []
    for ln in old_lines:
        if not ln.strip():
            continue
        try:
            normalized = json.dumps(json.loads(ln), sort_keys=True)
        except (json.JSONDecodeError, ValueError):
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


def _build_message_entry(m: dict) -> dict | None:
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
_TRANSIENT_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission"})


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
    non-destructive against cross-process appends. A
    disk line is treated as ours (dropped; the window re-serializes it) when its
    ``ts`` matches a window entry (covers in-place edits, which keep ``ts`` but
    change content) OR — as a COUNT-BOUNDED tiebreak — its ``(role, content)``
    matches an as-yet-unconsumed window entry (covers a same-process
    ``append_if_absent`` copy persisted with a FRESH ``ts`` distinct from the
    window entry's in-memory ``ts``). The tiebreak is bounded so each window
    entry absorbs AT MOST ONE disk copy: if the on-disk window region holds two
    lines with identical ``(role, content)`` but distinct timestamps — the
    window's own persisted copy PLUS a genuinely distinct event from another
    process (e.g. a repeated identical cron / workflow result) — only the first
    is folded into the window and the second is preserved as a foreign append.
    A plain ``(role, content)`` set collapsed those two real events into one;
    the bounded, timestamp-first identity
    fixes it. Timestamp is the closest thing to a stable per-message id today;
    the intended successor is a creation-time per-message uuid that demotes this
    heuristic to a legacy fallback for un-stamped lines (see also
    ``docs/system-specs/modules/history.md``). ``dedup_dropped`` returns any
    fresh-``ts`` content-tiebreak drops so the caller can route them through the
    archive — even the residual ambiguous case (a distinct message
    indistinguishable from an ``append_if_absent`` copy without a stable id)
    then loses no data permanently.

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
    if (
        cache is not None
        and cache[0] == mtime
        and cache[1] == size
        and cache[2] == disk_older
    ):
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
    # timestamp-first (the closest thing to a stable per-message id today), with
    # (role, content) used only as a COUNT-BOUNDED tiebreak so each window entry
    # absorbs at most ONE disk copy (see the module docstring / history.md).
    #
    # Build COUNT-BOUNDED consumption budgets over the window entries so each
    # on-disk window-region line is matched to AT MOST ONE window entry and each
    # window entry absorbs AT MOST ONE disk line. Identity is checked in three
    # tiers of decreasing confidence:
    #   (a) exact (ts, role, content) — an unchanged re-serialization (the common
    #       steady-save case), resolved first across ALL disk lines so a greedy
    #       edit/tiebreak match can never steal an entry a later exact line needs;
    #   (b) ts only — an in-place edit (same ``ts``, changed content: window wins);
    #   (c) (role, content) only — a same-content copy persisted with a FRESH
    #       ``ts`` (the ``append_if_absent`` case), routed to the archive.
    # Keying every tier by COUNT (deques of entry indices guarded by a shared
    # ``consumed`` flag) — rather than a ``ts -> entry`` dict plus a per-``ts``
    # ``set`` — is what makes this correct when several messages share one ``ts``.
    # Coarse system clocks (notably Windows' ~15ms tick) can stamp a burst of
    # rapid appends with an IDENTICAL ``datetime.now().isoformat()``; the old
    # dict/set collapsed those colliding-``ts`` entries to a single slot, so a
    # genuine window line was mis-classified as a foreign append and DUPLICATED on
    # disk. The bounded multiset below matches them one-for-one regardless of
    # ``ts`` collisions.
    exact_idx: dict[tuple[object, object, object], "deque[int]"] = {}
    ts_idx: dict[object, "deque[int]"] = {}
    rc_idx: dict[tuple[object, object], "deque[int]"] = {}
    for _i, e in enumerate(window_entries):
        _ets = e.get("ts")
        _erole = e.get("role")
        _econtent = e.get("content", "")
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
    # lines exactly as before), so the two matching passes share one parse.
    disk_msgs: list[tuple[str, object, object, object]] = []  # (norm, ts, role, content)
    for ln in body[disk_older:]:
        if not ln.strip():
            continue
        try:
            entry = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue  # corrupt window-region line — not a preservable message
        if not isinstance(entry, dict) or entry.get("_type") == "metadata":
            continue
        role = entry.get("role")
        if role is None or role in _TRANSIENT_ROLES:
            continue
        norm = ln if ln.endswith("\n") else ln + "\n"
        disk_msgs.append((norm, entry.get("ts"), role, entry.get("content", "")))

    # Pass 1 — exact (ts, role, content): unambiguously our own unchanged
    # re-serialization. Resolving these first makes the result independent of the
    # disk-line order (an earlier edit/tiebreak match can no longer consume an
    # entry that a later exact line requires).
    handled = [False] * len(disk_msgs)
    for _j, (_norm, ts, role, content) in enumerate(disk_msgs):
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
    for _j, (_norm, ts, _role, _content) in enumerate(disk_msgs):
        if ts and not handled[_j]:
            d_unmatched_ts[ts] = d_unmatched_ts.get(ts, 0) + 1

    # Pass 2 — for still-unmatched disk lines: ts-only (UNAMBIGUOUS in-place edit)
    # then the bounded (role, content) tiebreak, else genuinely foreign.
    for _j, (norm, ts, role, content) in enumerate(disk_msgs):
        if handled[_j]:
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
) -> None:
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
    """
    if not state.conversation_log:
        return
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
    if not window:
        return
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
        return
    history_key = slot_history_key(slot)
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
            existing_meta = state.conversation_log.get_metadata(history_key)

            path = state.conversation_log._path(history_key)
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
            if slot.agent:
                meta_line["agent"] = slot.agent
            meta_line["model"] = slot.model
            if slot.reasoning_effort:
                meta_line["reasoning_effort"] = slot.reasoning_effort
            if slot.mode:
                meta_line["mode"] = slot.mode
            if slot.workspace and slot.workspace != "default":
                meta_line["workspace"] = slot.workspace
            if slot.project:
                meta_line["project"] = slot.project
            if slot.folder_id:
                meta_line["folder_id"] = slot.folder_id
            if slot._app:
                meta_line["app"] = slot._app
            # Artifact companion binding — persisted so a bound
            # session restored after a gateway restart (or resumed from the
            # History page) comes back as the artifact's active bound session.
            if slot._artifact:
                meta_line["artifact"] = slot._artifact
            if slot.pinned:
                meta_line["pinned"] = True
            if slot.color_index is not None:
                meta_line["color_index"] = slot.color_index
            if slot.color_theme:
                meta_line["color_theme"] = slot.color_theme
            if slot.tags:
                meta_line["tags"] = list(slot.tags)
            if getattr(slot, "_auto_tagged", False):
                # Once-flag for project auto-tagging: without it a restart
                # re-runs maybe_auto_tag and silently re-adds a tag the user
                # removed (see chat_auto_tag.maybe_auto_tag).
                meta_line["auto_tagged"] = True
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
            window_entries = [
                e for m in window if (e := _build_message_entry(m)) is not None
            ]
            window_lines = [json.dumps(e) + "\n" for e in window_entries]
            frozen_prefix, foreign_lines, dedup_dropped = (
                _frozen_prefix_and_foreign_appends(
                    slot, path, disk_older, window_entries, collect_foreign=not rewrite
                )
            )
            # A fresh-``ts`` disk copy folded into the window by the bounded
            # (role, content) tiebreak is redundant with a window entry, so the
            # payload does not carry it. It is nonetheless the genuinely ambiguous
            # case (indistinguishable from a distinct same-content message without
            # a stable per-message id), so archive it before the atomic replace so
            # the trade-off loses no data permanently.
            if dedup_dropped:
                try:
                    base = (
                        state.conversation_log._dir
                        if state.conversation_log
                        else None
                    )
                    _archive_lines(
                        history_key, dedup_dropped, reason="foreign-dedup", base=base
                    )
                except Exception:
                    logger.warning(
                        "Failed to archive foreign-dedup drops for %s",
                        history_key,
                        exc_info=True,
                    )
            payload = meta_str + frozen_prefix + "".join(
                _interleave_foreign_lines(window_entries, window_lines, foreign_lines)
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
            state.conversation_log.invalidate_tab_id_cache()
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


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
) -> None:
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
    """

    def _do() -> None:
        _save_slot_to_history(
            state,
            slot,
            messages,
            closed=closed,
            closed_at=closed_at,
            force=force,
            rewrite=rewrite,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        if best_effort:
            try:
                _do()
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
            return
        _do()
        return
    if best_effort:
        try:
            await loop.run_in_executor(None, _do)
        except Exception:  # noqa: BLE001 - best-effort durable copy
            # See the inline branch above: re-arm the periodic flush so a
            # swallowed metadata/message save is retried rather than lost.
            slot._dirty = True
            logger.warning(
                "save_slot_off_loop: offloaded save failed slot=%s", slot.key, exc_info=True
            )
        return
    # Non-best-effort: propagate so the caller can roll back (do NOT remove the
    # session until the durable write is confirmed).
    await loop.run_in_executor(None, _do)


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
