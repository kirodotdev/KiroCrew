"""A session's channel links: the Slack thread link, the mirror binding and the session's own link.

A Slack link lives in the ``slack_thread_ts`` / ``slack_channel_id`` fields, with the reverse
index ``_thread_to_session`` that routes an inbound Slack reply back to its session; every other
channel stores a ``ChannelLink`` under ``mirror``, and ``mirror_accepts_inbound`` marks one that
resumes the session. This module holds the writers and readers of both, the compare-and-clear
unlinks that name a binding by its token, the per-delivery mutes, the per-binding nonces and
:class:`ConversationOwnershipConflict`, the refusal a session gets for claiming a conversation
another session already holds while either of them resumes through it. Announcing a committed
or a lost binding (``_note_bind``, ``_note_inbound_unbind``) is defined in
:mod:`kiro_crew.session_map`, beside the listeners it calls. New work on how a session is
linked to a channel goes here.

Each function here whose first parameter is ``self`` is a ``SessionMap`` method: the class
binds it by name, so it reads and writes the map's own state on the instance, and a patch of
the method on the class or on an instance reaches every caller.

Instance fields written: ``_data``, ``_thread_to_session``.

Instance fields read: none.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    binding_token,
    canonical_key,
    is_channel_session_key,
    legacy_dashboard_mirror_key,
)
from kiro_crew.session_map_store import _guarded

if TYPE_CHECKING:
    from kiro_crew.session_map import SessionMap

#: The map's own logger: this code logs under ``kiro_crew.session_map``.
logger = logging.getLogger("kiro_crew.session_map")


class ConversationOwnershipConflict(RuntimeError):
    """Raised when a second session tries to bind a conversation already held.

    One conversation belongs to at most one session. That is not a policy
    preference — it is what makes an inbound reply routable at all. The inbound
    resolver (``find_mirror_sessions(link, inbound_only=True)`` behind
    ``DiscordSessionResume.resumed_session``) refuses to choose between two
    candidates and returns ``None``, and "no owner" and "two owners" land on the
    same ``None``. So a duplicate binding does not send the reply to the wrong
    session, it sends it to NO session: the message silently starts a fresh
    conversation, which is the very bug an inbound binding exists to prevent.

    Enforced on the WRITER, deliberately. Readers stay permissive and keep
    reporting every owner they find, so a map written before this check existed
    still makes the resolver fail closed and still lets in-channel conflict
    detection see the duplicate. Callers translate this into their own surface's
    refusal — an HTTP 409, or the channel's own "run `!unlink` first" — never a
    500 or a generic failure, which would send the user off to retry a command
    that is working exactly as intended.
    """


# Raised to every surface that binds a conversation, and rendered as
# ``module.qualname`` in tracebacks and logged errors: it keeps naming the
# module callers import it from.
ConversationOwnershipConflict.__module__ = "kiro_crew.session_map"


@_guarded
def _rebuild_thread_index(self: SessionMap) -> None:
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

    Built into a FRESH dict and rebound at the end, never cleared in place.
    ``get_session_for_thread`` reads this index without the lock (one keyed
    lookup, the hot path of every inbound Slack reply), so a clear-then-refill
    would give it a window in which the thread it is resolving has no owner —
    and "no owner" is not a delay there, it is a brand-new conversation
    forked off the user's reply. A rebind is atomic under the GIL, so that
    reader sees either the whole old index or the whole new one.
    """
    rebuilt: dict[str, str] = {}
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
        rebuilt[ts] = key
    for ts, key in derived.items():
        rebuilt.setdefault(ts, key)
    self._thread_to_session = rebuilt


def _inbound_binding(entry: dict) -> ChannelLink | None:
    """The inbound resume binding *entry* holds, or None when it holds none.

    The single definition of "losing this strands a conversation", so every
    removal path announces the same thing. An entry flagged inbound whose
    ``mirror`` is missing or unparsable routes nothing already, so it is no loss.
    """
    if not entry.get("mirror_accepts_inbound"):
        return None
    raw = entry.get("mirror")
    if not isinstance(raw, dict):
        return None
    try:
        return ChannelLink.from_dict(raw)
    except (TypeError, ValueError):
        return None


