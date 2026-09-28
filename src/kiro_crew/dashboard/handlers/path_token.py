"""HMAC path-credential signing shared by the token-gated dashboard channels.

Some dashboard routes cannot carry a bearer header: the credential rides in a
URL an iframe loads, or in a document a browser fetches on its own. Those routes
authenticate on a signed path segment instead, and this module is the one
implementation of that scheme.

**Each channel owns its own secret, and that is load-bearing rather than
incidental.** A `PathTokenSigner` generates an independent per-process key on
construction, so the channels share the ALGORITHM and never the KEY. Amazon's
"Avoid Hard-coded HMAC Key and Function used for URL Redirect Token Generation"
best practice warns specifically against a centralized helper backed by one
well-known shared key: a single leak then forges credentials for every caller
at once. A module-level secret here would be exactly that shape, so there is
none — and a future channel must construct its own signer rather than reach for
a shared instance.

Nothing is persisted. A secret dies with the process, which bounds the lifetime
of every credential it ever signed to that process.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# Long enough that a forgery attempt is hopeless, short enough to stay readable
# in a URL path segment.
_MAC_CHARS = 43

# A real token is ~55 characters. The bound is checked BEFORE any parsing
# because a multi-thousand-digit expiry passes `isdigit()` and then raises past
# the int conversion digit limit, turning a malformed credential into a 500.
_MAX_TOKEN_CHARS = 128


class PathTokenSigner:
    """Mint and verify an expiring credential bound to a request's identity.

    `namespace` separates one channel's messages from another's even in the
    hypothetical case of an identical secret, so a credential minted for one
    route can never verify on a different one.
    """

    def __init__(self, namespace: str, ttl_secs: int) -> None:
        if not namespace:
            raise ValueError("a signer needs a namespace to bind its messages to")
        if ttl_secs <= 0:
            raise ValueError("a credential needs a positive lifetime")
        self._namespace = namespace
        self._ttl_secs = ttl_secs
        # Per-instance, per-process, never written down.
        self._secret = secrets.token_bytes(32)

    def _mac(self, exp: int, parts: tuple[str, ...]) -> str:
        # NUL-joined so no combination of parts can be rearranged into another
        # valid message (a plain concatenation would let "ab" + "c" collide with
        # "a" + "bc").
        msg = "\x00".join((self._namespace, *parts, str(exp))).encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:_MAC_CHARS]

    def mint(self, *parts: str) -> str:
        """Return `<expiry>.<mac>` binding this credential to `parts`."""
        exp = int(time.time()) + self._ttl_secs
        return f"{exp}.{self._mac(exp, parts)}"

    def verify(self, token: str, *parts: str) -> bool:
        """Whether `token` was minted by THIS signer for exactly `parts`.

        Fails closed on every malformed shape rather than raising, so a caller
        can treat the answer as the whole decision.
        """
        if len(token) > _MAX_TOKEN_CHARS:
            return False
        exp_s, _, mac = token.partition(".")
        if not exp_s.isdigit() or not mac:
            return False
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < time.time():
            return False
        return hmac.compare_digest(self._mac(exp, parts), mac)
