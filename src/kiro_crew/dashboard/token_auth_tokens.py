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
import contextlib
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
from kiro_crew.atomic_write import retry_read_sharing_window
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

    Two locks, so a slow disk never reaches the event loop. ``_lock`` guards the
    in-memory dict only and is never held across I/O: :meth:`is_revoked` runs on
    the loop for every cookie request, and a write that takes seconds (on-access
    antivirus scanning a new file) would otherwise park the loop on it until the
    loop watchdog kills the gateway. ``_io_lock`` serializes the disk writes,
    which run on worker threads; each writer snapshots the dict once it holds
    ``_io_lock``, so the last write to land always carries the newest state.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._revoked: dict[str, float] = {}  # nonce -> session_exp (eviction floor)
        # True while the file exists but could not be read (e.g. a Windows handle without
        # read sharing). Cookies fail closed and _persist refuses until a re-read works.
        self._state_path = state_path
        self._unreadable = not self._load()

    def revoke(self, nonce: str, session_exp: float) -> bool:
        """Record *nonce* as revoked until *session_exp*, evicting expired entries.

        Returns False when the file could not be read, so the record is kept in memory only
        (the next write after a successful re-read stores it). Callers run it off the loop.
        """
        now = time.time()
        with self._lock:
            self._revoked[nonce] = session_exp
            # Opportunistic eviction so a stream of logouts cannot grow the file.
            expired = [n for n, exp in self._revoked.items() if exp < now]
            for n in expired:
                self._revoked.pop(n, None)
        if not self.readable():
            return False
        self._persist()
        return True

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

    def _load(self) -> bool:
        """Load the denylist; ``False`` when the file is there but could not be read."""
        if self._state_path is None or not self._state_path.exists():
            return True
        path = self._state_path
        try:
            text = retry_read_sharing_window(lambda: path.read_text(encoding="utf-8"), path)
        except FileNotFoundError:
            return True
        except OSError:
            # The revocations are still on disk; reading them as none would let a logged-out
            # cookie back in, and the next logout would write the empty set over the file.
            logger.warning("could not read revoked-nonce store; failing closed", exc_info=True)
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("could not read revoked-nonce store; starting empty", exc_info=True)
            return True
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
        return True

    def readable(self) -> bool:
        """False while the file is there but could not be read; re-tries the read first.

        Reads only, never writes, so the cookie check can call it on the event loop.
        Entries recorded meanwhile stay in memory and go out with the next ``revoke``.
        """
        # The mark is cleared only after the records are in memory, so a concurrent caller
        # never sees a half-loaded denylist as trustworthy. Two concurrent re-reads merge the
        # same records, which is harmless.
        if self._unreadable and self._load():
            self._unreadable = False
        return not self._unreadable

    def _persist(self) -> None:
        if self._state_path is None or self._unreadable:
            return
        with self._io_lock:
            # Snapshot under _lock, then release it before any I/O (see the
            # class docstring).
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
                        "SECURITY: could not restrict revoked-nonce store %s to its owner; "
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
    session: bool = False,
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
    prompt/peer_key/kind) cannot be overridden. ``peer_key`` is a dedicated
    parameter because it is an authorization boundary, not generic metadata.

    *session* marks the token ``kind=session``, the only non-app token the
    session (cookie) path accepts. Pass it only where a session is minted: the
    middleware's link exchange and the refresh rotation. Every other token is a
    one-time link.

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
    # A link is printed, posted to chat and kept in history, so the session
    # path accepts only a token its minter declared a session.
    if session:
        payload_dict["kind"] = "session"
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
            "kind",
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
        # Fail closed while the denylist file cannot be read, as for an unreadable
        # revocation counter: an empty reading would let a logged-out cookie back in.
        if not _get_revoked_store().readable():
            return False, "", "session revocation state unavailable"
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


#: Refusal reason for a non-session token presented where a session is required.
LINK_NOT_A_SESSION = "link token is not a session"


def validate_token_with_app(
    token: str, *, use_session_exp: bool = False
) -> tuple[bool, str, str, str]:
    """Return ``(valid, user_id, reason, app_name)``.

    Extends :func:`validate_token` by also extracting the ``app`` field
    from the token payload.  This avoids changing the existing
    ``validate_token`` signature.

    On the session path (*use_session_exp*) it accepts only ``kind=session`` or
    an app token: a link's ``session_exp`` is the lifetime of the session it may
    be exchanged for. :func:`validate_token` callers only read claims.
    """
    valid, user_id, reason = validate_token(token, use_session_exp=use_session_exp)
    if not valid:
        return False, user_id, reason, ""
    # Extract app from payload
    try:
        payload_bytes = _b64url_decode(token.split(".")[0])
        data = json.loads(payload_bytes)
    except Exception:
        data = {}
    app_name = data.get("app", "")
    if use_session_exp and not app_name and data.get("kind") != "session":
        return False, "", LINK_NOT_A_SESSION, ""
    if app_name and not _app_token_live(app_name, _app_issued_at(data)):
        # Refused exactly like a forged token: the caller learns nothing about
        # the app's state, and its SDK re-exchanges the secret as it does today.
        return False, "", "invalid signature", ""
    return valid, user_id, reason, app_name


