"""Feishu (Lark) client -- wraps lark-oapi for async-compatible send/receive.

Inbound: lark-oapi ``ws.Client`` runs in a daemon thread and pushes
normalized ``LarkInbound`` frames into the async event loop via
``asyncio.run_coroutine_threadsafe``.

Outbound: ``send_reply`` wraps the sync lark-oapi REST API in
``run_in_executor`` so it never blocks the event loop.

``lark-oapi`` is an OPTIONAL dependency: it ships in the ``feishu`` extra, and
is installed on its own with ``pip install 'lark-oapi>=1.4,<2'`` (this project
is not on an index, so ``pip install kirocrew[feishu]`` cannot resolve).
It is imported lazily inside this module's methods so that every other module
in the package -- including :mod:`kiro_crew.feishu.transport` and the channel
roster in :mod:`kiro_crew.channels` -- imports cleanly on a build that does not
have it. ``maybe_start_feishu`` catches the resulting ``ImportError`` and skips
the channel with a log line rather than failing gateway boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kiro_crew import extras
from kiro_crew.messaging.renderer import _default_redactor, chunk_text
from kiro_crew.messaging.split import bounded_for_delivery, split_markdown_safe

logger = logging.getLogger(__name__)

# Per-message character cap (well under the 30 000-byte platform ceiling;
# generous for mixed CJK + ASCII content). A longer answer is SPLIT across
# replies, never truncated — see send_reply.
FEISHU_MAX_TEXT = 4000


def _graded_chunks(text: str) -> list[str]:
    """Split *text* to the Feishu cap and grade every seam, off the event loop.

    Both steps scan the whole text for a credential the model wrote across a
    boundary, which is O(len) work an oversized reply turns into seconds. The
    caller runs it under ``asyncio.to_thread`` so a large payload cannot stall
    the gateway's single loop past its watchdog.
    """
    chunks = split_markdown_safe(text, FEISHU_MAX_TEXT, redactor=_default_redactor)
    return bounded_for_delivery(chunks, FEISHU_MAX_TEXT, _default_redactor, chunk_text) or chunks


# Client construction is local and performs no network I/O. A bounded wait
# prevents a broken SDK import or constructor from leaving gateway startup
# suspended forever while preserving enough time on a loaded host.
_WS_CLIENT_READY_TIMEOUT_SECS = 5.0

# The only two chat types this channel serves. The transport gate names them
# explicitly so an absent or future value is denied rather than falling
# through the ungated direct-message path.
CHAT_P2P = "p2p"
CHAT_GROUP = "group"

# The app's own bot identity. A group message drives a turn only when its
# ``mentions`` list names this open_id, so it is resolved once per start from
# Feishu's bot-info endpoint. The probe runs on its own SDK client with a
# request timeout (the reply client has none), so a stalled probe frees its
# executor thread instead of holding one of the reply workers.
_BOT_INFO_URI = "/open-apis/bot/v3/info"
_BOT_INFO_TIMEOUT_SECS = 10.0
# Backoff between failed probes: doubling from the first delay, capped.
_BOT_INFO_RETRY_FIRST_SECS = 30.0
_BOT_INFO_RETRY_MAX_SECS = 600.0

# Bounded dedup window for redelivered WS frames: keep the most recent N
# message_ids and evict in arrival order (see ``_handle_receive_v1``).


@dataclass
class LarkInbound:
    """Normalised inbound Feishu message."""

    open_id: str  # sender open_id
    text: str  # message body, @ mentions resolved to display names
    message_id: str  # used as the reply anchor
    chat_type: str  # CHAT_P2P | CHAT_GROUP; "" when the frame omitted it
    chat_id: str  # group chat_id (empty for p2p)
    #: The body a slash command is matched against, with the bot's own leading
    #: @ placeholder removed -- see ``_command_body``. A group message reaches a
    #: turn only when it mentions the bot (``bot_mentioned``), so the resolved
    #: ``text`` starts with "@BotName" and a whole-string command match would
    #: never fire; the dispatcher cannot derive this itself because the
    #: ``@_user_N`` placeholders are gone by then. Empty whenever there is no
    #: single leading mention -- a DM (``text`` is already mention-free) or a
    #: message naming somebody else as well, which must NOT be read as a bare
    #: command.
    command_text: str = ""
    #: True when the event's ``mentions`` list names THIS app's bot by open_id.
    #: The transport refuses a group message without it, so a plain message
    #: from an allow-listed sender in an allow-listed group drives nothing.
    #: False whenever the bot's own open_id is not resolved, which is what
    #: keeps the group gate closed until the identity is known. DMs ignore it.
    bot_mentioned: bool = False
    #: Set by the transport's ``receive`` on a message a person sent, so its turn
    #: starts FOREGROUND (kiro_crew.start_priority); False on anything built elsewhere.
    person_origin: bool = False
    #: Feishu's ``message_type`` when it was something this channel cannot read
    #: (``image``, ``file``, ``audio``, ``post``...), otherwise empty. Such a
    #: message is carried to the transport with an empty ``text`` instead of
    #: being dropped at the parse step, so the SENDER can be told rather than
    #: only the gateway log -- but only after the transport's own gates have
    #: authorised them. It never drives a turn.
    unsupported_type: str = ""


# Signature for the async dispatch callback the transport injects.
MessageHandler = Callable[[LarkInbound], Awaitable[None]]

# Regex that matches Feishu @-mention placeholders in message bodies.
_AT_RE = re.compile(r"@_user_\d+\s*|@_all\s*")


def _resolve_mentions(raw_text: str, mentions: Any) -> str:
    """Replace Feishu mention placeholders with the mentioned display names.

    ``mentions`` is the event's mention list, each entry pairing a ``key``
    (the ``@_user_N`` placeholder as it appears in the text) with a ``name``.
    A placeholder with no resolvable name is dropped rather than left as an
    opaque token the agent would have to guess at; ``@_all`` becomes ``@all``
    so the instruction keeps its scope.
    """
    names: dict[str, str] = {}
    for m in mentions or ():
        key = str(getattr(m, "key", "") or "")
        name = str(getattr(m, "name", "") or "").strip()
        if key and name:
            names[key] = name

    def _sub(match: "re.Match[str]") -> str:
        token = match.group(0)
        placeholder = token.strip()
        if placeholder == "@_all":
            return "@all "
        name = names.get(placeholder)
        return f"@{name} " if name else ""

    return _AT_RE.sub(_sub, raw_text)


def _command_body(raw_text: str) -> str:
    """Text a slash command is matched against, or "" when there is no candidate.

    A group message must @-mention the bot, so a bare command arrives as
    "@_user_1 /new" and the whole-string command match needs the mention gone.
    But deleting EVERY placeholder is wrong: "@_user_1 /new @_user_2" would
    collapse to "/new", intercept, and reset a conversation on a message that
    named a third party and was never a bare command. That loss is
    unrecoverable, so ambiguity resolves to "not a command".

    Hence exactly ONE placeholder, and it must lead. The transport serves a
    group message only when its mentions name the bot, so on a served group
    message a single leading mention IS the bot. Everything else returns "" and
    the dispatcher falls back to the resolved text, which still carries the
    mention and therefore cannot match a command.
    """
    stripped = raw_text.lstrip()
    matches = list(_AT_RE.finditer(stripped))
    if len(matches) != 1 or matches[0].start() != 0:
        return ""
    return stripped[matches[0].end() :].strip()


def _mentions_bot(mentions: Any, bot_open_id: str) -> bool:
    """Whether an event's ``mentions`` list names the bot whose id is *bot_open_id*.

    Matched on ``id.open_id``, never on the display name: any member can carry
    the bot's name, while the open_id is unique to this app. An unresolved
    (empty) bot id matches nothing, so a mention entry with no open_id can
    never read as the bot.
    """
    if not bot_open_id:
        return False
    for m in mentions or ():
        ident = getattr(m, "id", None)
        if ident is not None and getattr(ident, "open_id", None) == bot_open_id:
            return True
    return False


def _parse_bot_open_id(content: Any) -> str:
    """The bot's open_id from a bot-info response body; ValueError otherwise.

    Expected shape: ``{"code": 0, "bot": {"open_id": "ou_..."}}``. A non-zero
    code, a missing or blank open_id, or a body that is not a JSON object is
    refused rather than read as an identity, because an empty id is what keeps
    the group gate closed.
    """
    if isinstance(content, (bytes, bytearray)):
        content = bytes(content).decode("utf-8", errors="replace")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("bot info response has no body")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("bot info response is not a JSON object")
    code = payload.get("code")
    if code != 0:
        raise ValueError(f"bot info refused: code={code!r} msg={payload.get('msg')!r}")
    bot = payload.get("bot")
    open_id = bot.get("open_id") if isinstance(bot, dict) else None
    if not isinstance(open_id, str) or not open_id.strip():
        raise ValueError("bot info response carries no open_id")
    return open_id.strip()


class LarkClient:
    """Feishu WebSocket + REST client.

    ``start()`` spawns a daemon thread that runs the lark-oapi WebSocket
    long-connection.  Inbound frames are forwarded to the async handler via
    ``asyncio.run_coroutine_threadsafe`` so the dispatcher never blocks.
    ``send_reply`` uses ``run_in_executor`` so it never blocks the loop.

    The official SDK stores its WebSocket event loop in a module global. The
    gateway imports the SDK on its own running loop, so the receiver thread must
    replace that global with a dedicated loop and construct ``ws.Client`` there
    before calling ``start``. ``close()`` prefers a future public ``stop()``
    method and otherwise shuts down the current 1.x SDK through its async
    disconnect coroutine.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_message: MessageHandler | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message: MessageHandler | None = on_message
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        #: Health observer ``(connected, reason)``, set by ``maybe_start_feishu``
        #: so the Settings badge tracks the receiver instead of guessing from
        #: "the channel was enabled at boot". Assigned after construction to
        #: avoid a client<->transport cycle; see ``_notify_state``.
        self.on_state_change: Callable[[bool, str], None] | None = None
        self._healthy: bool | None = None
        self._healthy_reason = ""
        #: This app's bot open_id, "" until ``_resolve_bot_open_id`` succeeds.
        #: Written on the event loop, read on the WS thread; a str attribute
        #: swap is atomic, and the WS thread only ever compares against it.
        self._bot_open_id = ""
        self._bot_identity_task: asyncio.Task[None] | None = None

        # Build the sync REST client once; it is thread-safe for outbound calls.
        try:
            import lark_oapi as lark  # noqa: PLC0415 (lazy import keeps the dep optional)

            self._lark = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            self._lark_mod = lark
        except ImportError as exc:
            raise ImportError(
                "lark-oapi is required for the Feishu channel. "
                f"Install it with: {extras.install_hint('feishu')}"
            ) from exc

        # Dedicated executor for the blocking REST replies. The default
        # (``None``) executor is process-global and shared with every other
        # ``run_in_executor`` caller in the gateway, so a burst of Feishu
        # replies would steal its threads.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-rest")
        # Forward ref to the WS client so ``close()`` can stop it.
        self._ws_client: Any = None

    # -- Outbound -----------------------------------------------------------

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._on_message = handler

    async def send_reply(self, message_id: str, text: str) -> bool:
        """Reply to *message_id* with *text*.  Returns True on success.

        A long answer is SPLIT rather than truncated: Feishu's per-message
        ceiling is a transport limit, not a reason to drop the tail of a reply.
        The split goes through the shared fence-safe splitter rather than a
        fixed-width cut, so a code block in a long answer is resealed across
        chunks instead of rendering broken. Every chunk is a reply to the same
        inbound anchor, in order, and the first failure stops the rest — a
        partial send reported as success is what makes a dropped answer
        invisible.

        The cut is credential-aware because each chunk is its own message: a
        boundary here is a seam between two replies the reader reads in order, and
        a key the model wrote across a line break is invisible to a per-message
        scan yet whole on screen once the break is gone. That cut is fail-closed --
        it can decline to cut and answer with the text whole -- and this ceiling
        drops a larger payload, so the answer is bounded and graded again before
        anything is sent.
        """
        chunks = await asyncio.to_thread(_graded_chunks, text)
        if not chunks:
            return True
        loop = asyncio.get_running_loop()
        for chunk in chunks:
            try:
                await loop.run_in_executor(self._executor, self._sync_reply, message_id, chunk)
            except Exception as exc:
                logger.error("Feishu reply failed (message_id=%s): %s", message_id, exc)
                return False
        return True

    def _sync_reply(self, message_id: str, text: str) -> None:
        """Blocking REST reply; call only from a worker thread."""
        from lark_oapi.api.im.v1 import (  # noqa: PLC0415
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        # ensure_ascii=False: the default escapes non-ASCII to ``\uXXXX``, which
        # DOUBLES a CJK reply's payload (6 bytes per char) and quadruples an
        # emoji's (12, as a surrogate pair). The splitter bounds the reply in
        # characters while Feishu bounds the request in serialized bytes, so the
        # escaped form pushes an ordinary 4000-char Chinese reply past the limit
        # and the API rejects it -- the user receives nothing.
        content = json.dumps({"text": text}, ensure_ascii=False)
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().content(content).msg_type("text").build()
            )
            .build()
        )
        resp = self._lark.im.v1.message.reply(req)
        if not resp.success():
            raise RuntimeError(f"Feishu reply error: code={resp.code} msg={resp.msg}")

    # -- Bot identity --------------------------------------------------------

    def _sync_fetch_bot_open_id(self) -> str:
        """Blocking bot-info probe; call only from a worker thread.

        A dedicated SDK client carries the request timeout, so a stalled
        endpoint raises and frees this executor thread rather than holding one
        of the workers ``send_reply`` shares. The tenant token comes from the
        same app credentials as the reply client.
        """
        lark = self._lark_mod
        probe = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .timeout(_BOT_INFO_TIMEOUT_SECS)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(_BOT_INFO_URI)
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        resp = probe.request(req)
        return _parse_bot_open_id(getattr(getattr(resp, "raw", None), "content", None))

    async def _resolve_bot_open_id(self) -> None:
        """Resolve this app's bot open_id, retrying with capped backoff.

        Until it resolves every group message reads ``bot_mentioned=False`` and
        the transport refuses it; DMs never consult it. The first failure logs
        one WARNING naming that consequence, later failures log at DEBUG, and a
        recovery after a failure logs once more so the operator sees groups
        return.
        """
        loop = asyncio.get_running_loop()
        delay = _BOT_INFO_RETRY_FIRST_SECS
        warned = False
        while not self._closed:
            try:
                open_id = await loop.run_in_executor(self._executor, self._sync_fetch_bot_open_id)
            except Exception as exc:
                if self._closed:
                    return
                if not warned:
                    warned = True
                    logger.warning(
                        "Feishu bot open_id could not be resolved (%s: %s); group "
                        "messages are refused until it is. Direct messages are "
                        "unaffected. Retrying with backoff.",
                        type(exc).__name__,
                        exc,
                    )
                else:
                    logger.debug("Feishu bot open_id probe failed again", exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BOT_INFO_RETRY_MAX_SECS)
                continue
            self._bot_open_id = open_id
            if warned:
                logger.info(
                    "Feishu bot open_id resolved after a failure; group messages are served."
                )
            else:
                logger.info("Feishu bot open_id resolved")
            return

    # -- Inbound WS handler (called from daemon thread) --------------------

    def _handle_receive_v1(self, data: Any) -> None:
        """Sync P2ImMessageReceiveV1 handler injected into the WS dispatcher."""
        event = getattr(data, "event", None)
        if event is None:
            logger.info("Feishu inbound dropped: frame has no event")
            return
        message = getattr(event, "message", None)
        if message is None:
            logger.info("Feishu inbound dropped: event has no message")
            return

        msg_id: str = message.message_id or ""
        if not msg_id:
            logger.info("Feishu inbound dropped: message has no message_id")
            return

        # NOTE: redelivery dedup deliberately does NOT happen here. Everything
        # below -- the message-type filter -- and everything in
        # ``FeishuTransport.receive`` -- the chat-type gate, the group
        # allow-list, ``authorize`` -- can still drop this message, and a window
        # spent on a message that never drives a turn is a window an authorized
        # message can be evicted from. Dedup lives in ``receive`` immediately
        # after authorization instead.

        # Only plain text drives a turn. The type is read here but decided
        # AFTER the sender, because a message this channel cannot read is now
        # carried to the transport rather than dropped -- and the transport
        # cannot authorise a sender it was never given.
        message_type = message.message_type or ""

        sender = getattr(event, "sender", None)
        sid = getattr(sender, "sender_id", None) if sender else None
        open_id: str = (getattr(sid, "open_id", None) or "") if sid else ""
        if not open_id:
            logger.info(
                "Feishu inbound dropped: sender has no open_id (message_id=%s)",
                msg_id,
            )
            return

        # Read before the type split so an unreadable message carries it too:
        # the transport's group gate refuses either kind without it.
        bot_mentioned = _mentions_bot(getattr(message, "mentions", None), self._bot_open_id)

        if message_type != "text":
            # Handed on with an empty ``text`` and the type recorded, so
            # ``FeishuTransport.receive`` can answer an AUTHORISED sender
            # instead of leaving them with silence. The log line stays: it is
            # the operator's record, a separate audience from the sender reply.
            logger.info(
                "Feishu inbound dropped: unsupported message_type=%r (message_id=%s)",
                message_type,
                msg_id,
            )
            self._deliver(
                LarkInbound(
                    open_id=open_id,
                    text="",
                    message_id=msg_id,
                    chat_type=message.chat_type or "",
                    chat_id=message.chat_id or "",
                    unsupported_type=message_type,
                    bot_mentioned=bot_mentioned,
                )
            )
            return

        try:
            content = json.loads(message.content or "{}")
            raw_text: str = content.get("text", "").strip()
        except Exception:
            logger.info(
                "Feishu inbound dropped: message content is not valid JSON (message_id=%s)",
                msg_id,
            )
            return

        # Feishu sends mentions as opaque placeholders (``@_user_1``) with the
        # display names in ``message.mentions``. Resolve them to names so an
        # instruction naming a third party survives -- deleting them turns
        # "ask @Alice to review" into "ask to review". A message that is
        # NOTHING but mentions carries no instruction, so it is still ignored,
        # which is what keeps a bare "@bot" from driving an empty turn.
        mention_free = _AT_RE.sub("", raw_text).strip()
        if not mention_free:
            logger.info(
                "Feishu inbound dropped: message body is mention-only "
                "(no instruction) (message_id=%s)",
                msg_id,
            )
            return
        # NOT ``mention_free``: that deletes every placeholder, which would read
        # "@bot /new @alice" as the bare command "/new" and reset the
        # conversation. See ``_command_body``.
        command_body = _command_body(raw_text)
        text = _resolve_mentions(raw_text, getattr(message, "mentions", None)).strip()
        if not text:
            logger.info(
                "Feishu inbound dropped: resolved text is empty (message_id=%s)",
                msg_id,
            )
            return

        inbound = LarkInbound(
            open_id=open_id,
            text=text,
            message_id=msg_id,
            # Preserved verbatim, NOT defaulted to "p2p": the transport gate
            # keys on the chat type, so inventing a DM type for a frame that
            # did not state one would route an unknown context down the
            # ungated path. An empty value is denied there.
            chat_type=message.chat_type or "",
            chat_id=message.chat_id or "",
            command_text=command_body,
            bot_mentioned=bot_mentioned,
        )

        self._deliver(inbound)

    def _deliver(self, inbound: LarkInbound) -> None:
        """Hand *inbound* to the transport on the event loop, if one is live.

        Extracted because two parse outcomes reach it now -- a text message and
        an unreadable one -- and the liveness guard must not be copied: a second
        copy is where a closed-loop check goes stale. Called from the WS thread,
        so the hop through ``run_coroutine_threadsafe`` is what makes it safe.
        """
        loop = self._loop
        handler = self._on_message
        if loop is not None and not loop.is_closed() and handler is not None:
            asyncio.run_coroutine_threadsafe(handler(inbound), loop)

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start the WS receive loop in a daemon thread."""
        import lark_oapi as lark  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        self._closed = False

        handler_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_receive_v1)
            .build()
        )
        ws_loop = asyncio.new_event_loop()
        self._ws_loop = ws_loop
        client_ready = threading.Event()
        startup_errors: list[BaseException] = []

        def _run() -> None:
            # lark-oapi binds both its module-global loop and constructor-time
            # helpers (including ExpiringCache) to the current event loop. The
            # client therefore has to be constructed here, after this thread
            # owns its loop; moving only ws.start() leaves SDK state split
            # across the gateway and receiver loops.
            try:
                try:
                    asyncio.set_event_loop(ws_loop)
                    from lark_oapi.ws import client as lark_ws_client  # noqa: PLC0415

                    lark_ws_client.loop = ws_loop
                    ws = lark.ws.Client(
                        self._app_id,
                        self._app_secret,
                        event_handler=handler_builder,
                        log_level=lark.LogLevel.WARNING,
                    )
                    self._ws_client = ws
                except BaseException as exc:
                    startup_errors.append(exc)
                    if not self._closed:
                        logger.exception("Feishu WS client initialization failed")
                        self._notify_state(False, f"receiver stopped: {type(exc).__name__}")
                    return
                finally:
                    client_ready.set()

                try:
                    ws.start()
                except Exception as exc:
                    if not self._closed:
                        logger.exception("Feishu WS loop raised; receiver is down")
                        self._notify_state(False, f"receiver stopped: {type(exc).__name__}")
                    return
                if not self._closed:
                    logger.error(
                        "Feishu WS loop returned without a close() -- receiver is "
                        "down and will not reconnect; restart the gateway."
                    )
                    self._notify_state(
                        False,
                        "receiver stopped (check the app id/secret and that the app "
                        "has the im:message events subscribed)",
                    )
            finally:
                client_ready.set()
                self._ws_loop = None
                pending = asyncio.all_tasks(ws_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    ws_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                ws_loop.close()

        self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        self._thread.start()
        ready = await asyncio.to_thread(client_ready.wait, _WS_CLIENT_READY_TIMEOUT_SECS)
        if not ready:
            self._closed = True
            self._executor.shutdown(wait=False)
            try:
                ws_loop.call_soon_threadsafe(ws_loop.stop)
            except RuntimeError:
                pass
            raise RuntimeError("Feishu WebSocket client initialization timed out")
        if startup_errors:
            self._executor.shutdown(wait=False)
            startup_error = startup_errors[0]
            raise RuntimeError("Feishu WebSocket client initialization failed") from startup_error

        # Healthy on launch, then corrected by _run's exit. start() proves the
        # thread is up, not that Feishu accepted the app -- but a refused app
        # ends the thread within seconds, so the badge self-corrects rather than
        # sitting on an optimistic claim indefinitely.
        self._notify_state(True, "")
        logger.info("Feishu WebSocket receiver started (app_id=%s)", self._app_id)
        # Off the start path: a slow probe must not delay the receiver, and a
        # message that lands first is a group message refused, never served.
        self._bot_identity_task = asyncio.create_task(
            self._resolve_bot_open_id(), name="feishu-bot-identity"
        )

    def _notify_state(self, connected: bool, error: str) -> None:
        """Publish a health transition to the dashboard badge.

        Deduped on the transition (mirrors ``TeamsClient._notify_state``): a
        repeated identical report must not overwrite the FIRST reason with the
        same later one. The first call always publishes, because the initial
        state is unknown rather than healthy.
        """
        if self._healthy is connected and error == self._healthy_reason:
            return
        self._healthy = connected
        self._healthy_reason = error
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("Feishu on_state_change observer raised", exc_info=True)

    async def close(self) -> None:
        """Signal shutdown and release the SDK-owned receiver loop."""
        self._closed = True
        identity_task = self._bot_identity_task
        self._bot_identity_task = None
        if identity_task is not None and not identity_task.done():
            identity_task.cancel()
        ws = self._ws_client
        if ws is not None:
            stop = getattr(ws, "stop", None)
            if callable(stop):
                # A future SDK may expose a synchronous stop. Keep it off the
                # gateway loop because a wedged peer would otherwise freeze all
                # tasks and make the caller's timeout ineffective.
                try:
                    await asyncio.get_running_loop().run_in_executor(self._executor, stop)
                except Exception:
                    logger.debug("Feishu WS stop failed", exc_info=True)
            else:
                ws_loop = self._ws_loop
                if ws_loop is not None and not ws_loop.is_closed():
                    # lark-oapi 1.x exposes only the private async disconnect
                    # used by its own reconnect path. Disable reconnect before
                    # closing, then stop the loop that blocks in start().
                    if hasattr(ws, "_auto_reconnect"):
                        ws._auto_reconnect = False
                    disconnect = getattr(ws, "_disconnect", None)
                    if callable(disconnect) and ws_loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(disconnect(), ws_loop)
                        try:
                            await asyncio.wrap_future(future)
                        except Exception:
                            logger.debug("Feishu WS disconnect failed", exc_info=True)
                    try:
                        ws_loop.call_soon_threadsafe(ws_loop.stop)
                    except RuntimeError:
                        # The disconnect may make a future SDK return from
                        # start() and close the loop before this signal lands.
                        pass
        # Do not wait: an in-flight REST reply must not hold up shutdown.
        self._executor.shutdown(wait=False)
        # An intentional shutdown is still "not connected" — with no reason,
        # because nothing failed.
        self._notify_state(False, "")
        logger.info("Feishu client closed")
