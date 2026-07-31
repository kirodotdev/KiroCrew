"""Calendar-provider seam + a stdlib iCalendar (``.ics``) reader.

Upstream fetched the user's calendar through a company-internal MCP server,
with a second internal fallback that scraped an internal web endpoint.
Neither can ship publicly, and neither had a public equivalent.

This module replaces both with the same extension-point shape as
:mod:`..providers.tasks`: an ABC (:class:`CalendarProvider`), a name-keyed
factory registry (:func:`register_calendar_provider`), and a resolver
(:func:`get_calendar_provider`). **Two implementations ship**: a no-op
(``none``, the default — the app is fully usable with manually created meetings)
and :class:`IcsCalendarProvider`, which reads the iCalendar format every
calendar service can export or publish.

The parser is stdlib-only and deliberately small — it reads exactly the
``VEVENT`` fields this app displays. It is NOT a general iCalendar
implementation: recurrence expansion (``RRULE``) is not attempted, because a
correct expansion needs a full RFC 5545 engine and silently showing wrong
occurrence times is worse than showing only the series' first instance.

Fetch safety (AUTOSDE ``no-blocking-call-on-event-loop`` + ``backend-security
-controls``):

* An ``https://`` source is fetched with **aiohttp**, never ``requests``/
  ``urllib`` — those block the gateway's single event loop.
* Only ``https://`` (and ``webcal://``, rewritten to https) is accepted;
  ``http://``, ``file://`` and every other scheme is refused, so a config value
  cannot turn the fetch into a local-file read or a plaintext exfiltration hop.
* A local source must be a real file path, is read off-loop, and is size-capped.
* Redirects are followed only within the https scheme, and the response is
  size-capped while streaming so a hostile endpoint cannot exhaust memory.
* The address the validator vetted is the address the socket connects to. DNS is
  resolved **once**, on the executor, and the answer is pinned onto the
  connection, so a name whose answer changes between the check and the connect
  (DNS rebinding) cannot steer the fetch at a private endpoint.
"""

from __future__ import annotations

import abc
import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import is_sensitive_path, redact

logger = logging.getLogger("kirocrew.app.meetings")

_ALLOWED_SCHEMES = ("https",)
# webcal:// is the de-facto "subscribe to this calendar" scheme; every provider
# serves the same document over https, so it is rewritten rather than refused.
_WEBCAL_SCHEMES = ("webcal", "webcals")
_MAX_FIELD_LEN = 1000
_MAX_ATTENDEES = 100

# RFC 5545 §3.1: a logical content line may be folded across physical lines,
# with continuations starting with a single space or tab.
_FOLD_RE = re.compile(r"\r?\n[ \t]")
# ``DTSTART;TZID=America/Los_Angeles:20260730T090000`` → name, params, value.
_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9-]+)(?P<params>;[^:]*)?:(?P<value>.*)$")


@dataclass
class CalendarEvent:
    """One upcoming meeting, normalized to the app's display schema."""

    event_id: str
    title: str
    start: str = ""
    end: str = ""
    location: str = ""
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CalendarProvider(abc.ABC):
    """Abstract source of upcoming meetings.

    Contract:

    * :meth:`fetch` is ``async`` — it may do network I/O, and MUST NOT block the
      event loop (use aiohttp, or offload a blocking read to an executor).
    * It returns normalized, already-redacted :class:`CalendarEvent` records.
    * It raises :class:`CalendarError` with a user-facing message on failure;
      the sync endpoint turns that into a 502 with the message.
    """

    @property
    @abc.abstractmethod
    def provider_id(self) -> str:
        """Stable identifier used in ``config.json``'s ``calendar.provider``."""

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """Human-readable label for the settings UI."""

    @property
    def requires_source(self) -> bool:
        """True when the provider needs a user-supplied ``calendar.source``."""
        return False

    @abc.abstractmethod
    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        """Return events starting within the next *days* days."""


class CalendarError(Exception):
    """A calendar sync failed for a reason worth showing the user."""


# ── the shipped no-op provider ──────────────────────────────────────────────


