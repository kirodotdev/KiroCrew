"""Named channel connections — the governed principal behind a chat surface.

A **connection** is one credentialed attachment of a chat app to this instance:
one bot token, one bot identity, one set of allowed senders, one ceiling. A
transport may carry several — ``telegram/ops-bot`` and ``telegram/raymond`` are
two different principals that happen to speak the same protocol.

WHY THIS IS ITS OWN IDENTITY (and not just ``channel_type``): the governance
``channels`` scope, ``MessagingTransport.channel_type``, the session-key surface
segment and the config section name are all the SAME string today
(``ChannelDescriptor``'s contract). That collapse is correct for everything a
transport is, and wrong for everything a *credential* is: two bots on one
transport share a protocol but not a trust level, so a ceiling expressed per
transport cannot say "the ops bot may not write files" without also saying it
about the owner's personal bot.

Two spellings, one concept, deliberately kept apart:

* the **governance item** — ``telegram/ops-bot``, what the ``channels`` ScopedMap
  matches and what a ``bind: {type: connection}`` profile names. Always fully
  qualified, so a rule can never accidentally address a whole transport when it
  meant one bot.
* the **session namespace** — ``telegram`` for the default connection,
  ``telegram.ops-bot`` for a named one. This is the first ``:``-segment of a
  session key, and its shape is FROZEN by keys already on disk: the default
  connection must keep the bare transport name or every existing session
  changes address.

The separators are therefore not interchangeable. ``/`` separates a governance
item because that is the form a policy author writes and the form
:func:`kiro_crew.platform.governance.mcp_title_to_ref` already established for
"a server and one of its tools". ``.`` separates a session namespace because
``:`` is the session-key field delimiter and ``/`` is not safe in the surface
segment that ``sel._infer_source`` and the dashboard slot map both parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: Separator in a governance item / policy pattern: ``telegram/ops-bot``.
ITEM_SEP = "/"

#: Separator in a session-key surface segment: ``telegram.ops-bot``.
NAMESPACE_SEP = "."

#: The connection every transport has when its config names no others. Keeps the
#: bare transport name as its session namespace, so existing session keys and
#: existing ``channels`` policies are unaffected by this module existing.
DEFAULT_CONNECTION = "default"

#: A connection name must survive being embedded in a session key, a governance
#: pattern and a filename. ``:`` would split the session key, ``/`` would split
#: the governance item, ``.`` would split the namespace, and whitespace makes a
#: policy pattern un-authorable. Lowercase alnum plus ``-`` and ``_`` is the
#: intersection that is safe in all three, and matches the profile-name rule in
#: ``governance._PROFILE_NAME_RE``.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConnectionNameError(ValueError):
    """A connection name cannot be expressed safely in all three positions.

    Raised at ENROLLMENT, never at message time: a transport whose configured
    name is unusable does not start (the caller logs and skips it), which is the
    fail-closed direction — an unaddressable connection is one no policy can
    ever constrain, so running it would create a principal outside governance.
    """


@dataclass(frozen=True)
class ConnectionRef:
    """One connection's identity, in every spelling the system needs."""

    transport: str
    name: str = DEFAULT_CONNECTION

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_CONNECTION

    def governance_item(self) -> str:
        """The ``channels``-scope item: always ``<transport>/<name>``.

        Fully qualified even for the default connection. A bare ``telegram``
        remains valid as a POLICY PATTERN (it covers every connection — see
        ``governance._match_channel``), but never as a queried item: querying the
        bare form would make a per-connection rule unreachable for whichever
        connection happened to be the default.
        """
        return f"{self.transport}{ITEM_SEP}{self.name}"

    def session_namespace(self) -> str:
        """The first ``:``-segment of this connection's session keys.

        Today every transport has exactly ONE connection, so this always returns
        the bare transport name and no key on disk carries the dotted form. The
        named spelling is defined here anyway because it is the shape a transport
        that grows a second connection MUST use, and defining it alongside the
        governance item is what forces that transport through the admission gate
        rather than letting it invent its own addressing. Whatever introduces
        named connections also has to teach ``link.channel_namespace_of`` and
        ``sel._infer_source`` this separator — until then a dotted key resolves to
        no connection at all (see :func:`connection_of_session_key`), which is the
        fail-closed direction.
        """
        return self.transport if self.is_default else f"{self.transport}{NAMESPACE_SEP}{self.name}"

    def __str__(self) -> str:
        return self.governance_item()


