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
CALENDAR_PROVIDER_NONE = "none"
DEFAULT_CALENDAR_PROVIDER = CALENDAR_PROVIDER_NONE

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
