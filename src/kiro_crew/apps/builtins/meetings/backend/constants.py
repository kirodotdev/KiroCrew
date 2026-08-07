"""Shared constants for the Meetings builtin app.

Every hardcoded string/limit the app's business logic depends on lives here
(AGENTS.md "no hardcoded strings in business logic"). Nothing in this module
touches the network or the filesystem at import time, so it is safe to import
from a Windows gateway even though the app's live-transcription feature is
macOS/Linux only.
"""

from __future__ import annotations

APP_NAME = "meetings"

# HTTP surface. Handlers are registered directly on the gateway's aiohttp
# Application (see backend/routes/__init__.py:register_routes), so this is the
# same ``/api/apps/{name}`` convention issue-radar and code-review-sage use —
# NOT the ``/apps/{name}/api`` reverse-proxy prefix used by child-process apps.
API_BASE = f"/api/apps/{APP_NAME}"

# Safety caps.
MAX_SESSION_DURATION = 4 * 3600  # a single meeting may run at most 4 hours
MAX_CONCURRENT_MEETINGS = 1
MAX_TRANSCRIPT_CHARS = 4000  # per dispatched transcription line
MAX_BATCH_CHARS = 60_000  # per flushed agent batch
MAX_ATTACHMENTS = 25
MAX_DICTIONARY_TERMS = 500
MAX_CALENDAR_EVENTS = 500
MAX_MEETING_ID_LEN = 128
MAX_TITLE_LEN = 300
MAX_ICS_BYTES = 4 * 1024 * 1024  # refuse absurd .ics payloads

#: Ceiling on one credential value from a settings write. Generous next to a
#: password or a client id, and small enough that the credential file stays a file
#: this app can read back in one gulp.
MAX_CREDENTIAL_VALUE_CHARS = 4096

#: Ceiling on a CalDAV ``REPORT`` Multi-Status response. Larger than
#: :data:`MAX_ICS_BYTES` because the response wraps one iCalendar document per
#: event in XML, so the same week of meetings arrives several times bigger.
MAX_CALDAV_BYTES = 8 * 1024 * 1024

#: A CalDAV ``REPORT`` makes the server run a time-range query across a
#: collection, which is slower than serving a static ``.ics``.
CALDAV_TIMEOUT_SECS = 30

#: How long a started OAuth authorization stays redeemable. Long enough for a
#: human to read a consent screen, short enough that an abandoned flow's verifier
#: is not sitting in memory for the rest of the session.
OAUTH_FLOW_TTL_SECS = 10 * 60

#: Refresh an access token this long before it actually expires. A token that
#: passes the freshness check and then dies in flight surfaces as a 401 halfway
#: through a sync, which is worse than one extra refresh.
OAUTH_REFRESH_SKEW_SECS = 120

#: Timeout for a token-endpoint round trip. Shorter than a calendar fetch: this
#: is one small POST to a provider's auth host, and a hang here blocks a user
#: sitting in front of a "connecting…" UI.
OAUTH_TOKEN_TIMEOUT_SECS = 20

#: Ceiling on a token-endpoint response. A token response is a few hundred bytes;
#: this is orders of magnitude above that and still refuses a hostile stream.
MAX_OAUTH_RESPONSE_BYTES = 256 * 1024
ICS_FETCH_TIMEOUT_SECS = 20
#: Redirects are followed MANUALLY so each hop is SSRF-validated, so the chain
#: needs its own bound (aiohttp's own `max_redirects` no longer applies).
ICS_MAX_REDIRECTS = 5
CALENDAR_SYNC_DAYS = 7

# Per-agent batching dispatcher.
BATCH_INTERVAL_SECS = 30.0
MAX_DISPATCH_FAILURES = 3
BACKOFF_STEP_SECS = 60.0
BACKOFF_CAP_SECS = 180.0

# Meeting lifecycle states. ``reviewing`` is the post-stop task-review gate.
STATUS_IDLE = "idle"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_REVIEWING = "reviewing"
STATUS_ENDED = "ended"
VALID_STATUSES = (STATUS_IDLE, STATUS_ACTIVE, STATUS_PAUSED, STATUS_REVIEWING, STATUS_ENDED)