# Per-binding identity, persisted BESIDE each binding (``mirror_nonce`` next to
# ``mirror``, ``slack_link_nonce`` next to the Slack fields). Minted when a
# binding is created or its target changes, kept across an identical rewrite
# (the inbound paths re-write the same coordinates on every turn), dropped
# with the binding. The dashboard's slots row digests it into the row's
# opaque ``binding`` token (``messaging.link.binding_token``), so a
# binding recreated to the SAME target after an unlink never spells the token
# a row drawn from the old one carried: a delayed unlink that names the old
# row is refused instead of deleting the new binding. Not a secret -- it only
# ever leaves as part of a digest -- but random rather than a counter, so a
# deleted-and-recreated entry cannot restart the sequence and collide.
def _new_binding_nonce() -> str:
    return secrets.token_hex(8)


def _is_self_derived(key: str, thread_ts: str) -> bool:
    """True when *key* derives from *thread_ts* itself (``slack:<ts>``).

    Mirrors the predicate in ``_rebuild_thread_index``: a self-derived
    claimant is the fork (or a self-link) and holds a contested thread
    only when nothing else claims it.
    """
    return is_channel_session_key(key) and key.endswith(thread_ts)


@_guarded
def _evict_rival_claimants(self: SessionMap, key: str, thread_ts: str) -> list[str]:
    """Clear the Slack link fields of every OTHER entry claiming *thread_ts*.

    Returns the evicted keys. Only link fields are cleared (entry and
    ``sid`` preserved, mirroring ``clear_slack_link``); the caller owns the
    reverse-index write and the ``_save()``. A self-derived claimant — a
    channel key whose ts IS the thread — never evicts: the load-time
    tie-break in ``_rebuild_thread_index`` awards a contested thread to the
    non-derived session, and stripping that owner's fields here would leave
    the heal with nothing to restore.
    """
    if self._is_self_derived(key, thread_ts):
        return []
    evicted: list[str] = []
    for other_key, other_entry in self._data.items():
        if other_key != key and other_entry.get("slack_thread_ts") == thread_ts:
            other_entry.pop("slack_thread_ts", None)
            other_entry.pop("slack_channel_id", None)
            evicted.append(other_key)
    if evicted:
        logger.info(
            "Slack thread %s reassigned to session %s from %s",
            thread_ts,
            key,
            ", ".join(evicted),
        )
    return evicted


@_guarded
def set_slack_link(self: SessionMap, key: str, thread_ts: str, channel_id: str | None) -> None:
    """Link a session to a Slack thread. Creates entry if needed.

    A thread has at most one owner: a non-derived claim evicts every other
    entry claiming *thread_ts* (see :meth:`_evict_rival_claimants`), and the
    eviction lands in the same ``_save()`` as the new claim so a crash
    between the two cannot persist a two-owner map. A self-derived claim
    never evicts — the load-time tie-break resolves that contest instead.
    An empty *thread_ts* is the clear sentinel and neither evicts anyone
    nor enters the reverse index.

    Establishing a link also drops any ``slack_paused`` marker, so a mute
    never outlives the binding it was set on — otherwise it would re-mute
    the next link, which the user never paused. That drop is scoped to a
    REBIND (different ts or channel). Identical coordinates are the SAME
    binding and keep the mute, because this method is not only called by an
    explicit connect: the Slack inbound path re-writes the same ts/channel on
    every turn as its thread registry (``slack/handler_runtime/turn.py``,
    ``slack/transport_dispatch.py``), so clearing here unconditionally let a
    single inbound message — or a cold start's ``set_channel`` — silently
    un-disconnect a thread the user had muted, and later dashboard turns
    resumed delivering to it. Connecting does not rely on this: the dashboard
    row lifts a mute through :meth:`set_slack_paused`, not by re-linking.
    """
    key = canonical_key(key)
    entry = self._data.get(key)
    evicted = self._evict_rival_claimants(key, thread_ts) if thread_ts else []
    if entry:
        if (
            entry.get("slack_thread_ts") == thread_ts
            and entry.get("slack_channel_id") == channel_id
        ):
            if thread_ts:
                if evicted:
                    self._thread_to_session[thread_ts] = key
                else:
                    self._thread_to_session.setdefault(thread_ts, key)
            # Only the eviction is a mutation on this branch now. The pause
            # is deliberately NOT touched here — see the docstring.
            if evicted:
                self._save()
            return
        # REBIND: the mute belonged to the binding being replaced, so it goes
        # with it rather than carrying onto a thread the user never muted.
        entry.pop("slack_paused", None)
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
        entry = self._data[key]
    # A NEW binding (this branch is only reached when the coordinates changed
    # or the entry is new) gets its own identity; the clear sentinel carries
    # none. See ``_new_binding_nonce``.
    if thread_ts:
        entry["slack_link_nonce"] = self._new_binding_nonce()
    else:
        entry.pop("slack_link_nonce", None)
    if thread_ts:
        # Same policy as the tie-break: a self-derived claim never
        # displaces a live owner from the reverse index — it routes the
        # thread only when nothing else claims it. A non-derived claim
        # (which just swept its rivals) takes the index outright.
        if self._is_self_derived(key, thread_ts):
            self._thread_to_session.setdefault(thread_ts, key)
        else:
            self._thread_to_session[thread_ts] = key
    self._save()
    if thread_ts:
        # A real binding, not the clear sentinel. The identical-coordinates branch
        # above returns before reaching here, so the inbound path re-writing the
        # same thread every turn does not announce: this fires on a binding that
        # CHANGED, which is what the sink records.
        self._note_bind(key)


