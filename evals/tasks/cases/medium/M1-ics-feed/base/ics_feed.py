"""Parse an iCalendar (.ics) feed into the upcoming meetings a dashboard lists.

Stdlib only. A published calendar is untrusted text from a remote server, so
malformed lines, unknown properties and odd values are skipped rather than
raising: one bad event must not cost the whole sync.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("ics_feed")

#: Most events one sync returns.
MAX_CALENDAR_EVENTS = 500
#: Longest meeting id (it becomes a directory name).
MAX_MEETING_ID_LEN = 128
#: Days ahead a sync looks by default.
CALENDAR_SYNC_DAYS = 7


def redact(text: str) -> str:
    """Mask secrets in display text. The app's real masker is out of scope here."""
    return text


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
    #: A whole-day event. ``start``/``end`` keep the date's midnight UTC as a
    #: DATE ANCHOR, not an instant: the renderer must display the calendar date
    #: without timezone conversion, or a browser west of UTC shows the event on
    #: the previous day. Every provider parsing a date-only value (an iCalendar
    #: ``VALUE=DATE``, a date-without-time from any other calendar API) sets
    #: this instead of dropping the event or leaving the flag to the renderer
    #: to guess from a midnight timestamp — a real 00:00 meeting is not all-day.
    all_day: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


