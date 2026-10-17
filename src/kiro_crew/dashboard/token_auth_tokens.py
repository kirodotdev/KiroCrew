"""Signed dashboard session tokens: minting, validating, pinning and revoking them.

HMAC-signed tokens and the claims they carry, the app secrets exchanged for app tokens,
the per-process token state (nonces, peer and IP pins, single-use consumption),
per-session access-cookie revocation, and the revocation-generation bump that ends every
session.

Served through :mod:`kiro_crew.dashboard.token_auth`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.boot_id import current_boot_id
from kiro_crew.dashboard.revocation_gen import (
    bump_revocation_gen,
    current_revocation_gen,
    current_revocation_gen_or_none,
)
from kiro_crew.dashboard.token_secret import _get_secret
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger("kiro_crew.dashboard.token_auth")


# -- Per-session access-cookie revocation -------------------------------------

_REVOKED_NONCES_FILE = "token_revoked_nonces.json"


class RevokedNonceStore:
    """Persisted denylist of explicitly-revoked access-cookie nonces.

    Enables PER-SESSION logout (CWE-613). The access cookie is a self-contained
    HMAC-signed token, so clearing it client-side (``Set-Cookie max_age=0``)
    does NOT stop a saved copy being replayed until its ``session_exp`` (up to
    20h). ``POST /api/auth/logout`` records the caller's access-cookie ``nonce``
    here; :func:`validate_token` (cookie path) then rejects any token whose
    nonce is listed — killing exactly that one session WITHOUT bumping the
    global generation counter (``revoke_all_sessions``), which would log out
    every other user too.

    Persisted to disk (mode ``0600``) so a revoked cookie stays dead across a
    gateway restart — unlike the in-memory link-nonce set in
    :class:`TokenStateManager`, which is restart-cleared and intentionally NOT
    consulted for cookies. Each entry stores the token's own ``session_exp`` as
    an eviction floor: once that passes, the expiry check rejects the token
    anyway, so the record is dropped and the file cannot grow without bound.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[str, float] = {}  # nonce -> session_exp (eviction floor)
        self._state_path = state_path
        self._load()

    def revoke(self, nonce: str, session_exp: float) -> None:
        """Record *nonce* as revoked until *session_exp*, evicting expired entries."""
        now = time.time()
        with self._lock:
            self._revoked[nonce] = session_exp
            # Opportunistic eviction so a stream of logouts cannot grow the file.
            expired = [n for n, exp in self._revoked.items() if exp < now]
            for n in expired:
                self._revoked.pop(n, None)
        self._persist()

    def is_revoked(self, nonce: str) -> bool:
        """Return True if *nonce* is on the denylist and not yet past its floor."""
        now = time.time()
        with self._lock:
            exp = self._revoked.get(nonce)
            if exp is None:
                return False
            if exp < now:
                # Stale entry — the token is already rejected by the expiry
                # check, so drop it lazily (no persist on this hot read path).
                self._revoked.pop(nonce, None)
                return False
            return True

    def clear_all(self) -> None:
        """Wipe all revoked-nonce records (used by tests)."""
        with self._lock:
            self._revoked.clear()
        self._persist()

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read revoked-nonce store; starting empty", exc_info=True)
            return
        now = time.time()
        with self._lock:
            for entry in data.get("revoked_nonces", []):
                if isinstance(entry, dict) and "nonce" in entry and "exp" in entry:
                    try:
                        exp = float(entry["exp"])
                    except (TypeError, ValueError):
                        continue
                    if exp >= now:  # skip already-expired records on load
                        self._revoked[str(entry["nonce"])] = exp

    def _persist(self) -> None:
        if self._state_path is None:
            return
        with self._lock:
            data = {
                "revoked_nonces": [{"nonce": n, "exp": exp} for n, exp in self._revoked.items()]
            }
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
                # Create the temp file EMPTY, lock it down, and only then write
                # the nonces. Writing first would leave the denylist under the
                # parent-inherited DACL for the length of the lockdown call —
                # brief now that it is in-process, but still a window on a file
                # whose contents are security state. O_TRUNC also
                # empties a stale temp file an earlier crash left behind, so the
                # lockdown never applies on top of someone else's contents.
                os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
                try:
                    # restrict_to_owner (fail-loud), NOT a raw chmod: on Windows
                    # os.chmod only toggles the read-only attribute and leaves the
                    # inherited DACL intact, so the nonces stay readable by other
                    # local accounts — and because that chmod SUCCEEDS, the
                    # warning below never fires. Matches token_secret.py and the
                    # app-token secret written further down this module.
                    platform_compat.restrict_to_owner(tmp)
                except OSError:
                    # Security-sensitive state (revoked session nonces). A
                    # lockdown failure must be observable, matching token_secret.py.
                    logger.warning(
                        "could not restrict revoked-nonce store %s to its owner; "
                        "file may be readable by other users",
                        tmp,
                        exc_info=True,
                    )
                tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, self._state_path)
            except OSError:
                logger.warning("could not persist revoked-nonce store", exc_info=True)


