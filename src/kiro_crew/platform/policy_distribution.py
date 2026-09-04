"""Central distribution of the Level-1 governance ceiling.

An enterprise IT admin owns **one** ``security_policy.json`` and every machine in
the fleet follows it: each host fetches the document from a central location,
keeps the last-known-good copy on disk, and re-fetches on an interval so a pushed
change binds without a restart, a redeploy, or a visit to the host.  This module
is that engine.  ``governance.PolicyDistribution`` is the parsed declaration;
``governance.load_security_policy`` calls in here for the fetch tier.

Where the source comes from
---------------------------

Two channels, and the split is deliberate:

* ``KIROCREW_POLICY_URL`` (plus the ``KIROCREW_POLICY_*`` siblings below) — the
  **fleet lever**, the same role ``KIROCREW_SECURITY_POLICY`` already plays.  A
  config-management push (Jamf / Intune / Ansible / Chef / Puppet) sets one
  variable and the host is centrally governed with no file to place and no
  package to rebuild.  It also carries the per-machine request credential, which
  a published document must not.
* ``distribution`` inside a policy that some LOWER tier already supplies — the
  **self-refresh** channel.  A fleet places one small bootstrap policy once (or
  an edition bundles it), and that document names where its own successors come
  from.  This is what makes "push a change to every instance" a property of the
  policy rather than of whatever placed it.

The env channel wins per setting, so a host can be redirected or have its
refresh interval changed without editing a signed document.

Precedence, and why this tier sits where it does
------------------------------------------------

``load_security_policy`` resolves, first present winning:

1. ``KIROCREW_SECURITY_POLICY`` — an explicit LOCAL file.
2. **this tier** — the centrally-distributed document.
3. the companion-bundled resource.
4. ``<data-home>/security_policy.json``.
5. none → ungoverned.

Tier 1 stays above this one because it is the **rollback lever**.  A bad central
push is the failure mode with the widest blast radius in the whole model — one
document, every host — and an operator recovering from it needs a channel that
outranks the thing that broke, reachable without waiting for the endpoint to be
fixed.  Tiers 3 and 4 sit below because they are what the fetched document is
*replacing*; a fleet that ships a bootstrap policy naming a source expects the
source to win, or the bootstrap could never be superseded.

Availability, and the one thing this must never do
--------------------------------------------------

A ceiling that a network can withhold is a ceiling that a network can also
*remove*, so the dispositions here are chosen around two failures that pull in
opposite directions:

* **The endpoint is unreachable.**  A cached last-known-good copy is served, and
  that is the normal answer — a host does not stop working because a bucket had a
  bad minute.  With no cache at all the host has no ceiling, and
  ``on_unavailable`` decides: ``fail_closed`` (the default) refuses to start,
  because a fleet that pointed a host at a central ceiling meant that ceiling to
  bind and "we could not tell" must not read as "run unbounded"; ``degrade``
  falls through to the next tier and records a governance incident.
* **The pushed document is bad.**  At boot there is nothing to fall back to, so a
  document that does not parse or does not verify fails like any other tier.
  **On a live refresh it is REJECTED and the running ceiling is kept** — a
  refresh validates a candidate through the same floor gates boot uses and only
  installs it if they all pass.  This is the asymmetry that keeps a typo from
  taking down a fleet that is already up.

Trust
-----

Fetched bytes are the tier where provenance matters most, and this module adds no
new trust root to establish it.  ``identity.signature`` is verified exactly as
for a file tier, against a key held in the operator-controlled admission policy —
never a key the fetched document supplies about itself.  The opt-in is that policy's
``require_policy_signature``, and this tier deliberately adds no per-tier flag of its
own: a document must not be the authority on whether it has to be authentic.  A
document that fails a mandated check is never cached, so a rejection does not persist
as a poisoned last-known-good.

The cache directory is on the ``security._SENSITIVE_HOME_DIRS`` keystone, which
is load-bearing rather than tidy: the cache holds both the document AND the
recorded source it came from, so an agent able to write it could point its own
ceiling somewhere it controls and have the next boot honour it.

Transport
---------

``register_policy_fetcher`` is an append-only seam mirroring
``governance.register_scope`` — including its refusal to silently shadow a
built-in: a scheme maps to a callable, and re-registering one raises.  The built-ins are
``https``, ``file``, and ``http`` restricted to loopback hosts.  An edition that
needs request signing for its own object store, or a management channel that is
not HTTP at all, registers a fetcher at import time and changes nothing else —
the cache, the validation, the refresh loop and the dispositions are shared.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterator, Mapping, Optional, Tuple

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import ENV_TRUTHY
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    DEFAULT_FETCH_TIMEOUT_SECS,
    MAX_DURATION_SECS,
    MAX_POLICY_BYTES,
    MIN_REFRESH_INTERVAL_SECS,
    SIGNATURE_VERIFIED,
    UNAVAILABLE_DEGRADE,
    UNAVAILABLE_FAIL_CLOSED,
    GovernanceCeiling,
    PolicyDistribution,
)
from kiro_crew.platform.governance_health import mark_governance_incident
from kiro_crew.platform_compat import (
    file_lock,
    is_link_or_junction,
    make_owner_only_dir,
    path_writable_by_current_user,
    stat_writable_by_current_user,
)

logger = logging.getLogger(__name__)

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

# ── Cache layout, under the data home and on the sensitive-path keystone ──
CACHE_DIR_LEAF = "policy_cache"
_CACHE_DOC_LEAF = "policy.json"
_CACHE_META_LEAF = "policy.meta.json"
#: Lock file serialising doc+meta pair mutations. Carries no content, so it is never
#: read and never validated as a policy -- and it must not collide with either leaf.
_CACHE_LOCK_LEAF = "policy.lock"

#: Refresh statuses reported by :func:`refresh_now`.  Names, not booleans, because
#: the CLI, the policy viewer and the audit trail all need to tell "nothing has
#: changed" apart from "a push was refused" — collapsing them would report a
#: rejected ceiling as a healthy no-op.
REFRESH_NOT_CONFIGURED = "not_configured"
REFRESH_UNCHANGED = "unchanged"
REFRESH_APPLIED = "applied"
REFRESH_REJECTED = "rejected"
REFRESH_UNREACHABLE = "unreachable"

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

#: When this tier last went to the network, across every caller.  See
#: :func:`_claim_fetch_slot` — ``load_security_policy`` is re-run per app callback,
#: so the tier needs its own bound independent of any one caller's discipline.
_last_fetch_attempt: float = 0.0
_FETCH_SLOT_LOCK = threading.Lock()

#: The cache directory this process has already created and tightened, or ``None``.
#: See :func:`_ensure_cache_dir`.
_cache_dir_ready: Optional[Path] = None

#: Digest of the policy document whose ceiling this process last INSTALLED.  See
#: :func:`_installed_digest` — it is what lets a 304 be judged against the ceiling in
#: effect rather than against a cache another process may have moved ahead.
_installed_body_digest: str = ""
_INSTALLED_LOCK = threading.Lock()


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
    policy file has the channel designed for it — ``KIROCREW_SECURITY_POLICY``, tier 1,
    which is read once at boot and outranks this tier anyway.

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
                "KIROCREW_SECURITY_POLICY instead — that is the channel designed for it, "
                "and it outranks this tier anyway."
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
    """
    return os.environ.get(POLICY_CACHE_ONLY_ENV, "").strip().lower() in ENV_TRUTHY


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
    """
    base = declared or PolicyDistribution()

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


# ──────────────────────────────────────────────────────────────────────────
# The last-known-good cache
# ──────────────────────────────────────────────────────────────────────────


def cache_dir() -> Path:
    """The cache directory, resolved lazily.

    Never a module-level ``config_dir()`` capture: importing this trust-root module
    must not fire the one-time data-home migration as a side effect, which is the
    same rule ``governance._policy_home_path`` follows.
    """
    return config_dir() / CACHE_DIR_LEAF


#: Query parameters that carry a signature rather than select a document, keyed by the
#: presigning scheme that defines them. Dropped from the source identity because they ROTATE:
#: whatever provisions the host re-issues the URL, and an identity that moves with the
#: credential discards the last-known-good on every rotation.
#:
#: A NAME list, not a heuristic on the value, and a small one: everything not named here stays
#: in the identity. That is the safe direction for both failure modes. An unknown presigning
#: scheme rotates the identity and costs the outage fallback until the next successful fetch —
#: bounded, and visible as a fetch. Wrongly dropping a document-SELECTING parameter costs the
#: opposite: ``?policy=team-a`` and ``?policy=team-b`` would share an identity, so a repoint to
#: a stricter policy would keep serving the looser one — indefinitely on a boot-only source,
#: which never re-fetches once a ceiling is established.
_PRESIGN_QUERY_PREFIXES = (
    # AWS SigV4 (S3 and anything behind it) and Google's copy of the same design.
    "x-amz-",
    "x-goog-",
)
#: Azure Blob SAS, which uses short opaque names rather than a namespaced prefix: signature,
#: service version, resource, permissions, and the start/expiry/protocol/identity set.
#:
#: Stripped ONLY when the query is positively identified as a SAS (see
#: :data:`_AZURE_SAS_MARKERS`), because these names are short and generic enough to be somebody
#: else's selector: ``?sp=team-a`` reads perfectly well as "policy = team-a", and collapsing it
#: with ``?sp=team-b`` is the exact failure the namespaced families cannot cause.
_AZURE_SAS_QUERY_NAMES = frozenset(
    {
        "sig",
        "sv",
        "sr",
        "sp",
        "st",
        "se",
        "spr",
        "sip",
        "si",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
        "srt",
        "ss",
    }
)

#: What makes a query positively a SAS rather than a coincidence: the signature AND the service
#: version, both of which every Azure SAS carries and neither of which a plain selector needs.
#: Requiring both keeps a lone ``?sig=…`` or ``?sv=…`` in the identity — the safe direction,
#: because an unrecognised rotation costs one fetch while a collapsed selector costs a superseded
#: ceiling staying in force.
_AZURE_SAS_MARKERS = frozenset({"sig", "sv"})


def _identity_query(query: str) -> str:
    """*query* with presigning parameters removed, order-normalised.

    Sorted so two spellings of the same selection compare equal: a URL is not required to
    preserve parameter order, and an identity that changed with it would discard the cache on a
    re-issue that merely reordered.
    """
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    drop_sas = _AZURE_SAS_MARKERS <= {name.lower() for name, _ in pairs}
    kept = []
    for name, value in pairs:
        lowered = name.lower()
        if lowered.startswith(_PRESIGN_QUERY_PREFIXES):
            continue
        if drop_sas and lowered in _AZURE_SAS_QUERY_NAMES:
            continue
        kept.append((name, value))
    return urllib.parse.urlencode(sorted(kept))


def _source_digest(source: str) -> str:
    """A stable, non-reversible stand-in for a source URL.

    The cache records WHICH source a copy came from so the repoint rule can refuse a
    retired endpoint's document. That question is an equality test, and an equality test
    does not need the plaintext — which matters because the URL may BE the credential: a
    pre-signed object-store link carries its signature in the query string, and this
    module's rule is that the source is emitted nowhere. Storing it in a file made the
    cache the one place that broke that rule, and the app backend reads that file.

    Not reversible in any useful sense: what an attacker would need is the signature, and
    that is an HMAC-SHA256 whose search space a preimage attempt cannot walk. An empty
    source digests to the empty string so "no source recorded" stays distinguishable from
    "recorded a source", rather than both hashing to the same constant.

    **The identity is what SELECTS the document, not what authenticates the request.** The
    fragment and the basic-auth PASSWORD are dropped (the username is not — it names the account,
    and two tenants at one host and path are two different documents), and so are the query parameters that carry a signature (see
    :data:`_PRESIGN_QUERY_NAMES`) — because a pre-signed URL is the documented shape for the
    environment channel and its signature ROTATES by design. Hashing the whole URL made every
    rotation look like a repoint, so the cache was discarded as a retired endpoint's copy and a
    host that then hit a transient outage had no last-known-good, aborting startup under the
    fail-closed default. Nobody had to do anything for that; it was the credential doing what
    credentials do.

    Every OTHER query parameter stays in, because a query can also select the document:
    ``?policy=team-a`` and ``?policy=team-b`` are different sources, and treating them as one
    would let a repoint to a stricter policy keep serving the looser one — indefinitely on a
    boot-only source, which never re-fetches once a ceiling is established. Kept parameters are
    sorted, so a re-issue that merely reorders them is not a repoint either.

    Scheme and host are lowercased (both are case-insensitive) and the path is not (it is not).
    """
    if not source:
        return ""
    parts = _split_url(source)
    host = (parts.hostname or "").lower()
    # ``.port`` is a LAZILY parsed property, so ``_split_url``'s guard does not cover it: it
    # raises ValueError for a non-numeric or out-of-range port, and this function is on the
    # boot path. The raw netloc port text is used instead when it cannot be parsed — an
    # identity does not need the number, only stability, and two spellings of an unusable port
    # are not a repoint between them.
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        _, _, raw_port = (parts.netloc or "").rpartition(":")
        port = f":{raw_port}" if raw_port else ""
    query = _identity_query(parts.query)
    # The USERNAME stays, the password goes — the same selector/credential split the query gets.
    # With basic auth the username names the account, and two tenants at one host and path are
    # two different documents: collapsing them would leave tenant A's possibly looser ceiling in
    # force after a repoint to tenant B. The password is the rotating half, so including it would
    # reintroduce exactly the discard-on-rotation this function exists to avoid.
    user = f"{parts.username}@" if parts.username else ""
    location = f"{parts.scheme.lower()}://{user}{host}{port}{parts.path}"
    if query:
        location = f"{location}?{query}"
    return hashlib.sha256(location.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedPolicy:
    """A last-known-good document plus the provenance needed to reuse it."""

    body: bytes
    #: Digest of the source, not the source. See :func:`_source_digest`.
    source_digest: str
    fetched_at: float
    etag: str = ""
    last_modified: str = ""

    def age_secs(self) -> float:
        """Seconds since the fetch, floored at 0.

        A clock that stepped backwards between the write and the read would
        otherwise produce a negative age that reads as impossibly fresh — or, once
        compared against a staleness bound, as a reason to refuse to boot.
        """
        return max(0.0, time.time() - self.fetched_at)

    def meta(self) -> "CachedMeta":
        """This entry's provenance without its body, for a metadata-only rewrite."""
        return CachedMeta(
            source_digest=self.source_digest,
            fetched_at=self.fetched_at,
            etag=self.etag,
            last_modified=self.last_modified,
            digest=_body_digest(self.body),
        )


