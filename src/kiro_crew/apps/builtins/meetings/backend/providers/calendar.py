"""Calendar-provider seam + a stdlib iCalendar (``.ics``) reader.

Upstream fetched the user's calendar through a company-internal MCP server,
with a second internal fallback that scraped an internal web endpoint.
Neither can ship publicly, and neither had a public equivalent.

This module replaces both with the same extension-point shape as
:mod:`..providers.tasks`: an ABC (:class:`CalendarProvider`), a name-keyed
factory registry (:func:`register_calendar_provider`), and a resolver
(:func:`get_calendar_provider`). **Three implementations ship**: a no-op
(``none``, the default — the app is fully usable with manually created meetings),
:class:`IcsCalendarProvider`, which reads the iCalendar format every calendar
service can export or publish, and :class:`CalDavCalendarProvider`, which runs a
time-ranged ``calendar-query`` against a CalDAV collection and reuses the same
parser on the iCalendar documents that come back.

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
import base64
import hashlib
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from kiro_crew import hooks, link_unfurl
from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import credentials
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact

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


#: ``DTSTART;TZID=America/Los_Angeles:…`` -> the zone name.
_TZID_RE = re.compile(r"TZID=([^;:]+)", re.IGNORECASE)


def _tzid_of(params: str) -> ZoneInfo | None:
    """The ``TZID`` parameter as a tzinfo, or ``None`` when absent/unresolvable.

    Never raises: the parameter is untrusted text from a downloaded calendar, and a
    zone this host's database does not carry must degrade to "no zone" (the caller
    then reads the time as UTC) rather than fail the whole sync.
    """
    match = _TZID_RE.search(params or "")
    if not match:
        return None
    name = match.group(1).strip().strip('"')
    # Some exporters emit a Windows zone name or a custom VTIMEZONE id, neither of
    # which is an IANA key.
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.debug("meetings: unknown calendar TZID %r; reading the time as UTC", name)
        return None


#: Length of the disambiguating digest appended to a sanitized event id.
#: Eight hex characters of sha256 — enough that a collision between two UIDs in one
#: person's calendar is not a practical concern, short enough to leave the readable
#: part of the id intact.
_UID_DIGEST_LEN = 8


def _event_id_for(uid: str) -> str:
    """A filesystem-safe meeting id that is UNIQUE per original UID.

    The id becomes a directory name via ``store.safe_meeting_id``, so characters
    outside its charset have to be collapsed here rather than failing validation
    later. Collapsing alone was not injective, and the consequence was not cosmetic:
    ``event/1`` and ``event?1`` both sanitized to ``event_1``, so two distinct
    calendar entries became ONE list row and shared a meeting directory — each
    overwriting the other's notes and tasks. Truncation at
    ``MAX_MEETING_ID_LEN`` collided the same way for two long UIDs sharing a prefix.

    A digest of the ORIGINAL uid is therefore appended, inside the cap: the readable
    stem stays recognizable, and the id is stable across syncs (the same UID always
    yields the same id, which is what lets a meeting be re-opened).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", uid)
    # `store.safe_meeting_id` rejects a LEADING dot outright, so an id may never
    # begin with one — that is what stops it naming a dotfile or `..`. A UID like
    # `../escape` sanitizes to `.._escape`, which would keep the dots and be refused
    # downstream, so the meeting simply could not be opened. Stripped rather than
    # substituted because the digest already carries the distinction.
    safe = safe.lstrip(".") or "event"
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:_UID_DIGEST_LEN]
    stem = safe[: max(1, k.MAX_MEETING_ID_LEN - _UID_DIGEST_LEN - 1)]
    return f"{stem}-{digest}"


