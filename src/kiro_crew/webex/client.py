"""Webex Messaging client transport layer.

Inbound: Webex has no long-polling API, and webhooks require a public URL.
Instead we register a *device* with the Webex Device Management service
(WDM) to obtain a per-device WebSocket URL, connect, authorize with the
bot token, and receive ``conversation.activity`` events in real time --
the same mechanism the official ``webex-bot`` SDK uses. Activity events
carry raw UUIDs; the public-API message id is the base64 "Hydra" encoding
of ``ciscospark://us/MESSAGE/{uuid}``. The event payload is only a signal:
the actual message (decrypted text, sender, room type) is fetched via the
documented ``GET /v1/messages/{id}`` REST call.

Outbound: plain REST -- ``POST /v1/messages`` to send (roomId or
toPersonEmail), ``PUT /v1/messages/{id}`` to edit (Webex caps a message at
10 edits -- callers must budget), ``DELETE /v1/messages/{id}`` to remove.

No Webex SDK dependency -- pure aiohttp (REST + WebSocket). This keeps the
module lightweight, OSS-clean, and easy to audit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, cast
from urllib.parse import urlparse

import aiohttp

from kiro_crew.messaging.split import truncate_utf8 as _truncate_utf8

logger = logging.getLogger(__name__)

# Webex REST API base.
_API_BASE = "https://webexapis.com/v1"
# Webex Device Management (device registration -> WebSocket URL). A REGIONAL
# host: the org's own Device Manager is discovered per token from the U2C
# service catalog below, and this is only the fallback when discovery fails.
_DEVICE_BASE = "https://wdm-a.wbx2.com/wdm/api/v1"
# User-to-Capabilities: maps a token to the service hosts for ITS org's cluster.
_U2C_CATALOG = "https://u2c.wbx2.com/u2c/api/v1/catalog?format=hostmap"
# Default Hydra cluster, used only when an activity carries no usable target URL
# to derive the real one from.
_DEFAULT_CLUSTER = "us"

# Webex caps a message's text/markdown at 7439 BYTES; stay comfortably under.
# The cap is enforced in UTF-8 bytes (not characters) — see ``truncate_utf8``.
WEBEX_MAX_TEXT = 7000


def truncate_utf8(text: str, max_bytes: int = WEBEX_MAX_TEXT) -> str:
    """Byte-exact truncation, defaulted to Webex's own cap.

    The implementation is the shared one in ``messaging.split``; this wrapper
    exists only to keep ``WEBEX_MAX_TEXT`` as the default for this module's three
    call sites. It loses the tail, so it is the last-resort guard for a SINGLE
    send — multi-message content is split losslessly first, by the shared
    ``messaging.split.chunk_utf8_bytes`` at the renderer's answer path.
    """
    return _truncate_utf8(text, max_bytes)


# A WS connection must live at least this long to count as "healthy" and reset
# the reconnect backoff. A connect->immediate-close (bad token) stays on the
# backoff curve so it cannot hot-loop with zero delay. Mirrors WeComClient.
_MIN_HEALTHY_CONN_SECS = 5.0

# How many recently-dispatched message ids to remember for deduplication.
#
# The device WebSocket redelivers an activity that was not acknowledged, and a
# reconnect can replay one the previous connection already handed off. Without a
# dedup memory the same user message starts a SECOND turn, or — worse — arrives
# while the first is still running and gets folded in as a mid-turn steer, so the
# agent is steered by an echo of the instruction it is already following.
# Bounded FIFO: only the recent window can plausibly be redelivered, and an
# unbounded set on a long-lived gateway is a leak.
_DEDUP_WINDOW = 512

# Activity verbs this client accepts, as a POSITIVE set rather than a widened
# negation. A negation ("anything that is not X") hands every verb Cisco adds
# later the same treatment as a user message, which is the permissive direction.
#
# * ``post``      — an ordinary message.
# * ``share``     — a message carrying one or more files. Arrives INSTEAD of
#                   ``post``, so filtering to ``post`` alone drops a file
#                   message whole, caption text included.
# * ``update``    — fires when the anti-malware scan finishes. In practice this
#                   is a DISCONNECT-RECOVERY path: a scan that completes while the
#                   socket is up follows a ``share`` that already dispatched and
#                   dedup-marked the message id, so the update is dropped as a
#                   redelivery. It earns its keep when no ``share`` was seen (the
#                   socket was down), which is also why an update whose files have
#                   not cleared is NOT dedup-marked — the next one is the signal
#                   this one was waiting for.
# * ``cardAction``— an Adaptive Card submit. Its payload is metadata only, so
#                   the inputs come from a follow-up REST fetch.
VERB_POST = "post"
VERB_SHARE = "share"
VERB_UPDATE = "update"
VERB_CARD_ACTION = "cardAction"
_MESSAGE_VERBS = frozenset((VERB_POST, VERB_SHARE, VERB_UPDATE))
_ACCEPTED_VERBS = _MESSAGE_VERBS | {VERB_CARD_ACTION}

# Quarantine states on a WS activity that are not a reason to keep waiting before
# dispatching. This is a SCHEDULING hint, not the security gate: the authoritative
# refusal is the content endpoint's 423 / 410 / 428 ladder in
# ``download_content``, which runs on every file regardless of what any activity
# claimed and is the only thing standing between the agent and a quarantined file.
#
# ``""`` is included on purpose. Webex populates ``malwareQuarantineState`` when
# there is a verdict to report, so an activity that omits it is not a "not safe"
# signal — treating it as one would park a file that is fine forever, waiting for
# a field that is never coming, while the download ladder would have refused it
# anyway if it were not.
_SAFE_QUARANTINE = frozenset(("safe", ""))

# How many resolved id -> attribute mappings to remember for authorizing card
# presses (person -> email, room -> type). One bound for both: they are populated
# by the same envelope and a room's membership is small either way.
_PERSON_CACHE_MAX = 256

# How long to keep re-requesting a file Webex is still scanning, in total. A
# BUDGET rather than an attempt count: the server sets the pacing through
# Retry-After, so N attempts is an unpredictable amount of wall clock — four
# attempts at the 1s floor gives a scan 3 seconds, which it routinely outlasts,
# and the file is then lost for the turn. Webex's own second chance (the `update`
# activity) cannot recover it either, because the `share` that started this ingest
# has already dedup-marked the message id.
#
# Bounded because the user is waiting on a reply, and this runs before the turn
# starts: past this the honest answer is "still scanning, re-send shortly".
_SCAN_WAIT_BUDGET_S = 60.0

# Host suffixes a Device Manager URL may name. The bearer token rides device
# registration, so the destination cannot be free-form — and both inputs that can
# name it are untrusted in different ways:
#
#   * ``webex.wdm_base`` is read from ``config.json``, which the agent CAN write
#     (``security.py`` deliberately does not over-block it, so sessions.db and
#     ordinary settings stay usable). A prompt-injected `config set` followed by a
#     restart would otherwise POST the token to an attacker's host.
#   * the U2C catalog's ``serviceLinks.wdm`` is a response body. Anyone able to
#     shape it already holds the token, so this is defence in depth there — but it
#     costs nothing to apply the same rule to both.
#
# Suffix-matched against the registrable host with a leading dot, so
# ``evil-wbx2.com`` and ``wbx2.com.attacker.net`` do not qualify.
_WDM_HOST_SUFFIXES = (".wbx2.com", ".webex.com", ".ciscospark.com")


def _is_webex_host(url: str) -> bool:
    """Whether *url* is an HTTPS URL on a Webex-owned host.

    Fail-closed on anything unparseable: a host that cannot be read is not one
    that can be vouched for.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host.endswith(suffix) for suffix in _WDM_HOST_SUFFIXES)


