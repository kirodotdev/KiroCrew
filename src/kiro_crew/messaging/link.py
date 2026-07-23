"""Layer 3 -- namespaced channel linkage.

Session keys are namespaced as ``f"{channel_type}:{conversation_id}"`` so
keys never collide across channels. Legacy native-Slack sessions were keyed
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
_SLACK_TS_RE = re.compile(r"\d+\.\d+")

SLACK_NAMESPACE = "slack"


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
    starts a fresh session rather than replaying another agent's history. This
    key shape is new for the recently added DM channels (Telegram, WeCom), which
    carry no prior persisted history to migrate; the legacy bare-thread Slack
    keys keep their existing compatibility shim (see ``canonical_key``) untouched.
    """
    if dm_scope == DM_SCOPE_UNIFIED and chat_type == CHAT_TYPE_DIRECT:
        bucket = f"{DM_SCOPE_UNIFIED}:{agent}"
    else:
        bucket = f"{channel}:{agent}:{chat_type}:{user}"
    return f"{bucket}:gen{gen}" if gen else bucket


def dashboard_mirror_key(channel_session_key: str) -> str:
    """The dashboard-side session key that mirrors a channel conversation.

    A channel session (e.g. ``telegram:kirocrew:direct:123:gen3``) is surfaced
    in the dashboard as a slot whose name is sanitized by ``history._safe_key``
    (``re.sub(r"[^\\w\\-.]", "_", key)`` — every non-word char, not only ``:``);
    that slot's runtime session key is ``dashboard:<slot>`` (the shape produced
    by ``dashboard.chat_utils._history_key_for``). A cross-surface mirror link
    set by an in-channel ``/link`` must be stored on THIS exact key so the
    dashboard turn loop's ``_deliver_cross_surface_*`` helpers read it back.
    Using the same ``_safe_key`` sanitizer is required for correctness: a channel
    key with any non-word char (an agent name with a space, or unicode) would
    otherwise sanitize differently here than in the slot path and silently
    mismatch, so the mirror never fires despite ``/link`` reporting success.
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