@_guarded
def get_slack_link(self: SessionMap, key: str) -> tuple[str | None, str | None]:
    """Return (thread_ts, channel_id) for a session."""
    entry = self._data.get(canonical_key(key))
    if not entry:
        return None, None
    return entry.get("slack_thread_ts"), entry.get("slack_channel_id")


@_guarded
def clear_slack_link(self: SessionMap, key: str) -> bool:
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
    entry.pop("slack_link_nonce", None)
    # The mute dies with the binding it muted. A marker left behind would
    # silently re-mute whatever link the user establishes next.
    was_paused = entry.pop("slack_paused", None) is not None
    if had_link or was_paused:
        self._save()
    return had_link


@_guarded
def set_slack_paused(self: SessionMap, key: str, paused: bool) -> bool:
    """Mute (or unmute) a linked Slack thread; return the PREVIOUS state.

    A mute is not an unlink. The thread binding, both coordinate fields and
    the ``_thread_to_session`` reverse index all survive, so a reply in the
    muted thread still resolves to THIS session rather than forking a new
    one; only outbound turn mirroring stops.

    Stored as a presence flag (``True`` or absent, never ``False``) on the
    same entry and under the same key resolution the link accessors use, so
    the flag cannot land on a spelling they do not read. That is what makes
    "a pause never outlives its binding" hold by construction instead of by
    bookkeeping in every caller.
    """
    entry = self._data.get(canonical_key(key))
    if not entry:
        return False
    was_paused = entry.get("slack_paused") is True
    if paused and not was_paused:
        entry["slack_paused"] = True
        self._save()
    elif not paused and was_paused:
        del entry["slack_paused"]
        self._save()
    return was_paused


@_guarded
def is_slack_paused(self: SessionMap, key: str) -> bool:
    """True iff this session's Slack link is muted AND still linked.

    The flag is only meaningful next to a live binding: a marker with no
    link is stale by definition, and reporting it would make an unlinked
    session render as a muted one.
    """
    entry = self._data.get(canonical_key(key))
    if not entry:
        return False
    if not (entry.get("slack_thread_ts") or entry.get("slack_channel_id")):
        return False
    return entry.get("slack_paused") is True


def get_session_for_thread(self: SessionMap, thread_ts: str) -> str | None:
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


