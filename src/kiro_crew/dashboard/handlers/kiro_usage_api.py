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

# kiro-cli / amazon-q SQLite auth stores. This is the authoritative live-token
# source on Linux (kiro-cli refreshes it during normal use); the JSON files
# above are not rewritten on refresh. ``~/.local/share`` is not a sensitive
# credential path, so this read is a direct read-only sqlite open. auth_kv holds
# a JSON blob per key.
_TOKEN_SQLITE_DBS = (
    Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3",
    Path.home() / ".local" / "share" / "amazon-q" / "data.sqlite3",
    Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3",
    Path.home() / "Library" / "Application Support" / "amazon-q" / "data.sqlite3",
)
_SQLITE_TOKEN_KEYS = ("kirocli:odic:token", "codewhisperer:odic:token")

# SEL audit label for the SQLite live-token read. Not an allowlist entry (that
# gate is for sensitive-path reads via safe_read_file_internal); this is only
# the audit event's tool_name so the credential access is traceable.
_SQLITE_AUDIT_READ_ID = "kiro_usage_api.sqlite_token"

# Range guard: reject absurd numbers so a corrupt/hostile response can't render
# as a wild figure. Real plans are in the thousands; a million-credit ceiling is
# comfortably above any real plan while still catching garbage.
_MAX_CREDITS = 1_000_000.0

# Cap the RTS response body so an oversized or indefinitely-streamed response
# cannot exhaust memory or tie up a shared subprocess worker. The usage JSON is
# a few KB; 1 MB is comfortably above any real response.
_MAX_RESP_BYTES = 1_000_000

# Memoized account profile ARN (stable per account). Populated only from a
# definitive ListAvailableProfiles 200 (the value may be None for individual
# accounts); transient failures are never cached. See _list_profile_arn.
_PROFILE_ARN_CACHE: dict[str, str | None] = {}

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


def _unexpired(exp: object, now: datetime) -> bool:
    """True only when the expiry is present, parseable, and in the future.

    Deny-by-default: a missing or unparseable expiry rejects the token rather
    than treating it as non-expiring. Both real stores always carry an expiry
    field, and the multi-candidate loop + text-scrape fallback absorb a false
    rejection gracefully — failing closed here costs nothing.
    """
    if not exp:
        return False
    parsed = _parse_iso(exp)
    if parsed is None:
        return False
    return parsed > now


def _token_from_json(read_id: str, now: datetime) -> str | None:
    """Return an unexpired accessToken from a sanctioned JSON SSO cache read.

    The bytes are read via ``hooks.safe_read_file_internal`` (not a direct
    ``~/.aws`` read) so the sensitive-path deny rule + SEL audit apply.
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
    return tok if _unexpired(exp, now) else None


def _token_from_sqlite(db: Path, now: datetime) -> str | None:
    """Return an unexpired access_token from a kiro-cli/amazon-q SQLite store.

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
                if not _unexpired(exp, now):
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
                return tok
        # Audit-on-every-outcome: the DB was opened, so record the access even
        # when it yielded no usable token (missing rows / malformed blob / query
        # failure) — otherwise an opened credential store would leave no trail.
        if not audited:
            hooks.emit_internal_read_audit(_SQLITE_AUDIT_READ_ID, "no_token")
        return None
    finally:
        con.close()


def _candidate_tokens() -> list[str]:
    """Return all unexpired bearer tokens to try, in priority order (deduped).

    Order: sanctioned JSON SSO cache reads (honored when an IDE keeps them
    fresh), then the kiro-cli/amazon-q SQLite auth stores (the live, refreshed
    token on Linux). Expired tokens are skipped and token values are never
    logged.

    Multiple candidates are returned (not just the first) because "unexpired"
    is not the same as "accepted": an unexpired-but-rejected JSON/IDE token
    must not shadow a working SQLite token. ``fetch_usage_limits`` tries each in
    turn until one is accepted, so a stale-yet-unexpired token can no longer
    mask a good one. v1 does not refresh — kiro-cli keeps the SQLite token
    fresh during normal use.
    """
    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    tokens: list[str] = []

    def _add(tok: str | None) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for read_id in _JSON_TOKEN_READ_IDS:
        _add(_token_from_json(read_id, now))
    for db in _TOKEN_SQLITE_DBS:
        _add(_token_from_sqlite(db, now))
    return tokens