def _parse_dt(value: str, params: str) -> datetime | None:
    """Parse an iCalendar DATE-TIME / DATE value into an aware UTC datetime.

    Handles the three forms RFC 5545 allows: UTC (``…Z``), local time with a
    ``TZID``, and a whole-day DATE.

    A ``TZID`` is RESOLVED, not assumed to be UTC. Treating
    ``DTSTART;TZID=America/Los_Angeles:20260803T090000`` as UTC displayed a 09:00
    meeting as 02:00 — and the previous rationale for that ("the display layer
    renders in the browser's locale anyway, so a wrong-by-hours guess is no better
    than a consistent one") does not hold: the value is not merely rendered
    differently, it names the wrong instant, so the sync window and the ordering
    are wrong too. ``zoneinfo`` is stdlib, and Windows carries ``tzdata`` as a
    declared dependency, so the lookup costs nothing.

    An unknown or unresolvable zone falls back to UTC rather than dropping the
    event: a visible meeting at a possibly-wrong hour beats a meeting that silently
    is not there. A FLOATING time (no ``TZID``, no ``Z``) is genuinely
    zone-less by spec, and UTC is the only defensible reading without the
    calendar's own default zone.
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
        parsed = datetime.strptime(raw, fmt)
    except ValueError:
        return None
    if raw.endswith("Z"):
        return parsed.replace(tzinfo=timezone.utc)
    zone = _tzid_of(params)
    if zone is not None:
        return parsed.replace(tzinfo=zone).astimezone(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


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

#: Ceiling on a parsed ``DURATION``, in days. Ten years — orders of magnitude above
#: any real meeting (the sync horizon is :data:`k.CALENDAR_SYNC_DAYS` days) and orders
#: of magnitude below ``timedelta.max``, so a delta that passes here cannot push a
#: datetime out of range when the caller adds it.
_MAX_DURATION_DAYS = 3650


def _duration_delta(raw: str) -> timedelta | None:
    """Parse an iCalendar DURATION, or ``None`` when it is unusable.

    A syntactically VALID duration can still be unrepresentable: the pattern's ``\\d+``
    groups are unbounded, so ``P<400 digits>D`` matches and then ``timedelta`` raises
    ``OverflowError: Python int too large to convert to C int``. Nothing up the stack
    catches it, so a remote ``.ics`` carrying one turned a calendar sync into a 500 —
    and the value comes from a REMOTE server, which is exactly the input this module
    already treats as untrusted for URLs and timezone ids.

    ``ValueError`` is caught alongside it because ``timedelta`` reports its own range
    limit that way (``days`` must fit ``-999999999..999999999``), which a 10-digit
    value reaches long before the C-int boundary. Returning ``None`` puts an
    unparseable duration on the same footing as a malformed one: the event keeps its
    start and is simply given no end.
    """
    match = _DURATION_RE.match((raw or "").strip().upper())
    if not match:
        return None
    try:
        parts = {key: int(value) for key, value in match.groupdict(default="0").items()}
        delta = timedelta(
            days=parts["days"], hours=parts["hours"],
            minutes=parts["minutes"], seconds=parts["seconds"],
        )
    except (ValueError, OverflowError):
        logger.debug("meetings: unrepresentable calendar DURATION, ignoring it")
        return None
    # Bounded, not merely CONSTRUCTIBLE.
    #
    # Catching the constructor's own errors was not enough: `P999999999D` is exactly
    # `timedelta.max.days`, so it builds fine and the overflow simply moved one line
    # down, to `start + delta` in `_finalize_event` — where `datetime + timedelta`
    # raises `OverflowError: date value out of range`. `handle_calendar_sync` catches
    # only `CalendarError`, so the sync answered 500 and the WHOLE feed was lost rather
    # than the single event this module promises to skip.
    #
    # The ceiling is the thing to check, because a delta this function returns is added
    # to a datetime by its caller — "can I build it" and "can it be used" are different
    # questions. `_MAX_DURATION_DAYS` is far above any real meeting and far below the
    # datetime range, so no arithmetic downstream can leave it.
    if abs(delta) > timedelta(days=_MAX_DURATION_DAYS):
        logger.debug("meetings: calendar DURATION exceeds the sane ceiling, ignoring it")
        return None
    return delta


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
    return build_event(
        uid=str(raw.get("uid") or ""),
        title=str(raw.get("title") or ""),
        start=start,
        end=end,
        location=raw.get("location"),
        organizer=raw.get("organizer"),
        attendees=[str(a) for a in raw.get("attendees", [])],
        description=raw.get("description"),
        uid_prefix="ics",
    )


def build_event(
    *,
    uid: str,
    title: str,
    start: datetime,
    end: datetime | None,
    location: object = "",
    organizer: object = "",
    attendees: list[str] | None = None,
    description: object = "",
    uid_prefix: str = "event",
) -> CalendarEvent:
    """Normalize and REDACT one event from any provider's raw fields.

    Every provider funnels through here rather than building a
    :class:`CalendarEvent` itself, for two reasons that are worth stating because
    the alternative looks equally reasonable:

    * **Redaction lives in one audited place.** ``providers/calendar.py`` is a
      registered redaction sink in :mod:`kiro_crew.security_posture`; a provider
      module that called :func:`redact` itself would be a second sink to classify
      and keep classified. One helper means one thing to audit for all of them.
    * **The id derivation cannot be skipped.** ``event_id`` becomes a meeting
      DIRECTORY name, and :func:`_event_id_for` is what makes it both filesystem
      safe and injective. A provider that formatted its own id could reintroduce
      the collision that made two calendar entries share one meeting directory.

    ``end`` defaults to an hour after ``start`` when a provider gives none, which
    is what :func:`_finalize_event` already does for a ``.ics`` event with neither
    ``DTEND`` nor ``DURATION``.

    ``uid_prefix`` names the SOURCE in a synthesized id, for the case where an
    event arrives with no usable identifier. It is a parameter rather than a
    constant so a Google event does not end up labelled ``ics-``: the id is
    user-visible in a URL, and a wrong provenance in it is the kind of small lie
    that costs somebody an afternoon later.
    """

    def clean(value: object) -> str:
        return redact(str(value or "").strip())[:_MAX_FIELD_LEN]

    resolved_end = end if isinstance(end, datetime) else start + timedelta(hours=1)
    safe_uid = clean(uid) or f"{uid_prefix}-{int(start.timestamp())}"
    return CalendarEvent(
        event_id=_event_id_for(safe_uid),
        title=clean(title) or "Meeting",
        start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=resolved_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        location=clean(location),
        organizer=clean(organizer),
        attendees=[clean(a) for a in (attendees or []) if str(a).strip()],
        description=clean(description),
    )


#: Redirect statuses we follow MANUALLY (301/302/303/307/308), so each hop can be
#: re-validated by :func:`_normalize_url` — and its address pinned — before the
#: gateway makes the next request.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Port assumed when a URL omits one. https is the only scheme that reaches the
#: fetch, so this is the only default needed.
_DEFAULT_HTTPS_PORT = 443
#: :class:`link_unfurl.UnfurlRejected` codes in the words this app shows the
#: operator. Those codes are wire codes for the unfurl endpoint; someone who just
#: typed a calendar URL needs to know which of the two things is wrong with it,
#: and "blocked" has to name the port rule as well as the address rule because
#: the vet refuses anything off 80/443.
_REJECTION_MESSAGES = {
    "invalid_url": "calendar URL is malformed",
    "blocked_url": (
        "calendar URL was refused: it must resolve to a public address and use "
        "the standard https port"
    ),
}
#: Used if that module ever grows a third code — a rejection must stay a
#: rejection, with a message, rather than becoming a KeyError and a 500.
_UNKNOWN_REJECTION = "calendar URL was refused"

#: The redirects that preserve the request method and body. 301/302/303 tell a
#: client to retry with GET, which is fine for a GET and wrong for a CalDAV
#: ``REPORT`` — see :func:`fetch_vetted`.
_METHOD_PRESERVING_REDIRECTS = frozenset({307, 308})


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

    The scheme rules are this app's; the ADDRESS vet is
    :func:`link_unfurl.vet_unfurl_url`, reused rather than reimplemented — see the
    comment at the delegation for the four refusals a local copy missed.

    Returns a :class:`VettedTarget` carrying the resolved, approved addresses so
    the caller can pin the connection to them — see that class for why the
    address must travel with the URL rather than be re-derived later.

    Name resolution is a blocking syscall, so callers MUST reach this from a
    worker thread — :meth:`IcsCalendarProvider._fetch_url` offloads it for
    exactly that reason.
    """
    # `urlsplit` RAISES on a malformed authority (`https://[`), it does not just
    # return empty parts — so an operator typo in `calendar.source` surfaced as an
    # uncaught ValueError and a 500 rather than the "your calendar URL is wrong"
    # message every other rejection here produces.
    try:
        parts = urlsplit(source)
    except ValueError as exc:
        raise CalendarError("calendar URL is malformed") from exc
    scheme = parts.scheme.lower()
    if scheme in _WEBCAL_SCHEMES:
        parts = parts._replace(scheme="https")
        scheme = "https"
    if scheme not in _ALLOWED_SCHEMES:
        raise CalendarError(
            f"calendar URL must use https:// (got {scheme or 'no'} scheme)"
        )
    # The address vet is `link_unfurl`'s, not a local copy. That module already
    # owns this problem for the unfurl endpoint, and it is not merely equivalent
    # to a hand-rolled check here — a local one written for this file missed four
    # things it covers:
    #
    # * `canonicalize_ip` folds the alternate IPv4 encodings the OS resolver
    #   accepts but `ipaddress` rejects. Without it `0177.0.0.1` is not a literal,
    #   falls through to DNS, and getaddrinfo reads the octal `0177` as decimal —
    #   so the vet approves `177.0.0.1` and the fetch goes somewhere the operator
    #   did not write.
    # * `100.64.0.0/10`, the CGNAT range a tailnet hands out. `is_private` does
    #   not cover it; only `is_global` does. On a machine on a tailnet, that range
    #   is the private network.
    # * `fec0::/10`, deprecated IPv6 site-local, which reports `is_global=True`.
    # * `.local` and `.onion`, which resolve through a side channel or not at all.
    #
    # Its `test_vet_rejects_every_special_purpose_range` pins the refusal set
    # against a table of IANA special-purpose prefixes, so the next gap is found
    # by the suite rather than by a reviewer — which a second implementation here
    # would not inherit.
    #
    # `resolve` is injected for one reason: to KEEP the addresses the vet
    # approved. `VettedUrl` reports a single `ip`, while the pin below serves
    # every vetted address so a multi-homed calendar host keeps its fallbacks.
    # Every address recorded here is one `vet_unfurl_url` checked — it vets the
    # whole answer, not just the address it keeps.
    approved: list[str] = []

    def _resolve_and_record(hostname: str, port: int) -> list[str]:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        addresses = [str(info[4][0]) for info in infos]
        for address in addresses:
            # Refused, not skipped. `_reject_if_internal_ip` returns SILENTLY for
            # anything that is not an IP literal, because for it a non-literal is
            # a hostname still to be resolved. Here the list is already a
            # resolution result, so an unreadable entry is an address that would
            # reach the pin unchecked. getaddrinfo should never produce one; if it
            # does, fail closed rather than trust it.
            try:
                ipaddress.ip_address(address)
            except ValueError:
                raise CalendarError(
                    "calendar URL resolved to an address that could not be parsed"
                ) from None
        approved.extend(addresses)
        return addresses

    try:
        vetted = link_unfurl.vet_unfurl_url(parts.geturl(), resolve=_resolve_and_record)
    except link_unfurl.UnfurlRejected as exc:
        raise CalendarError(_REJECTION_MESSAGES.get(exc.code, _UNKNOWN_REJECTION)) from None
    # An IP literal never reaches the injected resolver, so fall back to the
    # canonical form the vet resolved it to.
    addresses = tuple(dict.fromkeys(approved)) or (vetted.ip,)
    return VettedTarget(
        url=vetted.url,
        # `wire_host`, not `host`: the connector asks its resolver with the
        # IDNA-encoded form, so that is what the pin must be keyed on. Deriving it
        # here rather than re-parsing keeps "the pinned host is exactly what the
        # client asks for" true in one place.
        host=vetted.wire_host,
        port=vetted.port,
        addresses=addresses,
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
    :func:`link_unfurl.vet_unfurl_url` checked that literal, in its canonical
    form, against the private-address rules before the request was made.
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


async def vet_url(source: str) -> VettedTarget:
    """Run the SSRF gate on the executor — it performs a blocking DNS lookup.

    Module-level so every provider reaches the gate the same way. Looks
    ``_normalize_url`` up as a module global at call time, which is what lets a
    test substitute a pass-through recorder for it (the loopback servers the
    redirect tests need would otherwise be refused by the real gate).
    """
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _normalize_url, source
    )


