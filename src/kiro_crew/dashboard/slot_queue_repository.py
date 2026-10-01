"""Queue storage and delivery-ledger operations for dashboard chat slots."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat
from kiro_crew.hooks import safe_content_digest
from kiro_crew.prompt_attachments import named_in_text, probe_safe_path

if TYPE_CHECKING:
    from kiro_crew.subagent import SubagentDelivery

logger = logging.getLogger(__name__)

# This is well above the slot queue's legitimate in-flight set.  Eviction only
# bounds orphaned bookkeeping; an evicted agent remains recoverable on restart.
MAX_PENDING_SUBAGENT_DELIVERIES = 128

#: Cap on a slot's LIVE in-memory queue, shared by every producer that guards
#: an append against a full queue. One named constant because two bare
#: literals bounding the same population drift: the cron origin-injection path
#: (handlers/messaging.py) evicts the OLDEST entry at this cap, while the
#: MCP-App message path (handlers/mcp_apps.py) refuses the NEWEST — different
#: overflow semantics on purpose (a cron notification is periodic and
#: regenerates; an app message is a one-shot user action whose producer can be
#: told 429) — but the SIZE they guard is the same queue.
MAX_LIVE_QUEUE_ENTRIES = 50

#: How many queued user prompts one session's metadata line carries, and how
#: many a restore admits back. Front-first, because the front is what runs
#: first: an over-cap queue keeps the entries closest to delivery.
MAX_DURABLE_QUEUE_ENTRIES = 32

#: Byte budget for the whole serialized ``queued_prompts`` value. The metadata
#: line is ONE json line every reader of the session parses, so an unbounded
#: set of long prompts would make it expensive for every reader rather than
#: only for the queue. Entries are admitted front-first until the budget is
#: spent; a prompt is never truncated, because a shortened prompt replayed as
#: the user's own words is worse than one reported as not carried.
MAX_DURABLE_QUEUE_BYTES = 256_000

#: How many raw entries a restore INSPECTS, as distinct from how many it keeps.
#: A retention cap bounds this gateway's own writes; it does not bound a line
#: that was edited or corrupted outside the gateway into carrying a million
#: entries. The apply phase that calls :func:`sanitize_restored_queue` is
#: loop-affine (``_apply_recent_session``), so a scan proportional to the file's
#: content — even a cheap per-item scan — is startup work the event loop cannot
#: shed, and it blocks chat and heartbeat while it runs. Comfortably above the
#: retention cap so an ordinary line with a few dropped entries still restores
#: everything it should; entries past it are reported as not restored, never
#: silently ignored.
MAX_DURABLE_QUEUE_SCAN = 4 * MAX_DURABLE_QUEUE_ENTRIES

#: Queue-entry keys the durable copy carries. Everything else on an entry is
#: process-local plumbing (retry callbacks, synthetic payloads) or is
#: deliberately excluded — see :func:`durable_queue_entries`.
#:
#: ``_directive_user_origin`` and ``_directive_channel_origin`` are NOT here, and
#: their absence is a security property rather than an omission. They record that
#: an entry's words arrived from an authenticated human, and the drain turns that
#: into real authority: ``chat_runner`` reduces the consumed entries' flags into
#: ``producer_is_user_facing``, which admits user-surface and self-arming
#: directives and exempts them from the LINKED containment constraint. The
#: transcript this value is written to is an ordinary readable-writable file in
#: the crew home, not a write-protected one, so anything restored from it is
#: attacker-supplied for the purposes of that decision. Persisting provenance
#: that a later reader cannot distinguish from a hand-edited line would hand
#: human authority to whoever can write the file, so provenance is not carried
#: across a restart at all and restored entries fail closed to non-directive.
#: Marks an entry this process RESTORED rather than accepted. Process-local by
#: construction -- the writer emits only :data:`_DURABLE_QUEUE_KEYS`, so a
#: hand-edited line cannot clear it and cannot forge it either.
#:
#: It exists because dropping provenance is only half a fail-closed rule. A
#: restored entry carries no actor, and "no actor" resolves to ``user`` in the
#: drain -- which is exactly the arm ``model.route`` admits. So the absence has to
#: be readable as "unknown" rather than as "the person's", and this key is what a
#: consumer asks instead of trying to tell the two apart from the actor alone.
RESTORED_QUEUE_KEY = "_restored_from_disk"

#: Redacting provider paths can make them name nonexistent files; this separate
#: process-local copy keeps images usable while non-user-origin wire lists redact.
PROMPT_IMAGES_ENTRY_KEY = "_prompt_images"

_DURABLE_QUEUE_KEYS: tuple[str, ...] = (
    "id",
    "content",
    "meta",
)


def _is_durable_queue_entry(item: Any) -> bool:
    """True when *item* is a plain user prompt a restart may hand back.

    The queue holds two populations and only one of them survives its process.
    A plain user prompt is the user's own words, waiting for the running turn
    to end: nothing outside the queue holds it, so losing it loses speech.
    Everything else in the queue is a SYSTEM entry whose meaning is bound to
    live state this process is about to lose:

    - an entry carrying ``_on_consumed`` / ``_on_irreversibly_consumed``
      acknowledges the exact automatic payload that failed, and the callback
      does not survive the restart — replaying the text without it
      acknowledges nothing;
    - an entry carrying a ``payload`` is a synthetic recovery continuation
      (``is_synthetic_payload_item``), which dispatches an action a dead turn
      announced;
    - an entry carrying a ``kind`` is an injection whose producer is gone: a
      cron notification, a subagent completion, a plan approval. Its content
      names an event, and a restart is not that event happening again.
    """
    if not isinstance(item, dict):
        return False
    content = item.get("content")
    if not isinstance(content, str) or not content:
        return False
    if item.get("_on_consumed") is not None or item.get("_on_irreversibly_consumed") is not None:
        return False
    if item.get("payload"):
        return False
    if item.get("kind"):
        return False
    return True


def durable_queue_entries(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The json-safe copies of *queue* a metadata writer may persist.

    Copies rather than aliases, so a later in-memory mutation cannot rewrite a
    dict a writer is holding. Except for attachment lists, ``meta`` rides along
    VERBATIM through one json round-trip that also proves it can be serialized:
    it carries the admission-time containment snapshot the drain re-validates
    against, and an entry without one fails closed into the full
    current-constraint set, so dropping it would make a restored prompt
    refusable for a boundary its author was never subject to.

    Attachment lists are redacted because provenance does not survive a restart;
    raw lists would not match the redacted paths on a restored card's edit.

    An entry whose ``meta`` cannot be serialized keeps its content and loses
    only the metadata, because the prompt is the part that cannot be
    reconstructed.

    Pure: an over-cap queue is reported by the SAVE that writes the value, not
    from here (see :func:`count_durable_candidates`). This runs on every flush
    tick through ``queue_persist_pending`` and inside a retried snapshot, so a
    warning here would repeat for as long as the queue stayed over the cap.
    """
    # circular import: chat_delivery imports this module at module level.
    from kiro_crew.dashboard.chat_delivery import attachment_meta

    out: list[dict[str, Any]] = []
    budget = MAX_DURABLE_QUEUE_BYTES
    for item in queue:
        if not _is_durable_queue_entry(item):
            continue
        if len(out) >= MAX_DURABLE_QUEUE_ENTRIES:
            continue
        entry: dict[str, Any] = {}
        for key in _DURABLE_QUEUE_KEYS:
            if key not in item:
                continue
            value = item[key]
            if key == "meta":
                try:
                    value = json.loads(json.dumps(value))
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, dict):
                    continue
                value.update(attachment_meta(value))
            entry[key] = value
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            continue
        cost = len(json.dumps(entry))
        if cost > budget:
            continue
        budget -= cost
        out.append(entry)
    return out