async def validate_token_with_app_off_loop(
    token: str, *, use_session_exp: bool = False
) -> tuple[bool, str, str, str]:
    """:func:`validate_token_with_app`, in a worker thread for a signed app token.

    An app token's check can read the app's install record (a cache miss after an
    invalidation), so it never runs on the event loop. Any other token reads
    nothing there and is checked in place.
    """
    if token and _signed_app_claim(token):
        return await asyncio.to_thread(
            validate_token_with_app, token, use_session_exp=use_session_exp
        )
    return validate_token_with_app(token, use_session_exp=use_session_exp)


#: The issue time of the app token an app session cookie was exchanged from,
#: carried by the middleware's exchange (a string: ``extra`` claims are strings).
APP_IAT_CLAIM = "app_iat"


def _app_issued_at(data: dict) -> object:
    """When the app credential behind *data* was issued: the carried app-token
    ``iat`` on an exchanged session, else the token's own ``iat``.

    The session is a fresh mint, so its own ``iat`` can postdate a disable the
    app token it came from predates: a request whose token passed the check just
    before the owner's disable is exchanged just after it. The cutoff must judge
    the original. A carried value that is not a number reads as unknown, which
    refuses unless no cutoff was ever set.
    """
    carried = data.get(APP_IAT_CLAIM)
    if carried is None:
        return data.get("iat")
    try:
        return float(carried)
    except (TypeError, ValueError):
        return None


def _app_token_live(app_name: str, iat: object) -> bool:
    """Whether *app_name* is installed and enabled, and the token postdates its cutoff.

    The HMAC key is gateway-wide, so a signature alone outlives a disable or an
    uninstall. The record is read on every check, so a disable from any process
    (the CLI included) takes effect on the next request; the async middleware runs
    this check in a worker thread (:func:`validate_token_with_app_off_loop`), so
    the read never runs on the event loop. A name that is not a valid app name
    refuses without a read. Deferred import: ``apps`` imports the dashboard auth
    modules.
    """
    from kiro_crew.apps.manager import APP_NAME_RE, app_token_cutoff

    name = str(app_name)
    if not APP_NAME_RE.fullmatch(name):
        return False
    cutoff = app_token_cutoff(name)
    if cutoff is None:
        return False
    if isinstance(iat, bool) or not isinstance(iat, (int, float)):
        return cutoff <= 0
    # Strictly after: a token minted in the same clock tick as the disable is from
    # before it, and a coarse clock (Windows) can make the two equal.
    return iat > cutoff