#: Windows (CLDR ``windowsZones``) display names -> the IANA key ``zoneinfo``
#: understands.
#:
#: Microsoft Exchange and Outlook -- among the most common ``.ics`` producers --
#: stamp ``DTSTART;TZID=Romance Standard Time:...`` rather than an IANA key like
#: ``Europe/Paris``. ``ZoneInfo("Romance Standard Time")`` raises, so without this
#: table :func:`_tzid_of` returned ``None`` and :func:`_parse_dt` read the local
#: wall-clock time AS UTC -- a whole-timezone shift (a 16:00 Paris meeting stored
#: as 16:00Z instead of 14:00Z, DST included). Mapping to the CLDR primary-territory
#: IANA zone resolves the instant correctly, because the offset then comes from the
#: IANA rules for the event's actual date. Matched case-insensitively (see
#: :func:`_tzid_of`); an unmapped name still degrades to UTC-visible rather than
#: dropping the event, per the module's never-drop convention. This is the full
#: CLDR ``windowsZones`` 001-territory (primary) mapping, with deprecated IANA
#: aliases modernised to their current canonical keys (``Asia/Kolkata``, not
#: ``Asia/Calcutta``).
_WINDOWS_TO_IANA: dict[str, str] = {
    "dateline standard time": "Etc/GMT+12",
    "utc-11": "Etc/GMT+11",
    "aleutian standard time": "America/Adak",
    "hawaiian standard time": "Pacific/Honolulu",
    "marquesas standard time": "Pacific/Marquesas",
    "alaskan standard time": "America/Anchorage",
    "utc-09": "Etc/GMT+9",
    "pacific standard time (mexico)": "America/Tijuana",
    "utc-08": "Etc/GMT+8",
    "pacific standard time": "America/Los_Angeles",
    "us mountain standard time": "America/Phoenix",
    "mountain standard time (mexico)": "America/Mazatlan",
    "mountain standard time": "America/Denver",
    "yukon standard time": "America/Whitehorse",
    "central america standard time": "America/Guatemala",
    "central standard time": "America/Chicago",
    "easter island standard time": "Pacific/Easter",
    "central standard time (mexico)": "America/Mexico_City",
    "canada central standard time": "America/Regina",
    "sa pacific standard time": "America/Bogota",
    "eastern standard time (mexico)": "America/Cancun",
    "eastern standard time": "America/New_York",
    "haiti standard time": "America/Port-au-Prince",
    "cuba standard time": "America/Havana",
    "us eastern standard time": "America/Indianapolis",
    "turks and caicos standard time": "America/Grand_Turk",
    "paraguay standard time": "America/Asuncion",
    "atlantic standard time": "America/Halifax",
    "venezuela standard time": "America/Caracas",
    "central brazilian standard time": "America/Cuiaba",
    "sa western standard time": "America/La_Paz",
    "pacific sa standard time": "America/Santiago",
    "newfoundland standard time": "America/St_Johns",
    "tocantins standard time": "America/Araguaina",
    "e. south america standard time": "America/Sao_Paulo",
    "sa eastern standard time": "America/Cayenne",
    "argentina standard time": "America/Argentina/Buenos_Aires",
    "greenland standard time": "America/Nuuk",
    "montevideo standard time": "America/Montevideo",
    "magallanes standard time": "America/Punta_Arenas",
    "saint pierre standard time": "America/Miquelon",
    "bahia standard time": "America/Bahia",
    "utc-02": "Etc/GMT+2",
    "azores standard time": "Atlantic/Azores",
    "cape verde standard time": "Atlantic/Cape_Verde",
    "utc": "UTC",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "sao tome standard time": "Africa/Sao_Tome",
    "morocco standard time": "Africa/Casablanca",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris",
    "central european standard time": "Europe/Warsaw",
    "w. central africa standard time": "Africa/Lagos",
    "jordan standard time": "Asia/Amman",
    "gtb standard time": "Europe/Bucharest",
    "middle east standard time": "Asia/Beirut",
    "egypt standard time": "Africa/Cairo",
    "e. europe standard time": "Europe/Chisinau",
    "syria standard time": "Asia/Damascus",
    "west bank standard time": "Asia/Hebron",
    "south africa standard time": "Africa/Johannesburg",
    "fle standard time": "Europe/Kyiv",
    "israel standard time": "Asia/Jerusalem",
    "south sudan standard time": "Africa/Juba",
    "kaliningrad standard time": "Europe/Kaliningrad",
    "sudan standard time": "Africa/Khartoum",
    "libya standard time": "Africa/Tripoli",
    "namibia standard time": "Africa/Windhoek",
    "arabic standard time": "Asia/Baghdad",
    "turkey standard time": "Europe/Istanbul",
    "arab standard time": "Asia/Riyadh",
    "belarus standard time": "Europe/Minsk",
    "russian standard time": "Europe/Moscow",
    "e. africa standard time": "Africa/Nairobi",
    "iran standard time": "Asia/Tehran",
    "arabian standard time": "Asia/Dubai",
    "astrakhan standard time": "Europe/Astrakhan",
    "azerbaijan standard time": "Asia/Baku",
    "russia time zone 3": "Europe/Samara",
    "mauritius standard time": "Indian/Mauritius",
    "saratov standard time": "Europe/Saratov",
    "georgian standard time": "Asia/Tbilisi",
    "volgograd standard time": "Europe/Volgograd",
    "caucasus standard time": "Asia/Yerevan",
    "afghanistan standard time": "Asia/Kabul",
    "west asia standard time": "Asia/Tashkent",
    "ekaterinburg standard time": "Asia/Yekaterinburg",
    "pakistan standard time": "Asia/Karachi",
    "qyzylorda standard time": "Asia/Qyzylorda",
    "india standard time": "Asia/Kolkata",
    "sri lanka standard time": "Asia/Colombo",
    "nepal standard time": "Asia/Kathmandu",
    "central asia standard time": "Asia/Bishkek",
    "bangladesh standard time": "Asia/Dhaka",
    "omsk standard time": "Asia/Omsk",
    "myanmar standard time": "Asia/Yangon",
    "se asia standard time": "Asia/Bangkok",
    "altai standard time": "Asia/Barnaul",
    "w. mongolia standard time": "Asia/Hovd",
    "north asia standard time": "Asia/Krasnoyarsk",
    "n. central asia standard time": "Asia/Novosibirsk",
    "tomsk standard time": "Asia/Tomsk",
    "china standard time": "Asia/Shanghai",
    "north asia east standard time": "Asia/Irkutsk",
    "singapore standard time": "Asia/Singapore",
    "w. australia standard time": "Australia/Perth",
    "taipei standard time": "Asia/Taipei",
    "ulaanbaatar standard time": "Asia/Ulaanbaatar",
    "aus central w. standard time": "Australia/Eucla",
    "transbaikal standard time": "Asia/Chita",
    "tokyo standard time": "Asia/Tokyo",
    "north korea standard time": "Asia/Pyongyang",
    "korea standard time": "Asia/Seoul",
    "yakutsk standard time": "Asia/Yakutsk",
    "cen. australia standard time": "Australia/Adelaide",
    "aus central standard time": "Australia/Darwin",
    "e. australia standard time": "Australia/Brisbane",
    "aus eastern standard time": "Australia/Sydney",
    "west pacific standard time": "Pacific/Port_Moresby",
    "tasmania standard time": "Australia/Hobart",
    "vladivostok standard time": "Asia/Vladivostok",
    "lord howe standard time": "Australia/Lord_Howe",
    "bougainville standard time": "Pacific/Bougainville",
    "russia time zone 10": "Asia/Srednekolymsk",
    "magadan standard time": "Asia/Magadan",
    "norfolk standard time": "Pacific/Norfolk",
    "sakhalin standard time": "Asia/Sakhalin",
    "central pacific standard time": "Pacific/Guadalcanal",
    "russia time zone 11": "Asia/Kamchatka",
    "new zealand standard time": "Pacific/Auckland",
    "utc+12": "Etc/GMT-12",
    "fiji standard time": "Pacific/Fiji",
    "chatham islands standard time": "Pacific/Chatham",
    "utc+13": "Etc/GMT-13",
    "tonga standard time": "Pacific/Tongatapu",
    "samoa standard time": "Pacific/Apia",
    "line islands standard time": "Pacific/Kiritimati",
}


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
    # An IANA key resolves directly. A Windows/CLDR display name -- what Exchange
    # and Outlook emit (`Romance Standard Time`) -- is not an IANA key, so map it
    # before giving up; otherwise ``ZoneInfo`` raises and the caller silently reads
    # the wall-clock time as UTC, a whole-timezone error. A custom VTIMEZONE id that
    # is neither still degrades to UTC-visible.
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    mapped = _WINDOWS_TO_IANA.get(name.lower())
    if mapped is not None:
        try:
            return ZoneInfo(mapped)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
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
    stem = safe[: max(1, MAX_MEETING_ID_LEN - _UID_DIGEST_LEN - 1)]
    return f"{stem}-{digest}"


