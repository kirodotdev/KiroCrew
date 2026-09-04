"""Layer 3 -- namespaced channel linkage.

Session keys are namespaced as ``f"{channel_type}:{conversation_id}"`` so
keys never collide across channels. Legacy native-Slack sessions are keyed
by the bare ``thread_ts``; the helpers here provide the bidirectional
``bare <-> slack:`` shim used by ``SessionMap``.

Stdlib-only; imported by ``session_map`` (no import cycle).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

# ``SLACK_NAMESPACE`` and ``CHANNEL_SESSION_NAMESPACES`` are RE-EXPORTED from
# ``kiro_crew.constants``, which is their canonical home, because the roster has
# readers on both sides of an import cycle. This module is itself stdlib-only, but
# importing a name FROM it executes ``messaging/__init__.py`` first, which pulls in
# ``driver`` -> ``acp`` -> ``hooks``; since ``hooks`` -> ``webhooks`` ->
# ``validation`` is already an edge, a reader like ``validation`` would get a
# partially-initialized ``hooks``. Readers inside ``messaging`` and its dependents
# keep importing from here; readers outside it read ``constants`` directly.
#
# The semantics are documented at the definition. Summary: every session-key prefix
# a conversation started OUTSIDE the dashboard can carry, excluding the non-channel
# namespaces (``dashboard:``, ``cron:``, ``hook:``, ``subagent:``, ``channel:``).
# ``autonudge._CHANNEL_KEY_PREFIXES`` is a SEPARATE hand-kept copy, not a narrower
# one -- both hold the same 11 namespaces today. It answers a different question
# (does this key SHAPE belong to a channel rather than a dashboard slot), so do not
# assume the two have diverged, and do not assume they are kept in step either.
from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES, SLACK_NAMESPACE

logger = logging.getLogger(__name__)

#: Slack ts format: ``"{epoch_seconds}.{microseconds}"`` -- pure digits + one dot.
#: Both runs are BOUNDED. Unbounded ``\d+`` on either side of the dot makes
#: ``fullmatch`` backtrack quadratically on a long all-digits string, and this
#: predicate is reached with keys that originate outside Slack (any caller
#: resolving a session key, including app backends restoring a saved
#: conversation), so the input is not guaranteed to be a real timestamp. A real
#: ts is 10 digits + 6; 20 each leaves an order of magnitude of headroom.
_SLACK_TS_RE = re.compile(r"\d{1,20}\.\d{1,20}")

#: Both separators a namespace can be followed by. A live session key uses ``:``;
#: ``ConversationLog.list_sessions()`` reports the persisted FILENAME STEM, where
#: ``history._safe_key`` has folded ``:`` to ``_`` — so a caller reading the
#: session index sees ``slack_1785370133.085469``. Callers must accept both, the
#: same way the dashboard restore path accepts ``dashboard:`` and ``dashboard_``.
_CHANNEL_SESSION_PREFIXES: tuple[str, ...] = tuple(
    f"{ns}{sep}" for ns in CHANNEL_SESSION_NAMESPACES for sep in (":", "_")
)


def _in_namespace(key: str, ns: str) -> bool:
    """True when *key* sits in namespace *ns*, in either separator spelling.

    Both separators must be accepted: a live session key uses ``:`` while the
    persisted filename stem uses ``_`` (see :data:`_CHANNEL_SESSION_PREFIXES`).
    This is the per-namespace form, for callers that need to know WHICH
    namespace matched; :data:`_CHANNEL_SESSION_PREFIXES` is the flattened
    equivalent for a yes/no over every CHANNEL namespace. Also called with the
    ``_TELEMETRY_LOCAL_PREFIXES`` names, which that tuple deliberately excludes.

    ``sel.py``'s audit-source attributor and ``context._runtime_display_name``
    spell the same pair separately, on a LOWERCASED key over a narrower set and
    with a ``"slack"`` fallback — do not consolidate them onto this helper.
    """
    return key.startswith((f"{ns}:", f"{ns}_"))


def is_channel_session_key(key: str) -> bool:
    """True when *key* is a session started on a messaging channel.

    Accepts both the live ``slack:<ts>`` form and the persisted ``slack_<ts>``
    filename stem (see :data:`_CHANNEL_SESSION_PREFIXES`).

    Used by the dashboard to decide which persisted sessions deserve a chat slot
    of their own. Unlike :func:`kiro_crew.autonudge.is_channel_key` (which
    answers "can this session be nudged?"), this covers EVERY channel transport,
    including the reply-token-bound ones.
    """
    return key.startswith(_CHANNEL_SESSION_PREFIXES)


def channel_namespace_of(key: str) -> str:
    """Return the channel namespace of *key*, or ``""`` if it is not a channel key."""
    for ns in CHANNEL_SESSION_NAMESPACES:
        if _in_namespace(key, ns):
            return ns
    return ""


#: Non-channel session-key prefixes that still deserve their own telemetry label.
#: Kept in sync with the prefixes ``SessionManager`` mints; anything absent here
#: folds into ``"other"`` so an unrecognised key can never mint a metric series.
_TELEMETRY_LOCAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("dashboard", "dashboard"),
    ("cron", "cron"),
    ("subagent", "subagent"),
    ("taskrunner", "taskrunner"),
    ("secretary", "secretary"),
    ("side", "side"),
    ("wf-pool", "workflow_pool"),
    ("wf-author", "workflow_author"),
    # A workflow STAGE's own session (``wf:<run_id>:<n>``, built by
    # ``workflows/agent_exec.py``). Listed after the two ``wf-*`` namespaces
    # above and matched by ``_in_namespace`` on ``wf:``/``wf_`` only, so it
    # cannot absorb them. Without it every workflow turn reads as ``other``,
    # pooled with genuinely unrecognised key shapes — which is the one reading
    # this label set exists to keep separate.
    ("wf", "workflow"),
    # ``channel:`` is a namespace of its own (reply-token-bound sends), distinct
    # from the per-transport namespaces above.
    ("channel", "channel"),
)

#: Exact keys for the two singleton sessions.
_TELEMETRY_EXACT_KEYS: dict[str, str] = {
    "_bg": "background",
    "_hb": "heartbeat",
    # The CLI chat session's fixed key. Present so this function is a strict
    # SUPERSET of the labels ``validation.infer_use_case`` produced: the turn
    # histogram switched to this helper to gain the background surfaces, and
    # losing "cli" in the trade would have renamed an existing series to
    # "other" — a silent break dressed as a widening.
    "cli_chat": "cli",
}

#: A bare dashboard chat-slot key (``chat-12-1785445181``). The token row store
#: persists ``_ChatSlot.key``, which carries no namespace prefix, so the prefix
#: table above cannot see it — without this rule every dashboard turn read back
#: from that store classifies as ``other``. Anchored and digit-bound so an
#: arbitrary key that merely starts with "chat" is not absorbed.
_TELEMETRY_CHAT_SLOT_RE = re.compile(r"^chat-\d+-\d+$")

#: Every value :func:`telemetry_channel_of` can return. Metric attributes must
#: draw from a closed set — an unbounded label (a raw session key) would mint one
#: time series per conversation and blow up the metric store.
TELEMETRY_CHANNELS: frozenset[str] = frozenset(
    list(CHANNEL_SESSION_NAMESPACES)
    + [label for _, label in _TELEMETRY_LOCAL_PREFIXES]
    + list(_TELEMETRY_EXACT_KEYS.values())
    + ["unknown", "other"]
)


def telemetry_channel_of(key: str | None) -> str:
    """Classify *key* into a bounded metric label for the conversation source.

    Answers "who paid this cost" for latency instruments, which otherwise record
    a duration with no way to group it by where the conversation came from.

    Returns a member of :data:`TELEMETRY_CHANNELS`: a transport namespace
    (``telegram``, ``slack``, …) for channel keys, a local label
    (``dashboard``, ``cron``, ``subagent``, …) for the rest, ``"unknown"`` when
    no key is available, and ``"other"`` for a key shape this function does not
    recognise. Never returns the key itself, so cardinality stays bounded no
    matter what a caller passes.
    """
    if not key:
        return "unknown"
    if key in _TELEMETRY_EXACT_KEYS:
        return _TELEMETRY_EXACT_KEYS[key]
    ns = channel_namespace_of(key)
    if ns:
        return ns
    for prefix, label in _TELEMETRY_LOCAL_PREFIXES:
        if _in_namespace(key, prefix):
            return label
    if _TELEMETRY_CHAT_SLOT_RE.match(key):
        return "dashboard"
    # A BARE Slack thread_ts key, via the module's own predicate rather than a
    # fourth spelling of that shape. The rest of the system already treats such a
    # key as Slack (``canonical_key`` namespaces it), so labelling it ``other``
    # here would have contradicted them — and Slack is a live surface, so that
    # would have renamed a real series.
    if is_legacy_slack_key(key):
        return SLACK_NAMESPACE
    return "other"


@dataclass
class ChannelLink:
    """The inbound channel a session belongs to (its OWN channel).

    Distinct from the dashboard->Slack *mirror* binding, which stays behind
    ``SessionMap.get/set_slack_link`` and is NOT modeled here (guardrail G3).
    """

    channel_type: str
    channel_id: str | None = None
    thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChannelLink":
        return cls(
            channel_type=d.get("channel_type", ""),
            channel_id=d.get("channel_id"),
            thread_id=d.get("thread_id"),
        )


def session_key(channel_type: str, conversation_id: str) -> str:
    """Build a namespaced session key, e.g. ``slack:123.456``."""
    return f"{channel_type}:{conversation_id}"


# ── Unbind reasons: why an inbound resume binding was lost ───────────────────
# The audited vocabulary, kept in this leaf module because both sides need it:
# ``SessionMap`` stamps it on the audit event and normalizes to it, and the
# transports that clear a binding pass it. Every clearing call site names one of
# these constants — a bare literal grows a spelling the audit cannot group by, so
# ``UNBIND_REASONS`` below is the closed set.

# No caller named a reason. Its appearance in the trail is itself the finding: it
# names a clearing path that has not been threaded yet.
UNBIND_REASON_UNSPECIFIED = "unspecified"

# The user asked for it in the conversation and got a reply there, so a notice
# would be an echo. The audit still happens; only the announcement is suppressed,
# and the suppressing side is the listener rather than the map.
UNBIND_REASON_USER_UNLINK = "user_unlink"

# The dashboard's mirror-unlink endpoint. The click happens on a surface the bound
# channel cannot see, so this is the reason the notice exists for.
UNBIND_REASON_DASHBOARD_UNLINK = "dashboard_unlink"

# A transport re-binding the conversation it is being read in as its own origin
# mirror, which displaces whatever binding that key held.
UNBIND_REASON_ORIGIN_REBIND = "origin_rebind"

# An explicit, irreversible session teardown; the entry and every binding on it go.
UNBIND_REASON_SESSION_DESTROYED = "session_destroyed"

# A whole-entry delete whose caller named no motive — the shape of the removal
# rather than its cause.
UNBIND_REASON_ENTRY_DELETED = "entry_deleted"

# ``SessionMap.prune`` collected the entry as stale: its native session file is
# gone and the entry held nothing that had to outlive it. Distinct from
# ``entry_deleted`` because nobody asked for this one — it is the map's own
# garbage collection, so a binding appearing under this reason says the STALE
# predicate let a live conversation through rather than that a caller removed it.
UNBIND_REASON_PRUNED_STALE = "pruned_stale"

#: The closed vocabulary. A reason outside this set is normalized to
#: ``unspecified`` at the map's choke point, so it can neither fragment the audit
#: trail nor reach the channel notice's phrasing map as a miss.
UNBIND_REASONS: frozenset[str] = frozenset(
    {
        UNBIND_REASON_UNSPECIFIED,
        UNBIND_REASON_USER_UNLINK,
        UNBIND_REASON_DASHBOARD_UNLINK,
        UNBIND_REASON_ORIGIN_REBIND,
        UNBIND_REASON_SESSION_DESTROYED,
        UNBIND_REASON_ENTRY_DELETED,
        UNBIND_REASON_PRUNED_STALE,
    }
)


# ── Canonical address parsing (RFC §9 rule 4: exactly ONE parser module) ──


@dataclass(frozen=True)
class ParsedSessionKey:
    """A conversational session key decomposed per the RFC §9 grammar.

    ``{surface}:{agent}:{chat_type}:{scope…}[:genN]`` — the first segment is
    the surface and is the routing authority (``ChannelTurn.channel_type``
    MUST equal it; the contract tests pin this). ``scope`` is one or more
    segments carrying the transport's own topology; ``gen`` is the rotating
    generation (0 = bare bucket, no suffix).
    """

    surface: str
    agent: str
    chat_type: str
    scope: tuple[str, ...]
    gen: int = 0

    @property
    def bucket(self) -> str:
        """The durable bucket — the key with any generation suffix removed."""
        parts = [self.surface, self.agent, self.chat_type, *self.scope]
        return ":".join(parts)


_GEN_SUFFIX_RE = re.compile(r"^gen(\d+)$")


def parse_session_key(key: str) -> ParsedSessionKey | None:
    """Parse a canonical conversational key; ``None`` for anything else.

    Deliberately STRICT: only the §9 grammar parses. Legacy shapes — bare
    Slack ``thread_ts``, two-segment ``slack:<ts>``, ``dashboard:`` keys, the
    app-platform ``channel:{id}:{agent}`` prefix — return ``None`` rather than
    a wrong decomposition; they predate the grammar and their migration is
    explicitly out of scope (§9 accepted debts). Callers that must handle
    legacy keys keep using the prefix classifiers above.

    Consumers must treat a ``None`` as "not addressable by grammar", never as
    an error: the dispatch pipeline itself stays address-agnostic and does not
    call this (pinned in ``dispatch.py`` docstrings).
    """
    if not key:
        return None
    segments = key.split(":")
    if len(segments) < 4:
        return None
    surface = segments[0]
    if surface not in CHANNEL_SESSION_NAMESPACES:
        return None
    gen = 0
    tail = segments[-1]
    m = _GEN_SUFFIX_RE.match(tail)
    if m is not None:
        gen = int(m.group(1))
        segments = segments[:-1]
        if len(segments) < 4:
            return None
    if any(not s for s in segments):
        return None  # an empty segment means a malformed key, not an address
    return ParsedSessionKey(
        surface=surface,
        agent=segments[1],
        chat_type=segments[2],
        scope=tuple(segments[3:]),
        gen=gen,
    )


def assert_colon_free(segment: str, *, what: str) -> str:
    """Enforce §9 rule 4 at BUILD time: segments must not contain ``:``.

    A colon inside a segment silently shifts every later segment during
    parsing — the address becomes wrong, not invalid. Builders call this so
    the corruption is impossible to construct rather than detected later.
    """
    if ":" in segment:
        raise ValueError(f"session-key {what} must not contain ':': {segment!r}")
    return segment


def is_legacy_slack_key(key: str) -> bool:
    """True iff ``key`` is a bare Slack ``thread_ts`` (un-namespaced)."""
    return bool(_SLACK_TS_RE.fullmatch(key))


def canonical_key(key: str) -> str:
    """Normalize a legacy bare Slack ``thread_ts`` key to ``slack:<thread>``.

    Non-legacy keys (``dashboard:``, ``channel:``, ``slack:``, ...) pass
    through unchanged.
    """
    if is_legacy_slack_key(key):
        return f"{SLACK_NAMESPACE}:{key}"
    return key


def legacy_key(key: str) -> str | None:
    """Return the bare ``thread_ts`` for a ``slack:<thread>`` key, else None."""
    prefix = f"{SLACK_NAMESPACE}:"
    if key.startswith(prefix):
        rest = key[len(prefix) :]
        if is_legacy_slack_key(rest):
            return rest
    return None


# ── DM session-key model (two-level: stable bucket + rotating generation) ──

#: dmScope values controlling how direct messages map to session buckets.
DM_SCOPE_PER_CHANNEL_PEER = "per-channel-peer"
DM_SCOPE_UNIFIED = "unified"
#: Default isolates by ``(channel, user)`` so the same person on two channels
#: stays separate; ``unified`` opts into one shared bucket per agent.
DEFAULT_DM_SCOPE = DM_SCOPE_PER_CHANNEL_PEER

#: ``direct`` (1:1 DM) is the baseline; ``forum`` keys a Telegram supergroup
#: forum Topic ``(chat_id, thread_id)`` to its own session (Slack-thread style).
CHAT_TYPE_DIRECT = "direct"
CHAT_TYPE_FORUM = "forum"


def build_dm_session_key(
    channel: str,
    agent: str,
    user: str,
    *,
    gen: int = 0,
    dm_scope: str = DEFAULT_DM_SCOPE,
    chat_type: str = CHAT_TYPE_DIRECT,
) -> str:
    """Build a DM session key from a stable bucket + a rotating generation.

    The canonical shape is channel-first, ``{channel}:{agent}:{chatType}:{user}``,
    with an optional ``:gen{N}`` suffix. The bucket (everything before the
    suffix) is durable -- channel links and history hang off it -- while the
    generation rotates on reset (``/new``, idle, daily) to start a fresh
    transcript without discarding the bucket. Generation 0 is the bare bucket
    (no suffix).

    ``dm_scope``:
      * ``per-channel-peer`` (default) -- one bucket per ``(channel, user)``, so
        the same person on Telegram vs WeCom stays isolated.
      * ``unified`` -- direct (1:1) DMs collapse into a single ``unified:{agent}``
        bucket for cross-surface continuity (channel and user drop out of the
        key). Applies ONLY to direct DMs: a forum route (``chat_type ==
        CHAT_TYPE_FORUM``) ALWAYS keeps its full
        ``{channel}:{agent}:{chat_type}:{user}`` bucket regardless of dm_scope,
        so private DM content can never collapse into a shared group Topic.

    An unrecognized ``dm_scope`` falls back to per-channel-peer (safe isolation)
    rather than raising, so a hand-edited config can never crash dispatch.

    The ``agent`` is part of the durable bucket by design: a different agent is a
    different assistant/context, so switching the configured agent intentionally
    starts a fresh session rather than replaying another agent's history. The
    Telegram/WeCom DM channels carry no prior persisted history to migrate, so
    this key shape applies to them directly; the legacy bare-thread Slack keys
    keep their compatibility shim (see ``canonical_key``) untouched.
    """
    if dm_scope == DM_SCOPE_UNIFIED and chat_type == CHAT_TYPE_DIRECT:
        bucket = f"{DM_SCOPE_UNIFIED}:{assert_colon_free(agent, what='agent')}"
    else:
        # ``user`` is a SCOPE PATH, not a single segment: telegram forum routes
        # pass "{chat_id}:{thread}" here, which §9 rule 2 blesses as two scope
        # segments (hierarchy depth lives in the scope). So the colon-free rule
        # applies to its SUB-segments (none may be empty), not to the whole.
        if ":" in user and any(not s for s in user.split(":")):
            raise ValueError(f"session-key scope path has an empty segment: {user!r}")
        bucket = ":".join(
            (
                assert_colon_free(channel, what="channel"),
                assert_colon_free(agent, what="agent"),
                assert_colon_free(chat_type, what="chat_type"),
                user,
            )
        )
    return f"{bucket}:gen{gen}" if gen else bucket


def legacy_dashboard_mirror_key(channel_session_key: str) -> str:
    """The pre-unification key a channel conversation's mirror link was stored under.

    A channel conversation's dashboard turns now run under the channel session
    key itself, so that key is where its mirror binding belongs and where the
    turn path reads it back. Bindings created before that unification live on
    ``"dashboard:" + history._safe_key(channel_session_key)`` — the runtime key
    of the derived slot that used to own the conversation.

    Retained for compat only: reads and clears fall back to this spelling
    (``SessionMap._mirror_key``) so a link a user set earlier still resolves,
    and the in-channel ``/link`` / ``/unlink`` handlers clear it so a stale row
    cannot outlive a rebind. Never write a new binding here.
    """
    from kiro_crew.history import _safe_key

    return "dashboard:" + _safe_key(channel_session_key)


def release_conversation_location(
    sessions: Any,
    *,
    key: str,
    location: ChannelLink,
    channel: str,
) -> tuple[str, list[str]]:
    """Free a conversation's mirror LOCATION and shape the unlink reply.

    The in-channel unlink shared by the DM dispatchers. Key-addressed clears
    only reach rows spelled with the CURRENT session key, but the bindings
    that block a session resume at this conversation are matched by location
    value — a mirror row stranded under a rotated DM generation, or a
    dashboard session mirroring into the conversation, occupies the location
    while being unreachable by any spelling of *key*. Unlink means "nothing
    mirrors into this conversation": clear the conversation's own binding
    (current + legacy spelling), then sweep every binding targeting the exact
    *location*, so ✅ is only reported when the conversation is actually free.

    A swept key can belong to ANOTHER (dashboard) session — a cross-session
    write triggered by a one-word channel command — so the sweep is INFO-logged
    and the reply reports the count when more than one binding fell, rather
    than a bare ✅ that reads as "just yours".

    Returns ``(reply_text, swept_keys)``. A non-empty sweep is the caller's
    cue to refresh any dashboard projection of the cleared bindings.

    The three clears are ONE critical section and one whole-map write: they are
    one user-visible action ("nothing mirrors here anymore"), and each of them
    would otherwise rewrite the entire map and be individually interruptible, so
    a failure or a concurrent writer partway through could leave the location
    half-freed while the reply already claimed ✅. Nesting is counted, so a
    caller that batches a wider sequence around this one (Telegram pairs it with
    the opt-out write) still gets a single write.
    """
    with sessions.batched_save():
        cleared = int(sessions.clear_mirror_link(key, reason=UNBIND_REASON_USER_UNLINK))
        cleared += int(
            sessions.clear_mirror_link(
                legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_USER_UNLINK
            )
        )
        swept = sessions.clear_mirror_links_at(location, reason=UNBIND_REASON_USER_UNLINK)
    if swept:
        logger.info(
            "%s: unlink swept %d mirror binding(s) at this conversation: %s",
            channel,
            len(swept),
            ", ".join(swept),
        )
    cleared += len(swept)
    if cleared > 1:
        return f"✅ Unlinked ({cleared} bindings).", swept
    if cleared == 1:
        return "✅ Unlinked.", swept
    return "This conversation wasn't linked.", swept


def rebind_conversation_location(
    sessions: Any,
    *,
    key: str,
    location: ChannelLink,
    unlink_command: str,
) -> str:
    """Re-bind a conversation as its own mirror LOCATION and shape the link reply.

    The in-channel ``/link``, and the exact counterpart of
    :func:`release_conversation_location`: that one frees the location and returns
    the unlink reply, this one claims it and returns the link reply. Mirroring is
    automatic (:func:`bind_origin_mirror` re-asserts it on every turn), so this is
    the WITHDRAWAL of a previous unlink rather than the only way to turn it on --
    which makes clearing the opt-out the load-bearing half, since rebinding
    without it is undone by the next automatic bind check.

    *location* must be the channel's single definition of "this conversation", the
    same value handed to :func:`bind_origin_mirror` and
    :func:`release_conversation_location`, because the release matches an occupied
    location by VALUE. *unlink_command* is how the channel spells its own unlink
    in chat (``/unlink``, ``` `!unlink` ```), the only per-channel part of the
    reply.

    One write for the whole sequence: each of these mutations would otherwise
    rewrite the entire session map, stalling the event loop three times for what
    is one user-visible action.

    **The claim goes FIRST inside the batch.** ``batched_save`` writes on the way
    out even when the block raises, so a refusal raised after the opt-out
    withdrawal would PERSIST that withdrawal for a link that never happened --
    silently turning mirroring back on. ``set_mirror_link`` refuses before it
    mutates anything, so ordering it first leaves the batch clean and nothing is
    written.

    Raises ``ConversationOwnershipConflict`` (by that type, from
    ``session_map``) when an inbound-committed occupant holds the location. It is
    deliberately NOT caught here: a channel whose transport declares
    ``supports_session_resume`` has a conversation-specific instruction to give
    the user, and a channel that cannot reach the state should not carry a
    handler for it.
    """
    with sessions.batched_save():
        sessions.set_mirror_link(key, location, reason=UNBIND_REASON_ORIGIN_REBIND)
        sessions.set_mirror_opt_out(key, False)
        # Drop any pre-unification row so a stale binding cannot outlive the
        # rebind (reads prefer the channel key, but a leftover row would still
        # answer a clear).
        sessions.clear_mirror_link(
            legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_ORIGIN_REBIND
        )
    return (
        "✅ Linked. Replies from the dashboard for this conversation will also "
        f"show up here. Send {unlink_command} to stop."
    )


def _is_unrouted_slack_placeholder(link: ChannelLink) -> bool:
    """True for a Slack link that names no thread, i.e. one nobody chose.

    ``set_channel`` writes the conversation's namespaced bucket
    (``discord:<id>``) into the legacy ``slack_channel_id`` field, and
    :meth:`SessionMap.get_mirror_link` synthesizes a Slack ``ChannelLink`` from
    that field whenever no explicit ``mirror`` row exists — so the first turn of
    a new channel session reads back a Slack link it never asked for.

    A threadless Slack row is not a routable mirror: an empty ``thread_ts`` is
    Slack's own clear sentinel and never enters ``_thread_to_session``, so
    nothing can be delivered through it. A real Slack mirror always names its
    thread, which is why the thread — not the channel type — is what separates
    bookkeeping from a binding.
    """
    return link.channel_type == SLACK_NAMESPACE and not link.thread_id


def bind_origin_mirror(sessions: Any, *, key: str, location: ChannelLink) -> bool:
    """Bind the conversation a session is being READ in as its own outbound mirror.

    The in-channel counterpart of :func:`release_conversation_location`, shared by
    the DM dispatchers and called from the inbound turn path. A channel
    conversation IS its own mirror: the person reading the chat is the audience
    for every turn of that session, including the turns they later take from the
    dashboard. Slack has always stamped its own thread on every inbound turn and
    ``SessionMap.get_mirror_link`` synthesizes a mirror from that binding, so a
    dashboard turn on a Slack conversation has always reached Slack. A channel
    that writes the binding only from its explicit link command reaches nobody:
    ``_resolve_mirror_target`` finds no link for that channel and the chat sits
    there looking dead while the conversation continues elsewhere.

    Re-asserted on EVERY turn rather than only on a new session, because the
    binding is what a restart-cold session, an unlink at this location by another
    session, or a rival claim can take away — and only a self-healing bind cannot
    leave a live conversation silently unmirrored. Those all REMOVE a binding;
    none of them repoints one. So ANY binding already present is deliberate and is
    left alone — whichever conversation and whichever CHANNEL it names. The
    dashboard can point a session's mirror at any surface, so a channel
    conversation whose owner aimed it elsewhere keeps that target; overwriting it
    would silently redirect their replies into this chat. The one exception is the
    unrouted Slack placeholder (:func:`_is_unrouted_slack_placeholder`) — the
    first turn of a new channel session always reads one back, and it is
    bookkeeping surfacing through the synthesis path rather than a choice.

    Honours the persisted opt-out the in-channel unlink writes: without it, "off"
    would last exactly until the user's next message, because an entry with no
    binding is indistinguishable from one that was never linked. Declining is ALL
    the opt-out does here — this never clears a binding it finds, so an explicit
    dashboard link to a different target survives it.

    *location* is the single definition of "this conversation", carrying whatever
    thread/topic scoping the channel needs; the same value must be handed to
    :func:`release_conversation_location`, which matches an occupied location by
    VALUE, so a second spelling would let the release miss the binding this wrote.

    Skipped entirely when *key* does not identify ONE conversation.
    ``dm_scope="unified"`` collapses every allowed user's direct DMs into a single
    ``unified:{agent}`` bucket — the channel and the user drop out of the key — so
    "the origin conversation" has no single answer, and a mirror bound there would
    deliver one user's dashboard replies into another user's chat. The test reads
    the KEY rather than each channel's config, because the key is what the binding
    hangs off: a config-derived check in each dispatcher could disagree with the
    key actually in use, and one written per channel would have to be got right
    again every time. A forum/thread route keeps its full bucket under any scope,
    so it is unaffected, and the explicit in-channel link stays available — it
    names the conversation the user is actually in.

    Returns True iff a binding was written. Skipping is a no-op, and the steady
    state is a READ: ``SessionMap`` rewrites the whole map per mutation on the
    event loop, so a per-turn write would put that stall on the repeating path.

    Never raises. This runs on the turn path, where an uncaught raise drops the
    turn and answers the user nothing. ``ConversationOwnershipConflict`` is the
    reachable one: a transport declaring
    ``TransportCapabilities.supports_session_resume`` makes its conversations
    inbound-committable, so an inbound-committed occupant refuses this claim. A
    dispatcher that routes such a conversation to its occupant instead skips the
    bind entirely — but its resolver fails CLOSED on duplicate inbound bindings
    (ambiguous routing is denied), and that is precisely the state where the turn
    path reaches this bind and the claim is refused. It is caught by type rather
    than by name because ``session_map`` imports this module, so naming the
    exception here would be an import cycle.
    """
    if channel_namespace_of(key) == DM_SCOPE_UNIFIED:
        return False
    if sessions.mirror_opt_out(key):
        return False
    existing = sessions.get_mirror_link(key)
    if existing is not None and not _is_unrouted_slack_placeholder(existing):
        return False
    try:
        sessions.set_mirror_link(key, location)
    except Exception:
        logger.debug(
            "%s: origin mirror bind skipped for %s", location.channel_type, key, exc_info=True
        )
        return False
    return True


def seed_generation(
    sessions: Any,
    *,
    channel: str,
    agent: str,
    user_id: str,
    dm_scope: str,
    chat_type: str = CHAT_TYPE_DIRECT,
) -> int:
    """Seed a DM ``ConversationState`` generation from the persisted session map.

    The generation counter is in-memory (reset on restart); this returns the
    highest generation already persisted for the conversation's durable bucket
    (the ``gen=0`` key) so ``/new`` (and idle/daily rotation) always advance past
    a stale on-disk generation instead of colliding with and resurrecting it.
    Shared by every DM dispatcher so the restart-safe seeding lives in one place
    rather than being copy-pasted per channel.

    ``chat_type`` selects the bucket namespace (``direct`` for a 1:1 DM,
    ``forum`` for a per-topic session); it defaults to ``direct`` so existing
    callers keep their exact bucket shape.
    """
    bucket = build_dm_session_key(
        channel, agent, user_id, gen=0, dm_scope=dm_scope, chat_type=chat_type
    )
    return sessions.max_generation(bucket)


def should_rotate_generation(
    last_active: float,
    now: float,
    *,
    idle_minutes: int = 0,
    daily_reset_hour: int = -1,
) -> bool:
    """Decide whether an arriving message should rotate the session generation.

    Two opt-in triggers, evaluated against the previous activity timestamp:

      * **idle** -- the gap since ``last_active`` reached ``idle_minutes``
        (``<= 0`` disables it).
      * **daily** -- a local-time ``daily_reset_hour`` boundary (``0``-``23``)
        falls in ``(last_active, now]`` (``< 0`` disables it).

    The first message in a bucket (``last_active <= 0``) never rotates -- there
    is nothing yet to roll over.
    """
    if last_active <= 0:
        return False
    if idle_minutes > 0 and (now - last_active) >= idle_minutes * 60:
        return True
    if 0 <= daily_reset_hour <= 23:
        lt = time.localtime(now)
        midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        boundary = midnight + daily_reset_hour * 3600
        if boundary > now:
            boundary -= 86400
        if last_active < boundary <= now:
            return True
    return False