@_guarded
def set_mirror_link(
    self: SessionMap,
    key: str,
    link: ChannelLink | None,
    *,
    accepts_inbound: bool = False,
    reason: str = UNBIND_REASON_UNSPECIFIED,
) -> None:
    """Bind (or clear, when *link* is None) a session's mirror target.

    ``accepts_inbound`` marks a non-Slack mirror as a session-resume binding:
    messages arriving from that exact channel location may be routed back to
    *key*. Ordinary dashboard mirrors remain outbound-only. Slack keeps its
    dedicated reverse index and therefore ignores this flag.

    ``reason`` describes the removal this call performs, if any: a None
    *link*, or an overwrite that displaces an inbound binding — rebinding to
    another location, or to the same one as outbound-only, both end a session
    resume as thoroughly as an unlink does.

    The link's ``principal`` -- the peer the writer authorized this location
    for, when it could name one -- is stored inside the ``mirror`` row and
    REPLACED with it: a rewrite that names no peer stores none, so a recorded
    peer never outlives the write that vouched for it. It is not part of the
    binding's identity (see :class:`ChannelLink`), so it neither mints a nonce
    nor decides the occupancy check below. Nor is it trusted on its own by its
    reader: this file is writable by in-sandbox code, so the per-send recipient
    check hands the roster that peer only under a valid ``admission`` -- a MAC
    the gateway alone can mint over the session key and the whole location
    (:mod:`kiro_crew.mirror_admission`). This method NEVER mints one. The two
    paths that authorize a peer for a conversation (the dashboard link handler,
    the resume controller's pick commit) sign the link before handing it here,
    and everything else this method stores -- a rollback, a restore, any generic
    set -- carries the caller's ``admission`` bytes through verbatim: a generic
    writer that signed whatever it was handed would launder a forged row into a
    trusted one the moment any path re-set it. A restore that cannot vouch for a
    row strips the peer first (``mirror_admission.restorable_link``).

    Raises :class:`ConversationOwnershipConflict` when another session already
    holds this exact location AND the conversation is inbound-committed —
    either this claim is inbound-capable, or an occupant already is. See
    :meth:`mirror_claim_blockers`: exclusivity exists to keep inbound routing
    unambiguous, so it is scoped to that, and two outbound-only mirrors on one
    conversation stay as permitted as they were before this rule.
    """
    if link is None:
        self.clear_mirror_link(key, reason=reason)
        return
    if link.channel_type == SLACK_NAMESPACE:
        self.set_slack_link(key, link.thread_id or "", link.channel_id)
        return
    key = canonical_key(key)
    rivals = self.mirror_claim_blockers(key, link, accepts_inbound=accepts_inbound)
    if rivals:
        raise ConversationOwnershipConflict(
            f"{link.channel_type} conversation is already held by "
            f"{len(rivals)} other session(s)"
        )
    entry = self._ensure_entry(key)
    displaced = self._inbound_binding(entry)
    # Verbatim, admission included when the caller carried one: see the
    # docstring for why this writer never mints or repairs an admission.
    stored = link.to_dict()
    # Identity travels with the TARGET: a rewrite of the same coordinates
    # (the dispatcher rebinds a channel-born session's own conversation on
    # every inbound turn) is the same binding and keeps its nonce; a new
    # target, or a binding where none stood, is a new binding. Compared as
    # links, not as stored dicts: ``ChannelLink`` equality is its location,
    # so a rewrite that adds or drops the recorded ``principal`` -- which
    # rides inside ``mirror`` but describes who the location was admitted
    # for, not where it is -- is still the same binding.
    previous = entry.get("mirror")
    same_target = isinstance(previous, dict) and ChannelLink.from_dict(previous) == link
    if not same_target or not entry.get("mirror_nonce"):
        entry["mirror_nonce"] = self._new_binding_nonce()
    entry["mirror"] = stored
    if accepts_inbound:
        entry["mirror_accepts_inbound"] = True
    else:
        entry.pop("mirror_accepts_inbound", None)
    # Binding is how the user reconnects, so it lifts any mute. Same reason
    # as the Slack path: a marker outliving its binding re-mutes the next one.
    entry.pop("mirror_paused", None)
    self._save()
    self._note_bind(key)
    if displaced is not None and (displaced != link or not accepts_inbound):
        self._note_inbound_unbind(key, displaced, reason)


