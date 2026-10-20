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

``DiscordClient`` is composed from owners: the REST request ladder and its rate-limit
accounting (``client_rest``), the mid-send destination re-check (``client_recheck``) and
the Gateway connection and dispatch normalization (``client_gateway``). An owner
function whose first parameter is ``self`` is bound as the class attribute of the same
name. This module keeps the client's state and lifecycle, the REST verbs, the bodies
they send (the JSON message payload and the multipart upload form) with the
``api_json`` / ``api_files`` entry points, the attachment download, the DM pairing cache
and the thread check, and stays the only import path and patch surface:
:mod:`kiro_crew._facade_mirror` carries a patch of one of its names to every owner that
reads it. Which owner takes new work is recorded in
``docs/system-specs/modules/messaging.md`` (Discord's renderer and client composition).
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import math
import os
import random
import re
import sys as _sys
import time
import urllib.parse
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

import aiohttp

from kiro_crew import _facade_mirror
from kiro_crew.discord import client_gateway as _client_gateway
from kiro_crew.discord import client_recheck as _client_recheck
from kiro_crew.discord import client_rest as _client_rest
from kiro_crew.discord.client_gateway import DiscordInbound, DiscordInteraction
from kiro_crew.discord.client_recheck import SendPermission
from kiro_crew.discord.client_rest import (
    _INVALID_LIMIT,
    DiscordApiResult,
    _classify_edit,
    _remember,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.identity import channel_outbound_permitted
from kiro_crew.messaging.outbound_files import OutboundFile, upload_filename
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.discord.client")

# Discord message content limit (chars).
DISCORD_MAX_TEXT = 2000
# Safe chunk boundary (leave room for chip/footer overhead).
DISCORD_CHUNK_LIMIT = 1900

# Upload limits also bound extraction memory; refused files retain their markup.
DISCORD_MAX_FILE_BYTES = 10 * 1024 * 1024
DISCORD_MAX_FILES_PER_MESSAGE = 10
DISCORD_MAX_TOTAL_UPLOAD_BYTES = 25 * 1024 * 1024

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

#: Discord message flag SUPPRESS_EMBEDS (``1 << 2``). Every outbound message
#: carries it so an agent-written URL cannot trigger a server-side link-preview
#: fetch. This client constructs no explicit rich embeds, so suppressing embeds
#: does not remove any intentional message content.
_FLAG_SUPPRESS_EMBEDS = 1 << 2


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
        "flags": _FLAG_SUPPRESS_EMBEDS,
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
_CALLBACK_CHANNEL_MESSAGE_WITH_SOURCE = 4
_CALLBACK_DEFERRED_UPDATE_MESSAGE = 6

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

#: Upper bound on remembered ``DM channel id -> user id`` pairings. One entry per
#: distinct peer this process has opened a DM with, so an operator-sized roster
#: never approaches it. Capped least-recently-used: dropping a pairing costs only
#: the exact peer attribution for that channel, which returns the mid-send
#: re-check to the answer it gives for any DM it never opened.
_MAX_TRACKED_DM_PEERS = 512

#: Upper bound on remembered EVICTED DM channel ids. Reaching the pairing cap makes
#: a later refusal of that channel mean "this process forgot the peer" rather than
#: "an operator withdrew it", and only the id itself can tell those apart. Holding a
#: few decides no send -- it selects the audit reason -- so this is deliberately much
#: smaller than the pairing cap and drops the oldest first.
_MAX_EVICTED_DM_CHANNELS = 128

#: Longest accepted Discord snowflake. They are 64-bit ids rendered as decimal
#: digits, so 20 characters is the real ceiling; the slack absorbs a future
#: widening without retaining an unbounded string.
_MAX_SNOWFLAKE_LEN = 32


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
        # DM channel id -> the user snowflake it was opened for, learned in
        # ``create_dm_channel``. In-process only: a DM opened before a restart is
        # absent, which ``cached_dm_recipient`` reports as unknown rather than
        # guessing.
        self._dm_recipients: dict[str, str] = {}
        # Channel ids whose pairing the cap dropped, newest last, and how many were
        # dropped in all. Neither decides a send: they only let a refusal say that
        # this process forgot the peer rather than that an operator withdrew it.
        self._evicted_dm_channels: dict[str, None] = {}
        self._dm_pairings_evicted: int = 0
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
        #: Optional callback fired once at the START of ``close()``. The gateway
        #: uses it to retire this channel's process-global spawn-approval
        #: delivery hook so the host gate stops routing to a channel that is
        #: going away (see ``messaging/spawn_approval_delivery.py``).
        self.on_close: Callable[[], None] | None = None
        #: Optional destination predicate, asked again after every wait the REST
        #: ladder takes and before the attempt that follows it. The transport
        #: installs its own roster check here (``DiscordTransport.__init__``); the
        #: ladder additionally re-reads the operator's ``channels`` ceiling on its
        #: own. Unwired (a bare client in a unit harness) the ceiling stands alone.
        #: A raise is read as a REFUSAL: this is a network egress boundary, and a
        #: predicate that cannot answer has not said yes.
        #:
        #: It answers a :class:`SendPermission`, so a refusal reports WHY, which
        #: only the rosters can tell.
        self.still_permitted: Callable[[str], SendPermission] | None = None

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

    # Bound from client_rest: the REST request ladder.
    _ensure_session = _client_rest._ensure_session
    _hold_key = _client_rest._hold_key
    _set_hold = _client_rest._set_hold
    _note_headers = _client_rest._note_headers
    _await_capacity = _client_rest._await_capacity
    _note_rate_limit = _client_rest._note_rate_limit
    _breaker_allows = _client_rest._breaker_allows
    _note_invalid = _client_rest._note_invalid
    _api_request = _client_rest._api_request

    # Bound from client_recheck: the mid-send destination re-check.
    _destination_still_permitted = _client_recheck._destination_still_permitted
    _roster_still_permits = _client_recheck._roster_still_permits
    _audit_mid_send_decision = _client_recheck._audit_mid_send_decision
    _wait_then_revalidate = _client_recheck._wait_then_revalidate

    # Bound from client_gateway: the Gateway connection.
    _gateway_loop = _client_gateway._gateway_loop
    _run_connection = _client_gateway._run_connection
    _handle_frame = _client_gateway._handle_frame
    _identify = _client_gateway._identify
    _start_heartbeat = _client_gateway._start_heartbeat
    _stop_heartbeat = _client_gateway._stop_heartbeat
    _heartbeat_loop = _client_gateway._heartbeat_loop
    _on_dispatch = _client_gateway._on_dispatch
    _invoke_message = _client_gateway._invoke_message
    _invoke_interaction = _client_gateway._invoke_interaction

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
        # Retire any process-global registration this channel holds (the
        # spawn-approval delivery hook) BEFORE tearing the connection down, so the
        # host spawn gate stops routing to a dispatcher that is going away.
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                logger.debug("Discord on_close callback failed", exc_info=True)
        # The session close is the LAST thing this method must do and the one
        # thing it must not skip, so it lives in a `finally`. Every step above it
        # can raise: `task.cancel()` on a task that ALREADY died with an error
        # is a no-op, and the following `await self._task` then re-raises that
        # error -- `except asyncio.CancelledError` does not catch it. A shutdown
        # while this coroutine is itself being cancelled does the same, since
        # `CancelledError` is a `BaseException`.
        #
        # Leaking the `ClientSession` leaves its connector and open sockets
        # behind for the process lifetime (aiohttp reports it as "Unclosed
        # client session" at GC), and `self._session` stays non-None, so a
        # later close finds a session it believes is already handled.
        try:
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
                finally:
                    self._task = None
        finally:
            # The drain below awaits, so a cancellation landing DURING it would
            # exit this finally before the session close -- the nested finally
            # keeps the close unconditional either way.
            try:
                # Snapshot first: a cancelled handler's done-callback mutates the set.
                handlers = list(self._handler_tasks)
                for handler in handlers:
                    handler.cancel()
                if handlers:
                    # return_exceptions so one handler raising during unwind cannot
                    # abandon the others or skip the session close below.
                    await asyncio.gather(*handlers, return_exceptions=True)
            finally:
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

    async def edit_message_with_files_outcome(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        files: Sequence[OutboundFile],
        *,
        components: list[dict] | None = None,
    ) -> str:
        """:meth:`edit_message_with_files` CLASSIFIED as ``EDIT_OK/GONE/FAILED``.

        Returns ``EDIT_OK`` on success, ``EDIT_GONE`` when the message is gone
        (a moderator/automod deletion -- a 404, which Discord maps to a
        non-retryable permanent result), and ``EDIT_FAILED`` for a transient
        failure (429/5xx, timeout) where the message is still on screen. The seam
        grader needs the distinction: a fresh POST after a GONE bubble lands below
        the message that was ABOVE it (``_bubble_above``), while one after a
        TRANSIENT failure lands below the still-visible bubble (``_sent_tail``).
        """
        result = await self.edit_message_result(
            channel_id, message_id, text, files=files, components=components
        )
        return _classify_edit(result)

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

    async def send_document(
        self,
        channel_id: str,
        document: OutboundFile,
        *,
        caption: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Upload ONE file as a named attachment. Returns the message id or None.

        The generic-file counterpart of :meth:`send_message_with_files`, for
        attachments that are not raster images (PDF, CSV, archives — whatever the
        caller's own gates admitted).

        ``filenames`` pins the document's real name: the multipart sanitizer is
        aimed at LLM-authored reference paths and maps any non-raster mime to
        ``.bin``, and this caller's name has already passed the file_send gates —
        letting the sanitizer rewrite the extension would deliver ``report.pdf``
        as ``report.bin`` and break the receiver's file-type association.

        Refuses over ``DISCORD_MAX_TOTAL_UPLOAD_BYTES`` rather than uploading
        into a 413: one attachment IS the whole upload, so the message ceiling is
        this file's ceiling. The caller gets None and keeps its existing
        dashboard-link fallback.
        """
        if not document.data:
            return None
        if len(document.data) > DISCORD_MAX_TOTAL_UPLOAD_BYTES:
            logger.warning(
                "discord: refusing a %d-byte document (ceiling %d)",
                len(document.data),
                DISCORD_MAX_TOTAL_UPLOAD_BYTES,
            )
            return None
        result = await self._api_multipart(
            "POST",
            f"/channels/{channel_id}/messages",
            _create_payload(caption or "", None, reply_to_message_id),
            [document],
            filenames=[Path(document.path or "").name],
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
        destination: str = "",
    ) -> bool:
        """Answer an interaction with an immediate message.

        Ephemeral by default: a command reply carries runtime state, a login
        link, or a model list, and an approved guild thread is readable by every
        member who can see it. A caller that genuinely wants a visible message
        passes ``ephemeral=False`` and says why.
        """
        data = _message_payload(text, components, keep_empty_components=False)
        if ephemeral:
            data["flags"] = int(data["flags"]) | _FLAG_EPHEMERAL
        payload: dict[str, Any] = {
            "type": _CALLBACK_CHANNEL_MESSAGE_WITH_SOURCE,
            "data": data,
        }
        result = await self._api(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            payload,
            destination=destination,
        )
        return result is not None

    async def ack_component_interaction(
        self, interaction_id: str, interaction_token: str, *, destination: str = ""
    ) -> None:
        """Acknowledge a button press without changing the message.

        DEFERRED_UPDATE_MESSAGE stops Discord's "interaction failed" spinner;
        the actual message update happens via a normal ``edit_message``.
        """
        await self._api(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            {"type": _CALLBACK_DEFERRED_UPDATE_MESSAGE},
            destination=destination,
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
        if not result:
            return ""
        channel_id = str(result.get("id"))
        if channel_id and len(channel_id) <= _MAX_SNOWFLAKE_LEN:
            # Discord answers with the channel alone, so this call is the one place
            # the pairing is known for free. Holding it lets the mid-send re-check
            # name the peer a DM channel belongs to and decide on THAT user instead
            # of asking whether the roster admits anybody. Bounded: the store is
            # capped least-recently-used, and both ids are length-checked before
            # they are retained.
            peer = str(user_id)
            if len(peer) <= _MAX_SNOWFLAKE_LEN:
                self._remember_dm_pairing(channel_id, peer)
        return channel_id

    def remember_dm_recipient(self, channel_id: str, user_id: str) -> None:
        """Record a ``DM channel id -> user id`` pairing learned from inbound.

        ``create_dm_channel`` is not the only place the pairing is knowable: an
        inbound DM names both, and a reply goes to that SAME channel without ever
        opening it. Without this, replying to an authorized DM would meet a
        re-check that can place no peer for the channel and refuse the reply.

        Bounded on the same terms as the outbound half: both ids are length-checked
        and the store is capped least-recently-used.
        """
        if not channel_id or len(channel_id) > _MAX_SNOWFLAKE_LEN:
            return
        if not user_id or len(user_id) > _MAX_SNOWFLAKE_LEN:
            return
        self._remember_dm_pairing(channel_id, user_id)

    def _remember_dm_pairing(self, channel_id: str, user_id: str) -> None:
        """Record one pairing, and account for anything the cap drops to make room.

        The eviction matters because it changes what a later refusal of that channel
        MEANS: the peer is gone from this process, not from the operator's roster.
        Recording the id keeps that distinguishable, and it is audited so an operator
        can see the cap was reached at all rather than inferring it from refusals.
        Re-learning a channel clears it again, so a genuine withdrawal after a
        re-learn is not mislabelled.
        """
        self._evicted_dm_channels.pop(channel_id, None)
        for dropped in _remember(
            self._dm_recipients, channel_id, user_id, cap=_MAX_TRACKED_DM_PEERS
        ):
            self._dm_pairings_evicted += 1
            _remember(self._evicted_dm_channels, dropped, None, cap=_MAX_EVICTED_DM_CHANNELS)
            logger.info(
                "Discord REST: the DM pairing cache is at its %d-entry cap; forgot the "
                "peer for one channel (%d dropped in this process). A waited send to it "
                "will be refused until the pairing is learned again.",
                _MAX_TRACKED_DM_PEERS,
                self._dm_pairings_evicted,
            )
            self._audit_mid_send_decision(dropped, "pairing_evicted", "recorded")

    def cached_dm_recipient(self, channel_id: str) -> str | None:
        """Which user does this DM channel belong to, from the cache ALONE?

        ``None`` when this process never opened the channel, which is the ordinary
        state for a proactive send whose destination came from a persisted link
        written before a restart. Network-free by contract: the mid-send re-check
        runs inside the REST ladder and must not issue a call that re-enters it.

        Reading REFRESHES recency. The store is capped least-recently-used, so a
        pairing that is read but never rewritten would age out while it is still in
        active use, and the next waited send to that channel would be refused for
        want of a peer it had.
        """
        peer = self._dm_recipients.get(channel_id)
        if peer is None:
            return None
        self._dm_recipients.pop(channel_id, None)
        self._dm_recipients[channel_id] = peer
        return peer

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

        Retires an ``[OPTIONS:]`` button row after a choice is tapped
        without clobbering the answer text that carried it. Pass ``[]`` to
        remove the buttons.
        """
        result = await self._api(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            {
                "components": components,
                "flags": _FLAG_SUPPRESS_EMBEDS,
            },
        )
        return result is not None

    async def api_json(
        self,
        method: str,
        path: str,
        payload: dict | list | None,
        *,
        timeout: int = 30,
        destination: str = "",
    ) -> DiscordApiResult:
        """Call a REST endpoint with a JSON body, reporting the outcome.

        The body may be a LIST: the application-command bulk-overwrite endpoint
        takes a top-level JSON array, not an object.
        """
        return await self._api_request(
            method,
            path,
            timeout=timeout,
            build=lambda: {"json": payload},
            destination=destination,
        )

    async def api_files(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        files: Sequence[OutboundFile],
        *,
        timeout: int = 60,
        filenames: Sequence[str] | None = None,
    ) -> DiscordApiResult:
        """Send multipart, rebuilding the single-use form for every attempt.

        *filenames*, when supplied, is used verbatim in place of
        :func:`upload_filename` — see :meth:`send_document` for when that is the
        right call. Omit it for anything whose name the model influenced.
        """
        return await self._api_request(
            method,
            path,
            timeout=timeout,
            build=lambda: {"data": _build_upload_form(payload, files, filenames=filenames)},
        )

    async def _api(
        self,
        method: str,
        path: str,
        payload: dict | list | None,
        timeout: int = 30,
        *,
        destination: str = "",
    ) -> Any:
        """:meth:`api_json` reduced to its body: the parsed JSON ({} for a 204)
        or None on any failure. The verbs that can only answer "did it land"
        use this; anything that must act on WHY calls ``api_json``."""
        return (
            await self.api_json(method, path, payload, timeout=timeout, destination=destination)
        ).data

    async def _api_multipart(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        files: Sequence[OutboundFile],
        timeout: int = 60,
        *,
        filenames: Sequence[str] | None = None,
    ) -> Any:
        """:meth:`api_files` reduced to its body, mirroring :meth:`_api`."""
        return (
            await self.api_files(method, path, payload, files, timeout=timeout, filenames=filenames)
        ).data


def _safe_description(alt: str) -> str:
    """Redact the unescaped description before truncation can split a secret."""
    out, _ = redact_for_display(
        alt, lambda s: redact_credentials(redact_exfiltration_urls(s)[0])[0]
    )
    return out[:1024]


def _build_upload_form(
    payload: dict[str, Any],
    files: Sequence[OutboundFile],
    *,
    filenames: Sequence[str] | None = None,
) -> aiohttp.FormData:
    """Build matching attachment descriptors and indexed file parts.

    *filenames* is positional against *files* and used verbatim when supplied,
    bypassing :func:`upload_filename` for a name the caller's own gates already
    admitted (see :meth:`DiscordClient.send_document`). The descriptor and the
    file part always carry the SAME name — Discord matches them by ``id``, and a
    mismatch is what renames an attachment mid-upload.
    """
    form = aiohttp.FormData()
    descriptors: list[dict[str, Any]] = []
    names: list[str] = []
    for index, file in enumerate(files):
        pinned = filenames[index] if filenames is not None and index < len(filenames) else ""
        name = pinned or upload_filename(file, index)
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


#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    Mapping,
    TypeVar,
    channel_outbound_permitted,
    dataclass,
    field,
    math,
    random,
    sel,
    time,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.discord.client_rest",
    "kiro_crew.discord.client_recheck",
    "kiro_crew.discord.client_gateway",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
#: The unsplit module's names: everything the family binds except the methods the class
#: binds from its owners, which were never module names.
_SURFACE = frozenset().union(globals(), *(vars(_sys.modules[o]) for o in _OWNERS)) - {
    n for n, m in vars(DiscordClient).items() if getattr(m, "__module__", "") in _OWNERS
}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=(), surface=_SURFACE).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.discord.client_gateway import (  # noqa: F401
        _GATEWAY_URL,
        _HANDLED_INTERACTIONS,
        _INTERACTION_APPLICATION_COMMAND,
        _INTERACTION_MESSAGE_COMPONENT,
        _MIN_HEALTHY_CONN_SECS,
        _OP_DISPATCH,
        _OP_HEARTBEAT,
        _OP_HEARTBEAT_ACK,
        _OP_HELLO,
        _OP_IDENTIFY,
        _OP_INVALID_SESSION,
        _OP_RECONNECT,
        _OP_RESUME,
        _WS_HEARTBEAT_SECS,
        _command_options,
        _find_button_label,
    )
    from kiro_crew.discord.client_recheck import (  # noqa: F401
        _REVOKED_DETAIL,
        _UNANSWERABLE_DETAIL,
        _UNATTRIBUTABLE_DETAIL,
        _guarded_destination,
    )
    from kiro_crew.discord.client_rest import (  # noqa: F401
        _API_BASE,
        _BREAKER_COOLOFF_SECS,
        _DEFAULT_RETRY_AFTER_SECS,
        _DISCORD_INVALID_BAN_LIMIT,
        _HDR_BUCKET,
        _HDR_REMAINING,
        _HDR_RESET_AFTER,
        _HDR_RETRY_AFTER,
        _HDR_SCOPE,
        _ID_SEGMENT_RE,
        _INVALID_STATUSES,
        _INVALID_WINDOW_SECS,
        _MAJOR_PARAM_PARENTS,
        _MAX_GLOBAL_HOLD_SECS,
        _MAX_LITERAL_SEGMENT,
        _MAX_PREEMPT_SECS,
        _MAX_RETRY_AFTER_SECS,
        _MAX_TRACKED_ROUTES,
        _MIN_HOLD_SECS,
        _RATE_LIMIT_RETRIES,
        _ROUTE_SEP,
        _SCOPE_GLOBAL,
        _SCOPE_SHARED,
        _T,
        _TOKEN_SEGMENT_PARENTS,
        _TRANSIENT_BACKOFF_SECS,
        _TRANSIENT_RETRIES,
        DISCORD_BLOCKED,
        DISCORD_OK,
        DISCORD_PERMANENT,
        DISCORD_TRANSIENT,
        EDIT_FAILED,
        EDIT_GONE,
        EDIT_OK,
        _coerce_float,
        _failed,
        _header_float,
        _header_int,
        _is_global_limit_exempt,
        _rate_limit_headers,
        _route_key,
    )
