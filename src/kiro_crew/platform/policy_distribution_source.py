"""Where the central governance ceiling comes from: its settings and its transports.

The tier's per-machine settings (the ``KIROCREW_POLICY_*`` names), the fetch request and
fetched-document records, the append-only scheme-to-fetcher registry with the built-in
``https``, ``http`` and ``file`` transports, and the resolution of the distribution
settings, the environment channel overlaid on the policy's own declaration. A new
transport or distribution setting goes here.

:mod:`kiro_crew.platform.policy_distribution` is the import path and the patch surface:
callers import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from kiro_crew.constants import ENV_TRUTHY
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    DEFAULT_FETCH_TIMEOUT_SECS,
    MAX_DURATION_SECS,
    MAX_POLICY_BYTES,
    UNAVAILABLE_DEGRADE,
    UNAVAILABLE_FAIL_CLOSED,
    PolicyDistribution,
)
from kiro_crew.platform_compat import path_writable_by_current_user, stat_writable_by_current_user

# ── Per-machine configuration (the fleet lever) ──
#: The central source.  Set this alone and the host is centrally governed.
POLICY_URL_ENV = "KIROCREW_POLICY_URL"
#: Request headers as a JSON object, e.g. ``{"Authorization": "Bearer …"}``.  The
#: per-machine credential channel, deliberately NOT a field in the published
#: document (see ``PolicyDistribution``).
POLICY_HEADERS_ENV = "KIROCREW_POLICY_HEADERS"
POLICY_REFRESH_ENV = "KIROCREW_POLICY_REFRESH_SECS"
POLICY_TIMEOUT_ENV = "KIROCREW_POLICY_TIMEOUT_SECS"
POLICY_MAX_AGE_ENV = "KIROCREW_POLICY_MAX_CACHE_AGE_SECS"
POLICY_UNAVAILABLE_ENV = "KIROCREW_POLICY_ON_UNAVAILABLE"
#: Set by the gateway on a child that must inherit the fleet ceiling WITHOUT being
#: handed the means to fetch it — see :func:`cache_only`.
POLICY_CACHE_ONLY_ENV = "KIROCREW_POLICY_CACHE_ONLY"

#: The enterprise build's policy source (RFC rfc-crew-enterprise-edition, D2). In an
#: enterprise build this is the ONLY source: :data:`POLICY_URL_ENV`, the other
#: ``KIROCREW_POLICY_*`` settings and any ``distribution.source`` in a local or fetched
#: document are ignored. PLACEHOLDER on a reserved domain that never resolves, so an
#: enterprise build fails closed at boot until it is replaced with the real endpoint
#: before the enable step (the RFC's Migration plan, phase 2). An oss build never reads it.
ENTERPRISE_POLICY_URL = "https://governance.example.invalid/crew/policy"

#: Every environment variable this tier reads, owned in one place.
#:
#: The set exists so the AGENT-DENY list cannot drift from it: every name here must be
#: in ``sandbox._AGENT_DENIED_ENV_KEYS`` by exact name, and a test asserts that rather
#: than trusting two hand-written lists to stay equal. Adding a variable here is
#: therefore the whole change — it cannot quietly stay agent-readable.
#:
#: The other two places that care do NOT read this tuple, deliberately:
#: ``apps/backend.py`` forwards nothing and sets cache-only mode instead, and the test
#: conftest sweeps the ``KIROCREW_POLICY_`` prefix so an autouse fixture need not import
#: this module.
POLICY_DISTRIBUTION_ENV_VARS: Tuple[str, ...] = (
    POLICY_URL_ENV,
    POLICY_HEADERS_ENV,
    POLICY_CACHE_ONLY_ENV,
    POLICY_REFRESH_ENV,
    POLICY_TIMEOUT_ENV,
    POLICY_MAX_AGE_ENV,
    POLICY_UNAVAILABLE_ENV,
)

#: The one non-IP hostname a plain-``http`` source may name.  Every other loopback
#: host is recognised by :func:`_is_loopback` through ``ipaddress`` rather than a
#: literal set: a hand-written set gets both ends wrong, refusing a legitimate
#: ``127.0.0.2`` relay while carrying an unreachable ``"[::1]"`` entry that
#: ``urlsplit().hostname`` has already stripped the brackets from.
_LOOPBACK_HOSTNAME = "localhost"


# ──────────────────────────────────────────────────────────────────────────
# The transport seam
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchRequest:
    """What a fetcher is asked for.  Everything a conditional GET needs."""

    url: str
    timeout_secs: float = DEFAULT_FETCH_TIMEOUT_SECS
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Validators from the cached copy, so an unchanged document costs no body.
    etag: str = ""
    last_modified: str = ""


@dataclass(frozen=True)
class FetchedPolicy:
    """What a fetcher returns.

    ``not_modified`` is how a fetcher reports that the cached copy is still
    current (an HTTP 304, or a file whose mtime has not moved).  It is a distinct
    field rather than an empty ``body`` because "the document is unchanged" and
    "the document is empty" must not be the same answer — the second is a
    corrupted push and has to be rejected.
    """

    body: bytes = b""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


#: A fetcher answers one :class:`FetchRequest`.  It MUST raise on any failure
#: (the caller turns that into the unreachable disposition) and MUST NOT return an
#: empty body with ``not_modified=False``.
PolicyFetcher = Callable[[FetchRequest], FetchedPolicy]

_FETCHERS: Dict[str, PolicyFetcher] = {}
_FETCHER_LOCK = threading.Lock()


def register_policy_fetcher(scheme: str, fetcher: PolicyFetcher) -> None:
    """Register the transport for one URL *scheme*.  Append-only.

    Mirrors ``governance.register_scope``: an edition that needs request signing
    for its own object store, or a management channel that is not HTTP, registers
    here at import time and inherits the cache, the signature verification, the
    refresh loop and every disposition unchanged.

    **A conflicting registration RAISES**, exactly as ``register_scope`` does and for the
    same reason: a typo must not silently shadow a built-in, and this registry decides
    which code fetches the security ceiling — the one place where shadowing is worst.
    There is deliberately no override flag; the precedent has none either, and an edition
    that genuinely needs to replace ``https`` should be a conversation rather than a
    keyword argument.
    """
    key = scheme.strip().lower()
    if not key or not key.isalnum():
        raise PlatformCompositionError(
            f"policy fetcher scheme {scheme!r} must be a non-empty alphanumeric scheme"
        )
    with _FETCHER_LOCK:
        if key in _FETCHERS:
            raise PlatformCompositionError(
                f"a policy fetcher for scheme {key!r} is already registered"
            )
        _FETCHERS[key] = fetcher


def registered_policy_schemes() -> Tuple[str, ...]:
    """Every scheme a policy source may currently name, sorted."""
    with _FETCHER_LOCK:
        return tuple(sorted(_FETCHERS))


def _fetcher_for(url: str) -> PolicyFetcher:
    """Resolve the fetcher for *url*, raising if its scheme has none.

    An unknown scheme is a configuration error, not a transient one: it will never
    start working, so it must not read as "unreachable" and quietly hand the host to
    the cache. It is the operator's typo (``htps://``) or a missing edition, and both
    want to be loud.

    Loud **when a fetch is attempted**, which is the honest bound: a cache inside
    :func:`_fetch_window` is served before any scheme lookup happens, so a typo'd
    source can go unreported for one window (and across a restart inside it). That is
    a diagnosability gap, not an enforcement one — the ceiling being served is still
    the one the fleet published — and closing it by validating the scheme ahead of the
    shortcut would trade a real cost (a boot that refuses when it had a perfectly good
    ceiling on disk) for an earlier log line.
    """
    scheme = _split_url(url).scheme.lower()
    with _FETCHER_LOCK:
        fetcher = _FETCHERS.get(scheme)
    if fetcher is None:
        raise PlatformCompositionError(
            f"policy source {url!r} names scheme {scheme!r}, which no fetcher handles "
            f"(registered: {', '.join(registered_policy_schemes()) or 'none'})"
        )
    return fetcher


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect rather than quietly changing origin.

    ``urlopen`` follows 3xx automatically and its default handler permits ``http``
    as a target, so a ``302`` to ``http://…`` would downgrade the transport
    carrying the security ceiling — and the scheme guard cannot see it, because
    that validates the URL we ASK for, not the one we end up at.  A fetched
    ceiling's whole trust basis is "TLS to the address the operator named", so a
    redirect off it contradicts the premise whether it is hostile or a CDN
    misconfiguration.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        # The target is deliberately NOT named. It comes from the ENDPOINT's ``Location``
        # header, so it is neither ours to publish nor covered by ``_redact_source``
        # (which only knows the configured source) -- and a redirect to a pre-signed URL
        # would carry its signature into the boot abort text and the log ring. The scheme
        # is what an operator needs to tell a downgrade from an ordinary move.
        scheme = _split_url(newurl).scheme or "unknown"
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"refusing to follow a policy-source redirect (to a {scheme} target)",
            headers,
            fp,
        )


def _split_url(url: str) -> urllib.parse.SplitResult:
    """``urlsplit`` that never raises.

    It raises ``ValueError`` on a malformed bracketed host (``https://[::1``), and the sites
    here are not validation sites — they are dispatch, redaction and posture, reached at
    runtime with whatever the environment channel supplied. The redaction one matters most:
    a sanitiser that crashes on a malformed source takes the error REPORT down with it, so
    the operator sees a traceback about URL parsing instead of the problem they have.

    An unparseable URL yields an all-empty result, which every caller already handles: the
    fetcher table refuses an empty scheme as unknown, the loopback probe sees no host, and
    the posture reports an empty scheme. A DECLARED source never reaches here malformed —
    ``PolicyDistribution.from_dict`` refuses it with a composition error naming the key.
    """
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return urllib.parse.SplitResult("", "", "", "", "")


def _is_loopback(host: str) -> bool:
    """Does *host* name this machine?

    ``ipaddress`` rather than a literal set, so the whole ``127.0.0.0/8`` block and
    every spelling of IPv6 loopback are covered — a management relay bound to
    ``127.0.0.2`` is as local as one on ``127.0.0.1``.  ``localhost`` is the one
    name that has to be special-cased, because it is not an address.
    """
    candidate = host.strip().lower()
    if candidate == _LOOPBACK_HOSTNAME:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Not an address at all — a DNS name that may or may not resolve here.
        # Resolving it would make the decision depend on the network the check
        # exists to distrust, so an unrecognised name is not loopback.
        return False


def _assert_transport_permitted(url: str) -> None:
    """Refuse a plain-``http`` source unless it names a loopback host."""
    parts = _split_url(url)
    if parts.scheme.lower() != "http":
        return
    if _is_loopback(parts.hostname or ""):
        return
    raise PlatformCompositionError(
        f"policy source {url!r} uses plain http to a non-loopback host; a centrally "
        "distributed security ceiling must arrive over https (a clear-text ceiling "
        "is substitutable in transit by anyone on the path)"
    )


def _open(req: urllib.request.Request, timeout: float):
    """Open *req* with redirects refused.  THE seam the network call goes through.

    A named function rather than an inline ``urlopen`` so a test can intercept the
    network at a place that cannot drift; patching ``urllib.request.urlopen``
    would silently stop intercepting the moment this used an opener instead.  The
    opener is built per call because an opener is mutable shared state, and one
    built at import time is something any other import can reconfigure.

    **A loopback request bypasses the proxy environment.**  ``build_opener`` installs
    a default ``ProxyHandler`` that reads ``HTTP_PROXY``, and urllib has no implicit
    loopback exemption — so on a host with a proxy configured (routine in corporate
    and containerised environments) a request to ``http://127.0.0.1`` is sent to the
    proxy in ABSOLUTE form, request headers and all. That would hand the fleet's
    bearer token to the proxy and let it answer with a substituted ceiling, which is
    precisely the exemption ``loopback_http.py`` exists to document for the gateway's
    own loopback calls. A remote ``https`` source keeps the default handler, because
    there a corporate proxy is the intended path.
    """
    # The URL is operator-supplied rather than a literal, which is the point of the
    # feature; ``_assert_transport_permitted`` has already refused every scheme but
    # https (and http to loopback), ``_NoRedirects`` refuses a 3xx that would change
    # origin, and the body is capped before it is decoded.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    return urllib.request.build_opener(*_opener_handlers(req.full_url)).open(req, timeout=timeout)


def _opener_handlers(url: str) -> "list[urllib.request.BaseHandler]":
    """The handler chain for *url*.  Pure, so it is testable without a socket."""
    handlers: list[urllib.request.BaseHandler] = [_NoRedirects()]
    if _is_loopback(_split_url(url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))
    return handlers


def _fetch_http(request: FetchRequest) -> FetchedPolicy:
    """Conditional GET over https (or http to loopback)."""
    _assert_transport_permitted(request.url)
    headers = {"Accept": "application/json", **dict(request.headers)}
    if request.etag:
        headers["If-None-Match"] = request.etag
    if request.last_modified:
        headers["If-Modified-Since"] = request.last_modified
    req = urllib.request.Request(request.url, method="GET", headers=headers)
    try:
        with _open(req, request.timeout_secs) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise PlatformCompositionError(
                    f"policy source {request.url!r} returned HTTP {status}"
                )
            # Read one byte past the cap so an over-large body is DETECTED rather
            # than silently truncated into a document that parses as something
            # narrower — or wider — than what was published.
            raw = resp.read(MAX_POLICY_BYTES + 1)
            etag = resp.headers.get("ETag", "") or ""
            last_modified = resp.headers.get("Last-Modified", "") or ""
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            # The validators we sent still match: the cached copy IS the current
            # document. Carry them forward so the next request stays conditional.
            return FetchedPolicy(
                etag=request.etag, last_modified=request.last_modified, not_modified=True
            )
        raise
    if len(raw) > MAX_POLICY_BYTES:
        raise PlatformCompositionError(
            f"policy source {request.url!r} exceeds the {MAX_POLICY_BYTES}-byte ceiling"
        )
    return FetchedPolicy(body=raw, etag=etag, last_modified=last_modified)


def _fetch_file(request: FetchRequest) -> FetchedPolicy:
    """Read a ``file://`` source — a read-only mount or a management drop point.

    **Neither the file NOR any ancestor directory may be writable by this process.**  A
    ``file://`` source is a DISTRIBUTION channel, and a source the agent can rewrite is
    not a source: it runs as the same uid, so a writable path would let it publish itself
    a ceiling that the refresher then installs without even a restart. The ancestor walk
    is what makes the check mean something — a ``0444`` file inside a writable directory
    is replaceable by unlink-and-recreate, so a leaf-only check would accept a forged
    read-only file. The field manual already tells
    operators to distribute to a "read-only, root-owned path"; this makes that a
    precondition instead of advice. An operator who genuinely wants a local, editable
    policy file has the channel designed for it — ``KIROCREW_SECURITY_POLICY``, which is
    read once at boot and TIGHTENS this tier.

    **Opened ONCE, and the validator is a DIGEST of the bytes read.**  Stat-then-read is
    two trips to a path an administrator (or anything sharing the mount) can replace in
    between, so the bytes read need not be the bytes measured: a swapped-in oversized
    file defeats the ceiling, and a FIFO makes ``read()`` block forever — a boot that
    hangs rather than fails. Requiring a regular file on the handle and capping the read
    at one byte past the ceiling closes both, and keeps the over-size case DETECTED
    rather than silently truncated into a document that parses as something narrower
    than what was published.

    The digest replaces an ``mtime:size`` validator, which a same-size replacement with a
    preserved timestamp (``cp -p``, a restored backup, a deliberate ``touch``) slips past
    — leaving the previous, potentially looser ceiling enforced indefinitely while every
    poll reports "unchanged". Re-reading a local file per interval costs nothing next to
    getting that wrong.
    """
    parts = _split_url(request.url)
    if parts.netloc and parts.netloc.lower() not in ("", "localhost"):
        raise PlatformCompositionError(
            f"policy source {request.url!r} names remote host {parts.netloc!r}; a "
            "file:// source must be a local path (mount it and name the mount point)"
        )
    path = Path(urllib.request.url2pathname(urllib.parse.unquote(parts.path)))
    # O_NONBLOCK so opening a FIFO cannot block before the regular-file check runs;
    # it is harmless on a regular file, where reads are never short for this reason.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PlatformCompositionError(
                f"policy source {request.url!r} is not a regular file; a fetched ceiling "
                "must be a file whose size can be bounded before it is read"
            )
        if stat_writable_by_current_user(st) or path_writable_by_current_user(path):
            raise PlatformCompositionError(
                f"policy source {request.url!r} is writable by the account Kiro Crew runs "
                "as. A file:// distribution source must be read-only to it, or an agent "
                "subprocess could publish its own ceiling. Use a root-owned path or a "
                "read-only mount; for a local, editable policy use "
                "KIROCREW_SECURITY_POLICY instead — that is the channel designed for "
                "it; it tightens this tier."
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            body = handle.read(MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)
    if len(body) > MAX_POLICY_BYTES:
        raise PlatformCompositionError(
            f"policy source {request.url!r} exceeds the {MAX_POLICY_BYTES}-byte ceiling"
        )
    validator = _body_digest(body)
    if request.etag and request.etag == validator:
        return FetchedPolicy(etag=validator, not_modified=True)
    return FetchedPolicy(body=body, etag=validator)


register_policy_fetcher("https", _fetch_http)
# The same fetcher serves both: ``_assert_transport_permitted`` is what makes the
# plain-http case loopback-only, so there is one code path and one place that decides.
register_policy_fetcher("http", _fetch_http)
register_policy_fetcher("file", _fetch_file)


# ──────────────────────────────────────────────────────────────────────────
# Source resolution — the env channel overlaid on the policy declaration
# ──────────────────────────────────────────────────────────────────────────


def cache_only() -> bool:
    """Is this process required to take the ceiling from the cache and never fetch?

    Set by the gateway on a child that boots its own platform context but must not be
    handed the means to reach the fleet's control plane — today, an app backend, which
    is arbitrary third-party code.

    It resolves a genuine tension rather than picking a side of it. Forwarding the
    source and its credential would let an installed app read the ceiling document the
    keystone fences the agent away from, and a pre-signed URL is itself the credential.
    Forwarding NOTHING would drop the child to a lower policy tier — a looser ceiling
    for exactly the code that most needs one. In this mode the child needs neither: the
    gateway has already written the last-known-good cache, so the cache IS the
    administrator's ceiling, and the child adopts it with no URL, no token and no
    network.

    The recorded-source check is skipped in this mode, and that is sound rather than a
    relaxation: there is one cache, the parent wrote it, and the parent applied the
    repoint rule when it did. The child is inheriting a decision, not making one.

    **An enterprise build puts every sandboxed process in this mode too**, with no flag:
    a Kiro Crew MCP server kiro-cli starts inside the agent sandbox enters the central
    tier (RFC D2) but can reach neither the sign-in nor the control plane, and the
    sandbox shows it the cache read-only (``sandbox_tiers.enterprise_policy_cache_view``).
    "Sandboxed" is the launcher's marker, which the entry points note before they drop it
    (``sandbox_launch_mark``).
    """
    if os.environ.get(POLICY_CACHE_ONLY_ENV, "").strip().lower() in ENV_TRUTHY:
        return True
    from kiro_crew import sandbox_launch_mark

    return _is_enterprise() and sandbox_launch_mark.launched_in_sandbox()


def _env_number(name: str, *, whole: bool) -> Optional[float]:
    """Parse a numeric env var, or ``None`` when unset/blank.

    A malformed value RAISES rather than reading as unset: an operator who wrote
    ``KIROCREW_POLICY_REFRESH_SECS=15m`` asked for a refresh, and silently giving
    them none is how a fleet stops following its admin without anyone noticing.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise PlatformCompositionError(f"{name}={raw!r} is not a number") from None
    # ``float("nan")`` and ``float("inf")`` both succeed. Every comparison below is
    # FALSE for NaN, so it would reach int() and raise there instead of here.
    if not math.isfinite(value):
        raise PlatformCompositionError(f"{name}={raw!r} must be a finite number")
    if value < 0:
        raise PlatformCompositionError(f"{name}={raw!r} must not be negative")
    if whole and value != int(value):
        raise PlatformCompositionError(f"{name}={raw!r} must be a whole number of seconds")
    # The same platform ceiling the declared channel is bounded by, and needed here too: a
    # merely-LARGE finite value passes the isfinite screen above (``1e12`` is a perfectly good
    # float) and then raises OverflowError inside ``Event.wait`` or the socket timeout, killing
    # the poller thread so the host silently stops receiving policy updates.
    if value > MAX_DURATION_SECS:
        raise PlatformCompositionError(
            f"{name}={raw!r} must not exceed {int(MAX_DURATION_SECS)} seconds (the longest "
            "interval this platform can wait for)"
        )
    return value


def request_headers() -> Dict[str, str]:
    """Per-machine request headers from :data:`POLICY_HEADERS_ENV`.

    A JSON object of string→string.  Malformed content raises: a bearer token that
    silently fails to be sent turns into a 401 the caller can only report as
    "unreachable", which sends the operator hunting a network problem that is
    really a quoting problem.
    """
    raw = os.environ.get(POLICY_HEADERS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise PlatformCompositionError(f"{POLICY_HEADERS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise PlatformCompositionError(
            f"{POLICY_HEADERS_ENV} must be a JSON object mapping header names to strings"
        )
    return {str(k): str(v) for k, v in parsed.items()}


def resolve_distribution(declared: Optional[PolicyDistribution] = None) -> PolicyDistribution:
    """The effective distribution settings: the env channel over *declared*.

    Per-setting rather than all-or-nothing, so a fleet can retune one value —
    redirect a host to a canary endpoint, lengthen an interval during an incident —
    without editing (and re-signing) the published document.

    **Enterprise build (RFC D2): no local channel.** The source is
    :data:`ENTERPRISE_POLICY_URL`, every ``KIROCREW_POLICY_*`` setting is ignored, and
    ``on_unavailable`` is ``fail_closed`` whatever *declared* says. The cadence, timeout
    and staleness bound come from *declared* alone, and every caller hands an
    enterprise build only the FETCHED document's block: ``load_security_policy`` passes
    no local declaration, and ``compose_tier_ladder`` keeps the central block.
    """
    base = declared or PolicyDistribution()

    if _is_enterprise():
        return replace(base, source=ENTERPRISE_POLICY_URL, on_unavailable=UNAVAILABLE_FAIL_CLOSED)

    source = os.environ.get(POLICY_URL_ENV, "").strip() or base.source
    refresh = _env_number(POLICY_REFRESH_ENV, whole=True)
    timeout = _env_number(POLICY_TIMEOUT_ENV, whole=False)
    max_age = _env_number(POLICY_MAX_AGE_ENV, whole=True)

    raw_disposition = os.environ.get(POLICY_UNAVAILABLE_ENV, "").strip()
    if raw_disposition and raw_disposition not in (UNAVAILABLE_FAIL_CLOSED, UNAVAILABLE_DEGRADE):
        raise PlatformCompositionError(
            f"{POLICY_UNAVAILABLE_ENV}={raw_disposition!r} must be "
            f"{UNAVAILABLE_FAIL_CLOSED!r} or {UNAVAILABLE_DEGRADE!r}"
        )

    return replace(
        base,
        source=source,
        refresh_interval_secs=int(refresh) if refresh is not None else base.refresh_interval_secs,
        timeout_secs=timeout if timeout is not None else base.timeout_secs,
        max_cache_age_secs=int(max_age) if max_age is not None else base.max_cache_age_secs,
        on_unavailable=raw_disposition or base.on_unavailable,
    )


def host_pinned_source() -> str:
    """The source this host names outside any document, or ``""``.

    The build-time :data:`ENTERPRISE_POLICY_URL` in an enterprise build, otherwise
    :data:`POLICY_URL_ENV`. A non-empty answer means a document's own
    ``distribution.source`` is not followed.
    """
    if _is_enterprise():
        return ENTERPRISE_POLICY_URL
    return os.environ.get(POLICY_URL_ENV, "").strip()


def _body_digest(body: bytes) -> str:
    """A stable digest of a policy document's bytes.

    Only ever compared for equality — never stored, never published — so the choice
    of hash is about collision resistance against an ACCIDENTAL match, not about
    signing. That is what ``identity.signature`` is for.
    """
    return hashlib.sha256(body).hexdigest()


def _is_enterprise() -> bool:
    """``kiro_crew.edition.is_enterprise()``, read at call time.

    Imported here rather than at module scope so the facade family binds no new project
    module name (``test_policy_distribution_facade`` keeps that set closed).
    """
    from kiro_crew.edition import is_enterprise

    return is_enterprise()
