"""Durable spool for inbound messages the SHUTDOWN GATE refused.

The loss this closes
--------------------
A channel accepts an inbound message, the platform tells the user it was
delivered, and then ``get_or_create`` refuses the turn because ``close_all()``
has already set ``_closing``. No turn ever opened, so there is nothing to drain
and nothing to retry: the payload is discarded and the user is answered with the
channel's generic fault notice (or, on Slack, with silence).

What this module does about it -- and, deliberately, what it does NOT
---------------------------------------------------------------------
It records the refused message durably and, on the next start, tells the user
in that same conversation that the message was never processed, quoting it back
so a resend is one tap. It does **not** re-drive the message as a turn.

The re-dispatch half was built and then removed. Replaying a spooled entry as
the operator's own turn makes the spool a second, parallel INTAKE path into the
model, and every authorization the live path applies at intake -- the peer
allow-list, Telegram's forum gate, Discord's thread roster, WhatsApp's group
gate, conversation rotation -- has to be re-established on it, per channel, and
kept in step with the live path forever. Ten review rounds re-derived that
surface one gate at a time. A notice is a proactive SEND, and a proactive send
already has exactly one authorization seam in this codebase:
``MessagingTransport.may_send_to``. Scoping replay to the notice puts the whole
feature behind a gate that already exists and is already owned, instead of
introducing a parallel one. Re-dispatch, if wanted, is a separate design owned
by the channel dispatch wiring.

Why the spool is written at the refusal point and nowhere else
--------------------------------------------------------------
Nothing is written on the happy path, so there is no ack protocol to design;
every entry is a turn provably refused before it opened, so a completed turn can
never be re-answered; the happy path costs zero writes; and platform redelivery
is not required because replay reads our own disk (nine of ten channels ack
before the turn runs anyway).

Adoption is opt-in per channel via :attr:`ChannelTurn.inbound_route`. The route
is declared at the channel's dispatch site because ``ChannelTurn.conversation_id``
is a session ATTRIBUTION id, not a reply target. :attr:`InboundRoute.text` is the
message the USER sent -- never the turn's prompt, which on WhatsApp's rules mode
carries the group's private operating rules, and the notice quotes the entry
(display-safe and size-capped, but otherwise as sent). **A route is declared
only where ``may_send_to`` can express
revocation for it**: Discord answers threads from ``_allowed_threads``; WhatsApp's
answers from ``dm_policy`` alone and knows nothing of the group roster, so
WhatsApp spools DMs only; group routes belong to that re-dispatch design.

Bounding, in one primitive
--------------------------
The tree had no store combining a COUNT cap and an AGE horizon:
``jsonl_util.rotate_jsonl_at`` gives cap-on-append, the spec-builder tombstones
give slice-on-write, and the subagent tombstone sweep gives an age sweep. This
module is that combination, because a wedged gateway that crash-loops through
shutdown would otherwise accumulate a replay storm for the next start:

* :data:`SPOOL_MAX_ENTRIES` -- newest-wins count cap, applied on every write.
* :data:`SPOOL_MAX_AGE_SECS` -- an entry older than this is dropped on write and
  again on read. A message nobody answered for a day is not worth answering.
* :data:`TEXT_CAP` -- per-entry payload cap, so one message cannot be the whole
  budget.

Delivery is AT-LEAST-ONCE, one entry at a time
----------------------------------------------
Because the only action is a notice -- idempotent from the user's point of view,
a duplicate costs a repeated line, never a repeated side effect -- the entry is
removed from disk only AFTER the send returns a message id (or the transport
declares the conversation revoked). A crash mid-notice therefore re-notices on
the next start rather than losing the message, which is the opposite of the
tradeoff a re-dispatch would have to make. One entry per pass, so a crash costs
at most one duplicate notice and never the entries queued behind it. An entry
whose channel is not connected THIS run is never noticed: it stays on disk for a
start where the channel is back, and the age horizon bounds it.

Attachments are NOT spooled: an
ingested attachment lives in a turn-owned temp path that is gone after a
restart. The entry records how many were dropped and the notice says so.

Three things this module is trusted with, and why each is enforced rather than
assumed
----------------------------------------------------------------------------
* **The spool is a TRUST BOUNDARY even without re-dispatch.** An entry holds the
  verbatim text of a message the operator sent, and its route decides which
  conversation a proactive send lands in. It lives in its own ``inbound-spool``
  directory under the crew home, fenced from agent file tools
  (``security._CREW_SECRET_LEAVES``) and masked in every agent sandbox
  (``sandbox._CREW_HIDDEN_LEAVES``); a link planted at the directory, the leaf
  or the lock is refused before any read, write or unlink; reads open
  ``O_NOFOLLOW`` and ``fstat`` for a plain single-linked file.
* **The route is re-authorized at replay via ``may_send_to``**, the transport's
  revocation-at-egress decision, with the entry's ``user_id`` as the principal.
  A spooled conversation may have been revoked while the gateway was down, and
  the spool is not a standing grant.
* **The read-modify-write is serialized by a file lock**, and a read failure
  raises rather than reading as empty -- every writer rewrites from what it read
  and the reader unlinks an empty file, so a transient EIO would otherwise erase
  the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import stat as _stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.messaging.renderer import display_safe_for
from kiro_crew.messaging.transport import TransportCapabilities, delivery_confirmed
from kiro_crew.platform.governance_profiles import vet_and_audit
from kiro_crew.platform_compat import file_lock, is_link_or_junction
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The public surface is exactly what the two production callers use: the
#: refusal sites call ``spool_refused_turn`` (with an ``InboundRoute``) and the
#: gateway calls ``replay_spooled``. Everything else -- the record, the report,
#: the store primitives, the error types, the path -- is module-internal,
#: reached by tests through the module rather than advertised as API.
__all__ = [
    "InboundRoute",
    "replay_spooled",
    "spool_refused_turn",
]

#: Newest-wins count cap. Sized for "one shutdown's worth of in-flight
#: messages", not for a backlog: the spool is a rescue buffer, not a queue.
SPOOL_MAX_ENTRIES = 128

#: An entry older than this is dropped unreplayed. A day-old unanswered message
#: replayed into a conversation the user has moved on from is noise, and a
#: bounded horizon is what stops a gateway that cannot stay up from growing the
#: spool forever.
SPOOL_MAX_AGE_SECS = 24 * 60 * 60

#: Per-entry payload cap in characters. One long paste must not consume the
#: whole budget, and the notice quotes the stored text, so truncation is marked
#: in the text rather than done silently.
TEXT_CAP = 16_384

_TRUNCATION_MARK = "\n\n[… truncated when spooled during gateway shutdown]"
_QUOTE_TRUNCATION_MARK = "\n> […] (too long to quote in full — the rest is still yours to resend)"

#: What a user sees when their channel cannot rehydrate its own inbound. Names
#: the real cause (a restart, not a fault) and quotes the message so resending
#: is one tap. Deliberately not the generic "please try again", which is what
#: this issue's investigation found misleading.
RESTART_NOTICE = (
    "⚠️ The gateway was restarting when this arrived, so it was never "
    "processed — nothing is wrong with it. Resend it when you are ready:\n\n{quoted}"
)

_ATTACHMENT_NOTE = "\n\n(Its {count} attachment(s) are not carried over — resend those too.)"


@dataclass(frozen=True)
class InboundRoute:
    """How to reach the conversation a refused message came from.

    A channel declares this at its dispatch site, where it still holds its own
    normalized envelope. It is deliberately NOT derived from the pipeline's
    ``ChannelTurn.conversation_id``: that value is a session-attribution id
    (``"weixin:{user}"``) and is not addressable by ``send_message``, so reading
    it as a reply target would post the restart notice nowhere.

    Every field but ``conversation_id`` is optional, so a channel supplies what
    its platform actually has. A channel that supplies no route at all is not
    spooled and behaves exactly as it did before this module existed -- adoption
    is opt-in per channel rather than a default that silently half-works.
    """

    conversation_id: str

    text: str = ""
    """The message the USER sent, before any turn-side transformation.

    Named separately from ``ChannelTurn.user_text`` because those are not the same
    string, and using the turn's is a disclosure bug: WhatsApp's rules mode
    prepends ``build_silence_contract(verdict.rules)`` -- the group's private
    operating rules -- to the model prompt, so spooling that and quoting it in the
    restart notice would publish those rules into the group. The channels that
    ingest media also inline turn-owned temp paths into the prompt, which are dead
    after a restart.

    REQUIRED for a text-bearing message -- there is no fallback to the turn's
    prompt (an earlier fallback is how a media-only rules-mode message came to
    spool the group's rules). Empty means media-only, or nothing to spool.
    """

    user_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    attachments_dropped: int = 0


@dataclass(frozen=True)
class SpooledInbound:
    """One inbound message the shutdown gate refused, with its routing.

    Every field is plain JSON: the entry survives a process boundary, so it may
    hold no live object (no renderer, no socket, no temp path). Exactly the
    fields the notice needs and nothing recorded "for later": a field nothing
    reads is a field nothing tests, and a re-dispatch design would own
    its own record.
    """

    channel_type: str
    conversation_id: str
    text: str
    user_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    attachments_dropped: int = 0
    spooled_at: float = 0.0

    @property
    def dedupe_key(self) -> str:
        """Identity for collapsing a double-spool of the SAME message, or ``""``.

        Only the platform's own message id can carry this. An empty string means
        "this entry has no identity", and such an entry is NEVER collapsed against
        another -- which is the point rather than a gap.

        A body digest looks like the obvious fallback for a channel with no id and
        is a data-loss bug: two identical messages are ordinary (a repeated
        "status", a resent "?"), and on a channel with no message id they hash the
        same, so one accepted message would be silently discarded and never
        replayed. The hazard the fallback would guard -- the same refusal written
        twice -- does not exist: the refusal is one ``except`` branch that runs
        once per turn. Replay is at-least-once by design (a notice, not a turn),
        so this key is a WRITE-side collapse only and never a replay guard.
        """
        if not self.message_id:
            return ""
        return f"{self.channel_type}:{self.conversation_id}:{self.message_id}"

    @property
    def trace_id(self) -> str:
        """A short label for one entry in a log line or a replay report.

        Distinct from :attr:`dedupe_key`, which is a correctness identity and is
        deliberately empty for an entry the platform gave no id: a log line still
        needs something to name. Two identical bodies can share a trace id, which
        is acceptable for a diagnostic and is exactly why it must never
        decide whether one of them is a duplicate.
        """
        tail = self.message_id or hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:8]
        return f"{self.channel_type}:{self.conversation_id}:{tail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "conversation_id": self.conversation_id,
            "text": self.text,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "attachments_dropped": self.attachments_dropped,
            "spooled_at": self.spooled_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SpooledInbound | None":
        """Rebuild an entry, or return ``None`` for anything unusable.

        Fails closed on a record that cannot address a conversation: replaying
        into an empty ``conversation_id`` would send the notice nowhere, and a
        raise here runs inside the boot path.
        """
        try:
            channel_type = str(raw.get("channel_type") or "")
            conversation_id = str(raw.get("conversation_id") or "")
            text = str(raw.get("text") or "")
            dropped = max(0, int(raw.get("attachments_dropped") or 0))
            # Text OR media: an entry with neither has nothing to notify about.
            if not channel_type or not conversation_id or (not text and not dropped):
                return None
            return cls(
                channel_type=channel_type,
                conversation_id=conversation_id,
                text=text[:TEXT_CAP],
                user_id=str(raw.get("user_id") or ""),
                thread_id=str(raw.get("thread_id") or ""),
                message_id=str(raw.get("message_id") or ""),
                attachments_dropped=dropped,
                spooled_at=float(raw.get("spooled_at") or 0.0),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class ReplayReport:
    """What one replay pass did, so a caller can log a single line."""

    notified: list[str] = field(default_factory=list)
    """Notice confirmed delivered; entry removed."""
    dropped: list[str] = field(default_factory=list)
    """Route no longer authorized; entry removed without a notice."""
    held: list[str] = field(default_factory=list)
    """Left on disk for a later start because the channel was not connected."""
    unconfirmed: list[str] = field(default_factory=list)
    """Notice attempted but not confirmed; entry left on disk for the next start."""

    @property
    def total(self) -> int:
        return len(self.notified) + len(self.dropped) + len(self.held) + len(self.unconfirmed)

    def summary(self) -> str:
        return (
            f"{self.total} spooled inbound message(s): "
            f"{len(self.notified)} answered with a restart notice, "
            f"{len(self.dropped)} dropped (route revoked), "
            f"{len(self.held)} held (channel not connected), "
            f"{len(self.unconfirmed)} unconfirmed (kept for the next start)"
        )


async def spool_refused_turn(*, channel_type: str, route: InboundRoute | None) -> bool:
    """Spool a turn the shutdown gate refused. The one call every channel makes.

    The text spooled is :attr:`InboundRoute.text` and nothing else -- the message
    the USER sent, declared by the channel beside its reply target. There is
    deliberately NO separate text argument and NO fallback to the turn's prompt:
    an earlier revision fell back to ``ChannelTurn.user_text`` whenever the
    route's text was empty, and an empty route text is exactly what a media-only
    rules-mode WhatsApp message produces, so the fallback spooled the group's
    private rules -- the string the split exists to keep out -- and the notice
    quoted them into the group. A channel that declares a route declares its
    text; an empty text with attachments is a media-only entry, and an empty text
    with none is nothing to spool.

    Returns False without touching disk when *route* is ``None`` (the channel has
    not adopted the seam) or when the message carried nothing at all -- no text
    and no attachments. A MEDIA-ONLY message is still recorded: it cannot be
    re-dispatched (the media is gone), but the user still sent something the
    platform marked delivered, and refusing to write it would make an
    uncaptioned screenshot the one shape of message this spool silently drops.
    The entry carries an empty body and a nonzero ``attachments_dropped``, so
    replay routes it straight to the notice, which names the dropped media.
    """
    if route is None:
        return False
    if not route.text.strip() and not route.attachments_dropped:
        return False
    return await record_refusal(
        SpooledInbound(
            channel_type=channel_type,
            conversation_id=route.conversation_id,
            text=route.text,
            user_id=route.user_id,
            thread_id=route.thread_id,
            message_id=route.message_id,
            attachments_dropped=route.attachments_dropped,
        )
    )


def spool_path() -> Path:
    """Where the spool lives. Resolved per call so a HOME override is honoured.

    Its own top-level directory under the crew home rather than a file beside
    other state, because the fences that keep an agent out of it are
    DIRECTORY-scoped: an atomic write renames a sibling temp into place, so
    fencing only the final name would leave a writable path to the same bytes.
    The name is specific for the same reason a fence entry has to be -- a generic
    ``messaging`` directory would later acquire siblings that have no business
    behind a credential-grade fence.
    """
    return data_home() / "inbound-spool" / "refused.jsonl"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


#: Absent on Windows (``getattr`` yields 0, the flag is a no-op). There the
#: ``S_ISREG`` + ``st_nlink`` check on the OPENED descriptor carries the refusal,
#: pinned to the inode actually read rather than to a name that could be swapped
#: after the check. Same shape as ``cron_inflight._read_own_file``.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class SpoolRedirected(OSError):
    """The spool directory or one of its files is a link.

    The spool directory is FENCED from agent writes (``security._CREW_SECRET_LEAVES``,
    ``sandbox._CREW_HIDDEN_LEAVES``), but a fence only holds from the build that
    ships it. A same-UID agent that planted a symlink at ``inbound-spool`` -- or at
    the spool leaf, or the lock -- on a build that predates the fence would have
    every open in this module resolve inside whatever the link points at, which
    the fence never covered, and a JSON record forged there posts a notice on the
    next start into a conversation of the forger's choosing, quoting text the
    user never sent. So every path this module
    touches is refused when it is a link, BEFORE the lock is taken, the file is
    read, written or unlinked. Raised as its own ``OSError`` so the best-effort
    callers treat it exactly like every other refusal: the message degrades to
    the pre-feature loss (on write) or is left for a human (on read), and the
    refusal is logged at WARNING because a link here is never accidental.
    """


def _refuse_links(path: Path) -> None:
    """Refuse when the spool DIRECTORY, the spool LEAF or the LOCK is a link.

    The directory is checked because ``mkdir(exist_ok=True)`` succeeds on a link
    to a directory and every child open then follows it; the leaf and the lock
    because ``os.open``/``unlink``/``os.replace`` resolve the final component
    differently and a link at any of them is a redirect for at least one of
    those. lstat-based, so not race-free against a link planted BETWEEN this
    check and the open -- the reads below close that window with ``O_NOFOLLOW``
    and an ``fstat`` on the opened descriptor, and the writes go through
    ``atomic_write(restrict_to_owner=True)``, whose own linked-parent walk is the
    writers' half of the same rule. What this check removes on its own is the
    PRE-PLANTED shape, which is the one an attacker can set up at leisure.
    """
    for candidate in (path.parent, path, _lock_path(path)):
        try:
            linked = is_link_or_junction(candidate)
        except OSError as exc:
            raise SpoolRedirected(f"cannot inspect {candidate}: {exc}") from exc
        if linked:
            raise SpoolRedirected(f"inbound spool path is a link, refusing: {candidate}")


def _open_own_file(path: Path) -> int | None:
    """Open *path* for reading only if it is a plain, single-linked regular file.

    Returns the descriptor, or ``None`` when the file is absent. Raises
    :class:`SpoolRedirected` for a link, a FIFO, a device or a hard-linked inode --
    each is a way to make this module read bytes it did not write. The type check
    runs on the descriptor that is then read (``fstat``), so there is no window in
    which the name is swapped between check and use.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not _stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        os.close(fd)
        raise SpoolRedirected(f"inbound spool leaf is not a plain single-linked file: {path}")
    return fd


@contextlib.contextmanager
def _spool_lock(path: Path) -> Any:
    """Serialize a whole read-modify-write on the spool.

    Refuses a linked directory, leaf or lock FIRST -- see :func:`_refuse_links`.

    Two inbound messages refused in the same shutdown are two concurrent
    ``asyncio.to_thread`` writers, and the boot pass reads and removes in workers
    of its own. Without this they read the same snapshot and the second atomic
    replace silently drops the first -- reintroducing the loss this module
    exists to close, one layer up.

    A DEDICATED lock file, not the spool itself: the spool is replaced by rename,
    so a lock held on the old inode would not exclude a writer that opened the
    new one. ``file_lock`` fails closed, and this whole module is best-effort, so
    a lock that cannot be taken propagates to the caller's ``except`` and the
    message degrades to the pre-feature drop rather than to a torn file.
    """
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    _refuse_links(path)
    # O_NOFOLLOW on the lock too: a link planted between the check above and
    # this open would otherwise lock (and create) a file somewhere else.
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | _O_NOFOLLOW, 0o600)
    try:
        with file_lock(fd):
            yield
    finally:
        os.close(fd)