def _same_origin(left: VettedTarget, right: VettedTarget) -> bool:
    """True when two vetted targets share a scheme/host/port origin.

    Only https reaches a vetted target, so scheme is implied and host+port decide
    it. Used to decide whether an ``Authorization`` header may survive a redirect.
    """
    return left.host.rstrip(".").lower() == right.host.rstrip(".").lower() and (
        left.port == right.port
    )


async def fetch_vetted(
    source: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    auth_headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_secs: int = k.ICS_FETCH_TIMEOUT_SECS,
    max_bytes: int = k.MAX_ICS_BYTES,
    max_redirects: int = k.ICS_MAX_REDIRECTS,
    ok_statuses: frozenset[int] = frozenset({200}),
) -> bytes:
    """Fetch *source* through the SSRF gate, with DNS pinned per hop.

    This is the one outbound HTTP path every calendar provider uses. It was
    originally :meth:`IcsCalendarProvider._fetch_url`; it is module-level now
    because CalDAV, Google and Microsoft 365 all need the same posture, and three
    copies of a security control is three chances for one of them to drift.

    Every property the ``.ics`` fetch had is preserved and is load-bearing:

    * ``allow_redirects=False`` plus a MANUAL hop loop, so the gate runs on every
      hop. Letting aiohttp follow redirects validates only the FIRST url — a
      public host could then 302 to ``http://169.254.169.254/`` and the gateway
      would issue that request itself. Re-checking ``resp.url`` afterwards is too
      late; the request already happened.
    * ``use_dns_cache=False`` so the pin stays authoritative rather than racing
      aiohttp's own TTL cache.
    * No ``ssl=`` / ``verify_ssl=`` override, so the connector keeps aiohttp's
      default VERIFIED context. The request URL keeps its hostname, so ``Host``,
      TLS SNI and certificate validation are unchanged.
    * The response is size-capped WHILE STREAMING, so a hostile endpoint cannot
      exhaust memory before the cap is noticed.

    Two rules are new here, because the ``.ics`` path never sent a credential and
    only ever issued a GET:

    ``auth_headers`` are dropped across a cross-origin redirect. Anything in
    ``headers`` rides every hop; anything in ``auth_headers`` — a CalDAV
    ``Authorization: Basic``, a Google ``Bearer`` — is sent only while the hop
    stays on the origin that was originally addressed. Without this split, a
    calendar host (or anything that can forge its ``Location``) could bounce the
    request to a host it controls and be handed the user's live credential. The
    request still follows the redirect, it just arrives unauthenticated, which
    fails visibly instead of leaking.

    For a NON-GET request, only the method-preserving redirects (307/308) are
    followed. 301/302/303 instruct a client to retry with GET, which would
    silently turn a CalDAV ``REPORT`` query into a plain GET of some other
    resource and parse whatever came back — a wrong answer dressed as a right
    one. Those are refused with a message instead of guessed at.
    """
    verb = (method or "GET").upper()
    target = await vet_url(source)
    origin = target
    # One resolver for the whole session, pinned hop by hop. A redirect can only
    # reach an address some hop's validation already approved.
    resolver = _PinnedResolver()
    timeout = aiohttp.ClientTimeout(total=timeout_secs)
    chunks: list[bytes] = []
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            for _hop in range(max_redirects + 1):
                resolver.pin(target)
                sent = dict(headers or {})
                # The credential rides only while we are still talking to the
                # host the caller actually addressed.
                if auth_headers and _same_origin(origin, target):
                    sent.update(auth_headers)
                async with session.request(
                    verb, target.url, headers=sent or None, data=body, allow_redirects=False
                ) as resp:
                    if resp.status in _REDIRECT_STATUSES:
                        if verb != "GET" and resp.status not in _METHOD_PRESERVING_REDIRECTS:
                            raise CalendarError(
                                f"calendar server redirected a {verb} with HTTP "
                                f"{resp.status}, which would change the request method"
                            )
                        location = resp.headers.get("Location", "")
                        if not location:
                            raise CalendarError("calendar URL redirected with no target")
                        # Resolve a relative target against the current url, then
                        # run the SAME validator on the result — and pin THAT
                        # hop's answer on the next pass, so no hop is ever
                        # vetted-then-re-resolved. `URL(...)` RAISES on a
                        # malformed value, and this one comes from the REMOTE
                        # server's `Location` header.
                        try:
                            nxt = str(resp.url.join(URL(location)))
                        except ValueError as exc:
                            raise CalendarError("calendar redirect URL is malformed") from exc
                        target = await vet_url(nxt)
                        continue
                    if resp.status not in ok_statuses:
                        raise CalendarError(f"calendar URL returned HTTP {resp.status}")
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise CalendarError("calendar document is too large")
                        chunks.append(chunk)
                    break
            else:
                raise CalendarError("calendar URL redirected too many times")
    except aiohttp.ClientError as exc:
        raise CalendarError(f"calendar fetch failed: {type(exc).__name__}") from exc
    except TimeoutError as exc:
        raise CalendarError("calendar fetch timed out") from exc
    return b"".join(chunks)


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
        return await vet_url(source)

    async def _fetch_url(self, source: str) -> str:
        """Fetch the document through the shared, gated fetch.

        The hop loop this method used to carry inline now lives in
        :func:`fetch_vetted`, so CalDAV / Google / Microsoft 365 share one copy of
        the SSRF posture instead of each keeping their own. Behaviour here is
        unchanged: a plain GET, no credential, ``MAX_ICS_BYTES``, and only HTTP
        200 accepted.
        """
        raw = await fetch_vetted(
            source,
            timeout_secs=k.ICS_FETCH_TIMEOUT_SECS,
            max_bytes=k.MAX_ICS_BYTES,
            max_redirects=k.ICS_MAX_REDIRECTS,
        )
        return raw.decode("utf-8", errors="replace")

    async def _read_file(self, source: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _read_local_ics, source
        )


