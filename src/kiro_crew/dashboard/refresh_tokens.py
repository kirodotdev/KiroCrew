"""OAuth-style refresh tokens for the KiroCrew dashboard.

Adds a paired refresh cookie alongside the existing access cookie
(``mc_token_<port>``) so users do not need to re-mint via the
``kirocrew token`` URL every ~20 hours.

Design (full spec in ``docs/token-refresh/REQUIREMENTS.md``):

- Refresh cookie ``mc_refresh_<port>`` is path-restricted to
  ``/api/auth/refresh`` — narrower attack surface than the access cookie.
- Lifetime up to 30 days (``MAX_REFRESH_TTL_SECS``), HMAC-signed with the
  same persistent ``token_signing.key`` secret as the access cookie.
- Rotation-on-use: each ``/api/auth/refresh`` call mints a fresh pair and
  marks the prior ``jti`` consumed.
- Reuse detection (RFC 6819 §5.2.2.3): a consumed ``jti`` presented again
  outside the multi-tab grace window auto-revokes the entire chain.
- 60-second same-IP grace window absorbs benign multi-tab races.
- Persistence: ``~/.kirocrew/refresh_chains.json`` (mode ``0600``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.token_secret import _get_secret

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# --- Constants ---------------------------------------------------------------

# Maximum refresh-cookie lifetime. After this much idle time the user
# will be forced through the regular ``kirocrew token`` URL flow again
# to start a fresh chain.
MAX_REFRESH_TTL_SECS = 30 * 86400  # 30 days

# Cookies are path-restricted so they are only sent on the refresh
# endpoint, never on regular dashboard traffic.
#
# Scope = "/api/auth" (NOT "/api/auth/refresh") so the cookie is also
# sent to "/api/auth/logout" — without this, logout cannot read the
# refresh cookie and chain revocation silently fails. The HttpOnly +
# SameSite=Lax + same-origin Origin-check protections are unchanged;
# only the path scope is one segment broader so logout works. Any
# future "/api/auth/*" handler that should NOT see the refresh cookie
# must add its own scrubbing — but that's a deliberate choice we'd
# rather make than have logout silently no-op.
REFRESH_COOKIE_PATH = "/api/auth"

# Multi-tab grace window: a jti consumed within this many seconds is
# still accepted from the same chain + same source IP. The handler
# returns the most-recently-issued replacement pair instead of
# minting yet another rotation. Outside this window, reuse is
# treated as theft and the chain is revoked.
REFRESH_GRACE_SECS = 60

# Persistence file name (resolved against ``config_dir()`` lazily so
# imports stay cheap).
_STATE_FILE_NAME = "refresh_chains.json"


# --- State manager -----------------------------------------------------------


class RefreshStateManager:
    """Thread-safe state for refresh-token rotation and chain revocation.

    Persists consumed-``jti`` and revoked-``chain_id`` records to disk so
    rotation history survives gateway restarts. Atomic-rename writes guard
    against truncation on crash.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        # jti -> session_exp (eviction floor)
        self._consumed_jtis: dict[str, float] = {}
        # chain_id -> exp (the latest member's session_exp)
        self._revoked_chains: dict[str, float] = {}
        # chain_id -> (jti, exp, ip, replacement_pair_json) so the multi-tab
        # grace window can return the SAME replacement pair instead of
        # minting yet another rotation each time the same tab retries.
        self._grace_replacements: dict[str, tuple[str, float, str, str]] = {}
        self._state_path = state_path
        self._load()

    # -- public API --

    def mark_consumed(
        self,
        jti: str,
        chain_id: str,
        exp: float,
        ip: str,
        replacement: str,
    ) -> None:
        """Record that ``jti`` was used to mint ``replacement``.

        ``replacement`` is the JSON-encoded payload we returned to the
        client (so the multi-tab grace window can return the same pair).

        Auto-evicts expired entries on each call so the on-disk file
        cannot grow without bound (e.g. an attacker pumping rotations
        with a stolen refresh cookie before reuse-detection fires).
        """
        with self._lock:
            self._consumed_jtis[jti] = exp
            self._grace_replacements[chain_id] = (jti, time.time(), ip, replacement)
        self.evict_expired()
        self._persist()

    def is_consumed(self, jti: str) -> bool:
        with self._lock:
            return jti in self._consumed_jtis

    def revoke_chain(self, chain_id: str, exp: float) -> None:
        with self._lock:
            self._revoked_chains[chain_id] = exp
            # Drop any grace replacement so a revoked chain cannot
            # be served from cache.
            self._grace_replacements.pop(chain_id, None)
        self.evict_expired()
        self._persist()

    def is_chain_revoked(self, chain_id: str) -> bool:
        with self._lock:
            return chain_id in self._revoked_chains

    def grace_replacement(
        self,
        chain_id: str,
        jti: str,
        ip: str,
        now: float | None = None,
    ) -> str | None:
        """Return the cached replacement payload if the multi-tab grace applies.

        The grace window is: same chain, same jti, same source IP, within
        ``REFRESH_GRACE_SECS`` seconds. Returns the JSON-encoded payload that
        should be re-served, or ``None`` if no grace applies.
        """
        if now is None:
            now = time.time()
        with self._lock:
            entry = self._grace_replacements.get(chain_id)
            if not entry:
                return None
            cached_jti, ts, cached_ip, replacement = entry
            if cached_jti != jti:
                return None
            if cached_ip != ip:
                return None
            if now - ts > REFRESH_GRACE_SECS:
                return None
            return replacement

    def evict_expired(self, now: float | None = None) -> None:
        """Drop entries whose expiry has passed."""
        if now is None:
            now = time.time()
        with self._lock:
            for jti, exp in list(self._consumed_jtis.items()):
                if exp < now:
                    self._consumed_jtis.pop(jti, None)
            for chain_id, exp in list(self._revoked_chains.items()):
                if exp < now:
                    self._revoked_chains.pop(chain_id, None)
            for chain_id, (_, ts, _, _) in list(self._grace_replacements.items()):
                # Grace replacements are short-lived
                if now - ts > REFRESH_GRACE_SECS * 2:
                    self._grace_replacements.pop(chain_id, None)

    def clear_all(self) -> None:
        """Wipe all state. Used by tests and ``kirocrew logout``."""
        with self._lock:
            self._consumed_jtis.clear()
            self._revoked_chains.clear()
            self._grace_replacements.clear()
        self._persist()

    # -- persistence --

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = self._state_path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "refresh_tokens: failed to load state from %s (%s); starting empty",
                self._state_path,
                e,
            )
            return
        with self._lock:
            # A single corrupt `exp` (e.g. "abc" or null) must not brick the
            # store: float() raises TypeError/ValueError, and this runs in the
            # RefreshStateManager constructor, so an unguarded coercion made
            # _get_state() — and thus EVERY /api/auth/refresh call — 500 until
            # the file was hand-repaired. Skip the malformed entry instead.
            for entry in data.get("consumed_jtis", []):
                if isinstance(entry, dict) and "jti" in entry and "exp" in entry:
                    try:
                        self._consumed_jtis[str(entry["jti"])] = float(entry["exp"])
                    except (TypeError, ValueError):
                        logger.warning("refresh_tokens: dropping consumed_jti with bad exp: %r", entry)
            for entry in data.get("revoked_chains", []):
                if isinstance(entry, dict) and "chain_id" in entry and "exp" in entry:
                    try:
                        self._revoked_chains[str(entry["chain_id"])] = float(entry["exp"])
                    except (TypeError, ValueError):
                        logger.warning("refresh_tokens: dropping revoked_chain with bad exp: %r", entry)

    def _persist(self) -> None:
        if self._state_path is None:
            return
        # Hold the lock across the FULL serialize+write+rename so concurrent
        # writers cannot clobber each other's atomic-rename. Per a security
        # review finding: without this, thread A can snapshot
        # state S1, thread B can mutate + persist S2, and A's later os.replace
        # overwrites S2 with stale S1 -- losing B's consumed-jti record. After
        # a restart, reuse detection would silently fail to fire for that jti.
        # Holding the lock during file I/O is acceptable: callers run inside
        # asyncio.to_thread(), so we're already off the event loop, and the
        # write is ~100 bytes.
        with self._lock:
            data = {
                "consumed_jtis": [
                    {"jti": jti, "exp": exp}
                    for jti, exp in self._consumed_jtis.items()
                ],
                "revoked_chains": [
                    {"chain_id": cid, "exp": exp}
                    for cid, exp in self._revoked_chains.items()
                ],
            }
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
                payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
                # Create-empty → tighten-DACL → write pattern (not
                # write-then-restrict): on Windows restrict_to_owner is a
                # subprocess (icacls) that takes measurable time, so if we
                # wrote the payload first the .tmp file would carry the
                # parent-inherited DACL during that window and a local
                # co-tenant able to enumerate ~/.kirocrew could read the
                # consumed-JTI + revoked-chain state (breaking RFC-6819
                # §5.2.2.3 reuse-detection secrecy) or, worse, truncate the
                # .tmp before os.replace and substitute state that un-revokes
                # a stolen chain. restrict_to_owner (fail-loud) sits BEFORE
                # os.write; failure logs and continues (POSIX & Windows agree).
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    try:
                        platform_compat.restrict_to_owner(tmp)
                    except OSError:
                        # Logs the file PATH (tmp), never any token/secret value.
                        logger.warning(  # nosemgrep: python-logger-credential-disclosure
                            "refresh_tokens: failed to set owner-only permissions on %s; "
                            "file may be readable by other users",
                            tmp,
                            exc_info=True,
                        )
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                os.replace(tmp, self._state_path)
            except OSError as e:
                logger.warning(
                    "refresh_tokens: failed to persist state to %s (%s)",
                    self._state_path,
                    e,
                )