def _clamp_text(text: str) -> str:
    if len(text) <= TEXT_CAP:
        return text
    keep = TEXT_CAP - len(_TRUNCATION_MARK)
    return text[: max(0, keep)] + _TRUNCATION_MARK


class SpoolUnreadable(OSError):
    """The spool exists but could not be read.

    Deliberately NOT collapsed into "empty". Both writers rewrite the file from
    what they read, and the reader unlinks a file it read as empty -- so
    reporting a transient read failure (EIO, a permissions blip, a filesystem
    that is remounting) as ``[]`` would have the next write ERASE every queued
    message. A caller that sees this leaves the file untouched.
    """


def _read_entries(path: Path) -> list[SpooledInbound]:
    """Every parseable entry in *path*, oldest first.

    A MISSING file is empty. Any other read failure raises
    :class:`SpoolUnreadable` rather than returning ``[]`` -- see that class for
    why the distinction is load-bearing here when it usually is not.
    """
    entries: list[SpooledInbound] = []
    try:
        fd = _open_own_file(path)
        if fd is None:
            return []
        with os.fdopen(fd, "rb") as handle:
            for line in bounded_records(handle, path, label="inbound spool"):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(raw, dict):
                    continue
                entry = SpooledInbound.from_dict(raw)
                if entry is not None:
                    entries.append(entry)
    except SpoolRedirected:
        raise
    except OSError as exc:
        raise SpoolUnreadable(f"inbound spool unreadable at {path}: {exc}") from exc
    return entries