class _DoctypeRefused(Exception):
    """Raised by :class:`_NoDoctypeBuilder` when a document declares a DOCTYPE."""


class _NoDoctypeBuilder(ElementTree.TreeBuilder):
    """A tree builder that refuses any document type declaration.

    This is the whole defence against an XML entity-expansion bomb, and it is
    needed because the response is XML from a REMOTE server. Measured, not
    assumed, on this interpreter:

    * stdlib ``ElementTree`` DOES expand internal entities — a nine-line
      "billion laughs" document expands to thousands of characters, and each
      further nesting level multiplies that, so the size cap on the download
      bounds the input but not the expansion.
    * It does NOT resolve EXTERNAL entities: ``<!ENTITY x SYSTEM "file:///…">``
      fails with ``undefined entity``. So file disclosure is already closed and
      expansion is the part left to close.

    An entity bomb needs an internal DTD, so refusing the DOCTYPE outright kills
    it without having to analyse entity graphs. A legitimate CalDAV Multi-Status
    has no DOCTYPE, so nothing real is lost. The hook has to live on the
    TreeBuilder rather than on ``XMLParser``: ``XMLParser.doctype()`` is ignored
    on modern Pythons (it warns and does nothing), and raising from here aborts
    the parse at the declaration — before any expansion runs.
    """

    def doctype(self, name: str, pubid: str, system: str) -> None:
        raise _DoctypeRefused(name)


