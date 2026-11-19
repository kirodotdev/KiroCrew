"""Local authorization: scoped, revocable Crew API tokens for an app on this machine.

OAuth 2.1 authorization code with PKCE (S256, public clients), served by this gateway
(the Crew API RFC, "Local authorization"). This module is the state and the rules; the
four routes are in :mod:`kiro_crew.dashboard.routes.local_authorization`.

* An app records a grant request. Nothing here lets the app answer it: only
  :meth:`LocalAuthorization.decide`, which only the owner's side calls, approves or denies.
* After the owner approves, the app gets a one-time code on its loopback redirect
  address and exchanges it, with the PKCE verifier, for a 5-minute access token and a
  rotating refresh token. A code or refresh token that shows up twice revokes the grant.
* :meth:`LocalAuthorization.verify` is the one check a route needs: it answers who the
  app is and what it may do, or ``None``. It reads the grant on every call, so a revoked
  grant is refused on the next request.

Requests and grants are one JSON record under ``member-records/local-grants/``, which no
sandbox can read or write. Codes and refresh tokens are stored as SHA-256 digests only.
The access token is a JWT signed with a key derived from the gateway's own signing key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.dashboard.token_auth_tokens import _b64url_decode, _b64url_encode, _ct_eq

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
LOCAL_GRANTS_KIND = "local-grants"
#: Stored on every request and grant. One fixed value today.
NAMESPACE = "default"
TOKEN_TYP = "kiro-crew-local+jwt"
#: The scopes that need no entitlement; local authorization gives no others.
SCOPES: tuple[str, ...] = (
    "responses:write",
    "conversations:read",
    "instance:read",
    "bots:chat",
    "bots:read",
    "bots:wake",
    "artifacts:read",
    "artifacts:write",
)

# Lifetimes and limits. All but the 5-minute access token are proposals to be confirmed
# (the Crew API RFC, "Local authorization").
REQUEST_TTL_SECS = 600
CODE_TTL_SECS = 60
ACCESS_TTL_SECS = 300
REFRESH_IDLE_SECS = 24 * 3600
REFRESH_MAX_SECS = 7 * 24 * 3600
POLL_INTERVAL_SECS = 2
MAX_PENDING_PER_CLIENT = 5
MAX_PENDING = 20

_STATE_MAX_BYTES = 4 * 1024 * 1024
# shortcut: a replayed refresh token older than this many rotations reads as unknown
# rather than revoking the grant. Keep every digest when grants outlive 7 days.
_USED_REFRESH_KEPT = 64
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]+$")
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"
# shortcut: one lock, so one gateway process owns the record. Take a file lock when a
# second process serves these routes.
_LOCK = threading.Lock()


class AuthError(Exception):
    """A refusal in the OAuth error shape (RFC 6749 section 5.2)."""

    def __init__(self, error: str, description: str, status: int = 400, retry_after: int = 0):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class LocalCredential:
    """What a verified access token says about its caller."""

    client_id: str
    grant_id: str
    scopes: frozenset[str]
    namespace: str

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def _loopback_redirect(uri: str) -> bool:
    """``http://127.0.0.1:<port>/...`` or ``http://[::1]:<port>/...``, nothing else."""
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in ("127.0.0.1", "::1")
        and port is not None
        and "@" not in parts.netloc
        and not parts.fragment
    )


def _with_query(uri: str, params: dict[str, str]) -> str:
    return f"{uri}{'&' if urlsplit(uri).query else '?'}{urlencode(params)}"


def _request_view(request_id: str, rec: Mapping[str, Any]) -> dict[str, Any]:
    """A grant request without its secrets, as the app and the owner see it."""
    return {
        "request_id": request_id,
        "status": rec["status"],
        "user_code": rec["user_code"],
        "client_id": rec["client_id"],
        "client_name": rec["client_name"],
        "redirect_uri": rec["redirect_uri"],
        "requested_scopes": list(rec["scopes"]),
        "namespace": rec["namespace"],
        "created_at": int(rec["created_at"]),
        "expires_at": int(rec["expires_at"]),
    }