class NoCalendarProvider(CalendarProvider):
    """The default: no calendar wired up.

    The app is fully usable without one — a user starts a meeting from the
    "New meeting" action and never touches a calendar. This provider exists so
    the sync endpoint has a well-defined, honest answer instead of a stack trace.
    """

    @property
    def provider_id(self) -> str:
        return k.CALENDAR_PROVIDER_NONE

    @property
    def display_name(self) -> str:
        return "No calendar"

    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        raise CalendarError(
            "No calendar is configured. Point Settings -> Calendar at an .ics "
            "file or a published https:// calendar URL."
        )


# ── the shipped .ics provider ───────────────────────────────────────────────


def _unescape(value: str) -> str:
    """Undo RFC 5545 TEXT escaping (``\\n``, ``\\,``, ``\\;``, ``\\\\``)."""
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _parse_dt(value: str, params: str) -> datetime | None:
    """Parse an iCalendar DATE-TIME / DATE value into an aware UTC datetime.

    Handles the three forms RFC 5545 allows: UTC (``…Z``), floating/TZID local
    time, and a whole-day DATE. A floating or TZID time is treated as UTC — the
    named-timezone database lookup is intentionally skipped, because the display
    layer renders in the browser's locale anyway and a wrong-by-hours guess is
    no better than a consistent one.
    """
    raw = value.strip()
    if not raw:
        return None
    if "VALUE=DATE" in params.upper() and len(raw) == 8:
        try:
            return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    fmt = "%Y%m%dT%H%M%SZ" if raw.endswith("Z") else "%Y%m%dT%H%M%S"
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _mailto_name(value: str, params: str) -> str:
    """Best display name for an ATTENDEE/ORGANIZER line."""
    match = re.search(r"CN=(?P<cn>[^;:]+)", params or "", re.IGNORECASE)
    if match:
        return _unescape(match.group("cn")).strip().strip('"')
    return re.sub(r"^mailto:", "", value.strip(), flags=re.IGNORECASE)


def parse_ics(text: str, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
    """Parse *text* as iCalendar and return events in the next *days* days.

    Stdlib-only, and only the fields this app displays. Unknown properties,
    unknown components, and malformed lines are skipped rather than raising:
    a published calendar routinely carries vendor extensions, and one bad event
    must not cost the user the whole sync.
    """
    unfolded = _FOLD_RE.sub("", text.replace("\r\n", "\n"))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=max(1, min(int(days), 365)))
    # A published calendar can carry years of history; keep a small look-back so
    # a meeting that started before the sync still appears.
    floor = now - timedelta(days=1)

    events: list[CalendarEvent] = []
    current: dict[str, Any] | None = None
    for line in unfolded.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.upper() == "BEGIN:VEVENT":
            current = {"attendees": []}
            continue
        if line.upper() == "END:VEVENT":
            if current is not None:
                event = _finalize_event(current, floor, horizon)
                if event is not None:
                    events.append(event)
                if len(events) >= k.MAX_CALENDAR_EVENTS:
                    break
            current = None
            continue
        if current is None:
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name").upper()
        params = match.group("params") or ""
        value = match.group("value")
        if name == "UID":
            current["uid"] = value.strip()
        elif name == "SUMMARY":
            current["title"] = _unescape(value)
        elif name == "DTSTART":
            current["start"] = _parse_dt(value, params)
        elif name == "DTEND":
            current["end"] = _parse_dt(value, params)
        elif name == "DURATION":
            current["duration"] = value.strip()
        elif name == "LOCATION":
            current["location"] = _unescape(value)
        elif name == "DESCRIPTION":
            current["description"] = _unescape(value)
        elif name == "ORGANIZER":
            current["organizer"] = _mailto_name(value, params)
        elif name == "ATTENDEE":
            attendees = current["attendees"]
            if len(attendees) < _MAX_ATTENDEES:
                attendees.append(_mailto_name(value, params))

    events.sort(key=lambda e: e.start)
    return events


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _duration_delta(raw: str) -> timedelta | None:
    match = _DURATION_RE.match((raw or "").strip().upper())
    if not match:
        return None
    parts = {key: int(value) for key, value in match.groupdict(default="0").items()}
    return timedelta(
        days=parts["days"], hours=parts["hours"],
        minutes=parts["minutes"], seconds=parts["seconds"],
    )


