"""Echo discipline for a QR-linked personal account.

On a linked device the agent sends *as the operator*, so every send comes back
on the event stream with ``from_me=True`` — byte-identical in shape to a
message the operator typed themselves. Prefix markers or content matching
cannot tell them apart reliably (the operator may echo the agent's phrasing;
prefixes leak into the conversation). What works, and what this module
implements, is **sent-ID tracking**:

- :meth:`EchoTracker.remember` records the message ID of every outbound send,
  per conversation, the moment the client's send call returns it.
- :meth:`EchoTracker.is_own_echo` answers "did *we* send this?" for an inbound
  ``from_me`` event. Only a ``from_me`` message whose ID was remembered is an
  echo to drop; a ``from_me`` message we did NOT send is the operator typing
  in their own account — in the self-chat that is the primary command surface.

Entries expire (TTL + LRU cap) because WhatsApp redelivers within minutes, not
days; an unbounded set would leak for the gateway's lifetime. The 20-minute
TTL and 5000-entry cap follow the values proven in OpenClaw's WhatsApp bridge.

Thread-safety: the tracker is confined to the gateway event loop (neonize
delivers events onto the loop that called ``connect()``), so no lock is taken.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable

_DEFAULT_TTL_S = 20 * 60
_DEFAULT_MAX_ENTRIES = 5000


def _key(conversation_jid: str, message_id: str) -> str:
    # Normalized upstream (jids.normalize_jid); composite key keeps an ID
    # collision in one chat from shadowing another chat's message.
    return f"{conversation_jid}\n{message_id}"


class EchoTracker:
    """TTL + LRU cache of message IDs this channel has sent."""

    def __init__(
        self,
        ttl_s: float = _DEFAULT_TTL_S,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock or time.monotonic
        self._sent: OrderedDict[str, float] = OrderedDict()

    def remember(self, conversation_jid: str, message_id: str) -> None:
        """Record an outbound send. Call for EVERY send that yields an ID —
        including sends whose HTTP-level call reported failure but may still
        have gone through (late acceptance), so their echo is still killed."""
        if not message_id:
            return
        now = self._clock()
        key = _key(conversation_jid, message_id)
        self._sent.pop(key, None)
        self._sent[key] = now
        self._evict(now)

    def is_own_echo(self, conversation_jid: str, message_id: str) -> bool:
        """True when this ``from_me`` inbound is one of our own sends.

        Consuming reads keep the entry (WhatsApp can redeliver the same
        message after a reconnect; a one-shot pop would let the redelivery
        through as a phantom operator command).
        """
        now = self._clock()
        self._evict(now)
        key = _key(conversation_jid, message_id)
        stamp = self._sent.get(key)
        if stamp is None:
            return False
        if now - stamp > self._ttl_s:
            self._sent.pop(key, None)
            return False
        return True

    def _evict(self, now: float) -> None:
        while self._sent:
            oldest_key, stamp = next(iter(self._sent.items()))
            if now - stamp > self._ttl_s or len(self._sent) > self._max_entries:
                self._sent.pop(oldest_key, None)
                continue
            break

    def __len__(self) -> int:
        return len(self._sent)
