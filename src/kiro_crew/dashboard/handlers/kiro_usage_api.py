"""Direct RTS ``GetUsageLimits`` client for real Kiro credit usage.

Background — why this module exists
-----------------------------------
kiro-cli's ``/usage`` stdout stopped emitting the overage/total for org-managed
KIRO POWER accounts (it now caps at ``10000 of 10000 covered in plan``). The
dashboard credit pill scraped that text, so it froze at the plan limit even
when the account was well into overage. The real numbers still exist — the Kiro
IDE credit meter reads them from the ``GetUsageLimits`` API on the CodeWhisperer
runtime service (RTS). This module calls that same API so KiroCrew surfaces the
true used/limit/overage regardless of how kiro-cli reshuffles its stdout.

Whose credits are these?
------------------------
Several credentials can be readable at once (an IDE cache, a kiro-cli store, a
leftover file from a profile the user has since signed out of), and "unexpired"
does not mean "the one kiro-cli is actually using": a token for the previous
profile stays valid at the API until it expires on its own. Picking by fixed
path order therefore showed the OLD profile's credits after a profile switch,
and a gateway restart did not help because the order was the same on boot.

So the caller passes ``expected_arn`` — the profile ARN ``kiro-cli whoami``
reports for itself — and a candidate is only used when its own
ListAvailableProfiles ARN equals it. Credentials for any other account are
skipped without being spent on a GetUsageLimits call. When every candidate is
rejected the function fails closed (``None``) and the caller degrades to
scraping kiro-cli's own ``/usage`` output, which describes the right account by
construction. Showing a different account's number is the failure being
prevented; showing no number for one refresh is not.

There are TWO proofs of ownership, never an unverified path. When whoami reports a
profile ARN, a candidate qualifies by matching it. When it reports none — a Builder
ID account, or an older kiro-cli that prints no ``Profile:`` block, or a whoami that
failed outright — a candidate qualifies only by PROVENANCE: it must come from
kiro-cli's own auth store, where a token is the signed-in account's credential by
construction. That is the same trust argument that makes scraping kiro-cli's own
``/usage`` output safe, so it costs nothing to rely on here.

What is refused in the no-ARN case is a credential from the JSON SSO caches or
another product's store. Those may well be the same account, but nothing proves it,
and treating them as interchangeable is what served one Builder ID account's
leftover token as a different Builder ID account's balance.

Provenance-anchoring is what keeps the free API path available to every account
class. Requiring an ARN instead would push all profile-less accounts onto the text
scrape — which spends credits on every refresh, indefinitely, and hits the smallest
quotas hardest.

It is a read-only client: it reads the live bearer token that kiro-cli already
maintains and makes one authenticated call. The JSON SSO cache files live under
``~/.aws`` (a sensitive path), so they are read via
``hooks.safe_read_file_internal`` (which enforces ``is_sensitive_path`` and
emits a fail-closed SEL audit), not directly. On Linux, where those files are
not refreshed, the live token is read from the kiro-cli/amazon-q SQLite auth
store (``~/.local/share`` — not a sensitive path, so it cannot route through
``safe_read_file_internal``, but the credential read is still SEL-audited via
``hooks.emit_internal_read_audit`` and fails closed if the audit cannot be
recorded — opened read-only). It does NOT self-refresh the token via SSO-OIDC
(kiro-cli keeps the SQLite token fresh during normal use) — on 401/403 it fails
closed and the caller falls back to the legacy text scrape.

The HTTP call uses the Python standard library (``urllib``) rather than a
third-party client, so this module adds no new dependency to the public repo.

Security controls (fixed as acceptance criteria):
  1. Endpoint is hardcoded to AWS prod — never config-derived.
  2. TLS verification is always on (default ``ssl`` context: cert chain +
     hostname).
  3. Redirects are disabled (see ``_NoRedirect``) so the token can never be
     replayed to a redirected host.
  4. The bearer token is never logged (only HTTP status codes are).
  5. Any failure returns ``None`` (fail-closed) so the caller degrades to the
     text scrape rather than showing a fabricated number.
  6. The caller runs the returned dict through ``_redact_strings`` before it is
     cached and served.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from kiro_crew import hooks

logger = logging.getLogger(__name__)

# RTS / CodeWhisperer runtime endpoint (prod, IAD) — hardcoded, never derived
# from config or the token file (control 1).
_RTS_ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com"
_SVC = "com.amazon.aws.codewhisperer.runtime.AmazonCodeWhispererService"
_TARGET_GET_USAGE = f"{_SVC}.GetUsageLimits"
_TARGET_LIST_PROFILES = f"{_SVC}.ListAvailableProfiles"
_CONTENT_TYPE = "application/x-amz-json-1.0"
# A Kiro/Q user-agent is required for the IDE-origin code path; without it the
# API rejects the request.
_USER_AGENT = "AmazonQ-For-CLI/1.24.0 ua/2.0 os/linux lang/rust KiroCrew"
_TIMEOUT_SECS = 15
# Aggregate wall-clock budget across ALL candidate tokens. Each token can cost
# up to two 15s requests (ListAvailableProfiles + GetUsageLimits), and there can
# be several candidates, so without this the pill could spin for minutes before
# falling back to the text scrape. Once exceeded we stop trying tokens and let
# the caller degrade to the scrape.
_TOTAL_DEADLINE_SECS = 30

# Live bearer token sources kiro-cli / the Kiro IDE maintain.
#
# The JSON SSO cache files live under ``~/.aws`` (a sensitive credential path),
# so they are NOT read directly here — they are read via
# ``hooks.safe_read_file_internal(read_id)``, which enforces ``is_sensitive_path``
# and emits a fail-closed SEL audit. The read-ids below are registered in
# ``kiro_crew.hooks._INTERNAL_READ_ALLOWLIST``.
_JSON_TOKEN_READ_IDS = (
    "kiro_usage_api.sso_token_cli",
    "kiro_usage_api.sso_token_ide",
)

# kiro-cli's OWN SQLite auth store — the store kiro-cli itself authenticates
# from, and the authoritative live-token source (it refreshes this during normal
# use; the JSON files above are not rewritten on refresh). Being kiro-cli's own
# store is what makes a token here provably the SIGNED-IN account's credential,
# which is the basis of the source-anchored path in ``fetch_usage_limits`` — the
# same trust argument as scraping kiro-cli's own ``/usage`` output. On this host
# the running ``kiro-cli acp`` processes were observed holding descriptors on
# exactly this path.
# ``~/.local/share`` is not a sensitive credential path, so this read is a direct
# read-only sqlite open. auth_kv holds a JSON blob per key.
#
# The entries are deliberately UNGUARDED by ``sys.platform``: every candidate is
# existence-checked before it is opened, so a path that cannot exist on the
# running OS is inert, and a platform-independent module constant is what lets
# tests patch the tuple without becoming host-dependent.
#
# The Windows entry is the fixed default Roaming location, NOT resolved from
# ``%APPDATA%``. Membership here is a trust claim (``from_cli_store=True``)
# that rests on agent file tools being unable to write the store, and that
# fence (``_SENSITIVE_HOME_DIRS``) is home-anchored at exactly this path — so
# an APPDATA-resolved location either equals this entry or falls outside the
# fence and must not be trusted. A redirected Roaming profile therefore keeps
# the text-scrape fallback rather than gaining a forgeable trusted path; the
# same posture as the Linux entry, which does not follow ``XDG_DATA_HOME``
# redirection either.
_CLI_SQLITE_DBS = (
    Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3",
    Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3",
    Path.home() / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3",
)

# Auth stores belonging to OTHER products (amazon-q). Readable, and often the same
# account, but NOT kiro-cli's own credential — so a token from here can only be
# used when a matching profile ARN proves the account independently. It can never
# satisfy the source-anchored path.
_OTHER_SQLITE_DBS = (
    Path.home() / ".local" / "share" / "amazon-q" / "data.sqlite3",
    Path.home() / "Library" / "Application Support" / "amazon-q" / "data.sqlite3",
)
_SQLITE_TOKEN_KEYS = ("kirocli:odic:token", "codewhisperer:odic:token", "kirocli:social:token", "kirocli:external-idp:token")

# SEL audit label for the SQLite live-token read. Not an allowlist entry (that
# gate is for sensitive-path reads via safe_read_file_internal); this is only
# the audit event's tool_name so the credential access is traceable.
_SQLITE_AUDIT_READ_ID = "kiro_usage_api.sqlite_token"

# Range guard: reject absurd numbers so a corrupt/hostile response can't render
# as a wild figure. Real plans are in the thousands; a million-credit ceiling is
# comfortably above any real plan while still catching garbage.
_MAX_CREDITS = 1_000_000.0
_MAX_BONUS_GRANTS = 32
_MAX_BONUS_NAME_CHARS = 100

# Cap the RTS response body so an oversized or indefinitely-streamed response
# cannot exhaust memory or tie up a shared subprocess worker. The usage JSON is
# a few KB; 1 MB is comfortably above any real response.
_MAX_RESP_BYTES = 1_000_000

# Memoized account profile ARN (stable per account). Populated only from a
# definitive ListAvailableProfiles 200 (the value may be None for individual
# accounts); transient failures are never cached. See _list_profile_arn.
_PROFILE_ARN_CACHE: dict[str, str | None] = {}

# Size cap for the two profile caches below. They are keyed by token digest and
# never expire, so a long-lived gateway that sees many profile switches / token
# refreshes would otherwise accumulate one entry each. See _remember_profile.
_PROFILE_CACHE_MAX = 32

# Human profile/display name for the signed-in account, captured as a side
# effect of the same ListAvailableProfiles probe that yields the ARN and keyed
# by the same token digest. Surfaced as the usage dict's ``account`` field so
# the dashboard can show WHO the plan belongs to. Populated only alongside a
# found ARN (control-flow-coupled to _PROFILE_ARN_CACHE); may be None when the
# profile carries no name.
_PROFILE_NAME_CACHE: dict[str, str | None] = {}

# One WARN per api->text degradation transition (reset on the next success) so
# a persistently-failing API path is diagnosable at the default log level
# without spamming, and the legitimate no-token case stays at DEBUG.
_API_DEGRADED_WARNED = False


class _RequestError(Exception):
    """Transport-level failure (DNS, connect, TLS, timeout) from :func:`_post`.

    This is the analog of ``requests.exceptions.RequestException``. A non-2xx
    HTTP *response* is NOT a ``_RequestError``: it is returned as a ``_Resp`` so
    the caller can branch on ``status_code`` (mirroring requests, which does not
    raise on 4xx/5xx)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable HTTP redirects (security control 3).

    Returning ``None`` makes urllib raise the 3xx as an ``HTTPError`` rather
    than following it, so the bearer token is never replayed to a redirected
    host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class _Resp:
    """Minimal response view exposing only what the call sites use
    (``status_code`` and ``json()``), so the urllib transport is a drop-in for
    the previous requests-based one and the call sites stay unchanged."""

    __slots__ = ("status_code", "_text")

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self._text = text

    def json(self) -> dict:
        return json.loads(self._text) if self._text else {}


