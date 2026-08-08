"""OAuth-style refresh tokens for the KiroCrew dashboard.

Adds a paired refresh cookie alongside the existing access cookie
(``mc_token_<port>``) so users do not need to re-mint via the
``kirocrew token`` URL every ~20 hours.

Design (full spec in ``docs/system-specs/features/dashboard-token-auth.md``):

- Refresh cookie ``mc_refresh_<port>`` is path-restricted to
  ``/api/auth/refresh`` — narrower attack surface than the access cookie.
- Lifetime up to 30 days (``MAX_REFRESH_TTL_SECS``), HMAC-signed with the
  same persistent ``token_signing.key`` secret as the access cookie.
- Rotation-on-use: each ``/api/auth/refresh`` call mints a fresh pair and
  marks the prior ``jti`` consumed.
- Reuse detection (RFC 6819 §5.2.2.3): a consumed ``jti`` presented again
  outside the multi-tab grace window auto-revokes the entire chain.
- 60-second same-IP grace window absorbs benign multi-tab races.
- Persistence: ``~/.kiro/crew/refresh_chains.json`` (mode ``0600``).
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
from typing import TYPE_CHECKING, Iterable, Mapping

from kiro_crew.atomic_write import atomic_write
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

# Per-port cookie name prefixes. Browser cookies are NOT isolated by port
# (RFC 6265 §8.5), so on 127.0.0.1 every gateway instance ever run on a
# different port leaves its own ``mc_token_<port>`` / ``mc_refresh_<port>``
# pair in the single shared cookie jar. Nothing pruned them, so the Cookie
# header grew without bound until it crossed aiohttp's ``max_field_size``
# and every request 400'd (``LineTooLong``) inside the C parser, before any
# handler could run. Callers expire the OTHER-port pairs on a successful
# auth so the jar self-trims — see ``foreign_port_cookies``.
ACCESS_COOKIE_PREFIX = "mc_token_"
REFRESH_COOKIE_PREFIX = "mc_refresh_"
ACCESS_COOKIE_PATH = "/"

# Only trim the jar once it is actually approaching the header limit. Below
# this, per-port cookies coexist untouched — so a developer legitimately
# running two LIVE gateways in one browser (e.g. the main instance plus an
# isolated test pod) does not have one session's refresh expire the other's
# cookie. Pruning kicks in only when accumulation (mostly dead ports whose
# gateways no longer exist) genuinely threatens overflow. 6 KiB leaves ample
# headroom under the raised 32 KiB parser limit AND under the stock 8190-byte
# default any fronting proxy might still enforce.
COOKIE_JAR_PRUNE_THRESHOLD_BYTES = 6 * 1024

# Multi-tab grace window: a jti consumed within this many seconds is
# still accepted from the same chain + same source IP. The handler
# returns the most-recently-issued replacement pair instead of
# minting yet another rotation. Outside this window, reuse is
# treated as theft and the chain is revoked.
REFRESH_GRACE_SECS = 60

# Grace acceptance is CHAIN-HEAD-ONLY. We retain exactly ONE recently-consumed
# jti per chain — the single most-recently-rotated one (the chain head) — as
# the authenticator for a same-IP, in-window replay. An earlier design widened
# this to a bounded history of the last N consumed jtis so several lagging tabs
# could each authenticate a benign race; that was a deliberate weakening of the
# RFC 6819 §5.2.2.3 reuse signal (any of the last N consumed jtis, replayed
# same-IP within the window, resolved to the live head instead of tripping
# chain revocation) and was flagged by the Design + Long-Term Impact reviewers.
# The maintainer chose the STRONGER posture: only the chain head is accepted, so
# any OLDER rotated jti replayed within the window is treated as token reuse and
# revokes the chain. This restores an undiluted theft signal at the cost of some
# multi-tab UX (a second stale tab racing a refresh may be logged out). The
# single-tab / single-refresh false-revocation race (a duplicate request
# presenting the just-consumed head) is still absorbed. Keep this behaviour and
# the spec (``docs/system-specs/features/dashboard-token-auth.md`` — "Multi-tab
# grace window") in sync.

# Persistence file name (resolved against ``config_dir()`` lazily so
# imports stay cheap).
_STATE_FILE_NAME = "refresh_chains.json"


# --- State manager -----------------------------------------------------------


class RefreshStateManager:
    """Thread-safe state for refresh-token rotation and chain revocation.

    Persists consumed-``jti`` and revoked-``chain_id`` records to disk so
    reuse-detection state survives gateway restarts. Atomic-rename writes guard
    against truncation on crash.

    The multi-tab grace state (``_grace_replacements``) is deliberately
    IN-MEMORY only — it is never serialized by ``_persist()``/``_load()``.
    A gateway restart that lands inside a 60s grace window therefore drops the
    grace entry while ``consumed_jtis`` survives, so a lagging tab's in-flight
    refresh could still hit reuse-detection. This is a rare, self-correcting
    window (the user simply re-mints via the ``kirocrew token`` URL) and keeping
    short-lived race state off disk avoids persisting live token material.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        # jti -> session_exp (eviction floor)
        self._consumed_jtis: dict[str, float] = {}
        # chain_id -> exp (the latest member's session_exp)
        self._revoked_chains: dict[str, float] = {}
        # chain_id -> (jti, ts, ip, replacement_pair_json) for the single
        # most-recently-consumed jti on the chain (the CHAIN HEAD). The
        # multi-tab grace window re-serves this replacement pair instead of
        # minting yet another rotation when the just-consumed head jti is
        # replayed same-IP within the window.
        #
        # Exactly ONE entry per chain (chain-head-only): each consumption
        # OVERWRITES the prior entry, so only the newest jti authenticates a
        # grace replay. An older rotated jti no longer matches and is treated
        # as token reuse — preserving the undiluted RFC 6819 §5.2.2.3 theft
        # signal. The recorded replacement always carries the current live
        # head token, so re-serving it can never roll a shared cookie jar back
        # to an already-consumed jti.
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
            # Chain-head-only: record ONLY this (the newest) consumed jti as
            # the grace authenticator, overwriting any prior entry for the
            # chain. An older rotated jti therefore can no longer authenticate
            # a same-IP replay — it trips reuse-detection instead.
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
        """Return the CHAIN-HEAD replacement payload if the multi-tab grace applies.

        Grace acceptance is **chain-head-only**: we retain exactly one
        recently-consumed jti per chain — the most-recently-rotated one (the
        chain head) — and only accept a replay of *that* jti. The presented
        ``jti`` must equal the recorded head jti, and the replay must be from
        the same source IP within ``REFRESH_GRACE_SECS``. If so we re-serve the
        head's replacement pair (which carries the current live, not-yet-consumed
        refresh token) instead of minting yet another rotation — absorbing the
        benign single-refresh race where a duplicate request presents the
        just-consumed head.

        Any OLDER rotated jti (one the active tab has already rotated past) does
        NOT match the head and returns ``None``, so the caller treats it as
        token reuse and revokes the chain. This is the deliberate, stronger
        RFC 6819 §5.2.2.3 posture chosen by the maintainer over a wider
        multi-jti history: it keeps the theft signal undiluted at the cost of
        some multi-tab UX (a second stale tab racing a refresh may be logged
        out). Because only the live head pair is ever recorded and served, a
        slow lagging response can never roll a shared cookie jar back to an
        already-consumed jti.

        Returns the JSON-encoded head payload to re-serve, or ``None`` if no
        grace applies.
        """
        if now is None:
            now = time.time()
        with self._lock:
            entry = self._grace_replacements.get(chain_id)
            if entry is None:
                return None
            head_jti, ts, cached_ip, replacement = entry
            # Chain-head-only: accept a replay of ONLY the single most-recently-
            # rotated jti. Anything older is suspected reuse -> no grace.
            if jti != head_jti:
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
            for chain_id, entry in list(self._grace_replacements.items()):
                # Grace entries are short-lived: drop any whose recorded
                # timestamp is older than 2x the grace window.
                if now - entry[1] > REFRESH_GRACE_SECS * 2:
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
                # Create-empty → tighten-DACL → write, NOT write-then-restrict:
                # on Windows restrict_to_owner is a subprocess (icacls) that
                # takes measurable time, so if the payload were written first
                # the temp would carry the parent-inherited DACL during that
                # window and a local co-tenant able to enumerate ~/.kiro/crew
                # could read the consumed-JTI + revoked-chain state (breaking
                # RFC-6819 §5.2.2.3 reuse-detection secrecy) or, worse,
                # truncate the temp before the rename and substitute state that
                # un-revokes a stolen chain. atomic_write applies the lockdown
                # before any payload byte, and names the temp via mkstemp
                # (random + O_EXCL) rather than a predictable sibling, so the
                # substitution above has no name to pre-plant.
                #
                # restrict_on_error="warn" preserves this call site's original
                # policy: publish anyway and log. Escalating to a raise would
                # hit the outer OSError handler below and drop the
                # reuse-detection record entirely, which is worse than a state
                # file another local user can read.
                atomic_write(
                    self._state_path,
                    json.dumps(data, separators=(",", ":")).encode("utf-8"),
                    restrict_to_owner=True,
                    restrict_on_error="warn",
                )
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
    return f"{REFRESH_COOKIE_PREFIX}{port}"