@_guarded
def mirror_claim_blockers(
    self: SessionMap,
    key: str,
    link: ChannelLink,
    *,
    accepts_inbound: bool = False,
) -> list[str]:
    """Sessions that must stop *key* from binding *link*, or ``[]`` if it is free.

    The single definition of "this conversation is taken", shared by the
    atomic check inside :meth:`set_mirror_link` and by the dashboard
    endpoint's precheck. They must ask the same question with the same
    arguments: a backstop whose idea of "occupied" differs from the precheck
    it backs looks like coverage while diverging from it in exactly the cases
    that matter.

    Exclusivity is owed to INBOUND routing, so it is scoped to it. A
    conversation is exclusive when it is inbound-committed — either this claim
    is inbound-capable, or an existing occupant already is. That is precisely
    when a second binding does harm: the inbound resolver refuses to choose
    between two candidates and returns ``None``, and "no owner" and "two
    owners" are the same ``None`` to it, so the reply silently starts a fresh
    session instead of resuming.

    Two outbound-only mirrors on one conversation are left alone. They are
    merely noisy — both write out, nobody reads back — and refusing them
    would reach transports that cannot resume at all, whose in-channel link
    handlers do not translate this refusal because they can never provoke it.

    The occupancy scan itself is UNFILTERED once the conversation is
    inbound-committed: an outbound-only occupant counts. Narrowing that too
    would let an in-channel ``!link`` land a second binding on a conversation
    the dashboard is resuming through, which is the collision that strands the
    reply.

    A rebind by the SAME session is not a rivalry, and three spellings can all
    mean "me": the canonical key, the row the binding actually lives on today
    (a pre-unification ``dashboard:`` row, which only ``_mirror_key`` can
    identify), and any other spelling that canonicalizes to this key. Deriving
    the legacy name unconditionally instead of asking ``_mirror_key`` would
    excuse a row that is NOT this session's binding, letting a genuine
    duplicate persist.

    CONTRACT FOR A NEW TRANSPORT: declaring
    ``TransportCapabilities.supports_session_resume`` makes its conversations
    inbound-committable, and therefore makes this refusal reachable from that
    transport's own in-channel link handler. Translate it there into a
    followable message, as ``discord/transport_dispatch_commands.py`` does — an uncaught
    raise inside a channel handler is a dropped task and a silent no-reply.
    """
    key = canonical_key(key)
    selves = {key, self._mirror_key(key)}
    rivals = [
        other
        for other in self.find_mirror_sessions(link)
        if other not in selves and canonical_key(other) != key
    ]
    if not rivals:
        return []
    if accepts_inbound or any(
        (self._data.get(other) or {}).get("mirror_accepts_inbound") for other in rivals
    ):
        return rivals
    return []


@_guarded
def _mirror_key(self: SessionMap, key: str) -> str:
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


@_guarded
def get_mirror_link(self: SessionMap, key: str) -> ChannelLink | None:
    """Return a session's outbound mirror target as a channel-neutral link.

    Reads the explicit ``mirror`` binding first; for a legacy Slack session
    that only carries ``slack_thread_ts`` / ``slack_channel_id`` it
    synthesizes the equivalent Slack ``ChannelLink`` so callers never have
    to special-case Slack. Returns None when the session mirrors nowhere.

    A Slack row that names NO thread is not a mirror and is never synthesized
    into one. ``set_channel`` writes a channel conversation's namespaced bucket
    (``discord:<id>``) into the legacy ``slack_channel_id`` field on the first
    turn, and ``clear_mirror_link`` pops only the ``mirror`` row -- so without
    this filter every unlinked channel session, and every new one on its first
    turn, read back a Slack link nobody chose. An empty ``thread_ts`` is Slack's
    own clear sentinel and never enters the thread index, so nothing can be
    delivered through such a row: it is bookkeeping, and it names no audience.
    Filtered HERE rather than by each reader because the readers cannot all be
    enumerated: ``bind_origin_mirror`` and the owner-DM predicate each carried
    their own copy of the test, and the ``!sessions`` resume, the dashboard's
    link projection and the containment probe read this method too and would
    each have needed one. The thread, not the channel type, is what separates
    a binding from bookkeeping: a real Slack mirror always names its thread.
    """
    entry = self._data.get(self._mirror_key(key))
    if not entry:
        return None
    raw = entry.get("mirror")
    if raw:
        return ChannelLink.from_dict(raw)
    ts = entry.get("slack_thread_ts")
    ch = entry.get("slack_channel_id")
    if ts:
        return ChannelLink(channel_type=SLACK_NAMESPACE, channel_id=ch, thread_id=ts)
    return None


def mirror_accepts_inbound(self: SessionMap, key: str) -> bool:
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


