"""Zoom identity contract: meeting-id vs UUID, double-encoding, recurrence.

Pure logic. Zoom addresses a meeting/webinar by either a numeric ``meetingId``
or a per-instance UUID, and the two are never interchangeable. A UUID that
begins with ``/`` or contains ``//`` must be DOUBLE URL-encoded before it goes
into a path segment, or Zoom answers error code 3001 -- which then reads as a
false absence rather than a real one. Recurrence adds two more distinctions the
adapter must not blur: the instances list is keyed by the series' numeric id
(never an occurrence UUID), and the presence/absence of ``occurrence_id`` on an
update is what separates a single occurrence from the parent series. Occurrence
time is per-occurrence ``start_time`` plus the series ``timezone`` -- this module
performs NO local-time inference.

See ``docs/system-specs/modules/connector-zoom.md``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from kiro_crew.connections.control_plane import CredentialMode

# Zoom authenticates every operation with one of exactly TWO of the shared
# W01 credential modes -- it has no fine-grained PAT. These are the shared
# :data:`kiro_crew.connections.control_plane.CredentialMode` values, not a
# parallel Zoom-local enum: the credential axis is W01's, and Zoom only says
# WHICH of its values apply.
#: A user's own 3-legged OAuth grant (issues access + refresh token).
ZOOM_USER_OAUTH: CredentialMode = "oauth_user"
#: A 2-legged account-credentials (Server-to-Server) grant (access token only,
#: no refresh token -- a fresh token is requested on expiry).
ZOOM_SERVER_TO_SERVER: CredentialMode = "service_to_service"

#: The credential modes Zoom actually supports, a two-value subset of the shared
#: three-value :data:`CredentialMode` closed set (Zoom issues no fine-grained
#: PAT).
ZOOM_CREDENTIAL_MODES: tuple[CredentialMode, ...] = (
    ZOOM_USER_OAUTH,
    ZOOM_SERVER_TO_SERVER,
)

# An unverified (credential_mode, host) reachability question resolves to this
# until a per-operation scope verification says otherwise -- in EITHER
# direction. It is never inferred from the credential being account-level.
HOST_REACHABILITY_UNKNOWN = "unknown"


class OccurrenceTarget(enum.Enum):
    """What a recurrence-scoped request actually targets.

    An update carrying an ``occurrence_id`` edits that ONE occurrence; the same
    update MISSING ``occurrence_id`` edits the parent series (and therefore the
    whole recurring series). Dropping ``occurrence_id`` silently promotes a
    single-occurrence intent to a whole-series change.
    """

    SINGLE_OCCURRENCE = "single_occurrence"
    PARENT_SERIES = "parent_series"


def needs_double_encoding(raw_uuid: str) -> bool:
    """True when a meeting/webinar UUID must be double URL-encoded.

    Zoom requires double encoding for any UUID that begins with ``/`` or
    contains ``//``. A single encoding pass on such a UUID is insufficient and
    Zoom returns error code 3001 -- see :mod:`kiro_crew.connections.vendors.zoom.errors`,
    where 3001 is deliberately classified as AMBIGUOUS between this encoding
    fault and a genuine absence.
    """
    return raw_uuid.startswith("/") or "//" in raw_uuid


def encode_uuid_path_segment(raw_uuid: str) -> str:
    """Encode a UUID for use as a path segment, double-encoding when required.

    Percent-encodes the raw UUID once; if the raw UUID needed double encoding
    (:func:`needs_double_encoding`), percent-encodes the already-encoded string
    a second time. ``safe=""`` so ``/`` is itself encoded, which is the whole
    point of the rule. A numeric ``meetingId`` is not a UUID and must not be
    routed through this function; use it verbatim.
    """
    once = quote(raw_uuid, safe="")
    if needs_double_encoding(raw_uuid):
        return quote(once, safe="")
    return once


def series_id_for_instances(meeting_id: str) -> str:
    """Return the digits-only series id the instances-list endpoint expects.

    ``GET /past_meetings/{meetingId}/instances`` is keyed by the recurring
    series' numeric id, NOT by an occurrence UUID. This function refuses a UUID
    shape (a value that needs double encoding, or one carrying the non-numeric
    ``base64``/``==`` shape a Zoom instance UUID has) so a caller cannot silently
    pass an occurrence UUID where the series id belongs. A display-formatted
    number (``"982 610 0285"``) is accepted but returned NORMALIZED to bare
    digits, because the value goes into a path segment where the spaces/dashes
    are display-only and must not survive.
    """
    if needs_double_encoding(meeting_id):
        raise ValueError(
            "instances list is keyed by the series numeric id, not an "
            "occurrence UUID (value needs UUID double-encoding)"
        )
    normalized = meeting_id.replace(" ", "").replace("-", "")
    if not (normalized.isdigit() and normalized):
        raise ValueError(
            "instances list is keyed by the series numeric id, not an "
            f"occurrence UUID: {meeting_id!r} is not a numeric meeting id"
        )
    return normalized


def occurrence_target(occurrence_id: Optional[str]) -> OccurrenceTarget:
    """Classify what a recurrence request targets from its ``occurrence_id``.

    A non-empty ``occurrence_id`` targets a single occurrence; ``None`` or an
    empty string targets the parent series. This is the pin behind negative
    fault test 2: an update that MEANT to hit one occurrence but arrived without
    an ``occurrence_id`` is reported here as :data:`OccurrenceTarget.PARENT_SERIES`
    rather than being read as the intended single-occurrence edit.
    """
    if occurrence_id is None or occurrence_id == "":
        return OccurrenceTarget.PARENT_SERIES
    return OccurrenceTarget.SINGLE_OCCURRENCE


@dataclass(frozen=True)
class OccurrenceTime:
    """A per-occurrence start time bound to its series timezone.

    Carried verbatim: ``start_time`` is the occurrence's own value and
    ``timezone`` is the series' IANA zone. No instant is computed here -- reading
    the anchor as an instant in some assumed zone is exactly the inference this
    module refuses.
    """

    start_time: str
    timezone: Optional[str]

    @property
    def timezone_known(self) -> bool:
        """Whether a governing series timezone accompanies this start time."""
        return self.timezone is not None and self.timezone != ""


def resolve_occurrence_time(start_time: str, series_timezone: Optional[str]) -> OccurrenceTime:
    """Bind a per-occurrence ``start_time`` to the series ``timezone``, no more.

    Performs NO local-time inference: it never guesses a wall-clock time from a
    bare timestamp and never substitutes the host's or reader's local zone for a
    missing series timezone. A ``start_time`` without a governing timezone is
    carried with ``timezone=None`` and :attr:`OccurrenceTime.timezone_known`
    False, so a caller must decide explicitly rather than silently assuming a
    zone.
    """
    return OccurrenceTime(start_time=start_time, timezone=series_timezone)