@dataclass(frozen=True)
class CachedMeta:
    """The cache's provenance, without its body.

    Split out because most readers only need this half.  ``distribution_posture``
    wants an age and a source and is repolled by every open dashboard; the
    source-mismatch and staleness pre-checks want the same two before deciding
    whether the body is worth reading at all.  Reading a document that may be up to
    ``MAX_POLICY_BYTES`` to answer "how old is it" is the kind of waste that only
    shows up once a viewer is left open.
    """

    #: Digest of the source, not the source. See :func:`_source_digest`.
    source_digest: str
    fetched_at: float
    etag: str = ""
    last_modified: str = ""
    #: Digest of the document this metadata was written WITH.  See :func:`read_cache`.
    digest: str = ""

    def age_secs(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def read_cache_meta() -> Optional[CachedMeta]:
    """The cached document's provenance, or ``None`` when there is no usable cache.

    Never raises, for the reason :func:`read_cache` gives.  Still requires the
    DOCUMENT to be present — via a ``stat``, not a read — because the two files must
    agree: metadata alone has nothing to serve, and honouring it would report a
    cached ceiling that is not there.
    """
    directory = cache_dir()
    try:
        if not (directory / _CACHE_DOC_LEAF).stat().st_size:
            return None
        meta_raw = read_bytes_with_retry(directory / _CACHE_META_LEAF).decode("utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        logger.warning("policy cache metadata is not valid JSON; ignoring the cached ceiling")
        return None
    if not isinstance(meta, dict):
        return None
    fetched_at = meta.get("fetched_at")
    # A cache written before the source was digested carries the plaintext instead. Hash
    # it on read rather than discarding the entry: an upgrade must not throw away the
    # last-known-good copy, and the next write replaces the file with the digest form.
    recorded = meta.get("source_digest")
    if not isinstance(recorded, str) or not recorded:
        legacy = meta.get("source")
        recorded = _source_digest(legacy) if isinstance(legacy, str) and legacy else ""
    if not recorded or not isinstance(fetched_at, (int, float)):
        logger.warning("policy cache metadata is incomplete; ignoring the cached ceiling")
        return None
    return CachedMeta(
        source_digest=recorded,
        fetched_at=float(fetched_at),
        etag=str(meta.get("etag") or ""),
        last_modified=str(meta.get("last_modified") or ""),
        digest=str(meta.get("digest") or ""),
    )


def read_cache() -> Optional[CachedPolicy]:
    """The cached document, or ``None`` when there is no usable one.

    Never raises.  A cache is an optimisation over a fetch and a fallback for an
    outage; a corrupt one is neither, so it reads as absent and the caller fetches.
    Both files must agree: a document with no metadata has no provenance (we could
    not say what source it came from, and honouring it would let a stray file
    become the ceiling), and metadata with no document has nothing to serve.

    Reads through ``atomic_write.read_bytes_with_retry`` — the read-side twin of the
    ``replace_with_retry`` the writes go through — because the refresher thread
    publishes this cache while boot, the loader tier and the browser-polled posture
    read it. On Windows a read during a rename raises ``PermissionError``, which here
    would read as "no cache" and, on a host with an unreachable source, abort boot
    under the fail-closed default.
    """
    meta = read_cache_meta()
    if meta is None:
        return None
    try:
        body = read_bytes_with_retry(cache_dir() / _CACHE_DOC_LEAF)
    except (OSError, UnicodeError):
        return None
    if not body:
        return None
    # The two files are written separately, so overlapping writers can leave a NEW
    # document beside OLD metadata. That pair is not merely stale, it is wrong: the
    # metadata's ``source`` is what the repoint rule trusts, so a torn pair could hand a
    # repointed host the retired endpoint's document with the new endpoint's name on it.
    # The digest is written WITH the document, so a mismatch proves the tear and the
    # cache reads as absent — the caller then fetches, which is the safe answer.
    if meta.digest and _body_digest(body) != meta.digest:
        logger.warning("the policy cache document and its metadata disagree; ignoring it")
        return None
    return CachedPolicy(
        body=body,
        source_digest=meta.source_digest,
        fetched_at=meta.fetched_at,
        etag=meta.etag,
        last_modified=meta.last_modified,
    )


def write_cache(
    body: bytes,
    *,
    source: str,
    etag: str = "",
    last_modified: str = "",
    now: Optional[float] = None,
    expect_pair: Optional[Tuple[str, str]] = None,
) -> bool:
    """Persist a fetched document as the new last-known-good copy.

    Written through the shared :func:`kiro_crew.atomic_write.atomic_write` (never a
    hand-rolled temp + rename, which is how earlier writers in this repo silently
    lost the Windows rename retry) and document-before-metadata, so a process killed
    mid-write leaves either the previous consistent pair or a new document with old
    metadata — never metadata promising a document that is not there.
    ``restrict_to_owner`` also brings ``atomic_write``'s refusal to write through a
    symlinked parent, which matters more here than for an ordinary file: the parent
    is a trust root, and a link swapped in over it would redirect the write.

    The directory itself is created owner-only and INHERITABLY so, which is the
    control that actually matters — it holds the ceiling and the recorded source, so
    an agent able to write either could publish itself a ceiling with provenance.

    **Must not be called from the event loop.** ``replace_with_retry`` deliberately
    skips its Windows sharing-violation retry when it detects a running loop, so a
    write from there would lose it. Every caller today is boot (synchronous) or the
    refresher thread.

    Best-effort by design: a host that cannot write the cache is still governed by
    what it just fetched.  It simply has no outage fallback, which is worth a
    warning and not worth refusing to run.

    *expect_pair* makes the publish a **compare-and-swap**, checked inside the lock: the
    write happens only if the cache still holds the ``(body digest, source digest)`` the
    caller observed. ``("", "")`` expects no cache at all, and ``None`` (the default)
    publishes unconditionally. The pair rather than the body alone, because identical bytes
    can be published for a DIFFERENT source and overwriting that provenance with this
    call's own is exactly what the repoint rule then refuses at the next boot. It exists because a fetch is slow and a refresh is not the only
    writer: a refresher that fetched v2 over a slow link, while ``policy fetch --force``
    published v3, would otherwise write v2 over v3 and then INSTALL v2 — the ceiling
    rolling backward to a looser document, which is the one direction this tier must
    never move on its own.

    Returns whether the document was published. ``False`` means either the swap found the
    cache moved on (the caller must not install what it fetched) or the write failed.
    """
    directory = _ensure_cache_dir()
    if directory is None:
        return False
    payload = _meta_payload(
        source_digest=_source_digest(source),
        etag=etag,
        last_modified=last_modified,
        digest=_body_digest(body),
        now=now,
    )
    try:
        with _cache_write_lock(directory):
            if expect_pair is not None:
                observed = read_cache_meta()
                on_disk = _cache_pair(observed)
                # The PAIR, not the body digest alone. Identical bytes can be published
                # for a DIFFERENT source (a repoint whose document did not change), and a
                # body-only comparison would let this call overwrite that provenance with
                # its own -- recording the old source against bytes another process
                # published for the new one, which the next boot's repoint rule then
                # discards as not belonging to the source it resolves.
                if on_disk != expect_pair:
                    logger.info(
                        "another writer published a different ceiling while this fetch was "
                        "in flight; not overwriting it"
                    )
                    return False
            for path, data in (
                (directory / _CACHE_DOC_LEAF, body),
                (directory / _CACHE_META_LEAF, payload),
            ):
                _write_cache_file(path, data)
    except Exception:
        logger.warning("could not cache the fetched security policy", exc_info=True)
        return False
    return True


def touch_cache(
    meta: CachedMeta, *, etag: str = "", last_modified: str = "", now: Optional[float] = None
) -> None:
    """Record that the cached document was re-validated as current.

    A 304 proves the cached bytes ARE the published document right now, so its age
    must restart — otherwise a fleet with a stable policy and a staleness bound
    would refuse to boot for having successfully confirmed nothing changed.

    Takes the *meta* the caller already read and rewrites **only**
    ``policy.meta.json``.  A 304 is the common outcome on a stable fleet policy, and
    re-reading the document just to write the same bytes back would spend a full
    fsync'd write of up to ``MAX_POLICY_BYTES`` per interval per host to change one
    timestamp.  The document on disk is already correct, so the document-before-
    metadata ordering has nothing to preserve here.

    **Compare-and-swap, not a blind write.**  The lock serialises the two writers, but
    *meta* was read before it was taken, so a forced ``write_cache`` landing in between
    leaves this call about to stamp the OLD digest over metadata describing NEW bytes.
    Readers then see a torn pair and discard the cache — the last-known-good copy lost at
    the moment it is needed, during the outage that made the refresh fail. So the current
    metadata is re-read INSIDE the lock and the touch is skipped unless it still describes
    the document this caller validated. Skipping is right rather than merely safe: the
    writer that got there first already recorded a fresher fetch of the same source, so
    there is no age to restart.
    """
    directory = _ensure_cache_dir()
    if directory is None:
        return
    payload = _meta_payload(
        source_digest=meta.source_digest,
        etag=etag or meta.etag,
        last_modified=last_modified or meta.last_modified,
        # Carried forward, not recomputed: this rewrite does not touch the document, so
        # the digest still describes the bytes on disk.
        digest=meta.digest,
        now=now,
    )
    try:
        with _cache_write_lock(directory):
            current = read_cache_meta()
            if current is not None and (
                current.digest != meta.digest or current.source_digest != meta.source_digest
            ):
                logger.info(
                    "another writer advanced the policy cache while this 304 was in "
                    "flight; leaving its metadata alone"
                )
                return
            _write_cache_file(directory / _CACHE_META_LEAF, payload)
    except Exception:
        logger.warning("could not re-validate the cached security policy", exc_info=True)


def _meta_payload(
    *,
    source_digest: str,
    etag: str,
    last_modified: str,
    digest: str,
    now: Optional[float] = None,
) -> bytes:
    """The metadata document. Records a DIGEST of the source, never the source itself.

    See :func:`_source_digest`: the URL can BE the credential, and this file is read by
    the app backend.
    """
    return json.dumps(
        {
            "source_digest": source_digest,
            "fetched_at": float(now if now is not None else time.time()),
            "etag": etag,
            "last_modified": last_modified,
            "digest": digest,
        },
        sort_keys=True,
    ).encode("utf-8")


@contextlib.contextmanager
def _cache_write_lock(directory: Path) -> Iterator[None]:
    """Serialise every mutation of the cached doc/meta PAIR, across processes.

    The pair is two files and each is written atomically, but the pair is not: a
    forced ``write_cache`` (new document, new digest) interleaving with another
    process's 304 ``touch_cache`` (metadata only, carrying the digest it read before
    the write) persists NEW bytes against an OLD digest. Readers detect that and
    discard the cache as torn, so the cost is the last-known-good copy vanishing at
    the moment it is most needed — during the outage that made the refresh fail. Two
    writers is the ordinary case, not a corner: the gateway's refresher thread polls
    on its interval while ``kirocrew policy fetch --force`` runs from a shell.

    Exclusive, and shared with nobody: readers are NOT locked. They already
    revalidate the digest and retry, so they degrade to one transient cache miss,
    and locking them would put a boot behind a mutex that a hung refresher holds.

    Raises rather than proceeding unlocked if the lock cannot be taken. Both callers
    treat a raise as "no new cache this round", which preserves the existing good
    copy; writing unlocked could destroy it, and skipping is what a caller unable to
    write the cache at all already does.
    """
    lock_path = directory / _CACHE_LOCK_LEAF
    # Same hardening as the lock file in ``memory.py``: never follow a link to it and
    # require a lone regular inode, so an agent-planted link cannot redirect the open
    # onto a file it wants truncated. No O_TRUNC — a lock file carries no content.
    if is_link_or_junction(lock_path):
        raise OSError(f"refusing cache lock file (link or junction): {lock_path}")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"refusing cache lock file (not a lone regular inode): {lock_path}")
        with file_lock(fd, exclusive=True, required=True):
            yield
    finally:
        os.close(fd)


def _write_cache_file(path: Path, data: bytes) -> None:
    atomic_write(
        path,
        data,
        fsync=True,
        restrict_to_owner=True,
        # The directory's inheritable owner-only mode is the real control; a per-file
        # tighten is defence in depth and must not discard a ceiling we fetched.
        restrict_on_error="warn",
    )


def _ensure_cache_dir() -> Optional[Path]:
    """The cache directory, created owner-only on first use.  ``None`` if it cannot be.

    Created and tightened at most once per process.  The tighten is what matters and
    it matters at CREATION: re-applying it on every write costs a ``chmod`` on POSIX
    and, on Windows, a DACL write — which, with a 304 being the common poll outcome,
    would be filesystem work per refresh interval (and an unbounded SMB round-trip on
    a network-homed data home) to re-assert a DACL that has not changed.
    """
    global _cache_dir_ready
    directory = cache_dir()
    if _cache_dir_ready == directory:
        return directory
    try:
        make_owner_only_dir(directory)
    except Exception:
        logger.warning("could not create the policy cache directory %s", directory, exc_info=True)
        return None
    # Keyed on the PATH, not a bare bool: a test (and a re-homed process) moves the
    # data home, and a bool would skip the tighten for the new directory.
    _cache_dir_ready = directory
    return directory


def reset_fetch_window() -> None:
    """Forget when this tier last fetched, so the next call may go out again.

    Called by :func:`reset_process_state`.  Deliberately does NOT clear the
    installed-document digest: forgetting when we last fetched does not un-install the
    ceiling that is running.
    """
    global _last_fetch_attempt
    with _FETCH_SLOT_LOCK:
        _last_fetch_attempt = 0.0


def reset_process_state() -> None:
    """Clear every process-global this module keeps.  Test helper.

    All four are invisible to a test that does not know they exist, and each leaks a
    different way, so they are reset together rather than one at a time:

    * the fetch window would suppress a later test's fetch;
    * the installed-document digest would make a later test's 304 compare against a
      document some earlier test installed, so a poll adopts a cache it should have
      reported as unchanged;
    * the cache-directory memo holds a path from a previous test's data home, so a
      write would skip the owner-only tighten for the new one;
    * a post-install hook registered by one test would run on every later test's refresh,
      reaching into whatever subsystem it was written for.
    """
    global _cache_dir_ready, _installed_body_digest
    reset_fetch_window()
    with _INSTALLED_LOCK:
        _installed_body_digest = ""
    _cache_dir_ready = None
    _POST_INSTALL_HOOKS.clear()


# ──────────────────────────────────────────────────────────────────────────
# Parse + verify one fetched document
# ──────────────────────────────────────────────────────────────────────────


def parse_distributed_policy(body: bytes, *, source: str) -> GovernanceCeiling:
    """Turn fetched bytes into a verified ceiling, or raise.

    Raises ``PlatformCompositionError`` on anything that makes the document
    unusable: not JSON, not an object, not a valid policy, or — when provenance is
    mandated — not signed by a trusted issuer.  Every caller treats a raise as "do
    not adopt this document", which at boot means the unavailable disposition and
    on a live refresh means keeping the ceiling already installed.
    """
    from kiro_crew.platform.governance import (
        _policy_signature_required,
        _verify_policy_signature,
        parse_policy,
    )

    # One safe label for every mention of the source below, computed once. The signature
    # verifier records its label in the SEL audit trail, which is on the keystone and so
    # not agent-reachable — but a label reaching this module is a URL that may BE the
    # credential, and an audit record is not a place to put one either. Redacting here,
    # rather than teaching the verifier about URLs, keeps the rule where the URL is known;
    # for the file tiers the same parameter is the operator's own path and belongs in the
    # record as-is.
    label = _redact_source(source, source) if source else "the central policy source"

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlatformCompositionError(
            f"policy fetched from {label} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlatformCompositionError(f"policy fetched from {label} is not a JSON object")

    state = _verify_policy_signature(data, source=label)
    ceiling = parse_policy(data, signature_state=state)
    # Mandated provenance is enforced HERE as well as at the shared boot gate, so a
    # refused document never reaches the cache. The opt-in is ``require_policy_signature``
    # in the admission policy — the operator-controlled trust root that is on the
    # keystone. Deliberately NOT a key in the fetched document: a document must not be
    # the authority on whether it has to be authentic, or an attacker rewriting it
    # would simply clear the flag. The state test comes first because it is free,
    # while the opt-in reads a file.
    if state != SIGNATURE_VERIFIED and _policy_signature_required():
        raise PlatformCompositionError(
            f"policy fetched from {label} is {state}, but require_policy_signature is "
            "set in the admission policy; sign the published document and provision the "
            "issuer's trust key there"
        )
    return ceiling


# ──────────────────────────────────────────────────────────────────────────
# The boot tier
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _LoadOutcome:
    ceiling: Optional[GovernanceCeiling] = None
    #: Why no ceiling was produced, for the caller's log/incident.  Empty when
    #: distribution simply is not configured.
    reason: str = ""


def load_distributed_policy(
    declared: Optional[PolicyDistribution] = None,
) -> Optional[GovernanceCeiling]:
    """The tier: resolve the source, then serve the cache or fetch.

    Returns ``None`` when central distribution is not configured, or when it is
    configured, could not be established, and ``on_unavailable`` is ``degrade`` —
    in both cases the caller falls through to the next precedence tier.  Raises
    ``PlatformCompositionError`` when it could not be established under the
    ``fail_closed`` default, which aborts boot.

    Order within the tier is cache-first-when-fresh, and that is a boot-latency
    decision as much as an availability one: a warm host must not make startup wait
    on a network round trip it is about to make again in the background anyway.

    **This function is not called only at boot**, which is what the fetch window
    below exists for. ``load_security_policy`` is also re-run per app callback by
    ``mcp_gateway/app_call.py``, so without a bound an open dashboard would put one
    network round trip behind every app call. The window makes that at most one
    fetch per :func:`_fetch_window` seconds, from all callers combined.
    """
    dist = resolve_distribution(declared)
    if cache_only():
        return _load_cache_only(dist)
    if not dist.enabled:
        return None

    cached = read_cache()
    # A cache recorded against a DIFFERENT source is not this source's
    # last-known-good. Serving it would let a retired endpoint keep governing a
    # host that has been repointed — including a redirect made specifically to
    # replace a compromised source.
    if cached is not None and cached.source_digest != _source_digest(dist.source):
        logger.info("the cached ceiling came from a different source; ignoring it after a repoint")
        cached = None

    window = _fetch_window(dist)
    # A zero ``refresh_interval_secs`` means BOOT-ONLY, and it has to mean that on this
    # path too. ``_fetch_window`` floors the window at ``MIN_REFRESH_INTERVAL_SECS`` so a
    # zero never reads as "fetch on every call", but a floor is still a cadence: the
    # per-app-call re-read above would go back to the source every 60s, so an operator
    # who asked to freeze the ceiling for the process lifetime got a poller anyway — one
    # that could hand an app callback a LOOSENED document while the gateway kept the
    # ceiling it booted on. Once this process has established a central ceiling, a
    # boot-only source is served from the cache and never re-fetched.
    #
    # Gated on ``central_ceiling_installed()``, so boot's own first call still fetches
    # (nothing is installed yet), and a boot that degraded without establishing one
    # retries rather than freezing on a ceiling it never had. ``max_cache_age_secs`` is a
    # separate bound and still applies below, so this is not "trust bytes of any age".
    # ``refresh_now`` does not come through here, so `kirocrew policy fetch` remains the
    # operator's explicit lever in this mode.
    boot_only = dist.effective_refresh_interval() == 0 and central_ceiling_installed()
    fresh_enough = (
        cached is not None
        and (boot_only or cached.age_secs() < window)
        # Both declarations, tightest-wins: a bound set only in the published document
        # would otherwise be dropped at every restart, which is the case it exists for.
        and not _cache_is_too_old(dist, cached)
    )
    if fresh_enough and cached is not None:
        try:
            ceiling = parse_distributed_policy(cached.body, source=dist.source)
            # Record it: a ceiling served from the cache is still a ceiling this tier
            # installed, and the 304 comparison in ``refresh_now`` reads an EMPTY digest
            # as "we do not know what is running" — so without this a cache-booted
            # gateway would never notice another process advancing the cache.
            _record_installed(cached.body)
            return ceiling
        except PlatformCompositionError as exc:
            # The cache is unusable, so it is not a shortcut. Fall through to a
            # live fetch rather than failing: the published document may well be
            # fine and this copy merely stale-and-broken.
            #
            # Sanitised, and deliberately NOT `exc_info=True`: a traceback prints the
            # exception message, and a parse refusal names the source it refused --
            # which for a pre-signed URL IS the credential. The log ring is served by
            # `GET /api/logs`, so this is the same surface every other message in this
            # module is redacted for.
            logger.warning(
                "the cached ceiling is unusable; fetching: %s",
                _sanitize_detail(str(exc), dist.source),
            )

    if not _claim_fetch_slot(window):
        # Another caller already tried within this window and we have no fresh cache
        # to shortcut on, so the answer is whatever the cache can still offer. Going
        # to the network again would not produce a different result — the source was
        # just consulted — and on the per-app-call path it would produce one request
        # per call. ``_from_cache_on_outage`` applies the staleness bound, and a
        # ``None`` from it lands on the ordinary unavailable disposition.
        reason = _sanitize_detail(
            f"a fetch of {dist.source} was already attempted within the last {window}s",
            dist.source,
        )
        throttled = _from_cache_on_outage(dist, cached, reason)
        if throttled is not None:
            return throttled
        _unavailable(dist, reason)
        return None

    outcome = _load_by_fetch(dist, cached)
    if outcome.ceiling is not None:
        return outcome.ceiling
    _unavailable(dist, outcome.reason)  # raises under the fail-closed default
    return None


def _load_cache_only(dist: PolicyDistribution) -> GovernanceCeiling:
    """The ceiling from the cache alone, for a child that may not fetch.

    **Fails closed rather than falling through.**  The parent only puts a child in this
    mode when its OWN ceiling came from this tier
    (:func:`central_ceiling_installed`) — so an absent, stale or unusable cache here is
    not "this host has no central policy", it is "the parent had one and could not pass
    it on", which a successful fetch with a failed cache WRITE produces. Falling through
    would then start arbitrary third-party code under a local or absent ceiling on a
    host the administrator governs, and it would do it silently. Refusing is loud, and
    the failure is a disabled app rather than an unbounded one.
    """
    cached = read_cache()
    if cached is None:
        raise PlatformCompositionError(
            "cache-only policy resolution found no cached ceiling. The gateway is "
            "centrally governed, so this child cannot resolve the fleet policy and "
            "refuses to run under a local one. Check that the gateway could write "
            f"{cache_dir()}."
        )
    age = cached.age_secs()
    if _cache_is_too_old(dist, cached):
        bound = dist.max_cache_age_secs or _cached_max_cache_age(cached.body)
        raise PlatformCompositionError(
            f"the cached ceiling is {int(age)}s old, past the {bound}s "
            "staleness bound this fleet set; refusing to run a child under it."
        )
    try:
        # No source: this child was deliberately not given one, and the metadata records
        # only a digest. The parameter is used for error text, so "" is the honest value.
        ceiling = parse_distributed_policy(cached.body, source="")
    except PlatformCompositionError as exc:
        raise PlatformCompositionError(
            f"the cached ceiling is unusable in cache-only mode: {exc}"
        ) from exc
    _record_installed(cached.body)
    return ceiling


def effective_refresh_interval() -> int:
    """The poll cadence in force, or 0 for a boot-only source.

    Exists for the CLI, which has to tell an operator WHEN a running gateway will pick up
    what it just published: on the next cycle if there is one, at next start if there is
    not. ``refresh_now`` installs the ceiling in the calling process, and for
    ``kirocrew policy fetch`` that process exits immediately — so "applied" without a
    qualifier would claim something the command cannot do.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        return resolve_distribution(active_policy_distribution()).effective_refresh_interval()
    except PlatformCompositionError:
        return 0


def effective_max_cache_age() -> int:
    """The staleness bound in force, wherever it was declared.

    A cache-only child is handed this rather than the raw environment variable, because
    the bound is just as likely to come from the FETCHED document's ``distribution``
    block — and reading only the env var would give the child no bound at all on a fleet
    that set one in the policy, so it would accept an arbitrarily stale ceiling.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        return resolve_distribution(active_policy_distribution()).max_cache_age_secs
    except PlatformCompositionError:
        return 0


def central_ceiling_installed() -> bool:
    """Did THIS process's ceiling come from the central tier?

    The gateway asks before putting a child in cache-only mode. It is the difference
    between "the fleet policy is what governs here, pass it on" and "this host is
    running on a local tier or ungoverned", and only the first is a state a child should
    refuse to start without.
    """
    return bool(_installed_digest())


def _env_pins_the_source() -> bool:
    """Is the address coming from the environment rather than from a document?

    When it is, a document's own ``distribution.source`` is already ignored:
    :func:`resolve_distribution` lets the environment win per setting, precisely so
    whatever provisions the host owns the address. So a declaration that differs is not a
    migration this host would follow, and refusing the document over it would break the
    ordinary canary — one host pinned elsewhere by env while the fleet publishes its
    canonical address.
    """
    return bool(os.environ.get(POLICY_URL_ENV, "").strip())


def _declared_source(body: bytes) -> str:
    """The ``distribution.source`` a document declares for itself, or ``""``.

    Read straight from the JSON for the reason :func:`_cached_max_cache_age` gives: this
    runs before the document has been validated, so anything unreadable or not a non-empty
    string means "declares no source of its own" and the parse that follows reports it.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        declared = data["distribution"]["source"]
    except Exception:
        return ""
    return declared.strip() if isinstance(declared, str) else ""


def _cache_pair(meta: Optional["CachedMeta"]) -> Tuple[str, str]:
    """The cache's identity for a compare-and-swap: ``(body digest, source digest)``.

    Both halves, because either can move independently: a new document under the same
    source, or the SAME bytes recorded against a new source after a repoint. A swap that
    compared only the body would silently overwrite the second case's provenance.
    ``("", "")`` means "no cache", which is a state a caller can legitimately expect.
    """
    if meta is None:
        return ("", "")
    return (meta.digest, meta.source_digest)


def _cached_max_cache_age(body: bytes) -> int:
    """A ``max_cache_age_secs`` the CACHED document declares for itself, or 0.

    The bound is just as likely to live in the published policy as in the bootstrap
    declaration -- ``effective_max_cache_age`` already assumes so for the child it hands
    the value to. But at BOOT the resolved ``PolicyDistribution`` comes from the env or a
    lower tier, so a fleet that set the bound only in its own document had it applied
    while the process ran and then silently dropped on the next restart: exactly the
    restart-during-an-outage case the bound exists for, where an expired ceiling would be
    admitted instead of refused.

    Read straight from the JSON rather than through ``parse_policy``: this runs before the
    document has been validated, and the alternative is either parsing twice or letting a
    malformed cache raise from inside a staleness pre-check. Anything unreadable or not a
    positive whole number yields 0 -- "declares no bound of its own" -- so a broken cache
    is refused by the parse that follows, not by this.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        declared = data["distribution"]["max_cache_age_secs"]
    except Exception:
        return 0
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        return 0
    # ``isfinite`` only for a float: it converts its argument to a float first, so a JSON
    # integer of 310 digits would raise OverflowError out of a pre-check whose whole
    # contract is "anything unusable yields 0". The ``try`` above does not cover this line.
    if isinstance(declared, float) and not math.isfinite(declared):
        return 0
    if declared != int(declared) or declared <= 0:
        return 0
    return int(declared)


def _cache_is_too_old(dist: PolicyDistribution, cached: "CachedPolicy") -> bool:
    """Whether *cached* has outlived the staleness bound, from EITHER declaration.

    Tightest-wins, matching the governance model's own rule: a bound in the cached
    document binds even when the bootstrap declaration has none, and a tighter bootstrap
    bound is not loosened by the document.
    """
    age = cached.age_secs()
    if dist.cache_too_old(age):
        return True
    own = _cached_max_cache_age(cached.body)
    return bool(own) and age > own


def _fetch_window(dist: PolicyDistribution) -> int:
    """How long a fetch's result stands before this tier will go out again.

    The fleet's own cadence when it set one; otherwise
    :data:`MIN_REFRESH_INTERVAL_SECS`.  A source with no ``refresh_interval_secs``
    means "establish the ceiling at boot", not "never cache and never bound" — and
    an unbounded reading is unsafe here rather than merely slow, because this tier
    is on the per-app-call reload path. Sixty seconds keeps a boot-only
    configuration behaving like one (boot's first call fetches) while bounding every
    later caller.

    Deliberately the SAME value for the cache shortcut and the fetch cooldown, so
    there is one window to reason about instead of two that can disagree.
    """
    return dist.effective_refresh_interval() or MIN_REFRESH_INTERVAL_SECS


def _claim_fetch_slot(window_secs: int) -> bool:
    """Reserve the right to go to the network, or refuse because someone just did.

    Records the ATTEMPT, not the success: a source that is down would otherwise be
    retried by every caller, and the per-app-call path would turn a network outage
    into one timeout per app call.

    The first claim in a process always succeeds, so boot is never held back by this.
    Explicit refresh (``refresh_now``, and therefore the CLI and the background
    poller) does not come through here — an operator asking for a fetch gets one.
    """
    global _last_fetch_attempt
    now = time.time()
    with _FETCH_SLOT_LOCK:
        # A clock that stepped backwards would otherwise park the window in the
        # future and block fetching until real time caught up.
        if _last_fetch_attempt and 0 <= now - _last_fetch_attempt < window_secs:
            return False
        _last_fetch_attempt = now
        return True


def _load_by_fetch(dist: PolicyDistribution, cached: Optional[CachedPolicy]) -> _LoadOutcome:
    """Fetch, and on failure fall back to whatever the cache can still offer."""
    # Observed BEFORE the fetch, for the same reason and with the same ordering the live
    # refresh uses: the concurrent publish this guards against lands DURING the fetch, so a
    # snapshot taken afterwards already includes it. Boot is not exempt — a slow fetch of v2
    # racing a `policy fetch --force` that publishes v3 would otherwise overwrite the cache
    # with v2 and install it, rolling the ceiling BACKWARD to a possibly looser document on
    # the one code path where nothing is running yet to notice.
    expect_pair = _cache_pair(read_cache_meta())
    try:
        fetched = fetch_once(dist, cached)
    except PlatformCompositionError as exc:
        # A configuration error (unknown scheme, refused transport, an empty
        # document) is not transient. It must not be papered over by a cache,
        # because the operator would then never learn that the source they
        # configured is unusable.
        #
        # Re-raised through the sanitiser rather than verbatim: these messages are
        # built by the fetchers (and, through the seam, by an edition's transport) and
        # name the source, and a boot abort's text reaches stderr and any supervisor
        # that captures it. The module's rule is that the URL is emitted nowhere, and
        # a re-raise is not an exception to it.
        raise PlatformCompositionError(_sanitize_detail(str(exc), dist.source)) from exc
    except Exception as exc:
        # ``_sanitize_detail``, not ``_redact_source``: the exception text is the
        # ENDPOINT's, and an error page or a proxy echoing the request can carry the
        # Authorization header back. Redacting only the configured source would print it.
        reason = _sanitize_detail(
            f"could not fetch the ceiling from {dist.source}: {exc}", dist.source
        )
        logger.warning("%s", reason)
        return _LoadOutcome(ceiling=_from_cache_on_outage(dist, cached, reason), reason=reason)

    if fetched.not_modified:
        if cached is None:
            # A fetcher answered "unchanged" against validators we never sent.
            # Treating that as success would adopt nothing at all.
            return _LoadOutcome(
                reason=_sanitize_detail(
                    f"{dist.source} reported no change but nothing is cached", dist.source
                )
            )
        body, provenance = cached.body, "the re-validated cached copy"
    else:
        body, provenance = fetched.body, dist.source

    try:
        ceiling = parse_distributed_policy(body, source=dist.source)
    except PlatformCompositionError as exc:
        reason = _sanitize_detail(
            f"the ceiling published at {dist.source} is unusable: {exc}", dist.source
        )
        logger.error("%s", reason)
        # Do NOT cache a document we refused: a poisoned last-known-good would
        # outlive the bad push and keep this host failing after it was corrected.
        #
        # The cache is still an acceptable answer — it is a ceiling the fleet
        # published and this host verified. What is NOT acceptable is falling through
        # to a LOWER tier: ``on_unavailable`` answers "the ceiling could not be
        # REACHED", and a document we reached and REFUSED is a different question. An
        # invalid or signature-refused push must not quietly demote the host onto a
        # policy the administrator superseded, so with no usable cache this raises
        # regardless of the disposition.
        salvaged = _from_cache_on_outage(dist, cached, reason)
        if salvaged is not None:
            return _LoadOutcome(ceiling=salvaged)
        raise PlatformCompositionError(
            f"{reason}. Refusing to fall back to a lower policy tier: the document was "
            "read and refused, which is not an availability failure. Fix the published "
            f"document, or unset {POLICY_URL_ENV} to stop following it."
        ) from exc

    # The SAME two gates the live-refresh path applies, for the same reasons, so the two
    # paths agree on what is installable. Without them boot would cache and install a
    # document ``refresh_now`` refuses — and then the refresher would reject it on every
    # cycle for the lifetime of the process, logging a rejection forever while the host ran
    # on it.
    #
    # Handled exactly like the parse refusal above: salvage from the cache if it can still
    # serve, else raise. A document read and REFUSED is not an availability failure, so it
    # must not quietly demote the host onto a lower tier — and it is not cached either, so
    # a corrected push is not shadowed by a poisoned last-known-good.
    migrated = _declared_source(body)
    refusal = ""
    if migrated and migrated != dist.source and not _env_pins_the_source():
        refusal = (
            "the document published for this host moves distribution.source to a different "
            f"address. Migrating the source is a bootstrap change: repoint {POLICY_URL_ENV} "
            "(or the placed policy's distribution.source) instead, then publish."
        )
    else:
        try:
            validate_ceiling(ceiling)
        except PlatformCompositionError as exc:
            refusal = _sanitize_detail(
                f"the ceiling published at {dist.source} does not compose on this host: {exc}",
                dist.source,
            )
    if refusal:
        logger.error("%s", refusal)
        salvaged = _from_cache_on_outage(dist, cached, refusal)
        if salvaged is not None:
            return _LoadOutcome(ceiling=salvaged)
        raise PlatformCompositionError(refusal)

    # NOT recorded yet. ``_record_installed`` is what ``central_ceiling_installed`` answers
    # from, and the gateway flags an app backend cache-only on that answer — so recording
    # these bytes before the publish resolves would, on a lost swap whose winner is then
    # REJECTED, leave the host degraded while every child was told to resolve its ceiling from
    # a cache holding the document this host just refused. It is recorded at the end, once the
    # ceiling this function returns is the one that will govern.
    #
    # The cache is written only after the document proved usable, on both branches:
    # a 304 restarts the cached copy's age (it IS the published document right now,
    # so a stable policy must not trip a staleness bound for having confirmed that),
    # and new bytes become the new last-known-good.
    if fetched.not_modified and cached is not None:
        touch_cache(cached.meta(), etag=fetched.etag, last_modified=fetched.last_modified)
    elif not fetched.not_modified:
        published = write_cache(
            body,
            source=dist.source,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            expect_pair=expect_pair,
        )
        if not published and _cache_pair(read_cache_meta()) != expect_pair:
            # The swap lost: another writer published while this fetch was in flight, and
            # what is on disk now may be NEWER than what we hold. Boot must install
            # something, and the right something is the winner — so adopt the cache rather
            # than the bytes we fetched. It is re-gated rather than trusted, and adopted
            # WITHOUT a further write: the winner is already the cache, so re-publishing it
            # would be the overwrite this path exists to avoid. Not a retry loop — one
            # straight-line handoff — so a third writer arriving cannot spin it.
            logger.info(
                "another process published a newer ceiling during this boot fetch; adopting "
                "the cache instead of what was fetched"
            )
            winner = read_cache()
            if winner is not None:
                return _load_from_cached_winner(dist, winner)
        if not published:
            # ``write_cache`` is best-effort for the GATEWAY -- it is governed by what it
            # just fetched either way -- but a cache-only app backend resolves its ceiling
            # FROM this file. So an unpublished tightening leaves a stale, LOOSER document
            # as what every child spawned afterwards adopts: the exact failure the
            # cache-only handoff exists to prevent, arrived at by a swallowed write error.
            #
            # A stale pair that disagrees with what is being installed is therefore removed
            # rather than left. A child then finds no cache and fails CLOSED (a disabled
            # app, loudly) instead of starting under a ceiling the administrator
            # superseded. If it cannot be removed either -- the usual reason the write
            # failed in the first place -- the invariant cannot be established at all, and
            # that is a boot refusal rather than something to log and continue past.
            _discard_disagreeing_cache(body)
    _record_installed(body)
    logger.info("governance ceiling loaded from %s", _redact_source(provenance, dist.source))
    return _LoadOutcome(ceiling=ceiling)


def _load_from_cached_winner(dist: PolicyDistribution, winner: CachedPolicy) -> _LoadOutcome:
    """Adopt a cache another process published while this boot was fetching.

    Runs the winner through the SAME gates the fetched document goes through — parse, the
    source-migration refusal, ``validate_ceiling`` — because "someone else published it" is
    not evidence this host can run under it. A refusal here reports itself and leaves the
    caller on the ordinary unavailable disposition rather than installing a document this
    host rejects.

    Does not write the cache: the winner is already the cache, and re-publishing it would
    be the very overwrite this path exists to avoid.
    """
    try:
        # The REPOINT rule as well, which the CAS loss is exactly what makes reachable: the
        # winner was published by another process, and that process may have been configured
        # for a different source. Re-gating parse, migration and composition but not
        # provenance would let source B's ceiling govern a host pointed at source A.
        if winner.source_digest != _source_digest(dist.source):
            raise PlatformCompositionError(
                "it was cached against a different source, so it is not this host's "
                "last-known-good"
            )
        ceiling = parse_distributed_policy(winner.body, source=dist.source)
        migrated = _declared_source(winner.body)
        if migrated and migrated != dist.source and not _env_pins_the_source():
            raise PlatformCompositionError(
                "it moves distribution.source to a different address; migrating the source "
                f"is a bootstrap change ({POLICY_URL_ENV} or the placed policy)"
            )
        validate_ceiling(ceiling)
    except PlatformCompositionError as exc:
        reason = _sanitize_detail(
            f"the ceiling another process cached from {dist.source} is unusable: {exc}",
            dist.source,
        )
        logger.error("%s", reason)
        return _LoadOutcome(reason=reason)
    _record_installed(winner.body)
    logger.info("governance ceiling loaded from the cache another process just published")
    return _LoadOutcome(ceiling=ceiling)


def _discard_disagreeing_cache(installed: bytes) -> None:
    """Remove a cached pair that is not the document being installed.

    Called only when publishing *installed* failed. Raises ``PlatformCompositionError``
    if a disagreeing pair survives, because at that point nothing can make the cache
    agree with the ceiling this process is adopting, and a cache-only child would
    silently adopt the older one.

    A pair that already AGREES is left alone: an equal document is not stale, and the
    write may simply have been redundant. So is an ABSENT one: with nothing cached there
    is nothing a child could adopt instead, so a failed write costs the outage fallback
    and nothing else.
    """
    directory = cache_dir()
    digest = _body_digest(installed)
    # Cheap pre-check before taking a lock, which on a host that has never cached
    # anything would have to CREATE the directory and the lock file just to discover
    # there is nothing to remove. Re-read authoritatively under the lock below, so a
    # writer arriving in between is still seen.
    meta = read_cache_meta()
    if meta is None or meta.digest == digest:
        return
    try:
        with _cache_write_lock(directory):
            meta = read_cache_meta()
            if meta is None or meta.digest == digest:
                return
            logger.warning(
                "could not publish the fetched ceiling to the policy cache; removing the "
                "stale cached copy so an app backend cannot adopt it instead"
            )
            for leaf in (_CACHE_DOC_LEAF, _CACHE_META_LEAF):
                (directory / leaf).unlink(missing_ok=True)
            remaining = read_cache_meta()
            if remaining is not None and remaining.digest != digest:
                raise PlatformCompositionError("the stale cached ceiling could not be removed")
    except PlatformCompositionError:
        raise
    except Exception as exc:
        raise PlatformCompositionError(
            "the fetched ceiling could not be published to the policy cache and the stale "
            f"cached copy could not be removed ({exc}); a cache-only app backend would "
            f"adopt the superseded ceiling. Check that the gateway can write {cache_dir()}."
        ) from exc


def _from_cache_on_outage(
    dist: PolicyDistribution, cached: Optional[CachedPolicy], reason: str
) -> Optional[GovernanceCeiling]:
    """The last-known-good copy, when it is still an acceptable answer.

    This is the property that makes a central ceiling safe to depend on: a host
    keeps the ceiling it was last given rather than losing governance because a
    bucket had a bad minute.  It stops being acceptable in exactly two cases — no
    cache at all, and a cache past the fleet's ``max_cache_age_secs`` — and both
    hand the decision to ``on_unavailable``.
    """
    if cached is None:
        return None
    age = cached.age_secs()
    if _cache_is_too_old(dist, cached):
        logger.error(
            "the cached ceiling is %.0fs old, past the %ss bound, and %s",
            age,
            dist.max_cache_age_secs or _cached_max_cache_age(cached.body),
            reason,
        )
        return None
    try:
        ceiling = parse_distributed_policy(cached.body, source=dist.source)
    except PlatformCompositionError as exc:
        # Sanitised rather than a traceback, for the reason the loader's sibling arm gives.
        logger.error(
            "the cached ceiling is also unusable: %s", _sanitize_detail(str(exc), dist.source)
        )
        return None
    _record_installed(cached.body)
    mark_governance_incident("degraded", f"policy_distribution:cache:{int(age)}s")
    logger.warning("serving the cached governance ceiling (%.0fs old) because %s", age, reason)
    return ceiling


def _unavailable(dist: PolicyDistribution, reason: str) -> None:
    """Apply ``on_unavailable`` when no ceiling could be established."""
    detail = reason or _sanitize_detail(
        f"the ceiling at {dist.source} could not be established", dist.source
    )
    if dist.on_unavailable == UNAVAILABLE_DEGRADE:
        mark_governance_incident("degraded", "policy_distribution:unavailable")
        logger.error(
            "central policy distribution is configured but unusable (%s); falling "
            "through to the next policy tier as on_unavailable=%s",
            detail,
            UNAVAILABLE_DEGRADE,
        )
        return None
    mark_governance_incident("failed_closed", "policy_distribution:unavailable")
    raise PlatformCompositionError(
        f"central policy distribution is configured but no ceiling could be "
        f"established ({detail}). Refusing to run ungoverned. Set "
        f"{POLICY_UNAVAILABLE_ENV}={UNAVAILABLE_DEGRADE} to fall through instead, "
        f"or unset {POLICY_URL_ENV} to stop fetching centrally."
    )


def fetch_once(dist: PolicyDistribution, cached: Optional[CachedPolicy] = None) -> FetchedPolicy:
    """One conditional fetch through the registered transport for *dist.source*."""
    fetcher = _fetcher_for(dist.source)
    result = fetcher(
        FetchRequest(
            url=dist.source,
            timeout_secs=dist.effective_timeout(),
            headers=request_headers(),
            etag=cached.etag if cached else "",
            last_modified=cached.last_modified if cached else "",
        )
    )
    if not result.not_modified and not result.body:
        raise PlatformCompositionError(f"policy source {dist.source!r} returned an empty document")
    return result


# ──────────────────────────────────────────────────────────────────────────
# Live refresh — installing a pushed change without a restart
# ──────────────────────────────────────────────────────────────────────────


#: A request-header value shorter than this is not substituted out of a message. It is not a
#: credential at that length, and replacing a 1-3 character string would corrupt every
#: message that happened to contain those characters — the redaction would do more damage
#: than the leak it was guarding against.
_MIN_SUBSTITUTABLE_SECRET = 8


def _request_header_secrets() -> Tuple[str, ...]:
    """The exact header VALUES this host sends, longest first.  Never raises.

    Longest first so a value that is a prefix of another does not leave the remainder
    behind. Never raises because this feeds the sanitiser, and a sanitiser that can fail is
    one that stops sanitising at the worst moment: ``request_headers`` refuses malformed
    JSON by design, and that refusal must not become an exception on a logging path.
    """
    try:
        values = [v for v in request_headers().values() if len(v) >= _MIN_SUBSTITUTABLE_SECRET]
    except Exception:
        return ()
    return tuple(sorted(set(values), key=len, reverse=True))


def _sanitize_detail(text: str, source: str) -> str:
    """Make *text* safe for the CLI and the log ring.

    Three passes, because there are three different things in here that must not be
    published, from three different authors:

    * the SOURCE, which the operator configured — :func:`_redact_source`;
    * the request CREDENTIAL, which the operator also configured, substituted by exact
      value. ``security.redact`` below recognises credential SHAPES, and
      ``KIROCREW_POLICY_HEADERS`` is deliberately arbitrary: an ``X-Fleet-Key`` holding an
      opaque blob matches no pattern anyone can write, so pattern matching cannot reach it.
      What makes this tractable is that the value is not a pattern to us — we sent it, so
      we can substitute the string itself, which is strictly stronger than any shape rule
      for exactly the values at risk;
    * anything the ENDPOINT put in the message. A malformed document reaches the CLI
      through a parser error, and ``json``'s errors quote the offending text — so a
      document that echoes back the request's ``Authorization`` header (an endpoint
      returning its own request, a misconfigured proxy's error page) would carry that
      credential into ``kirocrew policy fetch`` and into ``GET /api/logs``. The bytes are
      not ours, so the shared credential + exfiltration-URL chain applies to them, the
      same way every other stderr-bearing surface in this package handles text it did
      not author.
    """
    from kiro_crew import security

    text = _redact_source(text, source)
    for secret in _request_header_secrets():
        text = text.replace(secret, "[REDACTED: policy request header]")
    return security.redact(text)


def _redact_source(text: str, source: str) -> str:
    """Replace the source URL (and its bare hostname) in *text* with its scheme.

    **This module emits the source URL nowhere.**  One rule rather than a per-surface
    judgement, because the surfaces are not as separable as they look: the log ring is
    served by ``GET /api/logs`` and rendered in the dashboard, which the agent's own
    browser tooling can drive, so "it is only a log line" is not a boundary here. The
    operator configured the endpoint and does not need it echoed; the scheme plus the
    error text is what diagnoses a failure, and ``kirocrew policy source`` reports the
    scheme by design.

    Redacting the STRING rather than rewriting each message is deliberate. These
    messages are assembled from exceptions raised all over this module and, through the
    fetcher seam, from an edition's own transport, so a per-message rule would be one
    someone has to remember; a substring replacement cannot be forgotten. It is a
    defence-in-depth measure over the messages themselves, not a substitute for them
    being careful.
    """
    if not source:
        return text
    parts = _split_url(source)
    scheme = parts.scheme or "unknown"
    placeholder = f"<the {scheme} policy source>"
    text = text.replace(source, placeholder)
    # The HOSTNAME on its own, because plenty of transport errors never quote the
    # whole URL: a TLS mismatch says "hostname 'config.corp.example' doesn't match",
    # and a DNS failure names the host alone. Replacing only the full source would
    # miss both and print the endpoint anyway. Done second so a message carrying the
    # complete URL still gets the more informative substitution.
    host = parts.hostname or ""
    if host:
        text = text.replace(host, placeholder)
    return text


@dataclass(frozen=True)
class RefreshOutcome:
    """The result of one refresh attempt, for the CLI, the viewer and the audit.

    ``detail`` carries no source URL — see :func:`_redact_source` for why this is the
    boundary where that matters.
    """

    status: str
    detail: str = ""
    signature_state: str = ""


def tier1_local_policy() -> str:
    """The explicit local policy path that OUTRANKS this tier, or ``""``.

    ``KIROCREW_SECURITY_POLICY`` is precedence tier 1 and the documented rollback
    lever: an operator recovering from a bad central push pins a local file, and the
    whole recovery story rests on that file winning. It has to win against a live
    REFRESH as well as against a boot, and a refresh is where it is easiest to miss —
    the poller is already running when the file appears.

    Checked by path EXISTENCE rather than by inferring provenance from what this
    process happens to have installed. That is what makes it exact: it asks the same
    question ``load_security_policy`` asks, so the refresher cannot disagree with the
    ladder. It also stays correct in the case digest-based inference gets wrong — a
    host that booted UNGOVERNED under ``degrade`` and then becomes able to reach its
    source has no tier-1 file, so the refresh proceeds and the ceiling finally binds.
    """
    raw = os.environ.get("KIROCREW_SECURITY_POLICY", "").strip()
    if not raw:
        return ""
    return raw if Path(raw).exists() else ""


def _body_digest(body: bytes) -> str:
    """A stable digest of a policy document's bytes.

    Only ever compared for equality — never stored, never published — so the choice
    of hash is about collision resistance against an ACCIDENTAL match, not about
    signing. That is what ``identity.signature`` is for.
    """
    return hashlib.sha256(body).hexdigest()


def _installed_digest() -> str:
    """The digest of the document this process last installed, or ``""``.

    Process-local on purpose. It answers "is the ceiling in effect here the one those
    bytes describe", which no shared file can: the cache is written by several
    processes (gatewayd's reload, an app backend's boot, ``kirocrew policy fetch``)
    while only this one installed a ceiling.
    """
    with _INSTALLED_LOCK:
        return _installed_body_digest


def _record_installed(body: bytes) -> None:
    global _installed_body_digest
    with _INSTALLED_LOCK:
        _installed_body_digest = _body_digest(body)


def validate_ceiling(ceiling: GovernanceCeiling) -> None:
    """Everything :func:`apply_ceiling` checks, without installing anything.

    Split out so a refresh can prove a candidate BEFORE it publishes the cache: the
    cache is what a concurrently-starting cache-only child adopts, so writing a document
    this host would refuse would hand that child a ceiling the gateway rejected.
    """
    from kiro_crew.platform.governance import assert_policy_signature_satisfied
    from kiro_crew.platform.governance_profiles import assert_profile_floor

    tier1 = tier1_local_policy()
    if tier1:
        raise PlatformCompositionError(
            f"refusing to install a centrally fetched ceiling while {tier1} supplies "
            "one: KIROCREW_SECURITY_POLICY is precedence tier 1 and the rollback lever, "
            "so a poll must not displace it"
        )
    assert_policy_signature_satisfied(ceiling)
    assert_profile_floor(ceiling)


def apply_ceiling(ceiling: GovernanceCeiling) -> None:
    """Validate *ceiling* as boot would, then install it process-wide.

    The validation is the whole point: a pushed document must clear the same floor
    gates boot applies, so a refresh cannot install a ceiling that this host would
    have refused to start under.  Both gates raise, and a raise here means the
    caller keeps the ceiling already installed.

    Installation replaces one field of the frozen context.  Every enforcement
    chokepoint reads ``current_context().governance`` per decision rather than
    capturing it, so the swap binds on the next call with nothing to invalidate by
    hand — and ``set_context`` bumps the ceiling generation, which is what makes
    the profile store recompose against the new ceiling instead of serving
    snapshots built against the retired one.

    The profile check is ``assert_profile_floor``, not the boot gate
    ``assert_profiles_within_ceiling``.  The extra refusal the boot gate carries is
    about the profile STORE's state rather than about the ceiling, and it is
    boot-only by design: at runtime a profile file that became unreadable is
    deliberately reported rather than escalated, so honouring it here would let one
    stray local file block every future fleet policy change on this host — including
    a tightening.  The ordinal floor, which is what "would this host have started
    under the candidate" actually means, still applies.
    """
    from kiro_crew.platform.context import current_context, set_context

    validate_ceiling(ceiling)
    set_context(replace(current_context(), governance=ceiling))

    # NOTE: the selectable-ACP-backend set is deliberately NOT recomputed here.
    #
    # Every other chokepoint reads ``current_context().governance`` per decision, so the
    # swap above is all they need. The backend gate cannot: its single gate
    # ``resolve_selected_backend`` runs inside ``KiroCrewConfig.load()``, and reading a
    # ceiling there re-enters that load (harness-parity H3). Its state therefore lives in
    # the ``acp_backends`` registry, established once from ``bootstrap_context``.
    #
    # Recomputing the registry here would bind the new ceiling for backend SELECTION
    # while leaving sessions and pooled providers already running a now-denied harness
    # untouched -- an enforcement that looks complete and is not. Retiring live work needs
    # a session-lifecycle path that does not exist yet (tracked separately), so the
    # promise this scope makes is deliberately the narrow one it can keep: the set is
    # decided at gateway start, and a policy change binds on the next start. That is
    # stated in the dashboard panel and in
    # ``docs/system-specs/modules/governance.md``. Wiring the recompute in is a
    # one-line change once retirement exists -- ``apply_selectable_denials`` assigns
    # ``baseline - denied`` rather than mutating, so it is already safe to re-run with a
    # looser ceiling.


def refresh_now(*, force: bool = False) -> RefreshOutcome:
    """Re-fetch the central ceiling and install it if it is usable.

    Never raises: this runs on a background timer and from an operator command,
    and neither wants an exception for "the endpoint was down".  Every failure mode
    keeps the running ceiling and reports itself.

    ``force`` skips the conditional-request validators so an operator can prove the
    round trip end to end, which is what they actually want from a manual fetch —
    a 304 tells them nothing about whether the document they just published is
    being read correctly.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        logger.error(
            "policy distribution is misconfigured: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        return RefreshOutcome(
            REFRESH_REJECTED, "policy distribution is misconfigured; see the gateway log"
        )
    if not dist.enabled:
        return RefreshOutcome(REFRESH_NOT_CONFIGURED)
    # Before spending a fetch: a tier-1 local file outranks this tier, so there is
    # nothing a refresh could usefully install. ``apply_ceiling`` refuses too, as the
    # hard guard; this arm exists so the operator gets a reason instead of a fetch
    # followed by a rejection.
    tier1 = tier1_local_policy()
    if tier1:
        detail = (
            f"{tier1} (KIROCREW_SECURITY_POLICY) outranks the central source; "
            "not fetching. Unset it to follow the fleet policy again."
        )
        logger.info("%s", detail)
        return RefreshOutcome(REFRESH_REJECTED, _sanitize_detail(detail, dist.source))

    # Observed BEFORE the fetch, and this ordering is the whole control: the concurrent
    # publish this guards against lands DURING the fetch, so a snapshot taken afterwards
    # already includes it and the compare-and-swap below would pass while overwriting a
    # NEWER document with the one this call fetched.
    #
    # The raw on-disk identity, deliberately not filtered by source: the question is "is
    # the cache still the pair I saw", and filtering would make a legitimate repoint (whose
    # cache holds the retired endpoint's document) look like a lost race.
    expect_pair = _cache_pair(read_cache_meta())
    pre_fetch = read_cache()
    if pre_fetch is not None and pre_fetch.source_digest != _source_digest(dist.source):
        pre_fetch = None
    cached = None if force else pre_fetch
    try:
        fetched = fetch_once(dist, cached)
    except PlatformCompositionError as exc:
        # A configuration or content error, not a transport one: an unknown scheme,
        # a refused transport, an over-size body, an empty document. Reporting these
        # as "unreachable" would send an operator hunting a network problem that is
        # really a bad push or a typo'd source, so they are classified with the other
        # refusals.
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"refused the fetch from {dist.source}: {exc}",
            incident="rejected",
        )
    except Exception as exc:
        return _refresh_failure(
            REFRESH_UNREACHABLE, dist, f"could not fetch {dist.source}: {exc}", incident="refresh"
        )

    # A 304 is answered against the CACHE's validators, and the cache is written by
    # other processes too: gatewayd's per-app-call reload, an app backend's boot, and
    # `kirocrew policy fetch` — the step the operator guide recommends for verifying a
    # rollout. So "the source has nothing newer than the cache" does NOT imply "this
    # process is already running it. Without the digest comparison below, one
    # `policy fetch` on a host would cache v2, and this poller would then report
    # `unchanged` forever while the gateway kept enforcing v1 — the live-refresh
    # guarantee silently withdrawn, under the most reassuring status there is.
    body = fetched.body
    if fetched.not_modified:
        if cached is None:
            # A fetcher answered "unchanged" against validators we never sent — which is the
            # normal state under ``--force``, where the validators are deliberately skipped.
            # Reporting that as UNCHANGED made ``kirocrew policy fetch --force`` exit 0 having
            # established nothing, and exiting 0 is exactly what a config-management run reads
            # as "this host took the change". The loader's own arm already refuses it; this is
            # the same refusal on the refresh path.
            return _refresh_failure(
                REFRESH_REJECTED,
                dist,
                f"{dist.source} reported no change, but nothing is cached to compare against "
                "-- with --force no validators were sent, so there was nothing for it to "
                "answer. Treat this as a source that does not honour a conditional request.",
                incident="rejected",
            )
        installed = _installed_digest()
        # Nothing to install when the cache is already what is running. An EMPTY digest
        # deliberately does NOT skip: it means this process does not know what it
        # installed — the case of a boot that DEGRADED to ungoverned — and skipping there
        # would leave such a host permanently below a ceiling another process had already
        # cached. Tier-1 precedence is guarded separately, above, by asking the loader
        # ladder's own question.
        if _body_digest(cached.body) == installed:
            # Re-validate even when nothing changed. "Unchanged" is a statement about the
            # DOCUMENT, and the trust root is a separate input that moves on its own
            # schedule: a fleet turning on ``require_policy_signature`` or rotating a trust
            # key makes the running ceiling untrusted without the endpoint publishing
            # anything, and a 304 would otherwise let it stand indefinitely. Cheap enough
            # to do per interval.
            try:
                parse_distributed_policy(cached.body, source=dist.source)
            except PlatformCompositionError as exc:
                return _refresh_failure(
                    REFRESH_REJECTED,
                    dist,
                    f"the ceiling in effect no longer satisfies the trust root: {exc}",
                    incident="rejected",
                )
            touch_cache(cached.meta(), etag=fetched.etag, last_modified=fetched.last_modified)
            # Hooks run on this path too, not just on an install. They are best-effort by
            # design, so a transient failure — the tailnet daemon busy for one cycle —
            # would otherwise never be retried: the document does not change, every later
            # poll returns UNCHANGED, and a control the ceiling forbids stays materialised
            # until someone restarts the host. Running them wherever a poll CONFIRMS what
            # governs makes the retry automatic, and they are cheap and idempotent for it:
            # the tailnet one is a governance evaluation that returns immediately unless
            # the capability is actually denied.
            _run_post_install_hooks()
            return RefreshOutcome(REFRESH_UNCHANGED)
        logger.info("the cached ceiling differs from the one installed; adopting the cache")
        body = cached.body

    try:
        ceiling = parse_distributed_policy(body, source=dist.source)
    except PlatformCompositionError as exc:
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"refused the ceiling published at {dist.source}: {exc}",
            incident="rejected",
        )

    # The pair to restore if the install below refuses. This module promises that a
    # document failing a mandated check is never left as the last-known-good, and the
    # publish-before-install ordering is what puts one at risk: the bytes are on disk
    # before ``apply_ceiling`` has had its say, and that step can refuse for reasons the
    # earlier validation could not see (a profile, the trust root, or a tier-1 pin that
    # moved between the two). Without this, a rejected push becomes what the next boot —
    # and every cache-only child — adopts.
    #
    # The same pre-fetch snapshot the swap expects, not a fresh read: restoring a copy
    # published by another writer during this fetch would undo ITS work, and a copy from
    # a different source is not a rollback target at all (re-caching a retired endpoint's
    # document under the new endpoint's name is what the repoint rule exists to refuse).
    previous = pre_fetch

    # A candidate that MOVES the source is refused rather than installed. Nothing else
    # would notice: the refresher re-reads the installed ceiling each cycle, so it would
    # start polling the new address and the migration would look like it worked — until a
    # restart, where the bootstrap declaration (the env variable or the lower-tier file)
    # still names the OLD source and the cache, recorded against the new one, is discarded
    # by the repoint rule. A fleet that had retired the old address would then have hosts
    # that run fine and cannot reboot, with nothing in between to warn them.
    #
    # Migrating a source is a BOOTSTRAP change: move ``KIROCREW_POLICY_URL`` (or the
    # ``distribution.source`` in the placed policy) and the new address is durable on the
    # next boot as well as this one. Refusing here keeps the two channels consistent
    # instead of offering a migration that only half-applies.
    migrated = _declared_source(body)
    if migrated and migrated != dist.source and not _env_pins_the_source():
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            "the published document moves distribution.source to a different address. A "
            "live refresh cannot migrate the source: the bootstrap declaration would "
            f"still name the old one at the next boot. Repoint {POLICY_URL_ENV} (or the "
            "placed policy's distribution.source) instead, then publish.",
            incident="rejected",
            signature_state=ceiling.signature_state,
        )

    try:
        # Validate BEFORE publishing, then publish BEFORE installing. A cache-only child
        # starting concurrently adopts whatever the cache holds, so installing first
        # leaves a window where the gateway enforces the new ceiling and a fresh app
        # backend adopts the retired one — and publishing first without validating would
        # hand that child a document this host is about to refuse.
        validate_ceiling(ceiling)
        if not write_cache(
            body,
            source=dist.source,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            expect_pair=expect_pair,
        ):
            # Not published, and the two reasons want different answers. Re-reading is how
            # they are told apart, and it is the honest way: the swap's own condition was
            # "the cache still holds what I observed".
            moved = _cache_pair(read_cache_meta()) != expect_pair
            if moved:
                # Another writer published during this fetch. Installing what we fetched
                # could roll the ceiling BACKWARD to a looser document, so this refresh
                # simply stands down; the next cycle finds the cache differs from what is
                # installed and adopts it, which is the path a 304 against a moved-ahead
                # cache already takes. Not a failure of anything, so not an incident.
                logger.info(
                    "another process published a newer ceiling during this fetch; standing "
                    "down and leaving it to the next cycle"
                )
                return RefreshOutcome(
                    REFRESH_UNCHANGED,
                    "another process published a newer ceiling during this fetch; it will "
                    "be adopted on the next cycle",
                )
            # The write itself failed. Reported like the read-back confirmation below,
            # because it is the same operator problem: the gateway cannot write the cache,
            # so a cache-only child would inherit the previous ceiling.
            return _refresh_failure(
                REFRESH_REJECTED,
                dist,
                "the refreshed ceiling could not be published to the policy cache, so a "
                "cache-only child would inherit the previous one; keeping the running "
                f"ceiling. Check that the gateway can write {cache_dir()}.",
                incident="compose",
                signature_state=ceiling.signature_state,
            )
        # ``write_cache`` is best-effort by design — a host that cannot write it is
        # still governed by what it fetched — but a cache-only child inherits the
        # ceiling FROM that file. So a swallowed write failure here would leave the
        # gateway enforcing the new (possibly tighter) ceiling while every app backend
        # spawned afterwards adopted the older, looser one. Confirming the publish is
        # what makes "the cache is the administrator's ceiling" true rather than
        # usually true.
        published = read_cache()
        # The SOURCE as well as the bytes. A repoint that publishes identical bytes plus a
        # failed metadata write leaves the old source's cache passing a body-only check,
        # and it is the recorded source the next boot's repoint rule judges — so the
        # confirmation would report a publish that a restart then discards, failing closed
        # on a host whose refresh had just reported success.
        if (
            published is None
            or published.body != body
            or published.source_digest != _source_digest(dist.source)
        ):
            raise PlatformCompositionError(
                "the refreshed ceiling could not be published to the policy cache, so a "
                "cache-only child would inherit the previous one; keeping the running "
                f"ceiling. Check that the gateway can write {cache_dir()}."
            )
        apply_ceiling(ceiling)
    except Exception as exc:
        # Only if the cache still holds what THIS refresh published: another process may
        # have published a newer document between the publish above and this failure, and
        # rolling back over it would destroy a valid ceiling nobody asked us to touch.
        _restore_cache(previous, published_pair=(_body_digest(body), _source_digest(dist.source)))
        # The document is well-formed but this host cannot run under it — a bound
        # profile is looser than the new ceiling, or a mandated signature is
        # missing. Keeping the running ceiling is the whole reason a live refresh
        # validates before installing: a fleet that is already up must not be taken
        # down by a push, and the operator has an audited reason to look at.
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"the ceiling published at {dist.source} does not compose on this host: {exc}",
            incident="compose",
            signature_state=ceiling.signature_state,
        )

    _record_installed(body)
    # After the install, so a hook sees the ceiling it is meant to enforce. Also run on the
    # UNCHANGED path above — see the note there for why a confirming poll is the right
    # trigger rather than an install alone.
    _run_post_install_hooks()
    logger.info(
        "installed a refreshed governance ceiling from the %s source",
        _split_url(dist.source).scheme or "unknown",
    )
    _audit_refresh(REFRESH_APPLIED, dist)
    return RefreshOutcome(REFRESH_APPLIED, signature_state=ceiling.signature_state)


def _restore_cache(previous: Optional["CachedPolicy"], *, published_pair: Tuple[str, str]) -> None:
    """Put *previous* back as the last-known-good after a refused install.

    A **compare-and-swap**, like the publish it undoes: the rollback happens only while
    the cache still holds *published_pair*, the ``(body digest, source digest)`` this
    refresh put there. The pair for the same reason the publish uses one — identical bytes
    under a different source are not this refresh's work to undo. The
    publish and this failure are separated by ``apply_ceiling``, so another process can
    publish in between — and rolling back over it would destroy a valid, newer ceiling
    nobody asked us to touch. When the digests disagree, the newer copy is left alone;
    this refresh's document is no longer the one on disk, so there is nothing of ours to
    undo.

    Called only from ``refresh_now``'s failure arm, where the new bytes are already on
    disk because the publish is deliberately confirmed before the install. Restoring
    keeps this module's promise that a document failing a mandated check never becomes
    what the next boot adopts.

    ``previous`` is ``None`` when there was nothing cached for this source, and then
    there is nothing to restore TO: the rejected bytes stay, and that is the honest
    state — deleting them would turn a refused push into "no central ceiling at all",
    which a cache-only child answers by refusing to start. A stale-but-composable
    document is the better of the two, and the running ceiling is untouched either way.

    Best-effort and never raises, for the same reason ``write_cache`` is: the caller is
    already returning a failure, and a restore that cannot be written must not turn a
    reported rejection into an exception on a background timer.

    The age is deliberately NOT restarted -- ``fetched_at`` is carried from the copy
    being put back, so a fleet's ``max_cache_age_secs`` still measures from the last
    successful fetch rather than from this failure.
    """
    meta = previous.meta() if previous is not None else None
    directory = _ensure_cache_dir()
    if directory is None:
        return
    try:
        with _cache_write_lock(directory):
            observed = read_cache_meta()
            if _cache_pair(observed) != published_pair:
                logger.info(
                    "another process published a ceiling after this refresh's own publish; "
                    "leaving it in place rather than rolling back over it"
                )
                return
            # INVALIDATE FIRST, then write the previous copy back. The ordering is what
            # makes a partial failure safe: the bytes on disk right now are the ones this
            # host just REFUSED, so if the restoring write fails after a delete the cache
            # is ABSENT — a cache-only child then fails closed (a disabled app, loudly) and
            # the next boot re-fetches. Writing over them first and failing halfway would
            # leave the rejected document as the last-known-good, which is the one outcome
            # this function exists to prevent. The gap is held under the lock and the
            # gateway's own ceiling is untouched throughout.
            for leaf in (_CACHE_DOC_LEAF, _CACHE_META_LEAF):
                (directory / leaf).unlink(missing_ok=True)
            if meta is None:
                # Nothing to restore TO, and deleting was the whole job. Not the same
                # trade-off as removing a stale-but-composable copy: leaving the refused
                # bytes would make the next boot serve them from cache instead of
                # re-fetching, so a source the administrator has since corrected would not
                # reach this host until the window expired, and a cache-only child would
                # adopt the refused ceiling meanwhile.
                return
            assert previous is not None  # meta is None iff previous is, handled above
            payload = _meta_payload(
                source_digest=meta.source_digest,
                etag=meta.etag,
                last_modified=meta.last_modified,
                digest=meta.digest,
                # Carried from the copy being put back, NOT restarted: otherwise a
                # repeatedly-failing refresh would keep resetting the staleness clock and
                # a fleet's max_cache_age_secs would never notice a source that broke.
                now=meta.fetched_at,
            )
            for path, data in (
                (directory / _CACHE_DOC_LEAF, previous.body),
                (directory / _CACHE_META_LEAF, payload),
            ):
                _write_cache_file(path, data)
    except Exception:
        # Reported at ERROR with a governance incident, not a quiet warning: the caller is
        # already returning a rejection and cannot raise (it runs on a background timer),
        # so this log is the only signal that the host has lost its outage fallback.
        logger.error(
            "could not restore the previous cached ceiling; this host has no "
            "last-known-good copy until the next successful fetch",
            exc_info=True,
        )
        mark_governance_incident("degraded", "policy_distribution:restore")


def _refresh_failure(
    status: str,
    dist: PolicyDistribution,
    detail: str,
    *,
    incident: str,
    signature_state: str = "",
) -> RefreshOutcome:
    """Log, mark, audit and report one refresh failure.  The single failure tail.

    An unreachable source is a WARNING and a ``degraded`` mark — a network having a
    bad minute is not an integrity event.  Everything else is an ERROR and
    ``failed_closed``: a document was refused, which is a decision.  Both are
    derivable from *status*, so a new failure site cannot pair them wrongly.
    """
    unreachable = status == REFRESH_UNREACHABLE
    detail = _sanitize_detail(detail, dist.source)
    (logger.warning if unreachable else logger.error)("%s", detail)
    mark_governance_incident(
        "degraded" if unreachable else "failed_closed", f"policy_distribution:{incident}"
    )
    _audit_refresh(status, dist)
    return RefreshOutcome(status, detail, signature_state)


#: SEL ``reason`` per refresh status.  Fixed phrases rather than the caller's
#: ``detail``, because that detail is built for an operator reading a log and
#: embeds the source URL — see :func:`_audit_refresh`.
_AUDIT_REASONS = {
    REFRESH_APPLIED: "installed a refreshed ceiling from the central source",
    REFRESH_REJECTED: "refused the ceiling published at the central source",
    REFRESH_UNREACHABLE: "the central source could not be reached",
}


def _audit_refresh(status: str, dist: PolicyDistribution) -> None:
    """Record a refresh outcome in the SEL.  Best-effort.

    Only the outcomes that CHANGED something or failed are recorded — an unchanged
    poll is the common case and auditing it would append one HMAC-chained row per
    interval per host for a decision that decided nothing.

    **Nothing here names the endpoint.**  The source is recorded as its SCHEME, and
    the reason is a fixed phrase keyed on the status rather than the prose the
    caller logged — that prose is written for an operator debugging a fetch and
    interpolates the URL. The SEL is readable through surfaces the agent can reach,
    and the endpoint is the fleet's control plane, so naming it would tell a
    prompt-injected agent exactly where to aim. The operator gets the URL in the
    gateway log and from ``kirocrew policy fetch``, which are operator surfaces the
    way the policy file itself is.
    """
    try:
        # Both imports are function-local. The SEL and the profile module are heavy
        # relative to this trust root, and this runs at most once per refresh
        # interval, so the import is already cached by the second call. The session
        # key comes from the module that DEFINES the host sentinel rather than a
        # literal here, so the two cannot drift.
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.sel import sel

        scheme = _split_url(dist.source).scheme or "unknown"
        sel().log_governance_decision(
            scope="distribution",
            item=scheme,
            outcome="allowed" if status == REFRESH_APPLIED else "denied",
            rule=status,
            layer="policy",
            reason=_AUDIT_REASONS.get(status, status),
            session_key=HOST_SESSION_KEY,
            tool_name="policy_distribution",
        )
    except Exception:
        logger.debug("could not audit the policy refresh", exc_info=True)


class _Refresher:
    """The background poll loop.  One per process."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last: Optional[RefreshOutcome] = None
        self._last_at: float = 0.0

    def start(self, interval_secs: int) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(max(interval_secs, MIN_REFRESH_INTERVAL_SECS),),
                name="kc-policy-refresh",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the loop to exit and wait up to *timeout* for it.

        The handle is cleared only once the thread has actually died.  Clearing it
        on a timed-out join would let the next :meth:`start` create a SECOND
        polling thread beside one that is still running — and the join here is
        deliberately short (the gateway gives it a fraction of a second), so a
        timeout is the expected case whenever a fetch is in flight.  Leaving a
        dead-but-not-``None`` handle costs nothing, because ``start`` tests
        ``is_alive()`` rather than identity.  The stop event stays set either way,
        so a surviving thread exits at the end of its current cycle.
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if not thread.is_alive():
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def _run(self, interval_secs: int) -> None:
        logger.info("polling the central policy source every %ds", interval_secs)
        # Wait BEFORE the first poll: boot has just established the ceiling from
        # this same source, so an immediate re-fetch would be a redundant round
        # trip on every host at startup — and across a fleet restarting together,
        # a thundering herd against the admin's own endpoint.
        while not self._stop.wait(interval_secs):
            try:
                outcome = refresh_now()
            except Exception:  # pragma: no cover - refresh_now is already total
                logger.warning("the policy refresh cycle raised", exc_info=True)
                continue
            with self._lock:
                self._last, self._last_at = outcome, time.time()
            # Re-read the cadence from the ceiling now in effect, so a pushed
            # document can retune its own polling — an admin lengthening the
            # interval during an incident should not need a fleet restart for it to
            # take. A push that REMOVES the source stops the loop for the same
            # reason: the fleet said to stop following it.
            interval_secs = self._next_interval(interval_secs)
            if interval_secs <= 0:
                # WARNING, not info: on the policy-block channel this is reachable by
                # publishing a document that FORGOT to carry its own ``distribution``
                # block forward, and the symptom is a fleet that silently stops
                # following its administrator. Deliberate withdrawal looks identical
                # from here, so it is not a governance incident — but it must be
                # visible in the log rather than inferred from the absence of polling.
                logger.warning(
                    "the ceiling now in effect names no central source; stopped polling. "
                    "A restart still re-fetches from whatever names the source at boot; "
                    "to keep polling, carry the 'distribution' block into each published "
                    "document or set %s.",
                    POLICY_URL_ENV,
                )
                return

    @staticmethod
    def _next_interval(current: int) -> int:
        """The interval for the next cycle: 0 to stop, else a clamped positive value.

        Keeps *current* on any resolution error.  A ceiling that momentarily cannot
        be read is not an instruction to stop polling — the next cycle is what would
        fix it.
        """
        from kiro_crew.platform.governance import active_policy_distribution

        try:
            dist = resolve_distribution(active_policy_distribution())
        except PlatformCompositionError as exc:
            # No resolved `dist` to redact against, so the env source is the one to
            # withhold -- the same fallback `refresh_now`'s misconfigured arm uses.
            logger.warning(
                "could not re-read the refresh cadence; keeping it: %s",
                _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
            )
            return current
        if not dist.enabled:
            return 0
        if tier1_local_policy():
            # An operator pinned a local file mid-incident. Polling on would fetch and
            # then refuse every cycle, so stop and say so once; the next boot resolves
            # the ladder afresh.
            logger.warning(
                "a local KIROCREW_SECURITY_POLICY file now outranks the central source; "
                "stopped polling"
            )
            return 0
        return dist.effective_refresh_interval() or current

    def last(self) -> Tuple[Optional[RefreshOutcome], float]:
        with self._lock:
            return self._last, self._last_at

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()


_REFRESHER = _Refresher()


#: Callables run after a refresh INSTALLS a new ceiling, so a control that was
#: materialised from the old one can be re-derived. Registered by the layer that owns the
#: control -- this module must not learn about tailnet, dashboards or anything above it.
_POST_INSTALL_HOOKS: list[Callable[[], None]] = []


def register_post_install_hook(hook: Callable[[], None]) -> None:
    """Register *hook* to run after a live refresh installs a new ceiling.

    A ceiling swap changes what every LIVE evaluation answers, and most controls are live
    evaluations: the sandbox floor is re-read per spawn, the governance gate per tool call.
    A few are not — they are **materialised** once, when an action is taken, and then
    outlive the decision. A published tailnet origin is the case that matters: the
    ``capabilities.tailnet_origin`` gate fires at publish time, so a ceiling that later
    pins it off does not retract what is already serving. Before live refresh existed the
    ceiling only changed at boot and that could not arise; this seam is what keeps it from
    arising now.

    Append-only, like :func:`register_policy_fetcher`, and deliberately unaware of what it
    is calling. Hooks run on the refresher thread, best-effort: an exception is logged and
    the next hook still runs, because a control that cannot be re-derived must not stop the
    ceiling that was already installed from being reported as installed.
    """
    _POST_INSTALL_HOOKS.append(hook)


def _run_post_install_hooks() -> None:
    """Re-derive materialised controls after an install.  Never raises.

    A failure is reported at ERROR **and marked as a governance incident**, because a
    swallowed one is a control the ceiling calls for and the host is not applying, with
    nothing on any surface saying so. The mark is what puts it on ``security_posture`` and the
    dashboard rather than leaving it in a log nobody is reading.

    It does NOT unwind the install, and that is the deliberate direction. Rolling back would
    restore the OLD, looser ceiling — strictly less protection than the new one with one
    materialised control stale, since the tightened ceiling still binds every call the stale
    control does not pre-approve. Combined with the retry (hooks run on every confirming poll,
    and the reprojection memo advances only after a success), a transient failure costs one
    interval; a persistent one is visible and the ceiling that was installed stays installed.
    """
    for hook in list(_POST_INSTALL_HOOKS):
        try:
            hook()
        except Exception:
            logger.error(
                "a post-install governance hook failed; a control the installed ceiling calls "
                "for is not being applied on this host. It is retried on the next confirming "
                "poll; the installed ceiling is unaffected and still binds everything that "
                "control does not pre-approve",
                exc_info=True,
            )
            mark_governance_incident("degraded", "policy_distribution:post_install_hook")


def start_refresher() -> bool:
    """Start the background poll loop if the active ceiling asks for one.

    Returns whether a loop is now running.  Safe to call more than once — a second
    call while one is alive is a no-op, so a process with two entry points does not
    end up polling twice.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        logger.warning(
            "policy distribution is misconfigured; not polling: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        return False
    interval = dist.effective_refresh_interval()
    if not dist.enabled or interval <= 0:
        return False
    return _REFRESHER.start(interval)


def stop_refresher(timeout: float = 2.0) -> None:
    """Stop the background poll loop (gateway shutdown, and tests)."""
    _REFRESHER.stop(timeout)


def refresher_running() -> bool:
    return _REFRESHER.running()


# ──────────────────────────────────────────────────────────────────────────
# Read-only posture, for the CLI and the policy viewer
# ──────────────────────────────────────────────────────────────────────────


#: ``distribution_posture()["error_code"]`` values.  A machine-readable enum, never
#: a rendered sentence: the posture is served in a JSON body the dashboard
#: translates, so English prose here would ship an untranslatable string — and an
#: exception's ``str()`` would additionally leak the endpoint, which the posture
#: exists to withhold.
POSTURE_ERROR_MISCONFIGURED = "misconfigured"


def distribution_posture() -> Dict[str, object]:
    """Posture of central distribution, with no secret and no endpoint in it.

    Deliberately reports the source's SCHEME and never its URL, for the reason the
    policy viewer already withholds rule contents: the dashboard is reachable by
    the agent's own browser tooling, and the endpoint is the fleet's control plane.
    Naming it would tell a prompt-injected agent exactly where to aim, while the
    scheme alone answers what an operator actually asks — is this host centrally
    governed, and is the channel encrypted.

    Every value is a number, a boolean, or a machine-readable enum, so the
    dashboard renders translated text and no English ships in a JSON body.  A
    fetcher's exception message appears nowhere in the result: it is prose, and it
    routinely embeds the URL.

    Total: any resolution failure reports ``configured: false`` with an
    ``error_code`` rather than raising, because this feeds a status panel.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    posture: Dict[str, object] = {
        "configured": False,
        "source_scheme": "",
        "refresh_interval_seconds": 0,
        "max_cache_age_seconds": 0,
        "on_unavailable": "",
        "refresher_running": False,
        "cache_present": False,
        "cache_age_seconds": None,
        "last_refresh_status": "",
        "last_refresh_age_seconds": None,
        "error_code": "",
    }
    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        # The reason is logged, not returned: it is English and it names the
        # endpoint. ``misconfigured`` is enough for the panel to tell an operator
        # to go read the policy file, which is where the detail belongs anyway.
        logger.warning(
            "central policy distribution is misconfigured: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        posture["error_code"] = POSTURE_ERROR_MISCONFIGURED
        return posture
    if not dist.enabled:
        return posture

    posture.update(
        {
            "configured": True,
            "source_scheme": _split_url(dist.source).scheme.lower(),
            "refresh_interval_seconds": dist.effective_refresh_interval(),
            "max_cache_age_seconds": dist.max_cache_age_secs,
            "on_unavailable": dist.on_unavailable,
            "refresher_running": refresher_running(),
        }
    )
    # Metadata only: the posture needs a source and an age, and this endpoint is
    # repolled every 30 seconds by every open Security tab. Reading a document of up
    # to MAX_POLICY_BYTES to answer "how old is it" is the waste that only shows up
    # once a viewer is left open.
    meta = read_cache_meta()
    if meta is not None and meta.source_digest == _source_digest(dist.source):
        posture["cache_present"] = True
        posture["cache_age_seconds"] = int(meta.age_secs())
    outcome, at = _REFRESHER.last()
    if outcome is not None:
        posture["last_refresh_status"] = outcome.status
        posture["last_refresh_age_seconds"] = max(0, int(time.time() - at))
    return posture


__all__ = [
    "POLICY_URL_ENV",
    "POLICY_HEADERS_ENV",
    "POLICY_REFRESH_ENV",
    "POLICY_TIMEOUT_ENV",
    "POLICY_MAX_AGE_ENV",
    "POLICY_UNAVAILABLE_ENV",
    "POLICY_CACHE_ONLY_ENV",
    "cache_only",
    "central_ceiling_installed",
    "effective_max_cache_age",
    "POLICY_DISTRIBUTION_ENV_VARS",
    "CACHE_DIR_LEAF",
    "REFRESH_NOT_CONFIGURED",
    "REFRESH_UNCHANGED",
    "REFRESH_APPLIED",
    "REFRESH_REJECTED",
    "REFRESH_UNREACHABLE",
    "POSTURE_ERROR_MISCONFIGURED",
    "FetchRequest",
    "FetchedPolicy",
    "PolicyFetcher",
    "CachedPolicy",
    "RefreshOutcome",
    "register_policy_fetcher",
    "registered_policy_schemes",
    "request_headers",
    "resolve_distribution",
    "cache_dir",
    "read_cache",
    "write_cache",
    "touch_cache",
    "reset_fetch_window",
    "reset_process_state",
    "tier1_local_policy",
    "load_distributed_policy",
    "fetch_once",
    "apply_ceiling",
    "validate_ceiling",
    "refresh_now",
    "start_refresher",
    "stop_refresher",
    "refresher_running",
    "distribution_posture",
]