def foreign_port_cookies(
    cookie_names: Iterable[str], current_port: str | int
) -> list[tuple[str, str]]:
    """Return ``(name, path)`` pairs for per-port auth cookies of OTHER ports.

    ``cookie_names`` is what the browser sent (e.g. ``request.cookies``);
    ``current_port`` is the port THIS gateway resolved for the request
    (``_cookie_port_from_host``), whose own pair is always preserved.

    The returned ``path`` matches how each cookie was originally set
    (access = ``/``, refresh = ``/api/auth``) because cookie deletion is
    path-sensitive: a ``Set-Cookie`` with ``max_age=0`` only removes a
    cookie when its ``path`` matches the one used to set it. Suffixes must
    be digit-only, so non-port names — including the legacy ``mc_token``
    (no suffix) — are never touched.

    Callers expire the returned cookies on successful auth so the shared
    127.0.0.1 jar self-trims and can never grow past aiohttp's header limit.
    """
    current = str(current_port)
    stale: list[tuple[str, str]] = []
    for name in cookie_names:
        for prefix, path in (
            (ACCESS_COOKIE_PREFIX, ACCESS_COOKIE_PATH),
            (REFRESH_COOKIE_PREFIX, REFRESH_COOKIE_PATH),
        ):
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix) :]
            if suffix.isdigit() and suffix != current:
                stale.append((name, path))
            break
    return stale


def cookie_jar_needs_pruning(cookies: Mapping[str, str]) -> bool:
    """True when the incoming cookie jar is large enough to warrant trimming.

    Approximates the wire size of the ``Cookie`` request header (``name=value``
    pairs joined by ``"; "``). Kept as a cheap gate so pruning only fires once
    accumulation approaches the limit — see ``COOKIE_JAR_PRUNE_THRESHOLD_BYTES``
    for why small jars are deliberately left alone.
    """
    total = sum(len(name) + len(value) + 2 for name, value in cookies.items())
    return total > COOKIE_JAR_PRUNE_THRESHOLD_BYTES
