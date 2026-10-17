"""The reads and screens a restore makes before it builds a slot.

Most of this is disk work or a pure screen that touches no slot state, which is
what lets the async restore drivers run it on a worker thread: the agent-to-model
map, the restore config, the open-tab snapshot and its key screen, the committed
agent choice, the delete-during-read witness, the app-owned channel-row screen and
the recent-session key map. The MCP-app claim recovery pairs its spool read with
the in-memory apply that follows a build. The drivers, the prefetch helpers and the
slot builders stay in ``chat_persistence``.

New restore-time reads and screens belong here.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from kiro_crew import mcp_apps_render
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.session_agent_selection import session_agent_selection_name

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")

#: The open-tab registry's file name, and the previous generation kept beside it.
#:
#: One pair of constants because three sites name these files -- the writer in
#: ``dashboard_persistence``, the restore read below, and the pre-restore merge read --
#: and a name spelled three times is a name that drifts.
OPEN_SLOTS_FILE = "open_slots.json"
OPEN_SLOTS_PREV_FILE = "open_slots.json.prev"


def open_slot_keys_of(text: str) -> "list[object] | None":
    """The raw ``keys`` list *text* states, or ``None`` when it states no answer.

    ``None`` and ``[]`` are different facts, and a caller that flattens them loses
    the whole point of keeping a previous generation. ``[]`` is an ANSWER -- the file
    parsed and says no tab was open, which is what closing the last tab writes.
    ``None`` is DAMAGE or absence: the text is empty or truncated (what an unclean
    reboot leaves behind on a filesystem that had not committed the data blocks), or
    parses to a shape with no ``keys`` list in it at all.

    That split is what makes the fallback safe. Falling back on ``[]`` would
    resurrect every tab the user deliberately closed, permanently, because the
    previous generation still lists them.

    Takes the TEXT rather than a path so a caller that must screen and then copy the
    same bytes can do both from one read.

    Entries are returned UNVALIDATED -- see :func:`_sanitize_open_slot_key`.
    """
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except Exception:
        logger.debug("open-tab snapshot is not parseable JSON", exc_info=True)
        return None
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return None
    return list(keys)


def open_slot_keys_usable(text: str) -> bool:
    """Whether *text* is a generation worth keeping as the fallback."""
    return open_slot_keys_of(text) is not None


class OpenSlotsUnreadable(OSError):
    """A registry generation exists but could not be READ this instant.

    The third state, and it is not a flavour of "no tabs". A file that is absent or
    damaged is an ANSWER about the open-tab set; a file the OS refused to hand over
    -- a Windows sharing violation from a foreign handle is the live example -- says
    only that the answer is unknown right now.

    Collapsing the two is how a transient failure destroys data: the caller reads
    "no keys", writes a snapshot built from that, and the intact generation it could
    not read a moment ago is gone. So the distinction is carried in the type system
    rather than in a comment, and every caller has to decide what to do with it.
    """


def open_slot_keys_in(path: "Path") -> "list[object] | None":
    """:func:`open_slot_keys_of` for the file at *path*; ``None`` when it has no answer.

    ``None`` is absence or damage, which are answers about this path. A file that
    exists and could not be read raises :class:`OpenSlotsUnreadable` instead, because
    no return value of this shape can carry "unknown" without a caller mistaking it
    for "empty".

    Undecodable BYTES are damage, not a failed read. A torn write can leave a
    fragment that is not valid UTF-8 at all, and ``read_text`` reports that as a
    ``UnicodeDecodeError`` -- which is a ``ValueError``, so it is caught here
    explicitly rather than by the ``OSError`` clause. Letting it escape would abort
    dashboard startup, from a file whose whole purpose is to be recoverable, and
    without ever consulting the generation beside it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        logger.debug("%s is not valid UTF-8", path.name, exc_info=True)
        return None
    except OSError as exc:
        logger.debug("%s unreadable", path.name, exc_info=True)
        raise OpenSlotsUnreadable(f"{path.name} could not be read") from exc
    return open_slot_keys_of(text)


