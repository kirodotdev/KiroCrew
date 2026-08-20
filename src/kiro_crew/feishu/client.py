"""Feishu (Lark) client -- wraps lark-oapi for async-compatible send/receive.

Inbound: lark-oapi ``ws.Client`` runs in a daemon thread and pushes
normalized ``LarkInbound`` frames into the async event loop via
``asyncio.run_coroutine_threadsafe``.

Outbound: ``send_reply`` wraps the sync lark-oapi REST API in
``run_in_executor`` so it never blocks the event loop.

``lark-oapi`` is an OPTIONAL dependency (``pip install "kirocrew[feishu]"``).
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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

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
_SEEN_MAX = 500
_SEEN_KEEP = 200


@dataclass
class LarkInbound:
    """Normalised inbound Feishu message."""

    open_id: str  # sender open_id
    text: str  # message body, @ mentions already stripped
    message_id: str  # used as the reply anchor
    chat_type: str  # CHAT_P2P | CHAT_GROUP; "" when the frame omitted it
    chat_id: str  # group chat_id (empty for p2p)


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
                "Install it with: pip install lark-oapi"
            ) from exc

        # Seen message-id window for deduplication (lark-oapi WS can redeliver).
        # An OrderedDict gives ARRIVAL-ORDERED eviction -- a plain set's
        # iteration order is arbitrary, so trimming it would drop random ids
        # and let an old redelivery through while evicting a fresh one. Written
        # from the lark-oapi WS thread, so guarded by a lock.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._seen_lock = threading.Lock()
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

        content = json.dumps({"text": text})
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
            return
        message = getattr(event, "message", None)
        if message is None:
            return

        msg_id: str = message.message_id or ""
        if not msg_id:
            return

        # Dedup -- lark WS may redeliver the same event. Test-and-insert must
        # be ATOMIC: lark-oapi dispatches from its own thread(s), so a
        # check-then-add would let a concurrent redelivery drive a second turn.
        with self._seen_lock:
            if msg_id in self._seen:
                return
            self._seen[msg_id] = None
            if len(self._seen) > _SEEN_MAX:
                # popitem(last=False) evicts the OLDEST arrival.
                while len(self._seen) > _SEEN_KEEP:
                    self._seen.popitem(last=False)

        # Only handle plain-text messages for now.
        if (message.message_type or "") != "text":
            return

        sender = getattr(event, "sender", None)
        sid = getattr(sender, "sender_id", None) if sender else None
        open_id: str = (getattr(sid, "open_id", None) or "") if sid else ""
        if not open_id:
            return

        try:
            content = json.loads(message.content or "{}")
            raw_text: str = content.get("text", "").strip()
        except Exception:
            return

        # Feishu sends mentions as opaque placeholders (``@_user_1``) with the
        # display names in ``message.mentions``. Resolve them to names so an
        # instruction naming a third party survives -- deleting them turns
        # "ask @Alice to review" into "ask to review". A message that is
        # NOTHING but mentions carries no instruction, so it is still ignored,
        # which is what keeps a bare "@bot" from driving an empty turn.
        if not _AT_RE.sub("", raw_text).strip():
            return
        text = _resolve_mentions(raw_text, getattr(message, "mentions", None)).strip()
        if not text:
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
            try:
                ws.start()
            except Exception:
                if not self._closed:
                    logger.exception("Feishu WS loop raised; receiver is down")
                return
            if not self._closed:
                logger.error(
                    "Feishu WS loop returned without a close() -- receiver is "
                    "down and will not reconnect; restart the gateway."
                )

        self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        self._thread.start()
        logger.info("Feishu WebSocket receiver started (app_id=%s)", self._app_id)

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
        logger.info("Feishu client closed")