#: Refusal reason for a scan that outlasted the budget. Reaches the user through
#: ``messaging.attachments``, so it reads as an instruction rather than an error.
_STILL_SCANNING = "still being scanned, re-send shortly"

# Bounds on the server-supplied Retry-After for the SCAN-WAIT ladder (the 423 path
# in ``download_content``, via ``_retry_after``): a missing header must not mean zero
# (a hot loop) and a hostile one must not park the turn. The 429 API back-off in
# ``_api`` is a separate policy with its own narrower bounds, and does not read these.
_RETRY_AFTER_MIN_S = 1.0
_RETRY_AFTER_MAX_S = 15.0
# Chunk size for streaming a download to disk, so a 100 MB file never lands in
# memory whole.
_DOWNLOAD_CHUNK = 64 * 1024

_DEVICE_PAYLOAD = {
    "name": "kirocrew",
    "deviceName": "kirocrew-gateway",
    "deviceType": "DESKTOP",
    "model": "kirocrew",
    "localizedModel": "kirocrew",
    "systemName": "kirocrew",
    "systemVersion": "1.0.0",
}


@dataclass
class WebexInbound:
    """Normalized inbound message from a Webex conversation.activity event."""

    person_email: str
    room_id: str
    text: str
    person_id: str = ""
    room_type: str = ""  # "direct" | "group"
    message_id: str = ""
    #: Thread root this message belongs to, if any. Webex threads are FLAT: a
    #: reply's parent resolves to the root, so this is the id to reply under.
    parent_id: str = ""
    #: Person ids the message @-mentioned. In a group space a bot only ever sees
    #: messages that mention it, and the platform does NOT strip its own name.
    mentioned_people: tuple[str, ...] = ()
    #: Opaque ``/v1/contents/{id}`` URLs for attached files, in order.
    file_urls: tuple[str, ...] = ()
    #: Card-action inputs, when this envelope came from an Adaptive Card submit
    #: rather than a typed message. Empty for an ordinary message.
    card_inputs: Mapping[str, Any] | None = None


def hydra_id(raw_id: str, resource_type: str = "MESSAGE", cluster: str = _DEFAULT_CLUSTER) -> str:
    """Base64-encode a raw UUID into the public-API ("Hydra") id format.

    WS activity events carry raw UUIDs; the REST API expects
    ``base64("ciscospark://{cluster}/{TYPE}/{uuid}")`` without padding.

    The CLUSTER is part of the id, and getting it wrong is a silent failure: the
    REST fetch simply returns nothing, so an org that does not live in ``us``
    sees every inbound message vanish behind a green badge. Callers pass the
    cluster read off the activity's own target URL (:func:`cluster_of`) and fall
    back to this default only when the frame carries none.
    """
    if not raw_id:
        return ""
    prefix = f"ciscospark://{cluster or _DEFAULT_CLUSTER}/{resource_type}/{raw_id}"
    return base64.b64encode(prefix.encode()).decode().rstrip("=")


class _HydrationFailed(Exception):
    """A dispatched activity could not be turned into a message.

    Distinguishes a RETRYABLE fetch failure from a failure of the turn it feeds:
    the first leaves the activity unacknowledged and its dedup mark released, so
    the service's own redelivery is a real second chance; the second must not be
    retried, because the message was already handed to the dispatcher.
    """