def _prune(entries: Iterable[SpooledInbound], *, now: float) -> list[SpooledInbound]:
    """Apply the age horizon then the count cap (newest wins), oldest first.

    Both bounds are applied on WRITE and again on READ. Applying them twice is
    deliberate: a spool written by a build with a looser cap, or one that sat on
    disk past the horizon while the gateway was down, must not be replayed just
    because it was legal when it was written.
    """
    fresh = [e for e in entries if now - e.spooled_at <= SPOOL_MAX_AGE_SECS]
    if len(fresh) > SPOOL_MAX_ENTRIES:
        fresh = fresh[-SPOOL_MAX_ENTRIES:]
    return fresh


def _serialize(entries: Iterable[SpooledInbound]) -> str:
    return "".join(json.dumps(e.to_dict(), separators=(",", ":")) + "\n" for e in entries)


def record_refusal_sync(
    entry: SpooledInbound,
    *,
    path: Path | None = None,
    now: float | None = None,
) -> bool:
    """Persist *entry*. Returns True when it is on disk, False on any refusal.

    Read-modify-write of the whole file rather than a bare append, because the
    count cap and the dedupe both need the existing set. That is affordable
    precisely because the file is bounded to :data:`SPOOL_MAX_ENTRIES` lines and
    is only ever touched on the refusal path.

    Never raises. This runs while the gateway is already shutting down, so a
    full disk or a read-only home must degrade to "the message is lost, as it
    was before" rather than becoming the thing that fails shutdown.
    """
    target = spool_path() if path is None else path
    stamp = time.time() if now is None else now
    try:
        record = SpooledInbound(
            channel_type=entry.channel_type,
            conversation_id=entry.conversation_id,
            text=_clamp_text(entry.text),
            user_id=entry.user_id,
            thread_id=entry.thread_id,
            message_id=entry.message_id,
            attachments_dropped=entry.attachments_dropped,
            spooled_at=entry.spooled_at or stamp,
        )
        if not record.channel_type or not record.conversation_id:
            return False
        if not record.text.strip() and not record.attachments_dropped:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        # The lock spans the READ and the replace, not just the write: the cap and
        # the dedupe are both decided from the existing set, so a snapshot taken
        # outside it is stale by the time it is written back and the other
        # writer's message is dropped.
        with _spool_lock(target):
            existing = _read_entries(target)
            key = record.dedupe_key
            # An identity-less entry (no platform message id) is appended rather
            # than matched: collapsing two of those would discard a second
            # genuine message whose text happens to be identical.
            kept = [e for e in existing if not key or e.dedupe_key != key]
            kept.append(record)
            # restrict_to_owner: owner-only mode AND atomic_write's own
            # linked-parent refusal, the writers' half of _refuse_links.
            atomic_write(
                target, _serialize(_prune(kept, now=stamp)), newline="", restrict_to_owner=True
            )
        return True
    except Exception:
        logger.warning(
            "inbound spool: could not record refused %s message", entry.channel_type, exc_info=True
        )
        return False


