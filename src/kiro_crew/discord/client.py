"""Discord transport layer — Gateway WebSocket (inbound) + REST (outbound).

Inbound: a persistent Gateway WebSocket connection (identify -> heartbeat ->
dispatch) receives MESSAGE_CREATE and INTERACTION_CREATE events and hands
normalized objects to the on_message / on_interaction handlers. Resume is
supported so a transient drop replays missed events instead of re-identifying.

Outbound (REST v10):
  - send_message: posts a new message (optionally with button components)
  - edit_message: edits an existing message in-place (for streaming)
  - send_message_with_files / edit_message_with_files: the same two verbs with
    attachments, over multipart instead of JSON
  - send_typing: triggers the "typing..." indicator (~10s)
  - add_reaction: emoji reaction (steer-ack receipts)
  - ack_component_interaction: DEFERRED_UPDATE_MESSAGE ack for a button press

Every outbound call runs through one ladder that spends Discord's own
rate-limit accounting (bucket pre-emption, a global hold, an invalid-request
breaker) and reports its outcome as a :class:`DiscordApiResult` so a caller can
tell a permanent refusal from something worth retrying. ``send_message_result``
and ``edit_message_result`` are the classified forms of the two send verbs;
the plain forms answer only "what is the id / did it land".

No external Discord library dependency — pure aiohttp against the public
Gateway + REST API. This keeps the module lightweight, OSS-clean, and easy to
audit (mirrors the Telegram client's design).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
import urllib.parse
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Discord message content limit (chars).
DISCORD_MAX_TEXT = 2000
# Safe chunk boundary (leave room for chip/footer overhead).
DISCORD_CHUNK_LIMIT = 1900

# Upload limits also bound extraction memory; refused files retain their markup.
DISCORD_MAX_FILE_BYTES = 10 * 1024 * 1024
DISCORD_MAX_FILES_PER_MESSAGE = 10
DISCORD_MAX_TOTAL_UPLOAD_BYTES = 25 * 1024 * 1024

# Sniffed MIME determines the canonical inline-rendering extension.
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}

# Multipart filenames are restricted before entering Content-Disposition.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_API_BASE = "https://discord.com/api/v10"
_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Transport-level WS ping interval (seconds). Deliberately a SECOND liveness
# layer under Discord's op-1 application heartbeat, because the two have
# different failure domains: op-1 runs in a cancellable user task and proves
# the Discord session is alive, while the aiohttp ping runs inside the
# library's read loop and proves the TCP path (host -> NAT -> edge) is alive.
# Without it, a NAT-evicted half-open connection blocks the dispatch loop
# forever with no error while outbound REST keeps working. 60s is longer than
# Discord's ~41s heartbeat so healthy traffic keeps the path warm anyway, and
# generous enough that a briefly stalled event loop does not flap the
# connection with false-positive pong timeouts.
_WS_HEARTBEAT_SECS = 60.0

# A Gateway connection must live at least this long for its close to count as
# "healthy" and reset the reconnect backoff; a shorter one stays on the backoff
# curve so a repeating clean close cannot hot-loop. Mirrors WebexClient and
# WeComClient, which carry the same guard for the same reason.
_MIN_HEALTHY_CONN_SECS = 5.0

# Every message body Kiro Crew sends is LLM- or tool-derived text, so no send
# may be allowed to notify anyone. ``parse: []`` is Discord's own suppression:
# it leaves ``@everyone``, ``@here``, role and user mentions rendering as text
# while stripping their notification, and it holds for a mention the renderer's
# text pass never saw. That pass (``_DISCORD_MENTION_AT_RE``, which inserts a
# zero-width space) covers only the three sites that route through
# ``_redact_transformed``; the option-choice echo, help card, queue receipt,
# threshold notice, session picker and every proactive delivery do not, and a
# text guard applied per call site is one new send path away from being wrong.
# So the guarantee lives HERE, at the one boundary every send crosses.
# ``replied_user`` is deliberately omitted (it defaults off): a reply already
# lands in the conversation the recipient is reading.
_NO_MENTIONS: dict[str, Any] = {"parse": []}


def _message_payload(
    text: str,
    components: list[dict] | None,
    *,
    keep_empty_components: bool,
) -> dict[str, Any]:
    """Build the JSON body shared by every message create/edit call.

    ``keep_empty_components`` distinguishes the two callers: an EDIT passes
    ``[]`` to retire a message's buttons, so an empty list must survive into the
    payload, while a CREATE treats empty as "no components" and omits the key.
    """
    payload: dict[str, Any] = {
        "content": text[:DISCORD_MAX_TEXT],
        "allowed_mentions": _NO_MENTIONS,
    }
    include = components is not None if keep_empty_components else bool(components)
    if include:
        payload["components"] = components
    return payload


def _create_payload(
    text: str, components: list[dict] | None, reply_to_message_id: str | None
) -> dict[str, Any]:
    """The JSON body for a message CREATE, with or without attachments.

    ``fail_if_not_exists: False`` keeps a reply to a message the user deleted
    mid-turn from failing the whole send; it lands unthreaded instead.
    """
    payload = _message_payload(text, components, keep_empty_components=False)
    if reply_to_message_id:
        payload["message_reference"] = {
            "message_id": reply_to_message_id,
            "fail_if_not_exists": False,
        }
    return payload


# Attachment URLs are signed but unauthenticated. Keep the exact CDN host
# allow-list here at the channel boundary; redirects are disabled during fetch
# so an allowed URL cannot bounce the downloader to an arbitrary host.
_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})

# Gateway intents. DM-only installations request DIRECT_MESSAGES alone. When
# an explicit server-thread allow-list is configured, GUILD_MESSAGES delivers
# thread messages and the privileged MESSAGE_CONTENT intent exposes their text.
_INTENT_DIRECT_MESSAGES = 1 << 12
_INTENT_GUILD_MESSAGES = 1 << 9
_INTENT_MESSAGE_CONTENT = 1 << 15
_THREAD_INTENTS = _INTENT_GUILD_MESSAGES | _INTENT_MESSAGE_CONTENT

# Discord channel types: announcement thread, public thread, private thread.
_THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})

# Gateway opcodes.
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Interaction types / callback types.
_INTERACTION_APPLICATION_COMMAND = 2
_INTERACTION_MESSAGE_COMPONENT = 3
_CALLBACK_CHANNEL_MESSAGE_WITH_SOURCE = 4
_CALLBACK_DEFERRED_UPDATE_MESSAGE = 6

#: Interaction types this client normalizes and forwards. Anything else is
#: dropped at the dispatch boundary rather than handed on as a half-filled
#: record — an autocomplete or modal submit needs its own response shape within
#: Discord's 3-second callback deadline, and a handler that received one without
#: knowing how to answer it would leave the user's client spinning.
_HANDLED_INTERACTIONS = frozenset(
    {_INTERACTION_APPLICATION_COMMAND, _INTERACTION_MESSAGE_COMPONENT}
)

#: Message flag EPHEMERAL (``1 << 6``): the reply is visible only to the user
#: who invoked the interaction and Discord does not persist it. Every command
#: reply uses it — a slash command in a shared guild thread would otherwise
#: publish runtime state, a dashboard login link, or a model list to everyone
#: who can read the thread.
_FLAG_EPHEMERAL = 1 << 6

#: Discord rejects a whole bulk-overwrite array on one malformed row, and
#: allows 200 command creates per day, so the payload is validated locally
#: before it is sent rather than discovered by a 400.
_APP_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_APP_COMMAND_DESC_LIMIT = 100


# ── REST rate-limit accounting ─────────────────────────────────────────────
#
# Discord answers every REST call with the state of the bucket it charged, and
# punishes an app that ignores it: 10,000 responses of status 401, 403 or 429
# within 10 minutes gets the IP temporarily blocked, which costs every channel
# on the host rather than the one route that misbehaved. So the ladder spends
# the accounting it is handed (wait out a bucket that has nothing left, hold
# every route while a GLOBAL limit is in force) and stops itself far short of
# that ceiling instead of discovering it.

#: Bucket state, present on every response. ``Reset-After`` is FRACTIONAL
#: seconds; ``Bucket`` is the opaque id of the limit the route was charged to,
#: and several routes share one, so it is what state is keyed by.
_HDR_BUCKET = "X-RateLimit-Bucket"
_HDR_REMAINING = "X-RateLimit-Remaining"
_HDR_RESET_AFTER = "X-RateLimit-Reset-After"
#: 429 only: ``user`` | ``global`` | ``shared``.
_HDR_SCOPE = "X-RateLimit-Scope"
#: Whole seconds, which is why the 429 body's fractional ``retry_after`` wins
#: when both are present.
_HDR_RETRY_AFTER = "Retry-After"
_SCOPE_GLOBAL = "global"
#: A 429 on a limit Discord shares between apps is not attributed to us and is
#: excluded from the invalid-request ban count, so it must not trip the breaker.
_SCOPE_SHARED = "shared"

#: Back-off floor, so a ``retry_after`` of 0 cannot become a hot loop.
_MIN_HOLD_SECS = 0.5
#: Ceiling on the back-off served INSIDE a call after a route-scoped 429. A
#: route's allowance resets in seconds; past this, returning a transient
#: failure the caller can re-drive beats parking the turn in the transport.
_MAX_RETRY_AFTER_SECS = 5.0
#: Ceiling on a wait taken BEFORE issuing a call, which may run longer than the
#: one above because waiting is strictly cheaper than the 429 it prevents: the
#: 429 spends ban budget shared with every other route, the wait spends nothing.
_MAX_PREEMPT_SECS = 10.0
#: Ceiling on a GLOBAL hold. Highest of the three because the app's whole
#: 50 requests/second allowance is what ran out: continuing to send through it
#: is precisely what earns the IP block, so waiting is the cheaper failure.
_MAX_GLOBAL_HOLD_SECS = 30.0
#: Back-off used when a 429 names no usable delay.
_DEFAULT_RETRY_AFTER_SECS = 1.0

#: Retries per failure class, deliberately different. A 429 already told us how
#: long to wait, so one honoured retry either lands or the route is genuinely
#: saturated, and every further 429 spends ban budget. A 5xx or a connector
#: blip spends none and is far likelier to clear, so it gets the same
#: three-attempt budget as the Slack DM path in ``slack/retry.py``.
_RATE_LIMIT_RETRIES = 1
_TRANSIENT_RETRIES = 2
#: Linear back-off step for the transient class (1s, then 2s), matching that
#: same path so the two cannot drift for opposite reasons.
_TRANSIENT_BACKOFF_SECS = 1.0

#: Statuses that spend Discord's invalid-request budget.
_INVALID_STATUSES = frozenset({401, 403, 429})
#: Discord's own ceiling, named so the breaker's margin is legible.
_DISCORD_INVALID_BAN_LIMIT = 10_000
#: Discord counts invalid responses over 10 minutes, so the breaker uses the
#: same window and trips at 5% of the ceiling: a runaway loop is stopped with
#: the budget almost untouched, and no realistic legitimate burst reaches it.
_INVALID_WINDOW_SECS = 600.0
_INVALID_LIMIT = 500
#: How long the breaker refuses to issue anything once tripped. Long enough
#: that the window drains meaningfully, short enough that a channel recovers
#: without an operator.
_BREAKER_COOLOFF_SECS = 120.0

#: Upper bound on tracked routes and buckets. The route space is small but open
#: (one entry per verb per channel), so the maps are capped least-recently-used.
_MAX_TRACKED_ROUTES = 256

#: Path segments whose FOLLOWING id is one of Discord's "major parameters":
#: those buckets are per-id, everything else shares one bucket across ids, so
#: the route key keeps the majors and collapses the rest.
_MAJOR_PARAM_PARENTS = frozenset({"channels", "guilds", "webhooks"})
_ID_SEGMENT_RE = re.compile(r"^\d{5,}$")
#: Parents whose next path segment is a CREDENTIAL, not an id: an interaction
#: token and a webhook token both authorize the call that carries them. They are
#: collapsed structurally rather than by the length rule below, because the route
#: key is what gets logged and a length threshold is the wrong thing to rest a
#: credential on: a short or changed token would print verbatim into the log ring
#: and into `kirocrew logs`.
_TOKEN_SEGMENT_PARENTS = frozenset({"interactions", "webhooks"})
#: An interaction token rides in the path and is unique per interaction, so a
#: verbatim route key would grow one entry per button press. No literal segment
#: in Discord's API is anywhere near this long.
_MAX_LITERAL_SEGMENT = 24

#: ``DiscordApiResult.outcome``: the request was delivered.
DISCORD_OK = "ok"
#: A 4xx other than 429. The same request will fail identically forever (a
#: missing permission, a deleted message, a malformed payload), so retrying it
#: only spends ban budget. This is ``slack/retry.py``'s classification rule,
#: kept identical here so the two channels cannot drift.
DISCORD_PERMANENT = "permanent"
#: A 429, a 5xx, a timeout or a connector error whose retry budget is spent.
#: The same request may well succeed later.
DISCORD_TRANSIENT = "transient"
#: Refused locally by the invalid-request breaker, without touching the
#: network. Transient in nature, but distinct because nothing was sent.
DISCORD_BLOCKED = "blocked"


def _rate_limit_headers(resp: Any) -> Mapping[str, str]:
    """Response headers as a mapping, empty when there are none to read.

    Discord sends the bucket headers on every REST response, but an
    intercepting proxy's error page need not: a response without them simply
    carries no accounting information, and must not turn a delivered request
    into a failed one.
    """
    headers = getattr(resp, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _coerce_float(raw: Any) -> float | None:
    """A numeric field as a FINITE float, or None when it is absent or not one.

    Every rate-limit number Discord sends, in a header or a 429 body, reaches a
    sleep duration through here, so the finiteness check belongs at this one
    chokepoint rather than at each clamp. ``float("nan")`` and ``float("inf")``
    both parse, and NaN then survives every guard downstream: ``nan <= 0`` is
    False so the "no hold needed" branch does not take it, and
    ``min(max(nan, floor), ceiling)`` is still NaN, so a single malformed or
    hostile header would become ``asyncio.sleep(nan)`` and wedge the client for
    the life of the process. Treating a non-finite value as absent falls back to
    the documented default instead.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _header_float(headers: Mapping[str, str], name: str) -> float | None:
    return _coerce_float(headers.get(name))


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = _coerce_float(headers.get(name))
    return None if value is None else int(value)


