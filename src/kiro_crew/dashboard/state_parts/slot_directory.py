"""Slot identity and lookup: keys, caps, origin, and getting or creating a slot.

Key normalising and minting, the live and per-creator caps, ``SlotOrigin``,
native-subagent snapshots, Slack thread links, and the member-row record a
created slot writes.

Instance fields of ``DashboardState`` this module reads and writes:

Reads only: ``_ephemeral_keys``, ``_restricted_keys``, ``_slack_to_slot``, ``_slots``,
``_slots_under_construction``, ``restoring_open_slots``, ``sessions``,
``unrestored_slot_keys``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.session_pulse_counter import increment_user_session_count_off_loop
from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
from kiro_crew.dashboard.state_parts.dashboard_services import _registry_for
from kiro_crew.dashboard.state_parts.transcript_rows import chat_message_note
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    is_channel_session_key,
    split_namespaced_channel_id,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


#: The single ceiling on live slots, owned by the module that owns the slot
#: table (see :meth:`DashboardState.live_slot_count`). Every path that allocates
#: a slot -- session create, chat fork, session import -- tests ``live_slot_count()``
#: against this one number, so raising the ceiling is a single edit and no entry
#: point can silently drift to a different limit.
MAX_LIVE_SLOTS = 500


#: The most live slots ONE creator may hold, as a sub-ceiling under
#: :data:`MAX_LIVE_SLOTS`. The global ceiling alone bounds the total but not the
#: distribution, so a single automated creator working through a nudge loop can
#: reach 500 on its own and every later create -- including the person opening a
#: new chat tab -- gets the 429. That is the availability half of the cap: a
#: bounded resource that one caller may exhaust entirely is not bounded from
#: anybody else's point of view.
#:
#: Deliberately far above real use and far below the global ceiling: a decomposed
#: goal runs on the order of ten concurrent sessions, so 50 never binds honest
#: work, while 450 slots stay reachable by everyone else no matter what one
#: caller does. Enforced in ``session_control.create_session``, which is the only
#: entry point that creates on some OTHER caller's behalf; a person's own new tab
#: and a fork are attributed to nobody and so are bounded by the global ceiling
#: alone.
MAX_SLOTS_PER_CREATOR = 50


def _is_genuine_slack_link(thread_ts: str | None, channel_id: str | None) -> bool:
    """True only for a complete Slack link, never another channel's legacy id."""
    namespaced = split_namespaced_channel_id(channel_id)
    return bool(
        thread_ts and channel_id and (namespaced is None or namespaced[0] == SLACK_NAMESPACE)
    )


# Native kiro-cli subagent reconnect policy. The slot state, writer, and replay
# path all import these bounds so retention cannot drift between modules.
NATIVE_SUBAGENT_OUTPUT_TAIL = 40_000


NATIVE_SUBAGENT_OUTPUT_HARD = 80_000


NATIVE_SUBAGENT_DONE_RESULT_CAP = 8_000


NATIVE_SUBAGENT_DONE_TRUNC_MARKER = "…(earlier output truncated)\n"


NATIVE_SUBAGENT_TERMINAL_KEEP = 50


NATIVE_SUBAGENT_TERMINAL_TTL_SECS = 3600.0


def native_subagent_output_tail(chunks: list[str], limit: int = NATIVE_SUBAGENT_OUTPUT_TAIL) -> str:
    """Join only the trailing ``limit`` characters of native-card output."""
    if limit <= 0:
        return ""
    collected: list[str] = []
    total = 0
    for chunk in reversed(chunks):
        collected.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    collected.reverse()
    return "".join(collected)[-limit:]


# Auto-minted dashboard slot keys share the shape "<prefix>-<N>-<ts>" where
# <prefix> is chat (the only auto-mint prefix in this fork), <N> is the
# monotonic _slot_counter, and <ts> is a unix second. Minting and index-parsing
# both go through these helpers so the format lives in exactly one place — a
# future change to the key shape can't silently desync the minter from
# reseed_slot_counter() (which would let the post-restart tab<->session
# collision quietly return).
def _mint_slot_key(prefix: str, counter: int, ts: int) -> str:
    """Build an auto-minted slot key of the canonical ``<prefix>-<N>-<ts>`` shape."""
    return f"{prefix}-{counter}-{ts}"