def read_open_slot_keys(config_dir: "Path") -> "tuple[list[object], bool]":
    """The open-tab keys to restore from, and whether the set is actually KNOWN.

    The durable read behind both restore drivers. When the current file holds no
    answer (:func:`open_slot_keys_in` returns ``None``) the previous generation is
    read instead, which is what turns a crash that truncated the live file from
    "16 tabs are gone" into "the tabs come back one generation stale".

    The second element is the one that prevents a second data loss. ``([], False)``
    means NOBODY LOOKED SUCCESSFULLY -- every generation that might hold the set was
    unreadable -- and a caller that treats it as "no tabs were open" prunes the file
    down to whatever is live, destroying the set it just failed to read. ``([], True)``
    is the documented no-op: the registry really is empty.
    """
    try:
        keys = open_slot_keys_in(config_dir / OPEN_SLOTS_FILE)
    except OpenSlotsUnreadable:
        keys = None
        current_known = False
    else:
        # It ANSWERED. ``None`` here is "absent or damaged", which is a fact about
        # the live file, not a failure to learn one -- only the raise above is that.
        current_known = True
    if keys is not None:
        return keys, True
    try:
        previous = open_slot_keys_in(config_dir / OPEN_SLOTS_PREV_FILE)
    except OpenSlotsUnreadable:
        logger.warning(
            "neither %s nor %s could be read; the open-tab set is unknown this boot",
            OPEN_SLOTS_FILE,
            OPEN_SLOTS_PREV_FILE,
        )
        return [], False
    if previous is None:
        # Both generations answered, and the answer is that there is no set to
        # restore -- UNLESS the current file was the one we could not read, in which
        # case an absent aside says nothing about what the live file holds.
        return [], current_known
    logger.warning(
        "%s holds no usable key list; restoring the open-tab set from %s (%d key(s))",
        OPEN_SLOTS_FILE,
        OPEN_SLOTS_PREV_FILE,
        len(previous),
    )
    # ``current_known``, not ``True``. The aside answering does not make the LIVE
    # set known, and the aside is a generation behind by construction: a current
    # file holding [A, B] whose read was refused, beside an aside holding [A], would
    # otherwise restore [A], call it authoritative, and let the next flush delete B
    # from a live file nobody ever managed to read. Restoring the stale set is still
    # right -- stale tabs beat absent ones -- but it must not license a prune.
    return previous, current_known


def _build_kiro_model_map() -> dict[str, str]:
    """Map kiro-agent name/stem -> configured model, for legacy sessions.

    Sessions persisted before ``model`` was written into their metadata resolve
    their model by agent name instead, so both restore paths need this map.
    Factored out of ``_rehydrate_slot_from_history`` and
    ``restore_recent_sessions`` because the former rebuilt it *per restored
    slot* — re-globbing and re-parsing every agent JSON on each of N tabs to
    produce a byte-identical dict. Callers restoring many slots should build it
    once and pass it down (the ``kiro_model_map`` parameters of the restore paths
    in ``chat_persistence``).
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    try:
        return cp.agent_model_map(
            agents_dir=cp.kiro_agents_dir_path(),
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
    whole thing into ``asyncio.to_thread``: inline, the read plus the JSON parse
    run on the event loop during startup.

    Entries are returned UNVALIDATED — the file is attacker-writable, so every
    caller must pass each one through :func:`_sanitize_open_slot_key` before it
    reaches path construction. Returns ``[]`` when neither the current file nor
    the previous generation holds a key list, which is the documented no-op.

    A file that holds no answer falls back to the previous generation
    (:func:`read_open_slot_keys`): the live file is rewritten in place every time the
    open-tab set changes, so a crash before its data blocks commit leaves a
    zero-length file in place of the whole working set, and reading that as "no
    tabs were open" is what makes the loss permanent.

    Drops the "was the set knowable" half of that read. Use
    :func:`_read_open_slots_snapshot` wherever a write may follow -- an empty list
    here can mean nobody looked successfully.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # Through the facade, not as a bare global: tests rebind the snapshot read on
    # ``chat_persistence``, and an owner reading its own module global would answer
    # from the real disk while the test believes it patched the read.
    return cp._read_open_slots_snapshot()[0]


def _read_open_slots_snapshot() -> "tuple[list[object], bool]":
    """:func:`_read_open_slots_keys` with the ``known`` flag kept.

    What the restore drivers read, because they go on to decide whether the live slot
    map may be treated as the authoritative open-tab set. Unknown must not license
    that: a flush would then publish a set assembled from an unsuccessful read.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    return read_open_slot_keys(cp.config_dir())


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
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    if not isinstance(raw, str) or not raw:
        return None
    if "/" in raw or "\\" in raw:
        logger.warning("restore_open_slots: rejecting key with path separators: %r", raw)
        return None
    # Fold to the canonical (filename-charset) key. An on-disk snapshot may
    # carry a raw display-style key (e.g.
    # "Artifact: My Doc") alongside its sanitized twin — after folding, the
    # second form hits the caller's dedup guard instead of restoring a duplicate
    # sidebar session backed by the same transcript.
    return cp._normalize_slot_key(raw)


def _restored_agent_name(session_key: str, meta: dict) -> str:
    """Restore the captured choice without changing member identity.

    History can retain a provisional agent after an interrupted switch. The
    canonical execution record is the committed choice; the runner verifies its
    namespace, revision and captured member/store assignment before use.
    An unreadable record leaves the transcript display intact, and the runner's
    strict read refuses execution rather than treating that display as authority.
    """
    try:
        selected = session_agent_selection_name(session_key)
    except UnknownMemoryStore:
        logger.warning("Could not read restored agent selection for %s", session_key, exc_info=True)
        selected = None
    agent = meta.get("agent")
    return selected or (agent if isinstance(agent, str) else "")


