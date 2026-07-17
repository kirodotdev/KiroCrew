"""Session persistence — save, restore, history prefix."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from kiro_crew import model_registry
from kiro_crew.agent import KIRO_AGENTS_DIR
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    _normalize_model,
    _sync_dashboard_slots,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _normalize_slot_key
from kiro_crew.effort import EFFORT_LEVELS, EFFORT_VALUES
from kiro_crew.history import _archive_lines
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import ARTIFACT_SLUG_RE

logger = logging.getLogger(__name__)


def _redact_value(v):  # type: ignore[no-untyped-def]
    """Recursively redact any value (str, dict, list, or passthrough)."""
    if isinstance(v, str):
        v, _ = redact_exfiltration_urls(v)
        v, _ = redact_credentials(v)
        return v
    if isinstance(v, dict):
        return _redact_meta(v)
    if isinstance(v, list):
        return [_redact_value(i) for i in v]
    return v


def _redact_meta(meta: dict) -> dict:
    """Recursively redact string values in meta dict."""
    return {k: _redact_value(v) for k, v in meta.items()}


def _redact_meta_for_role(role: str, meta: dict) -> dict:
    """Redact meta, but preserve role-specific user-actionable external URLs (e.g. mcp_oauth)."""
    if role == "mcp_oauth":
        out: dict = {}
        for k, v in meta.items():
            if k == "oauth_url" and isinstance(v, str):
                # Two gates on rehydrate:
                #   1. http(s)-only — a tampered history line can't smuggle a
                #      javascript:/data: URL into <a href>.
                #   2. URL must not embed a credential or exfil-eligible host —
                #      a legit OAuth consent URL never carries credential
                #      patterns; presence of one means it's tampered/bogus.
                lower = v.lower()
                safe_scheme = lower.startswith("https://") or lower.startswith("http://")
                _, hit_cred = redact_credentials(v)
                _, hit_exfil = redact_exfiltration_urls(v)
                out[k] = v if (safe_scheme and not hit_cred and not hit_exfil) else ""
            else:
                out[k] = _redact_value(v)
        return out
    return _redact_meta(meta)


_MAX_HISTORY_CHARS = 8000

# Bounded retries for taking a consistent (window, _disk_older_count) snapshot
# when _save_slot_to_history runs in the flush executor thread concurrently with
# event-loop mutations (#4). A handful suffices — the only racing mutation is the
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
# (Claude Code's --effort flag) and the ACP /effort slash command, so BSC1
# set-membership validation applies on the read path too, not just the API.
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
    # Union-only: never drop a previously-valid level (BSC1 persistence safety).
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
    ``~/.kirocrew`` snapshot.

    Returns the number of slots restored. Missing / malformed file is a
    no-op (returns 0). Sessions that have been explicitly closed
    (``meta.closed``) are skipped via _rehydrate_slot_from_history's own
    guard, so closing a tab and then restarting still loses the tab.
    """
    if not state.conversation_log:
        return 0
    path = config_dir() / "open_slots.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("open_slots.json unreadable; skipping", exc_info=True)
        return 0
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return 0
    restored = 0
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
            slot = _rehydrate_slot_from_history(state, raw)
        except Exception:
            logger.debug("restore_open_slots: rehydrate failed for %s", raw, exc_info=True)
            # Roll back any partial slot leaked by _rehydrate_slot_from_history.
            # It calls state.get_or_create_slot() BEFORE its fallible work
            # (read_messages, redaction, slot.append), so a failure there
            # leaves an empty slot registered in state._slots. Without this
            # pop, restore_recent_sessions runs next, hits its
            # `if slot_name in state._slots: continue` dedup guard, and skips
            # the proper restore — the user would see a tab with the right
            # title/agent but empty or wrong message history.
            state._slots.pop(raw, None)
            # _rehydrate_slot_from_history also adds `dashboard:{slot_name}`
            # to _restricted_keys before that fallible work for any
            # non-persistent memory_mode. Roll it back too, else a later
            # get_or_create_slot(slot_name) (default memory_mode='persistent')
            # silently inherits restricted status, blocking
            # consolidation/lessons for what should be a normal session.
            state._restricted_keys.discard(f"dashboard:{raw}")
            continue
        if slot is not None:
            restored += 1
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)
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