def _load_bearer_token() -> str | None:
    """Return the single freshest available bearer token, or None.

    Thin convenience wrapper over :func:`_candidate_tokens` (first candidate).
    ``fetch_usage_limits`` uses :func:`_candidate_tokens` directly so it can
    fall through to the next source on a rejected-but-unexpired token.
    """
    tokens = _candidate_tokens()
    return tokens[0] if tokens else None


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
    for p in profiles:
        if not isinstance(p, dict):
            continue
        candidate = p.get("arn") or p.get("profileArn")
        if candidate:
            arn = candidate
            break
    if arn is not None:
        _PROFILE_ARN_CACHE[key] = arn  # memoize only a found ARN, per token
    return arn


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

    # Best-effort bonus / free-trial pool. GetUsageLimits can carry a second
    # breakdown entry (a promotional / welcome or free-trial pool) that is spent
    # BEFORE the plan; kiro-cli's text output shows the same thing as a "Bonus
    # Credits" section. The exact resourceType label is not documented, so match
    # conservatively on known bonus-like markers and never treat the primary
    # CREDIT entry or an unrelated quota (e.g. TOKEN) as bonus. Emitted only when
    # a finite used+limit pair exists, mirroring the text-scrape fields so the
    # dashboard never branches on source.
    _bonus_markers = ("FREE_TRIAL", "FREETRIAL", "TRIAL", "BONUS", "PROMO", "GIFT", "WELCOME")
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
        result["bonus_used"] = b_used
        result["bonus_limit"] = b_limit
        label = b.get("title") or b.get("displayName") or rtype.replace("_", " ").title()
        if isinstance(label, str) and label:
            result["bonus_label"] = label[:100]
        break

    return result


def fetch_usage_limits() -> dict | None:
    """Fetch real credit usage via the direct RTS API. Synchronous (uses urllib).

    Returns the canonical usage dict on success, or None on ANY failure
    (no token, every candidate rejected, unparseable body, no CREDIT
    breakdown). The caller treats None as "fall back to the text scrape" — this
    function never raises and never logs the token (controls 4 & 5).

    Tries each candidate token in turn (JSON SSO cache, then SQLite auth store)
    until one is accepted, so an unexpired-but-rejected token no longer shadows
    a working one.

    Call from async code via ``asyncio.get_running_loop().run_in_executor(...)``
    so the blocking HTTP call does not stall the event loop.
    """
    try:
        tokens = _candidate_tokens()
    except Exception:
        # Token acquisition must fail closed to the text scrape, never raise —
        # an escaping error would make the caller cache {"available": False}
        # and skip the fallback entirely.
        logger.debug("Kiro usage API: token acquisition failed", exc_info=True)
        return None
    if not tokens:
        logger.debug("Kiro usage API: no live bearer token available")
        return None
    reason = "unknown"
    deadline = time.monotonic() + _TOTAL_DEADLINE_SECS
    for token in tokens:
        if time.monotonic() > deadline:
            # Bound total time so a host with several stale tokens can't stall
            # the pill for minutes; degrade to the text scrape instead.
            reason = f"aggregate deadline {_TOTAL_DEADLINE_SECS}s exceeded"
            logger.debug("Kiro usage API: %s; falling back to text scrape", reason)
            break
        try:
            arn = _list_profile_arn(token)  # None for individual (non-enterprise) accounts
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
    _note_api_outcome(False, f"all {len(tokens)} candidate token(s) failed; last: {reason}")
    return None
