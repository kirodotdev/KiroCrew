"""Session transfer — copy a session between Kiro Crew instances.

Two halves live here:

* :func:`build_transfer_bundle` serialises one slot's visible conversation into
  a portable, version-tagged dict. Called on the **sending** side.
* :func:`api_chat_slot_import` accepts such a dict and materialises it as a new
  slot. Called on the **receiving** side.

The wire hop between them is an ordinary authenticated dashboard request over
an Instances tunnel; see [instances.md](../../../docs/system-specs/modules/instances.md) §14.

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
import logging
import platform
from typing import Any

from aiohttp import web

from kiro_crew.agent_discovery import list_agents
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots, slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Bundle schema version. Bump on any incompatible change to the payload shape;
#: the importer refuses a version it does not know rather than guessing, because
#: the two ends of a transfer are independently-updated installs and a silently
#: misread field would land as corrupted conversation.
BUNDLE_VERSION = 1

#: Same cap the fork path uses, for the same reason: bound the number of live
#: slots so a repeated import cannot exhaust the slot table.
_MAX_SLOTS_FOR_IMPORT = 500

#: Per-bundle limits. A bundle arrives from another instance, so it is untrusted
#: input even though the peer is one the owner configured: these bound the work
#: a single request can cause before any of it is written to disk.
_MAX_MESSAGES = 5_000
_MAX_CONTENT_CHARS = 1_000_000
_MAX_TITLE_CHARS = 500
_MAX_TOTAL_CHARS = 20_000_000

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


async def build_transfer_bundle_async(
    state: DashboardState, slot: _ChatSlot, *, origin: str = ""
) -> dict[str, Any]:
    """:func:`build_transfer_bundle` with the disk read off the event loop.

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
                await save_slot_off_loop(state, slot, best_effort=False)
            except Exception as exc:
                logger.warning(
                    "session_transfer: could not persist slot=%s before bundling",
                    slot.key,
                    exc_info=True,
                )
                raise SnapshotUnstable(
                    "the session could not be persisted before copying"
                ) from exc
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
        # Snapshot the unpersisted tail (and the slot fields the bundle needs) ON
        # THE LOOP, so the thread below never touches the slot while the loop
        # could be appending to it. Everything past this point is plain data.
        tail = list(slot.messages[boundary_before:])
        title = slot.title if slot._titled else ""
        agent = slot.agent
        # Read AND assemble off the loop. Assembly redacts every assistant turn,
        # and the transcript can run to the bundle cap, so those regex scans are
        # far too much CPU to hold the loop with — the same starvation that
        # exits the gateway via LoopStallWatchdog.
        bundle = await asyncio.to_thread(
            _read_and_assemble, state, key, tail, title, agent, origin
        )
        # Re-check the guards AFTER the await, not only before it. A rewind or a
        # mid-stream flush can land during the threaded read, and the boundary
        # alone does not reveal a rewind: ``_pending_rewrite`` can flip to True
        # while ``_disk_window_len`` stays put, which would otherwise read as
        # "stable" and copy turns the user just discarded.
        _guard_snapshot(slot)
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
    raise SnapshotUnstable(
        f"transcript snapshot did not settle in {_SNAPSHOT_ATTEMPTS} attempts"
    )


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
            "the persisted boundary is ahead of the resident window "
            "(a flush landed mid-stream)"
        )


def _read_and_assemble(
    state: DashboardState,
    session_key: str,
    tail: list[dict],
    title: str,
    agent: str,
    origin: str,
) -> dict[str, Any]:
    """Read the transcript and assemble the bundle. **Runs in a thread.**

    Touches no slot state — *tail*, *title* and *agent* are snapshots the caller
    took on the event loop — so it is safe off-loop.
    """
    history = _read_chained_history(state, session_key)
    history.extend(tail)
    if not history:
        history = list(tail)
    return _assemble_bundle(history, title, agent, origin)


