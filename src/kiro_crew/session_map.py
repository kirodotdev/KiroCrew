"""Persistent session-to-kiro-cli mapping.

Stores ``session_map.json`` mapping session keys to kiro-cli session IDs,
with channel thread linkage (Slack legacy fields; other channels use the
generic ChannelLink mirror map) for bidirectional sync.
"""

from __future__ import annotations

import asyncio
import logging
import os  # noqa: F401
import threading
from collections.abc import Callable
from pathlib import Path

from kiro_crew import session_map_links as _links
from kiro_crew import session_map_store as _store
from kiro_crew._facade_bindings import own_methods as _own_methods
from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
from kiro_crew.atomic_write import replace_with_retry  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_sessions_dir
from kiro_crew.messaging.link import (  # noqa: F401
    SLACK_NAMESPACE,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASONS,
    ChannelLink,
    binding_token,
    canonical_key,
    is_channel_session_key,
    legacy_dashboard_mirror_key,
    split_dm_session_key,
)
from kiro_crew.messaging.privacy_mode import PRIVACY_LRU_MAX, _serialized, needs_tightening
from kiro_crew.sel import _infer_source, sel
from kiro_crew.session_map_links import ConversationOwnershipConflict  # noqa: F401
from kiro_crew.session_map_store import _MAP_LOCK, _P, _R, _guarded  # noqa: F401
from kiro_crew.validation import bounded_session_id

logger = logging.getLogger(__name__)

# Public because another instance's map is read by file path, not through this
# class: :func:`kiro_crew.session_storage.cotenant_sids` opens the map belonging
# to a pod that shares the replay store. A second literal there would be a silent
# hazard rather than a duplicate — a rename would turn that read into "no file",
# which reads as "that instance owns nothing" and withdraws the protection.
SESSION_MAP_FILENAME = "session_map.json"

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_KIRO_SESSIONS_DIR: Path | None = None


def _kiro_sessions_dir() -> Path:
    """kiro-cli sessions directory, resolved against the live data home."""
    return _KIRO_SESSIONS_DIR if _KIRO_SESSIONS_DIR is not None else kiro_sessions_dir()


#: Below this many bytes a kiro-cli session's ``.jsonl`` holds no turn: the
#: conversation exists on disk but ``session/load`` has nothing to restore, so
#: :meth:`SessionMap.get` prunes the mapping rather than resume it. The ONE
#: definition of that bar, read only through :func:`_jsonl_holds_a_turn` -- by
#: :meth:`SessionMap.get`, which prunes on it, and by
#: :func:`session_files_resumable`, the whole-rule predicate a reader outside
#: this module asks -- so the two cannot drift.
_RESUMABLE_JSONL_MIN_BYTES = 10


def _jsonl_holds_a_turn(sessions_dir: Path, sid: str) -> bool:
    try:
        size = (sessions_dir / f"{sid}.jsonl").stat().st_size
    except FileNotFoundError:
        size = 0
    return size >= _RESUMABLE_JSONL_MIN_BYTES


def resume_decided_by_load(provider: str = "") -> bool:
    """Whether a sid mapped under *provider* is validated by ``session/load`` alone.

    True for a backend that records its own provider label: its transcripts are
    not at a path this process can stat, so :meth:`SessionMap.get` honours the
    unlanded marker for it and nothing else. The kiro-cli label (or no label)
    answers False: its ``.jsonl`` already shows whether a turn landed.
    """
    return (provider or PROVIDER_LABEL_DEFAULT) != PROVIDER_LABEL_DEFAULT


def session_files_resumable(sid: str, provider: str = "") -> bool:
    """Whether *sid*'s on-disk files still let ``session/load`` resume it.

    The same rule :meth:`SessionMap.get` applies before it hands a sid out, in
    one place so a second reader cannot drift from it. Only kiro-cli keeps
    transcripts at a flat path this process can stat -- the ``{sid}.json``
    present and the ``{sid}.jsonl`` holding at least one turn. For every other
    backend the sid's validity is decided by ``session/load`` itself, so this
    answers True and leaves the typed refusal to the resume. An absent
    provider label means kiro-cli.
    """
    if (provider or PROVIDER_LABEL_DEFAULT) != PROVIDER_LABEL_DEFAULT:
        return True
    if not sid:
        return False
    sessions_dir = _kiro_sessions_dir()
    return (sessions_dir / f"{sid}.json").exists() and _jsonl_holds_a_turn(sessions_dir, sid)


# Per-conversation flag recording a refusal of automatic origin mirroring. Named
# here rather than at the caller because it is an ON-DISK contract: the map
# persists it, so renaming the literal would silently re-enable mirroring for
# every conversation that had already turned it off.
MIRROR_OPT_OUT_FLAG = "mirror_opt_out"

#: Set on a conversation whose owning app was uninstalled: its next cold start must
#: start EMPTY. Clearing the sid alone stops the native resume and not the replay —
#: the transcript stays on disk by design, so ``build_session_replay`` would inject
#: the removed app's history into the first turn of the next installation under the
#: same slot key, which is the bug the pointer drop exists to prevent. Persisted
#: rather than in-memory because the two writers are different processes (the
#: gateway-less CLI has no live manager) and because a gateway restart between the
#: uninstall and the reinstall must not lose it. One-shot: consumed, and cleared as
#: it is consumed, by the first cold start that honours it.
SUPPRESS_REPLAY_FLAG = "suppress_replay"

#: The sid of a fresh native session that still owes its Kiro Crew history: the
#: session was mapped at creation, but no turn has delivered the history to it yet
#: (``SessionManager.mark_first_turn_history_owed`` writes it, the settle in
#: ``consume_first_turn_history_owed`` clears it). It is the on-disk copy of that
#: in-memory debt, because the debt itself is gone after a restart. kiro-cli needs
#: no copy: :meth:`SessionMap.get` already prunes a sid whose ``.jsonl`` holds no
#: turn. A backend whose transcript this process cannot stat would instead be
#: resumed, and a ``session/load`` that succeeds on the unused session hands the
#: next turn no history at all. So ``get`` refuses a sid named here, without
#: touching the entry. Scoped to the sid rather than a bare flag, so a
#: later mapping of a different sid can never inherit the verdict.
UNLANDED_SID_FIELD = "unlanded_sid"

# Highest explicit DM generation acknowledged before its first provider turn.
# Stored on the stable bucket entry so repeated /new commands cost one integer,
# not one immortal map row per empty generation.
GENERATION_FLOOR_FIELD = "generation_floor"

# A rewind's request that the key's next cold start fork the native session it
# discarded: ``{"sid": <source sid>, "message_id": <fork point>}``. It stands in
# for an empty ``sid``, so every write or clear of ``sid`` drops it.
NATIVE_FORK_FIELD = "native_fork"

# Flags that are durable SETTINGS rather than session-scoped state, and so keep
# their entry alive through :meth:`SessionMap.prune`. Membership is opt-in
# BECAUSE immortality has a cost: an entry that prune can never collect is a row
# the map carries forever, and every mutation rewrites the whole map.
# ``SUPPRESS_REPLAY_FLAG`` is durable for the reason the paragraph above gives, not
# as an exception to it: it is a decision about the key's NEXT cold start, written at
# a moment when there is no session at all, and ``prune`` deletes a sid-less entry
# that nothing holds back. Losing it there would lose it in precisely the case it
# exists for — clear the pointer, restart the gateway, reinstall — so the flag would
# be decorative. The forever-row cost does not apply to it either: the flag is
# ONE-SHOT, so the row it keeps alive is collectable again as soon as the first
# cold start consumes it. (The privacy flags below are retained on separate
# grounds and bounded by a count; their own comment says why.)
_DURABLE_FLAGS = frozenset({MIRROR_OPT_OUT_FLAG, SUPPRESS_REPLAY_FLAG})