# --- Module-level singleton --------------------------------------------------

_state_singleton: RefreshStateManager | None = None
_state_singleton_lock = threading.Lock()


def _get_state() -> RefreshStateManager:
    """Return the lazily-initialized module singleton."""
    global _state_singleton
    if _state_singleton is None:
        with _state_singleton_lock:
            if _state_singleton is None:
                _state_singleton = RefreshStateManager(
                    state_path=config_dir() / _STATE_FILE_NAME
                )
    return _state_singleton


# --- Token generation / validation -------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def _sign(payload: bytes) -> str:
    """HMAC-SHA256 sign with the persistent token-signing secret."""
    return _b64url_encode(hmac.new(_get_secret(), payload, hashlib.sha256).digest())


def generate_refresh_token(
    user_id: str,
    *,
    chain_id: str | None = None,
    ttl_seconds: int = MAX_REFRESH_TTL_SECS,
) -> tuple[str, str, str, float]:
    """Generate a refresh token.

    Returns ``(token, chain_id, jti, session_exp)``.

    Pass ``chain_id`` to continue an existing rotation chain (during refresh).
    Omit it to start a fresh chain (initial mint after ``kirocrew token``
    URL is consumed).
    """
    now = time.time()
    session_ttl = min(ttl_seconds, MAX_REFRESH_TTL_SECS)
    if chain_id is None:
        chain_id = os.urandom(6).hex()  # 12 hex chars / 48 bits
    jti = os.urandom(12).hex()  # 24 hex chars / 96 bits

    payload_dict = {
        "sub": user_id,
        "kind": "refresh",
        "chain_id": chain_id,
        "jti": jti,
        "iat": now,
        "session_exp": now + session_ttl,
    }
    payload = json.dumps(payload_dict, separators=(",", ":")).encode()
    encoded_payload = _b64url_encode(payload)
    signature = _sign(payload)
    return f"{encoded_payload}.{signature}", chain_id, jti, now + session_ttl