def _slot_index_from_key(key: str) -> int | None:
    """Return the ``<N>`` index from a ``<prefix>-<N>-<ts>`` slot key, else None.

    Non-auto-minted keys (Slack sessions, ascii-sanitized display names) don't
    match the shape and return ``None``. The ``isascii()`` guard keeps a stray
    unicode-digit char (``str.isdigit()`` is True for e.g. superscripts, but
    ``int()`` would raise) from aborting boot-time reseeding.
    """
    parts = key.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
        return int(parts[1])
    return None


# Bare chat-N label matcher used by DashboardState.resolve_slot() for prefix fallback.
# Gates the prefix lookup to prevent broad matches (e.g. bare "chat" binding to any slot).
_CHAT_N_RE = re.compile(r"chat-\d+")


def _ascii_slot_key(name: str) -> str:
    """Return *name* with any character outside printable ASCII replaced by ``-``.

     A slot key becomes the session key (``dashboard:{slot.key}``) that
     kirocrew-core sends as the ``X-Session-Key`` HTTP header on every gateway
     call. Header values are latin-1 per RFC 7230, so a non-latin-1 char (e.g.
     an em-dash from a title-derived slot name) would abort every tool call
    . ASCII control characters (notably CR/LF) are excluded too, so
     a name can never inject into or split the header. Idempotent;
     printable-ASCII names — including the auto-generated ``chat-N-<ts>`` keys —
     are returned unchanged. (Path-separator/traversal containment for keys later
     used as filesystem paths is enforced separately at the persistence layer.)
    """
    return re.sub(r"[^\x20-\x7e]", "-", name)


# Characters that survive the history layer's ``_safe_key()`` filename fold
# (``re.sub(r"[^\w\-.]", "_", key)``). ``re.ASCII`` pins ``\w`` to
# ``[a-zA-Z0-9_]`` — the input is already ASCII-folded, so this matches what
# ``_safe_key`` produces byte-for-byte.
_SLOT_KEY_FILENAME_UNSAFE_RE = re.compile(r"[^\w\-.]", flags=re.ASCII)


# Longest slot key kept verbatim. A transcript is ``<stem>.jsonl`` -- the stem
# is ``dashboard_<key>`` for a dashboard slot and the key itself for a channel
# slot -- and filesystems refuse a name over 255 bytes (ENAMETOOLONG). So a key
# longer than this could never have had a transcript of either kind; only such
# a key is shortened, and a key whose transcript may already exist keeps its
# spelling so a restart still finds it. A longer key keeps its first
# ``_SLOT_KEY_KEEP_CHARS`` characters plus ``-`` and a short sha256 of the whole
# folded key: a valid filename stem, stable across calls, distinct per name, and
# short enough to stay unchanged when folded again.
_SLOT_KEY_MAX_CHARS = 255 - len(".jsonl")


_SLOT_KEY_KEEP_CHARS = 180


_SLOT_KEY_HASH_CHARS = 12


def _normalize_slot_key(name: str) -> str:
    """Return *name* folded to the exact charset of a persisted session filename.

    Guarantees the invariant a restart depends on: for any input,
    ``_safe_key(_history_key_for(key))`` == ``f"dashboard_{key}"`` — i.e. the
    slot key equals its JSONL filename stem minus the ``dashboard_`` prefix.

    Three steps compose: strip a ``dashboard:``/``dashboard_`` transport
    prefix (a full session key or filename stem sometimes reaches slot-name
    positions; ``_history_key_for`` strips the same prefixes when building the
    history key, so such names already share one transcript with their bare
    form and must share one slot), then :func:`_ascii_slot_key` (header
    safety), then a filename fold using the same character class as
    ``history._safe_key``.

    Without the filename fold, a display-style slot name (e.g.
    ``Artifact: My Doc`` from the artifact iterate flow) diverges from its
    sanitized filename stem. After a gateway restart, ``restore_open_slots``
    rehydrates the raw key from ``open_slots.json`` while
    ``restore_recent_sessions`` derives a second slot from the filename stem —
    the dedup guards compare mismatched strings, so the user sees two
    identical sidebar sessions backed by one transcript, and the next
    ``_persist_open_slots`` flush cements both keys. Idempotent;
    auto-generated ``chat-N-<ts>`` keys are returned unchanged. A key longer
    than ``_SLOT_KEY_MAX_CHARS`` is shortened (see the constant) so its
    transcript filename stays under the filesystem's name limit.
    """
    if name.startswith("dashboard:"):
        name = name[len("dashboard:") :]
    while name.startswith("dashboard_"):
        name = name[len("dashboard_") :]
    key = _SLOT_KEY_FILENAME_UNSAFE_RE.sub("_", _ascii_slot_key(name))
    if len(key) > _SLOT_KEY_MAX_CHARS:
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()[:_SLOT_KEY_HASH_CHARS]
        key = f"{key[:_SLOT_KEY_KEEP_CHARS]}-{digest}"
    return key


