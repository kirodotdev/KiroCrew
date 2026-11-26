"""The Crew's own check of the Kiro access token on a relayed request.

The relay verifies a person's Kiro access token, then names that person in the
``user`` field of the frame it sends down the host connection. Taking that field
on trust means trusting every relay task with every Crew's callers. So when a
``request`` or ``ws-upgrade`` frame also carries the token itself
(``accessToken``), the relay client checks it here before the caller is stamped,
and refuses the request when any check fails.

Kiro signs the token, and the relay holds only the public keys, so a relay that
cannot mint a token cannot invent a caller. The checks: ES256 under a key from
the issuer's key set; ``typ`` ``kiro-relay-access+jwt``; ``aud``
``kiro-relay/access``; ``iss``, ``rgn``, ``ep`` and ``kind`` equal to what this
Crew's own host token names (:class:`AccessScope`); ``nbf`` and ``exp`` with 30
seconds of skew; a lifetime of at most 300 seconds; and ``sub`` equal to the
frame's ``user``.

Where the expected values come from. The host token arrives from the lease
service, never from the relay, and is minted by the same Kiro issuer with the same
``iss`` and ``rgn``, for this Crew's endpoint (``ep``) and kind. The client already
reads its ``kind`` and ``sub`` to bind the host connection
(``token_source.host_binding``); :func:`access_scope` reads the rest the same way.
The key set is the issuer's ``/.well-known/jwks.json``, the path relay protocol v1
fixes for every issuer.

The key set is fetched on first use and again only for a key ID it does not
hold, at most once per :data:`KEY_SET_MIN_FETCH_INTERVAL_SECONDS` whatever the
outcome, so a flood of made-up key IDs costs one fetch per interval. Concurrent
lookups share one fetch. A failed fetch keeps the keys already held; a
successful one replaces them.

A token is never logged and never part of a ``repr``; a refusal names only its
reason code.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from kiro_crew.relay_client.protocol import ENDPOINT_ID_RE, USER_ID_RE

logger = logging.getLogger(__name__)

#: The one signature algorithm an access token may use.
ACCESS_TOKEN_ALG = "ES256"
#: JWT ``typ`` of an access token.
ACCESS_TOKEN_TYPE = "kiro-relay-access+jwt"
#: ``aud`` of an access token.
ACCESS_TOKEN_AUDIENCE = "kiro-relay/access"
#: Clock skew allowed on ``nbf`` and ``exp``, in seconds.
CLOCK_SKEW_SECONDS = 30
#: The longest an access token may last, ``exp`` minus ``iat``. The person's access
#: ends with the token, so a longer one is refused rather than trusted.
MAX_TOKEN_LIFETIME_SECONDS = 300
#: A compact token longer than this is refused before it is parsed.
MAX_TOKEN_LENGTH = 4096
#: Where every issuer publishes its key set, relative to ``iss``.
KEY_SET_PATH = "/.well-known/jwks.json"
#: Minimum time between two fetches of one key set, whatever their outcome.
KEY_SET_MIN_FETCH_INTERVAL_SECONDS = 10.0
#: A key set fetch that takes longer than this fails.
KEY_SET_FETCH_TIMEOUT_SECONDS = 5.0
#: A key set response larger than this fails.
KEY_SET_MAX_BYTES = 64 * 1024
#: At most this many keys are read from one key set.
KEY_SET_MAX_KEYS = 32
#: The longest key ID accepted.
MAX_KEY_ID_LENGTH = 256

#: Fetches a key set URL and returns the response body; raises on any failure.
KeySetFetch = Callable[[str], Awaitable[bytes]]


class AccessTokenRefused(Exception):
    """An access token that does not admit its request. ``code`` names the reason only."""

    def __init__(self, code: str) -> None:
        super().__init__(f"the relay's access token was refused ({code})")
        self.code = code


@dataclass(frozen=True)
class AccessScope:
    """What a valid access token for this Crew names: read from the Crew's own host token."""

    issuer: str
    region: str
    endpoint_id: str
    crew_kind: str

    @property
    def key_set_url(self) -> str:
        return self.issuer + KEY_SET_PATH


