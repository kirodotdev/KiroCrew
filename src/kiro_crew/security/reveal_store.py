"""Removed credential values, kept in gateway memory so the owner can reveal them.

When the credential redactor removes a value from an agent reply, the reply is
saved with ``[REDACTED: credential]`` and the value is gone from every saved
copy. An agent can create a secret that exists nowhere else (a token minted
into a store that cannot read it back), so the owner would lose it. This
module keeps each removed value in PROCESS MEMORY ONLY, behind a random handle
the reply's record carries, so the owner can reveal it from the browser.

The rules this module exists to keep (see *Owner reveal* in
``docs/request-for-change/rfc-redaction-explain-and-reveal.md``):

- **Memory only.** Nothing here is written to disk, logged, pickled or put in a
  repr. A gateway restart empties the store, and so does the TTL.
- **Owner browser only.** The one reader is the owner-gated reveal route
  (``dashboard/handlers/redacted_reveal.py``). No MCP tool, agent route,
  session context or tool result may call :func:`reveal`.
- **Bounded.** Entry count, value size and total size are capped; the oldest
  entry goes first.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import NoReturn

#: How long a removed value stays revealable.
TTL_SECONDS = 24 * 60 * 60
#: Most values kept at once; the oldest is dropped first.
MAX_ENTRIES = 2048
#: A longer value is not kept (a PEM key is a few KB).
MAX_VALUE_CHARS = 16 * 1024
#: Total characters kept across every entry.
MAX_TOTAL_CHARS = 4 * 1024 * 1024

#: The handle shape: ``rv_`` and 22 url-safe characters (128 random bits).
REVEAL_ID_RE = re.compile(r"^rv_[A-Za-z0-9_-]{22}$")


def new_handle() -> str:
    """A fresh random handle (128 bits); it names nothing until ``stash_as``."""
    return "rv_" + secrets.token_urlsafe(16)


class _Entry:
    __slots__ = ("expires", "scope", "value")

    def __init__(self, value: str, scope: str, expires: float) -> None:
        self.value = value
        self.scope = scope
        self.expires = expires

    def __repr__(self) -> str:  # never print the value
        return "<reveal entry>"

    def __reduce__(self) -> NoReturn:  # never pickle the value
        raise TypeError("reveal entries cannot be serialized")


class RevealStore:
    """The in-memory store. One per process (:data:`STORE`)."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()
        self._clock = clock

    def __repr__(self) -> str:
        return f"<RevealStore entries={len(self._entries)}>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("the reveal store cannot be serialized")

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total -= len(entry.value)

    def _sweep(self, now: float) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if entry.expires > now:
                break
            self._drop(key)

    def stash(self, value: str, *, scope: str) -> str | None:
        """Keep ``value`` for ``scope`` (a slot key); return its handle, or None."""
        handle = new_handle()
        return handle if self.stash_as(handle, value, scope=scope) else None

    def stash_as(self, handle: str, value: str, *, scope: str) -> bool:
        """Keep ``value`` under a handle :func:`new_handle` minted; False when refused.

        The two steps are split so a caller can put the handle in a record
        first and keep the value only for the record it actually saves.
        """
        if not isinstance(handle, str) or not REVEAL_ID_RE.match(handle):
            return False
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_CHARS:
            return False
        if not isinstance(scope, str) or not scope:
            return False
        with self._lock:
            now = self._clock()
            self._sweep(now)
            while self._entries and (
                len(self._entries) >= MAX_ENTRIES or self._total + len(value) > MAX_TOTAL_CHARS
            ):
                self._drop(next(iter(self._entries)))
            self._drop(handle)
            self._entries[handle] = _Entry(value, scope, now + TTL_SECONDS)
            self._total += len(value)
        return True

    def reveal(self, handle: str, *, scope: str) -> str | None:
        """The value behind ``handle`` when it was kept for ``scope`` and is live."""
        if not isinstance(handle, str) or not REVEAL_ID_RE.match(handle):
            return None
        with self._lock:
            self._sweep(self._clock())
            entry = self._entries.get(handle)
            if entry is None or entry.scope != scope:
                return None
            return entry.value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total = 0

    def __len__(self) -> int:
        return len(self._entries)


STORE = RevealStore()
