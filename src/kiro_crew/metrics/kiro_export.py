"""The built-in Kiro telemetry destination: its gate, its region, its headers.

This module owns everything that is specific to ONE destination -- the Kiro
telemetry endpoint -- and nothing about metrics in general. The core still owns
WHAT may leave the machine (the consent gate, the attribute sanitiser, the export
allowlist, the views, the cadence); this owns only whether that destination
exists, where it points, who it says we are, and what it does when the endpoint
will not take the data.

Four things live here, in the order the export path meets them:

* **The gate** (:func:`endpoint_for`). Four switches that are not the main
  consent gate, all of which must say yes: ``telemetry.kiro_export``, the two
  environment kill switches, and ``--test-mode``. Any one of them, or a region
  the endpoint does not serve, and there is no destination at all -- not a
  destination that sends nothing, which would still appear in the dashboard's
  egress disclosure.
* **The region** (:func:`resolve_region`). A closed set of two. Anything else
  disables the destination rather than guessing which one was meant.
* **The identity headers** (:func:`identity_headers`). The sign-in the person
  already has, read from its cache on every export. This module never starts a
  sign-in and never forces a refresh: a token that is missing, unreadable or
  inside its own refresh margin sends the request as not signed in instead.
* **The failure behaviour** (:class:`KiroExportSession`). A fixed timeout, one
  warning per outage, a pause after repeated refusals, and -- the point of all of
  it -- no exception and no unbounded log stream reaching the reader thread that
  the local sink also runs on.

Nothing here is buffered across intervals: an interval that cannot be sent is
dropped, so a long outage holds no growing data. Nothing here is written to disk,
and the only file read is the sign-in cache the rest of the product already keeps.

The endpoint is a constant. There is no configuration, environment variable or
provider override that can turn it into plain HTTP or aim it at another host; the
region picks between two spellings of the same TLS host and nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Mapping, Optional

import requests

from kiro_crew import beacon
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.edition_locks import local_enable_locked

logger = logging.getLogger(__name__)

#: The destination's short label. NON-SECRET by the ``OtlpDestination``
#: contract: it is what the core logs in place of the endpoint.
DESTINATION_NAME = "kiro-telemetry"

#: The one fixed point attribute every data point of this destination carries.
#: The receiving side routes on it, which is why it is a point attribute and not
#: a resource attribute.
CLIENT_TYPE_KEY = "ClientType"
CLIENT_TYPE_VALUE = "kiro-crew"
POINT_ATTRIBUTES: "Mapping[str, str]" = {CLIENT_TYPE_KEY: CLIENT_TYPE_VALUE}

#: The resource attributes this destination may carry, narrowed from the
#: process-wide resource. Deliberately a short list of per-process constants:
#: service identity, the operating system family and the pid (which keeps gauges
#: from several processes of one install out of a single interleaved series).
#:
#: What the narrowing EXCLUDES is the reason it exists. The shared resource also
#: carries ``service.instance.id`` -- the persisted install id -- plus
#: ``host.arch``, ``host.cpu.logical_count`` and the Python runtime version. The
#: install id is a stable per-install value, and this destination counts nothing
#: per machine: the only id it ever sends is the hashed header of
#: :func:`machine_id_hash`, for people who are not signed in, and a raw install
#: id riding on every data point would make that hash pointless. The ``host.*``
#: readings are machine fingerprinting for a question this destination does not
#: ask.
RESOURCE_ALLOW: "frozenset[str]" = frozenset(
    {"service.name", "service.version", "os.type", "process.pid"}
)

#: The regions the endpoint is served from, and the one used when the setting is
#: empty. A closed set: :func:`resolve_region` refuses anything else.
DEFAULT_REGION = "us-east-1"
REGIONS: "tuple[str, ...]" = ("us-east-1", "eu-central-1")

#: HTTPS, and a constant. The region is the only part a setting can move, and it
#: is checked against :data:`REGIONS` before it is interpolated, so no setting
#: can change the scheme or the host.
_ENDPOINT_TEMPLATE = "https://telemetry.{region}.kiro.dev/v1/metrics"

#: Set by the gateway when it is started with ``--test-mode``. An environment
#: marker rather than a parameter because the processes that build their own
#: recorder (spawned agents, installed apps) inherit the environment and not the
#: gateway's arguments -- a parameter would leave them exporting from a test run.
TEST_MODE_ENV = "KIROCREW_TEST_MODE"

#: The cross-product convention (consoledonottrack.com). Read inclusively: any
#: value that is not an explicit "off" disables this destination, so
#: ``DO_NOT_TRACK=true`` disables it as surely as ``DO_NOT_TRACK=1``. Reading it
#: strictly as ``=1`` would honour the letter of the convention and miss the
#: people using it.
DO_NOT_TRACK_ENV = "DO_NOT_TRACK"

#: The product's own kill switch for this destination, for a host that wants the
#: rest of telemetry. Read as an explicit off, so ``=0`` and ``=false`` both
#: disable and anything else leaves the decision to the config key.
DISABLE_ENV = "KIROCREW_TELEMETRY_KIRO_EXPORT"

_OFF_VALUES = frozenset({"0", "false", "no", "off"})

#: Request timeout, in seconds. Owned here rather than left to the exporter's own
#: default or its environment variable: a slow endpoint must not be able to hold
#: the reader thread for longer than this, whatever a host's environment says.
TIMEOUT_SECONDS = 10.0

#: Consecutive refused (4xx) requests before this destination stops trying, and
#: how long it then stops for. A refusal is a configuration answer, not weather:
#: retrying it every minute for a day is the cost this pause exists to avoid.
REFUSALS_BEFORE_PAUSE = 3
PAUSE_SECONDS = 3600.0

#: The ONLY headers this destination lets through from anywhere other than
#: :func:`identity_headers`. Everything else that a session may be carrying is
#: suppressed per request, and the reason is concrete rather than hygienic: the
#: OTLP exporter's constructor copies ``OTEL_EXPORTER_OTLP_HEADERS`` onto the
#: session it is handed, and that variable is the documented way to authenticate
#: an OPERATOR's own collector. Without this allowlist, a host that set it would
#: send its collector's ``Authorization`` and its vendor API keys to the Kiro
#: endpoint -- credentials for a third party, carried by a request that only
#: needed a machine id. Transport headers are kept because the request does not
#: parse without them.
_TRANSPORT_HEADERS = frozenset(
    {
        "content-type",
        "content-encoding",
        "content-length",
        "accept",
        "accept-encoding",
        "user-agent",
        "connection",
    }
)

HEADER_AUTHORIZATION = "Authorization"
HEADER_TOKEN_TYPE = "x-kiro-token-type"
HEADER_PROFILE_ARN = "x-kiro-profile-arn"
HEADER_MACHINE_ID = "x-kiro-machineid"

#: Spelled as the endpoint expects. Omitted for a sign-in the endpoint already
#: defaults correctly for (Builder ID, Identity Center, social).
TOKEN_TYPE_EXTERNAL_IDP = "EXTERNAL_IDP"
TOKEN_TYPE_API_KEY = "API_KEY"

#: Prefixed before hashing so the value cannot be matched against a hash of the
#: same install id taken anywhere else, including the usage beacon's own id. The
#: version in the prefix is what lets the input change without two generations of
#: the header silently meaning the same thing.
_MACHINE_ID_PREFIX = "kirocrew-metrics-machine-id-v1:"

#: The environment bearer the rest of the product honours for a headless host. It
#: is a raw key with no refresh and no profile ARN, which is exactly why it is
#: read only when the vault holds nothing usable.
_API_KEY_ENV = "KIRO_API_KEY"

#: The tool names this destination's governance decisions are audited under, one
#: per enforcement point, so the audit trail says which decision refused: the
#: recorder build, or the recheck before an export.
AUDIT_TOOL_BUILD = "kiro_export_build"
AUDIT_TOOL_SEND = "kiro_export_send"


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


#: Set while a caller is only DESCRIBING egress posture (the Privacy panel and the
#: config route, through ``provider.otlp_egress_active``). Under it
#: :func:`endpoint_for` answers with the CONFIGURED destination and does not ask
#: the shared gate, so nothing is audited for a question that
#: decides nothing and is asked on every panel refetch. Everything else is an
#: enforcement decision and is audited, so a caller that forgets the marker
#: over-records rather than under-records.
_DISCLOSURE_ONLY: "ContextVar[bool]" = ContextVar("kiro_export_disclosure_only", default=False)


@contextmanager
def disclosure_only() -> Iterator[None]:
    """Resolve this destination inside the block as configured, asking no gate."""
    token = _DISCLOSURE_ONLY.set(True)
    try:
        yield
    finally:
        _DISCLOSURE_ONLY.reset(token)


class _AuditLatch:
    """The last verdict one session AUDITED on its export path: 1 allowed, 0 denied.

    Why the export path audits on a CHANGE and not on every call: it re-asks the
    gate before every export, about once a minute, and the audited seam writes a
    record for an allow as well as a denial. Auditing each call would append a
    row a minute that says nothing new. Recording each change still records every
    enforcement decision that does something -- the export that stops because a
    ceiling arrived, and the one that resumes because it was lifted.

    Starts at 1: a session is only built when the recorder build's own audited
    verdict allowed, so the first export compares against that. Lives on the
    session, so a rebuild starts a fresh one. Numbers only.
    """

    __slots__ = ("_lock", "_last")

    def __init__(self, last: int = 1) -> None:
        self._lock = threading.Lock()
        self._last = last

    def differs(self, observed: int) -> bool:
        with self._lock:
            return observed != self._last

    def record(self, audited: int) -> None:
        with self._lock:
            self._last = audited


#: The latch of the session whose export is being rechecked, for the duration of
#: that recheck only. A context variable rather than a parameter so the resolver
#: chain keeps its signatures.
_EXPORT_LATCH: "ContextVar[Optional[_AuditLatch]]" = ContextVar(
    "kiro_export_audit_latch", default=None
)


def _env_off(name: str) -> bool:
    """Whether *name* is set to an explicit off value."""
    return os.environ.get(name, "").strip().lower() in _OFF_VALUES


def _env_on(name: str) -> bool:
    """Whether *name* is set to anything that is not an explicit off value."""
    raw = os.environ.get(name, "").strip().lower()
    return bool(raw) and raw not in _OFF_VALUES


def do_not_track() -> bool:
    """Whether the environment asks this process not to send telemetry."""
    return _env_on(DO_NOT_TRACK_ENV)


def env_disabled() -> bool:
    """Whether the environment pins THIS destination off."""
    return _env_off(DISABLE_ENV)


def in_test_mode() -> bool:
    """Whether this process belongs to a ``--test-mode`` gateway."""
    return _env_on(TEST_MODE_ENV)


def mark_test_mode() -> None:
    """Record in the ENVIRONMENT that this is a test-mode process.

    Called once by the gateway when it starts with ``--test-mode``. The marker
    goes in the environment rather than staying an argument because the processes
    that build their own recorder -- spawned agents, installed apps -- inherit the
    environment and not the gateway's arguments, and a test run must not export
    from any of them.
    """
    os.environ[TEST_MODE_ENV] = "1"


def resolve_region(cfg: Any) -> str:
    """The region this destination sends to, or ``""`` when there is none.

    Whitespace and letter case are normalised before the value is compared: a
    region name differs from another by its letters, never by its case, so
    ``" US-East-1 "`` names the same region as ``us-east-1`` and reading it as
    one is not a guess about intent. A value that is still not a region this
    endpoint is served from returns ``""`` -- the destination then does not
    exist, rather than silently sending to whichever region looked closest.
    """
    raw = str(getattr(cfg, "kiro_export_region", "") or "").strip()
    if not raw:
        return DEFAULT_REGION
    region = raw.lower()
    if region not in REGIONS:
        # The VALUE is safe to log (a short config token, not a credential), and
        # without it the operator cannot tell a typo from an unsupported region.
        logger.debug(
            "telemetry.kiro_export_region %r is not one of %s; "
            "the Kiro telemetry destination is disabled",
            raw,
            ", ".join(REGIONS),
        )
        return ""
    return region


def outbound_permitted() -> "beacon.Verdict":
    """The product's ONE gate for any outbound telemetry, reused rather than redone.

    ``beacon.telemetry_permitted`` already owns every suppression that is not
    specific to one signal: ``KIROCREW_TELEMETRY_DISABLED``, the
    ``capabilities.telemetry`` ceiling an administrator sets, a CI environment, a
    non-default ``KIROCREW_HOME`` (a dev home, a pod, a preview), and the
    first-run privacy disclosure. Its own docstring names the failure a second
    gate causes: a person turns telemetry off and one of two routes keeps
    sending. So this destination ASKS that function, and only the switches that
    are specific to it live in this module.

    ``enabled=True`` is passed deliberately. That parameter is the asking route's
    OWN switch; this destination's is ``telemetry.kiro_export``, already checked
    by the caller. Passing it here would make a refusal report
    ``telemetry.beacon_enabled is false`` about a host where that is not the
    reason.

    Audited the way every other enforcement caller of that function is, through
    its ``audit_tool``, so a refusal leaves a governance decision record:

    * **Recorder build** (the default): audited on every call, as
      ``beacon.should_send`` audits each send.
    * **Export recheck** (inside :func:`destination_still_resolves` for a
      session): asked unaudited, and asked again WITH the audit identity only when
      the answer differs from the last one that session audited. See
      :class:`_AuditLatch` for why.
    * **Disclosure** (inside :func:`disclosure_only`): not asked at all;
      :func:`endpoint_for` answers with the configured destination there.

    FAILS CLOSED: a verdict that cannot be computed refuses, and a config that
    cannot be read is not an acknowledgement.
    """
    try:
        acked = bool(KiroCrewConfig.load().dashboard.privacy_acked)
    except Exception as exc:  # noqa: BLE001 - an unreadable config is not consent
        logger.debug("privacy acknowledgement unreadable (%s)", type(exc).__name__)
        acked = False
    try:
        latch = _EXPORT_LATCH.get()
        if latch is None:
            return beacon.telemetry_permitted(
                enabled=True, acked=acked, audit_tool=AUDIT_TOOL_BUILD
            )
        verdict = beacon.telemetry_permitted(enabled=True, acked=acked)
        if latch.differs(int(verdict.ok)):
            # The answer changed since this session last audited one: ask again
            # through the audited seam and act on THAT answer, so the decision
            # that is recorded is the decision that is enforced.
            verdict = beacon.telemetry_permitted(
                enabled=True, acked=acked, audit_tool=AUDIT_TOOL_SEND
            )
            latch.record(int(verdict.ok))
        return verdict
    except Exception as exc:  # noqa: BLE001 - an unresolvable gate refuses
        logger.debug("outbound telemetry gate unresolvable (%s)", type(exc).__name__)
        return beacon.Verdict(False, "the outbound telemetry gate did not resolve", "error")


def endpoint_for(cfg: Any) -> str:
    """The endpoint this destination should use, or ``""`` for no destination.

    One place to read for "why is nothing being sent": this destination's own
    switches, then the shared gate of :func:`outbound_permitted`.

    ``telemetry.enabled`` is deliberately NOT checked here. The core checks
    consent before it builds a recorder, and the dashboard asks this same
    resolver the different question of whether enabling collection WOULD start
    egress. Folding consent in would answer that question "no" on a host where
    flipping the switch starts sending. The SEND path asks consent separately, in
    :func:`destination_still_resolves`.

    Under :func:`disclosure_only` the answer is the CONFIGURED destination: the
    shared gate is not asked, because a temporary "no" from it would describe a
    host that can start sending without any change by the person as one that
    sends nothing.

    The cheap destination-specific switches come first and the shared gate last,
    because that gate reads the config and, on an opted-in host, stats the
    first-send stamp. An install with ``telemetry.kiro_export`` off -- the default
    -- pays none of it.
    """
    if not bool(getattr(cfg, "kiro_export", False)):
        return ""
    # An enterprise build ignores this local switch (no governance key names an
    # OTLP host yet). The core already drops it before any provider reads the
    # config; asked here too because the per-export recheck reads the config
    # itself, not through the core.
    if local_enable_locked("telemetry.otlp_hosts"):
        return ""
    if do_not_track():
        logger.debug("%s is set; the Kiro telemetry destination is disabled", DO_NOT_TRACK_ENV)
        return ""
    if env_disabled():
        logger.debug("%s pins the Kiro telemetry destination off", DISABLE_ENV)
        return ""
    if in_test_mode():
        logger.debug("test mode; the Kiro telemetry destination is disabled")
        return ""
    region = resolve_region(cfg)
    if not region:
        return ""
    if _DISCLOSURE_ONLY.get():
        # A DESCRIPTION of posture (the Privacy panel, the config route's
        # refusal to enable collection as local-only) answers with the
        # CONFIGURED destination. The shared gate is left out here: it can
        # change without the person doing anything (a policy denial lifts, the
        # first-run notice is acknowledged), and a "no egress" answer while it
        # holds would let collection be enabled as local-only and then start
        # sending. Recorder builds and every export still ask it.
        return _ENDPOINT_TEMPLATE.format(region=region)
    verdict = outbound_permitted()
    if not verdict.ok:
        # The CODE, not the prose: that sentence is written for the beacon's own
        # CLI output, and the code is the stable discriminant.
        logger.debug(
            "outbound telemetry is not permitted (%s); the Kiro destination is disabled",
            verdict.code,
        )
        return ""
    return _ENDPOINT_TEMPLATE.format(region=region)


# ---------------------------------------------------------------------------
# identity (RFC 4.4)
# ---------------------------------------------------------------------------


def _stored_token() -> Optional[Any]:
    """The cached sign-in, if one can serve an export right now.

    Never starts a sign-in and never refreshes. ``is_expired`` is the product's
    own refresh-margin predicate, so a token the engine would renew before using
    counts as absent here rather than being sent and refused.

    Imported lazily: the token store pulls in the encrypted vault and the
    cryptography stack, and this module is imported wherever a recorder is built.
    """
    try:
        from kiro_crew.auth.store import TokenStore
        from kiro_crew.config.paths import data_home

        token = TokenStore(data_home()).resolve()
    except Exception as exc:  # noqa: BLE001 - any failure means "not signed in"
        # The exception CLASS only. A traceback or a message from this path can
        # carry the value being parsed, and nothing about the sign-in may reach a
        # log line.
        logger.debug(
            "sign-in cache unreadable (%s); exporting as not signed in", type(exc).__name__
        )
        return None
    if token is None:
        return None
    try:
        if token.is_expired():
            return None
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.debug(
            "sign-in expiry unreadable (%s); exporting as not signed in", type(exc).__name__
        )
        return None
    return token


def _token_type(token: Any) -> str:
    """The ``x-kiro-token-type`` value for *token*, or ``""`` to omit the header.

    Omitted for Builder ID, Identity Center and social sign-ins: the endpoint
    defaults those correctly, and sending a value we guessed would be worse than
    sending none.
    """
    provider = str(getattr(token, "provider", "") or "")
    identity = str(getattr(token, "identity", "") or "")
    auth_method = str(getattr(token, "auth_method", "") or "")
    if provider == "ApiKey" or auth_method == "api_key":
        return TOKEN_TYPE_API_KEY
    if identity == "external_idp" or auth_method == "external_idp":
        return TOKEN_TYPE_EXTERNAL_IDP
    return ""


def machine_id_hash() -> str:
    """A one-way hash of the existing install id, or ``""`` when there is none.

    The endpoint requires one of the two identity headers, so a person who is not
    signed in is identified by this value and nothing else. It is a hash of an id
    that already exists on disk, under a fixed prefix, which gives two
    properties: no new file is created for it, and it cannot be matched against
    the same install id used anywhere else.

    ``""`` when the install id cannot be read. There is no fallback id: a
    constant shared by every install would put every install into one rate limit,
    and a fresh per-process one would count a restart as a new machine. The
    caller drops the interval instead.
    """
    try:
        install_id = beacon.install_id(create=False)
    except Exception as exc:  # noqa: BLE001 - defensive; an unreadable id is no id
        logger.debug("install id unreadable (%s)", type(exc).__name__)
        return ""
    if not install_id:
        return ""
    return hashlib.sha256((_MACHINE_ID_PREFIX + install_id).encode("utf-8")).hexdigest()


def identity_headers() -> "dict[str, str]":
    """The identity headers for one export, or ``{}`` when there are none.

    Resolved per request, not per session: the sign-in can land, lapse or change
    account while the process runs, and the next export must say so.

    ``{}`` means neither identity is available -- no usable sign-in and no
    readable install id. The endpoint requires one of the two, so the caller
    drops the interval rather than sending a request that cannot be accepted.
    """
    token = _stored_token()
    if token is not None:
        access_token = str(getattr(token, "access_token", "") or "")
        if access_token:
            headers = {HEADER_AUTHORIZATION: f"Bearer {access_token}"}
            token_type = _token_type(token)
            if token_type:
                headers[HEADER_TOKEN_TYPE] = token_type
            profile_arn = str(getattr(token, "profile_arn", "") or "")
            if profile_arn:
                headers[HEADER_PROFILE_ARN] = profile_arn
            return headers
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if api_key:
        # A raw bearer with no refresh and no profile ARN, which is what the
        # endpoint's API_KEY token type describes.
        return {
            HEADER_AUTHORIZATION: f"Bearer {api_key}",
            HEADER_TOKEN_TYPE: TOKEN_TYPE_API_KEY,
        }
    machine_id = machine_id_hash()
    if machine_id:
        return {HEADER_MACHINE_ID: machine_id}
    return {}


# ---------------------------------------------------------------------------
# failure behaviour (RFC 4.7)
# ---------------------------------------------------------------------------


class _NoAmbientAuth(requests.auth.AuthBase):
    """Authenticates nothing, and exists so nothing else does it for us.

    ``requests`` reads ``.netrc`` and applies a matching entry as Basic
    authentication only when a session has no ``auth`` of its own. A ``default``
    entry in that file matches every host, so an operator who keeps one would
    authenticate to the Kiro telemetry endpoint with it. Occupying the slot with
    a no-op closes that without turning off ``trust_env``, which a host behind an
    environment-configured proxy still needs.
    """

    def __call__(self, request):
        return request


def destination_still_resolves(url: str, *, latch: Optional[_AuditLatch] = None) -> bool:
    """Whether re-resolving this destination NOW still yields exactly *url*.

    The one invariant the export path is allowed to rely on, and deliberately the
    WHOLE decision rather than any subset of it. Every switch that can create this
    destination can also be turned off while the process runs, and nothing
    rebuilds the metrics reader when one is: the reader holds its session and its
    endpoint for the life of the process, the gateway's config watcher is
    registered at boot only, and the periodic consent recheck re-reads
    ``telemetry.enabled`` alone. A check that asked about one switch would
    therefore have to be extended by one line per switch, and each omission is
    the same defect -- data still leaving after someone turned it off.

    So this asks :func:`endpoint_for` against freshly loaded configuration and
    compares the ANSWER to the URL in hand. That covers, with no enumeration:
    ``telemetry.kiro_export`` turned off, ``DO_NOT_TRACK`` or
    ``KIROCREW_TELEMETRY_KIRO_EXPORT`` set in THIS process's environment
    afterwards (a variable exported in another shell never reaches a running
    process, so for an operator the two take effect at the next start), a
    test-mode marker appearing, the region changed (the old endpoint stops
    resolving, so an in-flight reader cannot keep posting to the region nobody
    chose any more), a region that became invalid, and every suppression
    :func:`outbound_permitted` owns -- the policy ceiling included.

    Collection consent is asked here as well, though :func:`endpoint_for` leaves
    it out: ``telemetry.enabled`` (or its ``KIROCREW_TELEMETRY`` pin, resolved by
    the core's own ``provider._consent_enabled``). Withdrawing consent shuts the
    reader down, and a reader's shutdown runs one final export; without this
    check that last batch would still be sent after the person said stop.

    Costs one configuration read per export, which is a fingerprint-cache hit in
    the steady state, on the metrics reader thread. Nothing on the turn path.

    FAILS CLOSED: configuration that cannot be read resolves to no destination.

    *latch* is the calling session's audit state (see :class:`_AuditLatch`). It
    changes only which answers are AUDITED, never the answer itself.
    """
    token = _EXPORT_LATCH.set(latch)
    try:
        # Deferred: metrics.provider imports this module's caller chain, and by
        # the time an export runs it is loaded anyway.
        from kiro_crew.metrics.provider import _consent_enabled

        telemetry = KiroCrewConfig.load().telemetry
        if not _consent_enabled(telemetry):
            logger.debug("telemetry collection is off; the Kiro destination sends nothing")
            return False
        current = endpoint_for(telemetry)
    except Exception as exc:  # noqa: BLE001 - unreadable config is no destination
        logger.debug("telemetry configuration unreadable (%s)", type(exc).__name__)
        return False
    finally:
        _EXPORT_LATCH.reset(token)
    return bool(current) and current == str(url)


def _dropped(url: str, reason: str) -> requests.Response:
    """An OK response this destination answers itself, having sent nothing.

    Every outcome that is not a real success becomes one of these, which is what
    keeps the two promises the destination has to the reader thread it shares with
    the local sink: nothing raises out of it, and nothing logs per interval.

    It has to be an OK response, and that is worth being explicit about. A
    retryable failure makes the exporter sleep and post again inside that thread;
    a non-retryable one makes it log the endpoint's own response text at error
    level every minute. Both are precisely the costs this destination exists to
    avoid, and the batch is dropped in every case -- the worst case here is a lost
    minute of numbers. So the response says "accepted, and discarded", the reason
    is recorded on it for anyone holding one, and the drop itself is logged by
    whichever branch produced it.
    """
    response = requests.Response()
    response.status_code = 200
    response.reason = f"not sent: {reason}"
    response.url = url
    response._content = b""
    return response


class KiroExportSession(requests.Session):
    """The transport for the Kiro telemetry destination, and its failure policy.

    Three things ride on being a ``Session`` rather than living in the exporter:
    the identity headers are re-read per request (a sign-in that lands mid-process
    is used by the next export), the timeout is this destination's and not the
    environment's, and every failure is handled at the point where it happens --
    the destination boundary -- instead of propagating into the metrics reader
    thread that the local JSONL sink also runs on.

    The failure policy, in full:

    * The whole destination decision is re-resolved per export
      (:func:`destination_still_resolves`), so any switch turned off while the
      process runs stops this destination at the next interval rather than at the
      next restart.
    * One request per export, with a :data:`TIMEOUT_SECONDS` timeout.
    * A refused request (4xx, including an expired token's 401) is dropped. It is
      never retried as not-signed-in, which would count one interval twice.
    * :data:`REFUSALS_BEFORE_PAUSE` refusals in a row pause this destination for
      :data:`PAUSE_SECONDS`; a success anywhere in between clears the streak. A
      server error or an unreachable network is weather, not a refusal, and does
      not count toward the pause.
    * The first failure of an outage logs one warning; the rest log at debug. The
      first success after a failure logs one info line.
    * Nothing is held between intervals and nothing is written to disk.

    No log line here carries the token, the profile ARN, the machine id header or
    the endpoint's own response body. A cause is a status code or an exception
    class name, both of which are ours.

    Three things this session refuses to carry, each of which would otherwise send
    a credential that belongs to somebody else to this endpoint:

    * **Any header it was not asked for.** See :data:`_TRANSPORT_HEADERS`.
    * **Ambient ``.netrc`` credentials.** ``requests`` applies a matching
      ``.netrc`` entry as Basic authentication whenever ``trust_env`` is on and no
      explicit auth is set. A ``default`` entry matches every host, so an operator
      who keeps one would authenticate to this endpoint with it. Setting an
      explicit no-op ``auth`` suppresses that lookup while leaving ``trust_env``
      on, so a host that needs an environment proxy to reach anything still has
      one.
    * **Redirects.** The endpoint is a constant, so there is no legitimate
      redirect to follow, and following one is how a bearer token reaches a host
      nobody chose. ``rebuild_auth`` is also overridden, because it re-applies
      ``.netrc`` on a redirect independently of ``auth``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        #: Injectable so the pause can be tested without waiting an hour.
        self._clock = clock
        self._state_lock = threading.Lock()
        self._refusals = 0
        self._paused_until: Optional[float] = None
        self._warned = False
        #: What this session last audited on its export path; fresh per session,
        #: so per recorder build.
        self._audit_latch = _AuditLatch()
        # Explicit, and a no-op: present ONLY so requests' ``.netrc`` lookup,
        # which runs when ``auth`` is unset, never happens. The real identity is
        # applied per request in ``request`` below, because it must be able to
        # cancel the send when there is no identity at all, which an auth hook
        # cannot do.
        self.auth = _NoAmbientAuth()

    # -- requests.Session -----------------------------------------------------

    def rebuild_auth(self, prepared_request, response):  # type: ignore[override]
        """Strip authentication across a redirect and re-apply nothing.

        ``Session.rebuild_auth`` re-reads ``.netrc`` for the new URL whenever
        ``trust_env`` is on, which would reintroduce exactly the ambient
        credential this session exists to keep out. Redirects are refused
        outright in ``request``, so this is defence in depth for any path that
        reaches it anyway.
        """
        prepared_request.headers.pop(HEADER_AUTHORIZATION, None)
        return None

    def _outgoing_headers(self, supplied: Optional[Mapping[str, Any]]) -> "dict[str, Any]":
        """The headers for one request: transport, then identity, nothing else.

        A ``None`` value tells ``requests`` to DROP that key when it merges the
        per-request headers over the session's, which is how a header this
        session never asked for -- an exporter's copy of
        ``OTEL_EXPORTER_OTLP_HEADERS``, say -- is removed rather than sent.
        """
        headers: "dict[str, Any]" = {}
        for source in (self.headers, supplied or {}):
            for name, value in source.items():
                if str(name).lower() in _TRANSPORT_HEADERS:
                    headers[name] = value
                else:
                    headers[name] = None
        return headers

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        # This destination's timeout, not the caller's: the exporter passes its
        # own (from OTEL_EXPORTER_OTLP_METRICS_TIMEOUT when a host sets it), and a
        # host must not be able to make the reader thread wait longer.
        kwargs["timeout"] = TIMEOUT_SECONDS
        # THE invariant, and the only gate on this path: a request leaves only if
        # re-resolving the whole destination from the CURRENT configuration still
        # yields exactly this URL. See :func:`destination_still_resolves`.
        #
        # A refusal here is a POLICY stop, not a failure: it logs at debug, it
        # does not warn, and it touches neither the refusal streak nor the pause,
        # because the endpoint did nothing wrong and the next interval must ask
        # again rather than sit out an hour.
        if not destination_still_resolves(url, latch=self._audit_latch):
            logger.debug("the Kiro destination no longer resolves here; interval dropped")
            return _dropped(url, "destination no longer resolves")
        if self._paused():
            logger.debug("Kiro telemetry export is paused; this interval is dropped")
            return _dropped(url, "export paused")
        identity = identity_headers()
        if not identity:
            logger.debug("no usable sign-in and no readable install id; this interval is dropped")
            return _dropped(url, "no identity header")
        headers = self._outgoing_headers(kwargs.get("headers"))
        headers.update(identity)
        kwargs["headers"] = headers
        # The endpoint is a constant, so there is no redirect worth following,
        # and following one is how a bearer token reaches a host nobody chose.
        kwargs["allow_redirects"] = False
        # The exporter passes the host's OTLP TLS settings
        # (OTEL_EXPORTER_OTLP_CERTIFICATE / _CLIENT_CERTIFICATE / _CLIENT_KEY),
        # which are set for the operator's OWN collector: a private CA bundle
        # would fail every handshake with this public endpoint, and a client
        # certificate is that collector's credential, not this one's. Standard
        # verification, no client certificate.
        kwargs["verify"] = True
        kwargs["cert"] = None
        try:
            response = super().request(method, url, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the destination boundary
            self._note_failure(type(exc).__name__, refusal=False)
            return _dropped(url, "transport failure")
        if response.ok:
            self._note_success()
            return response
        status = response.status_code
        self._note_failure(f"HTTP {status}", refusal=400 <= int(status) < 500)
        return _dropped(url, f"HTTP {status}")

    # -- failure accounting ---------------------------------------------------

    def _paused(self) -> bool:
        with self._state_lock:
            until = self._paused_until
            if until is None:
                return False
            if self._clock() >= until:
                # The pause is over: clear it and the streak that caused it, so
                # the next export is a fresh attempt rather than an instant
                # fourth refusal.
                self._paused_until = None
                self._refusals = 0
                return False
            return True

    def _note_failure(self, cause: str, *, refusal: bool) -> None:
        with self._state_lock:
            if refusal:
                self._refusals += 1
            else:
                # "In a row" means in a row: a server error between two refusals
                # breaks the streak, because what the pause is for is a refusal
                # that will not stop being one.
                self._refusals = 0
            first = not self._warned
            self._warned = True
            starting_pause = (
                refusal and self._refusals >= REFUSALS_BEFORE_PAUSE and self._paused_until is None
            )
            if starting_pause:
                self._paused_until = self._clock() + PAUSE_SECONDS
        if first:
            logger.warning(
                "Kiro telemetry export failed (%s); this interval is dropped. "
                "Further failures are logged at debug level",
                cause,
            )
        else:
            logger.debug("Kiro telemetry export failed (%s); this interval is dropped", cause)
        if starting_pause:
            logger.debug(
                "Kiro telemetry export paused for %d minutes after %d refused requests",
                int(PAUSE_SECONDS // 60),
                REFUSALS_BEFORE_PAUSE,
            )

    def _note_success(self) -> None:
        with self._state_lock:
            recovered = self._warned
            self._warned = False
            self._refusals = 0
            self._paused_until = None
        if recovered:
            logger.info("Kiro telemetry export is reaching the endpoint again")


def build_session() -> requests.Session:
    """The authenticated, failure-contained transport for this destination.

    Opens no socket by itself: a ``Session`` connects on its first request, so
    building one to describe a destination costs nothing on a host that never
    exports.
    """
    return KiroExportSession()