def _issuer_problem(issuer: object) -> bool:
    """Whether *issuer* cannot name a key set: not a bare ``https://`` origin and path."""
    if not isinstance(issuer, str) or not issuer or len(issuer) > 512 or issuer.endswith("/"):
        return True
    try:
        parts = urlsplit(issuer)
    except ValueError:
        return True
    return (
        parts.scheme != "https"
        or not parts.hostname
        or bool(parts.username or parts.password or parts.query or parts.fragment)
    )


def access_scope(host_token_claims: Dict[str, Any]) -> Optional[AccessScope]:
    """The scope a host token's (unverified) claims name, or ``None`` when they name none.

    The relay verifies the host token on every dial; the client only reads it.
    """
    issuer = host_token_claims.get("iss")
    region = host_token_claims.get("rgn")
    endpoint = host_token_claims.get("ep")
    kind = host_token_claims.get("kind")
    if _issuer_problem(issuer):
        return None
    if not isinstance(region, str) or not region or len(region) > 64:
        return None
    if not isinstance(endpoint, str) or not ENDPOINT_ID_RE.match(endpoint):
        return None
    if kind not in ("personal", "team"):
        return None
    assert isinstance(issuer, str)
    return AccessScope(issuer=issuer, region=region, endpoint_id=endpoint, crew_kind=kind)


def _b64url(part: str) -> bytes:
    if not part or "=" in part:
        raise AccessTokenRefused("malformed")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError):
        raise AccessTokenRefused("malformed") from None