def _rehydrate_slot_from_history(state: DashboardState, slot_name: str) -> _ChatSlot | None:
    """Rehydrate a single dashboard slot from persisted history.

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
    history_key = _history_key_for(slot_name)
    meta = state.conversation_log.get_metadata(history_key)
    # No metadata → session was never persisted. Don't create a phantom slot.
    if not meta:
        return None
    if meta.get("closed"):
        return None
    try:
        _restore_cfg = KiroCrewConfig.load()
    except Exception:
        _restore_cfg = None
    # Build the same kiro-agent model map as restore_recent_sessions so
    # legacy sessions without persisted `model` still resolve correctly.
    kiro_model_map: dict[str, str] = {}
    try:
        for f in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    kiro_model_map[data["name"]] = model
                kiro_model_map[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
    slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
    # Pull display fields from session listing for title parity with bulk restore.
    sessions = state.conversation_log.list_sessions()
    session_info = next(
        (s for s in sessions if s.get("key") == history_key),
        {},
    )
    # Titles may have been auto-generated by an LLM (_generate_title_via_kiro)
    # and are surfaced on the dashboard, so apply the same redaction passes
    # used on assistant content before setting. Defence-in-depth — the title
    # author is trusted-ish (our own kiro process), but the generation input
    # is user content, so a prompt injection could craft a title with an
    # exfiltration URL or leaked credential.
    raw_title = session_info.get("title") or meta.get("title") or slot_name
    raw_title, _ = redact_exfiltration_urls(raw_title)
    raw_title, _ = redact_credentials(raw_title)
    slot.title = raw_title
    slot._titled = bool(session_info.get("title") or meta.get("title"))
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
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{slot_name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    # Restore the persisted tab_id so cross-restart fork chaining survives.
    # get_or_create_slot (called by our caller) assigns a fresh random uuid to
    # slot._tab_id; if we don't overwrite it here, the next _flush_dirty_slots
    # persists that uuid back into meta, severing the tab_id ancestry that
    # read_messages_chained walks across forks — one restart + one flush
    # permanently loses forked-session history. Mirrors restore_recent_sessions.
    tab_id = meta.get("tab_id")
    if not tab_id:
        tab_id = uuid.uuid4().hex[:12]
        state.conversation_log.update_metadata(history_key, {"tab_id": tab_id})
    slot._tab_id = tab_id
    # Use read_messages_chained (not read_messages) so the loaded window walks
    # the tab_id ancestry across forks, matching restore_recent_sessions.
    # read_messages alone caps visible history at 200 lines from THIS file and
    # drops the ancestor chain — long-running forked sessions would lose 200+
    # messages of context on every gateway restart.
    messages = state.conversation_log.read_messages_chained(history_key)
    # Only the recent window is loaded into memory; older on-disk lines become
    # the FROZEN PREFIX that saves never rewrite. _disk_older_count must
    # therefore count those older lines so the save model preserves them.
    slot._disk_older_count = max(0, len(messages) - 500)
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
        )
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


def restore_recent_sessions(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """Restore sessions as chat slots."""
    if not state.conversation_log:
        return 0
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    restored = 0

    kiro_model_map: dict[str, str] = {}
    try:

        for f in KIRO_AGENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                model = data.get("model", "")
                if data.get("name"):
                    kiro_model_map[data["name"]] = model
                kiro_model_map[f.stem] = model
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Failed to build kiro model map", exc_info=True)
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
        slot = state.get_or_create_slot(slot_name, app=meta.get("app", ""))
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
        mm = meta.get("memory_mode", "persistent")
        slot.memory_mode = mm
        if mm != "persistent":
            state._restricted_keys.add(f"dashboard:{slot_name}")
        if meta.get("forked_from") is not None:
            slot.forked_from = meta["forked_from"]
        tab_id = meta.get("tab_id")
        if not tab_id:
            tab_id = uuid.uuid4().hex[:12]
            state.conversation_log.update_metadata(key, {"tab_id": tab_id})
        slot._tab_id = tab_id
        messages = state.conversation_log.read_messages_chained(key)
        slot._disk_older_count = max(0, len(messages) - 500)
        for m in messages[-500:]:
            role = m.get("role", "assistant")
            cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
            content = m.get("content", "")
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            slot.append(
                role,
                content,
                cls,
                ts=m.get("ts", ""),
                meta=(
                    _redact_meta_for_role(role, m["meta"])
                    if isinstance(m.get("meta"), dict)
                    else None
                ),
            )
            _attach_variants(slot, m)
        slot.drain()
        slot._resumed_count = len(slot.messages)
        # Loaded window is the on-disk window region; older lines (counted in
        # _disk_older_count above) are the frozen prefix saves never rewrite.
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False
        restored += 1
        logger.info("Restored session %s (%s)", slot_name, slot.title)
    _sync_dashboard_slots(state)
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
    if role not in ("user", "system"):
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
    entry: dict = {
        "role": role,
        "content": content,
        "ts": m.get("ts", ""),
        "source_thread": "dashboard",
        "source_user": "dashboard",
    }
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


def _read_frozen_prefix(slot: _ChatSlot, path, disk_older: int) -> str:
    """Return the frozen-prefix bytes: the first *disk_older* on-disk message lines.

    These are the lines OLDER than the in-memory window — never rewritten, so
    older history survives a restart that only loaded a recent window. The bytes
    are cached on the slot keyed by ``(mtime, disk_older)`` so a steady 5s flush
    is O(window) rather than O(file size) (#5): the cache hits whenever the file
    has not changed on disk since the last save (the only writer of this file is
    this slot, so its own atomic_write bumps the mtime and the next call re-reads
    — but the prefix region is identical, so re-reads are rare in practice and
    always correct).

    Returns "" when there is no frozen prefix (a fresh slot, ``disk_older == 0``)
    or the file is missing/unreadable/has no metadata line.
    """
    if disk_older <= 0:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cache = slot._frozen_prefix_cache
    if cache is not None and cache[0] == mtime and cache[1] == disk_older:
        return cache[2]
    try:
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return ""
    if not existing or '"_type"' not in existing[0]:
        return ""
    body = existing[1:]  # message lines only (metadata excluded)
    prefix = "".join(body[:disk_older])
    slot._frozen_prefix_cache = (mtime, disk_older, prefix)
    return prefix


def _save_slot_to_history(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
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

    Concurrency (#4): ``_flush_dirty_slots`` runs this in an executor thread
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
    # A slot left in _pending_rewrite by a failed inline rewrite (#3) also takes
    # the archive-safe rewrite path until it succeeds.
    if messages is not None or slot._pending_rewrite:
        rewrite = True
    # Snapshot the window and its disk-older count CONSISTENTLY (#4). The save
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
    # resume would never reach disk (#2). closed/force/rewrite always proceed.
    if (
        slot._resumed_count > 0
        and len(window) <= slot._resumed_count
        and not slot._dirty
        and not closed
        and not force
        and not rewrite
    ):
        return
    history_key = _history_key_for(slot.key)
    try:
        existing_meta = state.conversation_log.get_metadata(history_key)

        path = state.conversation_log._path(history_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_line: dict = {
            "_type": "metadata",
            "created_at": existing_meta.get("created_at") or slot.created_at,
            "last_consolidated": existing_meta.get("last_consolidated", 0),
        }
        if closed:
            meta_line["closed"] = True
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
        # Artifact companion binding (Mesh-2772) — persisted so a bound
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
        if slot.forked_from is not None:
            meta_line["forked_from"] = slot.forked_from
        tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
        if tab_id:
            meta_line["tab_id"] = tab_id
        meta_str = json.dumps(meta_line) + "\n"

        # ── Frozen prefix (never rewritten) + freshly serialized window ──────
        # Read the verbatim bytes of the on-disk lines OLDER than the in-memory
        # window (cached, O(window) on a steady flush — #5). Then re-serialize
        # the ENTIRE window snapshot so in-place edits and reordering persist.
        frozen_prefix = _read_frozen_prefix(slot, path, disk_older)
        window_lines = [
            json.dumps(e) + "\n"
            for m in window
            if (e := _build_message_entry(m)) is not None
        ]
        payload = meta_str + frozen_prefix + "".join(window_lines)

        # Rewrite paths (rewind/regenerate/fork) intentionally TRUNCATE the
        # window, so the dropped tail must be archived first to stay
        # recoverable. The default save is a superset of what's on disk (frozen
        # prefix unchanged + same-or-grown window), so it drops nothing — and we
        # skip the O(file) archive-diff read there to keep a steady flush
        # O(window) (#5). Both sides are passed as proper per-line lists so the
        # normalized-JSON diff matches the frozen-prefix lines (never archived).
        if rewrite and path.exists():
            try:
                old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                new_lines = payload.splitlines(keepends=True)
                _archive_dropped_lines(state, history_key, old_lines, new_lines)
            except Exception:
                logger.warning(
                    "Failed to archive dropped lines for %s", history_key, exc_info=True
                )

        atomic_write(path, payload, fsync=True)
        # A rewrite (archive-safe) save succeeded → clear the pending-rewrite
        # flag so later saves return to the cheap default path (#3).
        if rewrite:
            slot._pending_rewrite = False
        # Record how many window messages are now on disk so memory trimming
        # can safely fold leading window messages into the frozen prefix (#8).
        slot._disk_window_len = len(window)
        # We just wrote the file, so we KNOW its frozen prefix is exactly
        # ``frozen_prefix`` at the new mtime — refresh the cache rather than
        # invalidating it, so the next steady flush is a cache hit (O(window),
        # #5) instead of an O(file) re-read.
        if disk_older > 0:
            try:
                slot._frozen_prefix_cache = (path.stat().st_mtime, disk_older, frozen_prefix)
            except OSError:
                slot._frozen_prefix_cache = None
        else:
            slot._frozen_prefix_cache = None
        state.conversation_log._invalidate_cache(history_key)
        state.conversation_log.invalidate_tab_id_cache()
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


def _build_history_prefix(slot: _ChatSlot) -> str:
    """Build a condensed history prefix from slot messages for session re-injection."""
    lines: list[str] = []
    total = 0
    for m in slot.messages:
        role = m.get("role", "")
        if role in ("chunk", "done", "streaming", "queued", "permission", "error", "tool"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = m.get("content", "")[:500]
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
