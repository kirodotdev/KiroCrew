"""Layer 3 -- namespaced channel linkage.

Session keys are namespaced as ``f"{channel_type}:{conversation_id}"`` so
keys never collide across channels. Legacy native-Slack sessions are keyed
by the bare ``thread_ts``; the helpers here provide the bidirectional
``bare <-> slack:`` shim used by ``SessionMap``.

Stdlib-only; imported by ``session_map`` (no import cycle).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

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
        bucket = f"{DM_SCOPE_UNIFIED}:{agent}"
    else:
        bucket = f"{channel}:{agent}:{chat_type}:{user}"
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
