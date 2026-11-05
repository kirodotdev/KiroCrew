"""Where a delivered cross-session row's envelope lines sit, as positions.

``session_send`` prepends ``[sent by session <key> via <verb>]`` and a blank
line to the text it delivers: the receiving AGENT reads who sent it from that
line. A person reading the transcript sees the sender on the session-message
card instead, so the dashboard drops those characters from the bubble.

The gateway wrote the envelope, so it records where: ``meta.relay.envelope`` is
a list of ``[start, end)`` spans into the row's ``content``. One span on a row
one send wrote; one per send on a row a drain merged from one sender's queued
sends. The dashboard removes exactly those spans and never looks for the text
itself. Offsets count UTF-16 code units, the unit the browser indexes strings
in, so a body holding characters outside the BMP still lines up.
"""

from __future__ import annotations

from typing import Any

#: The ``meta.relay`` key holding the spans.
ENVELOPE_KEY = "envelope"


def utf16_len(text: str) -> int:
    """*text*'s length in UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def leading_span(envelope: str) -> list[list[int]]:
    """The spans of a row that opens with *envelope*."""
    return [[0, utf16_len(envelope)]]


def relay_for(text: str, relays: list[Any], provenance: str) -> dict[str, Any] | None:
    """The ``meta.relay`` of a row whose *text* holds *relays*' sends, in order.

    *provenance* is the envelope format ``session_send`` writes
    (``session_messaging._SEND_PROVENANCE``); each send's envelope is rebuilt
    from its own sender and verb rather than read back from a stored entry, which
    is data once restored from disk. The spans are left out when an envelope is
    not where the format puts it, so such a row shows its text whole. None when a
    relay is malformed.
    """
    if not relays:
        return None
    envelopes: list[str] = []
    for relay in relays:
        sender = relay.get("senderKey") if isinstance(relay, dict) else None
        if not isinstance(sender, str) or not sender:
            return None
        envelopes.append(provenance.format(caller=sender, via=_via(relay)))
    last = relays[-1]
    record: dict[str, Any] = {"senderKey": last["senderKey"], "via": _via(last)}
    spans = envelope_spans(text, envelopes)
    if spans is not None:
        record[ENVELOPE_KEY] = spans
    return record


def _via(relay: dict) -> str:
    via = relay.get("via")
    return via if isinstance(via, str) and via else "session_send"


def envelope_spans(text: str, envelopes: list[str]) -> list[list[int]] | None:
    """The spans of *envelopes* in *text*, in order, or None if one is missing.

    Each envelope is looked up from the end of the one before, so a row merged
    from several sends gets one span per send. A sender cannot plant a second
    envelope in its own body (``session_send`` defangs one), and a body ahead of
    its envelope is never searched twice.
    """
    spans: list[list[int]] = []
    cursor = 0
    for envelope in envelopes:
        if not envelope:
            return None
        start = text.find(envelope, cursor)
        if start < 0:
            return None
        end = start + len(envelope)
        spans.append([utf16_len(text[:start]), utf16_len(text[:end])])
        cursor = end
    return spans or None