# The ``!temporary`` / ``!incognito`` privacy modes, spelled exactly as
# ``messaging.privacy_mode`` names them (MODE_TEMPORARY / MODE_INCOGNITO; a test
# pins the two spellings together), strictest first (the ranking
# ``privacy_mode.strictest`` uses; pinned by the same test). A privacy flag
# keeps its entry through BOTH stale paths (``prune`` and the per-read repair
# clear only a dead ``sid``), and no collection step removes such a row
# either. The flag is a record the channel gates read: the sync gates restore
# the trackers from this map alone (``privacy_mode.hydrate``), the async
# inbound gates from this map AND the transcript header together
# (``privacy_mode.hydrate_inbound``, the stricter of the two standing). The
# header alone is not enough to retire a row on: a transcript can be deleted
# or recreated apart from its row, and the sync gates never read it -- so a
# row removed for any reason, however complete the header, can leave a gate
# reading the thread as persistent after the next restart: its turns persisted
# and agent memory writes admitted again.
# The transcript header (``memory_mode``, the field every memory reader refuses
# on) is still ENSURED for every flagged row whose transcript exists, off the
# event loop and off the map lock, by :meth:`SessionMap.stamp_privacy_headers`,
# which the startup path runs after ``prune`` (awaited in place by a blocking
# start, inside its scheduled task by a non-blocking one, so the callers that
# restart the pool live never wait on the sweep) -- a transcript read is disk
# I/O the loop must not pay and the lock must not be held across (see the class
# docstring). The retained rows are bounded by a COUNT, and the bound is held
# by refusing, never by evicting: a NEW privacy flag past ``PRIVACY_ROW_CAP``
# is refused fail-closed (``set_flag`` raises :class:`PrivacyRowRefused`; the
# modifier tells the user the message was not processed), while every row
# already retained stays -- evicting one would run that thread as persistent,
# the leak the flag closes. A row's removal is a privacy leak, not
# housekeeping, and nothing here performs one.
_PRIVACY_STRICTNESS = ("temporary", "incognito")

#: How many privacy-flagged rows the map RETAINS -- the same bound the
#: process-local trackers already hold (``privacy_mode.PRIVACY_LRU_MAX``,
#: least-recently-marked eviction there), because the trackers hydrate from
#: these rows: a row past their bound would be one no tracker could carry
#: anyway. Reached, the bound REFUSES the next new flag rather than evicting a
#: row (see the module comment above). One small row each, so the cap is a
#: few megabytes of map at most, not a constraint an install meets in use.
PRIVACY_ROW_CAP = PRIVACY_LRU_MAX
#: The longest session key a privacy row is retained for. The map had no key
#: bound; this one is enforced at the same gate as the cap, and it is generous:
#: every channel key is under a hundred characters, and the transcript's
#: filename (``history._safe_key(key) + ".jsonl"``, plus its ``.lock`` sidecar)
#: has to fit the 255-byte name limit every filesystem shares, so a key past this
#: has no transcript to protect.
PRIVACY_ROW_KEY_MAX = 200


class PrivacyRowRefused(ValueError):
    """The map refused to retain a privacy row for *key* -- fail-closed.

    ``reason`` is ``"limit"`` (``PRIVACY_ROW_CAP`` rows already retained) or
    ``"key_too_long"`` (over ``PRIVACY_ROW_KEY_MAX``). Raised by :meth:`SessionMap.set_flag`
    before anything is written; ``privacy_mode.apply_mode`` turns it into the
    user-facing refusal and its audit record.
    """

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"privacy row refused for {key[:80]!r}: {reason}")
        self.key = key
        self.reason = reason


# How long a deferred flush waits before serializing, so a burst of mutations
# (a subagent wave calling ``set`` once per spawn) collapses into one write
# instead of one write per mutation. Small on purpose: the window is also how
# much recent state a crash can lose (the file stays a well-formed OLDER map —
# see ``_write_payload``'s tmp+rename), and durability-critical points force
# the write through :meth:`SessionMap.flush` rather than waiting it out.
_FLUSH_DEBOUNCE_SECS = 0.05


def _has_durable_flag(entry: dict) -> bool:
    """True iff *entry* carries a durable SETTING (:data:`_DURABLE_FLAGS`)."""
    flags = entry.get("flags")
    if not isinstance(flags, dict):
        return False
    return any(flags.get(name) for name in _DURABLE_FLAGS)


def _privacy_flags_on(entry: dict) -> list[str]:
    """The privacy flags *entry* carries, strictest first (``[]`` for none)."""
    flags = entry.get("flags")
    if not isinstance(flags, dict):
        return []
    return [name for name in _PRIVACY_STRICTNESS if flags.get(name)]


def _header_records_privacy_mode(key: str, flagged: list[str]) -> bool:
    """True iff *key*'s transcript header carries a mode at least as strict as *flagged*.

    The header (``memory_mode``, the field every memory reader refuses on, and
    the durable record the channel gate will read once it stops hydrating from
    the map alone) is ENSURED here, not merely read: a thread flagged before the
    modifier stamped headers has the mode in its map row alone, so the mode is
    copied into an EXISTING transcript's header first (``update_metadata_if``,
    tighten-only, ``require_existing`` -- this must never create a transcript),
    and only then is the header consulted. A transcript that does not exist, an
    unreadable header, or a header carrying a weaker mode than the flag all
    answer False. The answer decides nothing about the map row -- no privacy row
    is removed on it (see :func:`_keeps_entry`) -- it is the count
    :meth:`SessionMap.stamp_privacy_headers` reports.

    Disk I/O on purpose kept out of every guarded method: call it from a worker
    thread with no map lock held (:meth:`SessionMap.stamp_privacy_headers`).
    """
    if not flagged:
        return False
    # Call-time import, like ``channel_key_for_stem``'s ``_safe_key`` below, and
    # for weight rather than a cycle (there is none: nothing in ``history``'s
    # import graph imports this module). ``kiro_crew.history`` is the memory
    # facade -- the consolidator, skills, the vector-memory constants ride in
    # with it -- and this module is a leaf of the session layer: ``import
    # kiro_crew.session`` loads 519 modules without it and 1006 with it
    # (measured), for every process that needs sessions and no memory.
    from kiro_crew.history import ConversationLog, transcript_privacy_mode

    mode = flagged[0]

    def _tighten_only(metadata: dict) -> bool:
        # Normalized first: a raw ``Temporary`` would compare as unknown, and an
        # incognito stamp would then overwrite the stricter mode. No write at
        # all when the header already records the mode -- this runs for every
        # flagged row on every boot.
        return needs_tightening(transcript_privacy_mode(metadata.get("memory_mode")), mode)

    log = ConversationLog()
    try:
        log.update_metadata_if(key, {"memory_mode": mode}, _tighten_only, require_existing=True)
        header_mode = transcript_privacy_mode(log.get_metadata(key).get("memory_mode"))
    except Exception:
        return False
    if not header_mode:
        return False
    # ``_PRIVACY_STRICTNESS`` is strictest first: the header is enough iff it is
    # at least as strict as the strictest flag the entry carries.
    return _PRIVACY_STRICTNESS.index(header_mode) <= _PRIVACY_STRICTNESS.index(mode)


