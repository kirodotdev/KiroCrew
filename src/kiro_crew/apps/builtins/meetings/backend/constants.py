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
#: A four-hour meeting normally produces only a few megabytes of text. This
#: ceiling prevents an unbounded app-owned file while leaving generous headroom
#: for unusually dense speech and typed interventions. Reaching it is a loud
#: 413; accepted segments are never silently truncated.
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_BATCH_CHARS = 60_000  # per flushed agent batch
#: Ceiling on transcript lines held while a starting meeting initializes its
#: agents, counted in LINES rather than bytes: the producer is one finalized
#: speech segment at a time, so lines are what the overflow rule has to reason
#: about, and each is already capped at ``MAX_TRANSCRIPT_CHARS``.
#:
#: The window is the agent-initialization span, measured at ~46s on a real
#: meeting (issue #4610). Speech finals arrive every few seconds, so a real
#: opening lands 15-25 lines here; 200 leaves an order of magnitude of headroom
#: while bounding the hold at ``200 * MAX_TRANSCRIPT_CHARS`` (~800 KB) for the
#: one meeting ``MAX_CONCURRENT_MEETINGS`` permits.
#:
#: A bound is REQUIRED, not a nicety: ``POST .../dispatch`` accepts untrusted
#: text, so an unbounded hold on that path is a memory-exhaustion lever.
MAX_INIT_BUFFER_LINES = 200
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

#: How often the background calendar poll runs when nothing overrides it. Five
#: minutes catches a meeting accepted an hour before it starts and stays well
#: inside every provider's rate limit; a manual Sync is still instant.
CALENDAR_POLL_INTERVAL_SECS = 300
#: Bounds on the configurable poll cadence. The floor keeps a misconfigured
#: install from hammering a calendar host; the ceiling keeps "every so often"
#: from silently becoming "never".
CALENDAR_POLL_MIN_SECS = 60
CALENDAR_POLL_MAX_SECS = 3600
#: How long before a calendar event starts its meeting directory is created, so
#: the user opens a meeting that already exists rather than an empty list. ``0``
#: turns pre-creation off while leaving the background sync on.
CALENDAR_PRECREATE_LEAD_MINUTES = 15
CALENDAR_PRECREATE_LEAD_MAX_MINUTES = 24 * 60
#: Pause before the first background poll after the gateway starts, so a
#: calendar fetch is not part of the startup path.
CALENDAR_POLL_STARTUP_DELAY_SECS = 20

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

#: The one widget type whose output the user may edit (the editable minutes).
#:
#: ``html`` is a GENERATED artifact — the sketch artist's Mermaid document — not
#: prose, so a textarea over its source is not an affordance anyone wants, and
#: accepting hand-written HTML would open a document-authoring surface whose output
#: renders in an iframe for no gain (it is sandboxed and CSP-locked, so this is not a
#: new hole — just not a surface worth opening). ``chat`` agents have no output file
#: at all. Read by BOTH the edit-write gate and the read overlay, so the rule lives
#: in exactly one place and cannot come apart.
EDITABLE_WIDGET_TYPE = "markdown"

# On-disk layout under ``app_data_dir("meetings")``.
DATA_SUBDIRS = ("meetings", "notes", "widgets", "tasks", "configs", "edits")
CONFIG_FILE = "config.json"
DICTIONARY_FILE = "dictionary.toml"
CALENDAR_CACHE_FILE = "calendar-cache.json"
SESSION_META_FILE = "session.json"
TASKS_FILE = "tasks.json"
TRANSCRIPT_FILE = "transcript.jsonl"
TRANSLATIONS_FILE = "translations.json"

# Durable transcript entry sources. Speech is a finalized STT segment; typed is
# a line submitted through the broadcast bar.
TRANSCRIPT_SOURCE_SPEECH = "speech"
TRANSCRIPT_SOURCE_TYPED = "typed"
#: An app-authored transcript record — currently only the init-buffer overflow
#: marker. It MUST be in ``VALID_TRANSCRIPT_SOURCES``: ``read_transcript_page``
#: drops every record whose source it does not recognize, so a marker written
#: under an unlisted source would be filtered out on read and the gap it exists
#: to announce would be silent again.
TRANSCRIPT_SOURCE_SYSTEM = "system"
VALID_TRANSCRIPT_SOURCES = (
    TRANSCRIPT_SOURCE_SPEECH,
    TRANSCRIPT_SOURCE_TYPED,
    TRANSCRIPT_SOURCE_SYSTEM,
)

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

