"""Span attributes: the one allowlist, the correlation ids and their hashed forms.

Every attribute a span may carry is named in :data:`ALLOWED_KEYS`; anything else
is dropped by :func:`clean`, so no call site can put a new key on the wire by
accident. Never on a span: prompt or completion text, tool arguments or output,
file paths, MCP server names or MCP tool names.

**Correlation ids** follow the hook-correlation contract's names with a
``kirocrew.`` prefix, so hook records and spans join on the same values:

- random ids (turn, ACP session, tool call) are carried RAW: they reveal nothing;
- a Crew session key names the conversation, so it is carried as a keyed
  HMAC-SHA256 (:func:`keyed_hash`) whose key comes from the vault
  (``derive_subkey("trace-correlation")``) and is stable per install;
- the raw session key rides under ``kirocrew.local.*`` too, so a local consumer
  holding raw keys can join. Only the local JSONL sink exists today; an
  exporter that sends spans off the host must drop every ``kirocrew.local.*`` key.

**Values.** An id is shape-checked (:data:`_ID_CHARS`) rather than run through the
metrics content scrubber, because a random id is high-entropy by design and the
scrubber's entropy test would blank every tool call id a backend mints. Every
other string goes through ``metrics.schema.validate_attrs``.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping, Union

from kiro_crew.metrics.schema import validate_attrs

AttrValue = Union[str, bool, int, float]

#: Prefix of the keys that must never leave the host.
LOCAL_PREFIX = "kirocrew.local."

#: Correlation ids: carried raw (random) or as a keyed hash (session keys).
ID_KEYS = frozenset(
    {
        "kirocrew.turn_id",
        "kirocrew.acp_session_id",
        "kirocrew.tool_call_id",
        "kirocrew.session_key_hash",
        "session.id",
        "kirocrew.local.session_key",
        "kirocrew.subagent_id",
        "kirocrew.parent_session_key_hash",
        "kirocrew.crew.root",
        "kirocrew.crew.dispatcher",
        "kirocrew.work.conductor_hash",
        "kirocrew.work.item_id",
    }
)

#: Every attribute key a span may carry. Pinned by test_tracing_ids_attrs.py.
ALLOWED_KEYS = ID_KEYS | frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.tool.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "kirocrew.attempt",
        "kirocrew.tool_kind",
        "kirocrew.tool.outcome",
        "kirocrew.stop_reason",
        "kirocrew.chat.aggregate",
        "kirocrew.usage.credits",
        "kirocrew.usage.cost_usd",
        "kirocrew.usage.model_calls",
        "kirocrew.usage.complete",
        "kirocrew.usage.input_tokens_total",
        "kirocrew.usage.output_tokens_total",
        "kirocrew.usage.credits_total",
        "kirocrew.usage.cost_usd_total",
        "kirocrew.usage.turns_total",
        "kirocrew.run.kind",
        "kirocrew.run.continuation",
        "kirocrew.run.status",
        "kirocrew.crew.root_kind",
        "kirocrew.work.round",
        "langfuse.observation.type",
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "error.type",
    }
)

#: What a correlation id may look like: the alphabet backend ids and Crew
#: session keys use, bounded. Anything else is dropped rather than cut.
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:@/+=-")
_ID_MAX_LEN = 256


def _is_id(value: str) -> bool:
    return 1 <= len(value) <= _ID_MAX_LEN and all(c in _ID_CHARS for c in value)


#: Length of a keyed hash in hex (128 bits), the hook-correlation contract's width.
HASH_HEX_LEN = 32


def keyed_hash(key: bytes, value: str) -> str:
    """Keyed HMAC-SHA256 of *value*, first :data:`HASH_HEX_LEN` hex chars."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:HASH_HEX_LEN]


def session_attrs(key: bytes | None, session_key: str, root_session_key: str) -> dict[str, str]:
    """The session correlation set for one span.

    ``session.id`` is the trace ROOT's session (Langfuse treats it as
    trace-level); ``kirocrew.session_key_hash`` is the span's own. Without a key
    (the vault was unreadable) only the local raw form is kept.
    """
    if not session_key:
        return {}
    attrs = {LOCAL_PREFIX + "session_key": session_key}
    if key is not None:
        attrs["kirocrew.session_key_hash"] = keyed_hash(key, session_key)
        attrs["session.id"] = keyed_hash(key, root_session_key or session_key)
    return attrs


def clean(attrs: Mapping[str, object]) -> dict[str, AttrValue]:
    """Keep only allowlisted keys with usable values; never raises.

    ``None`` and empty strings are dropped, so a caller can pass every optional
    field and let absence mean "not reported".
    """
    ids: dict[str, AttrValue] = {}
    rest: dict[str, AttrValue] = {}
    for name, value in attrs.items():
        if name not in ALLOWED_KEYS or value is None or value == "":
            continue
        if name in ID_KEYS:
            if isinstance(value, str) and _is_id(value):
                ids[name] = value
        elif isinstance(value, (str, bool, int, float)):
            rest[name] = value
    try:
        rest = dict(validate_attrs(rest))
    except ValueError:
        rest = {}
    return {**rest, **ids}