def _survives_prune(entry: dict) -> bool:
    """True iff *entry* holds state that must outlive its native session.

    Durable settings, an explicit generation floor, and channel bindings all
    outlive a provider session. The generation floor prevents a restart from
    reusing a history key after ``/new`` was acknowledged before the first turn.
    Entry-only, no I/O; :func:`_keeps_entry` adds the privacy-flag rule.
    """
    floor = entry.get(GENERATION_FLOOR_FIELD)
    has_generation_floor = isinstance(floor, int) and not isinstance(floor, bool) and floor > 0
    return bool(
        _has_durable_flag(entry)
        or has_generation_floor
        or entry.get("slack_thread_ts")
        or entry.get("mirror")
    )


def _keeps_entry(entry: dict) -> bool:
    """The one predicate both stale paths ask: keep *entry* (clearing only its ``sid``)?

    :func:`_survives_prune` for the durable reasons, plus a privacy flag, which
    keeps the entry for as long as the flag is on: it is the record the
    channel's inbound gate hydrates from, so no stale path and no startup step
    removes the row (the module comment above ``_PRIVACY_STRICTNESS`` says why,
    and which separate change would let one go: a header-reading gate on the
    inbound path -- kirodotdev/KiroCrew#13218 -- after which a flagged row whose
    transcript header carries the mode can be collected again). Kept rows are
    bounded at ``PRIVACY_ROW_CAP`` by REFUSING the next new flag
    (:meth:`SessionMap.set_flag`), never by evicting one here. No I/O: this runs
    under the map lock, on the event loop.
    """
    return _survives_prune(entry) or bool(_privacy_flags_on(entry))


def _stash_and_clear_sid(entry: dict) -> bool:
    """Drop *entry*'s ``sid`` while keeping it as ``discarded_sid``. True if it changed.

    One definition for every path that empties ``sid`` in place, because the
    three that existed disagreed and the disagreement was reachable: a provider
    switch stashed the id it dropped, while both stale paths -- the startup
    prune and the per-read repair -- dropped theirs and left an OLDER id
    standing in ``discarded_sid``. A history reader then answered that older id
    as the key's last store, citing a predecessor two links back and orphaning
    the one between them. Which path emptied the field is not a distinction any
    reader of it can use, so the field cannot be written by only some of them.

    A pending native fork is dropped too, even with no ``sid`` to stash: it
    would otherwise restore a conversation the caller has just cleared.
    """
    fork_dropped = entry.pop(NATIVE_FORK_FIELD, None) is not None
    sid = entry.get("sid")
    if not sid:
        return fork_dropped
    entry["discarded_sid"] = sid
    entry["sid"] = ""
    return True


# The callable shape a lost-binding announcement is delivered through:
# ``(session_key, link, reason)``.
UnbindListener = Callable[[str, ChannelLink, str], None]

# Announces a lost inbound binding to the channel that lost it. Registered by the
# gateway, because reaching a channel means resolving a transport and SessionMap is
# the synchronous store every surface sits on. MODULE-level for the same reason as
# :data:`_MAP_LOCK`: a clearing call site may hold a throwaway ``SessionMap()``, and
# a per-instance listener would leave those removals unannounced.
_UNBIND_LISTENER: UnbindListener | None = None

# The callable shape a COMMITTED channel binding is announced through: ``(session_key,)``.
# Deliberately carries only the key: the sink resolves the session itself, because what
# it records is a property of that session rather than of the link.
BindListener = Callable[[str], None]

# Announces that a session's conversation is now published to a channel. Registered by
# the gateway, which is the only layer that can see a session's memory mode and owning
# app -- this store sees the binding and nothing else about the session. MODULE-level
# for the same reason as :data:`_UNBIND_LISTENER`: a binding call site may hold a
# throwaway ``SessionMap()``, and a per-instance listener would leave those
# announcements unmade.
#
# It exists because the crew log records a session's CLASS, and a reader deciding
# whether another session may read that log asks about the whole life of the log rather
# than about now. Sampling the class at each turn's start misses a link that commits
# and is removed inside ONE turn, and content authored through it is in the log with no
# record that it was published. Announcing the commit is what closes that: the record
# is written when the fact becomes true, not when someone next looks.
_BIND_LISTENER: BindListener | None = None


def _normalize_unbind_reason(reason: str) -> str:
    """Constrain *reason* to the audited vocabulary.

    The runtime guard behind :data:`~kiro_crew.messaging.link.UNBIND_REASONS`. An
    unexpected value would add an unbounded SEL dimension and reach the notice's
    phrasing map as a miss, so it is recorded as ``unspecified`` and the WARNING
    names the call site that needs threading.
    """
    if reason in UNBIND_REASONS:
        return reason
    logger.warning(
        "unknown inbound-unbind reason %r; recording as %r",
        reason,
        UNBIND_REASON_UNSPECIFIED,
    )
    return UNBIND_REASON_UNSPECIFIED


def set_unbind_listener(callback: UnbindListener | None) -> None:
    """Register (or clear, with None) the sink for inbound-binding removals.

    Invoked as ``callback(session_key, link, reason)`` after the binding is gone
    and persisted, so the callback observes a committed removal. Best-effort by
    contract: it is called inside the map lock on a synchronous path, so it must
    not block, and an exception it raises is swallowed — a broken notifier cannot
    fail the unlink that provoked it. Suppression by reason belongs to the
    callback, since only it knows what the user has already been told.
    """
    global _UNBIND_LISTENER
    _UNBIND_LISTENER = callback


def set_bind_listener(callback: BindListener | None) -> None:
    """Register (or clear, with None) the sink for COMMITTED channel bindings.

    Invoked as ``callback(session_key)`` once the IN-MEMORY binding is committed and
    while the map lock is still held, which is what makes it precede any traffic: routing
    an inbound message reads this map, so no message can be attributed to the session
    before the announcement has been made. The guarantee rests on that in-memory commit
    ALONE, and deliberately so: the ordering that matters is against readers of the map,
    and they read the dict, not the file. Where the file write has reached by then varies
    by context and is not part of the guarantee -- on a thread running an event loop it is
    only queued, while a caller with no running loop (CLI, tests, worker threads) has
    already written it inline. A sink that waited for the disk would hold the lock across
    a write on the one path that must not pay for it, without buying any ordering the
    in-memory commit does not already give.

    Best-effort at the call site, on the same contract as its unbind sibling -- it runs
    on a synchronous path, so it must not block, and an exception it raises is
    swallowed rather than failing the bind. That is safe here only because the thing it
    records is fail-closed at the far end: the record is handed to the crew log's
    writer without waiting, and a write the writer permanently loses is itself recorded,
    which the class fold reads as a hole and a cross-session read refuses on. So a lost
    announcement costs a refusal, never a silent grant.
    """
    global _BIND_LISTENER
    _BIND_LISTENER = callback