def validate_refresh_token(token: str) -> tuple[bool, str, str, str, str, float]:
    """Return ``(valid, user_id, reason, chain_id, jti, session_exp)``.

    Validates HMAC, ``kind=refresh``, ``session_exp``, and that the chain
    has not been revoked. Does NOT consult the consumed-jti map — callers
    decide whether to apply consumption semantics (the refresh endpoint
    does, the ``/auth/me`` peek does not).
    """
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False, "", "malformed token", "", "", 0.0
    encoded_payload, sig = parts
    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except (ValueError, TypeError):
        return False, "", "malformed token", "", "", 0.0
    expected = _sign(payload_bytes)
    if not hmac.compare_digest(sig, expected):
        return False, "", "bad signature", "", "", 0.0
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        return False, "", "malformed payload", "", "", 0.0
    if payload.get("kind") != "refresh":
        return False, "", "wrong token kind", "", "", 0.0
    user_id = str(payload.get("sub", ""))
    chain_id = str(payload.get("chain_id", ""))
    jti = str(payload.get("jti", ""))
    session_exp = float(payload.get("session_exp", 0))
    now = time.time()
    if session_exp < now:
        return False, user_id, "expired", chain_id, jti, session_exp
    if not chain_id or not jti:
        return False, "", "missing claims", "", "", 0.0
    state = _get_state()
    if state.is_chain_revoked(chain_id):
        return False, user_id, "chain revoked", chain_id, jti, session_exp
    return True, user_id, "", chain_id, jti, session_exp


def refresh_cookie_name(port: str | int) -> str:
    """Mirror the existing per-port pattern used for the access cookie."""
    return f"mc_refresh_{port}"
