"""Session transfer — copy a session between Kiro Crew instances.

Two halves live here:

* :func:`build_transfer_bundle_async` serialises one slot's visible conversation
  into a portable, version-tagged dict. Called on the **sending** side.
* :func:`api_chat_slot_import` accepts such a dict and materialises it as a new
  slot. Called on the **receiving** side.

The wire hop between them is an ordinary authenticated dashboard request over
an Instances tunnel; see [instances.md](../../../docs/system-specs/modules/instances.md) §14.

**Two layers travel.** *Layer A* is the visible transcript (the bundle's
``messages``) — what the imported tab DISPLAYS. *Layer B* (bundle_version 2) is
the kiro-cli context window itself (``<sid>.json`` + ``<sid>.jsonl``, stored
outside the crew home and joined via ``session_map.json``): carrying it lets the
imported session RESUME with full fidelity through ``session/load`` rather than
replaying the transcript as a lossy ~8K prefix. Layer B is optional — a v1
sender, or a session that never opened a kiro-cli context, ships Layer A only
and the peer falls back to the prefix. Sub-agent conversations do NOT travel;
their results already live inside Layer B as injected context.

**Copy, never move.** Import always allocates a NEW slot key and never touches
an existing session, so a transfer leaves the source intact and can be repeated
safely. Nothing here deletes anything.

**What deliberately does NOT travel.** A session's transcript is portable text,
but most of its *metadata* is a reference into the local instance's object graph
— a project path, a folder id, a workspace's memory, an agent template, a bound
artifact. Carrying those across would produce dangling references that render
as broken UI on arrival, so the bundle carries the transcript, the title, and an
agent *hint* only:

* ``project`` is intentionally dropped. The source's checkout path almost never
  exists on the target host (a Mac worktree path on a Linux dev desk), and a
  slot pointing at a missing directory scopes file search and steering to
  nothing. The imported session arrives with no project so the user re-picks it.
* ``model`` is not carried. Accounts differ in entitlement, so a model id that
  the source account is served can fail at runtime on the target; the target
  resolves its own default instead (see AGENTS.md § Model selection).
* ``workspace`` is not carried. Workspaces are per-instance memory scopes, and a
  name that matches on both hosts still means two different memories.
* ``agent`` is carried as a hint and applied ONLY if the target has an agent by
  that name; otherwise it is dropped rather than left dangling.
* ``folder_id``, ``tags``, ``pinned``, ``artifact``, ``app``,
  ``linked_session_key`` and ``forked_from`` are all local-graph references and
  are not carried at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat, validation
from kiro_crew.agent_discovery import list_agents
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_sessions_dir

# Layering: this module may import chat_handlers, never the reverse — the only
# consumer of session_transfer is handlers_instances, which chat_handlers'
# transitive graph does not reach. If chat_handlers ever needs session_transfer,
# move the shared collaborators down to chat_persistence first rather than
# creating the cycle.
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop, session_was_deleted
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.state import (
    _MAX_SLOT_MESSAGES,
    MAX_LIVE_SLOTS,
    DashboardState,
    _ChatSlot,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_directive import SECTION_MARKER_ROLE

logger = logging.getLogger(__name__)

#: Bundle schema version. Bump on any incompatible change to the payload shape;
#: the importer refuses a version it does not know rather than guessing, because
#: the two ends of a transfer are independently-updated installs and a silently
#: misread field would land as corrupted conversation.
BUNDLE_VERSION = 2

#: Versions this importer still accepts. v1 = transcript-only (the peer rebuilds
#: context via the lossy ~8K history prefix); v2 additionally carries **Layer B**
#: — the kiro-cli context window (``<sid>.json`` + ``<sid>.jsonl``) — so an
#: imported session resumes with full fidelity via ``session/load`` instead of a
#: replayed prefix. Accepting both lets a newer instance still receive a copy
#: from an older one; anything OUTSIDE the set is refused rather than
#: best-effort parsed, because a silently misread field lands as corrupted
#: conversation.
_SUPPORTED_BUNDLE_VERSIONS = (1, 2)

#: Row-contributing element classes in a bundle: transcript messages and section
#: markers. Named, not a literal 2, so a THIRD class has to change this line.
_BUNDLE_ELEMENT_CLASSES = 2

#: Per-bundle limits. A bundle arrives from another instance, so it is untrusted
#: input even though the peer is one the owner configured: these bound the work
#: a single request can cause before any of it is written to disk.
_MAX_MESSAGES = _MAX_SLOT_MESSAGES // _BUNDLE_ELEMENT_CLASSES
_MAX_CONTENT_CHARS = 1_000_000
_MAX_TITLE_CHARS = 500
_MAX_TOTAL_CHARS = 20_000_000

#: Cap on the Layer B events blob (``<sid>.jsonl``). Larger than the transcript
#: cap because Layer B also carries tool/system frames and the full context the
#: model actually holds, but still bounded: an oversized blob is refused before
#: anything is written, so a peer cannot make an import exhaust disk or memory.
_MAX_LAYER_B_CHARS = 40_000_000

#: Yield to the event loop once this many characters of message content have been
#: processed without a yield. Bounds how long the import can hold the loop, so a
#: large bundle degrades into "the tab appears a moment later" rather than
#: starving the liveness heartbeat into a watchdog-triggered gateway exit.
_YIELD_AFTER_CHARS = 262_144

#: How many times to re-take the transcript snapshot when the periodic flush
#: lands inside the off-loop read. Small on purpose: the flush is 5s-periodic, so
#: even one interleave is rare and a second is vanishingly unlikely. Exhausting
#: these falls back to a guaranteed-consistent inline read rather than shipping a
#: transcript that might be missing turns.
_SNAPSHOT_ATTEMPTS = 4


class SnapshotUnstable(RuntimeError):
    """No consistent view of the source transcript could be taken.

    Two causes: the periodic flush kept landing inside the off-loop read, or a
    rewind/regenerate rewrite is still owed so the on-disk transcript is stale.

    Raised instead of bundling anyway or falling back to a blocking inline read.
    A transfer is a copy, so failing it is cheap and the caller can retry, whereas
    shipping the bundle would send the wrong conversation and a synchronous read
    of a large transcript on the event loop can starve the liveness heartbeat
    until the watchdog exits the gateway.
    """


#: Roles that make up a visible conversation. Tool/system frames are not carried:
#: they reference local tool state that means nothing on the target instance.
_VISIBLE_ROLES = ("user", "assistant")

#: Prefix marking an imported session in the sidebar, so a transferred tab is
#: never mistaken for one that originated locally.
_IMPORT_TITLE_MARKER = "⇄ "


def local_instance_label() -> str:
    """A short human label for THIS instance, used as a transfer's ``origin``.

    The local instance is implicit in the registry and has no configured name
    (instances.md §1), so there is nothing to read: the host's first DNS label
    is the most recognisable stand-in and is short enough to sit in a session
    title. Falls back to ``"another instance"`` rather than raising, because a
    missing label must never fail a transfer.
    """
    try:
        return platform.node().split(".")[0] or "another instance"
    except Exception:
        return "another instance"


def _read_chained_history(state: DashboardState, session_key: str) -> list[dict]:
    """Read a session's full on-disk transcript. **Blocking** — file IO + JSON.

    Split out so a caller on the event loop can push it to a thread; see
    :func:`build_transfer_bundle_async`.
    """
    if state.conversation_log:
        return state.conversation_log.read_messages_chained(session_key)
    return []


def _events_jsonl_is_loadable(events: str) -> bool:
    """Whether an inbound Layer B events blob is structurally usable as JSONL.

    **Parses only — never re-serialises.** That distinction is the whole point:
    the previous version redacted each record and wrote it back, which is exactly
    what invalidated the thinking-block signatures the conversation depends on.
    Validation reads; it does not rewrite. The caller stores the original string
    unchanged.

    A non-blank record that does not parse rejects the WHOLE blob. Installing
    malformed JSONL as the peer's resumable context makes its ``session/load``
    fail later and silently fall back to transcript replay -- while this side has
    already reported ``resume_mode: session_load``, i.e. a lie. Refusing here
    degrades honestly instead (``prefix`` -> the row reads "Sent (transcript
    only)").

    Applied on BOTH sides, and cheap enough to be: the sender catches a
    crash-truncated file before pushing megabytes through the tunnel, and the
    receiver re-checks because it must not trust the peer. Both callers keep the
    ORIGINAL string; neither writes back what this parsed.
    """
    if not events:
        return True
    for line in events.split("\n"):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception:
            return False
    return True


def _resolve_layer_b_sid(sessions: Any, sm_key: str) -> str:
    """Resolve *sm_key*'s resumable sid. **MUST run on the event loop.**

    ``resumable_sid`` goes through ``SessionMap.get``, which SELF-PRUNES entries
    whose session files are gone -- a write, and therefore subject to the same
    on-loop contract as every other ``SessionMap`` access. Split out so the
    blocking file read can take the resulting sid into a worker thread without
    carrying a handle to the live map.
    """
    if sessions is None:
        return ""
    try:
        return sessions.resumable_sid(sm_key) or ""
    except Exception:
        logger.debug("session_transfer: session_map lookup failed for %s", sm_key, exc_info=True)
        return ""


def _read_layer_b(sid: str) -> dict[str, Any] | None:
    """Read Layer B (the kiro-cli context) for *sid*. **Blocking IO, thread-safe.**

    Layer A (the transcript in the bundle's ``messages``) is only the DISPLAY
    copy. Layer B is the model's actual context window plus tool/compaction
    state, stored OUTSIDE the crew home at ``kiro_sessions_dir()/<sid>.{json,jsonl}``
    and joined to a slot through ``session_map.json``. Carrying it is what makes
    an imported session RESUME with full fidelity (``session/load``) instead of
    replaying the transcript as a lossy ~8K prefix.

    Takes an already-resolved **sid**, never the live ``SessionManager``: the
    lookup that produces it (``resumable_sid`` -> ``SessionMap.get``) SELF-PRUNES
    entries whose files are gone, so it is a map *mutation* and must run on the
    event loop -- the same threading contract that governs the join
    (``subagent.py``: all ``SessionMap`` access stays on the loop because the map
    is an unlocked dict with whole-file saves). The caller resolves the sid on the
    loop and hands this function nothing but an immutable string.

    Returns ``{"sid", "envelope", "events"}`` or ``None`` when there is no Layer B
    (no sid, or the files are missing). Never raises — a transfer must degrade to
    transcript-only rather than fail.
    """
    if not sid:
        return None
    if not sid:
        return None
    try:
        d = kiro_sessions_dir()
        jf = d / f"{sid}.json"
        lf = d / f"{sid}.jsonl"
        if not jf.exists() or not lf.exists():
            return None
        # Cap BEFORE the read, not after. ``read_text`` on a multi-gigabyte
        # tool-output log allocates the whole blob first, so a post-read ``len``
        # check bounds nothing -- the allocation that OOMs the gateway has
        # already happened by the time it runs. ``st_size`` is the only bound
        # available ahead of the allocation, and it covers the ENVELOPE too: that
        # read is unbounded on the same path, and a session's ``.json`` grows
        # with its own metadata.
        #
        # This makes the ceiling effectively a BYTE cap where the name says
        # chars. For multibyte text that is strictly tighter -- a 40M-char CJK
        # log is ~120MB and now degrades to transcript-only where it previously
        # loaded -- and that is the correct direction for a memory-safety limit:
        # the ceiling has to bound what is actually allocated, and the fallback
        # is an honest transcript-only copy rather than a crashed gateway. The
        # char check below stays as the semantic cap.
        for f in (jf, lf):
            if f.stat().st_size > _MAX_LAYER_B_CHARS:
                logger.debug(
                    "session_transfer: Layer B file %s exceeds the %d-byte cap; "
                    "sending transcript-only",
                    f.name,
                    _MAX_LAYER_B_CHARS,
                )
                return None
        envelope = json.loads(jf.read_text(encoding="utf-8"))
        events = lf.read_text(encoding="utf-8")
    except Exception:
        logger.debug("session_transfer: could not read Layer B for sid=%s", sid, exc_info=True)
        return None
    if not isinstance(envelope, dict) or len(events) > _MAX_LAYER_B_CHARS:
        return None
    if not _events_jsonl_is_loadable(events):
        # A crash-truncated source file (kiro-cli killed mid-write) would ship a
        # blob the peer must refuse. Catch it here so the copy degrades to
        # transcript-only without pushing megabytes through the tunnel first.
        # Parse-only -- see below on why nothing is rewritten.
        logger.debug("session_transfer: Layer B for sid=%s is not loadable JSONL", sid)
        return None
    # Shipped BYTE-EXACT, deliberately: no redaction pass over Layer B.
    #
    # This is not an oversight, it is forced. The envelope carries thinking
    # blocks whose ``data.signature`` is a cryptographic signature OVER the
    # thinking content, and the provider validates it when the conversation is
    # replayed. Rewriting any covered byte invalidates it, so the peer's
    # ``session/load`` succeeds and then the very next turn is rejected -- a
    # failure that surfaces far from its cause. Redacting this artifact and
    # transplanting it are mutually exclusive; measured against this machine's own
    # 704 sessions, a leaf-string redaction pass rewrote a signature in 41% of
    # them.
    #
    # What makes byte-exact acceptable is the destination, not the payload: a send
    # goes hub -> the OPERATOR'S OWN peer instance, over a tunnel that operator
    # authenticated, and the peer stores it 0600. Layer B never leaves the user's
    # own trust boundary, and copying their own context between their own machines
    # is the operation they asked for. **Layer A keeps its redaction** -- that
    # text is rendered in a transcript and re-read by an agent as context, so it
    # stays scrubbed on the same boundary.
    return {"sid": sid, "envelope": envelope, "events": events}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rewrite_layer_b_envelope(env: dict[str, Any], new_sid: str, agent: str) -> dict[str, Any]:
    """Rewrite the machine-specific fields of a Layer B ``<sid>.json`` envelope.

    The conversation itself (``session_state.conversation_metadata`` — the
    compaction/turn state that IS the resumable context) is kept verbatim. Only
    the fields that reference the SOURCE host are rewritten, because they would
    otherwise point a resumed session at paths, an id, or an agent that do not
    exist here:

    * ``session_id`` → a FRESH uuid, so copy-never-move holds — a repeat send
      cannot collide with an earlier copy on this host;
    * ``cwd`` and ``permissions.filesystem.allowed_*_paths`` → cleared, matching
      the deliberate decision to drop ``project``; the imported session is
      unscoped until the user re-picks a checkout;
    * ``agent_name`` → the target-resolved agent (or ``None``);
    * timestamps refreshed; ``title`` dropped (it lives on the Layer A slot).
    """
    e = dict(env)
    e["session_id"] = new_sid
    e["cwd"] = ""
    now = _iso_now()
    e["created_at"] = now
    e["updated_at"] = now
    e["title"] = None
    ss = dict(e.get("session_state") or {})
    ss["agent_name"] = agent or None
    perms = dict(ss.get("permissions") or {})
    fs = dict(perms.get("filesystem") or {})
    for k in ("allowed_read_paths", "allowed_write_paths"):
        if k in fs:
            fs[k] = []
    perms["filesystem"] = fs
    ss["permissions"] = perms
    e["session_state"] = ss
    return e


def _write_layer_b_files(layer_b: dict[str, Any], agent: str) -> str | None:
    """Write imported Layer B to disk under a FRESH sid. **Blocking IO, thread-safe.**

    Deliberately does NOT touch the session map. ``SessionMap.set`` mutates a
    shared ``_data`` dict and then serialises the WHOLE file, so calling it from
    a worker thread races the event loop's own map writes: two concurrent
    whole-file writes can interleave and lose an entry, which is the same
    lost-resume-mapping failure this feature exists to avoid. The join is
    therefore performed by the caller ON THE LOOP -- see
    :func:`_join_layer_b`.

    Rewrites the envelope, re-redacts the events (ingress; the sender is not
    trusted), and returns the new sid, or ``None`` on any failure so the caller
    keeps the transcript-only import.
    """
    new_sid = ""
    try:
        new_sid = str(uuid.uuid4())
        # Installed BYTE-EXACT. The envelope is rewritten only where it names the
        # SOURCE HOST (sid / cwd / paths / agent / timestamps) -- never in the
        # conversation payload, whose thinking-block signatures the provider
        # validates on replay. See _read_layer_b for why redacting and
        # transplanting cannot both hold.
        envelope = _rewrite_layer_b_envelope(layer_b.get("envelope") or {}, new_sid, agent)
        events = layer_b.get("events") or ""
        if not _events_jsonl_is_loadable(events):
            # Structural check, not a rewrite: a record the sender shipped does
            # not parse. Installing it would make this side's own
            # ``session/load`` fail later; refuse now so the import lands as the
            # transcript-only copy.
            logger.warning(
                "session_transfer: refusing unparseable Layer B from the peer; "
                "importing transcript-only"
            )
            return None
        d = kiro_sessions_dir()
        # Owner-only, because Layer B is the model's WHOLE context window -- every
        # user turn and tool result in the session -- and default umask 022 would
        # land it at 0644 for any other local user to read.
        #
        # Only the directory WE create is hardened. This is kiro-cli's own
        # sessions dir, so chmod-ing a pre-existing one would mutate posture on a
        # directory this code does not own; the files are 0600 either way, which
        # is what actually contains the content. When we do create it, the chmod
        # is separate from ``mkdir`` because mkdir's mode argument is masked by
        # the umask (``pod/runtime.py`` makes the same two-step call for the same
        # reason).
        created = not d.exists()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if created:
            platform_compat.chmod_safe(d, 0o700)
        for path, text in (
            (d / f"{new_sid}.json", json.dumps(envelope)),
            (d / f"{new_sid}.jsonl", events),
        ):
            # The shared helper, which every atomic-write site in the repo is
            # required to use: it allocates the temp file with ``mkstemp`` so
            # concurrent writers cannot collide on a deterministic ``.tmp`` name
            # (an ENOENT race the previous hand-rolled write here was exposed to),
            # and it retries the Windows rename window.
            #
            # ``restrict_to_owner=True`` applies the owner-only lockdown to the
            # temp file BEFORE any content reaches it — POSIX mode bits are
            # meaningless against NTFS ACLs, and the previous post-rename
            # ``restrict_to_owner`` call left Layer B readable under the
            # inherited DACL for the whole write window (issue #5285). It
            # implies 0o600 on POSIX, and the default
            # ``restrict_on_error="raise"`` keeps this site FAIL CLOSED.
            try:
                atomic_write(path, text, restrict_to_owner=True)
            except OSError:
                # FAIL CLOSED. This is the model's whole context window; leaving it
                # readable by other local accounts on a shared machine is worse
                # than not resuming, and this feature already has an honest
                # fallback for exactly that -- returning ``None`` imports the
                # session transcript-only. Both files go, not just this one: the
                # pair is useless alone and the ``.json`` carries context too
                # (a lockdown failure on the SECOND file leaves the first,
                # already-published one behind otherwise).
                #
                # This overrides the warn-and-continue precedent in
                # ``handlers/weixin_qr.py``. That path has no fallback -- refusing
                # breaks the feature outright -- whereas refusing here costs only
                # resume fidelity, so the same trade resolves the other way.
                #
                # The outer ``except Exception`` below performs the same
                # cleanup as a backstop for non-OSError failures; keep the two
                # paths in sync.
                logger.warning(
                    "session_transfer: could not write owner-only Layer B file %s; "
                    "discarding the pair and importing transcript-only",
                    path.name,
                    exc_info=True,
                )
                _unlink_layer_b_files(new_sid)
                return None
        return new_sid
    except Exception:
        logger.warning(
            "session_transfer: Layer B file materialisation failed; "
            "session imports transcript-only",
            exc_info=True,
        )
        # Remove whatever landed. The pair is written one file at a time, so a
        # failure on the SECOND write (disk full, EIO) leaves the first behind:
        # an orphaned half-pair that no join references, that ``_read_layer_b``
        # will not load because it requires both files, and that nothing else ever
        # cleans up. ``new_sid`` is bound before the try so this path can name it
        # even if the failure happened before the assignment inside.
        _unlink_layer_b_files(new_sid)
        return None


def _join_layer_b(sessions: Any, sm_key: str, sid: str) -> bool:
    """Point the session map at *sid*. **MUST run on the event loop.**

    Two constraints meet here:

    * the join must go through the **LIVE** map (``seed_conversation``), not a
      fresh ``SessionMap()`` -- ``SessionManager`` holds a long-lived map whose
      ``_data`` is loaded once at startup and whose every ``set`` rewrites the
      whole file from that snapshot, so a detached instance's entry is dropped
      by the next unrelated write and the tab degrades to the lossy prefix;
    * and it must run on the loop, because that same whole-file write is
      unsynchronised against concurrent session starts.

    Establishing the entry is also what auto-disables the history-prefix
    fallback, which only fires when no resumable sid exists.
    """
    if sessions is None:
        # No live manager means nothing can resume from the join anyway.
        logger.warning(
            "session_transfer: no live session manager; %s imports transcript-only", sm_key
        )
        return False
    try:
        sessions.seed_conversation(sm_key, sid, provider="acp")
        return True
    except Exception:
        logger.warning(
            "session_transfer: Layer B join failed for %s; session imports transcript-only",
            sm_key,
            exc_info=True,
        )
        return False


async def build_transfer_bundle_async(
    state: DashboardState, slot: _ChatSlot, *, origin: str = ""
) -> dict[str, Any]:
    """Serialise *slot*'s visible conversation into a portable bundle, with the
    disk read off the event loop.

    Carries the FULL conversation rather than only the window currently held in
    memory — a long-running session keeps just its tail resident, and bundling
    ``slot.messages`` alone would silently truncate the transfer to that tail.
    *origin* is a human label for where the session came from (an instance name
    or ``"local"``); it is recorded for provenance and shown on arrival.

    The un-flushed tail is a ``_disk_window_len`` boundary slice, which is valid
    only because the flush below runs first: the save folds a durable injector's
    ``append_if_absent`` copy into the window and advances the boundary, so the
    counter is honest by the time the tail is snapshotted. A caller that bundled
    WITHOUT flushing could not use this slice — a durable injector
    (``cron_inject``, ``workflow_inject``, ``crew_chat``) puts the same row into
    the window and onto disk without a save, so the boundary would start one row
    too early and ship the injection twice. There is deliberately no such
    caller: this is the only builder, and it always flushes.

    The transcript read is synchronous file IO plus JSON parsing over a whole
    session, which is exactly the "large synchronous file IO" the
    ``no-blocking-call-on-event-loop`` rule forbids on the loop: on a long
    session it stalls every other task, and because the liveness heartbeat is
    itself a coroutine a stalled loop cannot pet LoopStallWatchdog, which then
    exits the gateway.

    **Offloading introduces an await, so the snapshot must be checked for
    consistency.** While we are off the loop the periodic 5s flush can run: it
    writes the dirty tail to disk AND advances ``_resumed_count`` / clears
    ``_dirty``. If that lands between our read and our merge, a naive merge reads
    pre-flush disk content and then sees a clean slot — silently dropping the
    tail from the copy.

    Because a completed flush advances ``_disk_window_len`` (the persisted
    boundary) as it writes, an unchanged value across the await is positive proof
    that no flush landed: ``history`` then corresponds exactly to
    ``messages[:_disk_window_len]``, so the tail merge is consistent. On a change
    we retry against the new state. Messages arriving during the await are
    harmless — they extend the tail we are about to copy, they do not move the
    boundary.

    If the retries are exhausted (a flush would have to land inside every one of
    them, which the 5s cadence makes effectively impossible), the transfer
    **fails** with :class:`SnapshotUnstable`. It deliberately does not fall back
    to an inline read: that would trade a lossy transcript for a blocking one,
    and on a large active session the blocking read is what starves the heartbeat
    into a watchdog-triggered gateway exit. Failing costs nothing here — a
    transfer is a copy, so the source is untouched and the user can just retry —
    which makes it strictly better than either losing turns or wedging the
    gateway.
    """
    # slot_history_key, NOT effective_session_key: this addresses a TRANSCRIPT
    # PATH, and for a channel-born slot the dashboard could not bind, the session
    # key resolves to ``dashboard:<stem>`` — a file no read path uses. Bundling
    # from that phantom transcript would ship only the resident window and
    # silently drop every older turn. chat_utils documents the split.
    key = slot_history_key(slot)
    # The session_map is keyed by the SESSION key (what turns run on), which for
    # a channel-bound slot differs from the transcript key above. Resolve it on
    # the loop (pure getattr) and hand it to the thread, so Layer B is read from
    # exactly where the resume path will later look for it.
    sm_key = effective_session_key(slot)
    # Resolve the Layer B sid HERE, on the loop: the lookup self-prunes the
    # session map, so it cannot go into the worker thread below (see
    # _resolve_layer_b_sid). The thread receives only an immutable string.
    #
    # SKIP Layer B entirely while a turn is in flight. Layer A records the user's
    # prompt as soon as it is submitted, but kiro-cli only writes Layer B when the
    # turn persists -- so a mid-turn bundle pairs a transcript that SHOWS the
    # prompt with a context that does not contain it, and the peer's
    # ``session/load`` would resume the model behind its own visible transcript.
    # That skew is specific to carrying Layer B; Layer A alone has no such
    # coupling. Degrading to transcript-only is the honest outcome and is already
    # plumbed end to end -- the import reports ``resume_mode: prefix`` and the
    # sender's row reads "Sent (transcript only)" -- so the user is told, rather
    # than being handed a silently divergent copy or a hard failure on a
    # legitimate action. ``_in_stage_execution`` is included because ``running``
    # reads False between the stages of a staged plan (chat_handlers).
    #
    # Computed INSIDE the retry loop below, never once up front: a retry happens
    # precisely because the slot changed, and a prompt starting during a threaded
    # read is one such change -- so a pre-loop value would let the retry pick up
    # the new prompt in Layer A while still shipping the pre-turn Layer B, which
    # is exactly the skew this check exists to prevent.
    _guard_snapshot(slot)
    # Persist a dirty slot BEFORE snapshotting. The tail slice only sees messages
    # at or past the boundary, so an edit made IN PLACE below it — a variant
    # switch replacing an already-persisted assistant turn — is invisible to it.
    # If that edit's own save failed, disk still holds the previous response and
    # the copy would ship it.
    #
    # Flushing here is safe now in a way it was not originally: the save advances
    # ``_disk_window_len`` itself, so afterwards the tail slice is empty and the
    # bundle comes wholly from disk. (The first version of this code flushed and
    # then sliced on ``_resumed_count``, which the save does NOT touch — that is
    # what duplicated the tail.)
    #
    # best_effort=False: a swallowed failure would put us right back to bundling
    # a stale transcript, so an unpersistable source fails the transfer instead.
    # The source is otherwise untouched — a flush persists what is already in
    # memory, it does not change the conversation.
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        # Flush on EVERY attempt, not once before the loop. A retry happens
        # precisely BECAUSE the slot changed, and that change is unpersisted, so
        # re-reading disk without flushing first would serialize the superseded
        # content — the exact staleness this flush exists to prevent.
        if slot._dirty:
            # The flush is itself an await, so an edit can land inside it: the
            # save writes the snapshot it captured on entry, leaving disk on the
            # EARLIER content while the slot is already newer. Pin the generation
            # across this await and spend an attempt rather than trusting it.
            gen_before_save = slot._dirty_gen
            try:
                saved = await save_slot_off_loop(state, slot, best_effort=False)
            except Exception as exc:
                logger.warning(
                    "session_transfer: could not persist slot=%s before bundling",
                    slot.key,
                    exc_info=True,
                )
                raise SnapshotUnstable("the session could not be persisted before copying") from exc
            if not saved:
                # Delete-won: the session was permanently deleted while the
                # flush awaited the lock. Bundling would ship the destroyed
                # conversation to the peer (or an empty shell of it), so the
                # transfer fails instead of answering success.
                logger.warning(
                    "session_transfer: slot=%s was permanently deleted during "
                    "the pre-bundle flush; refusing the transfer",
                    slot.key,
                )
                raise SnapshotUnstable("the session was permanently deleted")
            if slot._dirty_gen != gen_before_save:
                continue
            _guard_snapshot(slot)
        boundary_before = slot._disk_window_len
        # ``_dirty_gen`` is the primary marker: a monotonic counter the ``_dirty``
        # setter bumps centrally, so ANY mutation that marks the slot dirty moves
        # it — including an edit made IN PLACE, like a variant switch replacing an
        # already-persisted turn. Neither the boundary nor the message count moves
        # for that, so without this the copy could carry a superseded response.
        gen_before = slot._dirty_gen
        # The boundary catches the one mutation gen does NOT: a completed flush
        # advances ``_disk_window_len`` without marking the slot dirty.
        #
        # The count is a backstop for any path that mutates ``slot.messages``
        # without marking dirty. Strictly redundant against a correct dirty-mark,
        # kept because this snapshot has already been wrong twice by assuming a
        # single field told the whole story.
        count_before = len(slot.messages)
        # Direct delete check, independent of the flush arm above: if the
        # periodic 5s flush hit the delete-won guard first, it cleared
        # ``_dirty``, the flush arm here never ran, and the disk read below
        # would assemble a bundle from a permanently deleted session (its
        # in-memory tail plus an empty transcript). The ``saved``-check above
        # only covers a delete observed by THIS builder's own flush.
        if session_was_deleted(state, slot):
            logger.warning(
                "session_transfer: slot=%s belongs to a permanently deleted "
                "session; refusing the transfer",
                slot.key,
            )
            raise SnapshotUnstable("the session was permanently deleted")
        # Snapshot the unpersisted tail (and the slot fields the bundle needs) ON
        # THE LOOP, so the thread below never touches the slot while the loop
        # could be appending to it. Everything past this point is plain data.
        tail = list(slot.messages[boundary_before:])
        title = slot.title if slot._titled else ""
        agent = slot.agent
        # Layer B eligibility is decided HERE, per attempt, on the loop and in the
        # same breath as the tail snapshot -- so the transcript and the context we
        # ship always come from one consistent view of the slot. See the note
        # above for why a pre-loop value goes stale across a retry.
        mid_turn = bool(getattr(slot, "running", False)) or bool(
            getattr(slot, "_in_stage_execution", False)
        )
        if mid_turn:
            layer_b_sid = ""
            logger.info(
                "session_transfer: slot=%s has a turn in flight; sending transcript-only "
                "(Layer B would lag the displayed transcript)",
                slot.key,
            )
        else:
            layer_b_sid = _resolve_layer_b_sid(getattr(state, "sessions", None), sm_key)
        # Read AND assemble off the loop. Assembly redacts every assistant turn,
        # and the transcript can run to the bundle cap, so those regex scans are
        # far too much CPU to hold the loop with — the same starvation that
        # exits the gateway via LoopStallWatchdog.
        bundle = await asyncio.to_thread(
            _read_and_assemble, state, key, tail, title, agent, origin, layer_b_sid, mid_turn
        )
        # Re-check the guards AFTER the await, not only before it. A rewind or a
        # mid-stream flush can land during the threaded read, and the boundary
        # alone does not reveal a rewind: ``_pending_rewrite`` can flip to True
        # while ``_disk_window_len`` stays put, which would otherwise read as
        # "stable" and copy turns the user just discarded.
        _guard_snapshot(slot)
        # The deletion check too: the assembly read above is the longest await
        # in this builder (redaction regexes over the whole transcript), so a
        # permanent delete can complete inside it — after the pre-read probe
        # passed — and the bundle in hand is the destroyed conversation. A
        # delete is permanent, so this is a refusal, not a retry.
        if session_was_deleted(state, slot):
            logger.warning(
                "session_transfer: slot=%s was permanently deleted during "
                "bundle assembly; refusing the transfer",
                slot.key,
            )
            raise SnapshotUnstable("the session was permanently deleted")
        if (
            slot._dirty_gen == gen_before
            and slot._disk_window_len == boundary_before
            and len(slot.messages) == count_before
        ):
            return bundle
        logger.debug(
            "session_transfer: slot %s flushed during the transcript read; retrying",
            slot.key,
        )
    raise SnapshotUnstable(f"transcript snapshot did not settle in {_SNAPSHOT_ATTEMPTS} attempts")


def _guard_snapshot(slot: _ChatSlot) -> None:
    """Refuse to bundle from a slot whose disk view cannot be trusted.

    Called both before and after every awaited read — see the call sites.
    """
    # A rewind/regenerate marks the slot ``_pending_rewrite`` and only clears it
    # once the TRUNCATING rewrite has been written. While it is set, disk still
    # holds the PRE-EDIT transcript and is longer than the resident window, so the
    # boundary slice appends nothing and the bundle would carry turns the user
    # explicitly rewound away.
    if slot._pending_rewrite:
        raise SnapshotUnstable("a pending rewrite means the on-disk transcript is stale")
    # The boundary can also run AHEAD of the resident window, and then the tail
    # slice silently yields nothing. ``_save_slot_to_history`` sets
    # ``_disk_window_len = len(window)`` over the RAW window, streaming ``chunk``
    # rows included; ``_flush_segment`` then reassigns ``slot.messages`` to drop
    # that trailing chunk run and append the finalized assistant message, without
    # adjusting the boundary. (Memory trimming keeps the two in step; this does
    # not.)
    if slot._disk_window_len > len(slot.messages):
        raise SnapshotUnstable(
            "the persisted boundary is ahead of the resident window " "(a flush landed mid-stream)"
        )


def _read_and_assemble(
    state: DashboardState,
    session_key: str,
    tail: list[dict],
    title: str,
    agent: str,
    origin: str,
    layer_b_sid: str = "",
    layer_b_skipped: bool = False,
) -> dict[str, Any]:
    """Read the transcript + Layer B and assemble the bundle. **Runs in a thread.**

    Touches no slot state and no session map — *tail*, *title*, *agent* and
    *layer_b_sid* are all snapshots the caller took on the event loop — so it is
    safe off-loop. Only the file reads happen here.
    """
    history = _read_chained_history(state, session_key)
    history.extend(tail)
    layer_b = _read_layer_b(layer_b_sid)
    if layer_b_sid and layer_b is None:
        # A sid was MAPPED but its files would not read -- pruned, over the size
        # cap, or unparseable JSONL. That is context this session genuinely had
        # and is now giving up, which is the sender's other degradation case: the
        # peer must be told, or the receiving tab shows a full-looking copy with
        # no resumable context behind it. Distinct from ``layer_b_sid == ""``,
        # which means there was never a context to carry.
        layer_b_skipped = True
    return _assemble_bundle(history, title, agent, origin, layer_b, layer_b_skipped)


def _assemble_bundle(
    all_messages: list[dict],
    title: str,
    agent: str,
    origin: str,
    layer_b: dict[str, Any] | None = None,
    layer_b_skipped: bool = False,
) -> dict[str, Any]:
    """Turn a merged transcript into the wire bundle. Pure — thread-safe.

    Kept free of slot access on purpose: the redaction below is regex-heavy over
    up to the whole transcript, so :func:`build_transfer_bundle_async` runs this
    in a thread, and anything touching ``slot`` there would race the event loop.
    """
    messages: list[dict[str, Any]] = []
    section_markers: list[dict[str, Any]] = []
    for m in all_messages:
        role = m.get("role")
        if role == SECTION_MARKER_ROLE:
            # Carried OUT-OF-BAND: ``messages`` keeps exactly the roles a v1/v2
            # importer validates, so an older peer ignores this and does not reject.
            marker_meta = m.get("meta")
            label = marker_meta.get("label", "") if isinstance(marker_meta, dict) else ""
            # The label is a SECOND copy of caller text, so it needs the same egress
            # scrub as the rendered content beside it, not just a type coercion.
            marker_label = label if isinstance(label, str) else ""
            marker_label, _ = redact_exfiltration_urls(marker_label)
            marker_label, _ = redact_credentials(marker_label)
            marker_content, _ = redact_exfiltration_urls(m.get("content", ""))
            marker_content, _ = redact_credentials(marker_content)
            section_markers.append(
                {
                    "at": len(messages),
                    "label": marker_label,
                    "content": marker_content,
                    "ts": m.get("ts", ""),
                }
            )
            continue
        if role not in _VISIBLE_ROLES:
            continue
        content = m.get("content", "")
        # Redact on the way OUT, not only on the way in. This bundle leaves the
        # host, so this is an egress boundary: a transcript written before the
        # redactors existed (or one carried in from a channel) can still hold a
        # raw credential on disk, and relying on the peer to scrub it would send
        # the secret across the boundary first and trust the far side to clean up.
        # The importer redacts again — idempotent, and it must not assume a
        # well-behaved sender.
        #
        # User turns stay verbatim, matching the fork and import paths: redacting
        # what the human typed would corrupt their own words.
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        messages.append({"role": role, "content": content, "ts": m.get("ts", "")})

    # Strip our own marker so a session bounced back and forth does not
    # accumulate one prefix per hop.
    title = title.removeprefix(_IMPORT_TITLE_MARKER)
    # Titles are egress too. A title is generated from user content, and the
    # resume path assigns a client-supplied ``body["title"]`` with no scan of its
    # own, so a resumed title can carry a credential that would otherwise leave
    # the host verbatim. The importer redacts again; this is the boundary.
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "origin": origin,
        "title": title,
        # Hint only — the importer drops it unless the target has this agent.
        "agent": agent,
        "messages": messages,
    }
    # Additive and omitted when empty, so a bundle from a session with no markers
    # is shaped exactly as this code produced before markers existed.
    if section_markers:
        bundle["section_markers"] = section_markers
    # Layer B rides along only when the session has one. Its events were already
    # egress-redacted in :func:`_read_layer_b`; the envelope carries no secret
    # (its paths and title are neutralised on import).
    if layer_b:
        bundle["layer_b"] = {"envelope": layer_b["envelope"], "events": layer_b["events"]}
    elif layer_b_skipped:
        # An EXPLICIT degradation flag, because an absent ``layer_b`` is ambiguous
        # on its own: it means either "this session never had a kiro-cli context"
        # (nothing was lost -- flagging it would cry wolf on every such import) or
        # "the source was mid-turn, so shipping context would have lagged the
        # transcript" (something WAS given up, and the receiving tab should say
        # so). Only the sender can tell those apart, so it says which.
        bundle["layer_b_skipped"] = True
    return bundle


def _reject(reason: str, code: str) -> web.Response:
    """Return a 400 validation failure carrying a machine-readable ``code``.

    Every non-2xx body here needs ``code``: ``test_error_code_contract.py``
    ratchets on it, and a coded body is what lets the sending instance
    distinguish "peer is too old to understand this bundle" from "bundle was
    malformed" without parsing prose.

    The status is a literal 400 rather than a parameter on purpose — the
    contract gate reads the status statically, and a variable one lands in its
    "cannot decide" bucket. The single non-400 rejection (the slot cap) spells
    its own status out at the call site.
    """
    return web.json_response({"error": reason, "code": code}, status=400)


def _validate_bundle(body: Any) -> tuple[dict[str, Any], web.Response | None]:
    """Validate an inbound bundle. Returns ``(bundle, error_response)``."""
    if not isinstance(body, dict):
        return {}, _reject("body must be a JSON object", "transfer_body_not_object")

    version = body.get("bundle_version")
    # Reject an unknown version outright instead of best-effort parsing: see
    # BUNDLE_VERSION.
    if version not in _SUPPORTED_BUNDLE_VERSIONS:
        return {}, _reject(
            f"unsupported bundle_version {version!r} "
            f"(this instance speaks {list(_SUPPORTED_BUNDLE_VERSIONS)})",
            "transfer_version_unsupported",
        )

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        return {}, _reject("messages must be an array", "transfer_messages_not_array")
    if not raw_messages:
        return {}, _reject("bundle carries no messages", "transfer_bundle_empty")
    if len(raw_messages) > _MAX_MESSAGES:
        return {}, _reject(
            f"too many messages ({len(raw_messages)} > {_MAX_MESSAGES})",
            "transfer_too_many_messages",
        )

    total = 0
    messages: list[dict[str, Any]] = []
    for i, m in enumerate(raw_messages):
        if not isinstance(m, dict):
            return {}, _reject(f"message {i} is not an object", "transfer_message_not_object")
        role = m.get("role")
        if role not in _VISIBLE_ROLES:
            return {}, _reject(
                f"message {i} has role {role!r}; expected one of {list(_VISIBLE_ROLES)}",
                "transfer_message_bad_role",
            )
        content = m.get("content", "")
        if not isinstance(content, str):
            return {}, _reject(
                f"message {i} content must be a string", "transfer_message_bad_content"
            )
        if len(content) > _MAX_CONTENT_CHARS:
            return {}, _reject(
                f"message {i} content too long ({len(content)} > {_MAX_CONTENT_CHARS})",
                "transfer_message_too_long",
            )
        total += len(content)
        if total > _MAX_TOTAL_CHARS:
            return {}, _reject(
                f"bundle too large (> {_MAX_TOTAL_CHARS} chars of content)",
                "transfer_bundle_too_large",
            )
        ts = m.get("ts", "")
        messages.append({"role": role, "content": content, "ts": ts if isinstance(ts, str) else ""})

    # ADDITIVE and OPTIONAL: absent from every v1/v2 bundle, so absence is normal.
    # Peer-supplied, so bounds-checked exactly like ``messages`` above.
    raw_markers = body.get("section_markers", [])
    if not isinstance(raw_markers, list):
        return {}, _reject("section_markers must be a list", "transfer_bad_section_markers")
    if len(raw_markers) > _MAX_MESSAGES:
        return {}, _reject(
            f"too many section markers ({len(raw_markers)} > {_MAX_MESSAGES})",
            "transfer_too_many_section_markers",
        )
    section_markers: list[dict[str, Any]] = []
    for i, sm in enumerate(raw_markers):
        if not isinstance(sm, dict):
            return {}, _reject(
                f"section marker {i} is not an object",
                "transfer_section_marker_not_object",
            )
        at = sm.get("at")
        if isinstance(at, bool) or not isinstance(at, int) or not 0 <= at <= len(messages):
            return {}, _reject(
                f"section marker {i} has out-of-range at {sm.get('at')!r}",
                "transfer_section_marker_bad_at",
            )
        label = sm.get("label", "")
        marker_content = sm.get("content", "")
        if not isinstance(label, str) or not isinstance(marker_content, str):
            return {}, _reject(
                f"section marker {i} label and content must be strings",
                "transfer_section_marker_bad_text",
            )
        marker_ts = sm.get("ts", "")
        # A marker CREATED here passes SECTION_MARKER_SCHEMA; an IMPORTED one never goes
        # through creation, so the same schema has to be the gate on this path too.
        try:
            validation.validate_tool_args({"label": label}, validation.SECTION_MARKER_SCHEMA)
        except validation.ValidationError as exc:
            return {}, _reject(
                f"section marker {i} label is not importable: {exc}",
                "transfer_section_marker_bad_label",
            )
        section_markers.append(
            {
                "at": at,
                # NOT truncated: the schema above bounds the length and refuses the
                # line breaks a one-line rule cannot render, so shortening would hide them.
                "label": label,
                "content": marker_content[:_MAX_TITLE_CHARS],
                "ts": marker_ts if isinstance(marker_ts, str) else "",
            }
        )

    title = body.get("title", "")
    if not isinstance(title, str):
        return {}, _reject("title must be a string", "transfer_bad_title")
    origin = body.get("origin", "")
    if not isinstance(origin, str):
        return {}, _reject("origin must be a string", "transfer_bad_origin")
    agent = body.get("agent", "")
    if not isinstance(agent, str):
        return {}, _reject("agent must be a string", "transfer_bad_agent")

    validated: dict[str, Any] = {
        # The sender's explicit "I had context but withheld it" signal, coerced
        # because it arrives from an untrusted peer. Carried through because
        # validation normalises the body, so anything dropped here is invisible
        # downstream -- and this is what tells a degraded import apart from a
        # session that simply never had a kiro-cli context.
        "layer_b_skipped": bool(body.get("layer_b_skipped")),
        "section_markers": section_markers,
        "title": title[:_MAX_TITLE_CHARS],
        "origin": origin[:_MAX_TITLE_CHARS],
        "agent": agent,
        "messages": messages,
    }

    # Layer B is optional: absent on a v1 bundle, or on a session that never had
    # a kiro-cli context. When present it must be well-formed and bounded before
    # anything is written to disk — the same untrusted-input stance as messages.
    layer_b = body.get("layer_b")
    if layer_b is not None:
        if not isinstance(layer_b, dict):
            return {}, _reject("layer_b must be an object", "transfer_layer_b_not_object")
        env = layer_b.get("envelope")
        events = layer_b.get("events")
        if not isinstance(env, dict):
            return {}, _reject(
                "layer_b.envelope must be an object", "transfer_layer_b_bad_envelope"
            )
        if not isinstance(events, str):
            return {}, _reject("layer_b.events must be a string", "transfer_layer_b_bad_events")
        if len(events) > _MAX_LAYER_B_CHARS:
            return {}, _reject(
                f"layer_b too large (> {_MAX_LAYER_B_CHARS} chars)",
                "transfer_layer_b_too_large",
            )
        validated["layer_b"] = {"envelope": env, "events": events}

    return validated, None


def _resolve_agent(name: str) -> str:
    """Return *name* if this instance has an agent by that name, else ``""``.

    An agent template is a local object; carrying a name the target does not
    have would leave the slot pointing at nothing. Resolution failure is not an
    error — the session imports onto the default agent.

    **Blocking**: ``list_agents`` scans the agents directory and parses each
    manifest, so callers on the event loop must offload it (see the call site in
    :func:`api_chat_slot_import`).
    """
    if not name:
        return ""
    try:
        if any(getattr(a, "name", "") == name for a in list_agents()):
            return name
    except Exception:
        # Discovery is best-effort: a broken agents dir must not fail an import.
        logger.debug("session_transfer: agent discovery failed", exc_info=True)
    return ""


def _unlink_layer_b_files(sid: str) -> None:
    """Delete a materialised Layer B pair. **Blocking IO, thread-safe.**

    File-only, for the same reason :func:`_write_layer_b_files` is: the map half
    of the rollback belongs on the event loop.
    """
    if not sid:
        return
    for suffix in (".json", ".jsonl"):
        try:
            (kiro_sessions_dir() / f"{sid}{suffix}").unlink(missing_ok=True)
        except Exception:
            logger.debug("session_transfer: could not remove %s%s", sid, suffix, exc_info=True)


def _forget_layer_b_join(sessions: Any, sm_key: str) -> str:
    """Drop *sm_key*'s join and return the sid it pointed at. **On the loop.**

    Needed because the join is written BEFORE the transcript is persisted (see
    the ordering note in :func:`api_chat_slot_import`): a later failure rolls the
    slot back, so without this the map keeps an entry for a session that has no
    tab. Never raises — it runs on a path already returning a failure.
    """
    if sessions is None:
        return ""
    try:
        return sessions.forget_conversation(sm_key) or ""
    except Exception:
        logger.debug("session_transfer: could not drop the Layer B join", exc_info=True)
        return ""


async def api_chat_slot_import(request: web.Request) -> web.Response:
    """POST /api/chat/slots/import — materialise a transferred session bundle.

    Always creates a NEW slot (copy semantics, see the module docstring). The
    imported slot deliberately has no project directory: the user picks one on
    arrival.
    """
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"

    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )

    try:
        body = await request.json()
    except Exception:
        return _reject("invalid JSON body", "transfer_invalid_json")

    bundle, err = _validate_bundle(body)
    if err is not None:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="dashboard",
            resources="bundle validation",
            error="bundle rejected",
        )
        return err

    messages = bundle["messages"]
    # Agent resolution scans the agents directory and parses each manifest, so it
    # cannot run on the event loop. Only pay the thread hop when a hint was
    # actually sent — the common case is an empty hint, which resolves to "" with
    # no IO at all.
    agent_hint = bundle["agent"]
    resolved_agent = await asyncio.to_thread(_resolve_agent, agent_hint) if agent_hint else ""
    # Re-check the cap HERE, with no await between this test and the creation
    # below. The check at the top of the handler is necessary but not sufficient:
    # body parsing and agent resolution both await, so N concurrent imports near
    # the cap all clear that first test before any of them allocates, and all N
    # are admitted. This second test closes that window because the loop cannot
    # switch tasks between it and ``get_or_create_slot``.
    #
    # Distinct from the construction accounting below: that keeps a RETRACTED slot
    # counted, which is a different window (after creation). Both are needed.
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached (post-await recheck)",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )
    new_slot = state.get_or_create_slot(
        name=None,
        agent=resolved_agent,
        app=request_app,
    )
    # UNPUBLISH the slot for the duration of construction.
    #
    # ``get_or_create_slot`` registers the slot in ``state._slots`` AND calls
    # ``push_slots_update()`` before it returns (state.py), so the tab is visible
    # to every client and reachable by any handler that enumerates ``_slots``
    # (handlers/core.py, handlers/sessions.py) the moment it exists -- while its
    # transcript is still empty, its Layer B unjoined and nothing persisted. A
    # prompt in that window runs against a partial transcript, or cold-starts a
    # FRESH context that the join written afterwards can never attach to: the
    # silent context loss this feature exists to prevent, on its own import path.
    #
    # Retracting it here makes the slot unreachable until everything is in place;
    # it is re-registered and published once, at the end. Collision-safe: the key
    # is minted from ``_slot_counter`` (already advanced), not by scanning
    # ``_slots``, so a concurrent import cannot be handed the same key while this
    # one is parked.
    state._slots.pop(new_slot.key, None)
    # ...but it still occupies memory, so it stays COUNTED. Retracting without
    # this made the slot invisible to the cap check above, and the check happens
    # before creation: with the cap all but reached, concurrent imports each
    # sampled a count that excluded every other import in flight and were all
    # waved through. Released in the ``finally`` below -- a leaked key would
    # inflate the cap for the lifetime of the process, refusing imports forever.
    state.begin_slot_construction(new_slot.key)
    state.push_slots_update()
    # project is left empty on purpose — see the module docstring.
    source_title = bundle["title"] or "Untitled"
    source_title, _ = redact_exfiltration_urls(source_title)
    source_title, _ = redact_credentials(source_title)
    origin = bundle["origin"]
    origin, _ = redact_exfiltration_urls(origin)
    origin, _ = redact_credentials(origin)
    suffix = f" (from {origin})" if origin else ""
    new_slot.title = f"{_IMPORT_TITLE_MARKER}{source_title}{suffix}"
    new_slot._titled = True

    try:
        # Layer B FIRST, before the transcript append loop and the durable save.
        #
        # The slot is registered in ``state._slots`` synchronously by
        # get_or_create_slot above, and handlers enumerate that dict directly
        # (handlers/core.py, handlers/sessions.py), so the imported session is
        # reachable by a plain GET from the moment it is created -- before the
        # awaits below have run. If a prompt lands in that window while the join
        # is still missing, the turn cold-starts via ``session/new`` with a FRESH
        # context, and the Layer B written afterwards is bound to nothing: the
        # exact silent context loss this feature exists to prevent. Landing the
        # join first shrinks the exposed window to a single thread hop and makes
        # any prompt inside it resume correctly.
        #
        # NOTE: this reorder is why the failure paths below call
        # ``_forget_layer_b`` -- a rollback now has to undo a join that already
        # exists, which was not true when materialisation ran last.
        sessions = getattr(state, "sessions", None)
        layer_b = bundle.get("layer_b")
        sm_key = effective_session_key(new_slot)
        # Resume mode, reported back to the sender so a degraded copy is never
        # shown as a full one. "prefix" is correct for a v1/no-Layer-B bundle:
        # the session opens on the transcript, which is exactly what was sent.
        resume_mode = "prefix"
        layer_b_sid = ""
        if layer_b:
            # Files in a thread (blocking IO), join on the loop (the live map's
            # whole-file write is unsynchronised against concurrent session
            # starts). See _write_layer_b_files / _join_layer_b.
            written_sid = await asyncio.to_thread(_write_layer_b_files, layer_b, new_slot.agent)
            layer_b_sid = written_sid or ""
            resumable = bool(layer_b_sid) and _join_layer_b(sessions, sm_key, layer_b_sid)
            if not resumable:
                logger.info(
                    "session_transfer: imported %s without Layer B; "
                    "it will resume via the transcript prefix",
                    new_slot.key,
                )
                if layer_b_sid:
                    # Files landed but the join did not: remove them rather than
                    # leaving an unreferenced pair for a prune to sweep.
                    await asyncio.to_thread(_unlink_layer_b_files, layer_b_sid)
                    layer_b_sid = ""
            resume_mode = "session_load" if resumable else "prefix"

        # Mark the IMPORTED TAB when it arrived without resumable context.
        #
        # The sender's row already reports this, but that row lives in a menu's
        # component state and is gone the moment the menu closes -- while the
        # consequence is discovered later, on this machine, by whoever resumes the
        # work. The tab title is the one surface that persists (it is saved below)
        # and is present exactly where the loss will be felt, so the truth lives
        # there too. English here, like the existing "(from X)" suffix: this is
        # stored transcript metadata written by the backend, not a rendered
        # string.
        #
        # Marked when the sender either SENT context that failed to land here, or
        # told us it deliberately withheld context it had (``layer_b_skipped``,
        # set for a mid-turn source). Keying off ``layer_b`` alone suppressed the
        # marker in that second case -- the sender's row said "transcript only"
        # while the tab that outlives the row said nothing. Neither fires for a
        # bundle that simply never had a kiro-cli context (v1 peer, or a session
        # with none), where a suffix would cry wolf instead of informing.
        if resume_mode == "prefix" and (bundle.get("layer_b") or bundle.get("layer_b_skipped")):
            new_slot.title = f"{new_slot.title} — transcript only"

        since_yield = 0
        # Grouped by insertion point so a marker lands between the same two turns
        # it separated on the source instance.
        markers_at: dict[int, list[dict[str, Any]]] = {}
        for sm in bundle.get("section_markers") or []:
            markers_at.setdefault(sm["at"], []).append(sm)

        def _emit_markers(index: int) -> None:
            for sm in markers_at.get(index, ()):
                # Peer-supplied and never a user turn, so BOTH fields take the
                # ingress scrub the sibling loop below applies to assistant text.
                marker_content, _ = redact_exfiltration_urls(sm["content"])
                marker_content, _ = redact_credentials(marker_content)
                marker_label, _ = redact_exfiltration_urls(sm["label"])
                marker_label, _ = redact_credentials(marker_label)
                new_slot.append(
                    SECTION_MARKER_ROLE,
                    marker_content,
                    "",
                    ts=sm["ts"],
                    meta={"label": marker_label},
                    broadcast=False,
                )

        for position, m in enumerate(messages):
            _emit_markers(position)
            role = m["role"]
            content = m["content"]
            # Assistant content arrives from another instance and lands in a
            # transcript the dashboard renders and an agent later re-reads as
            # context, so it goes through the same redaction the fork path
            # applies. User turns are left verbatim, matching fork: redacting
            # what the human typed would corrupt their own words.
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            cls = "msg msg-u" if role == "user" else "msg msg-a"
            new_slot.append(role, content, cls, ts=m["ts"], broadcast=False)
            # Yield periodically. Redaction is regex-heavy (those regexes hold
            # the GIL) and a bundle carries up to _MAX_TOTAL_CHARS of PEER-
            # supplied content, so redacting it in one un-yielded pass starves
            # the loop heartbeat — and because ``_loop_heartbeat`` pets
            # LoopStallWatchdog *from a coroutine*, a blocked loop cannot pet
            # it: the watchdog's exit_after timer fires and _exit()s the
            # gateway. chat_persistence.restore_open_slots_async hit exactly
            # this on the same read-and-redact work and fixed it the same way.
            # Budgeted by CHARS rather than message count because the cost
            # scales with content size, not with how it is split into turns.
            since_yield += len(content)
            if since_yield >= _YIELD_AFTER_CHARS:
                since_yield = 0
                # sleep(0) yields to the ready queue with no wall-clock delay.
                await asyncio.sleep(0)
        # A marker recorded AT len(messages) trails the last turn, so the loop
        # above never reaches its index.
        _emit_markers(len(messages))
        new_slot.drain()
        # best_effort=False: a swallowed write failure would let us answer 200
        # while the imported session exists only in memory, so the peer believes
        # the transfer landed and a restart before the next flush loses it. An
        # import that cannot be persisted must fail loudly instead.
        try:
            await save_slot_off_loop(state, new_slot, best_effort=False)
        except Exception:
            # Retryable and the peer's own fault-free case, so give it a coded
            # answer rather than a bare 500: the source is untouched, so the user
            # can simply send again.
            # The slot is already unpublished (retracted right after creation),
            # so there is no phantom tab to retract here -- just make sure it
            # cannot be re-registered, and undo the join written above.
            state._slots.pop(new_slot.key, None)
            sid = _forget_layer_b_join(sessions, sm_key) or layer_b_sid
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
            logger.warning(
                "session_transfer: could not persist imported slot=%s; refusing the import",
                new_slot.key,
                exc_info=True,
            )
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_import",
                outcome="error",
                source="dashboard",
                resources=f"to={new_slot.key}",
                error="durable save failed",
            )
            return web.json_response(
                {
                    "error": "could not persist the imported session; please retry",
                    "code": "transfer_import_save_failed",
                },
                status=503,
            )
        new_slot._resumed_count = len(new_slot.messages)
    except asyncio.CancelledError:
        # ``CancelledError`` is a BaseException, so the ``except Exception`` below
        # never saw it: a gateway shutdown or a client disconnect after the join
        # left an orphaned session-map entry plus its files, pointing at a slot
        # that is never published. Roll back the same state, then re-raise so
        # cancellation still propagates.
        #
        # The unlink runs SYNCHRONOUSLY here rather than through
        # ``asyncio.to_thread``: awaiting anything inside a cancelled task is not
        # dependable, and this is two ``unlink`` calls on a teardown path.
        state._slots.pop(new_slot.key, None)
        try:
            _sessions = getattr(state, "sessions", None)
            sid = _forget_layer_b_join(_sessions, effective_session_key(new_slot))
            if sid:
                _unlink_layer_b_files(sid)
        except Exception:
            logger.debug("session_transfer: cancellation rollback failed", exc_info=True)
        raise
    except Exception:
        # The slot was never republished (see the retract above), so nothing is
        # visible to retract -- but the join and its files may exist because
        # Layer B is materialised before the transcript work.
        state._slots.pop(new_slot.key, None)
        try:
            _sessions = getattr(state, "sessions", None)
            sid = _forget_layer_b_join(_sessions, effective_session_key(new_slot))
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
        except Exception:
            logger.debug("session_transfer: join rollback failed", exc_info=True)
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={new_slot.key}",
            error="import finalisation failed",
        )
        raise
    finally:
        # Every exit releases the construction count: the 503 ``return`` above,
        # the ``raise`` here, and the success path. Ordering is safe -- this runs
        # before the re-registration below, but nothing between them awaits, so no
        # other coroutine can observe the slot as neither published nor counted.
        state.end_slot_construction(new_slot.key)

    sel().log_api_access(
        caller=caller,
        operation="chat.slot_import",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"to={new_slot.key},messages={len(messages)},"
            f"origin={origin or 'unknown'},agent={new_slot.agent or 'default'},"
            f"resume={resume_mode}"
        ),
    )
    # Everything is in place now -- transcript persisted, Layer B joined -- so
    # re-register the slot and publish it ONCE. This is the first moment a client
    # can reach the imported session, which is the point: a prompt from here on
    # resumes against real context instead of racing construction.
    state._slots[new_slot.key] = new_slot
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": new_slot.key,
            "title": new_slot.title,
            "messages": len(messages),
            # Resume fidelity, so the SENDER can say "Sent" vs "Sent (transcript
            # only)" instead of showing the same green row either way. Without
            # this the feature's own failure mode -- a silently lossy copy -- is
            # invisible to the person who will walk to the other machine and
            # expect to keep working.
            "resume_mode": resume_mode,
        }
    )