def _build_opener() -> urllib.request.OpenerDirector:
    """Build an opener with TLS verification ON (control 2) and redirects
    DISABLED (control 3).

    A fresh default SSL context verifies the certificate chain and hostname;
    ``_NoRedirect`` turns any 3xx into an error instead of replaying the bearer
    token to a redirected host."""
    ctx = ssl.create_default_context()  # verify_mode=CERT_REQUIRED, check_hostname=True
    return urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )


def _note_api_outcome(ok: bool, reason: str = "") -> None:
    """Track API-path health: WARN once (with the terminal reason) when tokens
    existed but every candidate failed, reset on the next success."""
    global _API_DEGRADED_WARNED
    if ok:
        _API_DEGRADED_WARNED = False
        return
    if not _API_DEGRADED_WARNED:
        _API_DEGRADED_WARNED = True
        logger.warning(
            "Kiro usage API degraded to text-scrape fallback (%s) — "
            "overage will not be visible until the API path recovers",
            reason,
        )


def _parse_iso(ts: object) -> datetime | None:
    """Parse an ISO8601 timestamp (handles trailing Z) to an aware datetime.

    kiro-cli emits nanosecond precision (9 fractional digits); Python 3.10's
    ``fromisoformat`` accepts at most 6, so the fraction is truncated to
    microseconds first. Without this the parse fails and an expired token would
    be treated as non-expiring (fail-open).
    """
    try:
        s = str(ts).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _expiry_if_unexpired(exp: object, now: datetime) -> datetime | None:
    """Return the parsed expiry only when it is present, parseable, and future.

    Deny-by-default, same contract as :func:`_unexpired` (which delegates here):
    a missing or unparseable expiry rejects the credential rather than treating
    it as non-expiring. The parsed value is returned rather than a bool so
    :func:`_candidate_tokens` can order candidates by it without re-parsing.
    """
    if not exp:
        return None
    parsed = _parse_iso(exp)
    if parsed is None or parsed <= now:
        return None
    return parsed