async def record_refusal(entry: SpooledInbound, *, path: Path | None = None) -> bool:
    """Off-loop :func:`record_refusal_sync`, shielded from the caller's cancel.

    The write takes a file lock and does disk I/O, so it belongs off the loop:
    a replay worker may hold the lock, and blocking the loop on it would stall
    every other task in a shutdown that is already racing a deadline.

    But the handler task that reached the refusal is one ``close_all`` is about
    to cancel, and a bare ``await asyncio.to_thread(...)`` is a cancellation
    point: the cancel would land there, this coroutine would unwind, and the
    write would be an orphan nobody awaits. ``asyncio.shield`` is what keeps
    those two apart -- the caller may be cancelled, the write is not, and this
    coroutine still returns its result when the caller is allowed to finish.
    (The residual is the process's ``os._exit`` landing while the worker is
    mid-write, which no in-process shape can close and which loses at most the
    one message being written.)

    The default executor may already be refusing work at this point
    (``RuntimeError: cannot schedule new futures after shutdown``); dropping the
    message because the pool closed first would reintroduce the loss this module
    exists to close, so that one case falls back to writing inline -- one small
    bounded file, on a loop that is being torn down anyway.
    """
    try:
        return await asyncio.shield(asyncio.to_thread(record_refusal_sync, entry, path=path))
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        return record_refusal_sync(entry, path=path)
    except Exception:
        logger.warning("inbound spool: off-loop record failed", exc_info=True)
        return record_refusal_sync(entry, path=path)


