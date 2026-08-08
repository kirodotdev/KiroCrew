"""Persistent session-to-kiro-cli mapping.

Stores ``session_map.json`` mapping session keys to kiro-cli session IDs,
with Slack thread linkage for bidirectional sync.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    ChannelLink,
    canonical_key,
    is_channel_session_key,
    legacy_dashboard_mirror_key,
)

logger = logging.getLogger(__name__)

_SESSION_MAP_FILE = "session_map.json"

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home" and
# issue #874; dashboard/handlers/usage.py is the reference implementation.
_KIRO_SESSIONS_DIR: Path | None = None


def _kiro_sessions_dir() -> Path:
    """kiro-cli sessions directory, resolved against the live data home."""
    return _KIRO_SESSIONS_DIR if _KIRO_SESSIONS_DIR is not None else kiro_sessions_dir()


class SessionMap:
    """Persistent mapping of session_key → kiro-cli session ID.

    Stored as ``~/.kiro/crew/session_map.json``. Atomic write via tmp+rename.
    Only used for long-lived conversational sessions (Slack DM, dashboard).
    Stateless sessions (cron, subagent, taskrunner) are excluded.

    Each entry is a dict with keys: ``sid``, ``slack_thread_ts``, ``slack_channel_id``.
    A reverse index ``_thread_to_session`` maps Slack thread_ts → session_key
    for bidirectional sync lookups.
    """

    def __init__(self) -> None:
        self._path = config_dir() / _SESSION_MAP_FILE
        self._data: dict[str, dict] = {}  # key → {"sid", "slack_thread_ts", "slack_channel_id"}
        self._thread_to_session: dict[str, str] = {}  # slack_thread_ts → session_key
        self._load()

    def _load(self) -> None:
        self._thread_to_session.clear()
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._data = {}
                return
            if not isinstance(raw, dict):
                self._data = {}
                return
            migrated = False
            new_data: dict[str, dict] = {}
            for key, val in raw.items():
                if isinstance(val, str):
                    # Backward compat: plain string → new dict format
                    entry: dict = {
                        "sid": val,
                        "slack_thread_ts": None,
                        "slack_channel_id": None,
                    }
                    migrated = True
                elif isinstance(val, dict) and "sid" in val:
                    entry = val
                else:
                    continue  # skip corrupt entries
                # v1c: namespace bare Slack thread_ts keys → slack:<thread>,
                # preserving sid. Keep the raw thread_ts inside the entry so the
                # reverse index + challenge-redirect resume path are unaffected,
                # and populate the Layer-3 own-channel link.
                canon = canonical_key(key)
                # Scrub a pre-release corruption signature: a ``dashboard:`` key
                # carrying no ``slack_thread_ts`` but a ``discord:``
                # ``slack_channel_id`` is an impossible pair. It is left by an
                # early Discord session-resume build that ran the legacy
                # Slack-only ``set_channel`` path on a cold resumed dashboard
                # session; ``!unlink`` removes the Discord mirror but cannot
                # clear these separate legacy fields, so a later resume attempt
                # sees a phantom Slack binding. A genuine Slack link always has a
                # thread timestamp, so scrub only this exact signature.
                legacy_channel = entry.get("slack_channel_id")
                if (
                    canon.startswith("dashboard:")
                    and not entry.get("slack_thread_ts")
                    and isinstance(legacy_channel, str)
                    and legacy_channel.startswith("discord:")
                ):
                    entry.pop("slack_thread_ts", None)
                    entry.pop("slack_channel_id", None)
                    migrated = True
                if canon != key:
                    migrated = True
                    if not entry.get("slack_thread_ts"):
                        entry["slack_thread_ts"] = key
                    if "link" not in entry:
                        entry["link"] = ChannelLink(
                            channel_type="slack",
                            channel_id=entry.get("slack_channel_id"),
                            thread_id=key,
                        ).to_dict()
                existing = new_data.get(canon)
                if existing is not None:
                    # Collision (e.g. partially-migrated file): never clobber a
                    # live session. Overwrite ONLY when the existing entry has no
                    # sid and the incoming one does; otherwise keep existing.
                    # Order-independent — if both have a sid, the first-seen wins
                    # deterministically rather than depending on dict iteration.
                    if not (entry.get("sid") and not existing.get("sid")):
                        continue
                new_data[canon] = entry
            self._data = new_data
            self._rebuild_thread_index()
            if migrated:
                self._save()
        else:
            self._data = {}

    def _rebuild_thread_index(self) -> None:
        """Rebuild _thread_to_session from current _data.

        Two entries can claim the same ``slack_thread_ts``: a dashboard session
        that created the thread via send-to-Slack, and a ``slack:<ts>`` session
        forked by an inbound reply that ignored the existing binding. A plain
        last-write-wins pass resolves the thread by dict order, which is file
        order -- so the fork usually wins and the thread keeps routing to the
        wrong session even after the fork bug is fixed.

        Break that tie in favour of the session that does NOT derive its key from
        this thread. A ``slack:<ts>`` key whose ts IS the thread is the fork (or
        a self-link, which is a no-op rewrite); any other key holds the real
        conversation. This heals maps corrupted before the fix, on load, with no
        migration pass.
        """
        self._thread_to_session.clear()
        derived: dict[str, str] = {}
        for key, entry in self._data.items():
            ts = entry.get("slack_thread_ts")
            if not ts or not isinstance(ts, str):
                # A hand-edited or legacy file can hold a non-string ts. The old
                # index only ever used it as a dict key, so it survived; the
                # tie-break below calls str.endswith, which would raise
                # TypeError here and take gateway startup down with it.
                continue
            if is_channel_session_key(key) and key.endswith(ts):
                # Self-derived: only usable if nothing else claims the thread.
                derived.setdefault(ts, key)
                continue
            self._thread_to_session[ts] = key
        for ts, key in derived.items():
            self._thread_to_session.setdefault(ts, key)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get(self, key: str) -> str | None:
        """Return kiro-cli session ID if mapping exists and .json file is present.

        Handles the dashboard history key round-trip: the original session key
        ``dashboard:chat-1-xxx`` becomes ``dashboard_chat-1-xxx`` on disk (via
        ``_safe_key``), and when resumed from history the slot name becomes
        ``dashboard_chat-1-xxx``, producing session key
        ``dashboard:dashboard_chat-1-xxx``.  We try the canonical form too.
        """
        entry = self._data.get(key)
        # Bidirectional bare <-> slack: shim: a not-yet-updated caller may pass
        # a bare thread_ts; resolve it to the namespaced entry written at load.
        if not entry:
            canon = canonical_key(key)
            if canon != key:
                entry = self._data.get(canon)
                if entry:
                    key = canon
        # Fallback: dashboard history round-trip (dashboard:dashboard_X → dashboard:X)
        matched_key = key
        if not entry and key.startswith("dashboard:dashboard_"):
            canonical = "dashboard:" + key[len("dashboard:dashboard_") :]
            entry = self._data.get(canonical)
            if entry:
                matched_key = canonical
        if not entry:
            return None
        sid = entry["sid"]
        if entry.get("provider") == "claude_code":
            return sid
        sessions_dir = _kiro_sessions_dir()
        if sid and (sessions_dir / f"{sid}.json").exists():
            jsonl = sessions_dir / f"{sid}.jsonl"
            try:
                jsonl_size = jsonl.stat().st_size
            except FileNotFoundError:
                jsonl_size = 0
            if jsonl_size < 10:
                logger.info("Session %s has empty JSONL — pruning stale entry for %s", sid, key)
                self._remove_entry(matched_key)
                return None
            return sid
        if sid:
            self._remove_entry(matched_key)
        return None

    def _remove_entry(self, key: str) -> None:
        """Remove an entry and update reverse index."""
        entry = self._data.pop(key, None)
        if entry:
            ts = entry.get("slack_thread_ts")
            if ts and self._thread_to_session.get(ts) == key:
                del self._thread_to_session[ts]
            self._save()

    def set(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Save mapping and persist to disk, preserving existing slack fields."""
        key = canonical_key(key)
        existing = self._data.get(key)
        if existing:
            existing["sid"] = sid
            if provider:
                existing["provider"] = provider
            if cwd:
                existing["cwd"] = cwd
        else:
            entry: dict = {"sid": sid, "slack_thread_ts": None, "slack_channel_id": None}
            if provider:
                entry["provider"] = provider
            if cwd:
                entry["cwd"] = cwd
            self._data[key] = entry
        self._save()

    def get_cwd(self, key: str) -> str:
        """Return the stored CWD for *key*, or '' if not set."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("cwd", "")

    def get_provider(self, key: str) -> str:
        """Return the stored provider for *key* (e.g. 'acp', 'claude_code'), or ''."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("provider", "")

    def clear_sid(self, key: str) -> None:
        """Clear the stored session ID without removing the entry.

        Used on provider switch: the SID is incompatible with the new
        provider, but we keep the entry so Slack link and CWD persist.
        """
        entry = self._data.get(canonical_key(key))
        if entry and entry.get("sid"):
            entry["sid"] = ""
            self._save()

    def delete(self, key: str) -> None:
        """Remove mapping and persist."""
        self._remove_entry(canonical_key(key))

    def prune(self) -> int:
        """Remove entries whose session files no longer exist."""
        sessions_dir = _kiro_sessions_dir()
        stale = [
            k
            for k, entry in self._data.items()
            if entry.get("provider") != "claude_code"
            and (
                (entry.get("sid") and not (sessions_dir / f"{entry['sid']}.json").exists())
                or (
                    not entry.get("sid")
                    and not entry.get("slack_thread_ts")
                    and not entry.get("mirror")
                )
            )
        ]
        for k in stale:
            del self._data[k]
        if stale:
            self._rebuild_thread_index()
            self._save()
            logger.info("Pruned %d stale session map entries", len(stale))
        return len(stale)

    def mapped_sids_by_key(self) -> dict[str, str]:
        """Session key to kiro-cli session ID, for every entry that has one.

        A session ID present here is one Kiro Crew can still resume. Callers that
        account for or reclaim disk space need both halves of this relation: the
        IDs to exclude from deletion, and the key each ID belongs to so a
        session's transcript can be paired with its replay log. Returning the
        mapping rather than only the ID set is what lets such a caller reclaim a
        session whole instead of leaving one half behind.
        """
        return {
            key: sid
            for key, entry in self._data.items()
            if isinstance(sid := entry.get("sid"), str) and sid
        }

    def set_slack_link(self, key: str, thread_ts: str, channel_id: str | None) -> None:
        """Link a session to a Slack thread. Creates entry if needed."""
        key = canonical_key(key)
        entry = self._data.get(key)
        if entry:
            if (
                entry.get("slack_thread_ts") == thread_ts
                and entry.get("slack_channel_id") == channel_id
            ):
                self._thread_to_session.setdefault(thread_ts, key)
                return
            old_ts = entry.get("slack_thread_ts")
            if old_ts and old_ts != thread_ts:
                self._thread_to_session.pop(old_ts, None)
            entry["slack_thread_ts"] = thread_ts
            entry["slack_channel_id"] = channel_id
        else:
            self._data[key] = {
                "sid": "",
                "slack_thread_ts": thread_ts,
                "slack_channel_id": channel_id,
            }
        self._thread_to_session[thread_ts] = key
        self._save()

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        """Return (thread_ts, channel_id) for a session."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return None, None
        return entry.get("slack_thread_ts"), entry.get("slack_channel_id")

    def clear_slack_link(self, key: str) -> bool:
        """Remove the Slack link from a session, keeping the session itself.

        Clears only ``slack_thread_ts`` + ``slack_channel_id`` (preserves
        ``sid`` and the entry) and evicts the ``_thread_to_session`` reverse
        index so a later Slack reply in the old thread does not re-route to
        this session and silently re-engage mirroring. Returns True iff a link
        was present (only then is ``_save()`` called).
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        old_ts = entry.get("slack_thread_ts")
        had_link = bool(old_ts or entry.get("slack_channel_id"))
        if old_ts and self._thread_to_session.get(old_ts) == key:
            del self._thread_to_session[old_ts]
        entry.pop("slack_thread_ts", None)
        entry.pop("slack_channel_id", None)
        if had_link:
            self._save()
        return had_link

    def get_session_for_thread(self, thread_ts: str) -> str | None:
        """Return the session key linked to a Slack thread_ts, or None."""
        return self._thread_to_session.get(thread_ts)

    # ── Channel-neutral outbound mirror binding (generalizes Slack linking) ──
    # ``set/get/clear_slack_link`` above are the Slack-specific backend of this
    # API: they own the dedicated ``slack_thread_ts`` / ``slack_channel_id``
    # fields and the ``_thread_to_session`` reverse index that powers Slack's
    # inbound leg. The trio below exposes the SAME binding channel-neutrally as
    # a ``ChannelLink`` so the dashboard turn path can deliver a reply to any
    # proactive-capable channel via ``Transport.send_message`` without
    # special-casing Slack. Slack routes back through the dedicated fields;
    # every other channel stores a ``ChannelLink`` under ``mirror``.

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink | None,
        *,
        accepts_inbound: bool = False,
    ) -> None:
        """Bind (or clear, when *link* is None) a session's mirror target.

        ``accepts_inbound`` marks a non-Slack mirror as a session-resume binding:
        messages arriving from that exact channel location may be routed back to
        *key*. Ordinary dashboard mirrors remain outbound-only. Slack keeps its
        dedicated reverse index and therefore ignores this flag.
        """
        if link is None:
            self.clear_mirror_link(key)
            return
        if link.channel_type == SLACK_NAMESPACE:
            self.set_slack_link(key, link.thread_id or "", link.channel_id)
            return
        key = canonical_key(key)
        entry = self._ensure_entry(key)
        entry["mirror"] = link.to_dict()
        if accepts_inbound:
            entry["mirror_accepts_inbound"] = True
        else:
            entry.pop("mirror_accepts_inbound", None)
        self._save()

    def _mirror_key(self, key: str) -> str:
        """The key a session's mirror binding is actually stored under.

        A channel conversation's binding belongs on its own session key — the key
        its dashboard turns run under. Bindings written before that unification
        sit on the sanitized ``dashboard:`` spelling
        (:func:`~kiro_crew.messaging.link.legacy_dashboard_mirror_key`); when
        that row holds the only binding it is still the live one, so reads and
        clears resolve to it instead of silently dropping a link the user set. A
        binding on the canonical key always wins, so a rebind supersedes the
        legacy row rather than being shadowed by it.
        """
        canon = canonical_key(key)
        entry = self._data.get(canon)
        if entry and entry.get("mirror") is not None:
            return canon
        if is_channel_session_key(canon):
            legacy = self._data.get(legacy_dashboard_mirror_key(canon))
            if legacy and legacy.get("mirror") is not None:
                return legacy_dashboard_mirror_key(canon)
        return canon

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        """Return a session's outbound mirror target as a channel-neutral link.

        Reads the explicit ``mirror`` binding first; for a legacy Slack session
        that only carries ``slack_thread_ts`` / ``slack_channel_id`` it
        synthesizes the equivalent Slack ``ChannelLink`` so callers never have
        to special-case Slack. Returns None when the session mirrors nowhere.
        """
        entry = self._data.get(self._mirror_key(key))
        if not entry:
            return None
        raw = entry.get("mirror")
        if raw:
            return ChannelLink.from_dict(raw)
        ts = entry.get("slack_thread_ts")
        ch = entry.get("slack_channel_id")
        if ts or ch:
            return ChannelLink(channel_type=SLACK_NAMESPACE, channel_id=ch, thread_id=ts)
        return None

    def mirror_accepts_inbound(self, key: str) -> bool:
        """True iff this session's mirror is a session-RESUME binding.

        The read counterpart of ``set_mirror_link(accepts_inbound=True)``.
        ``get_mirror_link`` deliberately returns a plain ``ChannelLink``, which
        cannot carry the flag, so a caller that needs to tell a two-way resume
        from an outbound-only mirror (e.g. the dashboard's link projection, which
        must not offer a one-way mirror the affordances of a resumed session)
        asks here. Slack is excluded: it routes inbound through its own
        ``_thread_to_session`` index and never sets this marker.
        """
        entry = self._data.get(canonical_key(key))
        return bool(entry and entry.get("mirror_accepts_inbound"))

    def find_mirror_sessions(
        self,
        link: ChannelLink,
        *,
        inbound_only: bool = False,
    ) -> list[str]:
        """Return sessions bound to an exact non-Slack mirror location.

        The list form makes duplicate/corrupt bindings explicit so callers can
        fail closed rather than routing an inbound message to an arbitrary
        session. With ``inbound_only=True``, ordinary outbound-only dashboard
        mirrors are excluded.
        """
        matches: list[str] = []
        for key, entry in self._data.items():
            raw = entry.get("mirror")
            if not isinstance(raw, dict):
                continue
            if inbound_only and not entry.get("mirror_accepts_inbound"):
                continue
            try:
                candidate = ChannelLink.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if candidate == link:
                matches.append(key)
        return matches

    def clear_mirror_links_at(self, link: ChannelLink) -> list[str]:
        """Clear EVERY session whose mirror targets an exact non-Slack location.

        The write counterpart of :meth:`find_mirror_sessions`. An in-channel
        unlink means "nothing mirrors into this conversation anymore", and the
        bindings that occupy a location are matched by VALUE there — so a row
        stranded under a key spelling the conversation no longer uses (a rotated
        DM generation, a pre-unification ``dashboard:`` row) or a dashboard
        session mirroring into the conversation still blocks it while being
        unreachable by any key-addressed :meth:`clear_mirror_link`. Clearing by
        location closes that gap and doubles as the repair path for duplicate
        bindings, which the inbound resolver deliberately refuses to pick from.

        Returns the cleared session keys (empty when the location was free).
        Slack mirrors live in their own reverse index and are out of scope,
        exactly as in :meth:`find_mirror_sessions`.
        """
        cleared: list[str] = []
        for key in self.find_mirror_sessions(link):
            entry = self._data.get(key)
            if entry is None:  # pragma: no cover - keys come from _data itself
                continue
            entry.pop("mirror", None)
            entry.pop("mirror_accepts_inbound", None)
            cleared.append(key)
        if cleared:
            self._save()
        return cleared

    def clear_mirror_link(self, key: str) -> bool:
        """Remove a session's outbound mirror binding; return True iff one existed.

        A non-Slack ``mirror`` field is dropped directly; a Slack binding is
        cleared via ``clear_slack_link`` so its reverse index is evicted too.
        Resolves through :meth:`_mirror_key` so an unlink reaches a binding still
        held under the legacy spelling — otherwise a mirror that reads as live
        could not be turned off.
        """
        mkey = self._mirror_key(key)
        entry = self._data.get(mkey)
        if not entry:
            return False
        if entry.get("mirror") is not None:
            entry.pop("mirror", None)
            entry.pop("mirror_accepts_inbound", None)
            self._save()
            return True
        if entry.get("slack_thread_ts") or entry.get("slack_channel_id"):
            return self.clear_slack_link(mkey)
        return False

    def max_generation(self, bucket: str) -> int:
        """Return the highest persisted DM generation for a session *bucket*.

        The bucket is the generation-0 key (e.g.
        ``telegram:<agent>:direct:<user>``); generations persist as ``{bucket}``
        (gen 0) and ``{bucket}:gen{N}``. Returns the max ``N`` with a persisted
        entry, or -1 when the bucket has none. Channels seed their in-memory
        generation counter from this so ``/new`` and idle/daily reset advance
        past any generation left on disk (restart-safe) instead of colliding
        with a stale session and resuming it.
        """
        bucket = canonical_key(bucket)
        best = 0 if bucket in self._data else -1
        prefix = f"{bucket}:gen"
        for key in self._data:
            if key.startswith(prefix):
                suffix = key[len(prefix) :]
                if suffix.isdigit():
                    best = max(best, int(suffix))
        return best

    def find_key_by_sid(self, session_id: str) -> str | None:
        """Find the session map key for a given kiro-cli session ID."""
        for k, entry in self._data.items():
            sid = entry.get("sid") if isinstance(entry, dict) else entry
            if sid == session_id:
                return k
        return None

    def channel_key_for_stem(self, stem: str) -> str:
        """The real channel session key whose transcript filename is *stem*.

        ``history._safe_key`` folds every ``:`` in a session key to ``_`` to
        build the filename, and that fold is NOT reversible: given
        ``discord_kirocrew_direct_123`` there is no way to tell which
        underscores were colons, and an agent name may legitimately contain
        one. This map holds the unfolded keys, so it is the only authority.

        Returns ``""`` when no mapped session matches, which callers must treat
        as "leave it unbound" rather than guessing — binding a tab to a
        wrongly-spelled key would answer the user from a session the channel
        never sees.
        """
        if not stem:
            return ""
        from kiro_crew.history import _safe_key

        for k in self._data:
            if is_channel_session_key(k) and _safe_key(k) == stem:
                return k
        return ""

    def get_link(self, key: str) -> ChannelLink | None:
        """Return the session's OWN inbound-channel link, or None.

        Distinct from the dashboard->Slack *mirror* binding, which stays
        behind ``get/set_slack_link`` (guardrail G3).
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return None
        raw = entry.get("link")
        return ChannelLink.from_dict(raw) if raw else None

    def set_link(self, key: str, link: ChannelLink) -> None:
        """Set the session's OWN inbound-channel link. Creates entry if needed."""
        key = canonical_key(key)
        entry = self._data.get(key)
        if entry:
            entry["link"] = link.to_dict()
        else:
            self._data[key] = {
                "sid": "",
                "slack_thread_ts": None,
                "slack_channel_id": None,
                "link": link.to_dict(),
            }
        self._save()

    # --- v1c-B: per-conversation state on the session entry ---------------
    # Durable backing for per-thread state (``temporary``, ``incognito``,
    # ``agents``, ``projects``). Storing it on the session entry makes it
    # survive gateway restarts and ties its lifetime to the session (pruned
    # with the entry) rather than an ad-hoc bounded LRU in module-global dicts.

    def _ensure_entry(self, key: str) -> dict:
        """Return the entry for *key*, creating a blank one if absent."""
        entry = self._data.get(key)
        if entry is None:
            entry = {"sid": "", "slack_thread_ts": None, "slack_channel_id": None}
            self._data[key] = entry
        return entry

    def set_flag(self, key: str, flag: str, value: bool) -> None:
        """Set or clear a boolean per-conversation flag (e.g. ``temporary``).

        Flags are stored under an ``flags`` sub-dict on the entry. Clearing the
        last flag removes the sub-dict so empty state does not accrete on disk.
        Idempotent: writing an unchanged value still persists (cheap) so callers
        need not pre-check.
        """
        key = canonical_key(key)
        if value:
            entry = self._ensure_entry(key)
        else:
            # Clearing a flag on a key that was never stored is a no-op — don't
            # materialize a blank entry (would accrete empty state on disk).
            existing = self._data.get(key)
            if not existing:
                return
            entry = existing
        flags = entry.get("flags") or {}
        if value:
            flags[flag] = True
        else:
            flags.pop(flag, None)
        if flags:
            entry["flags"] = flags
        else:
            entry.pop("flags", None)
        self._save()

    def get_flag(self, key: str, flag: str) -> bool:
        """Return the value of a per-conversation boolean *flag* (default False)."""
        entry = self._data.get(canonical_key(key))
        if not entry:
            return False
        flags = entry.get("flags")
        return bool(flags and flags.get(flag))

    def set_agent_override(self, key: str, agent: str | None) -> None:
        """Set (or clear, when *agent* is falsy) the per-thread agent override."""
        key = canonical_key(key)
        if agent:
            self._ensure_entry(key)["agent_override"] = agent
        else:
            entry = self._data.get(key)
            if not entry or "agent_override" not in entry:
                return
            entry.pop("agent_override", None)
        self._save()

    def get_agent_override(self, key: str) -> str | None:
        """Return the per-thread agent override for *key*, or None."""
        entry = self._data.get(canonical_key(key))
        return entry.get("agent_override") if entry else None

    def set_project_override(self, key: str, project: str | None) -> None:
        """Set (or clear, when *project* is falsy) the per-thread project dir."""
        key = canonical_key(key)
        if project:
            self._ensure_entry(key)["project_override"] = project
        else:
            entry = self._data.get(key)
            if not entry or "project_override" not in entry:
                return
            entry.pop("project_override", None)
        self._save()

    def get_project_override(self, key: str) -> str | None:
        """Return the per-thread project-dir override for *key*, or None."""
        entry = self._data.get(canonical_key(key))
        return entry.get("project_override") if entry else None