class SlotOrigin:
    """Slot creation origin — who initiated the slot.

    Used by the WS event scope gate to decide which events an app token may
    receive (e.g. ``slots:user`` grants visibility into ``USER``-origin slots
    regardless of their ``_app`` owner).
    """

    USER = "user"  # initiated from the dashboard UI (no app token)
    APP = "app"  # initiated by an app SDK call (carries owner _app)
    CRON = "cron"  # initiated by a cron job
    SYSTEM = "system"  # gateway-internal (startup, migration, etc.)


def request_slot_origin(app: str, *, cron_creator: str = "") -> str:
    """Origin for a slot created while serving an HTTP request.

    The request layer is the only place that knows whether an app token was
    presented, which is what separates APP from USER. Call it with the
    request's app name (``request.get("app", "")``) — empty means the caller
    authenticated as the dashboard user, so the slot genuinely is USER.

    Background callers (cron, workflow, Slack, rehydrate) must NOT use this:
    they have no request and would mislabel their slot as a person's, which is
    exactly what `slots:user` grants an app access to. They declare their own
    origin, or leave it untagged.

    ``cron_creator`` is the attested ``cron:<job id>`` key of the caller when
    the request carried one (``handlers._shared.cron_slot_creator``), and it
    wins: a slot a cron opens is CRON, never USER, for the same reason the
    background callers above declare it, so ``slots:user`` never exposes a
    cron's work. It is empty for a person and for an app token.
    """
    if cron_creator:
        return SlotOrigin.CRON
    return SlotOrigin.APP if app else SlotOrigin.USER


def get_slot(self: DashboardState, name: str) -> _ChatSlot | None:
    """Look up a slot by name without creating it. Returns None if absent.

    Also returns ``None`` for a slot still marked under construction. The
    hydrate loop is synchronous, but the import path does async Layer B
    write/join and a durable save after the loop; it RETRACTS the slot from
    ``_slots`` across that tail as the primary protection, so a lookup finds
    nothing then anyway. This construction-mark check is the belt-and-braces
    layer for any construction path that keeps the slot registered while it
    awaits: acquisition must not hand out a not-yet-finalized session.
    ``serialize_slots`` hides it from the payload; resume dedup reads
    ``_slots`` directly (``_live_slot_resume_response``), so a resuming slot
    stays discoverable for dedup while acquisition through this door is
    refused.
    """
    slot = _registry_for(self).get_slot(self, name)
    if slot is not None and slot.key in getattr(self, "_slots_under_construction", ()):
        return None
    return slot


def running_session_keys(self: DashboardState) -> frozenset[str]:
    """Return effective session keys whose current slots are running."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    return _registry_for(self).running_session_keys(self, effective_session_key)


def spend_slot_by_session(self: DashboardState) -> dict[str, str]:
    """Map live effective session identities to their spend-owning slot keys."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    return _registry_for(self).spend_slot_by_session(self, effective_session_key)