def _finalize_event(
    raw: dict[str, Any], floor: datetime, horizon: datetime
) -> CalendarEvent | None:
    start = raw.get("start")
    if not isinstance(start, datetime):
        return None
    if start < floor or start > horizon:
        return None
    end = raw.get("end")
    if not isinstance(end, datetime):
        delta = _duration_delta(str(raw.get("duration") or ""))
        end = start + (delta or timedelta(hours=1))

    def clean(value: object) -> str:
        return redact(str(value or "").strip())[:_MAX_FIELD_LEN]

    uid = clean(raw.get("uid")) or f"ics-{int(start.timestamp())}"
    return CalendarEvent(
        # The id becomes a filesystem path segment via store.safe_meeting_id, so
        # collapse anything outside its charset here rather than letting a UID
        # with slashes or spaces fail validation later.
        event_id=re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:k.MAX_MEETING_ID_LEN],
        title=clean(raw.get("title")) or "Meeting",
        start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        location=clean(raw.get("location")),
        organizer=clean(raw.get("organizer")),
        attendees=[clean(a) for a in raw.get("attendees", []) if str(a).strip()],
        description=clean(raw.get("description"))[:_MAX_FIELD_LEN],
    )


#: Redirect statuses we follow MANUALLY (301/302/303/307/308), so each hop can be
#: re-validated by :func:`_normalize_url` — and its address pinned — before the
#: gateway makes the next request.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Port assumed when a URL omits one. https is the only scheme that reaches the
#: fetch, so this is the only default needed.
_DEFAULT_HTTPS_PORT = 443


@dataclass(frozen=True)
class VettedTarget:
    """A validated URL **plus** the exact addresses vetted for its host.

    The two travel together on purpose. Returning only the URL is what made the
    old gate a TOCTOU: the validator resolved the name, approved the answer, and
    then aiohttp resolved the *same name again* for the connect. A name whose DNS
    answer changes between those two lookups — a short TTL, or a resolver that
    round-robins one public and one private record — passed the check and was
    then fetched at the private address (the classic target being cloud metadata
    at ``169.254.169.254``).

    ``addresses`` is therefore the resolution *result*, not a hint:
    :class:`_PinnedResolver` serves exactly these to the connector and never
    resolves the name a second time.
    """

    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def _normalize_url(source: str) -> VettedTarget:
    """Validate a remote calendar URL, rewriting webcal:// to https://.

    Refuses every other scheme. This is the gate that stops a ``calendar.source``
    value from turning the sync into a local-file read (``file://``), a
    plaintext hop (``http://``), or an arbitrary-protocol request.

    Returns a :class:`VettedTarget` carrying the resolved, approved addresses so
    the caller can pin the connection to them — see that class for why the
    address must travel with the URL rather than be re-derived later.

    Name resolution is a blocking syscall, so callers MUST reach this from a
    worker thread — :meth:`IcsCalendarProvider._fetch_url` offloads it for
    exactly that reason.
    """
    parts = urlsplit(source)
    scheme = parts.scheme.lower()
    if scheme in _WEBCAL_SCHEMES:
        parts = parts._replace(scheme="https")
        scheme = "https"
    if scheme not in _ALLOWED_SCHEMES:
        raise CalendarError(
            f"calendar URL must use https:// (got {scheme or 'no'} scheme)"
        )
    if not parts.hostname:
        raise CalendarError("calendar URL has no host")
    url = parts.geturl()
    # The connector keys its resolution off yarl's `raw_host`/`port` (IDNA-encoded,
    # lowercased, default port filled in), so the pin must be keyed the same way —
    # a pin recorded under urlsplit's Unicode hostname would simply never match,
    # and the connector would fall through to a fresh lookup.
    try:
        parsed = URL(url)
    except ValueError as exc:
        raise CalendarError("calendar URL is malformed") from exc
    host = parsed.raw_host
    if not host:
        raise CalendarError("calendar URL has no host")
    addresses = _vet_host_addresses(host)
    return VettedTarget(
        url=url,
        host=host,
        port=parsed.port or _DEFAULT_HTTPS_PORT,
        addresses=addresses,
    )


