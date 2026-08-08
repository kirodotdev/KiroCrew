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

logger = logging.getLogger(__name__)

#: Slack ts format: ``"{epoch_seconds}.{microseconds}"`` -- pure digits + one dot.
#: Both runs are BOUNDED. Unbounded ``\d+`` on either side of the dot makes
#: ``fullmatch`` backtrack quadratically on a long all-digits string, and this
#: predicate is reached with keys that originate outside Slack (any caller
#: resolving a session key, including app backends restoring a saved
#: conversation), so the input is not guaranteed to be a real timestamp. A real
#: ts is 10 digits + 6; 20 each leaves an order of magnitude of headroom.
_SLACK_TS_RE = re.compile(r"\d{1,20}\.\d{1,20}")

SLACK_NAMESPACE = "slack"

#: Session-key namespaces owned by a messaging channel, i.e. every prefix a
#: conversation started OUTSIDE the dashboard can carry. Slack keys are
#: ``slack:<thread_ts>``; every other transport uses
#: ``{channel}:{agent}:{chatType}:{user}[:genN]`` (see
#: :func:`build_dm_session_key`), plus the ``unified:`` bucket that
#: ``dm_scope="unified"`` collapses direct DMs into.
#:
#: Deliberately excludes the non-channel namespaces that also contain a colon
#: (``dashboard:``, ``cron:``, ``hook:``, ``subagent:``, ``channel:``) — those
#: are surfaced by their own owners, not by the channel-session reconciler.
#:
#: NOTE: ``autonudge._CHANNEL_KEY_PREFIXES`` is a deliberately NARROWER set —
#: only the transports that support unattended nudge fires. Do not merge them.
CHANNEL_SESSION_NAMESPACES: tuple[str, ...] = (
    SLACK_NAMESPACE,
    "discord",
    "telegram",
    "whatsapp",
    "webex",
    "wecom",
    "teams",
    "weixin",
    "unified",
)

#: Both separators a namespace can be followed by. A live session key uses ``:``;
#: ``ConversationLog.list_sessions()`` reports the persisted FILENAME STEM, where
#: ``history._safe_key`` has folded ``:`` to ``_`` — so a caller reading the
#: session index sees ``slack_1785370133.085469``. Callers must accept both, the
#: same way the dashboard restore path accepts ``dashboard:`` and ``dashboard_``.
_CHANNEL_SESSION_PREFIXES: tuple[str, ...] = tuple(
    f"{ns}{sep}" for ns in CHANNEL_SESSION_NAMESPACES for sep in (":", "_")
)


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
        if key.startswith((f"{ns}:", f"{ns}_")):
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
    # ``channel:`` is a namespace of its own (reply-token-bound sends), distinct
    # from the per-transport namespaces above.
    ("channel", "channel"),
)

#: Exact keys for the two singleton sessions.
_TELEMETRY_EXACT_KEYS: dict[str, str] = {
    "_bg": "background",
    "_hb": "heartbeat",
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
        if key.startswith((f"{prefix}:", f"{prefix}_")):
            return label
    if _TELEMETRY_CHAT_SLOT_RE.match(key):
        return "dashboard"
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
        rest = key[len(prefix):]
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
    """
    cleared = int(sessions.clear_mirror_link(key))
    cleared += int(sessions.clear_mirror_link(legacy_dashboard_mirror_key(key)))
    swept = sessions.clear_mirror_links_at(location)
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