def _json_object(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise AccessTokenRefused("malformed") from None
    if not isinstance(value, dict):
        raise AccessTokenRefused("malformed")
    return value


def _is_time(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same(a: str, b: str) -> bool:
    # Bytes, so a non-ASCII claim is an ordinary mismatch, never a TypeError.
    return hmac.compare_digest(
        a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass")
    )


def _public_key(jwk: object) -> Optional[ec.EllipticCurvePublicKey]:
    """A P-256 signing key from one JWK, or ``None`` for any other entry."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return None
    if jwk.get("alg", ACCESS_TOKEN_ALG) != ACCESS_TOKEN_ALG or jwk.get("use", "sig") != "sig":
        return None
    try:
        x = int.from_bytes(_b64url(jwk["x"]), "big")
        y = int.from_bytes(_b64url(jwk["y"]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except (AccessTokenRefused, KeyError, TypeError, ValueError):
        return None


def parse_key_set(raw: bytes) -> Dict[str, ec.EllipticCurvePublicKey]:
    """The P-256 signing keys of a key set document, by key ID. Raises ``ValueError``."""
    if len(raw) > KEY_SET_MAX_BYTES:
        raise ValueError("the key set is too large")
    document = json.loads(raw)
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise ValueError("the key set has no keys list")
    found: Dict[str, ec.EllipticCurvePublicKey] = {}
    for jwk in keys[:KEY_SET_MAX_KEYS]:
        kid = jwk.get("kid") if isinstance(jwk, dict) else None
        key = _public_key(jwk)
        if isinstance(kid, str) and 0 < len(kid) <= MAX_KEY_ID_LENGTH and key is not None:
            found[kid] = key
    return found


async def fetch_key_set(url: str) -> bytes:
    """GET *url* over HTTPS with the relay client's proxy rule, capped in time and size."""
    import aiohttp

    # Imported here: connection.py imports the ingress, which imports this module.
    from kiro_crew.relay_client.connection import env_proxy_for

    timeout = aiohttp.ClientTimeout(total=KEY_SET_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as session:
        async with session.get(url, allow_redirects=False, proxy=env_proxy_for(url)) as response:
            if response.status != 200:
                raise ValueError(f"the key set answered {response.status}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(8192):
                total += len(chunk)
                if total > KEY_SET_MAX_BYTES:
                    raise ValueError("the key set is too large")
                chunks.append(chunk)
    return b"".join(chunks)


class KeySet:
    """One issuer's key set, cached, refetched only for a key ID it does not hold."""

    def __init__(
        self,
        url: str,
        *,
        fetch: KeySetFetch = fetch_key_set,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = KEY_SET_MIN_FETCH_INTERVAL_SECONDS,
    ) -> None:
        self.url = url
        self._fetch = fetch
        self._monotonic = monotonic
        self._min_interval = min_interval
        self._keys: Dict[str, ec.EllipticCurvePublicKey] = {}
        self._last_attempt: Optional[float] = None
        self._in_flight: Optional[asyncio.Future[None]] = None
        #: Fetch attempts made, failed or not.
        self.fetches = 0

    async def key(self, kid: str) -> Optional[ec.EllipticCurvePublicKey]:
        """The key named *kid*, fetching the set once more when it is not held."""
        held = self._keys.get(kid)
        if held is not None:
            return held
        if self._in_flight is None:
            last = self._last_attempt
            if last is not None and self._monotonic() - last < self._min_interval:
                return None
            self._in_flight = asyncio.ensure_future(self._refresh())
        await asyncio.shield(self._in_flight)
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        self._last_attempt = self._monotonic()
        self.fetches += 1
        try:
            self._keys = parse_key_set(await self._fetch(self.url))
        except Exception as exc:  # any failure keeps the keys already held
            logger.warning("relay issuer key set fetch failed (%s)", type(exc).__name__)
        finally:
            self._in_flight = None


class AccessTokenVerifier:
    """Checks access tokens against the key set of the issuer an :class:`AccessScope` names."""

    def __init__(
        self,
        *,
        fetch: KeySetFetch = fetch_key_set,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._monotonic = monotonic
        self._key_sets: Dict[str, KeySet] = {}

    def key_set(self, scope: AccessScope) -> KeySet:
        url = scope.key_set_url
        held = self._key_sets.get(url)
        if held is None:
            held = self._key_sets[url] = KeySet(url, fetch=self._fetch, monotonic=self._monotonic)
        return held

    async def verify(self, token: object, *, scope: AccessScope, user: str) -> str:
        """The verified ``sub`` of *token*; raises :class:`AccessTokenRefused` otherwise."""
        if not isinstance(token, str) or not token.isascii() or len(token) > MAX_TOKEN_LENGTH:
            raise AccessTokenRefused("malformed")
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessTokenRefused("malformed")
        header = _json_object(_b64url(parts[0]))
        if header.get("alg") != ACCESS_TOKEN_ALG or "crit" in header:
            raise AccessTokenRefused("header")
        typ = header.get("typ")
        if (
            not isinstance(typ, str)
            or typ.lower().removeprefix("application/") != ACCESS_TOKEN_TYPE
        ):
            raise AccessTokenRefused("type")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KEY_ID_LENGTH:
            raise AccessTokenRefused("header")
        signature = _b64url(parts[2])
        if len(signature) != 64:
            raise AccessTokenRefused("signature")
        key = await self.key_set(scope).key(kid)
        if key is None:
            raise AccessTokenRefused("unknown_key")
        der = encode_dss_signature(
            int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        )
        try:
            key.verify(der, f"{parts[0]}.{parts[1]}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise AccessTokenRefused("signature") from None
        return self._check_claims(_json_object(_b64url(parts[1])), scope=scope, user=user)

    def _check_claims(self, claims: Dict[str, Any], *, scope: AccessScope, user: str) -> str:
        for name, want, code in (
            ("aud", ACCESS_TOKEN_AUDIENCE, "audience"),
            ("iss", scope.issuer, "issuer"),
            ("rgn", scope.region, "region"),
            ("ep", scope.endpoint_id, "endpoint"),
            ("kind", scope.crew_kind, "kind"),
        ):
            value = claims.get(name)
            if not isinstance(value, str) or not _same(value, want):
                raise AccessTokenRefused(code)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not USER_ID_RE.match(sub) or not _same(sub, user):
            raise AccessTokenRefused("subject")
        nbf, exp, iat = claims.get("nbf"), claims.get("exp"), claims.get("iat")
        if not (_is_time(nbf) and _is_time(exp) and _is_time(iat)):
            raise AccessTokenRefused("times")
        assert isinstance(nbf, (int, float)) and isinstance(exp, (int, float))
        assert isinstance(iat, (int, float))
        if exp - iat > MAX_TOKEN_LIFETIME_SECONDS:
            raise AccessTokenRefused("lifetime")
        now = self._clock()
        if now + CLOCK_SKEW_SECONDS < nbf:
            raise AccessTokenRefused("not_yet_valid")
        if now - CLOCK_SKEW_SECONDS >= exp:
            raise AccessTokenRefused("expired")
        return sub