class LocalAuthorization:
    """Grant requests, grants and tokens, kept in one record at *path*."""

    def __init__(self, path: Path, signing_key: bytes, clock: Callable[[], float] = time.time):
        self._path = path
        self._key = hmac.new(signing_key, TOKEN_TYP.encode(), hashlib.sha256).digest()
        self._clock = clock

    # ── the record ────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """The record. A missing file is empty; an unreadable one raises, so nothing is granted."""
        try:
            raw = read_bytes_with_retry(self._path, max_bytes=_STATE_MAX_BYTES)
        except FileNotFoundError:
            return {"requests": {}, "grants": {}}
        data = json.loads(raw.decode("utf-8"))
        if not (
            isinstance(data, dict)
            and isinstance(data.get("requests"), dict)
            and isinstance(data.get("grants"), dict)
        ):
            raise ValueError("local authorization record is malformed")
        return data

    def _save(self, state: dict[str, dict[str, dict[str, Any]]], now: float) -> None:
        state["requests"] = {
            rid: rec
            for rid, rec in state["requests"].items()
            if max(rec["expires_at"], rec.get("code_expires_at", 0)) > now
        }
        state["grants"] = {gid: g for gid, g in state["grants"].items() if g["expires_at"] > now}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Defence in depth only: the sandbox mask is the fence.
            platform_compat.restrict_dir_to_owner(self._path.parent)
        except OSError:
            pass
        atomic_write(self._path, json.dumps(state) + "\n", fsync=True)

    # ── the app: ask ──────────────────────────────────────────────────

    def authorize(self, query: Mapping[str, str]) -> tuple[str, Any]:
        """Record a request or ask again about one.

        Returns ``("pending", view)`` while the owner has not answered, else
        ``("redirect", location)``. Raises :class:`AuthError` when no redirect is safe.
        """
        client_id = query.get("client_id", "")
        client_name = query.get("client_name", "") or client_id
        redirect_uri = query.get("redirect_uri", "")
        state_param = query.get("state", "")
        challenge = query.get("code_challenge", "")
        if not (0 < len(client_id) <= 128 and _PRINTABLE_RE.match(client_id)):
            raise AuthError("invalid_request", "client_id is missing or malformed")
        if not (len(client_name) <= 80 and _PRINTABLE_RE.match(client_name)):
            raise AuthError("invalid_request", "client_name is malformed")
        if not _loopback_redirect(redirect_uri):
            raise AuthError("invalid_request", "redirect_uri must be a loopback address")
        if query.get("response_type") != "code":
            raise AuthError("invalid_request", "response_type must be code")
        if query.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.match(challenge):
            raise AuthError("invalid_request", "a PKCE S256 code_challenge is required")
        if not (0 < len(state_param) <= 512):
            raise AuthError("invalid_request", "state is missing or too long")
        scopes = query.get("scope", "").split()
        if not scopes or len(set(scopes)) != len(scopes) or not set(scopes) <= set(SCOPES):
            return "redirect", _with_query(
                redirect_uri, {"error": "invalid_scope", "state": state_param}
            )
        asked = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
            "state": state_param,
            "code_challenge": challenge,
        }
        now = self._clock()
        with _LOCK:
            state = self._load()
            request_id = query.get("request_id", "")
            if request_id:
                return self._ask_again(state, request_id, asked, now)
            waiting = [
                r
                for r in state["requests"].values()
                if r["status"] == "pending" and r["expires_at"] > now
            ]
            mine = sum(1 for r in waiting if r["client_id"] == client_id)
            if len(waiting) >= MAX_PENDING or mine >= MAX_PENDING_PER_CLIENT:
                raise AuthError("slow_down", "too many requests are waiting", 429, 60)
            request_id = "greq_" + secrets.token_urlsafe(12)
            code = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
            rec = {
                **asked,
                "client_name": client_name,
                "namespace": NAMESPACE,
                "user_code": f"{code[:4]}-{code[4:]}",
                "status": "pending",
                "created_at": now,
                "expires_at": now + REQUEST_TTL_SECS,
                "asked_at": now,
            }
            state["requests"][request_id] = rec
            self._save(state, now)
            return "pending", _request_view(request_id, rec)

    def _ask_again(
        self, state: dict[str, Any], request_id: str, asked: dict[str, Any], now: float
    ) -> tuple[str, Any]:
        rec = state["requests"].get(request_id)
        if rec is None or rec["expires_at"] <= now:
            raise AuthError("invalid_request", "the request is unknown or has expired")
        same = all(rec[k] == v for k, v in asked.items() if k != "code_challenge")
        if not (same and _ct_eq(rec["code_challenge"], asked["code_challenge"])):
            raise AuthError("invalid_request", "the parameters differ from the first request")
        if now - rec["asked_at"] < POLL_INTERVAL_SECS:
            raise AuthError(
                "slow_down",
                f"wait {POLL_INTERVAL_SECS} seconds between requests",
                429,
                POLL_INTERVAL_SECS,
            )
        rec["asked_at"] = now
        redirect_uri, state_param = rec["redirect_uri"], rec["state"]
        if rec["status"] == "pending":
            self._save(state, now)
            return "pending", _request_view(request_id, rec)
        if rec["status"] == "approved":
            code = "lc_" + secrets.token_urlsafe(32)
            rec.update(status="code_issued", code_hash=_digest(code))
            rec["code_expires_at"] = now + CODE_TTL_SECS
            self._save(state, now)
            return "redirect", _with_query(redirect_uri, {"code": code, "state": state_param})
        if rec["status"] == "denied":
            return "redirect", _with_query(
                redirect_uri, {"error": "access_denied", "state": state_param}
            )
        raise AuthError("invalid_request", "the authorization code was already sent")

    # ── the owner: answer, list, revoke ───────────────────────────────

    def pending(self) -> list[dict[str, Any]]:
        now = self._clock()
        with _LOCK:
            requests = self._load()["requests"]
        return [
            _request_view(rid, rec)
            for rid, rec in requests.items()
            if rec["status"] == "pending" and rec["expires_at"] > now
        ]

    def decide(
        self, request_id: str, approve: bool, scopes: list[str] | None = None
    ) -> dict[str, Any]:
        """The owner's answer. Raises ``KeyError`` (unknown), ``ValueError`` (not a subset
        of what was asked) or ``RuntimeError`` (already answered)."""
        now = self._clock()
        with _LOCK:
            state = self._load()
            rec = state["requests"].get(request_id)
            if rec is None or rec["expires_at"] <= now:
                raise KeyError(request_id)
            if rec["status"] != "pending":
                raise RuntimeError(rec["status"])
            granted = list(rec["scopes"]) if scopes is None else list(scopes)
            if approve and (not granted or not set(granted) <= set(rec["scopes"])):
                raise ValueError("scopes must be a non-empty subset of the requested scopes")
            rec["status"] = "approved" if approve else "denied"
            rec["granted_scopes"] = granted if approve else []
            self._save(state, now)
            return _request_view(request_id, rec)

    def revoke_grant(self, grant_id: str) -> bool:
        """Revoke one grant and every token under it. ``False`` when there is none."""
        now = self._clock()
        with _LOCK:
            state = self._load()
            grant = state["grants"].get(grant_id)
            if grant is None:
                return False
            grant["status"] = "revoked"
            self._save(state, now)
            return True

    # ── the app: tokens ───────────────────────────────────────────────

    def exchange_code(
        self, client_id: str, code: str, redirect_uri: str, verifier: str, issuer: str
    ) -> dict[str, Any]:
        now = self._clock()
        presented = _digest(code)
        with _LOCK:
            state = self._load()
            rec = next(
                (
                    r
                    for r in state["requests"].values()
                    if _ct_eq(r.get("code_hash", ""), presented)
                ),
                None,
            )
            if rec is None or rec["code_expires_at"] <= now:
                raise AuthError("invalid_grant", "the authorization code is wrong or expired")
            if rec["status"] != "code_issued":
                # A code used twice: whoever holds the tokens it gave may not be the app.
                grant = state["grants"].get(rec.get("grant_id", ""))
                if grant is not None:
                    grant["status"] = "revoked"
                    self._save(state, now)
                raise AuthError("invalid_grant", "the authorization code was already used")
            challenge = _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
            if not (
                rec["client_id"] == client_id
                and rec["redirect_uri"] == redirect_uri
                and _CHALLENGE_RE.match(verifier)
                and _ct_eq(rec["code_challenge"], challenge)
            ):
                raise AuthError(
                    "invalid_grant", "client_id, redirect_uri or code_verifier is wrong"
                )
            grant_id = "grt_" + secrets.token_urlsafe(12)
            grant = {
                "namespace": rec["namespace"],
                "kind": "local",
                "client_id": client_id,
                "client_name": rec["client_name"],
                "scopes": list(rec["granted_scopes"]),
                "status": "active",
                "created_at": now,
                "expires_at": now + REFRESH_MAX_SECS,
                "used_refresh": [],
            }
            state["grants"][grant_id] = grant
            rec.update(status="exchanged", grant_id=grant_id)
            return self._issue(state, grant_id, grant, grant["scopes"], issuer, now)

    def refresh(
        self, client_id: str, refresh_token: str, scope: str, issuer: str
    ) -> dict[str, Any]:
        now = self._clock()
        presented = _digest(refresh_token)
        with _LOCK:
            state = self._load()
            for grant_id, grant in state["grants"].items():
                if any(_ct_eq(used, presented) for used in grant["used_refresh"]):
                    # A rotated-out token came back: treat the grant as stolen.
                    grant["status"] = "revoked"
                    self._save(state, now)
                    break
                if not _ct_eq(grant["refresh_hash"], presented):
                    continue
                if (
                    grant["status"] != "active"
                    or grant["client_id"] != client_id
                    or min(grant["refresh_expires_at"], grant["expires_at"]) <= now
                ):
                    break
                scopes = scope.split() if scope else list(grant["scopes"])
                if not scopes or not set(scopes) <= set(grant["scopes"]):
                    raise AuthError("invalid_scope", "scope can only narrow the grant")
                grant["used_refresh"] = [*grant["used_refresh"], presented][-_USED_REFRESH_KEPT:]
                return self._issue(state, grant_id, grant, scopes, issuer, now)
        raise AuthError("invalid_grant", "the refresh token is wrong, used, expired or revoked")

    def _issue(
        self,
        state: dict[str, Any],
        grant_id: str,
        grant: dict[str, Any],
        scopes: list[str],
        issuer: str,
        now: float,
    ) -> dict[str, Any]:
        refresh_token = "lr_" + secrets.token_urlsafe(32)
        grant["refresh_hash"] = _digest(refresh_token)
        grant["refresh_expires_at"] = now + REFRESH_IDLE_SECS
        self._save(state, now)
        claims = {
            "iss": issuer,
            "client_id": grant["client_id"],
            "grant_id": grant_id,
            "scope": " ".join(scopes),
            "ent": [],
            "iat": int(now),
            "exp": int(now) + ACCESS_TTL_SECS,
            "jti": secrets.token_urlsafe(8),
        }
        head = _b64url_encode(json.dumps({"alg": "HS256", "typ": TOKEN_TYP}).encode())
        body = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
        return {
            "access_token": f"{head}.{body}.{self._sign(f'{head}.{body}')}",
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL_SECS,
            "refresh_token": refresh_token,
            "scope": claims["scope"],
            "crew_endpoint": issuer,
            "grant_id": grant_id,
        }

    def revoke_token(self, token: str) -> None:
        """The app gives up its grant (RFC 7009). An unknown token changes nothing."""
        claims = self._claims(token)
        presented = _digest(token)
        now = self._clock()
        with _LOCK:
            state = self._load()
            for grant_id, grant in state["grants"].items():
                held = [grant["refresh_hash"], *grant["used_refresh"]]
                is_access = claims is not None and claims.get("grant_id") == grant_id
                if is_access or any(_ct_eq(h, presented) for h in held):
                    grant["status"] = "revoked"
                    self._save(state, now)
                    return

    # ── the check a route runs ────────────────────────────────────────

    def _sign(self, signing_input: str) -> str:
        return _b64url_encode(hmac.new(self._key, signing_input.encode(), hashlib.sha256).digest())

    def _claims(self, token: str) -> dict[str, Any] | None:
        """The claims of a token this gateway signed, whatever its expiry; else ``None``."""
        try:
            head, body, signature = token.split(".")
            if not _ct_eq(signature, self._sign(f"{head}.{body}")):
                return None
            header = json.loads(_b64url_decode(head))
            claims = json.loads(_b64url_decode(body))
        except (ValueError, TypeError):
            return None
        if header != {"alg": "HS256", "typ": TOKEN_TYP} or not isinstance(claims, dict):
            return None
        return claims

    def verify(self, access_token: str) -> LocalCredential | None:
        """Who an access token speaks for, or ``None``: forged, expired, or its grant is
        revoked, expired or missing. Any failure to read the record refuses."""
        claims = self._claims(access_token)
        now = self._clock()
        if claims is None or not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
            return None
        try:
            with _LOCK:
                grant = self._load()["grants"].get(str(claims.get("grant_id")))
            if (
                grant is None
                or grant["status"] != "active"
                or grant["expires_at"] <= now
                or grant["client_id"] != claims.get("client_id")
            ):
                return None
            return LocalCredential(
                client_id=str(grant["client_id"]),
                grant_id=str(claims["grant_id"]),
                scopes=frozenset(str(claims.get("scope", "")).split()) & frozenset(grant["scopes"]),
                namespace=str(grant["namespace"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


def gateway_local_authorization() -> LocalAuthorization:
    """The gateway's own instance: the protected record and the gateway's signing key."""
    from kiro_crew import member_records
    from kiro_crew.dashboard.token_secret import _get_secret

    path = member_records.records_dir(LOCAL_GRANTS_KIND) / "state.json"
    return LocalAuthorization(path, _get_secret())


__all__ = [
    "LOCAL_GRANTS_KIND",
    "NAMESPACE",
    "SCOPES",
    "AuthError",
    "LocalAuthorization",
    "LocalCredential",
    "gateway_local_authorization",
]