def validate_connection_name(name: str) -> str:
    """Return *name* if it is safe in a key, a pattern and a path; else raise.

    ``default`` is always accepted (it is this module's own sentinel). Anything
    else must match :data:`_NAME_RE`.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ConnectionNameError("a connection name may not be empty")
    if candidate == DEFAULT_CONNECTION:
        return candidate
    if not _NAME_RE.match(candidate):
        raise ConnectionNameError(
            f"connection name {name!r} must match {_NAME_RE.pattern} — it has to be "
            "safe inside a session key (no ':'), a governance pattern (no '/') and "
            "a session namespace (no '.')"
        )
    return candidate


def make_connection(transport: str, name: str = DEFAULT_CONNECTION) -> ConnectionRef:
    """Build a validated :class:`ConnectionRef`. Raises on an unusable name."""
    tp = (transport or "").strip().lower()
    if not tp or ITEM_SEP in tp or NAMESPACE_SEP in tp or ":" in tp:
        raise ConnectionNameError(f"transport {transport!r} is not a usable channel type")
    return ConnectionRef(transport=tp, name=validate_connection_name(name))


def parse_item(item: str) -> ConnectionRef:
    """Parse a governance item / policy pattern into a ref.

    Accepts both spellings so a caller can hand over whatever it holds:
    ``telegram`` resolves to the DEFAULT connection, ``telegram/ops-bot`` to the
    named one. Splits on the FIRST ``/`` only; a name containing another ``/``
    is rejected by :func:`validate_connection_name` rather than silently
    truncated.
    """
    raw = (item or "").strip()
    transport, sep, name = raw.partition(ITEM_SEP)
    return make_connection(transport, name if sep else DEFAULT_CONNECTION)


def from_session_namespace(namespace: str) -> ConnectionRef:
    """Recover a ref from a session key's surface segment.

    ``telegram`` -> default, ``telegram.ops-bot`` -> named. The inverse of
    :meth:`ConnectionRef.session_namespace`.
    """
    raw = (namespace or "").strip()
    transport, sep, name = raw.partition(NAMESPACE_SEP)
    return make_connection(transport, name if sep else DEFAULT_CONNECTION)


def connection_of_session_key(session_key: str) -> Optional[ConnectionRef]:
    """The connection a channel session key belongs to, or ``None``.

    Returns ``None`` for a key whose surface is not a chat transport — a
    ``cron:``/``dashboard:``/``subagent:`` key is shaped exactly like a default
    connection (``<word>:<rest>``), so the namespace must be checked against the
    real transport roster rather than merely parsed. Without that check every
    local session would resolve to a phantom connection (``cron/default``) and be
    handed to the ``channels`` gate, which governs chat surfaces only.

    Also ``None`` for a channel key whose connection name is unusable, so a
    caller treats it as "not addressable" rather than guessing.
    """
    if not session_key:
        return None
    # Imported here, not at module scope: ``link`` is the heavier module and it is
    # the one that owns the transport roster, so keeping the edge lazy leaves this
    # module importable from anywhere (including ``link``'s own import path).
    from kiro_crew.messaging.link import channel_namespace_of

    transport = channel_namespace_of(session_key)
    if not transport:
        return None
    namespace = session_key.split(":", 1)[0]
    try:
        return from_session_namespace(namespace)
    except ConnectionNameError:
        return None


__all__ = [
    "ITEM_SEP",
    "NAMESPACE_SEP",
    "DEFAULT_CONNECTION",
    "ConnectionNameError",
    "ConnectionRef",
    "validate_connection_name",
    "make_connection",
    "parse_item",
    "from_session_namespace",
    "connection_of_session_key",
]