class SessionMap:
    """Persistent mapping of session_key → kiro-cli session ID.

    Stored as ``~/.kiro/crew/session_map.json``. Atomic write via tmp+rename.
    Only used for long-lived conversational sessions (channel DMs, dashboard).
    Stateless sessions (cron, subagent, taskrunner) are excluded.

    Each entry is a dict with keys: ``sid``, ``slack_thread_ts``, ``slack_channel_id``.
    A reverse index ``_thread_to_session`` maps Slack thread_ts → session_key
    for bidirectional sync lookups.

    THREADING CONTRACT
    ------------------
    Every mutation rewrites the WHOLE map from ``_data``, so a read-modify-write
    is only atomic if nothing else touches the structure in between. Three rules
    hold that together; the first two are enforced, the third cannot be:

    1. **Any thread may call this class.** :data:`_MAP_LOCK` (module-level,
       reentrant) guards every method that mutates the map, iterates it, or
       reads several fields as one unit. Single-key probes (``get_cwd``,
       ``get_flag``, ``get_session_for_thread``, …) are lock-free on purpose:
       one ``dict.get`` cannot observe a half-applied write, and taking the lock
       for them would put the map's cheapest reads — including the one on every
       inbound Slack reply — behind its most expensive write. What that costs is
       a rule for the WRITERS: a structure a lock-free reader looks at is
       replaced by rebinding a finished copy, never cleared and refilled in
       place (see :meth:`_rebuild_thread_index`).

       Because a caller can now WAIT, what may be held under the lock is
       bounded: **no guarded method reads or parses the map file.** The longest
       hold is one whole-map write (0.77 ms at today's size) plus, in ``get`` and
       ``prune``, a session-file stat per entry — costs the event loop already
       paid inline before this lock existed. Loading a file into a fresh
       instance is the one unbounded step, and :meth:`_load` is deliberately
       unguarded so a worker-thread construction cannot stall the loop behind
       its disk read.

    2. **A multi-mutation sequence must say so.** Two locked calls are two
       critical sections, and another thread's write can land between them and
       be lost by the second one's whole-map rewrite. :meth:`batched_save` holds
       the lock across the block, making the sequence one critical section AND
       one write. Related mutations belong inside it — never merely adjacent.
       It MUST NOT be held across an ``await``: the lock is per-thread
       reentrant, so an await inside a batch lets another coroutine on the same
       loop walk straight into the block, while a worker thread waiting on the
       lock stalls until the await returns. ``test_session_map_locking.py``
       ratchets this against the whole tree.

    3. **Writes go through the LIVE map.** The lock orders access to the
       structure; it cannot reconcile two instances that loaded ``_data``
       independently, because the loser's rewrite is a whole-file write of a
       stale snapshot. A throwaway ``SessionMap()`` is therefore READ-ONLY —
       see ``session_transfer._join_layer_b`` for what a detached write costs.

    On the event loop, a mutation does not pay the disk write inline: it marks
    the map dirty and a debounced flush task serializes UNDER the lock into an
    immutable payload, then writes tmp+rename in a worker thread — ``_data``
    never crosses the thread boundary, and the lock is never held across the
    await (the ratchet in ``test_session_map_locking.py`` checks that). Off the
    loop (CLI, tests, worker threads) writes stay inline and synchronous.
    :meth:`flush` (sync) and :meth:`aflush` (awaited) are the deterministic
    durability points; losing a pending flush leaves a well-formed OLDER file,
    never a truncated one. The lock is what
    makes the offload safe at all — before it, offloading a single write made
    the map racy instead of non-blocking.

    SCOPE: the lock is in-PROCESS. Two gateways writing one map file would still
    lose updates, and nothing here changes that — the data-home singleton is what
    keeps that from happening. ``history.ConversationLog._locked`` is the
    cross-process, off-loop-enforcing primitive; this is deliberately not it.
    """

    def __init__(self) -> None:
        self._path = config_dir() / SESSION_MAP_FILENAME
        self._data: dict[str, dict] = {}  # key → {"sid", "slack_thread_ts", "slack_channel_id"}
        self._thread_to_session: dict[str, str] = {}  # slack_thread_ts → session_key
        self._batch_depth = 0
        self._batch_dirty = False
        # Strong references to in-flight audit writes, so an executor Future is not
        # collected before its result is consumed. Discarded on completion.
        self._audit_futures: set["asyncio.Future[None]"] = set()
        # Deferred-flush state (all mutated under _MAP_LOCK). ``_dirty`` records
        # that in-memory state is ahead of the file; ``_flush_task`` is the one
        # loop task owed for it. ``_snapshot_seq`` tickets each serialized
        # snapshot and ``_written_seq`` (guarded by ``_io_lock``, not _MAP_LOCK)
        # refuses a STALE payload: an in-flight thread write racing a forced
        # inline write must not land an older map over a newer one. Keeping the
        # worker's write off _MAP_LOCK is what keeps loop-side MUTATIONS from
        # waiting on its disk I/O; the inline write paths (batch exit, flush,
        # load-time migration) do queue behind it on _io_lock — they already
        # paid an inline write before deferral existed, so that wait replaces
        # like with like.
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None
        self._snapshot_seq = 0
        self._written_seq = 0
        self._io_lock = threading.Lock()
        self._load()

    async def _flush_async(self) -> None:
        """Debounce, serialize under the lock, write off the loop; repeat.

        Loops because coalescing must not lose the last write: a mutation that
        lands while ``to_thread`` is writing re-marks dirty, and the next
        iteration picks it up. Exits only via :meth:`_take_pending_snapshot`
        observing a clean map (which retires the task under the lock).

        The lock is NEVER held across an await — serialization happens inside
        the guarded helpers, the write happens off-lock in a worker thread.
        Cancellation re-owes any claimed-but-unwritten snapshot and does NOT
        write (a cancellation handler that did disk I/O on the loop would hold
        up the very teardown that cancelled it): pending state is landed by the
        shutdown path awaiting :meth:`aflush`, or rescheduled by the next
        mutation. A task cancelled before its first step never runs this body
        at all — the same two nets cover it, because nothing here consumed the
        dirty mark. A write failure restores the mark and retires the task so
        a later mutation retries.
        """
        try:
            while True:
                await asyncio.sleep(_FLUSH_DEBOUNCE_SECS)
                snapshot = self._take_pending_snapshot()
                if snapshot is None:
                    return
                payload, seq = snapshot
                await asyncio.to_thread(self._write_payload, payload, seq)
        except asyncio.CancelledError:
            self._restore_dirty()
            raise
        except Exception:
            logger.exception("Deferred session-map flush failed; will retry on next mutation")
            self._restore_dirty()

    def _resolve_alias(self, key: str) -> "tuple[str, dict | None]":
        """Resolve *key* through the map's alias folds; return (matched_key, entry).

        The ONE place the key-alias rules live, shared by the pruning
        :meth:`get` and the read-only :meth:`has_hint` so they cannot drift:
        1. exact key;
        2. bidirectional bare <-> ``slack:`` shim (a not-yet-updated caller
           may pass a bare thread_ts; resolve to the namespaced entry);
        3. dashboard history round-trip (``dashboard:dashboard_X`` -> the
           original ``dashboard:X`` written via ``_safe_key``).
        Pure lookup: no disk I/O, no pruning, no save.
        """
        entry = self._data.get(key)
        if not entry:
            canon = canonical_key(key)
            if canon != key:
                entry = self._data.get(canon)
                if entry:
                    key = canon
        if not entry and key.startswith("dashboard:dashboard_"):
            canonical = "dashboard:" + key[len("dashboard:dashboard_") :]
            entry = self._data.get(canonical)
            if entry:
                key = canonical
        return key, entry

    @_guarded
    def get(self, key: str) -> str | None:
        """Return kiro-cli session ID if mapping exists and .json file is present.

        Handles the dashboard history key round-trip: the original session key
        ``dashboard:chat-1-xxx`` becomes ``dashboard_chat-1-xxx`` on disk (via
        ``_safe_key``), and when resumed from history the slot name becomes
        ``dashboard_chat-1-xxx``, producing session key
        ``dashboard:dashboard_chat-1-xxx``.  We try the canonical form too.
        """
        matched_key, entry = self._resolve_alias(key)
        if not entry:
            return None
        sid = entry["sid"]
        # Only kiro-cli keeps transcripts at a flat path this process can stat.
        # For every other backend the sid's validity is decided by session/load
        # itself, so skip the file check rather than pruning a live session.
        # An absent label means kiro-cli, so normalize before comparing.
        if resume_decided_by_load(entry.get("provider") or ""):
            # The one thing this process does know about such a sid: whether a
            # turn ever delivered its history (:data:`UNLANDED_SID_FIELD`). One
            # that never did is refused, so the next cold start replays the
            # conversation instead of loading an empty native session. The entry
            # is left as it is: ``get`` also runs while that session is still
            # live (an export resolving the sid mid-turn), and repairing the entry
            # there would drop the marker that ``close_all``'s write-back of the
            # same sid relies on to keep refusing it after a restart.
            if sid and entry.get(UNLANDED_SID_FIELD) == sid:
                logger.info("Session %s never landed its history — not resuming %s", sid, key)
                return None
            return sid
        sessions_dir = _kiro_sessions_dir()
        if sid and (sessions_dir / f"{sid}.json").exists():
            if not _jsonl_holds_a_turn(sessions_dir, sid):
                logger.info("Session %s has empty JSONL — pruning stale entry for %s", sid, key)
                self._repair_or_remove_stale(matched_key)
                return None
            return sid
        if sid:
            self._repair_or_remove_stale(matched_key)
        return None

    @_guarded
    def _repair_or_remove_stale(self, key: str) -> None:
        """Drop a stale entry, or clear only its ``sid`` when state must outlive it.

        Asks :func:`_keeps_entry`, the same predicate :meth:`prune` uses, so
        the two stale paths cannot disagree: an entry carrying a channel binding,
        a durable flag or a privacy flag keeps the entry and loses only the dead
        ``sid`` (a privacy-flagged entry is never removed by any path -- see
        :func:`_keeps_entry` -- and this one never reads a transcript). The
        binding is the conversation's identity — deleting it here would strand the
        channel. No inbound-unbind audit or notice fires on the repair branch,
        because no binding was removed; the removal branch reaches an entry that
        holds none.
        """
        entry = self._data.get(key)
        if entry is not None and _keeps_entry(entry):
            if _stash_and_clear_sid(entry):
                self._save()
            return
        self._remove_entry(key, reason=UNBIND_REASON_ENTRY_DELETED)

    def has_hint(self, key: str) -> bool:
        """Read-only, in-memory probe: does an entry exist for *key*?

        Unlike :meth:`get`, this never touches disk and never mutates the map
        (no stale-entry pruning, no ``_save``), so it is safe to call from the
        event loop and carries no cross-thread hazard. It can return a false
        positive for an entry whose session files are gone — callers that act
        on the hint must treat the pruning :meth:`get` inside the actual
        resume path as the authority and tolerate the resume falling back.
        Alias folding is shared with :meth:`get` via ``_resolve_alias``.
        """
        return self._resolve_alias(key)[1] is not None

    def mapped_sid(self, key: str) -> str:
        """Read-only, in-memory: the session ID *key* maps to, or ``""``.

        The value half of :meth:`has_hint`, and undecorated for the same reason:
        one dict lookup through the shared alias fold, no disk and no mutation,
        so it is safe on the event loop and carries no cross-thread hazard.

        It answers a question :meth:`get` deliberately does not. ``get`` asks
        "can this ID still be resumed", which is why it stats the transcript and
        PRUNES the entry when that file is gone or empty. A caller recording
        HISTORY wants the opposite: the ID this key was last serving, whether or
        not a resume would now succeed. Routing such a caller through ``get``
        loses the ID exactly when the two stores disagree -- a crew log unit can
        outlive a truncated ACP transcript -- and mutates the map as a side
        effect of being asked to describe it.

        So this is not an alternative spelling of ``get``: a caller deciding
        whether to RESUME must still use ``get``, whose file check is the whole
        point, and must not treat a value from here as a resumable session.

        A sid that was emptied in place still answers, from ``discarded_sid``.
        Three paths empty it and all three record what they dropped, through
        :func:`_stash_and_clear_sid`: the provider switch, the poisoned
        conversation discard, and the two stale paths whose transcript went
        missing. For the resume question an emptied sid IS the answer, which is
        why ``get`` must not see the stash. For the history question it is not:
        the emptied id is exactly "the ID this key was last serving", so reading
        ``sid`` alone would report a key that has served a session all day as
        having served none, and a successor would cite no predecessor at all.
        Recording it on only some of those paths is worse than recording it on
        none, because the field then holds a genuine id that is not the latest
        one, and a successor cites a predecessor two links back.
        """
        entry = self._resolve_alias(key)[1]
        if not entry:
            return ""
        sid = entry.get("sid")
        if isinstance(sid, str) and sid:
            return bounded_session_id(sid) or ""
        # `or ""` rather than a second bounding helper: this reader's callers want
        # "no id" as the empty string, and the shared bound answers None. Spelling
        # the sentinel at the call site keeps one definition of the bound, which is
        # what the two private copies that preceded it could not do -- they had
        # already diverged on exactly this sentinel.
        return bounded_session_id(entry.get("discarded_sid")) or ""

    def _note_bind(self, key: str) -> None:
        """Announce one COMMITTED channel binding. The choke point both bind paths use.

        Called after the binding is persisted and inside the map lock, so it describes
        something that has happened and precedes anything that could route through it.
        Best-effort, matching :meth:`_note_inbound_unbind`: a broken sink must not turn
        a bind into a raise.
        """
        listener = _BIND_LISTENER
        if listener is None:
            return
        try:
            listener(key)
        except Exception:
            logger.warning("channel-bind listener failed for %s", key, exc_info=True)

    def _note_inbound_unbind(self, key: str, link: ChannelLink, reason: str) -> None:
        """Audit and announce the removal of one inbound resume binding.

        The choke point every removal path funnels through, so a binding cannot
        die traceless: the SEL event is the durable record (lifecycle logs are
        INFO and a production gateway logs WARNING and above), and the listener is
        what reaches the channel that just lost its way back. Called after the
        removal is persisted, so the audit describes something that happened, and
        both legs are best-effort — a broken sink or notifier must not turn an
        unlink, or a teardown reaching here from a ``finally``, into a raise.

        The reason is normalized HERE rather than trusted from the caller: this is
        the only place that sees every removal.
        """
        reason = _normalize_unbind_reason(reason)
        target = f"{link.channel_type}:{link.channel_id or ''}"
        if link.thread_id:
            target = f"{target}/{link.thread_id}"
        self._emit_unbind_audit(key, target, reason)
        listener = _UNBIND_LISTENER
        if listener is None:
            return
        try:
            listener(key, link, reason)
        except Exception:
            logger.warning("inbound-unbind listener failed for %s", key, exc_info=True)

    def _emit_unbind_audit(self, key: str, target: str, reason: str) -> None:
        """Write the removal's SEL event without blocking a caller on the loop.

        ``sel()`` does real filesystem work on its first call — it resolves the
        data home, creates the log and mints the HMAC trust root — and every write
        appends. This is reached from synchronous ``SessionMap`` calls a coroutine
        makes inline, so doing it here stalls the gateway for the disk I/O. The
        WHOLE closure, ``sel()`` included, therefore runs in the running loop's
        executor, with the Future retained and its result consumed so a failure
        logs instead of vanishing. Off the loop there is nothing to protect, so it
        runs inline — same single event either way, no thread per event.
        """

        def _emit() -> None:
            sel().log_api_access(
                caller="kirocrew",
                operation="session.inbound_unbind",
                outcome="success",
                # ``_infer_source`` is the canonical key->surface classifier, so
                # this event's surface cannot drift from the rest of the trail.
                source=_infer_source(key),
                resources=f"{key} -> {target} ({reason})",
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _emit()
            except Exception:
                logger.warning("inbound-unbind audit failed for %s", key, exc_info=True)
            return
        future = loop.run_in_executor(None, _emit)
        self._audit_futures.add(future)

        def _consume(done: "asyncio.Future[None]") -> None:
            self._audit_futures.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                logger.warning("inbound-unbind audit failed for %s: %s", key, exc)

        future.add_done_callback(_consume)

    @_guarded
    def _remove_entry(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> None:
        """Remove an entry and update reverse index.

        A dying entry takes any inbound binding it held with it, so the removal
        is announced here rather than at each caller — this is the only path by
        which a whole entry leaves the map.
        """
        entry = self._data.pop(key, None)
        if not entry:
            return
        inbound = self._inbound_binding(entry)
        ts = entry.get("slack_thread_ts")
        if ts and self._thread_to_session.get(ts) == key:
            del self._thread_to_session[ts]
        self._save()
        if inbound is not None:
            self._note_inbound_unbind(key, inbound, reason)

    @_guarded
    def set(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Save mapping and persist to disk, preserving existing slack fields."""
        key = canonical_key(key)
        existing = self._data.get(key)
        if existing:
            if existing.get(UNLANDED_SID_FIELD) not in (None, sid):
                existing.pop(UNLANDED_SID_FIELD, None)
            existing["sid"] = sid
            existing.pop(NATIVE_FORK_FIELD, None)
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

    @_guarded
    def clear_sid(self, key: str) -> bool:
        """Clear the stored session ID without removing the entry.

        Used on provider switch (the SID is incompatible with the new
        provider) and by the poisoned-conversation discard. The cleared sid
        is stashed as ``discarded_sid`` so the operation is diagnosable and
        manually reversible — the native conversation still exists on disk;
        only the pointer to it is dropped.

        Returns whether a pointer was actually dropped, so a caller clearing a
        SET of keys can report how many conversations it orphaned without a
        second lookup. ``get`` is the wrong probe for that: it gates on the
        transcript file existing and prunes stale entries as a side effect, so
        it answers "is this resumable" rather than "is a pointer recorded".
        """
        entry = self._data.get(canonical_key(key))
        if entry and _stash_and_clear_sid(entry):
            self._save()
            return True
        return False

    @_guarded
    def mark_sid_unlanded(self, key: str, sid: str) -> bool:
        """Record that *sid*, *key*'s current mapping, has not received its history.

        Written only over the sid the entry already holds: a stale caller naming an
        older or newer sid writes nothing, so the marker can never describe a
        session other than the mapped one. Written only for an entry whose resume
        :meth:`get` gates on it (:func:`resume_decided_by_load`). Returns whether
        it was written. See :data:`UNLANDED_SID_FIELD`.
        """
        entry = self._data.get(canonical_key(key))
        if not sid or not entry or entry.get("sid") != sid:
            return False
        if not resume_decided_by_load(entry.get("provider") or ""):
            return False
        if entry.get(UNLANDED_SID_FIELD) == sid:
            return True
        entry[UNLANDED_SID_FIELD] = sid
        self._save()
        return True

    @_guarded
    def sid_unlanded_pending(self, key: str) -> bool:
        """Whether *key*'s mapped sid carries the unlanded marker right now."""
        entry = self._data.get(canonical_key(key))
        return bool(entry and entry.get("sid") and entry.get(UNLANDED_SID_FIELD) == entry["sid"])

    @_guarded
    def clear_sid_unlanded(self, key: str) -> bool:
        """Drop *key*'s unlanded marker once a turn has delivered the history."""
        entry = self._data.get(canonical_key(key))
        if not entry or UNLANDED_SID_FIELD not in entry:
            return False
        entry.pop(UNLANDED_SID_FIELD, None)
        self._save()
        return True

    def get_discarded_sid(self, key: str) -> str:
        """Return the last sid dropped from *key* by any path, or ''.

        Written by every path that empties ``sid`` in place -- the provider
        switch, the startup prune and the per-read stale repair -- through
        :func:`_stash_and_clear_sid`. Naming only one of them here once let the
        two stale paths drop a sid without recording it, leaving an older id
        standing as the key's last store.
        """
        entry = self._data.get(canonical_key(key))
        if not entry:
            return ""
        return entry.get("discarded_sid", "")

    @_guarded
    def delete_if_sid(self, key: str, expected_sid: str) -> tuple[bool, str | None]:
        """Delete *key* only when its current SID equals *expected_sid*.

        The comparison and removal share one :data:`_MAP_LOCK` critical section,
        so a successor mapping cannot land between them. The return is
        ``(removed, current_sid)``: ``current_sid`` is ``None`` when no entry
        exists, and otherwise reports the mismatched value without changing it.
        """
        key = canonical_key(key)
        entry = self._data.get(key)
        if entry is None:
            return False, None
        current_sid = entry.get("sid")
        if current_sid != expected_sid:
            return False, current_sid if isinstance(current_sid, str) else ""
        self._remove_entry(key, reason=UNBIND_REASON_ENTRY_DELETED)
        return True, expected_sid

    @_guarded
    def delete(self, key: str, *, reason: str = UNBIND_REASON_ENTRY_DELETED) -> None:
        """Remove mapping and persist.

        The catch-all reason names the shape of the removal rather than its
        motive: a caller that knows why (a session teardown, a recycle) passes
        its own so the audit says which one happened.
        """
        self._remove_entry(canonical_key(key), reason=reason)

    @_guarded
    def prune(self) -> int:
        """Remove entries whose session files no longer exist.

        An entry carrying a DURABLE flag or a channel binding is never deleted,
        and when its ``sid`` has gone stale the ``sid`` is cleared instead —
        :func:`_keeps_entry` is the single predicate both stale branches ask,
        so neither can start discarding what the other keeps. A durable flag is a
        per-conversation SETTING, not session state: it can be written before the
        conversation has ever run a turn (``/unlink`` as the very first message
        leaves no ``sid``, no thread and no mirror), and it must outlive the
        native session the conversation happened to be using. A channel binding
        (``slack_thread_ts``, ``mirror``) is the conversation's identity: it is
        what routes an inbound channel message back to this session. Deleting the
        entry either way would silently revert user state at the next restart —
        the setting returns to the default they had just turned off, or the next
        message from the channel opens a fresh session instead of resuming the
        one it is bound to.

        A privacy-mode flag (a temporary or incognito thread) keeps its entry here
        too, and nothing else removes it either: the flag is the record the
        channel's inbound gate hydrates from, so the row stays until that gate
        can read the transcript header instead (:func:`_keeps_entry`, and the
        module comment above ``_PRIVACY_STRICTNESS``). What this loop-side,
        lock-held method must NOT do for such a row is read its transcript;
        ensuring the header records the mode is disk I/O, and
        :meth:`stamp_privacy_headers`, awaited right after this on the startup
        path, does it on a worker thread.

        Returns the number of entries removed; a ``sid``-only reset is a repair,
        not a removal, so it is not counted.

        Collection goes through :meth:`_remove_entry` rather than deleting out of
        ``_data``, so it inherits the audit and the announcement every other
        removal path gets. For bound entries that is unreachable --
        :func:`_survives_prune` holds every bound entry back -- and reaching the
        choke point anyway is the point: a future loosening of that predicate then
        lands on an audited path instead of silently collecting a live binding. On
        prune's only production path (``start_pool``, on the startup loop) the
        per-entry saves coalesce through ``_save``'s debounced deferred flush into
        one worker-thread write; off the loop each save writes inline, which no
        production caller does.
        """
        sessions_dir = _kiro_sessions_dir()
        stale: list[str] = []
        repaired = False
        for key, entry in self._data.items():
            # Only kiro-cli's transcripts are stat-able here; other backends
            # own their own storage. An absent label means kiro-cli.
            if (entry.get("provider") or PROVIDER_LABEL_DEFAULT) != PROVIDER_LABEL_DEFAULT:
                continue
            sid = entry.get("sid")
            survives = _keeps_entry(entry)
            if sid and not (sessions_dir / f"{sid}.json").exists():
                if survives:
                    _stash_and_clear_sid(entry)
                    repaired = True
                else:
                    stale.append(key)
            elif not sid and not survives:
                stale.append(key)
        if stale:
            for k in stale:
                self._remove_entry(k, reason=UNBIND_REASON_PRUNED_STALE)
            # Still a FULL rebuild, not the per-key index drop
            # ``_remove_entry`` already did: two entries can claim one
            # thread, and only a rebuild re-resolves the thread to the
            # survivor instead of leaving it ownerless.
            self._rebuild_thread_index()
            # One more dirty-mark after the rebuild. ``_save`` is loop-aware:
            # on prune's only production path (``start_pool`` on the startup
            # loop) the saves coalesce into one deferred flush whose disk
            # write runs on a worker thread — the loop still pays the
            # serialize, never the write. A ``batched_save`` here would write
            # inline at batch exit on that same loop.
            self._save()
            logger.info("Pruned %d stale session map entries", len(stale))
        elif repaired:
            # A sid-only reset still has to reach disk, or the next startup sees
            # the same stale sid and repairs it again forever.
            self._save()
        return len(stale)

    @_guarded
    def privacy_flagged_entries(self) -> dict[str, list[str]]:
        """Every entry carrying a privacy flag: key -> its flags, strictest first.

        The cheap, lock-held half of :meth:`stamp_privacy_headers`: it names the
        rows whose transcript header must record the mode and performs NO
        filesystem call. Every flagged row qualifies -- channel-bound or not,
        live ``sid``, cleared ``sid`` or none -- because the header is the record
        for the THREAD, not for the provider session that happened to serve it,
        and ensuring it is idempotent (tighten-only, never creating a
        transcript). Nothing here decides a removal: no privacy row is removed
        (:func:`_keeps_entry`).
        """
        return {
            key: flagged
            for key, entry in self._data.items()
            if (flagged := _privacy_flags_on(entry))
        }

    def privacy_flags(self, key: str) -> list[str]:
        """*key*'s privacy flags as the map holds them NOW, strictest first (``[]`` for none).

        The per-row re-read :meth:`stamp_privacy_headers` makes once it holds
        the row's durable-write lock: the snapshot it walks only names
        candidates, and what the row says under the lock decides whether its
        header is stamped. A single-key probe, undecorated like
        :meth:`get_flag` (the class docstring's threading contract); *key* is a
        canonical map key, as the snapshot names them.
        """
        entry = self._data.get(key)
        return _privacy_flags_on(entry) if isinstance(entry, dict) else []

    async def stamp_privacy_headers(self) -> int:
        """Ensure every privacy-flagged row's EXISTING transcript header records its mode.

        The startup path awaits this right after :meth:`prune`. The rows are
        NAMED under the map lock with no filesystem call
        (:meth:`privacy_flagged_entries`) -- a snapshot of candidates, nothing
        more -- and each is then stamped one at a time under that row's
        durable-write lock (``privacy_mode._serialized``, the lock the
        modifier's commit and a reservation's release hold across their own
        row-and-header sequences): the row is re-read under the lock
        (:meth:`privacy_flags`), a row the re-read finds unflagged is skipped,
        and only a row still flagged has its transcript header probed and --
        for a row flagged before the modifier stamped headers, or tightened
        since -- written, on a worker thread
        (:func:`_header_records_privacy_mode`: tighten-only,
        ``require_existing``, so a transcript that does not exist stays that
        way). The event loop never pays a transcript read for these rows and
        the map lock is never held across one.

        Serialized with the release because the snapshot goes stale: on a
        non-blocking pool start the sweep runs as a detached task while traffic
        flows, and a reservation released after the snapshot restores the
        header and clears the row under ITS lock. Unserialized, a stamp from
        the snapshot alone writes the released mode back into a header the
        release has just restored, over a row that is gone -- and nothing
        loosens a header from there: the sweep only tightens, from rows, and a
        later release of the same mode reads the stale stamp as its own
        ``header_before`` and restores to it, so the thread's consolidation
        stays refused for good. Under the row's lock the stamp lands either
        before the release, which then finds the header still saying the mode
        and restores it, or after it, when the re-read finds no flag and
        nothing is written.

        REMOVES NOTHING. A privacy row is the record the channel's inbound gate
        hydrates from, and removing it -- however complete the header -- leaves
        that gate reading the thread as persistent after the next restart (the
        module comment above ``_PRIVACY_STRICTNESS``). A flag tightened DURING
        the probe leaves the header stamped with the mode the worker saw: never
        looser than before, since the stamp is tighten-only, and never lost,
        since the next pass reads the current flags and re-stamps the tightened
        mode. Returns the number of rows whose header records the mode
        afterwards -- housekeeping telemetry, nothing acts on it.
        """
        recorded = 0
        for key in self.privacy_flagged_entries():
            async with _serialized(key):
                flagged = self.privacy_flags(key)
                if not flagged:
                    continue
                if await asyncio.to_thread(_header_records_privacy_mode, key, flagged):
                    recorded += 1
        return recorded

    @_guarded
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

    @_guarded
    def reserve_generation(self, session_key: str) -> None:
        """Persist the generation in *session_key* before its first provider turn.

        The watermark lives on the stable bucket entry instead of materializing
        one map row per empty generation. It is monotonic: a delayed or repeated
        command can never lower the restart seed and make an older history key
        reusable.
        """
        parsed = split_dm_session_key(canonical_key(session_key))
        if parsed is None:
            raise ValueError(f"not a canonical DM session key: {session_key!r}")
        bucket, generation = parsed
        if generation <= 0:
            return
        entry = self._ensure_entry(bucket)
        current = entry.get(GENERATION_FLOOR_FIELD)
        if isinstance(current, int) and not isinstance(current, bool) and current >= generation:
            return
        entry[GENERATION_FLOOR_FIELD] = generation
        self._save()

    @_guarded
    def max_generation(self, bucket: str) -> int:
        """Return the highest persisted DM generation for a session *bucket*.

        The bucket is the generation-0 key (e.g.
        ``telegram:<agent>:direct:<user>``); generations persist as ``{bucket}``
        (gen 0) and ``{bucket}:gen{N}``. An explicit ``/new`` also records a
        monotonic generation floor on the bucket before the first provider turn.
        Returns the highest of those sources, or -1 when the bucket has none.
        """
        bucket = canonical_key(bucket)
        best = 0 if bucket in self._data else -1
        entry = self._data.get(bucket)
        floor = entry.get(GENERATION_FLOOR_FIELD) if entry else None
        if isinstance(floor, int) and not isinstance(floor, bool):
            best = max(best, floor)
        prefix = f"{bucket}:gen"
        for key in self._data:
            if key.startswith(prefix):
                suffix = key[len(prefix) :]
                if suffix.isdigit():
                    best = max(best, int(suffix))
        return best

    @_guarded
    def find_key_by_sid(self, session_id: str, *, exclude: str = "") -> str | None:
        """Find the session map key for a given kiro-cli session ID.

        *exclude* skips one key, for a caller asking whether ANOTHER key maps the same
        session -- the question "is this session still somebody's" cannot be answered by
        a lookup that can return the very key the caller is retiring.
        """
        for k, entry in self._data.items():
            if exclude and k == exclude:
                continue
            sid = entry.get("sid") if isinstance(entry, dict) else entry
            if sid == session_id:
                return k
        return None

    @_guarded
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

    # --- v1c-B: per-conversation state on the session entry ---------------
    # Durable backing for per-thread state (``temporary``, ``incognito``,
    # ``agents``, ``projects``). Storing it on the session entry makes it
    # survive gateway restarts and ties its lifetime to the session (pruned
    # with the entry) rather than an ad-hoc bounded LRU in module-global dicts.

    @_guarded
    def _ensure_entry(self, key: str) -> dict:
        """Return the entry for *key*, creating a blank one if absent."""
        entry = self._data.get(key)
        if entry is None:
            entry = {"sid": "", "slack_thread_ts": None, "slack_channel_id": None}
            self._data[key] = entry
        return entry

    @_guarded
    def set_flag(self, key: str, flag: str, value: bool) -> None:
        """Set or clear a boolean per-conversation flag (e.g. ``temporary``).

        Flags are stored under an ``flags`` sub-dict on the entry. Clearing the
        last flag removes the sub-dict so empty state does not accrete on disk.
        Idempotent: writing an unchanged value still persists (cheap) so callers
        need not pre-check.

        Setting a PRIVACY flag on an entry that carries none yet makes that
        entry a retained row (see the module comment above ``_PRIVACY_STRICTNESS``),
        so it passes the row gate first: for a key over ``PRIVACY_ROW_KEY_MAX``
        (``"key_too_long"``), then past ``PRIVACY_ROW_CAP`` retained rows
        (``"limit"``; the count is one pass over the map, paid only when a new
        row is admitted), :class:`PrivacyRowRefused` carrying that reason is
        raised and nothing is written -- fail-closed, the caller refuses the
        turn. Tightening a row already retained (``incognito`` then
        ``temporary``) is never refused: it adds no row. The gate lives here and
        nowhere else: a caller that must know before an irreversible step
        (``privacy_mode.reserve``) applies the flag ahead of that step and takes
        it back if the step fails, rather than pre-checking a count another
        writer can change under it.
        """
        key = canonical_key(key)
        if (
            value
            and flag in _PRIVACY_STRICTNESS
            and not _privacy_flags_on(self._data.get(key) or {})
        ):
            if len(key) > PRIVACY_ROW_KEY_MAX:
                raise PrivacyRowRefused(key, "key_too_long")
            retained = sum(1 for entry in self._data.values() if _privacy_flags_on(entry))
            if retained >= PRIVACY_ROW_CAP:
                raise PrivacyRowRefused(key, "limit")
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

    @_guarded
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

    @_guarded
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

    @_guarded
    def set_native_fork(self, key: str, source_sid: str, message_id: str) -> None:
        """Ask *key*'s next cold start to fork *source_sid* at *message_id*."""
        self._ensure_entry(canonical_key(key))[NATIVE_FORK_FIELD] = {
            "sid": source_sid,
            "message_id": message_id,
        }
        self._save()

    def get_native_fork(self, key: str) -> tuple[str, str] | None:
        """The pending ``(source_sid, message_id)`` fork for *key*, or None."""
        entry = self._data.get(canonical_key(key))
        fork = entry.get(NATIVE_FORK_FIELD) if entry else None
        if not isinstance(fork, dict):
            return None
        source_sid, message_id = fork.get("sid"), fork.get("message_id")
        if not (
            source_sid
            and isinstance(source_sid, str)
            and message_id
            and isinstance(message_id, str)
        ):
            return None
        return source_sid, message_id

    @_guarded
    def clear_native_fork(self, key: str) -> None:
        """Drop *key*'s pending native fork, if any."""
        entry = self._data.get(canonical_key(key))
        if entry and entry.pop(NATIVE_FORK_FIELD, None) is not None:
            self._save()

    # The map's critical section and its file: ``kiro_crew.session_map_store``.
    batched_save = _store.batched_save
    _load = _store._load
    _save = _store._save
    _take_pending_snapshot = _store._take_pending_snapshot
    _serialize = _store._serialize
    _restore_dirty = _store._restore_dirty
    _claim_for_flush = _store._claim_for_flush
    flush = _store.flush
    aflush = _store.aflush
    aclose = _store.aclose
    _write = _store._write
    _write_payload = _store._write_payload

    # A session's channel links: ``kiro_crew.session_map_links``.
    _rebuild_thread_index = _links._rebuild_thread_index
    _inbound_binding = staticmethod(_links._inbound_binding)
    _new_binding_nonce = staticmethod(_links._new_binding_nonce)
    _is_self_derived = staticmethod(_links._is_self_derived)
    _evict_rival_claimants = _links._evict_rival_claimants
    set_slack_link = _links.set_slack_link
    get_slack_link = _links.get_slack_link
    clear_slack_link = _links.clear_slack_link
    set_slack_paused = _links.set_slack_paused
    is_slack_paused = _links.is_slack_paused
    get_session_for_thread = _links.get_session_for_thread
    set_mirror_link = _links.set_mirror_link
    mirror_claim_blockers = _links.mirror_claim_blockers
    _mirror_key = _links._mirror_key
    get_mirror_link = _links.get_mirror_link
    mirror_accepts_inbound = _links.mirror_accepts_inbound
    has_mirror_row = _links.has_mirror_row
    mirror_link_nonce = _links.mirror_link_nonce
    slack_link_nonce = _links.slack_link_nonce
    find_mirror_sessions = _links.find_mirror_sessions
    clear_mirror_links_at = _links.clear_mirror_links_at
    clear_mirror_link = _links.clear_mirror_link
    clear_mirror_link_if = _links.clear_mirror_link_if
    replace_mirror_link_if = _links.replace_mirror_link_if
    clear_slack_link_if = _links.clear_slack_link_if
    set_mirror_paused = _links.set_mirror_paused
    is_mirror_paused = _links.is_mirror_paused
    get_link = _links.get_link
    set_link = _links.set_link


#: The modules whose ``self``-first functions :class:`SessionMap` binds, lowest layer first:
#: each imports only the ones before it, and none imports this module at run time.
#: ``test/test_session_map_bindings.py`` pins the bindings and that direction.
_OWNERS: tuple[str, ...] = ("kiro_crew.session_map_store", "kiro_crew.session_map_links")


_own_methods(SessionMap, frozenset(_OWNERS), module=__name__)