_revoked_store_singleton: RevokedNonceStore | None = None
_revoked_store_lock = threading.Lock()


def _get_revoked_store() -> RevokedNonceStore:
    """Return the lazily-initialized revoked-nonce store singleton.

    Lazy (not module-level) so merely importing token_auth never touches the
    filesystem — the CLI imports this module for every subcommand.
    """
    global _revoked_store_singleton
    if _revoked_store_singleton is None:
        with _revoked_store_lock:
            if _revoked_store_singleton is None:
                from kiro_crew.config.loader import config_dir

                _revoked_store_singleton = RevokedNonceStore(
                    state_path=config_dir() / _REVOKED_NONCES_FILE
                )
    return _revoked_store_singleton


class TokenStateManager:
    """Thread-safe manager for token authentication state.

    Encapsulates all mutable token state (nonces, IP bindings, consumption)
    with consistent locking. Uses OrderedDict for O(1) nonce eviction.

    Threading model: This class uses threading.Lock (not asyncio.Lock) because
    token operations are called from both async contexts (aiohttp middleware)
    and sync contexts (CLI commands like `kirocrew token`). The lock hold time
    is minimal (dict operations only), so blocking the event loop is negligible.
    """

    def __init__(self, max_concurrent_nonces: int = 50) -> None:
        self._lock = threading.Lock()
        self._max_nonces = max_concurrent_nonces
        # OrderedDict maintains insertion order for O(1) oldest eviction
        self._nonces: OrderedDict[str, float] = OrderedDict()
        # Observation latches for the Security Posture surface only — never read
        # by an auth decision. See bind_peer() / proxied_pin_observed().
        # _token_pin_key(token) → (peer key, exp, proxied). The key comes from
        # the signed payload rather than the token string, so every string that
        # authenticates as a session finds that session's pin. The peer key is
        # "ip:<addr>" for the default address pin and "ts:node:<login>@<node>" /
        # "ts:login:<login>" for a daemon-verified tailnet peer (RFC §3) —
        # in-memory only, regenerated on restart.
        self._peer_bindings: dict[str, tuple[str, float, bool]] = {}
        self._consumed: dict[str, float] = {}  # token → exp

    def register_nonce(self, nonce: str, expiry: float) -> str | None:
        """Register a nonce with its expiry time, evicting oldest if over limit."""
        with self._lock:
            self._nonces[nonce] = expiry
            self._nonces.move_to_end(nonce)  # Most recent at end
            if len(self._nonces) > self._max_nonces:
                evicted, _ = self._nonces.popitem(last=False)
                return evicted
            return None

    def is_nonce_valid(self, nonce: str) -> tuple[bool, str]:
        """Check if nonce is valid. Returns (valid, reason).

        Deny-by-default: rejects if no nonces registered or nonce not in set.
        Refreshes the nonce's eviction position on each successful check so
        that actively-used sessions are not evicted by newer token grants.
        """
        with self._lock:
            if not self._nonces:
                return False, "no active sessions"
            if nonce not in self._nonces:
                return False, "token superseded"
            self._nonces.move_to_end(nonce)
            return True, ""

    def bind_peer(
        self, token: str, peer_key: str, session_exp: float, proxied: bool = False
    ) -> None:
        """Bind a token to a peer key (``ip:<addr>`` or a ``ts:``-prefixed identity).

        ``proxied`` records that the pin is a same-host proxy's address rather
        than a client identity, which means this binding pins the token to the
        proxy and is therefore shared by every client behind it. A session
        pinned to a daemon-verified tailnet peer key is per-client, NOT shared
        — callers pass ``proxied=False`` for it. It is an observation for the
        Security Posture surface only — it does not change the binding or how
        :meth:`check_peer` compares it.
        """
        key = _token_pin_key(token)
        with self._lock:
            self._peer_bindings[key] = (peer_key, session_exp, proxied)

    def proxied_pin_observed(self, now: float) -> bool | None:
        """Report the pin scope of the sessions that are LIVE at *now*.

        ``None`` = no session is currently pinned, so there is no scope to
        report — which is NOT the same as "pins are effective" and must not be
        rendered as if it were. ``True`` = at least one live session is pinned to
        a same-host proxy's address and is therefore shared by every client
        behind it. ``False`` = live sessions are pinned per client (to a client
        address, or to a daemon-verified tailnet identity).

        Derived from the bindings rather than from a latch, deliberately. A latch
        would outlive the sessions it describes: one tunnelled login would report
        SHARED until the gateway restarted, even after that session expired and
        the user went back to direct access — the same class of stale claim this
        reporting exists to remove.

        Filters on ``exp`` rather than trusting :meth:`evict_expired` to have
        run, so the answer never depends on when eviction last happened.
        """
        with self._lock:
            live_proxied = [p for _, exp, p in self._peer_bindings.values() if exp > now]
            if not live_proxied:
                return None
            return any(live_proxied)

    def check_peer(self, token: str, peer_key: str) -> tuple[bool, str]:
        """Check *token* against its bound peer key (or unbound).

        Returns ``(ok, mismatch_reason)``. The reason names what the STORED pin
        was bound to, because that is what the user must fix: a node-scoped
        tailnet pin that stops matching means the device's identity changed
        (Tailscale re-enroll), and reporting it as "IP mismatch" would send
        them chasing the wrong thing.
        """
        key = _token_pin_key(token)
        with self._lock:
            entry = self._peer_bindings.get(key)
        if entry is None or entry[0] == peer_key:
            return True, ""
        stored = entry[0]
        if stored.startswith("ts:node:"):
            return False, "device identity mismatch"
        if stored.startswith("ts:"):
            return False, "peer identity mismatch"
        return False, "IP mismatch"

    def has_binding(self, token: str) -> bool:
        """Whether *token* currently has a peer binding (live or not)."""
        key = _token_pin_key(token)
        with self._lock:
            return key in self._peer_bindings

    def mark_consumed(self, token: str, session_exp: float) -> None:
        """Mark a token as consumed (used for one-time token patterns)."""
        with self._lock:
            self._consumed[token] = session_exp

    def is_consumed(self, token: str) -> bool:
        """Check if a token has been consumed."""
        with self._lock:
            return token in self._consumed

    def try_consume(self, token: str, session_exp: float) -> bool:
        """Atomically mark token consumed if not already.

        Returns True if this call consumed it, False if already consumed.
        """
        with self._lock:
            if token in self._consumed:
                return False
            self._consumed[token] = session_exp
            return True

    def evict_expired(self, now: float) -> None:
        """Remove all expired entries from all state stores."""
        with self._lock:
            # Evict expired peer bindings
            expired_tokens = [t for t, (_, exp, _p) in self._peer_bindings.items() if exp < now]
            for t in expired_tokens:
                self._peer_bindings.pop(t, None)
            # Evict consumed tokens independently using their own expiry
            expired_consumed = [t for t, exp in self._consumed.items() if exp < now]
            for t in expired_consumed:
                self._consumed.pop(t, None)
            # Evict expired nonces
            expired_nonces = [n for n, exp in self._nonces.items() if exp < now]
            for n in expired_nonces:
                self._nonces.pop(n, None)

    def clear_all(self) -> None:
        """Clear all token state (nonces, peer bindings, consumed tokens)."""
        with self._lock:
            self._nonces.clear()
            self._peer_bindings.clear()
            self._consumed.clear()