def native_subagent_snapshots(
    self: DashboardState,
    terminal_limit: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
    ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
) -> list[dict[str, object]]:
    """Return bounded native running and terminal cards for WS replay.

    DashboardState owns the slot record shape. The WebSocket layer consumes
    these transport-ready snapshots without reaching into private slot data.
    """
    now = time.time()
    running: list[dict[str, object]] = []
    done: list[dict[str, object]] = []
    for slot in list(self._slots.values()):
        output = slot._native_subagent_output
        for info in list(slot._native_subagent_tracker.values()):
            card_id = str(info.get("id") or "")
            if not card_id:
                continue
            base: dict[str, object] = {
                "id": card_id,
                "slot": slot.key,
                "task": str(info.get("task") or ""),
                "agent": str(info.get("agent") or ""),
            }
            if info.get("done"):
                done_at = float(info.get("done_at") or 0.0)
                if done_at and (now - done_at) > ttl_secs:
                    continue
                if info.get("stopped"):
                    outcome = "stopped"
                elif info.get("error"):
                    outcome = "failed"
                else:
                    outcome = "completed"
                done.append(
                    {
                        **base,
                        "done": True,
                        "elapsed": float(info.get("elapsed") or 0.0),
                        "error": info.get("error"),
                        "stopped": bool(info.get("stopped")),
                        "outcome": outcome,
                        "result": str(info.get("result") or ""),
                        "done_at": done_at,
                    }
                )
            else:
                running.append(
                    {
                        **base,
                        "done": False,
                        "streaming": native_subagent_output_tail(output.get(card_id, [])),
                        "last_tool": str(info.get("last_tool") or ""),
                        "started": float(info.get("started") or now),
                    }
                )
    if terminal_limit >= 0 and len(done) > terminal_limit:

        def snapshot_done_at(snapshot: dict[str, object]) -> float:
            value = snapshot.get("done_at")
            return float(value) if isinstance(value, (int, float)) else 0.0

        done.sort(key=snapshot_done_at, reverse=True)
        done = done[:terminal_limit]
    return running + done


def has_slot(self: DashboardState, name: str) -> bool:
    """Check if a slot exists by name."""
    return _registry_for(self).has_slot(self, name)


def slot_exists(self: DashboardState, name: str) -> bool:
    """Whether *name* names a session that is open, INCLUDING one still being built.

    The existence question, as opposed to the acquisition one :meth:`get_slot`
    answers. ``get_slot`` hides an under-construction slot so nobody acquires a
    half-finished session, and the import path even retracts its slot from
    ``_slots`` across its async tail while leaving the construction mark set. A
    caller asking "has this session ENDED" must read both as open: a worker that is
    rehydrating or resuming has not closed, and treating it as closed would let the
    work-ledger gate record a permanent "worker gone" for a live worker.

    Two more states are open for the same reason, though no slot object exists for
    either. A key the last open-tab restore could not READ (``unrestored_slot_keys``)
    is a session whose metadata read failed transiently, not one proven gone -- the
    restart-restore snapshot keeps it for exactly that reason. And while a restore is
    in flight (``restoring_open_slots``), a tab it has not reached yet has no slot, so
    absence proves nothing about any name until the restore ends.
    """
    if _registry_for(self).has_slot(self, name):
        return True
    if name in (getattr(self, "_slots_under_construction", None) or ()):
        return True
    if name in (getattr(self, "unrestored_slot_keys", None) or ()):
        return True
    return bool(getattr(self, "restoring_open_slots", False))


def get_linked_slot(self: DashboardState, session_key: str) -> "_ChatSlot | None":
    """Resolve a Slack link and clean up a stale reverse-index entry."""
    return _registry_for(self).get_linked_slot(self, session_key)


def resolve_slot(self: DashboardState, name: str) -> _ChatSlot | None:
    """Resolve an exact key or the newest timestamped bare ``chat-N`` key."""
    return _registry_for(self).resolve_slot(self, name, _CHAT_N_RE.fullmatch)


def link_slack(self: DashboardState, slot_name: str, thread_ts: str, channel_id: str) -> None:
    """Update a slot's Slack link state and persist to SessionStore."""
    slot = self._slots.get(slot_name)
    if not slot:
        logger.warning("slack link skipped: no live slot %r (chat closed?)", slot_name)
        return
    # A thread handoff is ONE action with TWO persisted writes: the previous
    # owner's link is cleared and this slot's is claimed. Each write rewrites
    # the whole session map, so as two separate writes they are separately
    # interruptible — a failure or a concurrent writer in between leaves the
    # thread with no owner (the clear landed, the claim did not) or with two
    # (the reverse). Batching makes the pair one critical section and one
    # write, matching the same guarantee ``SessionMap.set_slack_link``
    # already gives its own eviction-and-claim.
    with self.sessions.batched_save() if self.sessions else contextlib.nullcontext():
        self._link_slack_persisted(slot, slot_name, thread_ts, channel_id)
    self.push_slots_update()