#: Which status a meeting may move to, from each status. The SERVER's copy of the
#: rule the dashboard also applies (`ALLOWED_TRANSITIONS` in `useMeetingSession`).
#:
#: The client's copy is a UI affordance — it greys out buttons. It is not
#: enforcement: the endpoint accepted any member of `VALID_STATUSES`, so an
#: authenticated `POST status=idle` against an ACTIVE meeting persisted "idle"
#: while the live session stayed installed. Transcription then stopped feeding a
#: meeting the UI still showed as running, and starting another one answered 409
#: because `ACTIVE` was still held — a state reachable through the API that the UI
#: cannot produce or explain.
#:
#: A transition to the SAME status is allowed everywhere (an idempotent retry of a
#: request whose response was lost must not fail).
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_IDLE: (STATUS_ACTIVE,),
    # NOT `ended` from either of these: reaching `ended` goes through `reviewing`,
    # which is the action-item review gate the app is built around. Allowing a
    # direct active -> ended would let the API skip the review the UI requires,
    # which is the same class of "the client enforces it, the server does not" bug
    # this table exists to close. (`POST .../stop` is the separate, deliberate exit
    # that ends a meeting outright.)
    STATUS_ACTIVE: (STATUS_PAUSED, STATUS_REVIEWING),
    STATUS_PAUSED: (STATUS_ACTIVE, STATUS_REVIEWING),
    STATUS_REVIEWING: (STATUS_PAUSED, STATUS_ENDED),
    STATUS_ENDED: (STATUS_ACTIVE,),
}

# Agent output widget kinds → output-file extension. ``chat`` agents have no
# file (their output IS the chat transcript), hence None.
WIDGET_EXT_MAP: dict[str, str | None] = {"markdown": ".md", "html": ".html", "chat": None}
DEFAULT_WIDGET_TYPE = "markdown"

# On-disk layout under ``app_data_dir("meetings")``.
DATA_SUBDIRS = ("meetings", "notes", "widgets", "tasks", "configs")
CONFIG_FILE = "config.json"
DICTIONARY_FILE = "dictionary.toml"
CALENDAR_CACHE_FILE = "calendar-cache.json"
SESSION_META_FILE = "session.json"

#: Calendar credentials (CalDAV password, Google/M365 OAuth tokens).
#:
#: **Deliberately NOT in this list's directory.** Every other name here is
#: relative to ``app_data_dir("meetings")``; this one is resolved by
#: :func:`..credentials.credentials_file` under the crew data home instead,
#: because the app data tree is the one ``store.contain`` opens to agent-driven
#: paths. Registered in ``security._CREW_SECRET_LEAVES`` so agent file tools
#: cannot read or write it. See ``backend/credentials.py`` for the full argument.
CALENDAR_CREDENTIALS_FILE = "calendar-credentials.json"
TASKS_FILE = "tasks.json"

# The always-on system agent that maintains ``tasks.json``. Not a configurable
# entry in ``meeting_agents`` — it is a core feature of the app.
TASK_EXTRACTOR_ID = "task-extractor"

#: The task extractor's DISPATCHABLE agent name — the ``name`` field from
#: ``agents/meetings-task-extractor.json``.
#:
#: Not ``f"{APP_NAME}/meetings-task-extractor"``. The namespaced form is a
#: display/tracking id; what can actually be dispatched is the declared name,
#: because that is what kiro-cli enumerates and what
#: ``bridges._register_agents`` hands to ``publish_materialized_agents``. The
#: namespaced form produced ``Mode '…' not found`` and no agent ever started.
#:
#: A constant rather than two f-strings so the two call sites cannot drift.
TASK_EXTRACTOR_AGENT = "meetings-task-extractor"

#: The namespaced prefix older builds wrote into ``config.json`` under
#: ``meeting_agents[].agent``. Those values are not dispatchable, so
#: :func:`store.read_config` strips this prefix from builtin rows on read.
LEGACY_AGENT_NAMESPACE = f"{APP_NAME}/"

# Slot-name prefix for the per-agent background chat sessions this app drives.
SLOT_PREFIX = "meetings"