def _files_are_safe(activity: dict) -> bool:
    """Whether every file on an ``update`` activity has cleared malware scanning.

    Webex fires ``update`` when a scan finishes, and the per-file
    ``malwareQuarantineState`` is the verdict. Anything other than a safe state —
    still scanning, infected, unscannable — must not reach the agent, so this
    answers False rather than treating an unknown state as clean. An activity
    with no file list at all is not a file update and answers False too.
    """
    items = (((activity.get("object") or {}).get("files") or {}).get("items")) or []
    if not items:
        return False
    return all(
        str((item or {}).get("malwareQuarantineState", "")).lower() in _SAFE_QUARANTINE
        for item in items
    )


def cluster_of(hydra: str) -> str:
    """The cluster segment of a Hydra id, or ``""`` if it is not one.

    Lets a derived id seed the cluster for every id built from the same frame,
    so one geo-correct value propagates instead of being re-guessed.
    """
    if not hydra:
        return ""
    try:
        decoded = base64.b64decode(hydra + "=" * (-len(hydra) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return ""
    if not decoded.startswith("ciscospark://"):
        return ""
    rest = decoded[len("ciscospark://") :]
    return rest.split("/", 1)[0] if "/" in rest else ""


class WebexClient:
    """Webex Messaging client with device-WebSocket inbound and auto-reconnect.

    Registers a device with WDM to obtain a WebSocket URL, connects, and
    dispatches inbound messages to the on_message handler. Outbound sends
    ride the documented REST API.
    """

    def __init__(
        self,
        *,
        token: str,
        on_message: Callable[[WebexInbound], Awaitable[None]] | None = None,
        device_base: str = "",
        api_base: str = _API_BASE,
        proxy: str | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        # An explicit host is a PIN, kept separately from the host in use: a
        # restricted network may not reach the U2C catalog at all, and the pin has
        # to survive a reconnect — discovery writes to ``_device_base``, so a pin
        # stored only there is destroyed by the first successful discovery and the
        # config key silently becomes a no-op after one connect.
        # A pin that is not a Webex host is DROPPED, loudly, rather than honoured:
        # the alternative is sending this token wherever config.json says.
        pin = device_base.rstrip("/")
        if pin and not _is_webex_host(pin):
            logger.error(
                "Webex: ignoring webex.wdm_base — %r is not an https Webex host, and "
                "device registration would send the bot token there. Falling back to "
                "discovery.",
                pin,
            )
            pin = ""
        self._device_pin = pin
        self._device_base = self._device_pin or _DEVICE_BASE
        self._api_base = api_base.rstrip("/")
        self._proxy = proxy or _resolve_proxy()
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        # Bot identity (fetched once on connect) for self-message filtering.
        self.bot_email: str = ""
        self.bot_person_id: str = ""
        # Display name, for stripping the bot's own @mention out of a group
        # message — Webex leaves it in the text.
        self.bot_name: str = ""
        # Set once the WS is connected + authorized (cleared while
        # disconnected/reconnecting). ``wait_ready`` gates "connected" status.
        self.ready: asyncio.Event = asyncio.Event()
        # Short reason from the most recent connection failure; empty when
        # connected. Read by the status callback path.
        self.last_error: str = ""
        # Optional observer called with (connected: bool, error: str) on
        # connect and on disconnect — lets the gateway keep the dashboard
        # status badge truthful after boot. Mirrors DiscordClient.
        self.on_state_change: Callable[[bool, str], None] | None = None
        # Live turn tasks -- prevent GC of in-flight handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # Recently dispatched message ids, oldest first (see _DEDUP_WINDOW).
        self._seen: dict[str, None] = {}
        # Hydra cluster for this org, learned from the wire on first activity.
        self._cluster = _DEFAULT_CLUSTER
        # person id -> email, for authorizing an Adaptive Card press.
        self._person_emails: dict[str, str] = {}
        # room id -> room type, for the same reason: an attachment-action record
        # carries no roomType and the room gate is a type decision.
        self._room_types: dict[str, str] = {}
        # The live socket, so a processed activity can be acknowledged. Cleared
        # on disconnect: an ack written to a dead socket is a lost write, not an
        # error worth surfacing.
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    # ── Lifecycle ──

    async def start(self) -> None:
        """Launch the background connect/serve loop."""
        self._closed = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        """Gracefully shut down."""
        self._closed = True
        try:
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._task = None
        finally:
            if self._handler_tasks:
                for t in list(self._handler_tasks):
                    t.cancel()
                await asyncio.gather(*self._handler_tasks, return_exceptions=True)
                self._handler_tasks.clear()
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

    def set_message_handler(self, on_message: Callable[[WebexInbound], Awaitable[None]]) -> None:
        """Set/replace the inbound-message handler after construction.

        Lets the gateway wire ``transport.receive`` in once the transport
        (which needs the client) has been built, avoiding a construction
        cycle. Mirrors TelegramClient/WeComClient.
        """
        self._on_message = on_message

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the WS to be connected + authorized. Returns False on
        timeout (bad token, unreachable endpoint). Mirrors DiscordClient."""
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
                logger.debug("Webex on_state_change observer raised", exc_info=True)

    # ── Outbound REST API ──

    async def send_message(
        self,
        conversation_id: str,
        markdown: str,
        *,
        parent_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Send a new message; return its message id on success.

        ``conversation_id`` is a Webex roomId, or an email address (contains
        ``@``) to open/reuse the 1:1 space with that person -- the shape
        ``resolve_conversation`` hands proactive senders.
        """
        payload: dict[str, Any] = {"markdown": truncate_utf8(markdown or "…")}
        if "@" in conversation_id:
            payload["toPersonEmail"] = conversation_id
        else:
            payload["roomId"] = conversation_id
        if parent_id:
            payload["parentId"] = parent_id
        if attachments:
            # Webex accepts at most ONE card per message; sending more is a 400.
            payload["attachments"] = list(attachments)[:1]
        result = await self._api("POST", "/messages", payload)
        return result.get("id") if isinstance(result, dict) else None

    async def send_file(
        self,
        conversation_id: str,
        markdown: str,
        *,
        data: bytes,
        filename: str,
        mimetype: str,
        parent_id: str | None = None,
    ) -> str | None:
        """Post a message carrying ONE file, via multipart.

        Takes BYTES, never a path, and that is a security contract rather than a
        convenience: ``messaging.outbound_files`` validated the denylist, the
        symlink refusal, the descriptor-pinned read and the byte signature
        against the inode those bytes came from. Re-opening the path here would
        resolve the name a second time, and anything able to write that directory
        — another turn, a subagent, a cron — could have swapped the file in
        between, so the upload would carry something no gate ever saw.

        Webex takes at most one file per message ("accepts multiple values to
        allow for future expansion, but currently only one"), so a run of files is
        a run of messages.

        Deliberately not routed through :meth:`_api`: that path is JSON, and an
        upload has to be ``multipart/form-data``.
        """
        session = await self._ensure_session()
        url = f"{self._api_base}/messages"
        form = aiohttp.FormData()
        if "@" in conversation_id:
            form.add_field("toPersonEmail", conversation_id)
        else:
            form.add_field("roomId", conversation_id)
        form.add_field("markdown", truncate_utf8(markdown or " "))
        if parent_id:
            form.add_field("parentId", parent_id)
        form.add_field("files", data, filename=filename, content_type=mimetype)
        try:
            async with session.post(
                url,
                data=form,
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                if not 200 <= resp.status < 300:
                    # Status only: a response body here is externally derived.
                    logger.warning("Webex file upload failed: http=%s", resp.status)
                    return None
                body = await resp.json(content_type=None)
                return str(body.get("id") or "") or None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Webex file upload error: %s", type(exc).__name__)
            return None

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        """Edit an existing message in-place. Returns True on success.

        Webex allows at most 10 edits per message (further edits 400) --
        callers must budget their edits (see WebexRenderer).
        """
        payload = {"roomId": room_id, "markdown": truncate_utf8(markdown or "…")}
        result = await self._api("PUT", f"/messages/{message_id}", payload)
        return result is not None

    async def delete_message(self, message_id: str) -> None:
        """Delete a message (best-effort)."""
        await self._api("DELETE", f"/messages/{message_id}", None)

    async def list_messages(self, query: str) -> list[dict]:
        """Messages for a pre-built ``/messages`` query string, newest first.

        The caller owns the query because the useful filters differ per call
        (``roomId``, ``parentId``, ``max``), and a bot listing a GROUP room must
        additionally pass ``mentionedPeople=me`` — Webex requires it there. An
        error yields ``[]``: history is supplementary and must not fail a turn.
        """
        result = await self._api("GET", f"/messages{query}", None)
        items = result.get("items") if isinstance(result, dict) else None
        return [i for i in (items or []) if isinstance(i, dict)]

    async def head_content(self, url: str) -> tuple[str, str, int]:
        """``(filename, mimetype, size)`` for a content URL, without downloading.

        A HEAD returns Content-Disposition / Type / Length, so an oversized or
        unwanted file can be refused before any bytes move. Returns empty/zero
        values on failure; the caller decides what to do with an unknown file.
        """
        session = await self._ensure_session()
        if not self._is_content_url(url):
            return "", "", 0
        try:
            async with session.head(
                url,
                headers=self._headers(),
                proxy=self._proxy,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if not 200 <= resp.status < 300:
                    return "", "", 0
                disposition = resp.headers.get("Content-Disposition", "")
                name = ""
                if "filename=" in disposition:
                    name = disposition.split("filename=", 1)[1].strip().strip('"; ')
                mimetype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                try:
                    size = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                return name, mimetype, size
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Webex content HEAD error: %s", type(exc).__name__)
            return "", "", 0

    async def download_content(self, url: str, dest: str) -> None:
        """Download a ``/v1/contents/{id}`` file to *dest*.

        Webex puts a real anti-malware state machine in front of inbound files,
        and every state has to be handled or the agent is handed the wrong thing:

        * **423 Locked** — still being scanned. Honour ``Retry-After`` and keep
          retrying within a total time BUDGET; the docs warn the scan may still
          not be done when it expires.
        * **410 Gone** — scanned and INFECTED. Permanently unavailable, and a
          retry would only be a slower refusal.
        * **428 Precondition Required** — unscannable (an encrypted archive, say).
          Refused rather than re-requested with ``?allow=unscannable``: opting in
          would hand the agent a file Webex declined to vouch for, and that is an
          operator's decision, not a default.

        Raises ``ValueError`` naming only the exception CLASS or the state — never
        the URL, which carries a content id and rides an ``Authorization`` header.
        Redirects are refused for the same reason the Telegram downloader refuses
        them: a redirect could send the bearer token somewhere else.
        """
        session = await self._ensure_session()
        if not self._is_content_url(url):
            raise ValueError("refusing a non-Webex content URL")
        deadline = time.monotonic() + _SCAN_WAIT_BUDGET_S
        while True:
            try:
                async with session.get(
                    url,
                    headers=self._headers(),
                    proxy=self._proxy,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status == 423:
                        delay = _retry_after(resp.headers)
                        # Checked against the budget INCLUDING the sleep, so the
                        # wait never overruns it — a server-set Retry-After can be
                        # up to _RETRY_AFTER_MAX_S.
                        if time.monotonic() + delay >= deadline:
                            raise ValueError(_STILL_SCANNING)
                        await asyncio.sleep(delay)
                        continue
                    if resp.status == 410:
                        raise ValueError("quarantined as malware")
                    if resp.status == 428:
                        raise ValueError("could not be scanned")
                    if resp.status in (301, 302, 303, 307, 308):
                        raise ValueError("refusing a redirected content URL")
                    if not 200 <= resp.status < 300:
                        raise ValueError(f"content download failed: http={resp.status}")
                    await _write_stream(resp, dest)
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise ValueError(f"content download error: {type(exc).__name__}") from None

    def _is_content_url(self, url: str) -> bool:
        """Whether *url* is a Webex content URL under this client's API base.

        The bearer token rides these requests, so the destination is checked
        against the configured base instead of trusted from the message body — a
        message field is attacker-influenceable, and the token must not be sent
        anywhere it did not come from.
        """
        return bool(url) and url.startswith(f"{self._api_base}/contents/")

    async def fetch_message(self, message_id: str) -> dict | None:
        """Fetch a message's full record (decrypted text, sender, room)."""
        result = await self._api("GET", f"/messages/{message_id}", None)
        return result if isinstance(result, dict) else None

    # ── Identity ──

    async def _fetch_me(self) -> None:
        """Resolve the bot's own identity once (self-message filtering)."""
        me = await self._api("GET", "/people/me", None)
        if isinstance(me, dict):
            emails = me.get("emails") or []
            self.bot_email = (emails[0] if emails else "").lower()
            self.bot_person_id = me.get("id", "")
            self.bot_name = str(me.get("displayName") or me.get("nickName") or "")

    # ── WebSocket serve loop ──

    async def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff (mirrors WeComClient)."""
        attempt = 0
        while not self._closed:
            started = time.monotonic()
            reason: object | None = None
            try:
                await self._connect_and_serve()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                reason = type(exc).__name__
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Webex WS unexpected error")
                reason = "unexpected error"

            self.ready.clear()
            if self._closed:
                break
            if reason is None:
                # Clean server close: only reset backoff if the connection
                # actually lived a while, else it hot-loops on a bad token.
                if time.monotonic() - started >= _MIN_HEALTHY_CONN_SECS:
                    attempt = 0
                    continue
                reason = "server closed connection immediately"

            attempt += 1
            delay = min(1.0 * (2 ** (attempt - 1)), 60.0)
            self.last_error = str(reason)[:120]
            self._notify_state(False, self.last_error)
            logger.warning("Webex WS disconnected (%s), reconnect in %.1fs", reason, delay)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self) -> None:
        """Single connection lifecycle: register device, connect, authorize, serve."""
        session = await self._ensure_session()
        if not self.bot_email:
            await self._fetch_me()

        ws_url = await self._get_websocket_url()
        if not ws_url:
            raise aiohttp.ClientError("device registration returned no webSocketUrl")

        # heartbeat= keeps protocol-level ping/pong flowing so a dead
        # connection surfaces as an error instead of hanging silently.
        async with session.ws_connect(ws_url, proxy=self._proxy, heartbeat=20) as ws:
            auth_frame = {
                "id": str(uuid.uuid4()),
                "type": "authorization",
                "data": {"token": f"Bearer {self._token}"},
            }
            await ws.send_json(auth_frame)
            logger.info("Webex WS connected and authorized")
            # cast, and deliberately to the UNPARAMETERIZED type: aiohttp makes
            # ClientWebSocketResponse generic over its autoping flag, so
            # ``ws_connect`` yields ``[bool]`` while the attribute's bare
            # annotation resolves to the default ``[Literal[True]]``. Which
            # parameter it carries is irrelevant here — the socket is only ever
            # used to send an ack frame — and the versions differ between the two
            # Python lanes, so subscripting the annotation would fix one and break
            # the other.
            self._ws = cast("aiohttp.ClientWebSocketResponse", ws)
            self.ready.set()
            self.last_error = ""
            self._notify_state(True, "")
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            self._handle_frame(json.loads(msg.data))
                        except json.JSONDecodeError:
                            logger.warning("Webex WS: unparseable frame (%d bytes)", len(msg.data))
                        except Exception:
                            logger.exception("Webex WS: frame handler error; dropping frame")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                self._ws = None

    async def _discover_device_base(self) -> str:
        """The org's own Device Manager host, from the U2C service catalog.

        The WDM host is REGIONAL. Registering against a hardcoded one works for
        a US-resident org and silently fails for everyone else, so the host is
        discovered per token. Falls back to the documented default rather than
        raising: a discovery outage should degrade to the US host, not take the
        channel down.

        Only reached when no host was pinned — a pin means the operator's network
        reaches one host and possibly not the catalog.
        """
        session = await self._ensure_session()
        try:
            async with session.get(
                _U2C_CATALOG,
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return self._device_base
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug(
                "Webex U2C discovery failed (%s); using default WDM host", type(exc).__name__
            )
            return self._device_base
        wdm = ""
        if isinstance(data, dict):
            links = data.get("serviceLinks")
            if isinstance(links, dict):
                wdm = str(links.get("wdm") or "")
        if not _is_webex_host(wdm):
            logger.warning("Webex: U2C catalog named a non-Webex WDM host; using the default")
            return self._device_base
        return wdm.rstrip("/")

    async def _get_websocket_url(self) -> str:
        """Register (or reuse) a WDM device and return its WebSocket URL."""
        session = await self._ensure_session()
        if not self._device_pin:
            self._device_base = await self._discover_device_base()
        try:
            async with session.post(
                f"{self._device_base}/devices",
                json=_DEVICE_PAYLOAD,
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json(content_type=None)
                    return data.get("webSocketUrl", "")
            # Device cap reached / already exists: reuse the first device.
            async with session.get(
                f"{self._device_base}/devices",
                headers=self._headers(),
                proxy=self._proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                devices = data.get("devices") or []
                return devices[0].get("webSocketUrl", "") if devices else ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Log only the exception type -- never the URL/response, which
            # could carry token-adjacent material.
            logger.warning("Webex device registration error: %s", type(exc).__name__)
            return ""

    def _handle_frame(self, data: Any) -> None:
        """Filter a WS frame down to new-message activities and dispatch.

        The activity event is treated purely as a *signal* (ids only); the
        message content is fetched via the documented REST API in the
        background task, so the receive loop keeps breathing during long
        turns.
        """
        if not isinstance(data, dict):
            return
        payload = data.get("data")
        if not isinstance(payload, dict):
            return
        if payload.get("eventType") != "conversation.activity":
            return
        activity = payload.get("activity")
        if not isinstance(activity, dict):
            return
        verb = str(activity.get("verb") or "")
        if verb not in _ACCEPTED_VERBS:
            return
        actor = activity.get("actor") or {}
        actor_email = str(actor.get("emailAddress", "")).lower()
        # Ignore the bot's own messages (echo of our sends). A cardAction is
        # exempt: it is the USER acting on a card the bot posted, so the actor is
        # the user and the check is simply not the same question.
        if verb != VERB_CARD_ACTION and actor_email and actor_email == self.bot_email:
            return
        # The ack rule, stated once: acknowledge as soon as this FRAME is settled,
        # and defer only while a fetch might still deserve a retry. An unacked
        # frame is redelivered, so a path that decides not to dispatch has to ack
        # or the service keeps re-offering work we have already refused.
        if verb == VERB_UPDATE and not _files_are_safe(activity):
            # The scan has not cleared yet (or the file was quarantined). Not
            # dedup-marked: a later `update` for the same message is the signal
            # this one was waiting for — and this frame is settled either way.
            logger.debug("Webex WS: update activity with no safe files; waiting")
            self._ack(activity.get("id"))
            return

        resource = "ATTACHMENT_ACTION" if verb == VERB_CARD_ACTION else "MESSAGE"
        object_id = str((activity.get("object") or {}).get("id") or activity.get("id") or "")
        public_id = self._public_id(activity, object_id, resource)
        if not public_id:
            return
        if public_id in self._seen:
            # Settled: this message is already in flight or answered. The ack is
            # per-FRAME while the dedup mark is per-MESSAGE, so a redelivery under
            # a new activity id still has to be acknowledged — otherwise the
            # service redelivers a frame we are deliberately ignoring, forever.
            logger.debug("Webex WS: dropping redelivered activity")
            self._ack(activity.get("id"))
            return
        self._seen[public_id] = None
        while len(self._seen) > _DEDUP_WINDOW:
            self._seen.pop(next(iter(self._seen)), None)
        coro = (
            self._hydrate_card_action(public_id)
            if verb == VERB_CARD_ACTION
            else self._hydrate_and_dispatch(public_id)
        )
        # The dedup mark goes in BEFORE the fetch and the ack goes out AFTER it,
        # and the two together are what make a message neither lost nor doubled.
        #
        # Acking first (the obvious order, since the ack stops redelivery) means a
        # transient REST failure during hydration silently drops the message for
        # good: the activity is acknowledged, so Webex never sends it again, and
        # the dedup mark would refuse it if it did. Marking-but-not-acking is the
        # inverse: the mark absorbs a redelivery that races the fetch, while the
        # missing ack leaves Webex free to try again once the fetch has actually
        # failed and released the id.
        #
        # A failure downstream of hydration — the TURN itself — is deliberately
        # NOT retried. It has already been dispatched, so a redelivery would fold
        # into the running turn as a steer rather than answering anything.
        task = asyncio.create_task(self._ack_after(activity.get("id"), public_id, coro))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _ack_after(self, activity_id: Any, public_id: str, coro: Any) -> None:
        """Run *coro*, then acknowledge — or release *public_id* if it raised.

        Releasing the dedup mark is what turns a transient hydration failure into
        a retry: unacknowledged AND unmarked, the service's own redelivery gets a
        clean second attempt instead of being dropped as a duplicate.
        """
        try:
            await coro
        except _HydrationFailed as exc:
            self._seen.pop(public_id, None)
            logger.warning(
                "Webex WS: hydration failed (%s); leaving the activity "
                "unacknowledged so the service can redeliver it",
                exc,
            )
            return
        except Exception:
            # Anything else already ran past hydration, so the message reached the
            # dispatcher. Acknowledge it: a redelivery would not re-answer it, it
            # would fold into the turn that already failed.
            logger.exception("Webex WS: activity handler raised after hydration")
        self._ack(activity_id)

    def _public_id(self, activity: dict, object_id: str, resource: str) -> str:
        """The public ("Hydra") id for an activity's object.

        The cluster is read from the activity's own ``target.url`` when it has
        one, because that URL is issued by the org's own conversation service and
        therefore names the right cluster. Synthesising ``us`` instead is what
        makes a non-US org silently drop every message: the REST fetch resolves
        nothing and the failure surfaces as no reply at all.
        """
        if not object_id:
            return ""
        return hydra_id(object_id, resource, self._cluster_for(activity))

    def _cluster_for(self, activity: dict) -> str:
        """The Hydra cluster to build ids in for this activity.

        Prefers a cluster observed on the wire (learned once per connection from
        a target id the service issued), then the configured default.
        """
        target = activity.get("target")
        if isinstance(target, dict):
            observed = cluster_of(str(target.get("globalId") or ""))
            if observed:
                self._cluster = observed
        return self._cluster

    def _ack(self, activity_id: Any) -> None:
        """Tell the service this activity is handled, so it is not redelivered.

        Fire-and-forget from the synchronous frame handler: the receive loop must
        keep breathing, and a failed ack costs at most one redelivery, which the
        dedup window above absorbs.
        """
        ws = self._ws
        if ws is None or not activity_id:
            return

        async def _send() -> None:
            try:
                await ws.send_json({"type": "ack", "messageId": str(activity_id)})
            except Exception:
                # A closed or erroring socket is the expected failure here, and it
                # is already surfaced by the reconnect loop. Letting it escape
                # would only log an unretrieved task exception.
                logger.debug("Webex WS: ack send failed", exc_info=True)

        task = asyncio.create_task(_send())
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _hydrate_and_dispatch(self, message_id: str) -> None:
        """Fetch the full message via REST, normalize, and invoke the handler.

        Raises :class:`_HydrationFailed` when the FETCH could not produce a
        message, and only then. The caller reads that as "retry is worth
        something" and leaves the activity unacknowledged; anything the handler
        itself raises is logged and swallowed, because that turn has already been
        dispatched and a redelivery would fold into it as a steer.
        """
        msg = await self._fetch_for_dispatch(message_id)
        if msg is None:
            return
        try:
            person_id = msg.get("personId", "")
            # Belt-and-braces self filter: the WS actor email can be absent.
            if self.bot_person_id and person_id == self.bot_person_id:
                return
            files = tuple(str(u) for u in (msg.get("files") or []) if u)
            mentioned = tuple(str(p) for p in (msg.get("mentionedPeople") or []) if p)
            inbound = WebexInbound(
                person_email=str(msg.get("personEmail", "")).lower(),
                room_id=msg.get("roomId", ""),
                text=msg.get("text", "") or "",
                person_id=person_id,
                room_type=msg.get("roomType", ""),
                message_id=message_id,
                parent_id=str(msg.get("parentId") or ""),
                mentioned_people=mentioned,
                file_urls=files,
            )
            if self._on_message is not None:
                await self._on_message(inbound)
        except Exception:
            logger.exception("Webex on_message handler raised")

    async def _fetch_for_dispatch(self, message_id: str) -> dict | None:
        """The message record, or ``None`` when it is genuinely not ours to answer.

        Separates the two failure kinds the dedup/ack pair depends on: a fetch
        that ERRORED is retryable and raises, while a fetch that succeeded and
        returned nothing is a message that does not exist — retrying that forever
        would be a redelivery loop over a permanent condition.
        """
        try:
            msg = await self.fetch_message(message_id)
        except Exception as exc:
            raise _HydrationFailed(str(type(exc).__name__)) from exc
        if not msg:
            # ``fetch_message`` maps a transport/HTTP error onto None as well, so
            # this is retried too: an empty answer is far more likely a 5xx or a
            # dropped connection than a message id Webex just announced and then
            # denied. The dedup window bounds how often one id can come back.
            raise _HydrationFailed("empty message record")
        return msg

    async def _hydrate_card_action(self, action_id: str) -> None:
        """Fetch an Adaptive Card submit's inputs and dispatch it as an envelope.

        The websocket frame is metadata only — Webex documents that an
        ``attachmentActions`` payload "will not contain any sensitive data" — so
        the inputs need this second REST call. The result is normalised into the
        same :class:`WebexInbound` the typed path produces, with ``card_inputs``
        set, so the dispatcher has one envelope shape to reason about.

        Raises :class:`_HydrationFailed` when the REQUIRED lookups did not
        complete, exactly as the message path does — a press is a decision the
        user already made, and swallowing a transient 5xx here would acknowledge
        the frame and drop that decision for good. The AUTHORIZING lookups (email,
        room type) fail closed on their own, so a press that resolves to neither
        an allow-listed sender nor a permitted room is refused rather than retried.
        """
        try:
            action = await self._api("GET", f"/attachment/actions/{action_id}", None)
            if not isinstance(action, dict):
                # The action record IS the press. Nothing downstream can proceed
                # without it, and a failed `_api` call returns None here, so this
                # is the retryable case rather than a press that does not exist.
                raise _HydrationFailed("attachment action could not be read")
            inputs = action.get("inputs")
            room_id = str(action.get("roomId") or "")
            # An attachment-action record carries no ``parentId``, but the CARD's
            # message does — and the card was posted under whatever thread the turn
            # was answering in. Resolved here so a press envelope is shaped exactly
            # like a message envelope and no reply path needs to special-case it.
            card = await self.fetch_message(str(action.get("messageId") or ""))
            parent_id = str((card or {}).get("parentId") or "")
            inbound = WebexInbound(
                person_email=await self._email_of(str(action.get("personId") or "")),
                room_id=room_id,
                text="",
                person_id=str(action.get("personId") or ""),
                # Resolved, not left blank: the room gate is a TYPE decision, and
                # an envelope with no type would need its own gate branch — which
                # then necessarily disagrees with the one the message that posted
                # this card already passed.
                room_type=await self._room_type_of(room_id),
                message_id=str(action.get("messageId") or ""),
                parent_id=parent_id,
                card_inputs=inputs if isinstance(inputs, dict) else {},
            )
            if self._on_message is not None:
                await self._on_message(inbound)
        except _HydrationFailed:
            # Re-raised BEFORE the broad handler so the caller can leave the
            # activity unacknowledged; a bare `except Exception` below would
            # otherwise absorb it and lose the retry.
            raise
        except Exception:
            logger.exception("Webex card-action handler raised")

    async def _email_of(self, person_id: str) -> str:
        """The email for a person id, for authorizing a card press.

        A card action carries only the person id, but authorization is an EMAIL
        allow-list, so the id has to be resolved before the press can be trusted.
        Cached because a card conversation presses repeatedly and the mapping does
        not change. Returns ``""`` on failure, which authorizes nobody.
        """
        if not person_id:
            return ""
        cached = self._person_emails.get(person_id)
        if cached is not None:
            return cached
        person = await self._api("GET", f"/people/{person_id}", None)
        emails = (person.get("emails") or []) if isinstance(person, dict) else []
        email = str(emails[0]).lower() if emails else ""
        # Bounded: a room's membership is small, but a long-lived gateway seeing
        # many rooms should not accumulate without limit.
        if len(self._person_emails) >= _PERSON_CACHE_MAX:
            self._person_emails.pop(next(iter(self._person_emails)), None)
        self._person_emails[person_id] = email
        return email

    async def _room_type_of(self, room_id: str) -> str:
        """The room type (``direct``/``group``) for a room id.

        An attachment-action record carries no ``roomType``, so a press has to
        resolve one before the room gate can judge it: a direct room is always
        permitted, while a space must be both enabled and named in the allow-list.
        Cached because a card conversation presses repeatedly and a room's type is
        immutable. Returns ``""`` on failure, which the gate reads as an unknown
        type and denies.
        """
        if not room_id:
            return ""
        cached = self._room_types.get(room_id)
        if cached is not None:
            return cached
        room = await self._api("GET", f"/rooms/{room_id}", None)
        room_type = str(room.get("type") or "") if isinstance(room, dict) else ""
        if len(self._room_types) >= _PERSON_CACHE_MAX:
            self._room_types.pop(next(iter(self._room_types)), None)
        # Cached even when empty: a bot removed from the room would otherwise
        # re-request on every press of a card it can no longer be authorized for.
        self._room_types[room_id] = room_type
        return room_type

    # ── HTTP transport ──

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand.

        Guarded by a lock (double-checked) so concurrent callers -- the WS
        serve loop plus per-turn handler tasks -- can't each build a session
        and leak one unclosed. Mirrors TelegramClient.
        """
        if self._closed:
            raise RuntimeError("WebexClient is closed")
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._closed:
                    raise RuntimeError("WebexClient is closed")
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def _api(self, method: str, path: str, payload: dict | None, timeout: int = 30) -> Any:
        """Call a Webex REST endpoint. Returns the parsed JSON body (or ``{}``
        for empty 2xx responses) on success, None on error.

        Honors a single 429 ``Retry-After`` back-off, mirroring the Telegram
        client: a rate-limited status edit that we simply dropped would freeze
        the placeholder, so wait out the (usually short) cool-down once.
        """
        session = await self._ensure_session()
        url = f"{self._api_base}{path}"
        for attempt in range(2):
            try:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    headers=self._headers(),
                    proxy=self._proxy,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 and attempt == 0:
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                        except (TypeError, ValueError):
                            # Mirrors the header default above, so a malformed value
                            # paces exactly like a missing one. The clamp below is
                            # what bounds the wait either way.
                            retry_after = 1.0
                        await asyncio.sleep(min(max(retry_after, 0.5), 10.0))
                        continue
                    if 200 <= resp.status < 300:
                        if resp.status == 204:
                            return {}
                        try:
                            return await resp.json(content_type=None)
                        except ValueError:
                            return {}
                    # Response bodies are externally-derived; log status only.
                    logger.warning("Webex API %s %s failed: http=%s", method, path, resp.status)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Webex API %s %s transport error: %s", method, path, type(exc).__name__
                )
                return None
        return None


def _retry_after(headers: Any) -> float:
    """The scan retry delay, clamped. A missing header must not mean zero."""
    try:
        value = float(headers.get("Retry-After") or _RETRY_AFTER_MIN_S)
    except (TypeError, ValueError):
        value = _RETRY_AFTER_MIN_S
    return min(max(value, _RETRY_AFTER_MIN_S), _RETRY_AFTER_MAX_S)


async def _write_stream(resp: Any, dest: str) -> None:
    """Stream a response body to *dest* without holding it in memory.

    Every filesystem touch here is a blocking syscall, so all three run in a
    worker thread — the OPEN and the CLOSE as much as the writes. The gateway
    shares one event loop with every other conversation and the liveness
    heartbeat: a 100 MB write on it would stall all of them, and on a
    network-backed or FUSE temp directory an open or a close (which flushes)
    stalls just as long for the same reason. Off-loading only the middle of the
    three leaves the cheapest-looking calls holding the loop.
    """
    handle = await asyncio.to_thread(open, dest, "wb")
    try:
        async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
            await asyncio.to_thread(handle.write, chunk)
    finally:
        await asyncio.to_thread(handle.close)


def _resolve_proxy() -> str | None:
    """Resolve an outbound proxy from the environment, if set."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None
