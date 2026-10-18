"""SSRF-safe, size-bounded HTTP primitives shared by every skill provider.

A skill provider fetches content that will subsequently influence what agents do
on the user's machine, so the network layer is part of the trust boundary rather
than plumbing underneath it. The controls here are the ones that layer owes every
provider, and they live in ONE module for a reason: a provider carrying its own
copy would drift, and the drift would be discovered as a bypass. ``skillsh.py``
and ``github.py`` both call in here; neither reimplements a check.

Three controls, each closing a distinct vector:

- **Pre-connect internal-address screen** (:func:`is_internal_url`) rejects a URL
  naming a private, loopback, link-local, reserved, multicast or unspecified
  address — including the alternate IPv4 encodings ``inet_aton`` accepts and
  :func:`ipaddress.ip_address` does not, which is how a plain-looking
  ``http://0xa9fea9fe/`` reaches the cloud instance-metadata endpoint.
- **Redirect host allowlist** (:func:`is_allowed_host`) is what a hostname screen
  cannot be: DNS is not resolved here (a blocking call, and rebinding would
  defeat it anyway), so a 30x chain is held to an explicit HTTPS host list
  instead. Each provider supplies its own list.
- **Bounded body read** (:func:`read_bounded`) checks the RUNNING total, so an
  oversized response is abandoned mid-stream rather than accumulated whole.

The screen and the allowlist are both applied to every redirect target BEFORE the
redirect is followed, so no TCP connection is made to a refused host.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from kiro_crew.security import canonicalize_ip

logger = logging.getLogger(__name__)

# Timeout for one HTTP request (seconds).
TIMEOUT_SECS = 5

# User-Agent for our requests (good citizenship).
USER_AGENT = "KiroCrew/1.0 (skill-discovery)"

# Maximum response body size (1 MiB) — ``read_bounded`` accumulates the body in
# memory, so this bounds the bytes one fetch RETAINS. It is not a peak-memory
# figure: joining the chunks and decoding them each allocate another copy. It is
# also what bounds DISK, because an install bundle is assembled out of these
# responses and the discover handler writes those files out under a looser 5 MiB
# guard of its own — so this ceiling is the one that binds first. Raise it only
# having accounted for both. SKILL.md files are typically <50 KB.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Per-chunk read size while draining a response body (64 KiB).
READ_CHUNK_BYTES = 64 * 1024

#: A callable a provider passes so its OWN module-level audit hook is the one
#: invoked when the internal-address screen fires. Signature:
#: ``(url, host, canonical_host) -> None``.
AuditHook = Callable[[str, str, str], None]


class FetchError(Exception):
    """Why a fetch produced nothing, for a caller that asked to know.

    Raised only when a fetch is called with ``raise_on_failure=True``; every
    other caller keeps the ``None`` contract. It carries facts a provider can
    turn into a user-facing reason -- an HTTP status, whether the host said its
    rate limit is used up, and when that limit resets -- and never the response
    body or the URL, so nothing a remote host wrote reaches the user through it.

    ``reason`` is one of a fixed set: ``http`` (a non-200 answer, see
    ``status``), ``network`` (no answer: refused redirect, DNS, timeout, reset),
    ``blocked`` (the internal-address screen refused the URL), ``too_large``,
    ``decode`` (not UTF-8 text, or not JSON).
    """

    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        rate_limited: bool = False,
        reset_at: int | None = None,
    ) -> None:
        super().__init__(reason if status is None else f"{reason} {status}")
        self.reason = reason
        self.status = status
        self.rate_limited = rate_limited
        self.reset_at = reset_at


def _http_failure(exc: urllib.error.HTTPError) -> FetchError:
    """Classify a non-2xx answer, reading only its status and rate-limit headers.

    A 429 is always a rate limit. A 403 is one only when the host says so with
    ``X-RateLimit-Remaining: 0`` (GitHub's primary limit) or a ``Retry-After``
    (its secondary limit); any other 403 is a plain refusal. The reset time comes
    from ``X-RateLimit-Reset`` (epoch seconds) or ``Retry-After`` (seconds from
    now), and is dropped when it does not parse.
    """
    headers = exc.headers
    get = headers.get if headers is not None else (lambda _name, _default=None: None)
    remaining = (get("X-RateLimit-Remaining") or "").strip()
    retry_after = (get("Retry-After") or "").strip()
    rate_limited = exc.code == 429 or (exc.code == 403 and (remaining == "0" or bool(retry_after)))
    reset_at: int | None = None
    if rate_limited:
        # ``Retry-After`` first: on a secondary limit GitHub also sends the PRIMARY
        # window's ``X-RateLimit-Reset``, which can be most of an hour away.
        reset_raw = (get("X-RateLimit-Reset") or "").strip()
        now = int(time.time())
        if retry_after.isdigit():
            reset_at = now + int(retry_after)
        elif reset_raw.isdigit():
            reset_at = int(reset_raw)
        # A reset outside the next day is not a time worth naming (and a huge
        # value would overflow ``time.gmtime``), so drop it.
        if reset_at is not None and not now <= reset_at <= now + 86400:
            reset_at = None
    return FetchError(
        "http",
        status=exc.code,
        rate_limited=rate_limited,
        reset_at=reset_at,
    )


def audit_ssrf_blocked(caller: str, url: str, host: str, canonical_host: str) -> None:
    """Emit a SEL audit event for a blocked SSRF-to-internal-address attempt.

    Best-effort: a security event log failure must never turn the SSRF *defense*
    into a crash, so every error is swallowed. ``sel`` is imported lazily to
    avoid a module-load cycle (sel -> ... -> skill_providers).
    """
    try:
        from kiro_crew.sel import sel  # circular import: sel -> ... -> skill_providers

        detail = host if host == canonical_host else f"{host} -> {canonical_host}"
        sel().log_api_access(
            caller=caller,
            operation="ssrf_blocked",
            outcome="blocked",
            source="skill_provider",
            resources=f"{detail} ({url[:120]})",
        )
    except Exception:  # noqa: BLE001 — auditing must never break the guard
        logger.debug("SEL audit of blocked SSRF failed", exc_info=True)


def is_internal_url(url: str, *, audit: AuditHook | None = None) -> bool:
    """Return True if *url* names a private/internal/loopback address.

    Covers IPv4 private ranges, IPv6 loopback/link-local/ULA, IPv6-mapped IPv4,
    the hex/octal/decimal/short-form IPv4 encodings the OS resolver accepts
    (normalized through ``canonicalize_ip`` first), and the ``localhost``
    hostname. Called BEFORE and AFTER redirect resolution, so both pre-connect
    and post-redirect SSRF are covered.

    A non-IP hostname passes THIS check — DNS is not resolved here. The redirect
    allowlist (:func:`is_allowed_host`) is what holds a hostname, so a redirect
    to an arbitrary DNS name that would resolve to a private address is refused
    by allowlist rather than by resolution.

    *audit* is called only when an IP LITERAL is refused. A URL naming an
    internal literal is a genuine SSRF attempt (a legitimate registry fetch
    never targets one), while a parse failure or a plain hostname is not, so
    only the literal case is worth an audit row.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname  # lowercased, brackets stripped for IPv6
        if not host:
            return True  # no host = suspicious, block

        # Block "localhost" explicitly (covers DNS that resolves to 127.0.0.1).
        if host == "localhost":
            return True

        # Normalize the alternate IPv4 encodings libc inet_aton accepts but
        # ipaddress.ip_address() rejects — hex (0x7f000001), octal (0177.0.0.1),
        # 32-bit decimal (2130706433) and short forms (127.1). Without this,
        # ip_address() raises ValueError on those, we fall through to the
        # hostname branch, and a redirect to e.g. http://2852039166/ (==
        # 169.254.169.254, the cloud instance metadata endpoint) reads as "not
        # internal" — an SSRF-to-metadata credential read. canonicalize_ip is
        # the same hardened resolver the bash-command metadata gate uses; it
        # returns the dotted quad for any encoding, or the input unchanged for a
        # real hostname.
        canonical_host = canonicalize_ip(host)

        try:
            ip = ipaddress.ip_address(canonical_host)
        except ValueError:
            return False  # not a literal IP — it's a hostname, see docstring

        internal = (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
        if internal and audit is not None:
            # canonical_host may differ from host (0xa9fea9fe -> 169.254.169.254),
            # so the audit row carries both.
            audit(url, host, canonical_host)
        return internal
    except Exception:
        return True  # parse failure = suspicious, block


def is_allowed_host(url: str, allowed_hosts: Iterable[str]) -> bool:
    """True iff *url* is HTTPS on an exactly-matching allowlisted host.

    Exact match, never a suffix test: ``skills.sh.evil.example`` and
    ``evilskills.sh`` both fail against an allowlist containing ``skills.sh``.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme == "https" and (parsed.hostname or "") in set(allowed_hosts)
    except Exception:
        return False


def _redirect_handler(
    allowed_hosts: Iterable[str],
    internal_check: Callable[[str], bool],
) -> urllib.request.HTTPRedirectHandler:
    """A redirect handler that only follows allowlisted HTTPS redirect targets.

    Both checks run BEFORE the redirect is followed, so no TCP connection is
    ever made to a refused target. The handler is built per call rather than
    defined once at module level because the allowlist is per-provider.
    """
    hosts = frozenset(allowed_hosts)

    class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            if internal_check(newurl) or not is_allowed_host(newurl, hosts):
                raise urllib.error.URLError(f"Blocked redirect to disallowed URL: {newurl[:80]}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _SafeRedirectHandler()


def open_guarded(
    req: urllib.request.Request,
    *,
    allowed_hosts: Iterable[str],
    internal_check: Callable[[str], bool],
    raise_http_errors: bool = False,
):
    """Open *req* with redirects held to *allowed_hosts*. None if blocked/failed.

    With ``raise_http_errors`` a non-2xx answer re-raises its ``HTTPError``
    instead of reading as None, so a caller can see the status and headers.
    Either way this is the one opener every fetch uses, so its guards cover all.
    """
    opener = urllib.request.build_opener(_redirect_handler(allowed_hosts, internal_check))
    try:
        return opener.open(req, timeout=TIMEOUT_SECS)
    except urllib.error.HTTPError:
        if raise_http_errors:
            raise
        return None
    except urllib.error.URLError:
        return None


def read_bounded(resp, max_bytes: int) -> bytes | None:
    """Read a response body up to *max_bytes*. None if exceeded.

    The check is against the RUNNING total, so an oversized body is abandoned
    mid-stream rather than accumulated whole — *max_bytes* bounds the bytes
    retained here, not just a verdict on the finished body.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            logger.warning("Response exceeded %d bytes, aborting read", max_bytes)
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def sync_fetch_bytes(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    internal_check: Callable[[str], bool],
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    raise_on_failure: bool = False,
) -> bytes | None:
    """Fetch *url* and return its bounded body. None on any failure.

    Blocking; callers reach it through :func:`fetch_json` / :func:`fetch_text` or
    their own executor offload.

    With ``raise_on_failure`` every failure raises :class:`FetchError` instead,
    so a provider can tell the user WHY it got nothing. The guards are the same
    on both paths; only the way a failure is reported differs.
    """
    if internal_check(url):
        if raise_on_failure:
            raise FetchError("blocked")
        return None
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        try:
            resp = open_guarded(
                req,
                allowed_hosts=allowed_hosts,
                internal_check=internal_check,
                raise_http_errors=raise_on_failure,
            )
        except urllib.error.HTTPError as exc:
            failure = _http_failure(exc)
            exc.close()
            raise failure from None
        if resp is None:
            if raise_on_failure:
                raise FetchError("network")
            return None
        try:
            if resp.status != 200:
                if raise_on_failure:
                    raise FetchError("http", status=resp.status)
                return None
            body = read_bounded(resp, max_bytes)
            if body is None and raise_on_failure:
                raise FetchError("too_large")
            return body
        finally:
            resp.close()
    except (urllib.error.URLError, OSError):
        if raise_on_failure:
            raise FetchError("network") from None
        return None


def sync_fetch_json(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    internal_check: Callable[[str], bool],
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    raise_on_failure: bool = False,
) -> Any | None:
    """Fetch *url* and decode it as JSON. None on any failure.

    ``raise_on_failure`` as in :func:`sync_fetch_bytes`.
    """
    data = sync_fetch_bytes(
        url,
        allowed_hosts=allowed_hosts,
        internal_check=internal_check,
        headers=headers,
        max_bytes=max_bytes,
        raise_on_failure=raise_on_failure,
    )
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if raise_on_failure:
            raise FetchError("decode") from None
        return None


def sync_fetch_text(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    internal_check: Callable[[str], bool],
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    raise_on_failure: bool = False,
) -> str | None:
    """Fetch *url* and decode it as UTF-8 text. None on any failure.

    A body that is not valid UTF-8 returns None rather than lossily decoding:
    every consumer here writes the result to a skill file, and a mojibake'd
    binary blob on disk is worse than a skipped file. ``raise_on_failure`` as in
    :func:`sync_fetch_bytes`.
    """
    data = sync_fetch_bytes(
        url,
        allowed_hosts=allowed_hosts,
        internal_check=internal_check,
        headers=headers,
        max_bytes=max_bytes,
        raise_on_failure=raise_on_failure,
    )
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if raise_on_failure:
            raise FetchError("decode") from None
        return None


async def run_off_loop(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any | None:
    """Run a blocking fetch in the default executor. None on any failure.

    Every network call in this module is blocking ``urllib``, so each provider
    coroutine hands it here rather than stalling the event loop. Failures are
    swallowed to None because that is the contract every caller already has for
    "the fetch did not produce anything" -- except :class:`FetchError`, which only
    a fetch that asked for it raises, and which passes through so that caller
    learns why.
    """
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    except FetchError:
        raise
    except Exception:
        logger.debug("Off-loop skill-provider fetch failed", exc_info=True)
        return None