@_guarded
def has_mirror_row(self: SessionMap, key: str) -> bool:
    """Whether an explicit ``mirror`` row is stored under exactly *key*.

    The row :meth:`clear_mirror_links_at` and :meth:`clear_mirror_link` remove,
    read back with none of :meth:`get_mirror_link`'s answers for OTHER bindings:
    no Slack link synthesized from a surviving ``slack_thread_ts``, no fallback
    to the legacy ``dashboard:`` row. For a rollback that owns a clear and asks
    whether the row it removed has been re-filled since -- a newer mirror
    written in between is deliberate state and stays; an absent one is put
    back. Judged on ``get_mirror_link`` instead, a slot that also carries a
    Slack thread link would read as "still bound" and the rollback would skip
    the very restore it promised.
    """
    entry = self._data.get(key)
    if not entry:
        return False
    return isinstance(entry.get("mirror"), dict)


@_guarded
def mirror_link_nonce(self: SessionMap, key: str) -> str:
    """The per-binding nonce of the mirror :meth:`get_mirror_link` returns.

    Resolved exactly as ``get_mirror_link`` resolves the binding, so the two
    always describe the same row: the explicit ``mirror`` carries
    ``mirror_nonce``; a legacy Slack session whose mirror is synthesized from
    the Slack fields carries that link's ``slack_link_nonce``. ``""`` for no
    binding, and for a binding written before nonces existed -- whose token
    then digests the coordinates alone, as it always did.
    """
    entry = self._data.get(self._mirror_key(key))
    if not entry:
        return ""
    if entry.get("mirror"):
        return str(entry.get("mirror_nonce") or "")
    if entry.get("slack_thread_ts"):
        return str(entry.get("slack_link_nonce") or "")
    return ""


@_guarded
def slack_link_nonce(self: SessionMap, key: str) -> str:
    """The per-binding nonce of the Slack thread :meth:`get_slack_link` returns.

    ``""`` for no link and for a link written before nonces existed.
    """
    entry = self._data.get(canonical_key(key))
    if not entry or not entry.get("slack_thread_ts"):
        return ""
    return str(entry.get("slack_link_nonce") or "")