# System messages injected into agent sessions at lifecycle boundaries.
SYSTEM_MEETING_ENDED = "[system] Meeting ended. Finalize your output."
SYSTEM_MEETING_RESTARTED = (
    "[system] Meeting restarted. Disregard the previous 'Meeting ended' message. "
    "Continue listening for new transcription and appending to your output."
)
CHAT_PREFIX = "[chat]"

# Task provider ids (see backend/providers/tasks.py).
TASK_PROVIDER_LOCAL = "local"
DEFAULT_TASK_PROVIDER = TASK_PROVIDER_LOCAL

# Calendar provider ids (see backend/providers/calendar.py).
CALENDAR_PROVIDER_ICS = "ics"
CALENDAR_PROVIDER_CALDAV = "caldav"
CALENDAR_PROVIDER_GOOGLE = "google"
CALENDAR_PROVIDER_NONE = "none"
DEFAULT_CALENDAR_PROVIDER = CALENDAR_PROVIDER_NONE

# ── Google Calendar ──
#
# Endpoints are constants rather than config: they are protocol, not preference,
# and a settable authorization URL is a phishing surface (a rewritten authorize
# host collects the user's Google password). The only per-installation values are
# the client id and secret, which live in the credential store.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

#: Read-only, because this app only ever displays meetings. The narrower scope is
#: also the one Google's own verification treats as least sensitive.
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

#: ``access_type=offline`` is what makes Google issue a refresh token at all, and
#: ``prompt=consent`` is what makes it issue one AGAIN on a repeat authorization —
#: without it a reconnect returns only an access token and the calendar stops
#: working an hour later.
GOOGLE_AUTHORIZE_EXTRA = {"access_type": "offline", "prompt": "consent"}

#: Ceiling on one page of the events list.
MAX_GOOGLE_RESPONSE_BYTES = 4 * 1024 * 1024

#: Events requested per page. Google caps `maxResults` at 2500; this keeps a
#: single page well inside the byte ceiling above.
GOOGLE_PAGE_SIZE = 250

#: Pages followed before giving up, so a provider that keeps handing back a
#: ``nextPageToken`` cannot loop forever.
GOOGLE_MAX_PAGES = 10

# ── Microsoft 365 (Graph) ──
#
# Constants for the same reason as Google's: a settable authorize host is a
# phishing surface for the user's work account.
#
# ``/common/`` is the multi-tenant endpoint, so a personal account and a work
# account both work without the user having to find their tenant id.
MICROSOFT_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

#: ``calendarView`` rather than ``/events``: it expands a recurring series into
#: occurrences server-side, which is what lets this provider skip an RRULE engine.
MICROSOFT_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/calendarView"

#: ``offline_access`` is not optional decoration — it is what makes Microsoft
#: issue a refresh token at all, the same role ``access_type=offline`` plays for
#: Google. Without it the calendar stops working when the first access token
#: expires. ``Calendars.Read`` is read-only because the app only displays meetings.
MICROSOFT_CALENDAR_SCOPES = (
    "https://graph.microsoft.com/Calendars.Read",
    "offline_access",
)

#: Graph returns ``dateTime`` with NO offset and names the zone separately, so the
#: reading depends entirely on this header. Asking for UTC makes the naive value
#: unambiguous instead of something to guess at.
MICROSOFT_TIMEZONE_HEADER = 'outlook.timezone="UTC"'

MAX_MICROSOFT_RESPONSE_BYTES = 4 * 1024 * 1024
MICROSOFT_PAGE_SIZE = 250
MICROSOFT_MAX_PAGES = 10

CALENDAR_PROVIDER_MICROSOFT = "microsoft"

# STT provider ids. KiroCrew's own streaming endpoint is the only one.
STT_PROVIDER_KIROCREW = "kirocrew"
DEFAULT_STT_PROVIDER = STT_PROVIDER_KIROCREW

# Task review states.
REVIEW_PENDING = "pending"
REVIEW_ARCHIVED = "archived"
REVIEW_PUSHED = "pushed"
VALID_REVIEW_STATES = (REVIEW_PENDING, REVIEW_ARCHIVED, REVIEW_PUSHED)

TASK_PRIORITIES = ("high", "medium", "low")
DEFAULT_TASK_PRIORITY = "medium"
TASK_STATES = ("open", "done")