def _parse_xml_safely(payload: bytes) -> Any:
    """Parse *payload* as XML, refusing a DOCTYPE. Returns the root element."""
    parser = ElementTree.XMLParser(target=_NoDoctypeBuilder())
    try:
        parser.feed(payload)
        return parser.close()
    except _DoctypeRefused as exc:
        raise CalendarError(
            "calendar server returned XML with a document type declaration, "
            "which is not accepted"
        ) from exc
    except ElementTree.ParseError as exc:
        raise CalendarError("calendar server returned XML that could not be parsed") from exc


#: XML namespaces used by a CalDAV ``REPORT`` response (RFC 4791 / RFC 4918).
_NS_DAV = "DAV:"
_NS_CALDAV = "urn:ietf:params:xml:ns:caldav"

#: The ``REPORT`` body: a ``calendar-query`` narrowed to ``VEVENT`` inside a
#: time range. Asking the SERVER to filter is what keeps the response small — the
#: alternative is downloading a whole collection and discarding most of it — and
#: :func:`parse_ics` still applies the same window afterwards, so a server that
#: ignores the filter cannot widen the result.
_CALDAV_QUERY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
    '<C:filter><C:comp-filter name="VCALENDAR">'
    '<C:comp-filter name="VEVENT">'
    '<C:time-range start="{start}" end="{end}"/>'
    "</C:comp-filter></C:comp-filter></C:filter>"
    "</C:calendar-query>"
)