def count_durable_candidates(queue: list[dict[str, Any]]) -> int:
    """How many entries in *queue* are user prompts a restart could hand back.

    Paired with ``len(durable_queue_entries(queue))``, the difference is exactly
    how many accepted prompts the bounds refuse to persist. The send that
    accepted them is NOT rejected for it — refusing a queued send would take the
    user's words away at the one moment they cannot be re-typed from the
    transcript — so the count is what makes the shortfall visible instead of
    silent.

    Both halves must come from ONE read of the queue: see
    :func:`durable_queue_view`.
    """
    return sum(1 for item in queue if _is_durable_queue_entry(item))


def warn_if_not_durable(queue: list[dict[str, Any]], entry_id: str, slot_key: str) -> bool:
    """Report an accepted prompt the durable write will not keep. True when kept.

    The bounds refuse to persist an entry past the count cap or the byte budget,
    and the send is still ACCEPTED: refusing a queued send takes the user's words
    away at the one moment they cannot be re-typed from the transcript. That
    leaves the accept as the only place the difference can be told, and it has to
    be told somewhere — an accepted prompt that a restart drops is the exact
    silence this whole change exists to end, so it must not move from the prompt
    to its acknowledgment and stop there.

    So it is told at WARNING in the gateway log, naming the slot, the entry, how
    many candidates the queue holds and how many the write keeps. Both the
    verdict and the reason are read off the writer's OWN output rather than
    re-derived: an entry absent from a full set was refused by the count cap, and
    absent from a short one by the byte budget. The two ceilings interact — a
    large prompt can be refused at position 3 — so anything that re-implemented
    them here would answer differently from the writer for exactly the entries
    this exists to report.

    Not a receipt field. A caller-visible ``durable`` boolean on the enqueue
    acknowledgments has no reader, so it is not shipped; the on-screen queue-card
    marker belongs with its consumer.
    """
    if not entry_id:
        return False
    snapshot = list(queue)
    kept = durable_queue_entries(snapshot)
    if any(entry.get("id") == entry_id for entry in kept):
        return True
    candidates = count_durable_candidates(snapshot)
    reason = (
        "queue holds the {} durable entries it may".format(MAX_DURABLE_QUEUE_ENTRIES)
        if len(kept) >= MAX_DURABLE_QUEUE_ENTRIES
        else "entry does not fit the {}-byte durable budget".format(MAX_DURABLE_QUEUE_BYTES)
    )
    logger.warning(
        "Slot %s queued prompt %s is accepted but NOT persisted (%s); "
        "%d candidate(s) queued, %d carried, so a restart before it runs drops it",
        slot_key,
        entry_id,
        reason,
        candidates,
        len(kept),
    )
    return False


