"""Event vocabulary for the per-member append-only log.

One log per member, header first, then envelopes. Durable facts only: live
presence (a slot's ``running`` flag, approval prompts) never enters the log and
keeps riding the ``slots`` WebSocket frame.

Envelope (one JSON object per line after the header)::

    {"type": "<event type>", "seq": <int>, "time": <epoch ms>, "data": {...}}

``seq`` is the zero-based position of the event after the header. The writer
assigns it as ``len(events)`` and refuses anything else; the reader treats a
gap inside the committed region as corruption.
"""

from __future__ import annotations

from typing import Any, TypedDict

HEADER_TYPE = "member"
HEADER_VERSION = 1

# ---- durable event types -------------------------------------------------

# Snapshot of the config-derived roster fields, appended whenever the agents
# config for this member is written or found to differ at startup.
# data: {"kiro_agent", "workspace", "memory_store", "model", "source",
#        "starred", "avatar", "changed": [field, ...]}
MEMBER_CONFIG = "member/config"

# The DM slot this member is pinned to.        data: {"slot_key": str}
MEMBER_BINDING = "member/binding"

# The member's standing rules text changed.    data: {"text": str}
MEMBER_RULES = "member/rules"

# A message landed in the member's DM thread.  data: {"ts": float, "preview": str}
MEMBER_MESSAGE = "member/message"

# One participation / routing record (the former activity.jsonl line).
# data: the record as written by ``members.record_activity`` (must include "ts").
ACTIVITY_RECORD = "activity/record"

# A slot this member drives was opened / closed.
# data: {"slot_key": str}  /  {"slot_key": str, "reason": "closed" | "interrupted"}
SLOT_OPENED = "slot/opened"
SLOT_CLOSED = "slot/closed"

# The member's patrol (auto-nudge loop on its DM slot) started / stopped.
# data: {"slot_key": str}  /  {"slot_key": str, "reason": str}
# reason is the loop's stopped_reason, or "interrupted" when synthesised at load.
PATROL_STARTED = "patrol/started"
PATROL_STOPPED = "patrol/stopped"

ALL_EVENT_TYPES = frozenset(
    {
        MEMBER_CONFIG,
        MEMBER_BINDING,
        MEMBER_RULES,
        MEMBER_MESSAGE,
        ACTIVITY_RECORD,
        SLOT_OPENED,
        SLOT_CLOSED,
        PATROL_STARTED,
        PATROL_STOPPED,
    }
)

#: Namespaces the built-in vocabulary owns. A contributor's event type is
#: ``<app>/<name>`` (contribution protocol §2), and an app cannot be named for
#: one of these, so a type in a reserved namespace that is not in
#: :data:`ALL_EVENT_TYPES` is a typo'd built-in rather than a contribution --
#: and a typo must be refused, not written as a foreign event nothing folds.
RESERVED_EVENT_NAMESPACES = frozenset({"member", "activity", "slot", "patrol"})


def is_contributed_event_type(type_: str) -> bool:
    """Whether *type_* is a well-formed contributor event type.

    Syntax only: ``<namespace>/<name>`` with a namespace the built-in vocabulary
    does not own. WHETHER a given app may append it is authority, decided at the
    HTTP boundary against that app's declared ``contributions.events`` -- the log
    is not the place to answer it, and a log written by the gateway on behalf of
    an app that has since been uninstalled must still load.
    """
    if not isinstance(type_, str) or type_.count("/") != 1:
        return False
    namespace, name = type_.split("/", 1)
    if not namespace or not name:
        return False
    return namespace not in RESERVED_EVENT_NAMESPACES


def is_known_event_type(type_: str) -> bool:
    """Whether the log accepts *type_* at all: built-in or contributed."""
    return type_ in ALL_EVENT_TYPES or is_contributed_event_type(type_)


# ---- projection keys -----------------------------------------------------

PROJ_ROSTER = "roster"  # MemberRosterRow minus ``running``
PROJ_ACTIVITY = (
    "activity"  # {"recent": [record...] (newest first, <= 50), "today": int, "week": int}
)
PROJ_WAKE = "wake"  # {"patrol": "armed"|"stopped"|"none", "slot_key"?, "stopped_reason"?, "since"?: epoch ms}
PROJ_DRIVING = "driving"  # {"open": [slot_key...]}

ALL_PROJECTION_KEYS = (PROJ_ROSTER, PROJ_ACTIVITY, PROJ_WAKE, PROJ_DRIVING)

# ---- WebSocket frames ----------------------------------------------------

# Whole projected value. Client rule: higher seq wins, replays and stale frames drop.
WS_MEMBER_PROJECTION = "member_projection"  # {"slug", "key", "value", "seq"}
# Sent once per connection before any member_projection frame.
# Client rule: drop held rows whose seq > lastSeq for that slug.
WS_MEMBERS_SUBSCRIBED = "members_subscribed"  # {"lastSeqs": {slug: int}}


class Event(TypedDict):
    type: str
    seq: int
    time: int
    data: dict[str, Any]