def peek_next(
    *,
    path: Path | None = None,
    now: float | None = None,
    connected: Collection[str] | None = None,
    skip: Collection[str] = (),
) -> tuple[SpooledInbound | None, list[str]]:
    """Return ``(oldest actionable entry or None, held trace ids)`` WITHOUT removing.

    An entry is actionable when its channel is in *connected* (``None`` means
    every channel) and its ``trace_id`` is not in *skip*. An entry whose channel
    is NOT connected is left in place and its trace id is returned in the second
    element, so the caller can report it as held without a second read. Stale
    entries are pruned here rather than returned, so the age horizon is enforced
    on the read as well as on the write -- and an all-stale file is removed so it
    is not re-read on every start.

    The entry stays on disk until :func:`remove_entry` is called for it, which the
    replay driver does only after the notice is confirmed delivered. That is the
    at-least-once half: a crash between the send and the removal re-notices, it
    does not lose.

    Never raises. An unreadable or linked spool yields ``(None, [])`` and is left
    exactly as it was.
    """
    target = spool_path() if path is None else path
    stamp = time.time() if now is None else now
    held: list[str] = []
    try:
        with _spool_lock(target):
            entries = _read_entries(target)
            remaining = _prune(entries, now=stamp)
            if not remaining:
                if target.exists():
                    with contextlib.suppress(OSError):
                        target.unlink()
                return None, held
            if len(remaining) != len(entries):
                # Persist the prune so a stale entry is not re-parsed every pass.
                atomic_write(target, _serialize(remaining), newline="", restrict_to_owner=True)
            for entry in remaining:
                if connected is not None and entry.channel_type not in connected:
                    held.append(entry.trace_id)
                    continue
                if entry.trace_id in skip:
                    continue
                return entry, held
            return None, held
    except SpoolRedirected:
        logger.warning("inbound spool: %s is a link; refusing to read or replay it", target)
        return None, held
    except SpoolUnreadable:
        logger.warning(
            "inbound spool: could not read %s; leaving it for the next start",
            target,
            exc_info=True,
        )
        return None, held
    except OSError:
        logger.warning("inbound spool: could not read %s", target, exc_info=True)
        return None, held