def _link_slack_persisted(
    self: DashboardState, slot: Any, slot_name: str, thread_ts: str, channel_id: str
) -> None:
    """The link handoff itself: in-memory indexes plus both persisted writes.

    Split out only so :meth:`link_slack` can wrap the whole sequence in one
    ``batched_save``; the dashboard push stays OUTSIDE that block because it
    is not a map mutation.
    """
    # Remove the stale mapping if the slot is mapped to a different thread
    old_ts = slot._slack_thread_ts
    if old_ts and old_ts != thread_ts:
        self._slack_to_slot.pop(old_ts, None)
    # Clear the persisted link of the old slot if another slot owns this thread
    old_owner = self._slack_to_slot.get(thread_ts)
    if old_owner and old_owner != slot_name:
        old_slot = self._slots.get(old_owner)
        if old_slot:
            old_slot._slack_linked = False
            old_slot._slack_thread_ts = ""
            old_slot._slack_channel = ""
        if self.sessions:
            from kiro_crew.dashboard.chat_utils import (
                _history_key_for,
                effective_session_key,
            )

            # The previous owner's slot may already be gone; fall back to
            # deriving its key from the name in that case.
            old_key = effective_session_key(old_slot) if old_slot else _history_key_for(old_owner)
            self.sessions.set_slack_link(old_key, "", "")
    slot._slack_linked = True
    slot._slack_channel = channel_id
    slot._slack_thread_ts = thread_ts
    self._slack_to_slot[thread_ts] = slot_name
    # Persist so link survives gateway restarts
    if self.sessions:
        from kiro_crew.dashboard.chat_utils import effective_session_key

        self.sessions.set_slack_link(effective_session_key(slot), thread_ts, channel_id)