def durable_queue_view(queue: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The durable entries of *queue* and its candidate count, from ONE read.

    The shortfall the save reports is ``count - len(entries)``, and that
    subtraction is only true of a single observation. Taking the two halves from
    separate reads of a LIVE queue makes an ordinary prompt that merely arrived
    between them look like one the bounds refused: the operator is then told a
    prompt exceeded the durable bounds when it is simply owed to the next save.
    A list copy is what makes the pair one observation.
    """
    snapshot = list(queue)
    return durable_queue_entries(snapshot), count_durable_candidates(snapshot)


def queue_persist_signature(entries: list[dict[str, Any]]) -> str:
    """A stable identity for one durable queue value.

    The periodic flush compares this against what it last wrote, which is what
    makes durability independent of the call site: a queue mutated in place —
    a reorder, a plan-approval filter, a force-stop clear — drifts from the
    persisted signature and is picked up on the next pass, without every such
    site having to remember to mark the slot dirty.
    """
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8", "replace")
    ).hexdigest()


#: The signature of an empty queue, and the value a fresh slot starts at. A
#: slot with nothing queued owes no write: starting it here rather than at ``""``
#: is what keeps the drift check from making every newborn slot save itself once
#: — a save rewrites the transcript, and an unnecessary one invalidates every
#: cache keyed on the file's mtime (the session-intent summary among them). A
#: queue that WAS persisted and has since emptied still drifts, because its
#: stored signature is that of the non-empty value.
EMPTY_QUEUE_SIGNATURE = queue_persist_signature([])


def sanitize_restored_queue(raw: object) -> list[dict[str, Any]]:
    """Validate a persisted ``queued_prompts`` value back into queue entries.

    On-disk metadata is a trust boundary (the file can be edited or corrupted
    outside the gateway), so every field is re-checked instead of trusted, and
    anything the durable writer never emits is dropped rather than carried: a
    restored entry must not arrive wearing a ``kind``, a ``payload``, or a
    callback key, because each of those changes what the drain DOES with it.

    Provenance is dropped for the same reason and matters most:
    ``_directive_user_origin``, ``_directive_channel_origin`` and the entry's
    ``meta`` turn actor are never restored, so a restored entry is non-directive
    and actor-less by construction. The drain
    reduces the consumed entries' flags into the authenticated-human authority a
    directive is admitted under, and this line is an ordinary writable file — a
    carried flag would be authority granted to whoever edited it. The writer does
    not emit them either (:data:`_DURABLE_QUEUE_KEYS`); dropping them here is the
    reader's half of the same rule, so a hand-added flag buys nothing.

    ``meta``'s admission-time containment snapshot goes the same way, and the
    asymmetry there is sharper still. The drain's re-check
    (``newly_held_constraints``) treats a constraint the entry recorded as
    already-held as "not a change", so an ABSENT snapshot fails closed against
    every currently-held constraint while a FORGED all-True one reports nothing
    newly held and fails OPEN — a hand-written entry would drain into a linked or
    mirrored slot and republish to an audience its admission never contemplated.
    Stripping the key restores the fail-closed baseline: the entry is re-checked
    against the constraints that hold NOW, the only set this process can vouch
    for. The rest of ``meta`` rides along, because it carries the sender's own
    plumbing (``sendId``, attachments) that decides nothing about audience.

    Capped at :data:`MAX_DURABLE_QUEUE_ENTRIES` entries and
    :data:`MAX_DURABLE_QUEUE_BYTES` of serialized content, the SAME two ceilings
    the persist path admits, costed against the SAME key projection — because the
    writer's bounds only bound what this gateway wrote, and the line it reads back
    can have been edited or corrupted to carry more. Whatever is admitted here is
    retained in the live slot, re-projected to every websocket client and
    re-serialized by every later save, so an unbounded read would let one
    hand-edited line cost the process memory and work forever, not just once at
    parse time. An entry that does not fit is dropped whole: a truncated prompt
    handed back as the user's own words is worse than one reported as not
    carried.

    A third ceiling, :data:`MAX_DURABLE_QUEUE_SCAN`, bounds how much of the raw
    value is INSPECTED at all: the caller that applies a restored session runs on
    the event loop, so walking every entry of an arbitrarily long list — even to
    reject it — is startup work that blocks chat and the heartbeat.
    """
    if not isinstance(raw, list):
        return []
    # Local import: session_control reaches this module through state, so taking
    # the key at module level would close an import cycle.
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
    from kiro_crew.dashboard.session_control import (
        CHANNEL_RECIPIENT_META_KEY,
        QUEUED_CONTAINMENT_META_KEY,
        SEND_ORIGIN_META_KEY,
    )

    entries: list[dict[str, Any]] = []
    budget = MAX_DURABLE_QUEUE_BYTES
    skipped = 0
    # Bound the READ, not only the retention: see MAX_DURABLE_QUEUE_SCAN. The
    # unscanned tail is counted as skipped rather than dropped quietly, so the
    # warning below states the real number of prompts not handed back.
    scanned = raw[:MAX_DURABLE_QUEUE_SCAN]
    skipped += len(raw) - len(scanned)
    for index, item in enumerate(scanned):
        if len(entries) >= MAX_DURABLE_QUEUE_ENTRIES:
            # Stop, do not keep walking: the remaining items cannot be admitted,
            # and counting them is all that is left to do.
            skipped += len(scanned) - index
            break
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content:
            continue
        entry_id = item.get("id")
        entry: dict[str, Any] = {
            # A missing or invalid id gets a fresh one so the entry stays
            # addressable: every queue mutation the user can reach (promote,
            # edit, delete) is keyed by id.
            "id": entry_id if isinstance(entry_id, str) and entry_id else uuid.uuid4().hex[:12],
            "content": content,
            # Restored entries are plain user prompts by construction; the
            # empty kind is what keeps them out of the system-injection paths.
            "kind": "",
            # PROCESS-LOCAL, and never round-trips: the writer admits only
            # ``_DURABLE_QUEUE_KEYS`` (id, content, meta), so this key cannot be
            # set by editing the line -- which is the whole point of having it.
            # It records that this entry's provenance was established by a
            # previous process and cannot be vouched for by this one, which is
            # what :data:`RESTORED_QUEUE_KEY` is read for.
            RESTORED_QUEUE_KEY: True,
        }
        meta = item.get("meta")
        if isinstance(meta, dict):
            # The admission-time containment snapshot is stripped for the same
            # reason the provenance flags are, and the asymmetry it would
            # otherwise leave is sharper: the drain's re-check treats a
            # constraint the entry recorded as already-held at admission as "not
            # a change", so an ABSENT snapshot fails closed against every
            # currently-held constraint while a FORGED all-True one reports
            # nothing newly held and fails OPEN. A hand-written entry could then
            # drain into a linked or mirrored slot and republish to an audience
            # its admission never contemplated. Dropping the key restores the
            # fail-closed baseline: a restored entry is re-checked against the
            # constraints that hold NOW, which is the only set this process can
            # vouch for. The cost is narrow — an unlinked, unmirrored slot has no
            # boolean constraint held, so the ordinary restore is unchanged.
            # The TURN ACTOR goes with it, and for the plainer version of the
            # same argument. It names WHO authored the entry -- an app, a cron, a
            # sub-agent -- and the drain turns that into what the turn is allowed
            # to do: `model.route` admits only an actor of `user`, so an entry
            # whose stamp a file editor removed drains as the person's and gets an
            # owner-scoped model decision spent on an app's prompt. Restored, the
            # stamp is worth exactly what the file is worth, so it is dropped and
            # the drain re-derives what it can from the entry's `kind`, which the
            # writer never emits either. A restored app entry therefore carries no
            # actor at all -- the same fail-closed baseline the flags above get.
            #
            # The SENDING SLOT goes with them, and it is the sharpest of the
            # first three because the value is not merely read, it names a WRITE
            # TARGET: the drain resolves the recipient of its drop notice from
            # this key alone and appends the entry's own text there
            # (`session_control.notify_send_origin_dropped`). Carried back off
            # the line verbatim, an edited stamp turns a file write into a
            # transcript row in a session the editor does not own, with the
            # entry's content as its body. Nothing in the entry can attest to
            # who sent it, so the key is worth exactly what the file is worth
            # and is dropped. The cost is one notice: a relay that outlives a
            # restart and is then dropped reports to nobody, while the RELAY
            # itself still survives -- which is what putting the stamp in
            # ``meta`` rather than a consumption callback buys, since a
            # callback-carrying entry is not persisted at all.
            #
            # The CHANNEL RECIPIENT stamp goes for the same reason with a wider
            # blast radius: it names a conversation on a network surface and the
            # drop notice would carry the entry's text there, so an edited stamp
            # would turn a file write into an outbound channel message.
            entry["meta"] = {
                k: v
                for k, v in meta.items()
                if k
                not in (
                    QUEUED_CONTAINMENT_META_KEY,
                    TURN_ACTOR_META_KEY,
                    SEND_ORIGIN_META_KEY,
                    CHANNEL_RECIPIENT_META_KEY,
                )
            }
            # The lists come off a writable line and are re-persisted by every
            # later save, so they take the send path's bound; a refused list is
            # dropped, never carried.
            for key in ALL_ATTACHMENT_META_KEYS:
                if key in entry["meta"]:
                    bounded = bounded_attachment_list(key, entry["meta"][key])
                    if bounded is None:
                        del entry["meta"][key]
                    else:
                        entry["meta"][key] = bounded
        try:
            # Costed against the same key projection the WRITER admits
            # (:data:`_DURABLE_QUEUE_KEYS`, which has no ``kind``), not against
            # the entry handed back. Charging the reader for a key the writer
            # never emitted makes the reader's budget the smaller of the two, and
            # a queue persisted just under the ceiling would then drop its tail
            # on the way back in — losing a prompt that WAS durably written,
            # which is the one outcome this whole value exists to prevent.
            cost = len(json.dumps({k: v for k, v in entry.items() if k in _DURABLE_QUEUE_KEYS}))
        except (TypeError, ValueError):
            # ``meta`` came off an untrusted line: a value json cannot re-emit
            # would also break every later save of this slot.
            continue
        if cost > budget:
            skipped += 1
            continue
        budget -= cost
        entries.append(entry)
    if skipped:
        logger.warning(
            "%d persisted queued prompt(s) exceed the durable queue bounds and "
            "are not restored; %d handed back",
            skipped,
            len(entries),
        )
    return entries


def _delivery_key(content: str) -> str:
    """Return a compact identity that survives queue-entry ID replacement."""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:32]


#: Meta keys a queued send's attachment lists ride under, each with the marker
#: word its ``[<marker> N] path`` tokens use. ``files`` is the image-free list
#: ``[attached_file N]`` markers index into, ``dirs`` the folder list
#: ``[attached_dir N]`` markers index into. Defined here, on the queue entry's
#: own module, because every dashboard module that reads them sits downstream.
ATTACHMENT_META_KEYS: tuple[str, ...] = ("files", "dirs")
_ATTACHMENT_MARKERS: dict[str, str] = {"files": "attached_file", "dirs": "attached_dir"}

#: Meta key a send's IMAGE attachments ride under: the ordered list of local
#: image paths the composer uploaded. Kept apart from ``ATTACHMENT_META_KEYS``
#: because it indexes no ``[<marker> N]`` token -- the composer renders an image
#: as a ``![image](path)`` line -- and because it is the STRUCTURED source the
#: turn hands to the provider: the prompt builder emits image blocks from this
#: list alone and never scans the message text for paths, so a send that puts
#: the picture only in its text ships no picture.
IMAGE_ATTACHMENT_META_KEY = "images"

#: One entry per ``images`` entry: the file's identity as the send saw it, read
#: BEFORE any redaction rewrote the path (``file_identity``). A rebuild that
#: only has a redacted spelling recovers the picture from its own directory by
#: this identity, never by the spelling: two credential-shaped names redact to
#: one string, and the string must not let an edit swap in a file the send never
#: attached. An empty entry means the identity could not be read.
IMAGE_IDENTITY_META_KEY = "image_ids"


def attachment_paths(meta: Any) -> list[str]:
    """Every path of *meta*'s attachment lists, in key order; the identity list is not a path list."""
    if not isinstance(meta, dict):
        return []
    return [
        p for key in ALL_ATTACHMENT_META_KEYS for p in (meta.get(key) or []) if isinstance(p, str)
    ]


def file_identity(path: str) -> str:
    """The portable identity of the picture at *path*, or ``""`` when none can be read.

    ``<inode>:<size>:<digest>``: the file's index and size name the entry, the
    content digest names its bytes, so a later file that inherits the inode and
    matches the size is still refused at recovery. The same probe screen the
    reader applies runs first (a Windows host name, a linked ancestor, a leaf
    link, an over-long component), the token read refuses to follow a link, and
    the bytes are read through the sensitive-path gate every dashboard read
    uses, so a protected file is never hashed and no fingerprint of it exists.
    Either part missing records nothing.
    """
    screened = probe_safe_path(path)
    if screened is None:
        return ""
    token = platform_compat.file_identity_token(screened)
    if not token:
        return ""
    digest = safe_content_digest(str(screened))
    if not digest:
        return ""
    return f"{token}:{digest}"


def split_identity(identity: str) -> tuple[str, str] | None:
    """The ``inode:size`` part and the digest of a recorded identity, or None when
    it carries only one of them: recovery needs both to match."""
    stat_part, sep, digest = identity.rpartition(":")
    if not sep or stat_part.count(":") != 1 or not digest:
        return None
    return stat_part, digest


def aligned_image_identities(meta: Any, images: list[str]) -> list[str] | None:
    """The identity list *meta* carries for *images*, or ``None`` when it does
    not line up: a row is a writable file, so a list of the wrong shape is
    dropped rather than trusted position by position."""
    if not isinstance(meta, dict):
        return None
    ids = meta.get(IMAGE_IDENTITY_META_KEY)
    if not isinstance(ids, list) or len(ids) != len(images):
        return None
    if not all(isinstance(i, str) and len(i) <= 64 for i in ids):
        return None
    return list(ids)


#: Every attachment list a send's ``meta`` may carry.
ALL_ATTACHMENT_META_KEYS: tuple[str, ...] = (*ATTACHMENT_META_KEYS, IMAGE_ATTACHMENT_META_KEY)

# Upper bounds on a send's attachment lists (``meta.files`` / ``meta.dirs`` /
# ``meta.images``) as RETAINED anywhere. Every retention site stores the
# normalized result -- the queue entry, the pending-steer map, the persisted
# row meta, the restart marker's opening-row copy, and the ``steer_push`` /
# ``queue_pop`` frames. Redaction can rewrite credential content, but it does
# not limit input size. The gateway caps request bodies at 60 MiB, still far
# above the roughly 1 MiB per
# list these bounds admit, so these per-field bounds are the only size check
# before every retained copy. One named constant per bound, applied through
# :func:`bounded_attachment_list` at the send-path normalizer
# (``chat_delivery.attachment_meta``) AND wherever a list is read back off a
# persisted row or rebuilt from two accepted lists
# (:func:`retained_image_meta`, :func:`with_added_images`): a row is a
# writable file and a union of two accepted lists can exceed one, so a bound
# applied only at the send path is a bound the system's own writers bypass.
# Generous against the composer (20 files per upload batch; a path is a
# filesystem path, PATH_MAX 4096 on Linux) so a legitimate send never trips
# them.
ATTACHMENT_LIST_MAX_ITEMS = 256
ATTACHMENT_PATH_MAX_LEN = 4096


def image_list_refusal_text(count: int) -> str:
    """The reason shown for a refused image list; the count is the one cause a caller can tell apart."""
    if count > ATTACHMENT_LIST_MAX_ITEMS:
        return (
            "Too many pictures attached. A message can carry at most "
            f"{ATTACHMENT_LIST_MAX_ITEMS}. Remove some and try again."
        )
    return "This picture couldn't be attached. Remove it and try again."


def bounded_attachment_list(key: str, raw: Any) -> list[str] | None:
    """*raw* as the attachment list *key* may carry, or ``None`` when refused.

    Accepted: a non-empty list of non-empty strings, at most
    ``ATTACHMENT_LIST_MAX_ITEMS`` long, no entry over ``ATTACHMENT_PATH_MAX_LEN``
    chars. Anything else is refused WHOLE rather than sliced, and the two
    bound refusals say so once in the log: the marker lists are indexed by
    marker number, so a list cut at N leaves the markers past N resolving
    through the renderer's whitespace-bounded fallback (a spaced path
    truncated at its first space), and a path cut in place is a different
    path -- so the refusal takes the same shape a malformed list already
    gets. The image list indexes no marker but takes the same rule, so no two
    stores can disagree about what was admitted. Returns a new list.
    """
    if not isinstance(raw, list) or not raw:
        return None
    if not all(isinstance(p, str) and p for p in raw):
        return None
    if len(raw) > ATTACHMENT_LIST_MAX_ITEMS:
        logger.warning(
            "attachment meta %r refused: %d entries over the %d-entry bound",
            key,
            len(raw),
            ATTACHMENT_LIST_MAX_ITEMS,
        )
        return None
    longest = max(len(p) for p in raw)
    if longest > ATTACHMENT_PATH_MAX_LEN:
        logger.warning(
            "attachment meta %r refused: a %d-char path over the %d-char bound",
            key,
            longest,
            ATTACHMENT_PATH_MAX_LEN,
        )
        return None
    return list(raw)


def _marker_spans(content: str, marker: str, index: int, path: str) -> list[tuple[int, int]]:
    """Every span of the exact ``[<marker> <index>] <path>`` token in *content*.

    The path must end at a whitespace or the end of the text: a bare substring
    test would keep ``/tmp/report.pdf`` alive through ``/tmp/report.pdf.bak``,
    and a bare replace would rewrite the ``[attached_file 2] /tmp/b`` prefix of
    ``[attached_file 2] /tmp/bak`` -- one is a removed attachment drawing a card
    again, the other is a caption silently altered.
    """
    token = f"[{marker} {index}] {path}"
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        at = content.find(token, start)
        if at < 0:
            return spans
        end = at + len(token)
        if end == len(content) or content[end].isspace():
            spans.append((at, end))
        start = at + 1


def _renumber_marker(content: str, marker: str, old: int, new: int, path: str) -> str:
    """Rewrite each exact ``[<marker> <old>] <path>`` token to index *new*."""
    replacement = f"[{marker} {new}] {path}"
    for at, end in reversed(_marker_spans(content, marker, old, path)):
        content = content[:at] + replacement + content[end:]
    return content


_QUOTE_ATTRIBUTION = {
    "user": "— quoting an earlier message from the user",
    "assistant": "— quoting an earlier message from the assistant",
}


def quote_block(quote: Any) -> str | None:
    """The blockquote a ``meta.quote`` record serializes to, or ``None``.

    Mirrors the dashboard's ``quoteBlock`` (``chat-core/composer/messageQuote.ts``)
    byte for byte: the card the client draws strips exactly this block from
    the head of the row, so a record whose block is absent from the text has
    nothing to point at.
    """
    if not isinstance(quote, dict):
        return None
    text, role = quote.get("text"), quote.get("role")
    # ``role`` is type-gated before the dict lookup: a restored queue file is an
    # untrusted boundary, and an unhashable role must refuse the record, not
    # raise out of the edit that reads it.
    if not isinstance(text, str) or not isinstance(role, str) or role not in _QUOTE_ATTRIBUTION:
        return None
    return "\n".join("> " + line for line in [*text.split("\n"), _QUOTE_ATTRIBUTION[role]])


def prune_quote_meta(meta: Any, content: str) -> None:
    """Drop an entry's ``meta.quote`` when an edit took its block out of *content*.

    The drained row's card is drawn from the record and the record's block is
    stripped from the text; a record whose block the edit removed or changed
    would draw a card over text that does not open with it -- a stale card
    beside raw ``>`` lines. The edit keeps whatever the user wrote; only the
    card's claim is withdrawn.
    """
    if not isinstance(meta, dict) or "quote" not in meta:
        return
    block = quote_block(meta.get("quote"))
    if block is None or not content.startswith(block):
        meta.pop("quote", None)


def prune_attachment_meta(
    meta: Any,
    content: str,
    previous: str,
    *,
    display_content: str = "",
    display_previous: str = "",
) -> str:
    """Reconcile an entry's attachment lists with an edited *content*.

    An edit to a queued message can remove a ``[attached_file N] path`` marker;
    the agent then receives text without that path and never gets the file, so
    a list still naming it would make the drained row show a card for an
    attachment that was never delivered. Each list is filtered in place to the
    entries the edit did not remove (order kept; a list left empty is removed),
    and the surviving markers in the text are renumbered to the filtered list's
    positions. The renumbering is what keeps a spaced path lossless: the
    renderer reads ``files[N-1]`` for marker ``N`` and, when the two disagree,
    falls back to a whitespace-bounded capture of the marker text -- which
    would hand back ``/tmp/My`` for ``/tmp/My Report.pdf``.

    "Removed by the edit" means the exact numbered marker was in *previous*
    (the entry's text before this edit) and is not in *content*. An entry the
    previous text never named is out of the edit's reach and is kept as-is: a
    send can carry a list entry with no marker (a caller that stamps
    ``meta.files`` on markerless text; a path the list redacted while the text
    kept it verbatim, so the two spellings differ), and the edit did not take
    that attachment away from the agent -- dropping it would make the drained
    row lose an attachment the user never touched. Returns the content to
    store; it equals *content* whenever nothing was pruned.
    """
    if not isinstance(meta, dict):
        return content
    _prune_image_meta(
        meta,
        content,
        previous,
        display_content=display_content,
        display_previous=display_previous,
    )
    for key in ATTACHMENT_META_KEYS:
        raw = meta.get(key)
        if not isinstance(raw, list):
            continue
        marker = _ATTACHMENT_MARKERS[key]
        indexed = [(i + 1, p) for i, p in enumerate(raw) if isinstance(p, str) and p]
        kept = [
            (old, p)
            for old, p in indexed
            if _marker_spans(content, marker, old, p) or not _marker_spans(previous, marker, old, p)
        ]
        if len(kept) == len(indexed):
            continue
        for new, (old, p) in enumerate(kept, start=1):
            if new != old:
                content = _renumber_marker(content, marker, old, new, p)
        if kept:
            meta[key] = [p for _, p in kept]
        else:
            meta.pop(key, None)
    return content


def _prune_image_meta(
    meta: dict,
    content: str,
    previous: str,
    *,
    display_content: str = "",
    display_previous: str = "",
) -> None:
    """Drop from the image list every picture an edit took out of the text.

    The image list is the structured source of the turn's image blocks, so an
    entry left in it after the user deleted its ``![image](path)`` line would
    hand the model a picture the drained row does not show. "Removed by the
    edit" is decided the way the marker lists decide it: the path was in
    *previous* and is not in *content*, judging the as-typed and display-redacted
    copies together -- in ANY spelling the composer writes
    (``prompt_attachments.named_in_text``: the bare path, its forward-slash
    form, and the escaped or ``<...>``-wrapped destination), so a picture whose
    destination the composer had to escape is pruned like a plain one. A path
    *previous* never spelled out at all is out of the edit's reach and kept,
    and an image line is never renumbered because it carries no index.
    """
    raw = meta.get(IMAGE_ATTACHMENT_META_KEY)
    if not isinstance(raw, list):
        return
    ids = aligned_image_identities(meta, raw)
    kept_positions = [
        i
        for i, p in enumerate(raw)
        if isinstance(p, str)
        and p
        and (
            named_in_text(p, content)
            or named_in_text(p, display_content)
            or not (named_in_text(p, previous) or named_in_text(p, display_previous))
        )
    ]
    if len(kept_positions) == len(raw):
        return
    if kept_positions:
        meta[IMAGE_ATTACHMENT_META_KEY] = [raw[i] for i in kept_positions]
        if ids is not None:
            meta[IMAGE_IDENTITY_META_KEY] = [ids[i] for i in kept_positions]
    else:
        meta.pop(IMAGE_ATTACHMENT_META_KEY, None)
        meta.pop(IMAGE_IDENTITY_META_KEY, None)


def prune_queued_entry(
    item: dict[str, Any],
    content: str,
    previous: str,
    *,
    display_content: str = "",
    display_previous: str = "",
) -> str:
    """Reconcile a queue ENTRY with an edited *content*; return the new text.

    Pruning only the entry's visible lists would leave the provider copy
    carrying an image the user removed. The two lists need to be pruned
    TOGETHER, with each picture
    judged in BOTH spellings: the entry's stored text is raw, while the text
    the client edited can be display-redacted, so
    a picture whose filename the redactor rewrote is named by its raw spelling
    in *previous* and by its redacted spelling in *content*. Judged on the
    redacted list alone, such a picture reads as "never named before" and is
    KEPT after the user removed its line -- and the provider copy, left alone,
    would still hand it to the model. So each position is kept only when
    either spelling names it now, or neither spelling named it before; the
    two parallel lists are cut to the same positions, and an EMPTY provider
    copy stays on the entry, because the drain reads a present list as
    authoritative and falls back to ``meta.images`` only for an entry that never
    carried one (restored from disk). Lengths that disagree mean the copy
    cannot be trusted to line up: it is removed, and the meta prune alone
    decides.
    """
    meta = item.get("meta")
    raw = item.get(PROMPT_IMAGES_ENTRY_KEY)
    shown = meta.get(IMAGE_ATTACHMENT_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return prune_attachment_meta(
            meta,
            content,
            previous,
            display_content=display_content,
            display_previous=display_previous,
        )
    if not isinstance(meta, dict) or not isinstance(shown, list) or len(shown) != len(raw):
        item.pop(PROMPT_IMAGES_ENTRY_KEY, None)
        return prune_attachment_meta(
            meta,
            content,
            previous,
            display_content=display_content,
            display_previous=display_previous,
        )
    ids = aligned_image_identities(meta, shown)
    kept: list[tuple[str, str, str]] = []
    for position, (redacted, raw_path) in enumerate(zip(shown, raw)):
        if not (isinstance(redacted, str) and redacted and isinstance(raw_path, str) and raw_path):
            continue
        named_now = named_in_text(redacted, content) or named_in_text(raw_path, content)
        named_before = named_in_text(redacted, previous) or named_in_text(raw_path, previous)
        if named_now or not named_before:
            kept.append((redacted, raw_path, ids[position] if ids is not None else ""))
    item[PROMPT_IMAGES_ENTRY_KEY] = [raw_path for _, raw_path, _ in kept]
    # The marker lists take the ordinary prune with the image list out of its
    # reach, so the redacted-only rule cannot second-guess the decision above.
    meta.pop(IMAGE_ATTACHMENT_META_KEY, None)
    meta.pop(IMAGE_IDENTITY_META_KEY, None)
    new_content = prune_attachment_meta(meta, content, previous)
    if kept:
        meta[IMAGE_ATTACHMENT_META_KEY] = [redacted for redacted, _, _ in kept]
        if ids is not None:
            meta[IMAGE_IDENTITY_META_KEY] = [identity for _, _, identity in kept]
    return new_content


def retained_image_meta(
    row_meta: Any,
    content: str,
    previous: str,
    *,
    display_content: str = "",
    display_previous: str = "",
) -> dict[str, list[str]]:
    """The image list a re-run of a persisted user row still carries.

    A regenerate, an edit-resend or a rewind re-runs a row's text through the
    runner without the send's ``meta``, and the text alone puts no picture in
    front of the model -- the builder emits image blocks from the structured
    list only. So the re-run reads the list back off the row it re-runs and
    keeps every picture the edit did not remove (the same presence rule
    :func:`prune_attachment_meta` applies to a queued edit). Returns ``{}`` for
    a row that carried no images, and only the image list otherwise: the
    marker lists are the renderer's business and stay with the persisted row.

    The list is re-bounded here (:func:`bounded_attachment_list`), not trusted
    on a type check: the row is a writable file, and the send-path bound is
    the send path's -- a persisted list over it would otherwise be retained
    and forwarded to the provider whole.
    """
    if not isinstance(row_meta, dict):
        return {}
    raw = bounded_attachment_list(
        IMAGE_ATTACHMENT_META_KEY, row_meta.get(IMAGE_ATTACHMENT_META_KEY)
    )
    if raw is None:
        return {}
    meta: dict[str, list[str]] = {IMAGE_ATTACHMENT_META_KEY: raw}
    ids = aligned_image_identities(row_meta, raw)
    if ids is not None:
        meta[IMAGE_IDENTITY_META_KEY] = ids
    _prune_image_meta(
        meta,
        content,
        previous,
        display_content=display_content,
        display_previous=display_previous,
    )
    return meta


def _same_picture(path: str, identity: str, other: str, other_identity: str) -> bool:
    """Whether two list entries name one picture.

    An inode number is unique only within a filesystem, and the token carries
    no device, so equal tokens prove the same file only inside one directory
    (one directory, one filesystem); elsewhere only an equal spelling does.
    """
    if identity and other_identity and _same_directory(path, other):
        return identity == other_identity
    return path == other


def _same_directory(path: str, other: str) -> bool:
    return os.path.normcase(os.path.normpath(os.path.dirname(path))) == os.path.normcase(
        os.path.normpath(os.path.dirname(other))
    )


def with_added_images(
    image_meta: dict[str, list[str]],
    added: list[str],
    *,
    added_identities: Sequence[str] = (),
) -> dict[str, list[str]] | None:
    """Return the bounded union, {} when empty, or None on refusal.

    Separately admitted lists can exceed the bound together; a None result
    must not be committed as a row because it would erase retained pictures.
    The retained pictures keep the identities the row recorded; an added one
    carries the identity the send boundary read off the raw request path
    (*added_identities*, aligned with *added*); nothing is read here, so an
    entry without one is compared by its spelling.
    """
    kept = list(image_meta.get(IMAGE_ATTACHMENT_META_KEY) or [])
    kept_ids = aligned_image_identities(image_meta, kept) or [""] * len(kept)
    given = list(added_identities) if len(added_identities) == len(added) else [""] * len(added)
    new: list[tuple[str, str]] = []
    for path, identity in zip(added, given):
        # Two pictures are the same only by identity; two redacted spellings
        # that collide are two files when their identities differ, so both stay.
        if any(_same_picture(path, identity, p, i) for p, i in [*zip(kept, kept_ids), *new]):
            continue
        new.append((path, identity))
    merged = kept + [p for p, _ in new]
    if not merged:
        return {}
    bounded = bounded_attachment_list(IMAGE_ATTACHMENT_META_KEY, merged)
    if bounded is None:
        return None
    result = {IMAGE_ATTACHMENT_META_KEY: bounded}
    ids = kept_ids + [i for _, i in new]
    if any(ids):
        result[IMAGE_IDENTITY_META_KEY] = ids
    return result


class SlotQueueRepository:
    """Mutate the current facade-owned queue and delivery ledger.

    Every operation receives its owner explicitly.  Replay and cleanup paths
    replace ``_queue`` and ``_subagent_delivery_pending`` wholesale, so keeping
    either container on this repository would split the slot into two states.
    """

    def __init__(
        self,
        *,
        id_provider: Callable[[], str] | None = None,
        timestamp_provider: Callable[[], str] | None = None,
        delivery_key: Callable[[str], str] = _delivery_key,
        max_pending_deliveries: Callable[[], int] | None = None,
    ) -> None:
        self._id_provider = id_provider or (lambda: uuid.uuid4().hex[:12])
        self._timestamp_provider = timestamp_provider or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self._delivery_key = delivery_key
        self._max_pending_deliveries = max_pending_deliveries or (
            lambda: MAX_PENDING_SUBAGENT_DELIVERIES
        )

    def queue_append(
        self,
        owner: Any,
        content: str,
        kind: str = "",
        meta: dict | None = None,
        *,
        directive_user_origin: bool = False,
        directive_channel_origin: bool = False,
        prompt_images: list[str] | None = None,
    ) -> str:
        """Append an entry and return its process-local queue ID."""
        queue_id = self._id_provider()
        item: dict[str, Any] = {
            "id": queue_id,
            "content": content,
            "kind": kind,
        }
        # Append deliberately retains the producer's metadata object: enqueue
        # sites can finish populating structured facts after constructing it.
        if meta:
            item["meta"] = meta
        if prompt_images:
            item[PROMPT_IMAGES_ENTRY_KEY] = list(prompt_images)
        if directive_user_origin:
            item["_directive_user_origin"] = True
        if directive_channel_origin:
            item["_directive_channel_origin"] = True
        owner._queue.append(item)
        owner._note_enqueue()
        return queue_id

    def note_enqueue(self, owner: Any) -> None:
        """Record queue activity beside, rather than inside, an entry."""
        # Queue dicts are compared wholesale on the wire and in persistence
        # tests; placing the clock there would make their shape time-dependent.
        owner._last_enqueue_ts = self._timestamp_provider()

    def queue_insert(
        self,
        owner: Any,
        index: int,
        content: str,
        kind: str = "",
        payload: str = "",
        meta: dict | None = None,
        on_consumed: Callable[[bool], None] | None = None,
        on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
        directive_user_origin: bool = False,
        directive_channel_origin: bool = False,
        prompt_images: list[str] | None = None,
    ) -> str:
        """Insert one entry while preserving retry callbacks and provenance."""
        queue_id = self._id_provider()
        item: dict[str, Any] = {
            "id": queue_id,
            "content": content,
            "kind": kind,
            "payload": payload,
        }
        # Insert is the recovery path: its process-local retry entry owns a
        # snapshot, so later producer mutation must not rewrite queued facts.
        if meta:
            item["meta"] = dict(meta)
        if prompt_images:
            item[PROMPT_IMAGES_ENTRY_KEY] = list(prompt_images)
        if on_consumed is not None:
            item["_on_consumed"] = on_consumed
        if on_irreversibly_consumed is not None:
            item["_on_irreversibly_consumed"] = on_irreversibly_consumed
        if directive_user_origin:
            item["_directive_user_origin"] = True
        if directive_channel_origin:
            item["_directive_channel_origin"] = True
        owner._queue.insert(index, item)
        owner._note_enqueue()
        return queue_id

    def queue_pop(self, owner: Any, index: int = 0) -> dict[str, Any]:
        """Remove and return the exact entry at *index*."""
        return owner._queue.pop(index)

    def note_pending_subagent_delivery(
        self,
        owner: Any,
        content: str,
        deliveries: list[SubagentDelivery],
    ) -> None:
        """Remember which agents a queued completion still owes delivery."""
        if not content or not deliveries:
            return
        key = self._delivery_key(content)
        owed = owner._subagent_delivery_pending.setdefault(key, [])
        known = {delivery.agent_id for delivery in owed}
        for delivery in deliveries:
            if delivery.agent_id not in known:
                owed.append(delivery)
                known.add(delivery.agent_id)
        # Only the consuming row may settle an entry.  A turn tail can dequeue
        # its successor before the current settlement callback runs, so sweeping
        # merely because content left the queue would lose the successor's debt.
        while len(owner._subagent_delivery_pending) > self._max_pending_deliveries():
            owner._subagent_delivery_pending.pop(next(iter(owner._subagent_delivery_pending)))

    def owes_subagent_delivery(self, owner: Any, contents: list[str]) -> bool:
        """Return whether any named completion has unsettled delivery debt."""
        return any(
            self._delivery_key(content) in owner._subagent_delivery_pending for content in contents
        )

    def take_pending_subagent_deliveries(
        self, owner: Any, contents: list[str]
    ) -> list[SubagentDelivery]:
        """Claim delivery marks in consumed-row order and forget only those rows."""
        claimed: list[SubagentDelivery] = []
        for content in contents:
            claimed.extend(owner._subagent_delivery_pending.pop(self._delivery_key(content), []))
        return claimed

    def queue_remove_by_id(self, owner: Any, queue_id: str) -> str | None:
        """Remove the matching entry and return its content."""
        for index, item in enumerate(owner._queue):
            if item["id"] == queue_id:
                del owner._queue[index]
                return item["content"]
        return None

    def queue_edit_by_id(
        self,
        owner: Any,
        queue_id: str,
        content: str,
        *,
        directive_user_origin: bool = False,
        directive_channel_origin: bool = False,
        display_text: Callable[[str], str] | None = None,
    ) -> bool:
        """Edit a user-owned entry without changing its identity or position."""
        for item in owner._queue:
            if item["id"] != queue_id:
                continue
            # Retry callbacks settle the exact automatic payload that failed;
            # moving them to replacement text would acknowledge the wrong work.
            if "_on_consumed" in item or "_on_irreversibly_consumed" in item:
                return False
            # A system-injection entry's kind decides how the drain writes the
            # row (role, provenance meta, mirror suppression) — rewriting only
            # its content would drain the USER'S OWN replacement words as
            # machine-authored: an edited MCP-App entry, for example, would
            # land as an `inject` row labelled with the app, actor `app`, and
            # the linked-thread mirror suppressed, so a human on the mirrored
            # channel never sees what the user typed. Refused, not re-kinded:
            # the entry is not a user prompt to begin with. Scoped to the app
            # kind alone: other system kinds (queued cron text among them)
            # were editable before this endpoint existed, and taking that
            # away is not this feature's call. The frontend pencil gate
            # (`isAppMessageQueued`) withholds exactly this same set.
            from kiro_crew.dashboard.chat_utils import MCP_APP_MESSAGE_KIND

            if item.get("kind") == MCP_APP_MESSAGE_KIND:
                return False
            # The lists index the OLD text's markers; drop only what this edit
            # removed (named before, unnamed now) and renumber the survivors
            # (prune_attachment_meta). An entry the old text never named is
            # not the edit's to drop. The entry's provider copy of the image
            # list is cut in step, or a removed picture would still reach the
            # model through it (prune_queued_entry).
            previous = item.get("content")
            previous = previous if isinstance(previous, str) else ""
            # The quoted block is another message's words: a marker inside it
            # is not one of this entry's attachments, so it is neither counted
            # as removed nor renumbered. The block comes off both texts for the
            # pruning and goes back unchanged -- an edit that kept it keeps the
            # card; one that changed it falls through to `prune_quote_meta`.
            block = quote_block((item.get("meta") or {}).get("quote"))
            if block and content.startswith(block) and previous.startswith(block):
                body_content, body_previous = content[len(block) :], previous[len(block) :]
            else:
                block, body_content, body_previous = "", content, previous
            item["content"] = block + prune_queued_entry(
                item,
                body_content,
                body_previous,
                display_content=display_text(body_content) if display_text else "",
                display_previous=display_text(body_previous) if display_text else "",
            )
            prune_quote_meta(item.get("meta"), item["content"])
            if directive_user_origin:
                item["_directive_user_origin"] = True
            else:
                item.pop("_directive_user_origin", None)
            if directive_channel_origin:
                item["_directive_channel_origin"] = True
            else:
                item.pop("_directive_channel_origin", None)
            return True
        return False

    def queue_promote_by_id(self, owner: Any, queue_id: str) -> bool:
        """Move the exact matching entry to the front without rebuilding it."""
        for index, item in enumerate(owner._queue):
            if item["id"] == queue_id:
                owner._queue.insert(0, owner._queue.pop(index))
                return True
        return False