def _remember(store: dict[str, _T], key: str, value: _T) -> None:
    """Insert into a bounded most-recently-used map.

    Dropping the least recently touched entry costs only the pre-emption it
    carried: the next call on that route earns a 429 and re-learns its bucket,
    which is exactly the behaviour with no accounting at all. An unbounded map
    in a process that runs for weeks is the worse failure.
    """
    store.pop(key, None)
    store[key] = value
    while len(store) > _MAX_TRACKED_ROUTES:
        del store[next(iter(store))]


#: URL path separator for a Discord REST route. Fixed by the URL grammar, so
#: it is deliberately NOT a filesystem path join.
_ROUTE_SEP = "/"


def _route_key(method: str, path: str) -> str:
    """Collapse a concrete path onto the rate-limit route it belongs to.

    Discord buckets a route per "major parameter" (the channel, guild or
    webhook id) and shares one bucket across every other id, so
    ``/channels/A/messages/1`` and ``/channels/A/messages/2`` are one route
    while channel ``B`` is another.

    ``path`` is a REST route, not a filesystem path, so the separator is fixed by
    the URL grammar and named here rather than spelled inline: `os.path.join` /
    `pathlib` would emit a backslash on Windows and silently stop matching any
    bucket. The portability gate cannot tell the two apart from a line regex.
    """
    segments = path.split(_ROUTE_SEP)
    out: list[str] = []
    for index, segment in enumerate(segments):
        parent = segments[index - 1] if index else ""
        if _ID_SEGMENT_RE.match(segment):
            out.append(segment if parent in _MAJOR_PARAM_PARENTS else "{id}")
        elif index >= 2 and segments[index - 2] in _TOKEN_SEGMENT_PARENTS:
            # `/interactions/{id}/{token}/callback` and
            # `/webhooks/{id}/{token}` put the credential two segments after the
            # parent, so the grandparent is what identifies it.
            out.append("{token}")
        elif len(segment) > _MAX_LITERAL_SEGMENT:
            out.append("{token}")
        else:
            out.append(segment)
    return f"{method} {_ROUTE_SEP.join(out)}"