def get_or_create_slot(
    self: DashboardState,
    name: str | None = None,
    agent: str = "",
    workspace: str = "default",
    model: str = "",
    mode: str = "",
    memory_mode: str | None = None,
    ephemeral: bool | None = None,
    app: str = "",
    linked_session_key: str = "",
    channel_origin: bool = False,
    origin: str | None = None,
    *,
    # Opt-in for the survey's "new user" session counter, DISTINCT from the
    # origin tag: ``SlotOrigin.USER`` carries the ``slots:user`` privacy
    # semantics and is deliberately set by non-human paths too (the
    # session-control create verb mints USER slots so agent-created
    # sessions stay app-invisible), so origin alone cannot mean "a person
    # started this chat". Only the human request-layer paths (chat-send
    # auto-create, new-chat tab, fork) pass True. Default False is the
    # chosen fail direction, matching
    # the counter's own philosophy below: a future missed opt-in only
    # delays the survey (lost signal); defaulting to count would let
    # unattended agent activity satisfy the gate (corrupted signal).
    count_user_session: bool = False,
) -> _ChatSlot:
    """Return existing slot or create a new one.

    *linked_session_key* binds a new slot to the session its conversation
    actually runs on (a channel thread, a cron job). It must be supplied
    here rather than assigned afterwards: the Slack-link hydration below
    reads the persisted link off the slot's effective session key, so a
    binding applied later would hydrate against the wrong key and leave a
    channel-born tab looking unlinked.
    """
    existing, creation = _registry_for(self).prepare_creation(
        self,
        name,
        mode=mode,
        memory_mode=memory_mode,
        normalize_key=lambda value: _normalize_slot_key(value),
        mint_key=lambda prefix, index, timestamp: _mint_slot_key(prefix, index, timestamp),
        timestamp_provider=lambda: time.time(),
    )
    if existing is not None:
        # An under-construction slot is registered (so a same-key resume
        # dedups against it) but not yet a live session: the import path holds
        # construction across its async Layer B finalization tail. Refuse to
        # hand it to an acquirer that would treat it as resumable before that
        # lands. Create-or-send callers (``api_chat``) handle ValueError as a
        # 409 -- retry once the build finishes.
        if existing.key in getattr(self, "_slots_under_construction", ()):
            raise ValueError(f"slot {existing.key} is still being built; retry once it is ready")
        return existing
    assert creation is not None
    name = creation.key
    # Refuse to MINT on a key that is under construction. The existing-branch
    # guard above only fires when the slot is in ``_slots``; the import path
    # RETRACTS its slot from ``_slots`` for its async Layer B tail while
    # leaving the construction mark set, so a create on that (predictable,
    # ``chat-N-<ts>``-shaped) key would otherwise miss the guard, take this
    # mint path, and produce a second slot sharing the effective session key
    # Layer B was just joined to. Keying on the construction mark rather than
    # ``_slots`` membership covers both the registered and the retracted
    # window. The constructor itself does not trip this: import mints a fresh
    # key (name=None) that is not yet under construction, and only begins
    # construction after this returns. Depends on
    # ``_materialise_slot_from_history`` leaving the construction mark set on
    # its success path (see the comment there); do not change one side alone.
    if name in getattr(self, "_slots_under_construction", ()):
        raise ValueError(f"slot {name} is still being built; retry once it is ready")
    requested_name = creation.requested_name
    minted_new = creation.minted_new
    slot = _ChatSlot(
        name,
        agent=agent,
        workspace=workspace,
        model=model,
        mode=mode,
        memory_mode=memory_mode or "persistent",
    )
    if requested_name and requested_name != name:
        # The caller asked for a human-readable name (e.g. "Artifact: My
        # Doc"); the key had to be folded, but the pretty form makes a
        # better initial title than the "New session" placeholder. Titles
        # are dashboard-surfaced, so apply the same redaction as explicit
        # title pinning in api_chat_slot_create. ``_titled`` stays False —
        # auto-title and explicit pinning can still override.
        pretty_title, _ = redact_exfiltration_urls(requested_name)
        pretty_title, _ = redact_credentials(pretty_title)
        slot.title = pretty_title
    slot._tab_id = uuid.uuid4().hex[:12]
    slot._on_message = self._broadcast_chat_message
    slot._on_row = self._record_member_row
    slot._on_card_event = self.notify_dashboard_card
    slot._on_question_retired = self._broadcast_question_retired
    slot._coordinator_approvals = self.pending_coordinator_approvals
    slot._linked_session_busy = self.linked_session_busy
    slot._app = app
    # ``origin`` must be declared by the layer that actually knows it, and
    # an undeclared non-app slot stays UNTAGGED ("") rather than being
    # called USER.
    #
    # Deriving USER here would be fail-OPEN: this function cannot tell a
    # person typing in the dashboard from a background injection, so every
    # untagged caller — cron result injection, workflow inject, Slack, the
    # OpenAI-compatible endpoint — would read as USER and hand that private
    # content to an app holding `slots:user`. Only the request layer
    # knows whether an app token was presented, so USER/APP is decided
    # there (see chat_handlers) and background callers declare CRON/SYSTEM.
    #
    # "" is invisible to every cross-slot scope (the gate compares against
    # SlotOrigin.USER), so a caller that forgets to declare loses
    # visibility instead of leaking — the direction this has to fail in.
    slot._origin = origin or (SlotOrigin.APP if app else "")
    if minted_new and count_user_session and slot._origin == SlotOrigin.USER:
        # Count only genuine, newly-minted user chats toward the survey's
        # "new user" window (session_pulse_counter). `minted_new` excludes
        # restore/rehydrate (which passes the persisted key as name) and
        # get-existing, so a restart never re-counts already-seen sessions.
        # `count_user_session` carries the human-started signal: only the
        # request-layer paths a person actually drives (chat-send
        # auto-create, new-chat tab, fork) opt in, so agent-minted USER
        # slots (the session-control create verb) do not satisfy the
        # survey gate on their own. The
        # origin conjunct stays as the invariant floor: a caller can never
        # count a non-USER slot, flag or not. Best-effort:
        # the helper swallows its own I/O errors and never raises into
        # slot creation.
        #
        # Off the loop, because this method is synchronous and every
        # request-layer birth runs it on the gateway loop -- the counter's
        # read + mkdir + tempfile write + replace would stall it on slow
        # storage. The offload is the counter's, not this allocation's: this
        # block must not become a suspension point, or callers could observe
        # a half-configured slot.
        increment_user_session_count_off_loop()
    if memory_mode and memory_mode != "persistent":
        self._restricted_keys.add(f"dashboard:{name}")
    if ephemeral:
        self._ephemeral_keys.add(f"dashboard:{name}")
    # Hydrate only a complete, genuine Slack link. Other transports still
    # write their namespaced origin id through the legacy channel field;
    # those are projected separately via ``links`` and must never make the
    # destructive Slack actions appear.
    if channel_origin:
        # Additive: never cleared, because get_or_create_slot also returns
        # EXISTING slots and a later plain call must not downgrade a tab
        # that a channel path already claimed.
        slot.channel_origin = True
    if linked_session_key:
        slot.linked_session_key = linked_session_key
        # Every path that sets a link records the class it just changed. Free when
        # the slot has no live session yet, which is the common case here.
        self.note_crew_log_class(slot)
    elif self.sessions and not app:
        # No caller-supplied binding, but a channel-stem name means this slot
        # displays a conversation that runs on the channel's own session.
        # Resolving it HERE rather than in each caller is what makes the
        # binding correct by construction: the History resume path builds the
        # slot without one, and an unbound channel tab silently answers from a
        # dashboard-only session whose replies never reach the thread.
        #
        # Only ever adopts a key the session map actually holds, so a slot
        # whose name merely looks channel-shaped stays unbound. Validated the
        # same way ``surface_channel_session`` validates its own argument:
        # only a real channel key may become a binding, so a malformed map
        # answer leaves the slot unbound (a supported state) rather than
        # routing the user's replies to a session no channel reads.
        #
        # Never for an APP-owned slot: a channel thread is the person's
        # conversation, and the name is app-supplied, so resolving it here
        # would let an app that knows a stem mint a slot bound to — and
        # writing metadata into — a transcript it does not own.
        if is_channel_session_key(name):
            resolved = self.sessions.channel_key_for_stem(name)
            if isinstance(resolved, str) and is_channel_session_key(resolved):
                slot.linked_session_key = resolved
                self.note_crew_log_class(slot)
    try:
        if self.sessions:
            from kiro_crew.dashboard.chat_utils import effective_session_key

            _ts, _ch = self.sessions.get_slack_link(effective_session_key(slot))
            slot._slack_linked = _is_genuine_slack_link(_ts, _ch)
            if slot._slack_linked:
                namespaced = split_namespaced_channel_id(_ch)
                slot._slack_channel = namespaced[1] if namespaced else (_ch or "")
                slot._slack_thread_ts = _ts or ""
                # Rebuild the thread -> slot index too, not just the fields:
                # inbound replies resolve through the index, so restoring
                # the fields alone leaves a mirrored session delivering to
                # Slack but not back to its tab after a restart.
                #
                # Index ONLY a genuine mirror-OUT. A channel-born session's
                # ``slack_thread_ts`` is a SELF-reference -- the thread the
                # session lives IN, not one it mirrors TO -- and indexing
                # that would make every inbound Slack message resolve to a
                # "linked" slot and run through the dashboard chat runner
                # instead of the Slack transport, silently changing the
                # execution engine and approval semantics of all Slack
                # traffic.
                #
                # Both tests are load-bearing and neither is a name
                # heuristic. A channel slot whose stem RESOLVED is caught by
                # ``linked_session_key``; one whose stem did NOT resolve
                # (leaving that field empty) is caught by comparing the link
                # against the slot's own filename stem, because a channel
                # slot is named for the very thread it lives in. A dashboard
                # slot that merely happens to be named ``slack_...`` matches
                # neither test and is still indexed.
                from kiro_crew.history import _safe_key
                from kiro_crew.messaging.link import canonical_key

                _self_ref = False
                if _ts:
                    _self_ref = _safe_key(canonical_key(_ts)) == name
                if _ts and not slot.linked_session_key and not _self_ref:
                    self._slack_to_slot[_ts] = name
    except Exception:
        pass
    _registry_for(self).put_slot(self, name, slot)
    # Publish the updated key set to SessionManager and the surface
    # registry NOW, not just on the HTTP slot endpoints. Slots born here
    # programmatically (auto-research campaign workers, cron/workflow
    # inject, task runner, spec builder) never pass through those
    # endpoints, so ``_active_dashboard_slots`` stayed stale and the idle
    # sweep's orphan branch reaped their live sessions as "slot gone" —
    # killing the companion subagent runtime (and any subagent mid-prompt
    # on it) along the way. Guarded: tests build this state without a
    # SessionManager, and a sync failure must never break slot creation.
    if self.sessions:
        try:
            from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

            _sync_dashboard_slots(self)
        except Exception:
            logger.warning(
                "get_or_create_slot: active-slot sync failed for %s", name, exc_info=True
            )
    self.push_slots_update()
    return slot


