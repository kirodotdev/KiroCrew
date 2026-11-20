"""Local authorization: scoped, revocable Crew API tokens for an app on this machine.

OAuth 2.1 authorization code with PKCE (S256, public clients), served by this gateway
(the Crew API RFC, "Local authorization"). This module is the state and the rules; the
four routes are in :mod:`kiro_crew.dashboard.routes.local_authorization`.

* An app records a grant request. Nothing here lets the app answer it: only
  :meth:`LocalAuthorization.decide`, which only the owner's side calls, approves or denies.
* After the owner approves, the app gets a one-time code on its loopback redirect
  address and exchanges it, with the PKCE verifier, for a 5-minute access token and a
  rotating refresh token. A code or refresh token that shows up twice revokes the grant.
  The refresh token names its grant and its place in the rotation under a MAC, so the
  grant keeps one counter however often it rotates.
* :meth:`LocalAuthorization.verify` is the one check a route needs: it answers who the
  app is and what it may do, or ``None``. It reads the grant on every call, so a revoked
  grant is refused on the next request.

Requests and grants are one JSON record under ``member-records/local-grants/``, which no
sandbox can read or write. Codes are stored as SHA-256 digests only, and no refresh token
is stored at all.
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
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.dashboard.token_auth_tokens import _b64url_decode, _b64url_encode, _ct_eq
from kiro_crew.sel import sel

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
# The longest value of each field an app sends that is stored, echoed or audited.
CLIENT_ID_MAX = 128
CLIENT_NAME_MAX = 80
REDIRECT_URI_MAX = 512
STATE_PARAM_MAX = 512
#: The longest code, verifier, token or scope string the token and revoke routes hash,
#: compare or parse. An access token this gateway signs stays well under it.
PRESENTED_MAX = 4096

_STATE_MAX_BYTES = 4 * 1024 * 1024
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]+$")
# ``lr_<grant id>.<rotation>.<MAC>``; ASCII digits only, and no leading zero, so each
# (grant, rotation) has exactly one spelling.
_REFRESH_RE = re.compile(r"lr_(grt_[A-Za-z0-9_-]{16})\.(0|[1-9][0-9]{0,15})\.([A-Za-z0-9_-]{43})")
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


class UnknownRequest(Exception):
    """The owner answered a grant request that does not exist or has expired."""


class RequestAlreadyAnswered(Exception):
    """The owner answered a grant request that already has an answer."""


class ScopesNotRequested(Exception):
    """The owner approved scopes that are empty or not a subset of what the app asked."""


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


def _revoke(grant: dict[str, Any]) -> None:
    """Reduce *grant* to what keeps refusing it until its absolute expiry."""
    expires_at = grant["expires_at"]
    grant.clear()
    grant.update(status="revoked", expires_at=expires_at)


def _require_subject(caller: str) -> None:
    """An owner action is audited under who took it, so there is no default subject."""
    if not (isinstance(caller, str) and caller):
        raise ValueError("an owner action needs the subject of the request that made it")


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
        and "#" not in uri
    )


def _with_query(uri: str, params: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = "&".join(part for part in (parts.query, urlencode(params)) if part)
    return parts._replace(query=query).geturl()


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


def _kept(rec: Mapping[str, Any], grants: Mapping[str, Any], now: float) -> bool:
    """Whether the record still holds *rec*: until it and its code expire, or while
    the grant it led to has not expired."""
    grant = grants.get(rec.get("grant_id", ""))
    return max(rec["expires_at"], rec.get("code_expires_at", 0)) > now or (
        grant is not None and grant["expires_at"] > now
    )


#: What the owner is shown for each status the record keeps. A request whose code
#: was sent or redeemed is still an approved one to the owner.
_OWNER_STATUS = {"code_issued": "approved", "exchanged": "approved"}


def _request_for_owner(request_id: str, rec: Mapping[str, Any]) -> dict[str, Any]:
    """A grant request as the owner sees it: the app's view and how it was answered."""
    status = _OWNER_STATUS.get(rec["status"], rec["status"])
    view = _request_view(request_id, rec)
    view.update(
        status=status,
        decided_at=rec.get("decided_at"),
        decided_by=rec.get("decided_by"),
        grant_id=rec.get("grant_id"),
    )
    if status == "approved":
        view["granted_scopes"] = list(rec["granted_scopes"])
    return view