def remove_entry(entry: SpooledInbound, *, path: Path | None = None) -> bool:
    """Remove *entry* from the spool. Called only once its notice is confirmed.

    Matched by identity (``trace_id``), so a second entry with identical text on
    an id-less channel is not removed along with it. The removal is the atomic
    replace on every branch -- never a bare unlink, which on Windows fails
    routinely under an AV scanner's handle and would leave the entry to be
    noticed again. The unlink of an emptied spool is a best-effort tidy-up that
    gates nothing.

    Never raises; ``False`` means the entry is still on disk and will be noticed
    again on the next start, which is the safe direction.
    """
    target = spool_path() if path is None else path
    try:
        with _spool_lock(target):
            existing = _read_entries(target)
            # trace_id is channel+conversation+id (or a body digest), so two
            # identical id-less bodies share one; remove exactly ONE occurrence.
            removed = False
            rest: list[SpooledInbound] = []
            for e in existing:
                if not removed and e.trace_id == entry.trace_id:
                    removed = True
                    continue
                rest.append(e)
            if not removed:
                return False
            atomic_write(target, _serialize(rest), newline="", restrict_to_owner=True)
            if not rest:
                with contextlib.suppress(OSError):
                    target.unlink()
            return True
    except Exception:
        logger.warning(
            "inbound spool: could not remove %s entry", entry.channel_type, exc_info=True
        )
        return False