def _is_date_only(value: str) -> bool:
    """True when *value* is an RFC 5545 DATE body — a whole-day value, no time part.

    The body's SHAPE decides: a DATE is exactly eight digits (``YYYYMMDD``) and
    every timed form is longer, so the ``VALUE`` parameter is deliberately not
    consulted. Exporters in the wild emit a date body with the parameter
    missing, vendor-prefixed (``X-VALUE=DATE``), or mislabeled
    (``VALUE=DATE-TIME``); a parameter test drops those events (the body then
    fails every DATE-TIME format), while the shape test keeps them visible as
    the dates they are — the module's never-drop convention.

    One predicate shared by the parse (:func:`_parse_dt`) and the ``all_day``
    classification in :func:`parse_ics`, so the two can never disagree about
    which values are whole-day.
    """
    raw = value.strip()
    return len(raw) == 8 and raw.isdigit()


def _parse_dt(value: str, params: str) -> datetime | None:
    """Parse an iCalendar DATE-TIME / DATE value into an aware UTC datetime.

    Handles the three forms RFC 5545 allows: UTC (``…Z``), local time with a
    ``TZID``, and a whole-day DATE. A DATE parses to the date's midnight UTC —
    a **date anchor**, not an instant. The caller records date-onlyness
    separately (:func:`_is_date_only` → ``CalendarEvent.all_day``) so the
    renderer can display the calendar date without zone conversion; reading the
    anchor as an instant shows the previous day everywhere west of UTC.

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
    if _is_date_only(raw):
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


def parse_ics(text: str, *, days: int = CALENDAR_SYNC_DAYS) -> list[CalendarEvent]:
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
                if len(events) >= MAX_CALENDAR_EVENTS:
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
            # Whether the event is whole-day is a property of DTSTART's raw
            # body, decided here where that body is still in hand — a midnight
            # timestamp alone cannot prove it later (a real 00:00 meeting is
            # not all-day).
            current["all_day"] = _is_date_only(value)
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
#: any real meeting (the sync horizon is :data:`CALENDAR_SYNC_DAYS` days) and orders
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
        # RFC 5545 §3.6.1: a DATE-valued DTSTART with neither DTEND nor
        # DURATION spans that whole calendar date; only a timed event defaults
        # to a nominal hour.
        if delta is None:
            delta = timedelta(days=1) if raw.get("all_day") else timedelta(hours=1)
        end = start + delta

    def clean(value: object) -> str:
        return redact(str(value or "").strip())[:_MAX_FIELD_LEN]

    uid = clean(raw.get("uid")) or f"ics-{int(start.timestamp())}"
    return CalendarEvent(
        event_id=_event_id_for(uid),
        title=clean(raw.get("title")) or "Meeting",
        start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        location=clean(raw.get("location")),
        organizer=clean(raw.get("organizer")),
        attendees=[clean(a) for a in raw.get("attendees", []) if str(a).strip()],
        description=clean(raw.get("description"))[:_MAX_FIELD_LEN],
        all_day=bool(raw.get("all_day")),
    )


#: Redirect statuses we follow MANUALLY (301/302/303/307/308), so each hop can be