class LocalAuthorization:
    """Grant requests, grants and tokens, kept in one record at *path*."""

    def __init__(self, path: Path, signing_key: bytes, clock: Callable[[], float] = time.time):
        self._path = path
        self._key = hmac.new(signing_key, TOKEN_TYP.encode(), hashlib.sha256).digest()
        # Its own key, so a refresh MAC can never be read as an access token signature.
        self._refresh_key = hmac.new(self._key, b"refresh-token", hashlib.sha256).digest()
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

    def _encode(self, state: dict[str, dict[str, dict[str, Any]]], now: float) -> str:
        """The record to write, without what has expired. Raises when it would not fit."""
        state["grants"] = {gid: g for gid, g in state["grants"].items() if g["expires_at"] > now}
        state["requests"] = {
            rid: rec for rid, rec in state["requests"].items() if _kept(rec, state["grants"], now)
        }
        # Refuse growth rather than write a record the bounded reader cannot open. A
        # revocation only shrinks a grant, so it is never the write that is refused.
        serialized = json.dumps(state) + "\n"
        if len(serialized.encode("utf-8")) > _STATE_MAX_BYTES:
            raise ValueError("local authorization record is full")
        return serialized

    def _write(self, serialized: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Defence in depth only: the sandbox mask is the fence.
            platform_compat.restrict_dir_to_owner(self._path.parent)
        except OSError:
            pass
        atomic_write(self._path, serialized, fsync=True)
        # Persist the replacement and the record directory's own entry before acknowledging.
        fsync_dir(self._path.parent)
        fsync_dir(self._path.parent.parent)

    def _save(self, state: dict[str, dict[str, dict[str, Any]]], now: float) -> None:
        self._write(self._encode(state, now))

    # ── the app: ask ──────────────────────────────────────────────────

    def authorize(self, query: Mapping[str, str]) -> tuple[str, Any]:
        """Record a request or ask again about one.

        Returns ``("created", view)`` when this call recorded a new request,
        ``("pending", view)`` when an existing one still waits for the owner, else
        ``("redirect", location)``. Raises :class:`AuthError` when no redirect is safe.
        """
        client_id = query.get("client_id", "")
        sent_name = query.get("client_name", "")
        redirect_uri = query.get("redirect_uri", "")
        state_param = query.get("state", "")
        challenge = query.get("code_challenge", "")
        if not (0 < len(client_id) <= CLIENT_ID_MAX and _PRINTABLE_RE.match(client_id)):
            raise AuthError("invalid_request", "client_id is missing or malformed")
        if sent_name and not (len(sent_name) <= CLIENT_NAME_MAX and _PRINTABLE_RE.match(sent_name)):
            raise AuthError("invalid_request", "client_name is malformed")
        # The owner's view shows the full client_id next to this name.
        client_name = sent_name or client_id[:CLIENT_NAME_MAX]
        if len(redirect_uri) > REDIRECT_URI_MAX or not _loopback_redirect(redirect_uri):
            raise AuthError("invalid_request", "redirect_uri must be a short loopback address")
        if query.get("response_type") != "code":
            raise AuthError("invalid_request", "response_type must be code")
        if query.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.match(challenge):
            raise AuthError("invalid_request", "a PKCE S256 code_challenge is required")
        if not (0 < len(state_param) <= STATE_PARAM_MAX):
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
            return "created", _request_view(request_id, rec)

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
            _request_for_owner(rid, rec)
            for rid, rec in requests.items()
            if rec["status"] == "pending" and rec["expires_at"] > now
        ]

    def request(self, request_id: str) -> dict[str, Any]:
        """One request as the owner sees it, answered or not, for as long as the record
        keeps it. Raises :class:`UnknownRequest` otherwise."""
        now = self._clock()
        with _LOCK:
            state = self._load()
        rec = state["requests"].get(request_id)
        if rec is None or not _kept(rec, state["grants"], now):
            raise UnknownRequest(request_id)
        return _request_for_owner(request_id, rec)

    def decide(
        self,
        request_id: str,
        approve: bool,
        scopes: list[str] | None = None,
        *,
        caller: str,
    ) -> dict[str, Any]:
        """The owner's answer, audited under *caller*, the subject token auth gave whoever
        answered. Raises :class:`UnknownRequest`, :class:`RequestAlreadyAnswered` or
        :class:`ScopesNotRequested`; anything else means the record could not be used."""
        _require_subject(caller)
        now = self._clock()
        with _LOCK:
            state = self._load()
            rec = state["requests"].get(request_id)
            if rec is None or rec["expires_at"] <= now:
                raise UnknownRequest(request_id)
            if rec["status"] != "pending":
                raise RequestAlreadyAnswered(rec["status"])
            granted = list(rec["scopes"]) if scopes is None else list(scopes)
            if approve and (
                not granted
                or len(set(granted)) != len(granted)
                or not set(granted) <= set(rec["scopes"])
            ):
                raise ScopesNotRequested("scopes must be a non-empty subset of those requested")
            rec["status"] = "approved" if approve else "denied"
            rec["granted_scopes"] = granted if approve else []
            rec["decided_at"] = int(now)
            # The kind of caller, never a token. Only the Owner API answers today.
            rec["decided_by"] = {"kind": "owner", "via": "api"}
            # Encoded first: a record too full to take the answer fails before the audit.
            serialized = self._encode(state, now)
            sel().log_api_access(
                caller=caller,
                operation="local_auth.decide",
                outcome="approved" if approve else "denied",
                source="local-authorization",
                resources=f"request={request_id}",
                critical=True,
            )
            self._write(serialized)
            return _request_for_owner(request_id, rec)

    def revoke_grant(self, grant_id: str, *, caller: str) -> bool:
        """Revoke one grant and every token under it, audited under *caller*. ``False``
        when there is none. A grant already revoked stays so: nothing is written or audited."""
        _require_subject(caller)
        now = self._clock()
        with _LOCK:
            state = self._load()
            grant = state["grants"].get(grant_id)
            if grant is None or grant["expires_at"] <= now:
                return False
            if grant["status"] == "revoked":
                return True
            _revoke(grant)
            serialized = self._encode(state, now)
            sel().log_api_access(
                caller=caller,
                operation="local_auth.revoke",
                outcome="revoked",
                source="local-authorization",
                resources=f"grant={grant_id}",
                critical=True,
            )
            self._write(serialized)
            return True

    # ── the app: tokens ───────────────────────────────────────────────

    def exchange_code(
        self, client_id: str, code: str, redirect_uri: str, verifier: str, issuer: str
    ) -> dict[str, Any]:
        if max(map(len, (client_id, code, redirect_uri, verifier))) > PRESENTED_MAX:
            raise AuthError("invalid_request", "a field is too long")
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
            if rec is None:
                raise AuthError("invalid_grant", "the authorization code is wrong or expired")
            if rec["status"] != "code_issued":
                # A code used twice: whoever holds the tokens it gave may not be the app.
                grant = state["grants"].get(rec.get("grant_id", ""))
                if grant is not None:
                    _revoke(grant)
                    self._save(state, now)
                raise AuthError("invalid_grant", "the authorization code was already used")
            if rec["code_expires_at"] <= now:
                raise AuthError("invalid_grant", "the authorization code is wrong or expired")
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
                "client_id": client_id,
                "client_name": rec["client_name"],
                "scopes": list(rec["granted_scopes"]),
                "status": "active",
                "created_at": now,
                "expires_at": now + REFRESH_MAX_SECS,
                "refresh_n": 0,
            }
            state["grants"][grant_id] = grant
            rec.update(status="exchanged", grant_id=grant_id)
            return self._issue(state, grant_id, grant, grant["scopes"], issuer, now)

    def refresh(
        self, client_id: str, refresh_token: str, scope: str, issuer: str
    ) -> dict[str, Any]:
        if max(map(len, (client_id, refresh_token, scope))) > PRESENTED_MAX:
            raise AuthError("invalid_request", "a field is too long")
        now = self._clock()
        parts = self._refresh_parts(refresh_token)
        with _LOCK:
            state = self._load()
            grant = state["grants"].get(parts[0]) if parts else None
            if parts is not None and grant is not None and grant["status"] == "active":
                grant_id, rotation = parts
                if rotation < grant["refresh_n"]:
                    # A rotated-out token came back: treat the grant as stolen.
                    _revoke(grant)
                    self._save(state, now)
                elif (
                    rotation == grant["refresh_n"]
                    and grant["client_id"] == client_id
                    and min(grant["refresh_expires_at"], grant["expires_at"]) > now
                ):
                    scopes = scope.split() if scope else list(grant["scopes"])
                    if (
                        not scopes
                        or len(set(scopes)) != len(scopes)
                        or not set(scopes) <= set(grant["scopes"])
                    ):
                        raise AuthError("invalid_scope", "scope can only narrow the grant")
                    grant["refresh_n"] = rotation + 1
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
        refresh_token = self._refresh_token(grant_id, grant["refresh_n"])
        grant["refresh_expires_at"] = now + REFRESH_IDLE_SECS
        self._save(state, now)
        claims = {
            "iss": issuer,
            "client_id": grant["client_id"],
            "grant_id": grant_id,
            "scope": " ".join(scopes),
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

    def revoke_token(self, token: str) -> bool:
        """The app gives up its grant (RFC 7009). True when a grant was ended.

        An unknown token changes nothing.
        """
        if len(token) > PRESENTED_MAX:
            return False
        parts = self._refresh_parts(token)
        claims = None if parts else self._claims(token)
        grant_id = parts[0] if parts else str(claims.get("grant_id")) if claims else ""
        now = self._clock()
        with _LOCK:
            state = self._load()
            grant = state["grants"].get(grant_id)
            if grant is None or grant["status"] != "active":
                return False
            if parts is None or parts[1] <= grant["refresh_n"]:
                _revoke(grant)
                self._save(state, now)
                return True
            return False

    # ── the check a route runs ────────────────────────────────────────

    def _refresh_token(self, grant_id: str, rotation: int) -> str:
        body = f"{grant_id}.{rotation}"
        mac = hmac.new(self._refresh_key, body.encode(), hashlib.sha256).digest()
        return f"lr_{body}.{_b64url_encode(mac)}"

    def _refresh_parts(self, token: str) -> tuple[str, int] | None:
        """``(grant id, rotation)`` of a refresh token this gateway made, else ``None``."""
        match = _REFRESH_RE.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            return None
        grant_id, rotation = match.group(1), int(match.group(2))
        if not _ct_eq(token, self._refresh_token(grant_id, rotation)):
            return None
        return grant_id, rotation

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
    "RequestAlreadyAnswered",
    "ScopesNotRequested",
    "UnknownRequest",
    "gateway_local_authorization",
]