def live_slot_count(self: DashboardState) -> int:
    """Count published and allocated-but-unpublished slots."""
    return _registry_for(self).live_slot_count(self)


def creator_slot_count(self: DashboardState, creator_key: str) -> int:
    """Count live slots attributed to one non-empty creator key."""
    return _registry_for(self).creator_slot_count(self, creator_key)


def begin_slot_construction(self: DashboardState, key: str) -> None:
    """Mark a slot key as allocated but not yet published."""
    _registry_for(self).begin_slot_construction(self, key)


def end_slot_construction(self: DashboardState, key: str) -> None:
    """Release an allocated-but-unpublished slot marker."""
    _registry_for(self).end_slot_construction(self, key)


def reseed_slot_counter(self: DashboardState) -> None:
    """Advance the mint counter past every parseable current slot key."""
    _registry_for(self).reseed_slot_counter(
        self,
        slot_index_from_key=lambda name: _slot_index_from_key(name),
        logger_provider=lambda: logger,
    )


def _broadcast_chat_message(self: DashboardState, slot_key: str, msg: dict) -> None:
    """Push a chat message to all SSE clients via the global stream."""
    workspace = None
    if msg.get("role", "") != "user" and msg.get("content", ""):
        workspace = getattr(self.get_slot(slot_key), "workspace", None)
    self._broadcast(chat_message_note(slot_key, msg, workspace=workspace))