def _is_global_limit_exempt(path: str) -> bool:
    """True for the routes Discord exempts from the app's global allowance.

    Interaction callbacks and their followups are exempt, and they answer a
    ~3 second deadline: holding one behind a global limit it does not consume
    would leave the user's client spinning for nothing.
    """
    return path.startswith("/interactions/") or path.startswith("/webhooks/")


@dataclass(frozen=True)
class DiscordApiResult:
    """One REST call's outcome.

    A bare ``None`` could only say "something went wrong", which is how a turn
    whose entire output failed to send gets recorded as a delivered turn. This
    record says WHICH kind of wrong, so a caller can distinguish a refusal that
    will repeat forever (``DISCORD_PERMANENT``: report it, do not re-send) from
    one worth another attempt later (``DISCORD_TRANSIENT`` / ``DISCORD_BLOCKED``).

    ``data`` is the parsed JSON body (``{}`` for a 204 or an undecodable 2xx)
    and is ``None`` on every failure, so both historical body tests
    (``if body:`` and ``body is not None``) keep meaning "did it land".
    """

    outcome: str
    data: Any = None
    #: HTTP status, ``0`` when no response was received (timeout, connector
    #: error, or a request the breaker refused to issue).
    status: int = 0
    #: Discord's own JSON error code, ``0`` when the body carried none.
    code: int = 0
    #: Short operator-facing reason. Never carries the bot token.
    detail: str = ""

    def __bool__(self) -> bool:
        """True only for a delivered request."""
        return self.outcome == DISCORD_OK

    @property
    def retryable(self) -> bool:
        """True when the identical request could succeed on a later attempt."""
        return self.outcome in (DISCORD_TRANSIENT, DISCORD_BLOCKED)

    @property
    def message_id(self) -> str:
        """The created/edited message's snowflake, empty when none was returned."""
        return str(self.data.get("id", "")) if isinstance(self.data, dict) else ""


def _failed(outcome: str, *, status: int = 0, code: int = 0, detail: str = "") -> DiscordApiResult:
    """A failure result. ``data`` stays None so body checks read as "no send"."""
    return DiscordApiResult(outcome=outcome, status=status, code=code, detail=detail)


@dataclass
class DiscordInbound:
    """Normalized inbound message from a MESSAGE_CREATE dispatch."""

    channel_id: str
    user_id: str
    username: str = ""
    text: str = ""
    message_id: str = ""
    guild_id: str = ""  # empty string == DM channel
    is_bot: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscordInteraction:
    """Normalized INTERACTION_CREATE dispatch — a button press or a command.

    One record covers both because everything the dispatcher needs first
    (identity, channel, guild, and the ack it owes Discord within ~3s) is shared;
    ``kind`` selects which of the two payload halves is populated.
    """

    interaction_id: str
    interaction_token: str
    channel_id: str
    user_id: str
    message_id: str
    custom_id: str = ""
    label: str = ""  # button text, recovered from the message's components
    guild_id: str = ""  # empty string == DM channel
    username: str = ""
    #: Discord interaction type — ``2`` application command, ``3`` message
    #: component. Defaults to the component type so the pre-existing button
    #: call sites (and the tests that construct this directly) keep their
    #: meaning without naming it.
    kind: int = _INTERACTION_MESSAGE_COMPONENT
    #: Application-command name (``kind == 2`` only), without a leading slash.
    command_name: str = ""
    #: Top-level command options as ``{name: value}``, values stringified.
    #: Discord types them, but every command here takes free text or nothing,
    #: and one shape keeps the parser shared with the ``!`` text commands.
    options: dict[str, str] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        """True for an application (slash) command invocation."""
        return self.kind == _INTERACTION_APPLICATION_COMMAND