# Maximum concurrent valid tokens before oldest is evicted.
# Raised from 5 to 50 so pending Slack challenge links aren't evicted
# by other token minting activity (crons, dashboard links, etc.).
MAX_CONCURRENT_NONCES = 50

# Module-level singleton instance
_state: TokenStateManager = TokenStateManager(max_concurrent_nonces=MAX_CONCURRENT_NONCES)


# Link click window — URL must be opened within this time
LINK_WINDOW_SECS = 300  # 5 minutes
# Maximum session TTL — cookie cannot exceed this
MAX_SESSION_TTL_SECS = 20 * 3600  # 20 hours


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def _token_pin_key(token: str) -> str:
    """The identity a token's peer pin is stored under.

    Derived from the signed payload bytes, not from the token string, because
    the two are not one-to-one: :func:`_b64url_decode` uses the stdlib decoder's
    default ``validate=False``, which discards characters outside the base64
    alphabet, so a cosmetically re-encoded copy of a token is a different string
    carrying byte-identical payload bytes -- and :func:`validate_token` verifies
    the signature over those bytes, so it accepts both as the same session.

    Keyed on the string, such a copy authenticates as the session while missing
    its binding, and :meth:`TokenStateManager.check_peer` treats an absent entry
    as unbound. Keyed on the payload, every string that can authenticate as a
    session resolves to that session's pin, so the pin cannot be shed by
    re-spelling the cookie. The signed payload carries a per-mint nonce and
    ``iat``, so two separate mints never share a key.

    A token whose payload cannot be decoded keeps the raw string as its key. It
    cannot authenticate at all (:func:`validate_token` rejects it as invalid
    encoding), and folding every undecodable string into one shared key would
    alias unrelated tokens onto one another's pins.
    """
    try:
        payload = _b64url_decode(token.split(".", 1)[0])
    except Exception:
        return token
    return hashlib.sha256(payload).hexdigest()


def _sign(payload: bytes) -> str:
    return _b64url_encode(hmac.new(_get_secret(), payload, hashlib.sha256).digest())