#: Announces that the init-window hold overflowed and dropped its OLDEST lines.
#:
#: Written to the durable transcript AND enqueued to the agents, because a
#: truncation only one of them can see is the failure this marker exists to
#: prevent: the notes would simply begin mid-sentence with nothing to show a
#: turn was lost.
SYSTEM_INIT_BUFFER_OVERFLOW = (
    "[system] {count} line(s) of speech from the start of this meeting were "
    "dropped: they arrived faster than the agents could be initialized and "
    "exceeded the hold of {limit} lines. The transcript above is complete; the "
    "agents did not receive the dropped lines."
)

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

# ── live translation ────────────────────────────────────────────────────────

# Target languages for live transcript translation, as ``(code, label)``.
#
# The label is the language's own endonym and is deliberately NOT translated:
# a picker of target languages is the one place where every option should be
# readable to whoever wants that option. Same rationale as the dashboard's own
# UI-language picker.
#
# A curated list rather than every code a model might manage: each entry is a
# promise that the translation is worth reading, and it is the set MeetNote
# shipped. The empty string is not a member — see DEFAULT_TRANSLATION_LANG.
TRANSLATION_LANGS: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("zh", "中文 (简体)"),
    ("zh-TW", "中文 (繁體)"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("ru", "Русский"),
)

TRANSLATION_LANG_CODES: frozenset[str] = frozenset(code for code, _ in TRANSLATION_LANGS)

#: Off. Live translation costs one model call per spoken line, so it is opt-in —
#: a default-on feature would bill every meeting for something most do not need.
DEFAULT_TRANSLATION_LANG = ""

#: Lines waiting to be translated before the OLDEST are dropped.
#:
#: Translation runs one line at a time behind live speech, so a slow model builds
#: a backlog. Dropping is the right failure: the panel is a live aid, and a
#: translation that arrives ten minutes late is worth less than keeping up with
#: what is being said now. Transcription and the agents are never affected —
#: they do not wait on this queue.
MAX_TRANSLATION_BACKLOG = 40

#: Translated lines retained in ``translations.json``.
#:
#: Trimmed from the front when exceeded. Line numbers stay monotonic, so a client
#: polling with ``since`` is unaffected by trimming — it only loses scroll-back.
MAX_TRANSLATION_LINES = 2000
# ── editable minutes ────────────────────────────────────────────────────────

#: App-owned root holding user edits of agent outputs, outside every meeting's
#: agent-writable directory.
#:
#: The complete path is ``<data>/edits/<safe_meeting_id>/<agent-output-name>``.
#: ``apps/meetings/data/edits`` is protected by the shared sensitive-path gate, so
#: an agent can neither read an owner's unredacted correction nor overwrite it with
#: ``fs_write``. The app backend opens the sidecars directly and remains able to
#: save, overlay, revert, and delete them.
AGENT_EDITS_DIR = "edits"

#: Ceiling on one edited agent output.
#:
#: This is a whole GENERATED document the user is correcting rather than a short
#: field: the note-taker rewrites its entire file after every transcription batch,
#: so a long meeting's minutes are already larger than anything a person types into
#: the app's ordinary forms.
MAX_MINUTES_CHARS = 200_000

#: Body cap for the minutes PUT specifically, replacing ``_common.MAX_BODY_BYTES``.
#:
#: **The relationship to :data:`MAX_MINUTES_CHARS` is load-bearing, so it is spelled
#: out.** ``json_body``'s default cap is 256 KiB, which is FEWER wire bytes than
#: ``MAX_MINUTES_CHARS`` characters as soon as the text is not ASCII. A valid JSON
#: client may also escape an astral character as two UTF-16 surrogate escapes —
#: twelve wire bytes for one Python character. The body cap has to cover that valid
#: worst case, not only the browser's compact UTF-8 encoding, or the route accepts a
#: character count that some callers cannot save.
#:
#: 3 MiB covers ``200_000 * 12`` plus the small JSON envelope, and remains far
#: below the gateway's own 60 MiB ``client_max_size``. Raised for this ONE route
#: rather than for all of them: every other body here is a short field.
MAX_MINUTES_BODY_BYTES = 3 * 1024 * 1024