def _quote(entry: SpooledInbound, transport: Any) -> str:
    """The restart notice for *entry*, made display-safe and sized for *transport*.

    The quoted body is the user's own text, but it still passes through the
    channel-neutral defang: a message injected into a group conversation can
    carry a broadcast mention, and echoing it back unescaped would fire it. The
    defang runs on the QUOTE before it is sized, because it inserts characters.

    Sized to ``capabilities.max_message_chars``, because the notice PREFIXES the
    quote: a message that fit the platform's cap on the way in overflows the cap
    with the prefix and the ``> `` markers added, and a transport's ``send_message``
    that slices to its cap and returns an id would confirm a notice whose tail
    was silently cut. The quote is truncated here, VISIBLY, before the send, so
    what the user sees says so. It is not chunked into several messages: the
    quote is an echo of text the sender still holds and is told to resend, so a
    marked truncation is an honest notice while a multi-part send with per-chunk
    confirmation would be a delivery protocol for an echo.
    """
    body = entry.text
    if entry.attachments_dropped:
        body += _ATTACHMENT_NOTE.format(count=entry.attachments_dropped)
    quoted = "\n".join(f"> {line}" for line in body.splitlines() or [""])
    capabilities = getattr(transport, "capabilities", None)
    if capabilities is not None:
        try:
            quoted = display_safe_for(quoted, capabilities)
        except Exception:
            pass
    cap = int(getattr(capabilities, "max_message_chars", 0) or 0)
    if cap > 0:
        room = cap - len(RESTART_NOTICE.format(quoted=""))
        if len(quoted) > room:
            keep = max(0, room - len(_QUOTE_TRUNCATION_MARK))
            quoted = quoted[:keep].rstrip() + _QUOTE_TRUNCATION_MARK
    return RESTART_NOTICE.format(quoted=quoted)


def _audit_route(entry: SpooledInbound, outcome: str) -> None:
    """SEL record for the ``may_send_to`` decision on *entry*, grant or denial.

    Both outcomes, as the cross-surface proactive send records them: a denial
    also DELETES a stored user message, and a grant is what puts the gateway's
    own text into a conversation on the boot path. Best-effort, so a SEL failure
    cannot turn either into a raise where an exception costs the gateway its
    start.
    """
    try:
        sel().log_api_access(
            caller=entry.user_id or "unknown",
            operation="channel.proactive_send_authorize",
            outcome=outcome,
            source=entry.channel_type,
            resources=f"inbound-spool -> {entry.channel_type}:{entry.conversation_id}",
        )
    except Exception:
        logger.debug("SEL logging failed for inbound-spool authz %s", outcome, exc_info=True)


def _route_authorized(entry: SpooledInbound, transport: Any) -> bool:
    """Whether the notice may still be posted to this conversation.

    A spooled entry is not a standing grant: the peer may have left the roster,
    a Discord thread may have been revoked, all while the gateway was down.
    ``may_send_to`` is the transport's own revocation-at-egress decision and the
    notice is a proactive send, which is exactly what it governs. Fails CLOSED on
    a transport that cannot answer or that raises.

    The principal is passed for a DM route ONLY. A THREADED route (a Discord
    thread, a Telegram forum Topic) is authorized by the thread roster and
    nothing else: Discord's ``may_send_to`` falls from a thread not in
    ``_allowed_threads`` to its DM arm, ``principal in _allowed``, on the stated
    assumption that a thread route names no principal. A spooled thread entry
    DOES name one -- the sender -- so passing it would let a still-allowed sender
    authorize a notice into a thread that was revoked while the gateway was
    down. The sender being on the DM allow-list says nothing about whether the
    thread may be posted to; withholding the principal keeps the two rosters
    from answering for each other.
    """
    gate = getattr(transport, "may_send_to", None)
    if gate is None:
        return False
    thread = entry.thread_id or None
    principal = "" if thread else entry.user_id
    try:
        permitted = bool(gate(entry.conversation_id, thread, principal=principal))
    except Exception:
        logger.warning(
            "inbound spool: %s may_send_to raised; treating the route as revoked",
            entry.channel_type,
            exc_info=True,
        )
        permitted = False
    _audit_route(entry, "allowed" if permitted else "denied")
    return permitted


def _channel_permitted_sync(channel_type: str) -> bool:
    """The ``channels`` governance ceiling, audited, for one notice send.

    The notice is a proactive send on a network surface, and every other
    proactive-send site in the tree (``channel.send_message`` on the dashboard,
    the cron fallback legs) asks this same seam before sending. A policy that
    denies the channel is an ordinary operational event -- an operator can tighten
    it while the gateway is down -- and the transport being CONNECTED says nothing
    about whether it may be written to. Fail-closed: a degraded evaluation denies.
    Same seam as :func:`kiro_crew.dashboard.handlers.messaging._vet_channel_send`,
    so the audit record has the same shape; the ``tool_name`` names this caller.
    """
    try:
        decision = vet_and_audit(
            "channels",
            channel_type,
            session_key=f"inbound-spool:{channel_type}",
            tool_name="inbound_spool.notice",
            fail_closed=True,
        )
        return bool(getattr(decision, "permitted", False))
    except Exception:
        logger.warning("inbound spool: channel governance check failed", exc_info=True)
        return False