def build_transfer_bundle(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    origin: str = "",
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """Serialise *slot*'s visible conversation into a portable bundle.

    Carries the FULL conversation rather than only the window currently held in
    memory — a long-running session keeps just its tail resident, and bundling
    ``slot.messages`` alone would silently truncate the transfer to that tail.

    *history* supplies an already-read on-disk transcript so the blocking read
    can happen in a thread; when omitted it is read inline, which is fine for
    tests and any caller not on the event loop.

    *origin* is a human label for where the session came from (an instance name
    or ``"local"``); it is recorded for provenance and shown on arrival.
    """
    all_messages: list[dict] = (
        list(history)
        if history is not None
        else _read_chained_history(state, slot_history_key(slot))
    )
    # Append the resident messages that are not on disk yet, so the bundle carries
    # the tail the user can actually see.
    #
    # The boundary is ``_disk_window_len`` — set by the save path to "how many
    # window messages are now on disk" — NOT ``_resumed_count``, which only
    # records how many messages were loaded when the slot was rehydrated. For a
    # session created in this gateway run ``_resumed_count`` stays 0 no matter how
    # many times it flushes, so using it appended the ENTIRE resident window on
    # top of the disk history and duplicated every persisted turn.
    #
    # No ``_dirty`` gate: the boundary alone is authoritative, and the slice is
    # empty when everything is persisted.
    new_msgs = slot.messages[slot._disk_window_len :]
    if new_msgs:
        all_messages.extend(new_msgs)
    if not all_messages:
        all_messages = list(slot.messages)
    return _assemble_bundle(
        all_messages,
        slot.title if slot._titled else "",
        slot.agent,
        origin,
    )


def _assemble_bundle(
    all_messages: list[dict],
    title: str,
    agent: str,
    origin: str,
) -> dict[str, Any]:
    """Turn a merged transcript into the wire bundle. Pure — thread-safe.

    Kept free of slot access on purpose: the redaction below is regex-heavy over
    up to the whole transcript, so :func:`build_transfer_bundle_async` runs this
    in a thread, and anything touching ``slot`` there would race the event loop.
    """
    messages: list[dict[str, Any]] = []
    for m in all_messages:
        role = m.get("role")
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
    return {
        "bundle_version": BUNDLE_VERSION,
        "origin": origin,
        "title": title,
        # Hint only — the importer drops it unless the target has this agent.
        "agent": agent,
        "messages": messages,
    }


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
    if version != BUNDLE_VERSION:
        return {}, _reject(
            f"unsupported bundle_version {version!r} (this instance speaks {BUNDLE_VERSION})",
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

    title = body.get("title", "")
    if not isinstance(title, str):
        return {}, _reject("title must be a string", "transfer_bad_title")
    origin = body.get("origin", "")
    if not isinstance(origin, str):
        return {}, _reject("origin must be a string", "transfer_bad_origin")
    agent = body.get("agent", "")
    if not isinstance(agent, str):
        return {}, _reject("agent must be a string", "transfer_bad_agent")

    return (
        {
            "title": title[:_MAX_TITLE_CHARS],
            "origin": origin[:_MAX_TITLE_CHARS],
            "agent": agent,
            "messages": messages,
        },
        None,
    )


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


async def api_chat_slot_import(request: web.Request) -> web.Response:
    """POST /api/chat/slots/import — materialise a transferred session bundle.

    Always creates a NEW slot (copy semantics, see the module docstring). The
    imported slot deliberately has no project directory: the user picks one on
    arrival.
    """
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"

    if len(state._slots) >= _MAX_SLOTS_FOR_IMPORT:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={len(state._slots)}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({_MAX_SLOTS_FOR_IMPORT})",
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
    new_slot = state.get_or_create_slot(
        name=None,
        agent=resolved_agent,
        app=request_app,
    )
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
        since_yield = 0
        for m in messages:
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
            # Broadcast the removal: get_or_create_slot already told every
            # client the session exists, so popping it silently leaves a
            # phantom tab that resolves to nothing.
            state._slots.pop(new_slot.key, None)
            state.push_slots_update()
            logger.warning(
                "session_transfer: could not persist imported slot=%s; refusing the import",
                new_slot.key,
                exc_info=True,
            )
            sel().log_api_access(
                caller=caller, operation="chat.slot_import", outcome="error",
                source="dashboard", resources=f"to={new_slot.key}",
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
    except Exception:
        # Broadcast the removal: get_or_create_slot already told every
        # client the session exists, so popping it silently leaves a
        # phantom tab that resolves to nothing.
        state._slots.pop(new_slot.key, None)
        state.push_slots_update()
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={new_slot.key}",
            error="import finalisation failed",
        )
        raise

    sel().log_api_access(
        caller=caller,
        operation="chat.slot_import",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"to={new_slot.key},messages={len(messages)},"
            f"origin={origin or 'unknown'},agent={new_slot.agent or 'default'}"
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": new_slot.key,
            "title": new_slot.title,
            "messages": len(messages),
        }
    )