def _record_member_row(self: DashboardState, slot_key: str, msg: dict) -> None:
    """Append one ``member/message`` for a row landing in a member DM slot.

    Wired as ``Slot._on_row``, which fires for EVERY live appended row, and
    deliberately NOT off ``_broadcast_chat_message``: that method is the SSE
    delivery path and is skipped for a ``user`` row the dashboard composer
    already echoed, and for any row on a slot with an HTTP stream reader
    attached. The roster's ``last_active_ts`` is folded from these events, so
    a recorder reachable only through the delivery path cannot see the one
    action the user most expects to reorder the roster: typing into a
    crewmate's DM.

    Nothing outside a member DM slot is recorded: ``member_slug_for_slot``
    answers ``None`` for every other key.

    Best-effort throughout, deliberately: a logging fault must not break a
    message the user has already sent.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")
    try:
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import MEMBER_MESSAGE

        _mslug = eventlog_hooks.member_slug_for_slot(slot_key)
        if _mslug is None:
            return
        _raw_ts = msg.get("ts", "")
        try:
            _ev_ts = float(_raw_ts)
        except (TypeError, ValueError):
            _ev_ts = time.time()

        # Same redaction chain the members roster read uses, run
        # before the length cap so a credential split by truncation
        # cannot leak. The payload is built by the one shared spelling
        # (`member_message_payload` -> `speech_preview`) so the folded
        # preview equals what `GET /api/members` reads back.
        def _sanitize_preview(text: str) -> str:
            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
            return text

        _payload = eventlog_hooks.member_message_payload(
            role, content, msg.get("meta"), _ev_ts, sanitize=_sanitize_preview
        )

        # Off the event loop: emit opens the member log and does a
        # synchronous os.fsync append. This callback runs loop-side, so
        # hand the write to a worker thread (fire-and-forget, best-effort
        # like the rest of this block) rather than stalling every gateway
        # task on the fsync.
        def _emit_message() -> None:
            eventlog_hooks.emit(
                _mslug,
                None,
                MEMBER_MESSAGE,
                _payload,
            )

        # Queued on the ordered executor either way -- see the slot
        # emit for why a no-loop caller queues rather than writing
        # inline.
        #
        # The return is deliberately not read, and this is the ONE thing
        # that makes it safe: nothing here records that the event was
        # written. A refused or failed append costs this path exactly the
        # event it was given, which the queue's own ceiling documents and
        # reports. The slot path above cannot do the same because it keeps
        # a checkpoint, and a checkpoint that outlives a lost append turns
        # one missing event into a view that never recovers.
        eventlog_hooks.submit(_emit_message)
    except Exception:
        logger.debug("member/message event-log hook failed", exc_info=True)