class DiscordClient:
    """Discord Gateway + REST client with auto-reconnect and resume.

    Connects via the Gateway WebSocket (works behind NAT/firewall — no public
    webhook endpoint needed). Dispatches messages to on_message and button
    presses to on_interaction.
    """

    def __init__(
        self,
        *,
        token: str,
        on_message: Callable[[DiscordInbound], Awaitable[None]] | None = None,
        on_interaction: Callable[[DiscordInteraction], Awaitable[None]] | None = None,
        enable_guild_threads: bool = False,
        proxy: str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._on_interaction = on_interaction
        self._intents = _INTENT_DIRECT_MESSAGES | (_THREAD_INTENTS if enable_guild_threads else 0)
        self._proxy = proxy or _resolve_proxy()
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._hb_task: asyncio.Task[None] | None = None
        self._closed = False
        # Resume state (from READY / dispatch sequence numbers).
        self._seq: int | None = None
        self._session_id: str = ""
        self._resume_url: str = ""
        self._hb_acked = True
        self._ws: Any = None  # WS handle; aiohttp generics vary across versions
        # Live turn tasks — prevent GC of in-flight handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # Bot's own user id (from READY) so we can drop our own messages.
        self.bot_user_id: str = ""
        # Application id (from READY) — the path parameter for application
        # command registration. Empty until the first handshake completes.
        self.application_id: str = ""
        # channel_id -> Discord channel type. This proves a configured ID is
        # actually a thread before any shared-channel turn can run.
        self._channel_types: dict[str, int] = {}
        # Rate-limit accounting, per client rather than per process: the limits
        # it models are per bot token, and a second client (a test, a second
        # channel) must not inherit another's holds or breaker state.
        # route key -> the Discord bucket id that route was last charged to.
        self._route_buckets: dict[str, str] = {}
        # bucket id (or route key, before one is known) -> monotonic deadline
        # before which that bucket has no requests left.
        self._holds: dict[str, float] = {}
        # Monotonic deadline for a GLOBAL limit: holds every non-exempt route.
        self._global_ready_at: float = 0.0
        # Monotonic stamps of responses that spent invalid-request budget.
        # Bounded by construction so a long-lived process cannot grow it.
        self._invalid_hits: deque[float] = deque(maxlen=_INVALID_LIMIT)
        # Monotonic deadline while the invalid-request breaker is OPEN; 0 closed.
        self._breaker_until: float = 0.0
        # Set when the Gateway handshake reaches READY (cleared while
        # disconnected/reconnecting). ``wait_ready`` gates "connected" status.
        self.ready: asyncio.Event = asyncio.Event()
        # Short reason when the connection died non-recoverably (bad token /
        # bad intents); empty otherwise. Read by the status callback path.
        self.fatal_error: str = ""
        # Optional observer called with (connected: bool, error: str) on READY
        # and on non-recoverable close — lets the gateway keep the dashboard
        # status badge truthful after boot.
        self.on_state_change: Callable[[bool, str], None] | None = None

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the Gateway handshake to reach READY. Returns False on
        timeout or when the connection already failed non-recoverably."""
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _notify_state(self, connected: bool, error: str) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("Discord on_state_change observer raised", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background Gateway connection loop."""
        self._closed = False
        self._task = asyncio.create_task(self._gateway_loop())

    async def close(self) -> None:
        """Gracefully shut down.

        Inbound work is fast-acked into background tasks (``_invoke_message`` /
        ``_invoke_interaction``), so those tasks are cancelled and AWAITED
        before the shared ``ClientSession`` they send through is closed —
        the transport-shutdown quiescence invariant in
        ``docs/system-specs/modules/messaging.md``. Closing the session first
        leaves an in-flight turn issuing REST calls against a closed session,
        which surfaces to the user as a reply that silently stops mid-stream
        rather than as a shutdown.
        """
        self._closed = True
        self._stop_heartbeat()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Snapshot first: a cancelled handler's done-callback mutates the set.
        handlers = list(self._handler_tasks)
        for handler in handlers:
            handler.cancel()
        if handlers:
            # return_exceptions so one handler raising during unwind cannot
            # abandon the others or skip the session close below.
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def set_message_handler(self, on_message: Callable[[DiscordInbound], Awaitable[None]]) -> None:
        """Set/replace the inbound-message handler after construction.

        Lets the gateway wire ``transport.receive`` in once the transport
        (which needs the client) has been built, avoiding a construction cycle.
        """
        self._on_message = on_message

    # ── Outbound REST ──

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send a new message. Returns the message id (snowflake string).

        Discord renders standard Markdown natively, so ``text`` is sent as-is.
        A caller that must act on a failure (report it, retry it, refuse to
        record the turn as delivered) uses :meth:`send_message_result` instead:
        an id of ``None`` cannot say whether sending again would help.
        """
        result = await self._api(
            "POST",
            f"/channels/{channel_id}/messages",
            _create_payload(text, components, reply_to_message_id),
        )
        return str(result.get("id")) if result else None

    async def send_message_result(
        self,
        channel_id: str,
        text: str,
        *,
        files: Sequence[OutboundFile] = (),
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
    ) -> DiscordApiResult:
        """Send a new message and report the outcome, not just the id.

        The classified form of :meth:`send_message` and
        :meth:`send_message_with_files`: ``result.message_id`` is the snowflake,
        ``result.outcome`` says whether a failure is worth another attempt.
        Attachments switch the same call to multipart, so one verb covers both.
        """
        payload = _create_payload(text, components, reply_to_message_id)
        path = f"/channels/{channel_id}/messages"
        if files:
            return await self.api_files("POST", path, payload, files)
        return await self.api_json("POST", path, payload)

    async def edit_message_result(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        files: Sequence[OutboundFile] = (),
        components: list[dict] | None = None,
    ) -> DiscordApiResult:
        """Edit a message in place and report the outcome.

        The classified form of :meth:`edit_message` /
        :meth:`edit_message_with_files`. A streaming edit that fails
        permanently (the message was deleted) must stop the stream rather than
        be retried, which a bare ``False`` cannot express.
        """
        payload = _message_payload(text, components, keep_empty_components=True)
        path = f"/channels/{channel_id}/messages/{message_id}"
        if files:
            return await self.api_files("PATCH", path, payload, files)
        return await self.api_json("PATCH", path, payload)

    async def create_thread_from_message(
        self, channel_id: str, message_id: str, name: str
    ) -> str | None:
        """Create a public thread rooted at an existing channel message."""
        result = await self._api(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/threads",
            {"name": name[:100], "auto_archive_duration": 1440},
        )
        if not result:
            return None
        thread_id = str(result.get("id") or "")
        if thread_id:
            self._channel_types[thread_id] = 11
        return thread_id or None

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        components: list[dict] | None = None,
    ) -> bool:
        """Edit an existing message in-place (for streaming)."""
        payload = _message_payload(text, components, keep_empty_components=True)
        result = await self._api("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)
        return result is not None

    async def send_message_with_files(
        self,
        channel_id: str,
        text: str,
        files: Sequence[OutboundFile],
        *,
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send validated bytes; paths supply only sanitized filenames."""
        if not files:
            return await self.send_message(
                channel_id,
                text,
                components=components,
                reply_to_message_id=reply_to_message_id,
            )
        result = await self._api_multipart(
            "POST",
            f"/channels/{channel_id}/messages",
            _create_payload(text, components, reply_to_message_id),
            files,
        )
        return str(result.get("id", "")) if result is not None else None

    async def edit_message_with_files(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        files: Sequence[OutboundFile],
        *,
        components: list[dict] | None = None,
    ) -> bool:
        """Edit a streamed message and replace its attachments with ``files``."""
        if not files:
            return await self.edit_message(channel_id, message_id, text, components=components)
        payload = _message_payload(text, components, keep_empty_components=True)
        result = await self._api_multipart(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", payload, files
        )
        return result is not None

    async def send_typing(self, channel_id: str) -> None:
        """Trigger the 'typing...' indicator (Discord shows it ~10s)."""
        await self._api("POST", f"/channels/{channel_id}/typing", {})

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Add a unicode emoji reaction to a message (steer-ack receipt).

        Best-effort: callers should treat failures as non-fatal.
        """
        enc = urllib.parse.quote(emoji, safe="")
        await self._api(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{enc}/@me",
            None,
        )

    async def register_application_commands(self, commands: list[dict[str, Any]]) -> bool:
        """Publish the global application (slash) command set, replacing it.

        ``PUT`` is a bulk overwrite, so this is the whole command set every time
        and a command dropped from the payload disappears from Discord — which
        is what makes it safe to call on every start rather than diffing.

        Rows that break Discord's own constraints are skipped rather than sent:
        Discord rejects the ENTIRE array on one bad row, so a single malformed
        entry would otherwise cost the user every command. Returns False when
        there is no application id yet (no READY) or the call failed; a caller
        treats that as "no slash menu this run", never as fatal — the ``!``
        text commands are the floor and remain available regardless.
        """
        if not self.application_id:
            logger.warning("Discord: no application id yet — skipping command registration")
            return False
        rows: list[dict[str, Any]] = []
        for cmd in commands:
            name = str(cmd.get("name", "") or "")
            desc = str(cmd.get("description", "") or "")
            if not _APP_COMMAND_NAME_RE.match(name) or not desc:
                logger.warning("Discord: skipping malformed application command %r", name)
                continue
            rows.append({**cmd, "name": name, "description": desc[:_APP_COMMAND_DESC_LIMIT]})
        if not rows:
            return False
        result = await self._api("PUT", f"/applications/{self.application_id}/commands", rows)
        if result is None:
            return False
        logger.info("Discord: registered %d application command(s)", len(rows))
        return True

    async def respond_interaction(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool = True,
        components: list[dict] | None = None,
    ) -> bool:
        """Answer an interaction with an immediate message.

        Ephemeral by default: a command reply carries runtime state, a login
        link, or a model list, and an approved guild thread is readable by every
        member who can see it. A caller that genuinely wants a visible message
        passes ``ephemeral=False`` and says why.
        """
        payload: dict[str, Any] = {
            "type": _CALLBACK_CHANNEL_MESSAGE_WITH_SOURCE,
            "data": {
                **_message_payload(text, components, keep_empty_components=False),
                **({"flags": _FLAG_EPHEMERAL} if ephemeral else {}),
            },
        }
        result = await self._api(
            "POST", f"/interactions/{interaction_id}/{interaction_token}/callback", payload
        )
        return result is not None

    async def ack_component_interaction(self, interaction_id: str, interaction_token: str) -> None:
        """Acknowledge a button press without changing the message.

        DEFERRED_UPDATE_MESSAGE stops Discord's "interaction failed" spinner;
        the actual message update happens via a normal ``edit_message``.
        """
        await self._api(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            {"type": _CALLBACK_DEFERRED_UPDATE_MESSAGE},
        )

    async def download_attachment(self, url: str, dest: str) -> None:
        """Download a signed Discord CDN attachment without bot credentials.

        Only Discord's two documented delivery hosts are accepted. Redirects
        are deliberately refused so host validation remains true for the bytes
        ultimately written to ``dest``.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid Discord attachment URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ATTACHMENT_HOSTS
            or port not in (None, 443)
        ):
            raise ValueError("refusing non-Discord attachment URL")

        session = await self._ensure_session()
        async with session.get(
            url,
            proxy=self._proxy,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,
        ) as resp:
            if 300 <= resp.status < 400:
                raise ValueError("refusing redirected Discord attachment URL")
            resp.raise_for_status()
            fh = await asyncio.to_thread(open, dest, "wb")
            try:
                async for chunk in resp.content.iter_chunked(8192):
                    await asyncio.to_thread(fh.write, chunk)
            finally:
                await asyncio.to_thread(fh.close)

    async def create_dm_channel(self, user_id: str) -> str:
        """Create (or fetch) the DM channel for a user id. Returns the channel
        id, or empty string on failure. Needed for proactive sends — outbound
        messages address a channel, not a user."""
        result = await self._api("POST", "/users/@me/channels", {"recipient_id": user_id})
        return str(result.get("id")) if result else ""

    async def is_thread_channel(self, channel_id: str) -> bool:
        """Confirm ``channel_id`` is a Discord thread, failing closed.

        IDs are user-configured, but an ID alone does not prove channel type.
        Resolve the channel once through Discord and cache its immutable type,
        so accidentally allow-listing a normal guild channel cannot expose
        agent or tool output there.
        """
        cached = self._channel_types.get(channel_id)
        if cached is not None:
            return cached in _THREAD_CHANNEL_TYPES
        result = await self._api("GET", f"/channels/{channel_id}", None)
        if not isinstance(result, dict) or not isinstance(result.get("type"), int):
            return False
        channel_type = int(result["type"])
        self._channel_types[channel_id] = channel_type
        return channel_type in _THREAD_CHANNEL_TYPES

    async def edit_message_components(
        self, channel_id: str, message_id: str, components: list[dict]
    ) -> bool:
        """Edit ONLY a message's components, leaving its content intact.

        Used to retire an ``[OPTIONS:]`` button row after a choice is tapped
        without clobbering the answer text that carried it. Pass ``[]`` to
        remove the buttons.
        """
        result = await self._api(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            {"components": components},
        )
        return result is not None

    # ── Gateway connection loop ──

    async def _gateway_loop(self) -> None:
        """Persistent Gateway connection with resume + exponential backoff."""
        attempt = 0
        while not self._closed:
            started = time.monotonic()
            reason: str | None = None
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if self._closed:
                    break
                # Log only the exception type — never the token.
                reason = type(exc).__name__
            except Exception:
                if self._closed:
                    break
                logger.exception("Discord gateway unexpected error")
                # Flat, not on the curve: an unexpected exception is a code
                # fault rather than a network condition, and its retry cadence
                # is pinned separately.
                await asyncio.sleep(5.0)
                continue
            finally:
                self._stop_heartbeat()
            if self._closed:
                break
            if reason is None:
                # A clean close reconnects promptly — that is what an op-7
                # RECONNECT asks for — but only when the connection actually
                # lived a while. A connect->immediate-clean-close (a proxy
                # terminating the upgrade, a server-side condition that
                # persists) would otherwise spin with ZERO delay: `attempt`
                # reset on every pass, so the backoff curve was unreachable
                # from this path and nothing bounded the rate. Discord bans an
                # identity for 10 minutes after 10,000 invalid requests, so an
                # unbounded loop costs the channel, not just CPU.
                if time.monotonic() - started >= _MIN_HEALTHY_CONN_SECS:
                    attempt = 0
                    continue
                reason = "gateway closed the connection immediately"
            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 60.0) + random.random()
            logger.warning("Discord gateway disconnected (%s), reconnect in %.1fs", reason, delay)
            await asyncio.sleep(delay)

    async def _run_connection(self) -> None:
        """One Gateway connection: hello -> identify/resume -> dispatch loop."""
        session = await self._ensure_session()
        url = self._resume_url if (self._session_id and self._resume_url) else _GATEWAY_URL
        ws: Any = None
        try:
            async with session.ws_connect(
                url, proxy=self._proxy, heartbeat=_WS_HEARTBEAT_SECS, max_msg_size=0
            ) as ws:
                self._ws = ws
                try:
                    async for raw in ws:
                        if raw.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_frame(ws, json.loads(raw.data))
                        elif raw.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                finally:
                    self._ws = None
            # Clean close only: classify the close code (4004 etc. surfaces
            # via ws.close_code). An exception path skips this block and gets
            # its log line from the gateway loop's reconnect handler instead.
            code = ws.close_code or 0
            if code in (4004, 4010, 4011, 4012, 4013, 4014):
                # Non-recoverable (bad token / bad intents). Stop rather than
                # hammering the gateway forever; the failure is already logged.
                logger.error(
                    "Discord gateway closed with non-recoverable code %s — "
                    "check the bot token and intents. Channel stopped.",
                    code,
                )
                self._closed = True
                self.fatal_error = f"gateway close {code} (check bot token/intents)"
                self._notify_state(False, self.fatal_error)
            elif code in (4007, 4009):
                # Invalid seq / session timed out — must re-identify from scratch.
                self._session_id = ""
                self._seq = None
            if not self._closed:
                # Every connection end is WARNING-visible: the default log
                # level hides INFO, and an unlogged silent reconnect loop is
                # indistinguishable from a dead channel when diagnosing
                # "Discord stopped responding".
                logger.warning(
                    "Discord gateway connection ended (close code %s) — reconnecting",
                    code or "none",
                )
        finally:
            # Runs on clean closes AND exception escapes (a send raising
            # mid-dispatch would otherwise skip this): the READY flag and the
            # dashboard badge must never keep asserting a connection that no
            # longer exists. The 4004 branch sets _closed before its own
            # fatal notify, so this cannot overwrite that reason; deliberate
            # close() also sets _closed and is skipped the same way.
            self.ready.clear()
            if not self._closed:
                close_code = ws.close_code if ws is not None else None
                self._notify_state(False, f"reconnecting (close {close_code or 'none'})")

    async def _handle_frame(self, ws: Any, frame: dict) -> None:
        op = frame.get("op")
        if frame.get("s") is not None:
            self._seq = frame["s"]
        if op == _OP_HELLO:
            interval = float(frame["d"]["heartbeat_interval"]) / 1000.0
            self._start_heartbeat(ws, interval)
            if self._session_id and self._resume_url:
                await ws.send_json(
                    {
                        "op": _OP_RESUME,
                        "d": {
                            "token": self._token,
                            "session_id": self._session_id,
                            "seq": self._seq or 0,
                        },
                    }
                )
            else:
                await self._identify(ws)
        elif op == _OP_HEARTBEAT:
            await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
        elif op == _OP_HEARTBEAT_ACK:
            self._hb_acked = True
        elif op == _OP_RECONNECT:
            await ws.close()
        elif op == _OP_INVALID_SESSION:
            if not frame.get("d"):
                self._session_id = ""
                self._seq = None
            await asyncio.sleep(1.0 + random.random() * 4.0)  # per docs
            await ws.close()
        elif op == _OP_DISPATCH:
            self._on_dispatch(frame.get("t") or "", frame.get("d") or {})

    async def _identify(self, ws: Any) -> None:
        await ws.send_json(
            {
                "op": _OP_IDENTIFY,
                "d": {
                    "token": self._token,
                    "intents": self._intents,
                    "properties": {
                        "os": "linux",
                        "browser": "kirocrew",
                        "device": "kirocrew",
                    },
                },
            }
        )

    def _start_heartbeat(self, ws: Any, interval: float) -> None:
        self._stop_heartbeat()
        self._hb_acked = True
        self._hb_task = asyncio.create_task(self._heartbeat_loop(ws, interval))

    def _stop_heartbeat(self) -> None:
        task, self._hb_task = self._hb_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        """Heartbeat at the server's interval; a missed ack forces a reconnect
        (close -> the gateway loop resumes the session)."""
        try:
            await asyncio.sleep(interval * random.random())  # jitter per docs
            while not self._closed and not ws.closed:
                if not self._hb_acked:
                    logger.warning("Discord gateway heartbeat not acked — reconnecting")
                    await ws.close()
                    return
                self._hb_acked = False
                await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            # This task is the only sender of Discord's mandatory op-1
            # heartbeat: if it dies and the connection stays up, the server
            # eventually drops the session and — behind NAT, where the close
            # frame can be lost — the dispatch loop blocks on a half-open
            # socket with heartbeats gone. Close the socket so the gateway
            # loop reconnects instead of carrying a heartbeat-less connection.
            logger.warning("Discord heartbeat loop failed — recycling connection", exc_info=True)
            try:
                await ws.close()
            except Exception:
                logger.debug("Discord heartbeat close failed", exc_info=True)

    # ── Dispatch normalization ──

    def _on_dispatch(self, event: str, d: dict) -> None:
        if event == "READY":
            self._session_id = d.get("session_id", "")
            self._resume_url = d.get("resume_gateway_url", "")
            if self._resume_url and "?" not in self._resume_url:
                self._resume_url += "/?v=10&encoding=json"
            self.bot_user_id = str((d.get("user") or {}).get("id", ""))
            # READY carries the application object, so the id needed to register
            # application commands arrives without a second REST call — and it
            # is kept as a STRING because a snowflake exceeds 2^53 and would
            # lose precision as a float on any round-trip through JSON.
            self.application_id = str((d.get("application") or {}).get("id", "") or "")
            logger.info("Discord gateway READY (bot user %s)", self.bot_user_id)
            self.ready.set()
            self._notify_state(True, "")
            return
        if event == "RESUMED":
            logger.info("Discord gateway session resumed")
            self.ready.set()
            self._notify_state(True, "")
            return
        if event == "MESSAGE_CREATE":
            author = d.get("author") or {}
            inbound = DiscordInbound(
                channel_id=str(d.get("channel_id", "")),
                user_id=str(author.get("id", "")),
                username=author.get("username", ""),
                text=d.get("content", ""),
                message_id=str(d.get("id", "")),
                guild_id=str(d.get("guild_id", "") or ""),
                is_bot=bool(author.get("bot", False)),
                attachments=[
                    attachment
                    for attachment in (d.get("attachments") or [])
                    if isinstance(attachment, dict)
                ],
            )
            if inbound.is_bot or inbound.user_id == self.bot_user_id:
                return  # never respond to bots (incl. ourselves) — loop guard
            task = asyncio.create_task(self._invoke_message(inbound))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)
            return
        if event == "INTERACTION_CREATE":
            kind = d.get("type")
            if kind not in _HANDLED_INTERACTIONS:
                return
            data = d.get("data") or {}
            msg = d.get("message") or {}
            user = d.get("user") or (d.get("member") or {}).get("user") or {}
            custom_id = data.get("custom_id", "")
            interaction = DiscordInteraction(
                interaction_id=str(d.get("id", "")),
                interaction_token=d.get("token", ""),
                channel_id=str(d.get("channel_id", "")),
                user_id=str(user.get("id", "")),
                message_id=str(msg.get("id", "")),
                custom_id=custom_id,
                label=_find_button_label(msg.get("components") or [], custom_id),
                guild_id=str(d.get("guild_id", "") or ""),
                username=user.get("username", ""),
                kind=int(kind),
                command_name=str(data.get("name", "") or "").lower(),
                options=_command_options(data.get("options")),
            )
            task = asyncio.create_task(self._invoke_interaction(interaction))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _invoke_message(self, inbound: DiscordInbound) -> None:
        if self._on_message is None:
            return
        try:
            await self._on_message(inbound)
        except Exception:
            logger.exception("Discord on_message handler raised for user=%s", inbound.user_id)

    async def _invoke_interaction(self, interaction: DiscordInteraction) -> None:
        if self._on_interaction:
            try:
                await self._on_interaction(interaction)
            except Exception:
                logger.exception("Discord on_interaction handler raised")

    # ── HTTP transport ──

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand
        (double-checked under a lock — REST calls run concurrently with the
        gateway loop)."""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    # -- Rate-limit accounting ---------------------------------------------

    def _hold_key(self, route: str) -> str:
        """State key for ``route``: Discord's bucket id once a response has
        named one, so routes SHARING a bucket pre-empt each other, and the
        route itself until then."""
        return self._route_buckets.get(route) or route

    def _set_hold(self, key: str, delay: float) -> None:
        """Hold ``key`` for at least ``delay`` seconds, never shortening an
        existing hold: two responses can disagree, and the later deadline is
        the one that keeps us out of a 429."""
        deadline = time.monotonic() + delay
        _remember(self._holds, key, max(self._holds.get(key, 0.0), deadline))

    def _note_headers(self, route: str, headers: Mapping[str, str]) -> None:
        """Learn the route's bucket, and pre-empt when the bucket is spent.

        A response saying ``Remaining: 0`` is Discord telling us the next call
        on this bucket will be a 429. Recording the reset here is what turns
        that into a wait instead of a refusal.
        """
        bucket = str(headers.get(_HDR_BUCKET, "") or "")
        if bucket:
            _remember(self._route_buckets, route, bucket)
        remaining = _header_int(headers, _HDR_REMAINING)
        reset_after = _header_float(headers, _HDR_RESET_AFTER)
        if remaining is None or remaining > 0 or reset_after is None or reset_after <= 0:
            return
        self._set_hold(bucket or route, min(reset_after, _MAX_PREEMPT_SECS))

    async def _await_capacity(self, route: str, *, exempt: bool) -> None:
        """Wait out a known-spent bucket, and any global hold, before sending.

        ``exempt`` routes skip the GLOBAL hold only: Discord does not charge
        interaction callbacks to the app's 50 requests/second allowance, so
        holding them there would blow their ~3 second deadline for a limit they
        never consumed. Their own bucket still applies.
        """
        deadlines = [self._holds.get(self._hold_key(route), 0.0)]
        if not exempt:
            deadlines.append(self._global_ready_at)
        wait = max(deadlines) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    def _note_rate_limit(self, route: str, data: Any, headers: Mapping[str, str]) -> float:
        """Record a 429 and return the back-off this attempt must serve.

        The body's ``retry_after`` is fractional seconds while the
        ``Retry-After`` header is rounded to whole seconds, so the body wins
        when both are present. A GLOBAL 429 is the app's entire allowance, not
        this route's, so it holds EVERY non-exempt route: letting the other
        routes keep firing is how one rate limit becomes an IP block.
        """
        body = data if isinstance(data, dict) else {}
        retry_after = _coerce_float(body.get("retry_after"))
        if retry_after is None:
            retry_after = _header_float(headers, _HDR_RETRY_AFTER)
        if retry_after is None:
            retry_after = _DEFAULT_RETRY_AFTER_SECS
        if bool(body.get("global")) or headers.get(_HDR_SCOPE, "") == _SCOPE_GLOBAL:
            hold = min(max(retry_after, _MIN_HOLD_SECS), _MAX_GLOBAL_HOLD_SECS)
            self._global_ready_at = max(self._global_ready_at, time.monotonic() + hold)
            logger.warning("Discord REST: GLOBAL rate limit, holding every route for %.2fs", hold)
            return hold
        delay = min(max(retry_after, _MIN_HOLD_SECS), _MAX_RETRY_AFTER_SECS)
        self._set_hold(self._hold_key(route), delay)
        return delay

    def _breaker_allows(self) -> bool:
        """False while the invalid-request breaker is OPEN.

        Closes lazily when the cool-off has elapsed, clearing the window so the
        next runaway sequence starts from a clean slate.
        """
        if not self._breaker_until:
            return True
        if time.monotonic() < self._breaker_until:
            return False
        self._breaker_until = 0.0
        self._invalid_hits.clear()
        logger.info("Discord REST: invalid-request breaker closed, resuming requests")
        return True

    def _note_invalid(self, status: int, headers: Mapping[str, str]) -> None:
        """Count a response that spent Discord's invalid-request budget, and
        trip the breaker before that budget can run out.

        The breaker fails toward NOT sending on purpose: a turn that goes
        undelivered costs one conversation, while the IP block it is avoiding
        costs every channel on the host for ten minutes.
        """
        if status not in _INVALID_STATUSES:
            return
        if status == 429 and headers.get(_HDR_SCOPE, "") == _SCOPE_SHARED:
            # A shared-scope 429 is another app's traffic on a limit we merely
            # share, and Discord excludes it from the ban count. Counting it
            # would stop this channel sending over something it did not cause.
            return
        now = time.monotonic()
        cutoff = now - _INVALID_WINDOW_SECS
        while self._invalid_hits and self._invalid_hits[0] < cutoff:
            self._invalid_hits.popleft()
        self._invalid_hits.append(now)
        if self._breaker_until or len(self._invalid_hits) < _INVALID_LIMIT:
            return
        self._breaker_until = now + _BREAKER_COOLOFF_SECS
        logger.warning(
            "Discord REST: %d invalid responses (401/403/429) within %.0fs: pausing all "
            "outbound requests for %.0fs to stay clear of Discord's %d-per-10min IP block. "
            "Check the bot token, its channel permissions, and any send loop.",
            len(self._invalid_hits),
            _INVALID_WINDOW_SECS,
            _BREAKER_COOLOFF_SECS,
            _DISCORD_INVALID_BAN_LIMIT,
        )

    # -- Request ladder -----------------------------------------------------

    async def api_json(
        self, method: str, path: str, payload: dict | list | None, *, timeout: int = 30
    ) -> DiscordApiResult:
        """Call a REST endpoint with a JSON body, reporting the outcome.

        The body may be a LIST: the application-command bulk-overwrite endpoint
        takes a top-level JSON array, not an object.
        """
        return await self._api_request(
            method, path, timeout=timeout, build=lambda: {"json": payload}
        )

    async def api_files(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        files: Sequence[OutboundFile],
        *,
        timeout: int = 60,
    ) -> DiscordApiResult:
        """Send multipart, rebuilding the single-use form for every attempt."""
        return await self._api_request(
            method,
            path,
            timeout=timeout,
            build=lambda: {"data": _build_upload_form(payload, files)},
        )

    async def _api(
        self, method: str, path: str, payload: dict | list | None, timeout: int = 30
    ) -> Any:
        """:meth:`api_json` reduced to its body: the parsed JSON ({} for a 204)
        or None on any failure. The verbs that can only answer "did it land"
        use this; anything that must act on WHY calls ``api_json``."""
        return (await self.api_json(method, path, payload, timeout=timeout)).data

    async def _api_multipart(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        files: Sequence[OutboundFile],
        timeout: int = 60,
    ) -> Any:
        """:meth:`api_files` reduced to its body, mirroring :meth:`_api`."""
        return (await self.api_files(method, path, payload, files, timeout=timeout)).data

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        timeout: int,
        build: Callable[[], dict[str, Any]],
    ) -> DiscordApiResult:
        """Shared REST ladder. ``build`` supplies the per-ATTEMPT body kwargs.

        One pass: wait for capacity, send, account for what came back, then
        either answer or retry within the failure class's own budget. Every
        wait is an ``await`` so the event loop keeps running the turn's other
        work while a bucket refills.
        """
        session = await self._ensure_session()
        url = _API_BASE + path
        auth = {"Authorization": f"Bot {self._token}"}
        route = _route_key(method, path)
        exempt = _is_global_limit_exempt(path)
        rate_limited = 0
        transient = 0
        # The attempt that follows a 429 has already served that back-off, so it
        # sends immediately; re-checking the hold it just waited out would
        # charge it twice.
        served_backoff = False
        while True:
            if not self._breaker_allows():
                return _failed(DISCORD_BLOCKED, detail="invalid-request breaker open")
            if not served_backoff:
                await self._await_capacity(route, exempt=exempt)
            served_backoff = False
            try:
                async with session.request(
                    method,
                    url,
                    headers=auth,
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    **build(),
                ) as resp:
                    headers = _rate_limit_headers(resp)
                    self._note_headers(route, headers)
                    if resp.status == 204:
                        return DiscordApiResult(DISCORD_OK, data={}, status=204)
                    # A proxy or Discord error page can return non-JSON; a
                    # decode failure must degrade to an error result, never
                    # propagate into the renderer/dispatcher.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if 200 <= resp.status < 300:
                        # Malformed 2xx body: succeed with an empty result so
                        # callers treat the operation as done (it was).
                        return DiscordApiResult(
                            DISCORD_OK,
                            data=data if data is not None else {},
                            status=resp.status,
                        )
                    self._note_invalid(resp.status, headers)
                    body = data if isinstance(data, dict) else {}
                    if resp.status == 429:
                        delay = self._note_rate_limit(route, data, headers)
                        rate_limited += 1
                        if rate_limited > _RATE_LIMIT_RETRIES:
                            logger.warning(
                                "Discord API %s rate limited again (retry_after %.2fs), "
                                "giving up; the route is held for the next caller",
                                route,
                                delay,
                            )
                            return _failed(DISCORD_TRANSIENT, status=429, detail="rate limited")
                        await asyncio.sleep(delay)
                        served_backoff = True
                        continue
                    if resp.status >= 500:
                        transient += 1
                        if transient > _TRANSIENT_RETRIES:
                            logger.warning(
                                "Discord API %s failed: status=%s (retries exhausted)",
                                route,
                                resp.status,
                            )
                            return _failed(
                                DISCORD_TRANSIENT,
                                status=resp.status,
                                detail="server error",
                            )
                        backoff = _TRANSIENT_BACKOFF_SECS * transient
                        logger.warning(
                            "Discord API %s failed: status=%s, retrying in %.1fs",
                            route,
                            resp.status,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    # Anything else below 500 will fail identically forever: a
                    # retry cannot grant a permission or undelete a message, it
                    # only spends invalid-request budget.
                    logger.warning(
                        "Discord API %s failed: status=%s code=%s message=%s",
                        route,
                        resp.status,
                        body.get("code"),
                        body.get("message"),
                    )
                    code = _coerce_float(body.get("code"))
                    return _failed(
                        DISCORD_PERMANENT,
                        status=resp.status,
                        code=int(code) if code is not None else 0,
                        detail=str(body.get("message", "") or "")[:200],
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                transient += 1
                # Log only the exception type — never anything token-adjacent.
                logger.warning(
                    "Discord API %s transport error: %s",
                    route,
                    type(exc).__name__,
                )
                if transient > _TRANSIENT_RETRIES:
                    return _failed(DISCORD_TRANSIENT, detail=type(exc).__name__)
                await asyncio.sleep(_TRANSIENT_BACKOFF_SECS * transient)


def _safe_description(alt: str) -> str:
    """Redact the unescaped description before truncation can split a secret."""
    out, _ = redact_for_display(
        alt, lambda s: redact_credentials(redact_exfiltration_urls(s)[0])[0]
    )
    return out[:1024]


def upload_filename(file: OutboundFile, index: int) -> str:
    """Derive and re-scan a header-safe filename from an untrusted path."""
    ext = _MIME_EXT.get(file.mime, "bin")
    stem = _UNSAFE_FILENAME_RE.sub("_", Path(file.path).name).lstrip(".")
    stem = stem[: -len(Path(stem).suffix)] if Path(stem).suffix else stem
    stem = stem.strip("._")[:64]
    name = f"{stem or f'image_{index}'}.{ext}"
    redacted, _ = redact_exfiltration_urls(name)
    redacted, _ = redact_credentials(redacted)
    return name if redacted == name else f"image_{index}.{ext}"


def _build_upload_form(payload: dict[str, Any], files: Sequence[OutboundFile]) -> aiohttp.FormData:
    """Build matching attachment descriptors and indexed file parts."""
    form = aiohttp.FormData()
    descriptors: list[dict[str, Any]] = []
    names: list[str] = []
    for index, file in enumerate(files):
        name = upload_filename(file, index)
        names.append(name)
        descriptor: dict[str, Any] = {"id": index, "filename": name}
        if file.alt:
            descriptor["description"] = _safe_description(file.alt)
        descriptors.append(descriptor)
    # Put descriptors before the file parts they describe.
    form.add_field(
        "payload_json",
        json.dumps({**payload, "attachments": descriptors}),
        content_type="application/json",
    )
    for index, file in enumerate(files):
        form.add_field(
            f"files[{index}]",
            file.data,
            filename=names[index],
            content_type=file.mime,
        )
    return form


def _command_options(raw: Any) -> dict[str, str]:
    """Flatten an application command's top-level options to ``{name: value}``.

    Only the top level is read: every command registered here is flat, so a
    nested SUB_COMMAND row (option type 1 or 2, which carries its own
    ``options`` list instead of a ``value``) has no name to bind and is skipped
    rather than recorded with an empty value that a handler would then treat as
    "the user passed nothing".
    """
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for opt in raw:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name", "") or "")
        if not name or "value" not in opt:
            continue
        out[name] = str(opt["value"])
    return out


def _find_button_label(components: list[dict], custom_id: str) -> str:
    """Recover the pressed button's display text from the message's components
    (custom_id carries only our compact routing data)."""
    for row in components:
        for btn in row.get("components", []):
            if btn.get("custom_id") == custom_id:
                return btn.get("label", "")
    return ""


def _resolve_proxy() -> str | None:
    """Resolve outbound proxy from environment."""
    for var in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        val = os.environ.get(var)
        if val:
            return val
    return None
