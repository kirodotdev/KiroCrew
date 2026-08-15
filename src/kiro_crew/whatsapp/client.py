"""Low-level WhatsApp client for the channel: a thin adapter over neonize.

Owns the neonize ``NewAClient`` lifecycle and flattens its protobuf event
stream into the small set of callbacks the transport consumes. Everything
neonize is imported **lazily inside methods** — importing this module must
never load the bundled Go core, so a missing ``kirocrew[whatsapp]`` extra
can't break gateway boot (same discipline as ``weixin/client.py``).

Pairing state machine (read by the Settings badge + QR flow):

    unpaired -> pairing (QR codes rotating) -> paired/connected
    connected -> logged_out (phone revoked the link; needs a fresh pairing)
    connected -> banned (WhatsApp temporary ban; reason surfaced verbatim)

The session database (whatsmeow's sqlite store) lives at
``<data home>/whatsapp/session.db`` unless ``whatsapp.db_path`` overrides it.
Deleting the file unpairs the device from this side.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew.whatsapp.jids import OwnIdentity, jid_to_str

logger = logging.getLogger(__name__)

MISSING_EXTRA_HINT = (
    "The WhatsApp channel needs the optional dependency extra: "
    "pip install 'kirocrew[whatsapp]'"
)

STATE_UNPAIRED = "unpaired"
STATE_PAIRING = "pairing"
STATE_CONNECTED = "connected"
STATE_LOGGED_OUT = "logged_out"
STATE_BANNED = "banned"
STATE_ERROR = "error"

_INTER_CHUNK_DELAY_S = 0.4


def default_db_path(home: str | os.PathLike[str]) -> Path:
    return Path(home) / "whatsapp" / "session.db"


def neonize_available() -> bool:
    """True when the optional extra is importable. Checked WITHOUT loading
    the Go core: find_spec only touches import metadata."""
    from importlib.util import find_spec

    try:
        return find_spec("neonize") is not None
    except (ImportError, ValueError):
        return False


class WhatsAppClient:
    """Async adapter over ``neonize.aioze.client.NewAClient``.

    Callbacks (all optional, set before :meth:`connect`):
      on_message(event)              — raw neonize MessageEv (transport normalizes)
      on_state_change(state, detail) — pairing/connection badge updates
      on_qr(codes)                   — rotating QR code strings for the pairing UI
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        self.state: str = STATE_UNPAIRED
        self.state_detail: str = ""
        self.me = OwnIdentity()
        self.push_name: str = ""
        self.on_message: Callable[[Any], Awaitable[None]] | None = None
        self.on_state_change: Callable[[str, str], None] | None = None
        self.on_qr: Callable[[list[str]], None] | None = None
        self._client: Any = None
        self._idle_task: asyncio.Task | None = None
        self.connected_at: float | None = None
        #: latest rotating QR codes + monotonic stamp (Settings pairing UI).
        self.latest_qr: list[str] = []
        self.latest_qr_at: float = 0.0

    # -- state ---------------------------------------------------------

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.state_detail = detail
        logger.info("whatsapp: state -> %s%s", state, f" ({detail})" if detail else "")
        if self.on_state_change is not None:
            try:
                self.on_state_change(state, detail)
            except Exception:  # noqa: BLE001 — observer must never kill the loop
                logger.warning("whatsapp: state observer failed", exc_info=True)

    @property
    def is_connected(self) -> bool:
        return self.state == STATE_CONNECTED

    def session_exists(self) -> bool:
        """A session DB on disk means a pairing was completed at some point.
        (Whether it is still valid only shows up as LoggedOut on connect.)"""
        try:
            return Path(self.db_path).exists() and Path(self.db_path).stat().st_size > 0
        except OSError:
            return False

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        """Build the neonize client, register events, and start connecting.

        Returns once the connection attempt is underway; pairing/connection
        progress arrives via callbacks. Raises RuntimeError with an install
        hint when the optional extra is missing.
        """
        if not neonize_available():
            raise RuntimeError(MISSING_EXTRA_HINT)

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(Path(self.db_path).parent, 0o700)
        except OSError:
            logger.warning("whatsapp: could not restrict session dir permissions")

        # Lazy: these imports load the bundled Go core (ctypes.CDLL).
        from neonize.aioze.client import NewAClient
        from neonize.aioze.events import (
            ConnectedEv,
            DisconnectedEv,
            LoggedOutEv,
            MessageEv,
            PairStatusEv,
            QREv,
            TemporaryBanEv,
        )

        # The first positional arg doubles as the sqlite session-store path
        # (ClientFactory passes its database_name through the same slot).
        client = NewAClient(self.db_path)
        self._client = client

        @client.event(QREv)
        async def _on_qr(_client: Any, event: Any) -> None:
            import time as _time

            codes = list(getattr(event, "Codes", []) or [])
            self.latest_qr = codes
            self.latest_qr_at = _time.monotonic()
            self._set_state(STATE_PAIRING, "scan the QR code from your phone")
            if self.on_qr is not None and codes:
                try:
                    self.on_qr(codes)
                except Exception:  # noqa: BLE001
                    logger.warning("whatsapp: QR observer failed", exc_info=True)

        @client.event(PairStatusEv)
        async def _on_pair(_client: Any, event: Any) -> None:
            status = int(getattr(event, "Status", 0) or 0)
            if status == 2:  # SUCCESS
                self._set_state(STATE_CONNECTED, "paired")
            else:
                self._set_state(STATE_ERROR, str(getattr(event, "Error", "")) or "pairing failed")

        @client.event(ConnectedEv)
        async def _on_connected(_client: Any, _event: Any) -> None:
            import time

            self.connected_at = time.time()
            await self._load_identity()
            self._set_state(STATE_CONNECTED)

        @client.event(DisconnectedEv)
        async def _on_disconnected(_client: Any, _event: Any) -> None:
            # whatsmeow auto-reconnects; report without tearing state down.
            if self.state == STATE_CONNECTED:
                self._set_state(STATE_ERROR, "disconnected (auto-reconnecting)")

        @client.event(LoggedOutEv)
        async def _on_logged_out(_client: Any, event: Any) -> None:
            reason = str(getattr(event, "Reason", "") or "")
            self._set_state(STATE_LOGGED_OUT, reason or "device unlinked — re-pair from Settings")

        @client.event(TemporaryBanEv)
        async def _on_ban(_client: Any, event: Any) -> None:
            self._set_state(STATE_BANNED, f"temporary ban: {event}")

        @client.event(MessageEv)
        async def _on_message(_client: Any, event: Any) -> None:
            if self.on_message is None:
                return
            try:
                await self.on_message(event)
            except Exception:  # noqa: BLE001 — one bad message must not kill inbound
                logger.exception("whatsapp: inbound handler failed")

        if not self.session_exists():
            self._set_state(STATE_PAIRING, "waiting for first QR")
        await client.connect()
        self._idle_task = asyncio.get_running_loop().create_task(
            client.idle(), name="whatsapp-idle"
        )

    async def disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._idle_task = None
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.debug("whatsapp: stop() during disconnect failed", exc_info=True)
            self._client = None
        if self.state == STATE_CONNECTED:
            self._set_state(STATE_UNPAIRED if not self.session_exists() else STATE_ERROR,
                            "disconnected")

    async def logout(self) -> None:
        """Unlink this device (invalidates the session DB server-side)."""
        if self._client is None:
            return
        try:
            await self._client.logout()
        finally:
            self._set_state(STATE_LOGGED_OUT, "unlinked by operator")

    async def _load_identity(self) -> None:
        try:
            device = await self._client.get_me()
            self.me = OwnIdentity(
                jid=jid_to_str(getattr(device, "JID", None)),
                lid=jid_to_str(getattr(device, "LID", None)),
            )
            self.push_name = str(getattr(device, "PushName", "") or "")
        except Exception:  # noqa: BLE001 — identity is best-effort at connect
            logger.warning("whatsapp: get_me() failed", exc_info=True)

    # -- outbound -------------------------------------------------------

    async def send_text(self, jid_str: str, text: str) -> list[str]:
        """Send ``text`` to a chat, chunked; returns the message IDs sent.

        Caller records each ID with the echo tracker BEFORE the next await
        so the echo can never race the bookkeeping.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        from kiro_crew.whatsapp.renderer import render_chunks

        jid = self._parse_jid(jid_str)
        ids: list[str] = []
        for i, chunk in enumerate(render_chunks(text)):
            if i:
                await asyncio.sleep(_INTER_CHUNK_DELAY_S)
            response = await self._client.send_message(jid, chunk)
            message_id = str(getattr(response, "ID", "") or "")
            if message_id:
                ids.append(message_id)
        return ids

    async def send_typing(self, jid_str: str, active: bool) -> None:
        """Best-effort composing indicator (never raises)."""
        if self._client is None:
            return
        try:
            from neonize.utils.enum import ChatPresence, ChatPresenceMedia

            state = (
                ChatPresence.CHAT_PRESENCE_COMPOSING
                if active
                else ChatPresence.CHAT_PRESENCE_PAUSED
            )
            await self._client.send_chat_presence(
                self._parse_jid(jid_str), state, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )
        except Exception:  # noqa: BLE001 — presence is cosmetic
            logger.debug("whatsapp: send_chat_presence failed", exc_info=True)

    async def list_groups(self) -> list[dict]:
        """Joined groups as ``{"jid", "name"}`` dicts (Settings group picker)."""
        if self._client is None:
            return []
        out: list[dict] = []
        try:
            for info in await self._client.get_joined_groups():
                jid = jid_to_str(getattr(info, "JID", None))
                name = ""
                group_name = getattr(info, "GroupName", None)
                if group_name is not None:
                    name = str(getattr(group_name, "Name", "") or "")
                if jid:
                    out.append({"jid": jid, "name": name})
        except Exception:  # noqa: BLE001 — picker degrades to manual JID entry
            logger.warning("whatsapp: get_joined_groups failed", exc_info=True)
        return out

    def _parse_jid(self, jid_str: str) -> Any:
        from neonize.proto.Neonize_pb2 import JID

        from kiro_crew.whatsapp.jids import USER_SERVER, normalize_jid

        norm = normalize_jid(jid_str)
        user, _, server = norm.partition("@")
        return JID(User=user, Server=server or USER_SERVER)