def _vet_host_addresses(hostname: str) -> tuple[str, ...]:
    """Resolve *hostname* and return its addresses, or refuse the lot.

    ``calendar.source`` reaches this from a dashboard ``PUT /config`` request and
    is *fetched by the gateway*, so an internal-only address would make this
    endpoint a server-side request-forgery hop into the user's own network. A
    literal IP is checked directly; a name is checked against its resolved
    addresses.

    **All-or-nothing across a multi-record answer.** A host that answers with a
    mix of public and private addresses is refused outright rather than filtered
    down to the public ones: that mix is not a legitimate calendar host, it is
    the exact signature of a rebinding attempt, and keeping the public record
    would let an attacker retry until the connector happened to pick the private
    one. The same rule covers IPv4 and IPv6 — every address in the answer must
    pass, and the surviving set is what gets pinned.
    """
    try:
        ipaddress.ip_address(hostname)
        candidates: list[str] = [hostname]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CalendarError(f"cannot resolve calendar host: {hostname}") from exc
        candidates = [str(info[4][0]) for info in infos]
    vetted: list[str] = []
    for candidate in candidates:
        # An unparseable address is refused rather than skipped. The old code
        # `continue`d here, which meant anything getaddrinfo returned in a shape
        # ipaddress could not read went UNCHECKED — a skipped candidate is an
        # unvetted one, and it could still have been connected to.
        #
        # A scoped link-local ("fe80::1%en0") parses and keeps its scope in `str()`,
        # so it never reaches the pin: `_refuse_private_address` rejects it as
        # link-local first.
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            raise CalendarError(
                "calendar URL resolved to an address that could not be parsed"
            ) from None
        _refuse_private_address(addr)
        text = str(addr)
        if text not in vetted:
            vetted.append(text)
    if not vetted:
        raise CalendarError(f"cannot resolve calendar host: {hostname}")
    return tuple(vetted)