def _signed_app_claim(token: str) -> str:
    """The ``app`` claim of *token* when its signature verifies, else ``""``.

    Signature only, no expiry or revocation: it decides whether the check runs in a
    worker thread, and the token is fully validated there. A forged token is
    checked in place and reads nothing.
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return ""
    try:
        payload = _b64url_decode(parts[0])
    except Exception:
        return ""
    if not _ct_eq(parts[1], _sign(payload)):
        return ""
    try:
        data = json.loads(payload)
    except Exception:
        return ""
    app = data.get("app") if isinstance(data, dict) else None
    return app if isinstance(app, str) else ""


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


#: Signed claim on a token minted FOR the owner (the local bootstrap, the startup
#: link, the owner's Slack link; a phone link inherits it from the lending session). The subject alone cannot say this: it is fixed at
#: mint time, so once the configured owner changes, a former owner's session and a
#: messaging-allowlist user's session look the same. The claim lets the owner gate
#: tell the first one to sign in again instead of handing it a dead-end 403. It
#: only relabels a denial; it never grants anything.
OWNER_MINT_CLAIM = "owner_mint"


def owner_mint_extra(extra: dict[str, str] | None = None) -> dict[str, str]:
    """``extra`` plus the owner-mint claim, for an owner-identity mint site."""
    return {**(extra or {}), OWNER_MINT_CLAIM: "1"}


def token_owner_minted_unverified(token: str) -> bool:
    """Whether a token carries the owner-mint claim.

    Read only after normal signature validation (same contract as
    :func:`required_peer_key_unverified`). An undecodable token answers False,
    the generic-denial direction.
    """
    try:
        data = json.loads(_b64url_decode(token.split(".")[0]))
    except Exception:
        return False
    return isinstance(data, dict) and str(data.get(OWNER_MINT_CLAIM, "")) == "1"


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
    """Validate an app secret for an app the owner enabled through the gateway.

    The authority is the owner-enable marker ``<crew-home>/host-exec-grants/{app_name}``
    (``apps.host_exec_grants``): a sealed, top-level directory only the unsandboxed
    gateway writes, on an owner-authenticated enable or first owner registration,
    and every disable and uninstall revokes. Files under the agent-writable
    ``apps/{app_name}/`` (``installed.json``, ``app.json``) are never enough. With the
    marker present, the secret is compared in constant time against the gateway's
    own record (:func:`read_app_secret`), which no sandbox can read.
    """
    # circular import: apps.manager imports write_app_secret from token_auth
    from kiro_crew.apps.host_exec_grants import host_exec_granted

    if not provided_secret or not host_exec_granted(app_name):
        return False
    stored = read_app_secret(app_name)
    if not stored:
        return False
    return _ct_eq(stored, provided_secret)


#: Serializes the one-time move of an app's secret out of its folder, so two readers
#: never store two different secrets for one app.
_app_secret_move_lock = threading.Lock()


def _legacy_secret_path(app_name: str) -> Path:
    from kiro_crew.config.loader import config_dir

    return config_dir() / "apps" / app_name / ".app_secret"


def read_app_secret(app_name: str) -> str:
    """*app_name*'s gateway secret, or ``""`` when it has none. Read-only; gateway only.

    The secret lives in ``member-records/app-secrets/`` (:mod:`apps.protected_app_state`),
    which every sandbox masks. An ``apps/{app_name}/.app_secret`` an older version left
    in the agent-readable folder is never read: until a channel that can hand the app a
    new secret replaces it (:func:`replace_legacy_app_secret`), the app has none.
    """
    # circular import: apps.protected_app_state -> member_records -> config
    from kiro_crew.apps import protected_app_state

    try:
        return protected_app_state.read_secret(app_name) or ""
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


def replace_legacy_app_secret(app_name: str) -> str:
    """The stored secret, or a NEW one for an app an older version left a folder secret.

    Called where the app gets what the secret grants: the backend spawn, which hands it
    over as ``KIROCREW_PROXY_SECRET``, and the load of the app's in-process hooks, whose
    context mints app tokens with no secret (:func:`issue_in_process_app_token`). The
    old file's bytes are never read, since a sandbox could have read or planted them;
    the file is removed. An app with neither gets none. Writes to disk, so never call it
    on the event loop.
    """
    # circular import: apps.protected_app_state -> member_records -> config
    from kiro_crew.apps import protected_app_state

    stored = read_app_secret(app_name)
    if stored:
        return stored
    legacy = _legacy_secret_path(app_name)
    with _app_secret_move_lock:
        try:
            stored = protected_app_state.read_secret(app_name) or ""
            if stored:
                return stored
            if not os.path.lexists(legacy):
                return ""
            secret = generate_app_secret()
            protected_app_state.write_secret(app_name, secret)
        except (OSError, UnicodeDecodeError, ValueError):
            logger.warning("could not move an app secret out of the app's folder", exc_info=True)
            return ""
        with contextlib.suppress(OSError):
            legacy.unlink()
    logger.info(
        "%s: replaced the value its folder held; this start hands over the new one", app_name
    )
    return secret


def write_app_secret(app_name: str, secret: str) -> None:
    """Store *app_name*'s secret where only the gateway reads it (:func:`read_app_secret`).

    Any copy an older version left in the app's own folder is removed, so that folder
    never holds a secret the gateway accepts.
    """
    # circular import: apps.protected_app_state -> member_records -> config
    from kiro_crew.apps import protected_app_state

    protected_app_state.write_secret(app_name, secret)
    with contextlib.suppress(OSError):
        _legacy_secret_path(app_name).unlink()


def issue_in_process_app_token(app_name: str) -> str:
    """An app token for code *app_name* runs in the gateway process, or ``""``.

    ``AppContext.app_token`` calls this for an app's hooks, which hold no secret to
    exchange: the gateway built that context for this app alone, so the context is the
    proof of identity. The gate is the one :func:`validate_app_secret` applies first,
    the owner-enable marker, so a hook task that outlives a disable gets nothing. The
    token is the one the exchange route issues. Reads a sealed record, so never call it
    on the event loop.
    """
    # circular import: apps.manager imports write_app_secret from token_auth
    from kiro_crew.apps.host_exec_grants import host_exec_granted

    if not app_name or not host_exec_granted(app_name):
        _sel_fn().log_api_access(
            caller=app_name or "unknown",
            operation="app_token_exchange",
            outcome="denied",
            source="app_context",
            error="app not enabled by the owner",
        )
        return ""
    token = generate_token(app_name, app=app_name)
    _sel_fn().log_api_access(
        caller=app_name,
        operation="app_token_exchange",
        outcome="granted",
        source="app_context",
    )
    return token


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

    Raises ``OSError`` when the denylist file could not be read, so the record
    is in memory only and the caller must not report the logout as stored.

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
    # Both denylist answers come after every claim check, so the token is authentic. Record
    # (and store) it again: an earlier logout may be held in memory only, after an unreadable
    # file, and must not be reported as done until it is on disk.
    if not valid and _reason not in ("session revocation state unavailable", "session revoked"):
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
    if not _get_revoked_store().revoke(nonce, session_exp):
        # Held in memory only: a restart before the file reads again would forget it.
        raise OSError("revoked-nonce store could not be read; the logout was not stored")
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
