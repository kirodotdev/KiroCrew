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
from kiro_crew.messaging.split import split_markdown_safe

logger = logging.getLogger(__name__)

# Per-message character cap (well under the 30 000-byte platform ceiling;
# generous for mixed CJK + ASCII content). A longer answer is SPLIT across
# replies, never truncated — see send_reply.
FEISHU_MAX_TEXT = 4000

# The only two chat types this channel serves. The transport gate names them
# explicitly so an absent or future value is denied rather than falling
# through the ungated direct-message path.
CHAT_P2P = "p2p"
CHAT_GROUP = "group"

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
    #: @ placeholder removed -- see ``_command_body``. A group message must
    #: mention the bot, so the resolved ``text`` starts with "@BotName" and a
    #: whole-string command match would never fire; the dispatcher cannot derive
    #: this itself because the ``@_user_N`` placeholders are gone by then. Empty
    #: whenever there is no single leading mention -- a DM (``text`` is already
    #: mention-free) or a message naming somebody else as well, which must NOT
    #: be read as a bare command.
    command_text: str = ""


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

    Hence exactly ONE placeholder, and it must lead. A group message always
    mentions the bot, so a single leading mention IS the bot. Everything else
    returns "" and the dispatcher falls back to the resolved text, which still
    carries the mention and therefore cannot match a command.
    """
    stripped = raw_text.lstrip()
    matches = list(_AT_RE.finditer(stripped))
    if len(matches) != 1 or matches[0].start() != 0:
        return ""
    return stripped[matches[0].end() :].strip()


class LarkClient:
    """Feishu WebSocket + REST client.

    ``start()`` spawns a daemon thread that runs the lark-oapi WebSocket
    long-connection.  Inbound frames are forwarded to the async handler via
    ``asyncio.run_coroutine_threadsafe`` so the dispatcher never blocks.
    ``send_reply`` uses ``run_in_executor`` so it never blocks the loop.

    The lark-oapi ``ws.Client`` does not expose a clean async stop; ``close()``
    sets a flag and calls ``stop()`` -- the daemon thread exits naturally once
    the WS is closed.
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
        self._thread: threading.Thread | None = None
        self._closed = False
        #: Health observer ``(connected, reason)``, set by ``maybe_start_feishu``
        #: so the Settings badge tracks the receiver instead of guessing from
        #: "the channel was enabled at boot". Assigned after construction to
        #: avoid a client<->transport cycle; see ``_notify_state``.
        self.on_state_change: Callable[[bool, str], None] | None = None
        self._healthy: bool | None = None
        self._healthy_reason = ""

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
        """
        chunks = split_markdown_safe(text, FEISHU_MAX_TEXT)
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

        # Only handle plain-text messages for now.
        message_type = message.message_type or ""
        if message_type != "text":
            logger.info(
                "Feishu inbound dropped: unsupported message_type=%r (message_id=%s)",
                message_type,
                msg_id,
            )
            return

        sender = getattr(event, "sender", None)
        sid = getattr(sender, "sender_id", None) if sender else None
        open_id: str = (getattr(sid, "open_id", None) or "") if sid else ""
        if not open_id:
            logger.info(
                "Feishu inbound dropped: sender has no open_id (message_id=%s)",
                msg_id,
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
        )

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
        ws = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler_builder,
            log_level=lark.LogLevel.WARNING,
        )
        self._ws_client = ws

        def _run() -> None:
            # lark-oapi's ws.Client owns its own auto-reconnect, so this call
            # blocks for the life of the channel. Both exits still get a log
            # line: a RETURN means lark gave up reconnecting, which would
            # otherwise kill the receiver in total silence.
            #
            # Both exits also publish an UNHEALTHY transition, from this thread
            # (``_notify_state`` only calls a plain callback, which assigns two
            # attributes on DashboardState -- no loop affinity required). Without
            # it a rejected app id/secret leaves the badge reading "connected"
            # forever, because the only evidence of refusal is this thread
            # ending seconds after start().
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

        self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        self._thread.start()
        # Healthy on launch, then corrected by _run's exit. start() proves the
        # thread is up, not that Feishu accepted the app -- but a refused app
        # ends the thread within seconds, so the badge self-corrects rather than
        # sitting on an optimistic claim indefinitely.
        self._notify_state(True, "")
        logger.info("Feishu WebSocket receiver started (app_id=%s)", self._app_id)

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
        """Signal shutdown; the daemon thread exits once the WS closes."""
        self._closed = True
        ws = self._ws_client
        if ws is not None:
            # ws.stop() is lark-oapi's SYNCHRONOUS shutdown — it closes the
            # socket and can block on a wedged peer. Called directly it would
            # run on the gateway event loop, and the caller's asyncio.wait_for
            # could not rescue it: a timeout only fires at an await point, so a
            # hung peer would freeze every task on the loop rather than just
            # this shutdown. Offloading keeps the await interruptible; a leaked
            # worker thread is survivable, a frozen loop is not.
            try:
                await asyncio.get_running_loop().run_in_executor(self._executor, ws.stop)
            except Exception:
                logger.debug("Feishu WS stop failed", exc_info=True)
        # Do not wait: an in-flight REST reply must not hold up shutdown.
        self._executor.shutdown(wait=False)
        # An intentional shutdown is still "not connected" — with no reason,
        # because nothing failed.
        self._notify_state(False, "")
        logger.info("Feishu client closed")