def _refuse_private_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Refuse a loopback/private/link-local/reserved/multicast/unspecified address.

    An IPv4 address embedded in IPv6 — ``::ffff:10.0.0.1`` (v4-mapped) or
    ``2002:a00:1::`` (6to4) — is judged by the address it embeds, because that is
    the address the packet ultimately reaches. Without unwrapping, ``is_private``
    on the v6 form can read as public while the traffic lands inside the network.
    """
    for embedded in (getattr(addr, "ipv4_mapped", None), getattr(addr, "sixtofour", None)):
        if embedded is not None:
            _refuse_private_address(embedded)
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise CalendarError(
            "calendar URL resolves to a private or loopback address, which is not allowed"
        )


class _PinnedResolver(AbstractResolver):
    """Serves ONLY pre-vetted addresses, and never performs a DNS lookup.

    This is the whole anti-rebinding mechanism. :class:`aiohttp.TCPConnector`
    calls ``resolve()`` to turn a host into addresses; handing it a resolver that
    can only answer from the pin means the socket connects to the address
    :func:`_normalize_url` approved — there is no second lookup to poison.

    Why a resolver rather than rewriting the URL to its IP: the connector derives
    both the ``Host`` header and the TLS SNI / certificate-verification hostname
    from ``req.url``, so swapping the host for an IP would break certificate
    validation for every real calendar host (and the "fix" for that is disabling
    verification, which is not a fix). Substituting only the *resolution* step
    leaves the URL — and therefore ``Host``, SNI, and cert checking — untouched.

    A host that was not pinned is refused rather than resolved. Refusing is what
    keeps this fail-closed: a future code path that reached the connector with an
    unvetted host would get an error, never an unchecked connection.

    One case never reaches here: aiohttp short-circuits a **literal-IP** URL and
    connects without consulting any resolver. That is sound rather than a gap —
    there is no name to re-resolve, so there is no rebinding window, and
    :func:`_vet_host_addresses` checked that exact literal against the
    private-address rules before the request was made.
    """

    def __init__(self) -> None:
        self._targets: dict[tuple[str, int], tuple[str, ...]] = {}

    def pin(self, target: VettedTarget) -> None:
        """Authorize *target*'s vetted addresses for its host/port.

        Called once per hop, BEFORE that hop's request, so a redirect can only
        reach an address some hop's validation already approved.
        """
        self._targets[(target.host.rstrip(".").lower(), target.port)] = target.addresses

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = self._targets.get((host.rstrip(".").lower(), port))
        if not addresses:
            raise OSError(f"calendar host {host!r} was not vetted for this connection")
        results: list[ResolveResult] = []
        for address in addresses:
            is_v6 = ":" in address
            addr_family = socket.AF_INET6 if is_v6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, addr_family):
                continue
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=addr_family,
                    proto=socket.IPPROTO_TCP,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        if not results:
            raise OSError(f"no vetted address for calendar host {host!r} in this family")
        return results

    async def close(self) -> None:
        """Nothing to release — this resolver owns no socket or thread."""


class IcsCalendarProvider(CalendarProvider):
    """Reads meetings from an iCalendar document — a local file or an https URL.

    Every mainstream calendar can produce one: an exported ``.ics`` file, or a
    "publish"/"secret address in iCal format" subscription URL.
    """

    def __init__(self, source: str = "") -> None:
        self._source = (source or "").strip()

    @property
    def provider_id(self) -> str:
        return k.CALENDAR_PROVIDER_ICS

    @property
    def display_name(self) -> str:
        return "iCalendar (.ics file or URL)"

    @property
    def requires_source(self) -> bool:
        return True

    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        if not self._source:
            raise CalendarError("no calendar source configured")
        text = (
            await self._fetch_url(self._source)
            if "://" in self._source
            else await self._read_file(self._source)
        )
        # Parsing is pure CPU over a bounded (<=4 MiB) string, so it stays on the
        # loop; the two I/O paths above are the parts that were offloaded.
        return parse_ics(text, days=days)

    @staticmethod
    async def _vet(source: str) -> VettedTarget:
        """Run the gate on the executor — it performs a blocking DNS lookup."""
        return await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _normalize_url, source
        )

    async def _fetch_url(self, source: str) -> str:
        target = await self._vet(source)
        # One resolver for the whole session, pinned hop by hop. A redirect can
        # only reach an address some hop's validation already approved.
        resolver = _PinnedResolver()
        timeout = aiohttp.ClientTimeout(total=k.ICS_FETCH_TIMEOUT_SECS)
        chunks: list[bytes] = []
        try:
            # The resolver is what closes the DNS-rebinding window: validation
            # resolved the name once, on the executor, and the connector is given
            # a resolver that can ONLY answer from that result — so the socket
            # lands on the vetted address instead of whatever a second lookup
            # would return. `use_dns_cache=False` keeps the pin authoritative
            # rather than racing aiohttp's own TTL cache. Note what is NOT passed:
            # no `ssl=`/`verify_ssl=` override, so the connector keeps aiohttp's
            # default VERIFIED context, and because the request URL still carries
            # the hostname, `Host` and TLS SNI/cert validation are unchanged.
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                # `allow_redirects=False` and a MANUAL hop loop, because the
                # SSRF gate must run on every hop. Letting aiohttp follow
                # redirects validates only the FIRST url: a public host can then
                # 302 to http://169.254.169.254/ and the gateway makes that
                # request itself, which is the whole SSRF shape this endpoint is
                # otherwise careful about. Re-validating the final `resp.url`
                # after the fact is too late — the request already happened.
                for _hop in range(k.ICS_MAX_REDIRECTS + 1):
                    resolver.pin(target)
                    async with session.get(target.url, allow_redirects=False) as resp:
                        if resp.status in _REDIRECT_STATUSES:
                            location = resp.headers.get("Location", "")
                            if not location:
                                raise CalendarError("calendar URL redirected with no target")
                            # Resolve relative targets against the current url,
                            # then run the SAME validator (scheme allow-list +
                            # post-DNS private-address refusal) on the result —
                            # and pin THAT hop's answer too, on the next pass, so
                            # no hop is ever vetted-then-re-resolved.
                            nxt = str(resp.url.join(URL(location)))
                            target = await self._vet(nxt)
                            continue
                        if resp.status != 200:
                            raise CalendarError(f"calendar URL returned HTTP {resp.status}")
                        total = 0
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > k.MAX_ICS_BYTES:
                                raise CalendarError("calendar document is too large")
                            chunks.append(chunk)
                        break
                else:
                    raise CalendarError("calendar URL redirected too many times")
        except aiohttp.ClientError as exc:
            raise CalendarError(f"calendar fetch failed: {type(exc).__name__}") from exc
        except TimeoutError as exc:
            raise CalendarError("calendar fetch timed out") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _read_file(self, source: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _read_local_ics, source
        )


def _read_local_ics(source: str) -> str:
    """Read a local ``.ics`` file, size-capped. Runs on an executor thread.

    The path is operator-supplied config (never LLM- or request-supplied), but
    it is still refused when it names a sensitive credential location — the same
    floor every other file read in the gateway sits behind.
    """
    path = Path(source).expanduser()
    if is_sensitive_path(str(path)):
        raise CalendarError("calendar path is blocked by security policy")
    try:
        resolved = path.resolve()
        if not resolved.is_file():
            raise CalendarError(f"calendar file not found: {source}")
        if resolved.stat().st_size > k.MAX_ICS_BYTES:
            raise CalendarError("calendar file is too large")
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CalendarError(f"cannot read calendar file: {type(exc).__name__}") from exc


# ── registry ────────────────────────────────────────────────────────────────

CalendarProviderFactory = Callable[[str], CalendarProvider]

_factories: dict[str, CalendarProviderFactory] = {
    k.CALENDAR_PROVIDER_NONE: lambda _source: NoCalendarProvider(),
    k.CALENDAR_PROVIDER_ICS: IcsCalendarProvider,
}


def register_calendar_provider(
    provider_id: str, factory: CalendarProviderFactory | None
) -> None:
    """Register (or, with ``None``, unregister) a calendar provider.

    The factory receives the user's configured ``calendar.source`` string, so an
    edition provider can take an account id / endpoint from the same field
    without adding config keys. Registering an existing id replaces it.
    """
    key = (provider_id or "").strip().lower()
    if not key:
        raise ValueError("provider_id must be a non-empty string")
    if factory is None:
        _factories.pop(key, None)
        return
    _factories[key] = factory


def available_calendar_providers() -> list[dict[str, Any]]:
    """Registered providers as rows for the settings UI."""
    rows: list[dict[str, Any]] = []
    for key, factory in sorted(_factories.items()):
        try:
            provider = factory("")
            rows.append(
                {
                    "id": key,
                    "label": provider.display_name,
                    "requires_source": provider.requires_source,
                }
            )
        except Exception:  # pragma: no cover — a broken edition factory
            logger.warning(
                "meetings: calendar provider %s failed to construct", key, exc_info=True
            )
    return rows


def get_calendar_provider(provider_id: str = "", source: str = "") -> CalendarProvider:
    """Resolve *provider_id*, falling back to the no-op provider.

    An unknown id degrades to :class:`NoCalendarProvider`, whose ``fetch``
    raises a message telling the user to configure a calendar — better than a
    config typo silently returning zero events as if the calendar were empty.
    """
    key = (provider_id or k.DEFAULT_CALENDAR_PROVIDER).strip().lower()
    factory = _factories.get(key)
    if factory is None:
        logger.info("meetings: unknown calendar provider %r — using none", key)
        return NoCalendarProvider()
    return factory(source)