def _ct_eq(a: str, b: str) -> bool:
    """Constant-time str equality that answers False, never raises, for non-ASCII.

    ``hmac.compare_digest`` raises ``TypeError`` on a str with a non-ASCII
    character, which turned a forged credential into a 500 instead of a denial.
    """
    return hmac.compare_digest(
        a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass")
    )


def generate_token(
    user_id: str,
    ttl_seconds: int = 3600,
    *,
    app: str = "",
    prompt: str = "",
    peer_key: str = "",
    extra: dict[str, str] | None = None,
    register_nonce: bool = True,
) -> str:
    """Return ``base64url(payload).base64url(signature)``.

    The token carries two expiry times:
    - ``exp``: link click window (5 minutes, clamped to the session TTL so the
      link never authenticates past the session it grants) — URL must be
      opened before this
    - ``session_exp``: cookie session TTL (capped at 20 hours)

    When *app* is provided, the token payload includes ``"app": app`` so
    downstream middleware can extract the verified app identity.

    When *prompt* is provided, it is included in the signed payload so the
    dashboard can auto-submit the user's original Slack message. The prompt
    is covered by the HMAC signature to prevent tampering.

    *extra* adds further string claims to the signed payload — used by the
    Slack challenge-and-redirect flow to carry ``channel``, ``thread_ts`` and
    an existing linked ``session_key`` so the dashboard can reconnect to (or
    auto-link) the correct Slack-linked session instead of always spawning a
    fresh, disconnected one. Reserved keys (sub/exp/session_exp/iat/nonce/app/
    prompt/peer_key) cannot be overridden. ``peer_key`` is a dedicated
    parameter because it is an authorization boundary, not generic metadata.

    Up to ``_MAX_CONCURRENT_NONCES`` tokens can be valid concurrently.
    When the limit is exceeded, the oldest nonce is evicted (O(1) via OrderedDict).
    """
    if peer_key and not (extra and extra.get("require_peer") == "1"):
        raise ValueError("peer_key requires the require_peer claim")
    _evict_expired()
    now = time.time()
    nonce = os.urandom(8).hex()
    session_ttl = min(ttl_seconds, MAX_SESSION_TTL_SECS)

    # register_nonce=False: the token will only ever be validated on the COOKIE
    # path (use_session_exp=True), which does not consult the link-nonce set.
    # Skipping registration keeps the exchanged session token OUT of the bounded
    # (50-slot) set so high-frequency link→session exchanges (self-nudge polling,
    # instance-iframe re-navigation) don't churn/evict pending one-time link
    # nonces (e.g. Slack challenge links). The nonce is still embedded in the
    # payload so the token remains individually revocable (RevokedNonceStore).
    if register_nonce:
        evicted = _state.register_nonce(nonce, now + session_ttl)
        if evicted:
            _sel_fn().log_api_access(
                caller=user_id,
                operation="nonce_evicted",
                outcome="ok",
                source="token_auth",
                resources=f"evicted_nonce={evicted}",
            )

    payload_dict: dict[str, object] = {
        "sub": user_id,
        # The link-click window never extends past the session the link
        # grants. The query-param path validates against ``exp`` alone (the
        # nonce set is membership-only), so an uncapped window would let the
        # raw link keep authenticating requests after ``session_exp`` passed —
        # a token whose session lifetime was capped by its caller's own bounds
        # (the mobile-link and tailnet-QR mints) would outlive the session
        # that authorized it by up to the full window.
        "exp": now + min(LINK_WINDOW_SECS, session_ttl),
        "session_exp": now + session_ttl,
        "iat": now,
        "nonce": nonce,
        # Revocation generation: validate_token rejects a token whose gen is
        # below the current persisted value, so revoke_all_sessions() kills
        # established cookies (not just the per-process nonce store).
        "gen": current_revocation_gen(),
    }
    if app:
        payload_dict["app"] = app
    if prompt:
        payload_dict["prompt"] = prompt
    if peer_key:
        payload_dict["peer_key"] = peer_key
    if extra:
        _reserved = {
            "sub",
            "exp",
            "session_exp",
            "iat",
            "nonce",
            "gen",
            "app",
            "prompt",
            "peer_key",
        }
        for k, v in extra.items():
            if k not in _reserved and isinstance(v, str) and v:
                payload_dict[k] = v
    payload = json.dumps(payload_dict, separators=(",", ":")).encode()
    encoded_payload = _b64url_encode(payload)
    signature = _sign(payload)
    return f"{encoded_payload}.{signature}"