def _unexpired(exp: object, now: datetime) -> bool:
    """True only when the expiry is present, parseable, and in the future.

    Deny-by-default: a missing or unparseable expiry rejects the token rather
    than treating it as non-expiring. Both real stores always carry an expiry
    field, and the multi-candidate loop + text-scrape fallback absorb a false
    rejection gracefully — failing closed here costs nothing.
    """
    return _expiry_if_unexpired(exp, now) is not None


def _token_from_json(read_id: str, now: datetime) -> tuple[str, datetime] | None:
    """Return ``(accessToken, expiry)`` from a sanctioned JSON SSO cache read.

    The bytes are read via ``hooks.safe_read_file_internal`` (not a direct
    ``~/.aws`` read) so the sensitive-path deny rule + SEL audit apply. The
    expiry is returned alongside the token so candidates can be ordered by
    freshness; ``None`` means no usable unexpired credential here.
    """
    raw = hooks.safe_read_file_internal(read_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tok = data.get("accessToken") or data.get("access_token")
    exp = data.get("expiresAt") or data.get("expires_at")
    if not isinstance(tok, str) or not tok:
        return None
    expiry = _expiry_if_unexpired(exp, now)
    return (tok, expiry) if expiry is not None else None


def _token_from_sqlite(db: Path, now: datetime) -> tuple[str, datetime] | None:
    """Return ``(access_token, expiry)`` from a kiro-cli/amazon-q SQLite store.

    Opened read-only; the token value is never logged. This is the live token
    source on Linux, where the JSON cache file is not refreshed. The store is
    now classified sensitive (``is_sensitive_path``), so agent file tools cannot
    read it; this audited helper is the sole sanctioned reader. It cannot route
    through the byte-oriented ``hooks.safe_read_file_internal`` (sqlite needs to
    open the DB file), so the credential access is audited via
    ``hooks.emit_internal_read_audit`` (same SEL event + fail-closed carve-out
    as the JSON path): a successful read whose audit cannot be recorded is
    denied (returns None) rather than handing back an unaudited credential. A
    symlinked DB path is refused so the read cannot be redirected to another file.
    """
    try:
        if db.is_symlink() or not db.exists():
            return None
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except (sqlite3.Error, OSError):
        # OSError covers db.exists() on a permission-denied path — the
        # fail-closed contract must hold for it too.
        hooks.emit_internal_read_audit(_SQLITE_AUDIT_READ_ID, "unreadable")
        return None
    try:
        audited = False
        for key in _SQLITE_TOKEN_KEYS:
            try:
                row = con.execute(
                    "SELECT value FROM auth_kv WHERE key=?", (key,)
                ).fetchone()
            except sqlite3.Error:
                continue
            if not row:
                continue
            try:
                blob = json.loads(row[0])
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if not isinstance(blob, dict):
                continue
            tok = blob.get("access_token") or blob.get("accessToken")
            exp = blob.get("expires_at") or blob.get("expiresAt")
            if isinstance(tok, str) and tok:
                expiry = _expiry_if_unexpired(exp, now)
                if expiry is None:
                    # The credential blob WAS read even though it is expired —
                    # record that access so the audit trail covers every read
                    # of the store, not only reads that yield a usable token.
                    hooks.emit_internal_read_audit(_SQLITE_AUDIT_READ_ID, "expired")
                    audited = True
                    continue
                # Audit the live-token read; fail closed if it can't be recorded
                # (a logger line is not an SEL audit) so an unaudited credential
                # is never returned — the caller degrades to the text scrape.
                if not hooks.emit_internal_read_audit(_SQLITE_AUDIT_READ_ID, "success"):
                    return None
                return (tok, expiry)
        # Audit-on-every-outcome: the DB was opened, so record the access even
        # when it yielded no usable token (missing rows / malformed blob / query
        # failure) — otherwise an opened credential store would leave no trail.
        if not audited:
            hooks.emit_internal_read_audit(_SQLITE_AUDIT_READ_ID, "no_token")
        return None
    finally:
        con.close()


class _Candidate(NamedTuple):
    """A usable bearer token plus the provenance needed to trust it.

    ``from_cli_store`` is True only for a token read from kiro-cli's OWN auth
    store (:data:`_CLI_SQLITE_DBS`). That flag is a trust claim, not a label: it
    is what lets :func:`fetch_usage_limits` accept a credential for an account
    with no profile ARN, because a token in kiro-cli's own store is the
    signed-in account's credential by construction. Tokens from the JSON SSO
    caches and from other products' stores carry False — they may well be the
    same account, but nothing here proves it, so they are usable only when a
    matching ARN proves it independently.
    """

    token: str
    expiry: datetime
    from_cli_store: bool


def _candidate_tokens() -> list[_Candidate]:
    """Return all unexpired candidates to try, freshest expiry first (deduped).

    Path order is deliberately NOT the ranking. Every enumerated source can hold
    a valid credential at the same time, so ordering by path meant a leftover
    token from a profile the user has since signed out of was tried first and,
    being genuinely valid, was accepted — the "wrong profile" readout. Ordering
    by expiry descending puts the most recently issued credential first, because
    a token refreshed later carries a later expiry.

    This is a heuristic, not proof: sources can issue different lifetimes, so a
    long-lived stale token could still outrank a short-lived fresh one. Ordering
    only decides which proven candidate is tried first —
    ``fetch_usage_limits`` establishes ownership itself, by matching ARN or by
    provenance. Ties keep the original path precedence (``sorted`` is stable).

    Multiple candidates are returned (not just the first) because "unexpired" is
    not the same as "accepted": an unexpired-but-rejected token must not shadow a
    working one. Expired tokens are skipped and token values are never logged.
    v1 does not refresh — kiro-cli keeps its own store fresh during normal use.
    """
    now = datetime.now(timezone.utc)
    # Deduped by token value, keeping the latest expiry seen for it and ORing the
    # provenance: the same token can appear in more than one store, and if any of
    # them is kiro-cli's own store then it is kiro-cli's credential.
    freshest: dict[str, tuple[datetime, bool]] = {}

    def _add(found: tuple[str, datetime] | None, *, from_cli_store: bool) -> None:
        if not found:
            return
        tok, exp = found
        current = freshest.get(tok)
        if current is None:
            freshest[tok] = (exp, from_cli_store)
        else:
            freshest[tok] = (max(exp, current[0]), current[1] or from_cli_store)

    for read_id in _JSON_TOKEN_READ_IDS:
        _add(_token_from_json(read_id, now), from_cli_store=False)
    for db in _CLI_SQLITE_DBS:
        _add(_token_from_sqlite(db, now), from_cli_store=True)
    for db in _OTHER_SQLITE_DBS:
        _add(_token_from_sqlite(db, now), from_cli_store=False)
    ranked = sorted(freshest.items(), key=lambda kv: kv[1][0], reverse=True)
    return [_Candidate(tok, exp, own) for tok, (exp, own) in ranked]


def _load_bearer_token() -> str | None:
    """Return the single freshest available bearer token, or None.

    Thin convenience wrapper over :func:`_candidate_tokens` (first candidate,
    which is the freshest-expiry one). It discards provenance and applies NO
    ownership proof, so it must not be used to choose the credential a request is
    made with — ``fetch_usage_limits`` uses :func:`_candidate_tokens` directly so
    it can check each candidate's account (by ARN or by provenance) and fall
    through to the next source on a rejected-but-unexpired token.
    """
    candidates = _candidate_tokens()
    return candidates[0].token if candidates else None


def _post(token: str, target: str, payload: dict) -> _Resp:
    """POST an AWS-JSON request to the hardcoded RTS endpoint over urllib.

    TLS verification (control 2) and no-redirect (control 3) come from
    :func:`_build_opener`. A non-2xx HTTP *response* is returned as a ``_Resp``
    (requests parity — the caller branches on ``status_code``); only a
    transport-level failure raises :class:`_RequestError`.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _RTS_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": _CONTENT_TYPE,
            "X-Amz-Target": target,
            "User-Agent": _USER_AGENT,
        },
    )
    opener = _build_opener()
    try:
        with opener.open(req, timeout=_TIMEOUT_SECS) as resp:
            text = _read_capped(resp)
            return _Resp(getattr(resp, "status", 200) or 200, text)
    except urllib.error.HTTPError as e:
        # A non-2xx HTTP response (e.g. 403 FEATURE_NOT_SUPPORTED), or a 3xx that
        # _NoRedirect refused to follow, is a response — not a transport error.
        try:
            text = _read_capped(e)
        except Exception:  # noqa: BLE001 - body is best-effort only
            text = ""
        return _Resp(e.code, text)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _RequestError(str(e)) from e


def _read_capped(fp: object) -> str:
    """Read at most ``_MAX_RESP_BYTES`` from a response body.

    A bounded read so an oversized or indefinitely-streamed response cannot
    exhaust memory or tie up a shared subprocess worker. Returns ``""`` when the
    body exceeds the cap — which parses to an empty dict and fails closed to the
    text scrape.
    """
    data = fp.read(_MAX_RESP_BYTES + 1)  # type: ignore[attr-defined]
    if len(data) > _MAX_RESP_BYTES:
        logger.debug("Kiro usage API: response body exceeded %d bytes; discarded", _MAX_RESP_BYTES)
        return ""
    return data.decode("utf-8", "replace")


def _list_profile_arn(token: str) -> str | None:
    """Return the account's profile ARN, or None for non-enterprise accounts.

    Enterprise/IdC accounts (KIRO POWER etc.) must pass ``profileArn`` to
    GetUsageLimits or it returns 403 FEATURE_NOT_SUPPORTED.

    The ARN is account-stable per token, so a found ARN is memoized in
    ``_PROFILE_ARN_CACHE`` keyed by a token digest (never the token itself) to
    save one RTS round-trip per refresh. Two things are deliberately NOT
    cached: transient failures (network error, non-200), and a 200 with no
    profiles — an empty list can be post-login propagation lag on an
    enterprise account, so pinning arn=None would strand that account on the
    text fallback until restart. Both are re-probed on the next refresh, and
    each candidate token is probed for its own account (no cross-token reuse).
    """
    key = hashlib.sha256(token.encode()).hexdigest()[:16]
    if key in _PROFILE_ARN_CACHE:
        return _PROFILE_ARN_CACHE[key]
    try:
        r = _post(token, _TARGET_LIST_PROFILES, {})
    except _RequestError:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    profiles = body.get("profiles")
    if not isinstance(profiles, list):
        return None
    arn: str | None = None
    name: str | None = None
    for p in profiles:
        if not isinstance(p, dict):
            continue
        candidate = p.get("arn") or p.get("profileArn")
        if candidate:
            arn = candidate
            # Human label for the signed-in account. Bounded like the plan name
            # so a hostile/oversized value can't reach the cache/UI unbounded;
            # only a non-empty string qualifies.
            pname = p.get("profileName") or p.get("profileDisplayName")
            if isinstance(pname, str) and pname:
                name = pname[:100]
            break
    if arn is not None:
        _remember_profile(key, arn, name)
    return arn


def _remember_profile(key: str, arn: str, name: str | None) -> None:
    """Memoize a found ARN (and its co-cached display name) under a size cap.

    Entries are keyed by token digest and never expire, so each profile switch
    or token refresh adds one. The cap bounds that growth over a long-lived
    gateway; eviction is insertion-order (dicts preserve it), and both caches are
    evicted together so a name can never outlive the ARN it belongs to and be
    attributed to a different account.
    """
    if key not in _PROFILE_ARN_CACHE and len(_PROFILE_ARN_CACHE) >= _PROFILE_CACHE_MAX:
        oldest = next(iter(_PROFILE_ARN_CACHE), None)
        if oldest is not None:
            _PROFILE_ARN_CACHE.pop(oldest, None)
            _PROFILE_NAME_CACHE.pop(oldest, None)
    _PROFILE_ARN_CACHE[key] = arn
    _PROFILE_NAME_CACHE[key] = name


def _account_name(token: str) -> str | None:
    """Return the cached profile display name for ``token``, or None.

    Populated as a side effect of :func:`_list_profile_arn`; this getter never
    issues a request itself, so the ARN probe must have run first (it always
    does in ``fetch_usage_limits``). Keyed by the same token digest (never the
    token itself)."""
    return _PROFILE_NAME_CACHE.get(hashlib.sha256(token.encode()).hexdigest()[:16])


def _bounded(value: object) -> float | None:
    """Coerce to a finite float within the safe range, else None."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0 or f > _MAX_CREDITS:
        return None
    return f


def _map_response(data: dict) -> dict | None:
    """Translate a GetUsageLimits response into KiroCrew's canonical usage dict.

    Canonical shape (shared with the text-scrape fallback so consumers never
    branch on source):
      credits_used     total consumed this cycle (currentUsage)
      credits_plan     plan limit (usageLimit)
      credits_overage  overage above plan (currentOverages, else max(0, used-plan))
      credits_covered  in-plan portion = min(used, plan)
      percentage       used / plan * 100 (source-authoritative; may exceed 100)
      cost_usd         overage charges
      overage_rate     per-credit overage rate
      plan, resets     display strings
      source           "api"

    Returns None when no usable CREDIT breakdown with a finite used+limit exists.
    """
    if not isinstance(data, dict):
        return None
    breakdowns = data.get("usageBreakdownList")
    if not isinstance(breakdowns, list):
        breakdowns = []
    # Drop any non-dict entries so a malformed shape (e.g. [null]) can't raise
    # AttributeError and strand the caller on {"available": False} instead of
    # the text fallback.
    breakdowns = [b for b in breakdowns if isinstance(b, dict)]
    credit = None
    for b in breakdowns:
        if b.get("resourceType") == "CREDIT":
            credit = b
            break
    if credit is None:
        # Legacy/untyped responses omit resourceType — accept a single untyped
        # entry, but never a typed non-CREDIT entry (e.g. a TOKEN quota), which
        # would display an unrelated number as the credit total.
        for b in breakdowns:
            if not b.get("resourceType"):
                credit = b
                break
    if not credit:
        return None

    # Prefer the *-WithPrecision fields only when they are valid numbers; a
    # present-but-null/malformed precision value must fall back to the legacy
    # currentUsage/usageLimit rather than reject an otherwise-valid response.
    used = _bounded(credit.get("currentUsageWithPrecision"))
    if used is None:
        used = _bounded(credit.get("currentUsage"))
    plan = _bounded(credit.get("usageLimitWithPrecision"))
    if plan is None:
        plan = _bounded(credit.get("usageLimit"))
    if used is None or plan is None or plan <= 0:
        return None

    overage = _bounded(credit.get("currentOverages"))
    if overage is None:
        overage = max(0.0, used - plan)

    result: dict[str, object] = {
        "credits_used": used,
        "credits_plan": plan,
        "credits_overage": overage,
        "credits_covered": min(used, plan),
        "percentage": round(used / plan * 100, 1),
        "source": "api",
    }

    rate = _bounded(credit.get("overageRate"))
    if rate is not None:
        result["overage_rate"] = rate
    cost = _bounded(credit.get("overageCharges"))
    if cost is not None:
        result["cost_usd"] = cost

    sub = data.get("subscriptionInfo")
    if not isinstance(sub, dict):
        sub = {}
    plan_name = sub.get("subscriptionTitle") or sub.get("type")
    # Only a non-empty string may reach the dashboard as usage.plan — an object
    # or array here would be cached and handed to React, crashing the usage UI.
    # Bound the length defensively too.
    if isinstance(plan_name, str) and plan_name:
        result["plan"] = plan_name[:100]

    ts = data.get("nextDateReset")
    if ts is not None:
        try:
            result["resets"] = datetime.fromtimestamp(
                float(ts), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError, TypeError):
            pass

    # Best-effort bonus / free-trial pools. GetUsageLimits can carry additional
    # breakdown entries (promotional / welcome or free-trial pools) that are spent
    # BEFORE the plan; kiro-cli's text output shows the same thing as a "Bonus
    # Credits" section. The exact resourceType label is not documented, so match
    # conservatively on known bonus-like markers and never treat the primary
    # CREDIT entry or an unrelated quota (e.g. TOKEN) as bonus. Emitted only when
    # a finite used+limit pair exists, mirroring the text-scrape fields so the
    # dashboard never branches on source. Retain every bounded grant; the legacy
    # scalar fields mirror the first one for older clients.
    _bonus_markers = ("FREE_TRIAL", "FREETRIAL", "TRIAL", "BONUS", "PROMO", "GIFT", "WELCOME")
    bonus_credits: list[dict[str, object]] = []
    for b in breakdowns:
        if b is credit:
            continue
        rtype = str(b.get("resourceType") or "").upper()
        if not any(mk in rtype for mk in _bonus_markers):
            continue
        b_used = _bounded(b.get("currentUsageWithPrecision"))
        if b_used is None:
            b_used = _bounded(b.get("currentUsage"))
        b_limit = _bounded(b.get("usageLimitWithPrecision"))
        if b_limit is None:
            b_limit = _bounded(b.get("usageLimit"))
        if b_used is None or b_limit is None or b_limit <= 0:
            continue
        label = b.get("title") or b.get("displayName") or rtype.replace("_", " ").title()
        if not isinstance(label, str) or not label or not label.isprintable():
            continue
        bonus_credits.append(
            {
                "name": label[:_MAX_BONUS_NAME_CHARS],
                "used": b_used,
                "total": b_limit,
            }
        )
        if len(bonus_credits) >= _MAX_BONUS_GRANTS:
            break
    if bonus_credits:
        result["bonus_credits"] = bonus_credits
        first = bonus_credits[0]
        result["bonus_used"] = first["used"]
        result["bonus_limit"] = first["total"]
        result["bonus_label"] = first["name"]

    return result


def fetch_usage_limits(expected_arn: str | None) -> dict | None:
    """Fetch real credit usage via the direct RTS API. Synchronous (uses urllib).

    A candidate credential is used only when its ownership by the signed-in
    account is PROVEN. There are two proofs, and ``expected_arn`` selects which:

    * **ARN-anchored** (``expected_arn`` is a non-empty ARN — the one
      ``kiro-cli whoami`` reports for itself): a candidate qualifies when its own
      ListAvailableProfiles ARN is present and equal. This admits a credential
      from any readable source, including an IDE cache, because the ARN match is
      the proof.
    * **Source-anchored** (``expected_arn`` is ``None`` — the account has no
      profile ARN, or whoami could not be resolved): a candidate qualifies only if
      it came from kiro-cli's OWN auth store (``from_cli_store``). A token there is
      the signed-in account's credential by construction — the identical trust
      argument that makes scraping kiro-cli's own ``/usage`` output safe. Whatever
      ARN that credential reports is then used for the request payload, so an
      enterprise account on a kiro-cli that prints no ``Profile:`` block still gets
      its real overage figure instead of the lossier scrape.

    ``None`` is therefore NOT an unverified mode: it swaps ARN proof for
    provenance proof. What it will never do is accept a credential from the JSON
    SSO caches or another product's store, which is what let one Builder ID
    account's leftover token be served as a different Builder ID account's balance.

    Returns the canonical usage dict on success, or None on ANY failure (no
    token, no candidate proven, unparseable body, no CREDIT breakdown). The caller
    treats None as "fall back to the text scrape". This function never raises and
    never logs the token (controls 4 & 5).

    Call from async code via ``asyncio.get_running_loop().run_in_executor(...)``
    so the blocking HTTP call does not stall the event loop.
    """
    try:
        candidates = _candidate_tokens()
    except Exception:
        # Token acquisition must fail closed to the text scrape, never raise —
        # an escaping error would make the caller cache {"available": False}
        # and skip the fallback entirely.
        logger.debug("Kiro usage API: token acquisition failed", exc_info=True)
        return None
    if not candidates:
        logger.debug("Kiro usage API: no live bearer token available")
        return None
    reason = "unknown"
    deadline = time.monotonic() + _TOTAL_DEADLINE_SECS
    for candidate in candidates:
        token = candidate.token
        if time.monotonic() > deadline:
            # Bound total time so a host with several stale tokens can't stall
            # the pill for minutes; degrade to the text scrape instead.
            reason = f"aggregate deadline {_TOTAL_DEADLINE_SECS}s exceeded"
            logger.debug("Kiro usage API: %s; falling back to text scrape", reason)
            break
        try:
            if not expected_arn and not candidate.from_cli_store:
                # Source-anchored mode with nothing to prove this credential's
                # account. Skip WITHOUT probing or spending it: this is the exact
                # candidate class that produced the wrong-account readout.
                reason = "no credential from kiro-cli's own store"
                logger.debug(
                    "Kiro usage API: skipping a credential that is not from "
                    "kiro-cli's own auth store"
                )
                continue
            arn = _list_profile_arn(token)
            if expected_arn and (not arn or arn != expected_arn):
                # ARN-anchored mode: not provably the signed-in account. Skip
                # WITHOUT calling GetUsageLimits. A null ``arn`` is rejected rather
                # than compared, because None carries no identity — it is what a
                # transient profile lookup returns AND what every profile-less
                # account returns, so comparing it would match one account's
                # leftover credential against a different one.
                #
                # ARNs are not secret, but they identify an account, so only the
                # outcome is logged.
                reason = "no candidate credential proved it belongs to the signed-in profile"
                logger.debug(
                    "Kiro usage API: skipping a credential whose profile is absent or "
                    "does not match the signed-in account"
                )
                continue
            payload: dict[str, object] = {"origin": "AI_EDITOR"}
            if arn:
                payload["profileArn"] = arn
            try:
                r = _post(token, _TARGET_GET_USAGE, payload)
            except _RequestError as e:
                reason = f"request failed: {type(e).__name__}"
                logger.debug("Kiro usage API %s", reason)
                continue  # try the next candidate token
            if r.status_code != 200:
                # Fail over to the next token — do not log the token, only the status.
                reason = f"HTTP {r.status_code}"
                logger.debug("Kiro usage API returned %s", reason)
                continue
            try:
                body = r.json()
            except ValueError:
                reason = "non-JSON body"
                logger.debug("Kiro usage API returned %s", reason)
                continue
            mapped = _map_response(body)
            if mapped is not None:
                # Tag the usage with WHO the plan belongs to (profile display
                # name from the ListAvailableProfiles probe above). Absent for
                # individual Builder ID accounts, which have no profile.
                account = _account_name(token)
                if account:
                    mapped["account"] = account
                # Coupling metadata for the caller's identity check (private —
                # stripped before the payload is cached or served).
                #
                # These numbers now provably belong to the account kiro-cli is
                # signed in as: no candidate with a different ARN could have
                # reached this point. The ARN is still handed back because
                # matching on ``None`` (both sides have no profile — a Builder ID
                # account) is weaker than matching a specific ARN: two different
                # Builder ID accounts both present no ARN, so that case is
                # consistent but not proof of the same account. The caller's
                # ``_identity_matches_account`` encodes exactly that distinction
                # and is what decides whether an email may sit next to an overage
                # figure.
                mapped["_profile_arn"] = arn  # None for individual accounts
                _note_api_outcome(True)
                return mapped
            # A 200 with no usable CREDIT breakdown is a shape problem, not an auth
            # one — another token would return the same body, so stop here.
            _note_api_outcome(False, "no usable CREDIT breakdown in response")
            return None
        except Exception:  # noqa: BLE001 — never let an unexpected shape escape
            # Any unforeseen parsing/attribute error for one token must not
            # propagate: it would make _fetch_usage_bg cache {"available": False}
            # and skip the text-scrape fallback. Move on to the next candidate.
            reason = "unexpected error"
            logger.debug("Kiro usage API: unexpected error for a token candidate", exc_info=True)
            continue
    _note_api_outcome(False, f"all {len(candidates)} candidate credential(s) failed; last: {reason}")
    return None
