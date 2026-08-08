"""Feishu (Lark) client -- wraps lark-oapi for async-compatible send/receive.

Inbound: lark-oapi ``ws.Client`` runs in a daemon thread and pushes
normalized ``LarkInbound`` frames into the async event loop via
``asyncio.run_coroutine_threadsafe``.

Outbound: ``send_reply`` wraps the sync lark-oapi REST API in
``run_in_executor`` so it never blocks the event loop.

No external dependency beyond ``lark-oapi>=1.0`` (already a required dep
when ``feishu.enabled`` is True).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Safe character cap for a single Feishu text message (well under the 30 000-
# byte platform ceiling; generous for mixed CJK + ASCII content).
FEISHU_MAX_TEXT = 4000


@dataclass
class LarkInbound:
    """Normalised inbound Feishu message."""

    open_id: str   # sender open_id
    text: str      # message body, @ mentions already stripped
    message_id: str  # used as the reply anchor
    chat_type: str   # "p2p" | "group"
    chat_id: str     # group chat_id (empty for p2p)


# Signature for the async dispatch callback the transport injects.
MessageHandler = Callable[[LarkInbound], Awaitable[None]]

# Regex that matches Feishu @-mention placeholders in message bodies.
_AT_RE = re.compile(r"@_user_\d+\s*|@_all\s*")


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

        # Seen message-id set for deduplication (lark-oapi WS can redeliver).
        self._seen: set[str] = set()
        # Forward ref to the WS client so ``close()`` can stop it.
        self._ws_client: Any = None

    # -- Outbound -----------------------------------------------------------

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._on_message = handler

    async def send_reply(self, message_id: str, text: str) -> bool:
        """Reply to *message_id* with *text*.  Returns True on success."""
        if len(text) > FEISHU_MAX_TEXT:
            text = text[: FEISHU_MAX_TEXT - 3] + "..."
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._sync_reply, message_id, text)
            return True
        except Exception as exc:
            logger.error("Feishu reply failed (message_id=%s): %s", message_id, exc)
            return False

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
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("text")
                .build()
            )
            .build()
        )
        resp = self._lark.im.v1.message.reply(req)
        if not resp.success():
            raise RuntimeError(
                f"Feishu reply error: code={resp.code} msg={resp.msg}"
            )

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

        # Dedup -- lark WS may redeliver the same event.
        if msg_id in self._seen:
            return
        self._seen.add(msg_id)
        if len(self._seen) > 500:
            self._seen = set(list(self._seen)[-200:])

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

        # Strip Feishu @-mention placeholders so the agent never sees them.
        text = _AT_RE.sub("", raw_text).strip()
        if not text:
            return

        inbound = LarkInbound(
            open_id=open_id,
            text=text,
            message_id=msg_id,
            chat_type=message.chat_type or "p2p",
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

        self._loop = asyncio.get_event_loop()
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
            try:
                ws.start()
            except Exception as exc:
                if not self._closed:
                    logger.error("Feishu WS loop exited unexpectedly: %s", exc)

        self._thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        self._thread.start()
        logger.info("Feishu WebSocket receiver started (app_id=%s)", self._app_id)

    async def close(self) -> None:
        """Signal shutdown; the daemon thread exits once the WS closes."""
        self._closed = True
        ws = self._ws_client
        if ws is not None:
            try:
                ws.stop()
            except Exception:
                pass
        logger.info("Feishu client closed")