def validate_token(token: str, *, use_session_exp: bool = False) -> tuple[bool, str, str]:
    """Return ``(valid, user_id, reason)``.

    When *use_session_exp* is ``True`` (cookie-based access), validates
    against ``session_exp`` instead of ``exp`` (link click window).
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False, "", "malformed token"
    encoded_payload, sig = parts
    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except Exception:
        return False, "", "invalid encoding"
    expected = _sign(payload_bytes)
    if not _ct_eq(sig, expected):
        return False, "", "invalid signature"
    try:
        data = json.loads(payload_bytes)
    except Exception:
        return False, "", "invalid payload"
    exp_field = "session_exp" if use_session_exp else "exp"
    if time.time() > data.get(exp_field, data.get("exp", 0)):
        return False, "", "token expired"
    # Revocation generation: an explicit revoke_all_sessions() (e.g. kirocrew
    # logout) bumps the persisted counter. A token minted before that — link OR
    # cookie — carries a lower gen and is rejected. This is the ONLY check that
    # invalidates an established cookie (the nonce store is per-process and
    # restart-cleared; the HMAC secret is persisted, not rotated), so it is what
    # makes "revoke all sessions" actually revoke cookie sessions. Refresh-token
    # validation applies the same check, so the counter is authoritative over
    # BOTH cookie types. Tokens minted before this field existed default to
    # gen 0, matching the initial counter. Fail-closed: when the persisted
    # counter cannot be read, the token cannot be proven un-revoked, so it is
    # rejected (the next validation retries the read).
    current_gen = current_revocation_gen_or_none()
    if current_gen is None:
        return False, "", "revocation state unavailable"
    if int(data.get("gen", 0)) < current_gen:
        return False, "", "session revoked"
    # Boot binding: a token minted with a ``boot`` claim is scoped to the
    # gateway PROCESS that issued it, so a restart ends it. This is what makes
    # an opt-in "the phone stays signed in until the gateway restarts" session
    # honest — the alternative, a long wall-clock TTL, keeps working after a
    # restart and after the operator has stopped thinking about that device.
    #
    # CLAIM-GATED on purpose: a token without the claim is not checked at all,
    # so this cannot log out an existing session or change any default. The
    # check is also deliberately unconditional in the other direction — it
    # applies to the LINK path and the COOKIE path alike, because a boot-bound
    # link that survived a restart in someone's history must not be redeemable
    # either.
    token_boot = str(data.get("boot", ""))
    if token_boot and token_boot != current_boot_id():
        return False, "", "session ended at gateway restart"
    # Nonce is a single-use guard for the one-time LINK click only. For an
    # established session cookie (use_session_exp=True), a valid HMAC signature
    # plus an unexpired session_exp is sufficient — requiring the in-memory
    # nonce there would invalidate every live cookie on each gateway restart
    # (the nonce store is per-process), locking users out for no security gain.
    # Cookie revocation is handled by the gen check above, not the nonce.
    token_nonce = data.get("nonce", "")
    if use_session_exp:
        # Per-session logout (CWE-613): POST /api/auth/logout adds THIS cookie's
        # nonce to a persisted server-side denylist. Deny-by-default: a cookie
        # with no nonce cannot be checked against the denylist, so it is
        # rejected outright rather than silently skipping the revocation check.
        # Every token minted by generate_token carries a nonce, so this rejects
        # only malformed/forged cookies — without the nuclear
        # revoke_all_sessions() gen bump that kills every other session. (The
        # in-memory nonce *set* is still not consulted here — that would break
        # all live cookies on restart; only the explicit denylist is.)
        if not token_nonce:
            return False, "", "missing nonce"
        if _get_revoked_store().is_revoked(token_nonce):
            return False, "", "session revoked"
    else:
        valid, reason = _state.is_nonce_valid(token_nonce)
        if not valid:
            return False, "", reason
    return True, data.get("sub", ""), ""


def token_embed_parent_port(token: str) -> int | None:
    """Return the ``embed_parent_port`` claim from a validly-signed token, or None.

    Drives the CSP ``frame-ancestors`` allowlist for the multi-instance embed: the
    parent dashboard's port (the embedding desktop app's ``KIROCREW_PORT``) is
    carried as a signed claim minted at connect time, so the embedded remote can
    authorize exactly that loopback parent origin to frame it — no hardcoded port,
    no wildcard. Verifies HMAC signature + session expiry + revocation gen (a
    forged/revoked token yields None). The single-use link-nonce is intentionally
    NOT required: the claim is read on every framed document load for the life of
    the session, so it is validated on the cookie/session path.
    """
    if not token:
        return None
    valid, _uid, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return None
    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
    except Exception:
        return None
    raw = data.get("embed_parent_port")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def validate_token_with_app(
    token: str, *, use_session_exp: bool = False
) -> tuple[bool, str, str, str]:
    """Return ``(valid, user_id, reason, app_name)``.

    Extends :func:`validate_token` by also extracting the ``app`` field
    from the token payload.  This avoids changing the existing
    ``validate_token`` signature.
    """
    valid, user_id, reason = validate_token(token, use_session_exp=use_session_exp)
    if not valid:
        return False, user_id, reason, ""
    # Extract app from payload
    app_name = ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        app_name = data.get("app", "")
    except Exception:
        pass
    return valid, user_id, reason, app_name


def claims_an_app_unverified(token: str) -> bool:
    """Whether *token* CLAIMS an ``app`` identity, without verifying its signature.

    Used only to REFUSE — never to grant. The cookie fallback in the middleware
    treats an invalid ``?token=`` as absent so a dead link token cannot veto a
    live session cookie, but that must not apply to an APP token: an app token
    and a dashboard-user cookie can legitimately arrive together (an installed
    app's UI is served from this same origin, so the browser attaches the user's
    cookie), and adopting the cookie there would swap an app-scoped identity for
    the user's own and skip ``_enforce_app_scope`` entirely.

    Reading an unverified claim is sound in exactly this direction. The claim can
    only ever make the decision STRICTER, so a forged ``app`` value buys an
    attacker a refusal, not a grant — and an unparseable payload answers ``False``
    because such a token carries no app scope to protect in the first place (it
    is refused on its own merits by ``validate_token`` regardless). It is NEVER
    correct to read this to decide what a caller may reach: use the ``app_name``
    that ``validate_token_with_app`` returns, which is signature-checked.
    """
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("app"))


def requires_verified_peer_unverified(token: str) -> bool:
    """Whether *token* requires a daemon-verified tailnet peer.

    Like :func:`claims_an_app_unverified`, this reads an unverified claim only
    to make an already-validated request STRICTER.  The middleware calls it
    after signature and expiry validation and uses a positive result solely to
    refuse access when Tailscale cannot resolve the forwarded peer.  A payload
    that cannot be decoded is treated conservatively: it must never turn an
    identity-required session into the ordinary token+IP fallback path.
    """
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return True
    return not isinstance(data, dict) or str(data.get("require_peer", "")) == "1"


def required_peer_key_unverified(token: str) -> str:
    """Return an identity-bound token's signed original peer key.

    Read only after normal signature/expiry validation, and used only to make
    the decision stricter.  An empty result is not an unbound wildcard for a
    cookie carrying ``require_peer``: it denotes a legacy session that cannot
    prove its original device after the in-memory pin disappears at restart.
    """
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("peer_key", "")
    return value if isinstance(value, str) else ""


def extract_prompt_from_token(token: str) -> str:
    """Extract the ``prompt`` field from a validated token payload.

    Validates the token first (deny-by-default). Returns the prompt
    string if valid and present, empty string otherwise.
    """
    valid, _user_id, _reason = validate_token(token)
    if not valid:
        return ""
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
        return data.get("prompt", "")
    except Exception as exc:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the decode step and exception type, never the token
        logger.warning(
            "extract_prompt_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return ""


def extract_claims_from_token(token: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Extract selected string claims from a validated token payload.

    Validates the token first (deny-by-default). Returns a dict containing
    only the requested *keys* that are present and string-typed; returns an
    empty dict if the token is invalid. Used by the Slack challenge-redirect
    frontend to recover ``channel``/``thread_ts``/``session_key`` so it can
    reconnect to (or auto-link) the correct Slack-linked session.

    Validates against ``session_exp`` (use_session_exp=True), NOT the 5-minute
    link window: claim recovery happens after the user has clicked through and
    established a session, so binding it to the link ``exp`` would lose the
    thread context (channel/thread_ts/session_key) the moment the click window
    closed, breaking auto-link/reconnect for the rest of the session.
    """
    valid, _user_id, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return {}
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception as exc:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the decode step and exception type, never the token
        logger.warning(
            "extract_claims_from_token: post-validation decode failed (%s)", type(exc).__name__
        )
        return {}
    out: dict[str, str] = {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    return out


def extract_numeric_claim(token: str, key: str) -> float | None:
    """Extract a single numeric (int/float) claim from a validated token.

    ``extract_claims_from_token`` intentionally returns only STRING claims (it
    serves the Slack-redirect channel/thread_ts recovery path), so it silently
    drops numeric claims like ``session_exp``. Callers that need a numeric claim
    (e.g. ``api_auth_me`` reporting the cookie's ``session_exp`` so the frontend
    can schedule its proactive refresh) must use this instead. Validates the
    token first (deny-by-default); returns ``None`` if the token is invalid, the
    claim is absent, or it is not a real number (bool is rejected).

    Validates against ``session_exp`` (use_session_exp=True), NOT the 5-minute
    link window — matching ``extract_claims_from_token``: the cookies this reads
    outlive the link window, survive restarts, and are minted with
    ``register_nonce=False`` by both the middleware link->session exchange and
    ``api_auth_refresh``, so link-path validation would return ``None`` for all
    of them and the fix would be a runtime no-op.
    """
    valid, _user_id, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return None
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception as exc:
        logger.warning(
            "extract_numeric_claim: post-validation decode failed (%s)", type(exc).__name__
        )
        return None
    v = data.get(key)
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def generate_app_secret() -> str:
    """Generate a random 64-char hex secret for app authentication."""
    return os.urandom(32).hex()


def validate_app_secret(app_name: str, provided_secret: str) -> bool:
    """Validate an app secret against the stored secret on disk.

    Reads ``~/.kiro/crew/apps/{app_name}/.app_secret`` and performs
    constant-time comparison via :func:`hmac.compare_digest`.
    Returns ``False`` if the file doesn't exist or doesn't match.
    """
    from kiro_crew.config.loader import config_dir

    secret_path = config_dir() / "apps" / app_name / ".app_secret"
    try:
        stored = secret_path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return False
    if not stored or not provided_secret:
        return False
    return _ct_eq(stored, provided_secret)


def write_app_secret(app_name: str, secret: str) -> None:
    """Write an app secret to ``~/.kiro/crew/apps/{app_name}/.app_secret``.

    Creates the directory if needed and sets file mode to 0o600.
    """
    from kiro_crew.config.loader import config_dir
    from kiro_crew.owner_only_files import mkdirs_owner_only

    secret_dir = config_dir() / "apps" / app_name
    mkdirs_owner_only(secret_dir)  # apps/ and apps/<name>/ are born 0700
    secret_path = secret_dir / ".app_secret"
    # os.O_TRUNC truncates any pre-existing file BEFORE the DACL tightens,
    # then restrict_to_owner locks it down while it is still empty, then we
    # write the secret bytes. This ordering matters on Windows because the
    # lockdown replaces the file's DACL rather than being set at create time —
    # if we wrote first the secret would sit under the parent-inherited DACL
    # until that call landed. On failure we unlink the just-created empty file (mirroring
    # dashboard/server.py:_write_secret_file) so we don't leave a zero-byte
    # .app_secret under the default DACL that a later successful write
    # (which does not re-inherit on O_TRUNC) could then populate.
    fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # restrict_to_owner (fail-loud), NOT fchmod_safe: fchmod_safe swallows
        # OSError, which would defeat the cleanup-and-reraise below for this
        # app secret. On POSIX applies chmod 0o600 by path; on
        # Windows an owner-only DACL (fchmod doesn't exist on
        # Windows, where an IS_POSIX no-op would let per-app secrets
        # land readable by other local users).
        platform_compat.restrict_to_owner(secret_path)
        with os.fdopen(fd, "w") as f:
            fd = -1  # fdopen took ownership; skip the redundant close below
            f.write(secret)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            secret_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _evict_expired() -> None:
    """Remove token state entries whose session has expired."""
    _state.evict_expired(time.time())


def bind_token_peer(
    token: str, peer_key: str, session_exp: float = 0.0, proxied: bool = False
) -> None:
    """Bind a token to a peer key for session validation (RFC §3).

    *peer_key* is ``ip:<addr>`` for the default address pin — byte-for-byte
    today's behaviour for every non-Tailscale path — or ``ts:node:<login>@<node>`` / ``ts:login:<login>``
    for a daemon-verified tailnet peer. ``proxied`` is an observation only (see
    ``TokenStateManager.bind_peer``): it records that the pin is a same-host
    proxy's address and therefore shared, so the Security Posture surface can
    report it. It never changes the binding or the comparison.
    """
    _state.bind_peer(token, peer_key, session_exp or time.time() + MAX_SESSION_TTL_SECS, proxied)


def bind_token_ip(token: str, ip: str, session_exp: float = 0.0, proxied: bool = False) -> None:
    """Thin compat wrapper over :func:`bind_token_peer` for address pins."""
    bind_token_peer(token, f"ip:{ip}", session_exp, proxied)


def proxied_pin_observed() -> bool | None:
    """Report the pin scope of the sessions live right now.

    ``None`` = nothing is currently pinned (no scope to report), ``True`` = at
    least one live session is pinned to a same-host proxy address and is
    therefore shared by every client behind it, ``False`` = live sessions are
    pinned per client (client address or daemon-verified tailnet identity).
    Recovers on its own once proxied sessions expire — no gateway restart
    needed.
    """
    return _state.proxied_pin_observed(time.time())


def check_token_peer(token: str, peer_key: str) -> tuple[bool, str]:
    """Check *token* against its bound peer key. ``(ok, mismatch_reason)``."""
    return _state.check_peer(token, peer_key)


def check_token_ip(token: str, ip: str) -> bool:
    """Thin compat wrapper over :func:`check_token_peer` for address pins."""
    return _state.check_peer(token, f"ip:{ip}")[0]


def mark_consumed(token: str, session_exp: float = 0.0) -> None:
    """Mark a token as consumed."""
    _state.mark_consumed(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def is_consumed(token: str) -> bool:
    """Check if a token has been consumed."""
    return _state.is_consumed(token)


def try_consume(token: str, session_exp: float = 0.0) -> bool:
    """Atomically consume a token if not already consumed.

    Returns True if this call consumed it, False if already consumed.
    """
    return _state.try_consume(token, session_exp or time.time() + MAX_SESSION_TTL_SECS)


def revoke_access_cookie(token: str) -> bool:
    """Revoke a SINGLE access cookie by adding its nonce to the denylist.

    Validates the token (signature + session_exp + gen) first — deny-by-default,
    so attacker-controlled junk is never written into the persisted store. Then
    extracts the per-token ``nonce`` and ``session_exp`` and records the nonce
    as revoked until that expiry. Returns True if a nonce was revoked, False
    otherwise (malformed / already-expired / nonce-less token — nothing to do).

    This is the per-session counterpart to :func:`revoke_all_sessions`: logout
    calls it so the caller's own cookie is rejected immediately, without the
    global generation bump that would terminate every other active session.
    """
    valid, _uid, _reason = validate_token(token, use_session_exp=True)
    if not valid:
        return False
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return False
    nonce = data.get("nonce", "")
    if not nonce:
        return False
    try:
        session_exp = float(data.get("session_exp", 0.0))
    except (TypeError, ValueError):
        return False
    if session_exp <= time.time():
        return False
    _get_revoked_store().revoke(nonce, session_exp)
    return True


def revoke_all_sessions() -> None:
    """Revoke all active dashboard sessions (also used for test isolation).

    Emits a SEL audit event before clearing state so the revocation is recorded.
    Raises ``OSError`` (fail-closed) when the persisted revocation counter
    cannot be read or the bumped value cannot be written — see
    ``bump_revocation_gen``. On failure the generation is unchanged (in memory
    and on disk) so no token is ever minted with an unpersisted generation;
    the in-memory link/IP/consumed state has still been cleared.
    """
    _sel_fn().log_api_access(
        caller="system",
        operation="dashboard_sessions_revoked",
        outcome="ok",
        source="token_auth",
        resources="action=revoke_all",
    )
    _state.clear_all()
    # Bump the persisted revocation generation so already-issued cookies (which
    # the cleared per-process nonce store cannot touch) are rejected on their
    # next request. Both access-cookie and refresh-token validation check the
    # gen, so this ends established browser sessions AND their refresh chains.
    bump_revocation_gen()


def parse_duration(s: str) -> int | None:
    """Parse ``'<int>h'`` or ``'<int>m'`` into seconds, or *None*.

    Returns *None* for invalid input. Caps at ``MAX_SESSION_TTL_SECS``.
    """
    m = re.fullmatch(r"(\d+)(h|m)", s)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    secs = value * 3600 if unit == "h" else value * 60
    return min(secs, MAX_SESSION_TTL_SECS)


def _cookie_port_from_host(request: web.Request, fallback: int) -> str:
    """Return the port the browser connects to (Host header), else *fallback*.

    The dashboard cookie is named ``mc_token_<port>``. Keying it by the server's
    own listen port breaks under SSH tunnels: two Cloud Desktops both serving on
    7777 but tunneled to distinct local ports would collide on a single
    ``mc_token_7777`` because browser cookies are not isolated by port (RFC 6265
    scopes by host only). The browser-facing port is unique per dashboard, so it
    is the correct key. Falls back to the server port when the Host header
    carries no port, preserving behavior for direct (non-tunnel) access.
    """
    host = request.headers.get("Host", "")
    if ":" in host:
        candidate = host.rsplit(":", 1)[-1].strip()
        if candidate.isdigit():
            return candidate
    return str(fallback)


async def warm_auth_singletons() -> None:
    """Prime the signing-secret and revoked-nonce singletons OFF the event loop.

    Both ``_get_secret()`` and ``_get_revoked_store()`` do blocking file I/O on
    first use (read/create ``token_signing.key`` + read the persisted nonce
    denylist; on Windows also the owner-only DACL on the key file).
    Calling them lazily from the request path — or synchronously inside the
    ``token_auth_middleware()`` factory, which runs on the loop via the async
    ``start_dashboard()`` / ``start_api_server()`` — would land that I/O on the
    event loop (no-blocking-call-on-event-loop).

    The async startup paths ``await`` this exactly once, BEFORE constructing
    the middleware chain and before the server begins accepting connections, so
    the first auth op hits the already-built singletons with no blocking I/O on
    the loop. Idempotent: both callees memoize under a lock.
    """
    await asyncio.to_thread(_get_secret)
    await asyncio.to_thread(_get_revoked_store)
    # The revocation generation is lazy-loaded from disk on first use; prime it
    # here too so the first token validation never does file I/O on the loop.
    await asyncio.to_thread(current_revocation_gen)
