"""A slot's sidebar placement: folder, pin, tags, colour and theme consent.

Owns the process-wide tags revision (its epoch file and counter are rebound only
here) that every slot stamps at birth and on each tag edit.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_artifact``, ``_channel_folder_filed``, ``_folder_changed``,
``_folder_suggested``, ``color_hex``, ``color_index``, ``color_theme``, ``folder_id``,
``muted``, ``mutes_opened``, ``pinned``, ``tags``, ``tags_revision``, ``theme_consent``,
``theme_consent_sha``.

A function whose first parameter is ``self`` is a ``_ChatSlot`` method: the
class binds it under the same name (``state_parts/chat_slot.py``), so it reads
and writes the slot's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.loader import config_dir

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.state")


# Tag revisions are totally ordered across gateway restarts. Each process claims
# an EPOCH once at startup (``ensure_tags_revision_epoch``, run off the event
# loop from ``DashboardState.load_tags``): ``max(persisted counter + 1, current
# clock in microseconds)``, persisted atomically to the data home. Paired with a
# strictly increasing in-process sequence, a revision minted by a later process
# always sorts after every revision of an earlier one, so a slow reply from the
# pre-restart process can never masquerade as newer. The persisted counter keeps
# the order monotonic across a backward clock step; the clock floor keeps a
# writable restart above everything minted before it. A process whose claim
# cannot be persisted mints OPAQUE revisions instead: an ordering that was never
# made durable is never asserted, and clients fall back to equality + lineage.
_TAGS_REVISION_EPOCH_FILE = "tags_revision_epoch"


_TAGS_REVISION_EPOCH: int | None = None


_TAGS_REVISION_SEQ_LOCK = threading.Lock()


_TAGS_REVISION_SEQ = 0


def _claim_tags_revision_epoch() -> int | None:
    """Claim, persist and return this process's epoch, or None if it could not
    be persisted.

    The claim is ``max(previous + 1, now_microseconds)``: never below the
    persisted counter (so a backward clock step cannot regress the order) and
    never below the current clock (so a writable restart sorts above anything
    minted before it). If the claim cannot be persisted, no epoch is returned
    and ``mint_tags_revision`` falls back to OPAQUE revisions: an ordering
    that was never made durable must not be asserted, because the next restart
    cannot know about it and a backward clock step could then produce a lower
    orderable epoch that clients would reject. Opaque revisions keep the
    equality/lineage behaviour that fixes the reported flicker; only the
    cross-restart ordering refinement is given up, on a home that cannot
    persist anything anyway.
    """
    path = config_dir() / _TAGS_REVISION_EPOCH_FILE
    previous = 0
    try:
        previous = int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # An unreadable or malformed counter is NOT "no counter": the real value
        # may be higher than anything the clock would now yield, so re-seeding
        # from the clock could persist a LOWER epoch than clients already hold.
        # Refuse to assert an order this process cannot prove.
        logger.warning(
            "tags revision epoch file unreadable; minting opaque (unordered) revisions",
            exc_info=True,
        )
        return None
    claimed = max(previous + 1, time.time_ns() // 1000)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The anti-regression guarantee rests on the persisted counter surviving
        # a crash: without the data AND the directory entry on disk, a power
        # loss inside the flush window followed by a backward clock step would
        # re-claim an epoch that connected clients already hold.
        atomic_write(path, f"{claimed}\n", fsync=True)
        fsync_dir(path.parent)
    except OSError:
        logger.warning(
            "tags revision epoch not persisted; minting opaque (unordered) revisions",
            exc_info=True,
        )
        return None
    return claimed


# Sentinel stored in _TAGS_REVISION_EPOCH once a claim failed, so the disk is not
# retried on every mint; the process stays on opaque revisions until restart.
_TAGS_REVISION_EPOCH_UNPERSISTED = -1


def ensure_tags_revision_epoch() -> int | None:
    """Claim this process's epoch now (idempotent); None when unpersisted.

    Called from startup code that already runs off the event loop
    (``DashboardState.load_tags`` via ``asyncio.to_thread``) so the one disk
    read/write the claim performs never happens inside a request handler;
    ``mint_tags_revision`` keeps a lazy claim only as a fallback for callers
    that construct slots without a full startup (tests, tools).
    """
    global _TAGS_REVISION_EPOCH
    with _TAGS_REVISION_SEQ_LOCK:
        if _TAGS_REVISION_EPOCH is None:
            claimed = _claim_tags_revision_epoch()
            _TAGS_REVISION_EPOCH = _TAGS_REVISION_EPOCH_UNPERSISTED if claimed is None else claimed
        epoch = _TAGS_REVISION_EPOCH
    return None if epoch == _TAGS_REVISION_EPOCH_UNPERSISTED else epoch


def mint_tags_revision() -> str:
    """Return a new tag revision: ``<16-digit epoch>.<20-digit sequence>-<8 hex>``.

    Zero-padded epoch then sequence sort lexically and numerically alike, so a
    client compares two revisions for staleness by string order alone: a later
    gateway process (higher epoch) always wins over an earlier one, and within a
    process the sequence orders commits. The random suffix keeps revisions
    unique even if two processes ever claimed the same epoch. When no epoch
    could be persisted (unwritable data home) an opaque ``uuid4`` hex is
    returned instead, which clients treat with equality + lineage only.
    """
    global _TAGS_REVISION_SEQ
    epoch = ensure_tags_revision_epoch()
    if epoch is None:
        # No durable epoch: an opaque revision. Clients treat it with the
        # equality/lineage rules (no ordering is asserted).
        return uuid.uuid4().hex
    with _TAGS_REVISION_SEQ_LOCK:
        _TAGS_REVISION_SEQ += 1
        seq = _TAGS_REVISION_SEQ
    return f"{epoch:016d}.{seq:020d}-{uuid.uuid4().hex[:8]}"


def init_sidebar_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    # Artifact companion binding: set when this slot is a
    # companion chat session for an artifact (slug). At most one
    # non-archived slot per slug by convention — the frontend flow
    # maintains the invariant (archive-then-create); the backend accepts
    # any valid slug and does not enforce uniqueness. This IS serialized
    # (to_dict) and persisted (history meta) — the dashboard resolves the
    # active binding from the slots snapshot, and the binding must survive
    # gateway restarts.
    self._artifact = ""
    # True once per-channel default filing has been APPLIED to this
    # conversation (see kiro_crew.dashboard.channel_folders). Persisted,
    # because it is the only durable record that the automatic placement
    # already happened: `folder_id` is omitted from the metadata line when
    # empty, so a conversation the user drags out to the top level is
    # otherwise indistinguishable from one that was never filed, and the
    # next reconcile pass after a restart would file it right back in.
    # Default filing is a first-surface action, not a recurring one.
    self._channel_folder_filed = False
    self.folder_id = ""  # project folder assignment
    self._folder_changed = False  # re-inject [FOLDER] breadcrumb next turn after move
    # One-shot claim for the post-titling folder suggestion (see
    # chat_folder_suggest.maybe_suggest_folder). In-memory only: a restored
    # slot is already titled, so the suggestion hook never re-fires for it
    # and a reset flag cannot produce a second card.
    self._folder_suggested = False
    self.pinned = False  # pinned to top of sidebar
    # When set on a creating session, every session it opens (and anything
    # those open, down the ``_created_by`` chain) is muted for attention:
    # no turn-done chime, background-finished toast or unread badge. The
    # creator itself stays unmuted -- its reports are the signal the user
    # wants -- and tool-approval prompts stay exempt. Owned by the
    # user through the slot's kebab menu, never set by an agent, and
    # derived from the durable ``created_by`` chain on the client.
    self.mutes_opened = False
    # Per-row mute of THIS session, set by the user from its own kebab
    # menu: no turn-done chime, background-finished toast or unread badge
    # on the client. Tool-approval prompts stay exempt. Durable like ``pinned``; never set by an agent.
    self.muted = False
    self.tags = []  # assigned tag ids (see DashboardState._tags)
    # Change identity for tag snapshots. Orderable (see mint_tags_revision):
    # equality identifies a specific frame, and the sequence prefix lets a
    # client classify an unseen older snapshot as stale rather than newer.
    self.tags_revision = mint_tags_revision()
    self.color_index = None
    # Custom per-session color (#rrggbb, lowercase). Mutually exclusive
    # with color_index: the PATCH handler clears one when the other is
    # set, and the frontend renders color_hex with priority. Unlike
    # color_index (resolved against the viewer's generated palette, so it
    # follows theme/palette switches), a custom hex is deliberately
    # frozen.
    self.color_hex = None
    self.color_theme = ""
    # Explicit user consent for the active INSTALLED theme's experience
    # layer (persona injection is gated on this; fail-closed default).
    self.theme_consent = False
    # Content-bound persona consent: sha256 hex of the installed pack's
    # persona text the user granted in the consent modal. Persona injection
    # requires this to match the persona read from disk (fail-closed None).
    self.theme_consent_sha = None


def bump_tags_revision(self: _ChatSlot) -> str:
    """Rotate and return the revision for the current tag list.

    Totally ordered, not merely opaque: ``mint_tags_revision`` pairs a
    persisted per-process epoch with a strictly increasing sequence, so a
    client can tell an older snapshot it has never seen (a delayed HTTP
    fetch landing after a newer WebSocket frame, or a slow reply from the
    pre-restart process) from a genuinely newer commit by string order
    alone. No wall-clock value participates after the first epoch is
    seeded, so a clock step cannot make revisions regress.
    """
    self.tags_revision = mint_tags_revision()
    return self.tags_revision