async def _notify_one(entry: SpooledInbound, transport: Any) -> str:
    """Send the notice. Returns ``"notified"`` or ``"dropped"``; raises on unconfirmed.

    Raising is how "keep the entry" is expressed: the caller removes an entry
    from disk only when this returns. A ``dropped`` return also removes it -- the
    route is revoked, and there is nothing more this module may do with it.
    """
    if not _route_authorized(entry, transport):
        logger.info(
            "inbound spool: dropping %s message — the conversation is no longer an "
            "authorized destination",
            entry.channel_type,
        )
        return "dropped"
    send = getattr(transport, "send_message", None)
    if send is None:
        return "dropped"
    message_id = await send(
        entry.conversation_id, _quote(entry, transport), entry.thread_id or None
    )
    # The shared predicate, not a local re-spelling: it is what knows that WeCom and
    # Feishu return "" on SUCCESS (``returns_message_id=False``), and a local copy
    # that forgot that would re-notice those two forever.
    capabilities = getattr(transport, "capabilities", None) or TransportCapabilities()
    if not delivery_confirmed(capabilities, str(message_id or "")):
        raise RuntimeError("notice send returned no message id")
    return "notified"


async def replay_spooled(
    *,
    transports: Mapping[str, Any],
    path: Path | None = None,
    now: float | None = None,
) -> ReplayReport:
    """Notify every spooled entry's conversation that its message was not processed.

    *transports* is the live ``channel_type -> MessagingTransport`` map the host
    already keeps (``DashboardState.channel_transports``). An entry whose channel
    is not connected THIS run is left on disk untouched -- a channel can be absent
    because its startup transiently failed -- and the age horizon bounds it.

    AT-LEAST-ONCE: an entry is removed only after its notice is confirmed
    delivered or its route is found revoked. An unconfirmed send (a raise, an
    empty message id) leaves the entry in place for the next start, and the loop
    moves on so one bad route cannot block the rest. A crash between the send and
    the removal re-notices on the next start; a duplicate notice is a repeated
    line, never a repeated side effect, which is why this direction is safe here
    and would not have been for a re-dispatch.

    Once per entry per pass. An entry the pass attempted and LEFT ON DISK -- an
    unconfirmed send, or a notice that landed but whose removal failed -- is never
    returned to it again, so a spool that has become unwritable costs one notice
    per entry and not one per loop iteration; the next start tries again. An
    entry that was removed is not remembered: two identical id-less bodies share
    a ``trace_id``, and forgetting the removed one is what lets its twin be
    noticed in the same pass rather than the next.

    Never raises -- this runs on the boot path, where an exception would cost the
    gateway its start.
    """
    report = ReplayReport()
    connected = set(transports)
    seen: set[str] = set()

    for _ in range(SPOOL_MAX_ENTRIES):
        entry, held = await asyncio.to_thread(
            peek_next, path=path, now=now, connected=connected, skip=seen
        )
        for trace_id in held:
            if trace_id not in seen:
                seen.add(trace_id)
                report.held.append(trace_id)
                logger.info(
                    "inbound spool: leaving %s message on disk — channel is not connected",
                    trace_id.split(":", 1)[0],
                )
        if entry is None:
            break
        # The governance ceiling, per entry rather than once per channel: the
        # decision is cheap, the pass is bounded, and asking per send is the
        # shape every other proactive-send site uses. A denied channel is HELD,
        # not dropped: the route is not revoked, the channel is governed off, and
        # the operator may loosen the policy before the age horizon expires it.
        if not await asyncio.to_thread(_channel_permitted_sync, entry.channel_type):
            if entry.trace_id not in seen:
                logger.info(
                    "inbound spool: leaving %s message on disk — channel is denied by the "
                    "active governance profile",
                    entry.channel_type,
                )
                report.held.append(entry.trace_id)
            seen.add(entry.trace_id)
            continue
        transport = transports[entry.channel_type]
        try:
            outcome = await _notify_one(entry, transport)
        except Exception:
            logger.warning(
                "inbound spool: notice for %s not confirmed; keeping the entry for the next start",
                entry.channel_type,
                exc_info=True,
            )
            seen.add(entry.trace_id)
            report.unconfirmed.append(entry.trace_id)
            continue
        if not await asyncio.to_thread(remove_entry, entry, path=path):
            # The notice landed but the entry could not be taken off disk. Marked
            # seen so this pass will not notice it again; the next start will
            # (one duplicate line), which is the at-least-once contract.
            seen.add(entry.trace_id)
            logger.warning(
                "inbound spool: notice for %s sent but the entry could not be removed; "
                "it will be noticed again on the next start",
                entry.channel_type,
            )
            report.unconfirmed.append(entry.trace_id)
            continue
        getattr(report, outcome).append(entry.trace_id)
    if report.total:
        logger.info("inbound spool: replayed %s", report.summary())
    return report