class CalDavCalendarProvider(CalendarProvider):
    """Reads meetings from a CalDAV collection (Nextcloud, Fastmail, iCloud, …).

    ``calendar.source`` is the **collection** URL — the one that addresses a
    single calendar, e.g.
    ``https://host/remote.php/dav/calendars/alice/personal/``. Principal and
    calendar-home discovery is deliberately not implemented: it is two more
    round trips and a second XML shape, and every server that supports CalDAV
    also shows the user this URL. A URL that is not a collection produces the
    server's own error rather than a guess.

    The username and password come from the credential store, never from
    ``config.json`` — see :mod:`..credentials`. They are read inside
    :meth:`fetch` rather than in ``__init__`` because
    :func:`available_calendar_providers` constructs every provider just to read
    its label, and a constructor that touched the disk would put a file read
    behind rendering the settings picker.

    Two consequences worth stating plainly:

    * **A LAN or localhost server cannot be used.** The shared gate refuses
      private and loopback addresses because the GATEWAY makes this request, so a
      self-hosted CalDAV on an internal address is refused by design. Only a
      publicly resolvable https host works.
    * **A recurring event collapses to one entry.** The server returns each
      override as its own ``VEVENT`` sharing the series UID, and
      :func:`parse_ics` does not read ``RECURRENCE-ID``, so they map to one
      ``event_id``. Duplicates are folded to the earliest start rather than
      allowed to share a meeting directory. That matches this module's existing
      stance on recurrence — showing the series' first instance instead of
      silently wrong occurrence times.
    """

    def __init__(self, source: str = "") -> None:
        self._source = (source or "").strip()

    @property
    def provider_id(self) -> str:
        return k.CALENDAR_PROVIDER_CALDAV

    @property
    def display_name(self) -> str:
        return "CalDAV (Nextcloud, Fastmail, iCloud)"

    @property
    def requires_source(self) -> bool:
        return True

    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        if not self._source:
            raise CalendarError(
                "No CalDAV collection URL is configured. Put your calendar's "
                "CalDAV URL in Settings -> Calendar."
            )
        stored = await credentials.read_for(k.CALENDAR_PROVIDER_CALDAV)
        username = stored.get("username", "")
        password = stored.get("password", "")
        if not username or not password:
            raise CalendarError(
                "CalDAV needs a username and password. Add them in "
                "Settings -> Calendar."
            )

        window = max(1, min(int(days), 365))
        now = datetime.now(timezone.utc)
        body = _CALDAV_QUERY.format(
            # The look-back matches `parse_ics`'s own floor, so a meeting that
            # started just before the sync is still returned.
            start=(now - timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ"),
            end=(now + timedelta(days=window)).strftime("%Y%m%dT%H%M%SZ"),
        )
        payload = await fetch_vetted(
            self._source,
            method="REPORT",
            headers={
                # Depth 1 = the collection and its direct children, which is
                # where calendar objects live. Depth infinity is both slower and
                # more than this needs.
                "Depth": "1",
                "Content-Type": 'application/xml; charset="utf-8"',
            },
            # In `auth_headers`, not `headers`, so the credential is dropped if
            # the server redirects off its own origin.
            auth_headers={"Authorization": _basic_auth(username, password)},
            body=body.encode("utf-8"),
            timeout_secs=k.CALDAV_TIMEOUT_SECS,
            max_bytes=k.MAX_CALDAV_BYTES,
            # 207 Multi-Status is the success answer for a REPORT; 200 is
            # accepted because some servers answer a single-resource query that
            # way.
            ok_statuses=frozenset({207, 200}),
        )
        return _events_from_multistatus(payload, days=window)


def _basic_auth(username: str, password: str) -> str:
    """An HTTP Basic ``Authorization`` value.

    RFC 7617 leaves the charset implementation-defined and every CalDAV server in
    practice reads UTF-8, which matters because app-specific passwords are ASCII
    but usernames are often an email address that need not be.
    """
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _events_from_multistatus(payload: bytes, *, days: int) -> list[CalendarEvent]:
    """Pull every ``calendar-data`` out of a Multi-Status and parse each one.

    Each blob is parsed on its OWN, not concatenated, so one malformed calendar
    object costs only its own event instead of the whole sync — the same bargain
    :func:`parse_ics` already makes for one bad line.

    Duplicate ``event_id``s are folded to the earliest start. A server returns a
    recurring series' overrides as separate ``VEVENT``s sharing one UID, and
    letting both through would hand two list rows the same meeting directory,
    which is precisely the collision ``_event_id_for`` exists to prevent.
    """
    root = _parse_xml_safely(payload)
    by_id: dict[str, CalendarEvent] = {}
    for node in root.iter(f"{{{_NS_CALDAV}}}calendar-data"):
        text = (node.text or "").strip()
        if not text:
            continue
        for event in parse_ics(text, days=days):
            existing = by_id.get(event.event_id)
            if existing is None or event.start < existing.start:
                by_id[event.event_id] = event
        if len(by_id) >= k.MAX_CALENDAR_EVENTS:
            break
    events = sorted(by_id.values(), key=lambda e: e.start)
    return events[: k.MAX_CALENDAR_EVENTS]


def _parse_rfc3339(value: str) -> datetime | None:
    """Parse a Google/Graph timestamp into an aware UTC datetime.

    Both APIs answer RFC 3339. ``fromisoformat`` handles the offset forms on 3.11+
    including the trailing ``Z``; a value it cannot read is returned as ``None`` so
    the event is skipped rather than taking the whole sync down — the same bargain
    :func:`parse_ics` makes for a malformed line.

    A value with no offset is read as UTC. Graph returns exactly that shape and
    names the zone in a sibling field, which the caller applies before getting
    here; anything still naive at this point has no zone to apply.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _google_when(node: object) -> datetime | None:
    """Read a Google ``start``/``end`` object.

    Google sends either ``{"dateTime": "...", "timeZone": "..."}`` for a timed
    event or ``{"date": "YYYY-MM-DD"}`` for a whole-day one. ``dateTime`` already
    carries its own offset, so ``timeZone`` is redundant for converting it and is
    not consulted.
    """
    if not isinstance(node, dict):
        return None
    stamp = node.get("dateTime")
    if isinstance(stamp, str) and stamp:
        return _parse_rfc3339(stamp)
    whole_day = node.get("date")
    if isinstance(whole_day, str) and whole_day:
        try:
            return datetime.strptime(whole_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _person_name(node: object) -> str:
    """Best display value for a Google organizer/attendee object."""
    if not isinstance(node, dict):
        return ""
    return str(node.get("displayName") or node.get("email") or "")


class GoogleCalendarProvider(CalendarProvider):
    """Reads meetings from Google Calendar's v3 ``events.list``.

    ``requires_source`` is False: this reads the signed-in user's ``primary``
    calendar. Making it configurable would mean asking for a calendar id, which
    is not something a user has to hand, and the app's purpose is "the meetings
    I am in".

    Recurrence is handled properly here, unlike the ``.ics`` and CalDAV paths:
    ``singleEvents=true`` makes Google expand a series into instances server-side,
    each with its own id. So there is no RRULE engine to get wrong and no
    UID collision to fold — the thing this module refuses to guess at, the
    provider does authoritatively.

    Credentials (client id/secret and the OAuth tokens) come from the credential
    store and are read inside :meth:`fetch`, never in ``__init__`` — see
    :class:`CalDavCalendarProvider` for why that matters to the settings picker.
    """

    @property
    def provider_id(self) -> str:
        return k.CALENDAR_PROVIDER_GOOGLE

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    @property
    def requires_source(self) -> bool:
        return False

    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        # Imported inside the method to break an import cycle: `oauth` needs
        # `fetch_vetted` and `CalendarError` from this module, so it cannot be
        # imported at module scope here. By the time this runs both modules are
        # fully loaded. `context.py` breaks its resource-probe cycle the same way.
        from kiro_crew.apps.builtins.meetings.backend import oauth

        token = await oauth.access_token(google_oauth_client())
        window = max(1, min(int(days), 365))
        now = datetime.now(timezone.utc)
        params = {
            # The one-day look-back matches `parse_ics`'s floor, so a meeting that
            # started just before the sync still appears.
            "timeMin": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": (now + timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(k.GOOGLE_PAGE_SIZE),
        }

        events: list[CalendarEvent] = []
        page_token = ""
        for _page in range(k.GOOGLE_MAX_PAGES):
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            payload = await fetch_vetted(
                f"{k.GOOGLE_EVENTS_URL}?{urlencode(query)}",
                # In `auth_headers`, so a redirect off googleapis.com cannot carry
                # the user's bearer token with it.
                auth_headers={"Authorization": f"Bearer {token}"},
                headers={"Accept": "application/json"},
                timeout_secs=k.ICS_FETCH_TIMEOUT_SECS,
                max_bytes=k.MAX_GOOGLE_RESPONSE_BYTES,
            )
            body = _json_object(payload)
            events.extend(_google_events(body.get("items")))
            if len(events) >= k.MAX_CALENDAR_EVENTS:
                break
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                break

        events.sort(key=lambda e: e.start)
        return events[: k.MAX_CALENDAR_EVENTS]


def google_oauth_client() -> Any:
    """The :class:`oauth.OAuthClient` describing Google's endpoints."""
    from kiro_crew.apps.builtins.meetings.backend import oauth

    return oauth.OAuthClient(
        provider_id=k.CALENDAR_PROVIDER_GOOGLE,
        authorize_url=k.GOOGLE_AUTHORIZE_URL,
        token_url=k.GOOGLE_TOKEN_URL,
        scopes=(k.GOOGLE_CALENDAR_SCOPE,),
        extra_authorize_params=dict(k.GOOGLE_AUTHORIZE_EXTRA),
    )


def _json_object(payload: bytes) -> dict[str, Any]:
    """Decode a JSON object, or raise a :class:`CalendarError`.

    A bare ``ValueError`` here would escape the sync route as a 500; the route
    turns ``CalendarError`` into a 502 with the message.
    """
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CalendarError("the calendar provider's response was not JSON") from exc
    if not isinstance(parsed, dict):
        raise CalendarError("the calendar provider's response was not an object")
    return parsed


def _google_events(items: object) -> list[CalendarEvent]:
    """Turn Google ``items`` into events, skipping the ones that make no sense.

    A cancelled instance of a recurring series still appears in the list with
    ``status: "cancelled"`` and usually no ``start``; showing it as a meeting
    would put a phantom row in the user's day.
    """
    out: list[CalendarEvent] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "cancelled":
            continue
        start = _google_when(item.get("start"))
        if start is None:
            continue
        attendees = [
            _person_name(a) for a in (item.get("attendees") or [])[:_MAX_ATTENDEES]
        ]
        out.append(
            build_event(
                uid=str(item.get("id") or ""),
                title=str(item.get("summary") or ""),
                start=start,
                end=_google_when(item.get("end")),
                location=item.get("location"),
                organizer=_person_name(item.get("organizer")),
                attendees=attendees,
                description=item.get("description"),
                uid_prefix=k.CALENDAR_PROVIDER_GOOGLE,
            )
        )
    return out


def _graph_when(node: object) -> datetime | None:
    """Read a Graph ``start``/``end`` object.

    Graph sends ``{"dateTime": "2026-08-05T09:00:00.0000000", "timeZone": "UTC"}``
    — the stamp carries NO offset and the zone is a sibling field, which is why
    the request sends ``Prefer: outlook.timezone="UTC"``.

    ``timeZone`` is still honoured when it resolves, so a response that ignored
    the header is not silently misread. Graph often answers with a WINDOWS zone
    name (``Tokyo Standard Time``), which is not an IANA key and will not resolve;
    that falls back to UTC, matching :func:`_tzid_of`'s existing stance that a
    visible meeting at a possibly-wrong hour beats a meeting that is not there.
    """
    if not isinstance(node, dict):
        return None
    stamp = str(node.get("dateTime") or "")
    if not stamp:
        return None
    parsed = _parse_rfc3339(stamp)
    if parsed is None:
        return None
    zone_name = str(node.get("timeZone") or "").strip()
    if zone_name and zone_name.upper() != "UTC" and stamp[-1:] != "Z":
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.debug("meetings: unknown Graph timeZone %r; reading it as UTC", zone_name)
        else:
            # `_parse_rfc3339` already stamped the naive value as UTC, so the zone
            # has to be applied to the wall-clock reading rather than converted
            # from it.
            return parsed.replace(tzinfo=zone).astimezone(timezone.utc)
    return parsed


def _graph_person(node: object) -> str:
    """Best display value for a Graph organizer/attendee.

    The name is nested one level deeper than Google's: Graph wraps it in
    ``emailAddress``.
    """
    if not isinstance(node, dict):
        return ""
    email = node.get("emailAddress")
    if isinstance(email, dict):
        return str(email.get("name") or email.get("address") or "")
    return str(node.get("name") or "")


class MicrosoftCalendarProvider(CalendarProvider):
    """Reads meetings from Microsoft 365 through Graph's ``/me/calendarView``.

    ``calendarView`` rather than ``/me/events`` because it expands a recurring
    series into occurrences server-side — the same reason
    :class:`GoogleCalendarProvider` asks for ``singleEvents``, and the same
    payoff: no RRULE engine and no UID collision to fold.

    Like the Google provider this needs no ``calendar.source``; it reads the
    signed-in user's own calendar.
    """

    @property
    def provider_id(self) -> str:
        return k.CALENDAR_PROVIDER_MICROSOFT

    @property
    def display_name(self) -> str:
        return "Microsoft 365 / Outlook"

    @property
    def requires_source(self) -> bool:
        return False

    async def fetch(self, *, days: int = k.CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
        # Function-local for the same import-cycle reason as the Google provider.
        from kiro_crew.apps.builtins.meetings.backend import oauth

        token = await oauth.access_token(microsoft_oauth_client())
        window = max(1, min(int(days), 365))
        now = datetime.now(timezone.utc)
        query = {
            "startDateTime": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": (now + timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$orderby": "start/dateTime",
            "$top": str(k.MICROSOFT_PAGE_SIZE),
        }
        url = f"{k.MICROSOFT_EVENTS_URL}?{urlencode(query)}"

        events: list[CalendarEvent] = []
        for _page in range(k.MICROSOFT_MAX_PAGES):
            payload = await fetch_vetted(
                url,
                auth_headers={"Authorization": f"Bearer {token}"},
                headers={
                    "Accept": "application/json",
                    "Prefer": k.MICROSOFT_TIMEZONE_HEADER,
                },
                timeout_secs=k.ICS_FETCH_TIMEOUT_SECS,
                max_bytes=k.MAX_MICROSOFT_RESPONSE_BYTES,
            )
            body = _json_object(payload)
            events.extend(_graph_events(body.get("value")))
            if len(events) >= k.MAX_CALENDAR_EVENTS:
                break
            # Graph paginates with a FULL URL rather than a token. It comes from
            # the response body, so it is re-vetted by `fetch_vetted`'s gate on
            # the next pass exactly like a redirect target would be — an
            # `@odata.nextLink` pointing at a private address is refused there.
            url = str(body.get("@odata.nextLink") or "")
            if not url:
                break

        events.sort(key=lambda e: e.start)
        return events[: k.MAX_CALENDAR_EVENTS]


def microsoft_oauth_client() -> Any:
    """The :class:`oauth.OAuthClient` describing Microsoft's endpoints."""
    from kiro_crew.apps.builtins.meetings.backend import oauth

    return oauth.OAuthClient(
        provider_id=k.CALENDAR_PROVIDER_MICROSOFT,
        authorize_url=k.MICROSOFT_AUTHORIZE_URL,
        token_url=k.MICROSOFT_TOKEN_URL,
        scopes=tuple(k.MICROSOFT_CALENDAR_SCOPES),
    )


def _graph_events(items: object) -> list[CalendarEvent]:
    """Turn a Graph ``value`` array into events.

    A cancelled occurrence is still returned by ``calendarView`` with
    ``isCancelled: true``; skipping it keeps a phantom meeting out of the day.
    """
    out: list[CalendarEvent] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("isCancelled") is True:
            continue
        start = _graph_when(item.get("start"))
        if start is None:
            continue
        location = item.get("location")
        location_name = (
            location.get("displayName") if isinstance(location, dict) else location
        )
        attendees = [
            _graph_person(a) for a in (item.get("attendees") or [])[:_MAX_ATTENDEES]
        ]
        out.append(
            build_event(
                uid=str(item.get("id") or ""),
                title=str(item.get("subject") or ""),
                start=start,
                end=_graph_when(item.get("end")),
                location=location_name,
                organizer=_graph_person(item.get("organizer")),
                attendees=attendees,
                # `bodyPreview` rather than `body`: the full body is HTML and can
                # be enormous, and the display only ever shows a summary line.
                description=item.get("bodyPreview"),
                uid_prefix=k.CALENDAR_PROVIDER_MICROSOFT,
            )
        )
    return out


def _read_local_ics(source: str) -> str:
    """Read a local ``.ics`` file, size-capped. Runs on an executor thread.

    Routed through :func:`hooks.safe_read_file_bytes`, the gateway's central
    file-read gate, rather than reading the path directly. The path is
    operator-supplied config (never LLM- or request-supplied), so this is
    defense in depth — but the hand-rolled version it replaces had a real gap:
    it called ``is_sensitive_path`` on the path AS WRITTEN and then read
    ``path.resolve()``, so a symlink whose target is ``~/.aws/credentials``
    passed the check and was followed anyway. ``safe_read_file_bytes`` checks the
    CANONICAL target (``validate_file_path`` → ``realpath``) and opens it with
    ``O_NOFOLLOW`` against a TOCTOU swap of the final component.

    The size cap stays app-local: the shared gate's ceiling is a general
    file-read limit, while ``MAX_ICS_BYTES`` is what this parser will accept.
    Checked on the returned bytes, so the smaller of the two always wins.
    """
    data = hooks.safe_read_file_bytes(source)
    if data is None:
        # One message for "blocked" and "unreadable" alike: the gate does not
        # distinguish them, and probing which one it was is not something a
        # calendar path should be able to do.
        raise CalendarError(f"cannot read calendar file: {source}")
    if len(data) > k.MAX_ICS_BYTES:
        raise CalendarError("calendar file is too large")
    return data.decode("utf-8", errors="replace")


# ── registry ────────────────────────────────────────────────────────────────

CalendarProviderFactory = Callable[[str], CalendarProvider]

_factories: dict[str, CalendarProviderFactory] = {
    k.CALENDAR_PROVIDER_NONE: lambda _source: NoCalendarProvider(),
    k.CALENDAR_PROVIDER_ICS: IcsCalendarProvider,
    k.CALENDAR_PROVIDER_CALDAV: CalDavCalendarProvider,
    k.CALENDAR_PROVIDER_GOOGLE: lambda _source: GoogleCalendarProvider(),
    k.CALENDAR_PROVIDER_MICROSOFT: lambda _source: MicrosoftCalendarProvider(),
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