@_guarded
def find_mirror_sessions(
    self: SessionMap,
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


@_guarded
def clear_mirror_links_at(
    self: SessionMap, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
) -> list[str]:
    """Clear EVERY session whose mirror targets an exact non-Slack location.

    The write counterpart of :meth:`find_mirror_sessions`. An in-channel
    unlink means "nothing mirrors into this conversation anymore", and the
    bindings that occupy a location are matched by VALUE there — so a row
    stranded under a key spelling the conversation has stopped using (a rotated
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
    lost: list[tuple[str, ChannelLink]] = []
    for key in self.find_mirror_sessions(link):
        entry = self._data.get(key)
        if entry is None:  # pragma: no cover - keys come from _data itself
            continue
        inbound = self._inbound_binding(entry)
        entry.pop("mirror", None)
        entry.pop("mirror_nonce", None)
        entry.pop("mirror_accepts_inbound", None)
        entry.pop("mirror_paused", None)
        cleared.append(key)
        if inbound is not None:
            lost.append((key, inbound))
    if cleared:
        self._save()
    # A sweep can clear several sessions at one location; each one lost its
    # own way back, so each is audited and announced separately.
    for key, inbound in lost:
        self._note_inbound_unbind(key, inbound, reason)
    return cleared


@_guarded
def clear_mirror_link(
    self: SessionMap, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED
) -> bool:
    """Remove a session's outbound mirror binding; return True iff one existed.

    A non-Slack ``mirror`` field is dropped directly; a Slack binding is
    cleared via ``clear_slack_link`` so its reverse index is evicted too.
    Resolves through :meth:`_mirror_key` so an unlink reaches a binding still
    held under the legacy spelling — otherwise a mirror that reads as live
    could not be turned off.

    Both spellings go in ONE clear. A channel session that rebound from the
    dashboard can hold two rows -- the canonical binding every read prefers
    and the pre-unification ``dashboard:`` row it superseded -- and
    ``_mirror_key`` falls back to the older row the moment the canonical one
    is gone. Clearing the winner alone therefore does not unlink the session:
    the next read answers the OLD target, the dashboard redraws the row it
    just reported removed, and that mirror keeps delivering. Every row that
    held a binding is popped before the single save, so no reader can
    observe the fallback in between.
    """
    canon = canonical_key(key)
    rows = [canon]
    if is_channel_session_key(canon):
        rows.append(legacy_dashboard_mirror_key(canon))
    lost: list[tuple[str, ChannelLink]] = []
    cleared = False
    for row_key in rows:
        entry = self._data.get(row_key)
        if not entry or entry.get("mirror") is None:
            continue
        inbound = self._inbound_binding(entry)
        entry.pop("mirror", None)
        entry.pop("mirror_nonce", None)
        entry.pop("mirror_accepts_inbound", None)
        entry.pop("mirror_paused", None)
        cleared = True
        if inbound is not None:
            lost.append((row_key, inbound))
    if cleared:
        self._save()
        for row_key, inbound in lost:
            self._note_inbound_unbind(row_key, inbound, reason)
        return True
    # No explicit ``mirror`` on either spelling: the only mirror left to clear
    # is the one ``get_mirror_link`` synthesizes from the legacy Slack fields.
    mkey = self._mirror_key(key)
    entry = self._data.get(mkey)
    if entry and (entry.get("slack_thread_ts") or entry.get("slack_channel_id")):
        return self.clear_slack_link(mkey)
    return False


# ── Compare-and-clear: the one way an unlink that NAMES a binding clears it ──
# A route that reads the binding, compares it and then clears it performs two
# steps, and a rebind landing between them -- another thread's `!sessions`
# pick, a rival claim, the dispatcher re-asserting an origin mirror -- is
# cleared by a stale unlink that matched the binding before it. Both steps
# therefore live HERE, under the map's own lock, and the unlink routes call
# nothing else: `test_channel_connect_row.py` enumerates them and asserts it.


@_guarded
def clear_mirror_link_if(
    self: SessionMap,
    key: str,
    channel_type: str,
    token: str,
    *,
    reason: str = UNBIND_REASON_UNSPECIFIED,
) -> bool:
    """Clear *key*'s mirror iff it is exactly the binding ``(channel_type, token)`` names.

    *token* is the row's :func:`~kiro_crew.messaging.link.binding_token`; it
    is recomputed from the binding this map holds and that binding's own
    nonce, and the compare and the clear are one guarded step, so nothing can
    rebind between them. Returns True iff the binding was cleared (the whole
    of :meth:`clear_mirror_link`: both spellings, one save, the unbind
    audit); False means a MISMATCH -- the binding changed under the caller,
    or there is none -- and nothing was touched. No binding matches nothing:
    an unlink naming a row whose binding is already gone is refused, never
    reported as an unlink.
    """
    current = self.get_mirror_link(key)
    if current is None:
        return False
    if (current.channel_type or "").lower() != channel_type:
        return False
    if binding_token(current, self.mirror_link_nonce(key)) != token:
        return False
    return self.clear_mirror_link(key, reason=reason)


@_guarded
def replace_mirror_link_if(
    self: SessionMap, key: str, expected: ChannelLink, link: ChannelLink
) -> bool:
    """Store *link* as *key*'s mirror iff the stored row is still exactly *expected*.

    A repair, not a rebind: the compare (whole row, :meth:`ChannelLink.same_row`,
    on the row stored under the canonical key itself) and the write are one
    guarded step, so a rebind landing between the caller's read and this write
    is kept and False is returned. The row's inbound flag and mute are carried
    over in the same step. Bytes are stored verbatim, as by :meth:`set_mirror_link`.
    """
    canon = canonical_key(key)
    entry = self._data.get(canon)
    if not entry:
        return False
    stored = entry.get("mirror")
    if not isinstance(stored, dict) or not ChannelLink.from_dict(stored).same_row(expected):
        return False
    inbound = entry.get("mirror_accepts_inbound") is True
    paused = entry.get("mirror_paused") is True
    try:
        # On a loop each save is a deferred off-loop flush that sees both
        # mutations; a batch there would write inline on the loop.
        asyncio.get_running_loop()
        batch: AbstractContextManager[None] = nullcontext()
    except RuntimeError:
        # Off a loop each save writes inline, so batch the pair into one write.
        batch = self.batched_save()
    with batch:
        self.set_mirror_link(canon, link, accepts_inbound=inbound)
        if paused:
            entry["mirror_paused"] = True
            self._save()
    return True


@_guarded
def clear_slack_link_if(self: SessionMap, key: str, channel_type: str, token: str) -> bool:
    """Clear *key*'s Slack thread iff it is exactly the binding ``(channel_type, token)`` names.

    The Slack twin of :meth:`clear_mirror_link_if`, on the Slack fields the
    dashboard's row is projected from. On a match the thread is cleared on
    *key* and, for a ``dashboard:``-keyed session, on the bare key too: the
    turn runner copies a dashboard session's link from the bare key onto the
    prefixed one when a turn runs, so a clear that left either spelling would
    see the next turn re-inherit the link (a channel key has no such twin).
    Returns True iff cleared; False is a mismatch and nothing was touched.
    """
    thread_ts, channel_id = self.get_slack_link(key)
    if not thread_ts:
        return False
    current = ChannelLink(SLACK_NAMESPACE, channel_id=channel_id, thread_id=thread_ts)
    if channel_type != SLACK_NAMESPACE:
        return False
    if binding_token(current, self.slack_link_nonce(key)) != token:
        return False
    cleared = self.clear_slack_link(key)
    if key.startswith("dashboard:"):
        cleared = self.clear_slack_link(key[len("dashboard:") :]) or cleared
    return cleared


@_guarded
def set_mirror_paused(self: SessionMap, key: str, paused: bool, *, origin: bool = False) -> bool:
    """Mute (or unmute) a non-Slack delivery for *key*; return the PREVIOUS state.

    Two DISTINCT non-Slack deliveries can exist on one session, so they get
    two distinct flags rather than sharing one:

    * ``mirror_paused`` — the explicit ``mirror`` binding (``origin=False``).
    * ``origin_paused`` — the conversation the session was BORN in
      (``origin=True``), which reaches the dashboard as a separate row
      derived from the legacy namespaced ``slack_channel_id``.

    A session born in Discord that also mirrors to Telegram renders both
    rows, and a single scalar made disconnecting one silently disconnect the
    other. Keying the flag to the row's source is what makes the two
    independent; the caller says which row it is acting on, because only the
    caller knows.
    """
    field = "origin_paused" if origin else "mirror_paused"
    # The ORIGIN flag belongs to the SESSION and is keyed canonically;
    # ``_mirror_key`` is reserved for the MIRROR flag, which belongs to the
    # binding. That distinction is load-bearing, not stylistic: _mirror_key
    # migrates between the canonical row and the legacy ``dashboard:``
    # spelling depending on where a mirror binding currently lives, so writing
    # the origin flag through it strands the pause the moment a canonical
    # mirror is added -- the lookup moves rows, the flag does not, and the
    # conversation the user muted silently resumes delivering.
    entry = self._data.get(canonical_key(key) if origin else self._mirror_key(key))
    if not entry:
        return False
    was_paused = entry.get(field) is True
    if paused and not was_paused:
        entry[field] = True
        self._save()
    elif not paused and was_paused:
        del entry[field]
        self._save()
    return was_paused


@_guarded
def is_mirror_paused(self: SessionMap, key: str, *, origin: bool = False) -> bool:
    """True iff the named non-Slack delivery for *key* is muted.

    A mute is only meaningful next to something that can actually deliver, so
    a flag with nothing behind it reads as not-paused rather than reporting a
    session that mirrors nowhere as merely quiet. What counts as "something"
    differs per flag, which is why the existence check is not shared:

    * ``origin=True`` requires the session to be channel-BORN, and is read
      from the CANONICAL row -- the session's own -- never through
      ``_mirror_key``. That conversation is permanent, so the flag cannot be
      orphaned by its target disappearing; it CAN be orphaned by the lookup
      moving, which is what keying it to the mirror binding would do.
    * ``origin=False`` requires an explicit ``mirror`` dict, and follows the
      binding through ``_mirror_key``.
    """
    canon = canonical_key(key)
    entry = self._data.get(canon if origin else self._mirror_key(key))
    if not entry:
        return False
    if origin:
        if not is_channel_session_key(canon):
            return False
        return entry.get("origin_paused") is True
    if not isinstance(entry.get("mirror"), dict):
        return False
    return entry.get("mirror_paused") is True


def get_link(self: SessionMap, key: str) -> ChannelLink | None:
    """Return the session's OWN inbound-channel link, or None.

    Distinct from the dashboard->Slack *mirror* binding, which stays
    behind ``get/set_slack_link`` (guardrail G3).
    """
    entry = self._data.get(canonical_key(key))
    if not entry:
        return None
    raw = entry.get("link")
    return ChannelLink.from_dict(raw) if raw else None


@_guarded
def set_link(self: SessionMap, key: str, link: ChannelLink) -> None:
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