def _deletion_during_read(
    conv_log: ConversationLog,
    history_key: str,
    pre_meta: dict,
    pre_messages: list[dict] | None,
) -> str | None:
    """Was *history_key* deleted (or deleted-and-recreated) during a prefetch?

    Returns a short reason for logging, or ``None`` when it is safe to build.

    Offloading a transcript read opens a window an atomic on-loop read-then-build
    does not have: ``ConversationLog.delete_session``
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


def _reconcile_mcp_app_claims(slot: _ChatSlot, claims: list[set[str]]) -> None:
    """Recover app flags whose claim reached disk but whose row flag did not.

    A gateway death between ``_take_claim``'s sidecar write and the slot save
    that would have persisted ``meta["mcp_app"]`` leaves a SPENT claim on an
    unflagged row, so the app is gone with nothing saying it existed. Nothing
    else recovers it: the in-turn recovery is in ``handle_tool_result``, whose
    only caller is the live turn path, and the marker that would let a replay
    re-detect it was stripped before the row was written.

    Best-effort by construction. It is a display flag, not state anything else
    reads, so a failure here must leave the restore itself untouched -- a session
    that loads without a notice is the state we already had, while a restore that
    raises loses the whole transcript.

    Two phases, split the way every restore path splits: *claims* is read on
    a worker thread by :func:`_read_mcp_app_claims` and handed in, while this half
    only touches memory and is safe on the loop. Call it AFTER the restore has
    marked the slot clean -- a recovered flag has to leave it dirty again, since
    the sidecar it came from is swept at its TTL and an unsaved recovery is a
    notice lost for good.
    """
    try:
        if mcp_apps_render.apply_claimed_rows(claims, slot.messages):
            slot._dirty = True
    except Exception:
        logger.debug("mcp-apps claim reconcile failed for %s", slot.key, exc_info=True)


def _read_mcp_app_claims(session_key: str) -> list[set[str]]:
    """One row group per spent claim for *session_key*, read from the spool.

    Grouped rather than flattened: each claim is one app occurrence and only the
    grouping says which rows belong to which, so a flat set would let one
    occurrence's lead marker suppress another's after a reset reissued a call id.

    Takes the KEY, not a slot, because one caller has no slot yet: the targeted
    rehydration must read the spool in its prefetch phase, before the slot exists
    at all. Every caller must pass the CANONICAL producing-session key -- what
    ``effective_session_key`` answers for a built slot and ``session_key_for`` for
    a name plus its persisted link -- since the claim records that key and the bare
    slot key every other chat event routes on matches nothing here, recovering
    nothing in silence.

    This is the FILESYSTEM half, so a loop-affine caller must hand it to a worker
    thread (:func:`_recover_mcp_app_claims_async`). Best-effort: an unreadable
    spool returns nothing to recover rather than failing a restore.
    """
    try:
        return mcp_apps_render.load_claimed_row_groups(session_key)
    except Exception:
        logger.debug("mcp-apps claim read failed for %s", session_key, exc_info=True)
        return []


def _recover_mcp_app_claims(slot: _ChatSlot) -> None:
    """Recover app flags for *slot*, reading the spool inline.

    For the restore drivers whose reads are inline by construction -- the
    generator behind :func:`restore_open_slots` and its recent-sessions twin. A
    loop-affine driver must use :func:`_recover_mcp_app_claims_async` instead, or
    it stalls the gateway on a spool scan.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    _reconcile_mcp_app_claims(slot, cp._read_mcp_app_claims(effective_session_key(slot)))


def _is_app_owned_channel_row(meta: dict, history_key: str) -> bool:
    """A persisted row an APP owns whose conversation is a channel thread.

    The two cannot go together: a channel thread is the person's conversation,
    and ``get_or_create_slot`` refuses to bind an app-owned slot to one. A row
    of this shape is the artifact of the earlier auto-bind (an app naming its
    slot after a channel stem) and is not surfaced — restoring it would load the
    channel transcript into an app's slot. Its file is left untouched, and the
    skip is logged so a session that stops appearing at boot can be traced.
    """
    if not str(meta.get("app") or ""):
        return False
    linked = str(meta.get("linked_session_key") or "")
    if is_channel_session_key(history_key) or (bool(linked) and is_channel_session_key(linked)):
        logger.warning(
            "restore: not surfacing app-owned row %s (app=%s, linked=%s) — a channel "
            "thread is never an app's slot; the file is left as is",
            history_key,
            str(meta.get("app") or "")[:64],
            linked[:64] or "-",
        )
        return True
    return False


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
