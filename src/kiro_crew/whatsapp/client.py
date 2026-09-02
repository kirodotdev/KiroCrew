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
import mimetypes
import os
import wave
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew import extras
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime
from kiro_crew.platform_compat import make_owner_only_dir, restrict_to_owner
from kiro_crew.whatsapp.jids import OwnIdentity, jid_to_str

logger = logging.getLogger(__name__)

MISSING_EXTRA_HINT = (
    f"The WhatsApp channel needs its optional dependency: {extras.install_hint('whatsapp')}"
)

STATE_UNPAIRED = "unpaired"
STATE_PAIRING = "pairing"
STATE_CONNECTED = "connected"
STATE_LOGGED_OUT = "logged_out"
STATE_BANNED = "banned"
STATE_ERROR = "error"

_INTER_CHUNK_DELAY_S = 0.4

#: How long WhatsApp accepts an edit to a sent message. whatsmeow pins
#: ``EditWindow = 20 * time.Minute``; neither it nor neonize ENFORCES it, so a
#: later edit is refused by the server rather than rejected locally. A streaming
#: reply that outlives this has to stop editing and open a new bubble.
EDIT_WINDOW_S = 20 * 60

#: Longest edge (px) of the inline preview WhatsApp shows in the bubble and the
#: chat list before the recipient downloads the full picture. Pillow's
#: ``thumbnail`` fits the image inside the box and keeps its aspect ratio, so one
#: number covers both axes. Matches what neonize's own image build produces, so
#: assembling the protobuf here does not change how a sent image looks.
_THUMBNAIL_EDGE_PX = 200

#: Type put on image bytes whose leading bytes match no raster signature. Only
#: reached when the caller declared no type either; the bytes still go out,
#: because a picture the recipient's client will decode for itself is worth more
#: than a refused send over a label.
_DEFAULT_IMAGE_MIMETYPE = "image/jpeg"

#: Type put on document bytes when neither the caller nor the filename says. The
#: recipient's client then offers a plain download, which is the honest outcome
#: for bytes nothing has identified.
_DEFAULT_DOCUMENT_MIMETYPE = "application/octet-stream"


def default_db_path(home: str | os.PathLike[str]) -> Path:
    return Path(home) / "whatsapp" / "session.db"


def _image_thumbnail(data: bytes) -> bytes:
    """A small JPEG preview of *data*, or ``b""`` when one cannot be made.

    BLOCKING: a full raster decode plus a rescale and a JPEG encode, on inputs up
    to this channel's per-file ceiling. Callers hand it to a worker thread, since
    doing it inline stalls every other channel, every live turn and the liveness
    heartbeat on the one gateway event loop.

    Best-effort: a missing preview costs a grey placeholder until the recipient
    taps the picture, while raising would cost the whole reply.

    Pillow is imported here rather than at module scope, matching the discipline
    the rest of this module keeps for neonize: ``whatsapp`` is reachable from the
    gateway's import graph, so a top-level import lands on every operator's boot
    path whether or not this channel is enabled.
    """
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            img.thumbnail((_THUMBNAIL_EDGE_PX, _THUMBNAIL_EDGE_PX))
            # JPEG has no alpha channel and no palette, so anything else is
            # converted rather than handed to the encoder to reject.
            frame = img if img.mode == "RGB" else img.convert("RGB")
            preview = BytesIO()
            frame.save(preview, format="JPEG")
            return preview.getvalue()
    except Exception:  # noqa: BLE001 (a missing preview must not fail the send)
        logger.debug("whatsapp: could not build an image preview", exc_info=True)
        return b""


def _wav_seconds(data: bytes) -> int:
    """Whole seconds of PCM WAV in *data*, or 0 when it is not readable WAV.

    WhatsApp draws a voice note's duration from ``AudioMessage.seconds`` and
    labels it ``0:00`` without one. neonize derives that figure by shelling out to
    ``ffmpeg``, which this channel cannot require, so the one container the
    bundled speech synthesis emits is read from its own header instead: Piper
    writes PCM WAV, and frames divided by the frame rate is the whole answer.
    Any other container (a Polly MP3) answers 0, which costs the label alone.

    A header parse over an in-memory buffer, so it holds no descriptor and does
    no I/O.
    """
    try:
        with wave.open(BytesIO(data), "rb") as handle:
            rate = handle.getframerate()
            return int(handle.getnframes() / rate) if rate else 0
    except Exception:  # noqa: BLE001 (a missing duration is cosmetic)
        return 0


def _image_mimetype(data: bytes, declared: str = "") -> str:
    """The type to put on outbound image bytes.

    *declared* wins when the caller has one, because it comes from the same
    leading-bytes sniff the outbound file gate already ran over these exact
    bytes. Sniffing again here only covers a caller that has none.
    """
    return declared or sniff_raster_mime(bytes(data[:SNIFF_BYTES])) or _DEFAULT_IMAGE_MIMETYPE


def _document_mimetype(filename: str, declared: str = "") -> str:
    """The type to put on outbound document bytes.

    Derived from the filename rather than from the bytes: a document is any type
    at all, so there is no signature table to consult, and ``mimetypes`` reads
    only the extension.
    """
    return declared or mimetypes.guess_type(filename)[0] or _DEFAULT_DOCUMENT_MIMETYPE


def neonize_available() -> bool:
    """True when the optional extra is importable. Checked WITHOUT loading
    the Go core: find_spec only touches import metadata."""
    try:
        return find_spec("neonize") is not None
    except (ImportError, ValueError):
        return False


def _load_neonize() -> "tuple[Any, dict[str, Any]]":
    """Import neonize and return ``(NewAClient, {event name: class})``.

    Sync on purpose: :meth:`WhatsAppClient.connect` hands it to
    ``asyncio.to_thread`` because the import is heavy enough to stall the loop.
    Kept out of module scope so importing this module never touches the Go core.
    """
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import (
        ConnectedEv,
        DisconnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
        TemporaryBanEv,
    )

    return NewAClient, {
        "ConnectedEv": ConnectedEv,
        "DisconnectedEv": DisconnectedEv,
        "LoggedOutEv": LoggedOutEv,
        "MessageEv": MessageEv,
        "PairStatusEv": PairStatusEv,
        "TemporaryBanEv": TemporaryBanEv,
    }


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

    def _restrict_session_store(self) -> None:
        """Make the session directory (and any existing store) owner-only.

        Sync, so :meth:`connect` can hand it to a worker thread. Raises on
        failure: continuing would pair the device and leave the linked-device
        credential readable by another local principal, which is the case a
        warning nobody reads does not cover.
        """
        make_owner_only_dir(Path(self.db_path).parent)
        if Path(self.db_path).exists():
            # An existing store predates this call and may carry looser modes from
            # an earlier version, so it is tightened rather than trusted.
            restrict_to_owner(self.db_path)

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

        # The session store IS the linked-device credential: anything that can
        # read it can act as the operator on WhatsApp. `os.chmod(0o700)` is a
        # silent no-op on Windows, so this goes through the shim that also sets
        # inheritable owner-only ACLs, and it fails loud rather than warn-and-continue.
        # FAIL LOUD, not warn-and-continue. This directory holds the linked-device
        # session, which IS the credential: anything that can read it can act as
        # the operator on WhatsApp, read every chat and send as them. Continuing
        # after the restriction failed would pair the device and leave that
        # credential readable by any other local principal, which is exactly the
        # case a warning nobody reads does not cover.
        # Off the loop: these are blocking filesystem calls (the Windows DACL is
        # applied in-process), and the one event loop also carries every other
        # channel and the liveness heartbeat. Still awaited rather than
        # fire-and-forget, because pairing must not begin until the credential's
        # directory is actually locked down.
        await asyncio.to_thread(self._restrict_session_store)

        # Loading neonize means a ~19 MB ctypes.CDLL plus dozens of protobuf
        # descriptor modules, all synchronous. On the gateway's single event loop
        # that stalls every other channel, every live turn and the liveness
        # heartbeat for the duration, so it is done on a worker thread. Still
        # LAZY (never at module import) so a missing extra cannot break boot.
        NewAClient, events = await asyncio.to_thread(_load_neonize)
        ConnectedEv = events["ConnectedEv"]
        DisconnectedEv = events["DisconnectedEv"]
        LoggedOutEv = events["LoggedOutEv"]
        MessageEv = events["MessageEv"]
        PairStatusEv = events["PairStatusEv"]
        TemporaryBanEv = events["TemporaryBanEv"]

        # Snapshot BEFORE NewAClient: constructing the client creates the sqlite
        # store (schema and all), so a `session_exists()` asked afterwards is
        # ALWAYS true and the first-boot pairing state would never be announced.
        had_session = self.session_exists()

        # The first positional arg doubles as the sqlite session-store path
        # (ClientFactory passes its database_name through the same slot).
        client = NewAClient(self.db_path)
        self._client = client

        # QR does NOT travel on the numbered event stream, so it cannot be
        # registered with `client.event(...)`: `Event.execute` only dispatches
        # INT_TO_EVENT codes into `list_func`, and QR is never one of them. The Go
        # core delivers each rotating code by calling `NewAClient.__onQr`, which
        # invokes `client.event._qr` directly, and `Event.__init__` pre-seeds that
        # slot with a default that renders the code to a terminal — which a
        # gateway started from a desktop launcher has no reader for. `client.qr()`
        # is the supported registration, and it hands over ONE code as bytes per
        # emission rather than a batch carrying `.Codes`.
        async def _on_qr(_client: Any, data_qr: Any) -> None:
            import time as _time

            code = (
                data_qr.decode("utf-8", "replace") if isinstance(data_qr, bytes) else str(data_qr)
            )
            codes = [code] if code else []
            self.latest_qr = codes
            self.latest_qr_at = _time.monotonic()
            self._set_state(STATE_PAIRING, "scan the QR code from your phone")
            if self.on_qr is not None and codes:
                try:
                    self.on_qr(codes)
                except Exception:  # noqa: BLE001
                    logger.warning("whatsapp: QR observer failed", exc_info=True)

        client.qr(_on_qr)

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

        if not had_session:
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
            self._set_state(
                STATE_UNPAIRED if not self.session_exists() else STATE_ERROR, "disconnected"
            )

    async def logout(self) -> None:
        """Unlink this device (invalidates the session DB server-side).

        RAISES when there is no live client, rather than returning quietly. The
        caller deletes the session store on success, and that store is the only
        credential that can revoke this device: returning as though the unlink had
        happened would destroy it while the device stays linked on WhatsApp's
        side, still receiving and able to send. Failing loudly is what lets the
        endpoint keep the store and say so.

        The state moves only on SUCCESS, so a refused logout does not report the
        device as revoked. It is the third thing on this path that must not lie
        about the same failure: the endpoint keeps the store and answers 502, and
        the panel renders this state as its badge, so setting it here would show
        "the link was revoked" for a device that is still linked and still able to
        send. There is nothing to unwind on the failure path, so there is nothing
        a ``finally`` is needed for.
        """
        if self._client is None:
            raise RuntimeError("whatsapp: no live client, so nothing was unlinked")
        await self._client.logout()
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

    async def phone_for_lid(self, lid_jid: str) -> str:
        """The ``user@s.whatsapp.net`` JID behind an ``@lid`` alias, or ``""``.

        WhatsApp multi-device increasingly addresses a sender by their Linked
        Identity (``<id>@lid``) rather than their phone number, and the two
        user parts are UNRELATED strings. An operator configures an allowlist
        with phone numbers, so without this resolution a lid-addressed sender
        never matches and the channel silently ignores a person the operator
        explicitly allowed. It fails closed, which is the safe direction but
        reads to the operator as the channel being broken.

        Best-effort by design: a lookup failure returns ``""`` and the caller
        keeps the lid form, so a resolver outage degrades to today's behaviour
        (deny) rather than to an open door.
        """
        if self._client is None or not lid_jid:
            return ""
        try:
            resolved = await self._client.get_pn_from_lid(self._parse_jid(lid_jid))
        except Exception:  # noqa: BLE001 — alias resolution is best-effort
            logger.debug("whatsapp: get_pn_from_lid(%s) failed", lid_jid, exc_info=True)
            return ""
        return jid_to_str(resolved)

    # -- outbound -------------------------------------------------------

    async def send_text(
        self,
        jid_str: str,
        text: str,
        on_sent: "Callable[[str], None] | None" = None,
    ) -> list[str]:
        """Send ``text`` to a chat, chunked; returns the message IDs sent.

        ``on_sent`` is invoked with each message id the moment that chunk's send
        returns, before any further await. The echo tracker is what it is for:
        recording ids only from the returned list would leave every chunk but the
        last unremembered while the loop sleeps between sends.

        Each chunk is wrapped in an explicit ``Message(conversation=...)`` rather
        than handed over as a ``str``. neonize treats a bare string as something
        to INTERPRET: it runs ``_parse_mention`` over it, so any ``@<digits>``
        the model happened to write becomes a real WhatsApp mention of that
        number, and in a group that notifies whoever owns it. Reply text is not
        trustworthy input -- a prompt-injected agent chooses what it writes --
        and the same branch also does a group-mention lookup on every send.
        Passing the protobuf skips both.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        from kiro_crew.whatsapp.renderer import render_chunks_off_loop

        jid = self._parse_jid(jid_str)
        ids: list[str] = []
        for i, chunk in enumerate(await render_chunks_off_loop(text)):
            if i:
                await asyncio.sleep(_INTER_CHUNK_DELAY_S)
            response = await self._client.send_message(jid, Message(conversation=chunk))
            message_id = str(getattr(response, "ID", "") or "")
            if message_id:
                ids.append(message_id)
                # Handed over BEFORE the next await, which is the whole point of
                # the callback. Returning the list and letting the caller record
                # it afterwards leaves every chunk but the last untracked across
                # the inter-chunk sleep below, and WhatsApp delivers the echo of
                # chunk 1 during exactly that window: the echo gate then reads it
                # as the operator typing and feeds the agent its own output.
                if on_sent is not None:
                    on_sent(message_id)
        return ids

    async def download_media(self, message: Any) -> bytes:
        """Decrypted bytes for a media message.

        WhatsApp media is end-to-end encrypted and only the paired client holds
        the keys, so there is no URL another layer could fetch. Takes the
        UNWRAPPED message: the outer carrier (ephemeral, view-once, edited) has no
        media submessage, so handing it the envelope silently downloads nothing.

        The neonize call is already dispatched to a worker thread by its own async
        wrapper, so awaiting it does not hold the loop.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        return await self._client.download_any(message)

    async def send_image_bytes(
        self,
        jid_str: str,
        data: bytes,
        caption: str = "",
        on_sent: "Callable[[str], None] | None" = None,
        *,
        mimetype: str = "",
    ) -> str:
        """Upload an image from BYTES and return its message id.

        Bytes, never a path, and this is a contract rather than a preference:
        ``messaging/outbound_files`` applied every gate (raster sniff by leading
        bytes, the sensitive-path denylist, the symlink and root checks) to one
        inode, and anything able to write that directory in between (another
        turn, a subagent, a cron) could substitute what a re-opened path names.

        The protobuf is assembled here rather than handed to neonize's
        ``send_image``, for the two reasons :meth:`send_text` builds its own
        ``Message``:

        * ``build_image_message`` runs ``_parse_mention`` over the CAPTION and
          puts the result in ``contextInfo.mentionedJID``, so an ``@<digits>`` run
          in the caption becomes a real WhatsApp mention of that number and in a
          group notifies whoever owns it. The caption is agent-authored markdown
          alt text, which is untrusted: a prompt-injected agent chooses what it
          writes. The same branch also does a group-mention lookup per send.
        * that helper decodes and rescales the whole raster INLINE on the event
          loop to build the preview. Here that work goes to a worker thread and
          only the upload and the send are awaited.

        The id is handed to ``on_sent`` before returning, for the same reason
        :meth:`send_text` does it: the echo of an upload arrives on the event
        stream like any other message the account sent.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo, ImageMessage, Message
        from neonize.utils.enum import MediaType

        preview = await asyncio.to_thread(_image_thumbnail, data)
        # The media type is declared rather than left to neonize's libmagic
        # probe, which would run on the loop before the upload is dispatched.
        upload = await self._client.upload(data, MediaType.MediaImage)
        message = Message(
            imageMessage=ImageMessage(
                URL=upload.url,
                caption=caption or None,
                directPath=upload.DirectPath,
                fileEncSHA256=upload.FileEncSHA256,
                fileLength=upload.FileLength,
                fileSHA256=upload.FileSHA256,
                mediaKey=upload.MediaKey,
                mimetype=_image_mimetype(data, mimetype),
                JPEGThumbnail=preview,
                thumbnailDirectPath=upload.DirectPath,
                thumbnailEncSHA256=upload.FileEncSHA256,
                thumbnailSHA256=upload.FileSHA256,
                # Empty on purpose, and the whole point of building this here:
                # an empty mention list is what keeps the caption inert.
                contextInfo=ContextInfo(),
            )
        )
        return await self._send_built(jid_str, message, on_sent)

    async def send_voice_bytes(
        self,
        jid_str: str,
        data: bytes,
        *,
        mimetype: str,
        seconds: int = 0,
        on_sent: "Callable[[str], None] | None" = None,
    ) -> str:
        """Send audio BYTES as a voice note (push-to-talk) and return its id.

        The channel transcribes an inbound voice note, so being unable to send
        one is an asymmetry the operator feels: they talk to the agent and it
        cannot answer in kind. ``PTT=True`` is what makes the recipient's client
        draw a play button and a waveform instead of a file attachment.

        *mimetype* is required, not sniffed: audio has no signature table this
        repo carries, and the caller minted these bytes so it knows the container.
        **WhatsApp's own voice notes are ``audio/ogg; codecs=opus``**, and a
        client may decline to play a push-to-talk message in another container;
        the bundled speech synthesis emits MP3 or WAV, so a caller that needs
        certainty has to transcode before calling.

        *seconds* is the duration the recipient sees. Passing it is preferred; 0
        falls back to :func:`_wav_seconds`, which answers only for PCM WAV.

        Assembled here rather than through neonize's ``send_audio`` for a
        different reason than :meth:`send_image_bytes`: that helper has no caption
        to misread, but it shells out to ``ffmpeg`` for the duration, so a host
        without ffmpeg cannot send a voice note at all.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message
        from neonize.utils.enum import MediaType

        upload = await self._client.upload(data, MediaType.MediaAudio)
        message = Message(
            audioMessage=AudioMessage(
                URL=upload.url,
                directPath=upload.DirectPath,
                fileEncSHA256=upload.FileEncSHA256,
                fileLength=upload.FileLength,
                fileSHA256=upload.FileSHA256,
                mediaKey=upload.MediaKey,
                mimetype=mimetype,
                seconds=seconds or _wav_seconds(data),
                PTT=True,
            )
        )
        return await self._send_built(jid_str, message, on_sent)

    async def send_document_bytes(
        self,
        jid_str: str,
        data: bytes,
        filename: str,
        *,
        mimetype: str = "",
        caption: str = "",
        on_sent: "Callable[[str], None] | None" = None,
    ) -> str:
        """Upload a non-raster file from BYTES as a document; return its id.

        Bytes rather than a path for the same contract as
        :meth:`send_image_bytes`, and assembled locally for the same first
        reason: neonize's ``build_document_message`` runs its mention parser over
        the caption too.

        Only the BASENAME of *filename* travels. WhatsApp shows the recipient
        whatever ``fileName`` carries, and the caller's name is a real path on the
        operator's machine, so sending it whole would put their directory layout
        in someone else's chat.
        """
        if self._client is None:
            raise RuntimeError("whatsapp client is not connected")
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo, DocumentMessage, Message
        from neonize.utils.enum import MediaType

        name = Path(filename).name
        upload = await self._client.upload(data, MediaType.MediaDocument)
        message = Message(
            documentMessage=DocumentMessage(
                URL=upload.url,
                caption=caption or None,
                directPath=upload.DirectPath,
                fileEncSHA256=upload.FileEncSHA256,
                fileLength=upload.FileLength,
                fileSHA256=upload.FileSHA256,
                mediaKey=upload.MediaKey,
                mimetype=_document_mimetype(name, mimetype),
                fileName=name,
                # What the recipient's client shows when it renders no filename.
                title=name,
                contextInfo=ContextInfo(),
            )
        )
        return await self._send_built(jid_str, message, on_sent)

    async def _send_built(
        self,
        jid_str: str,
        message: Any,
        on_sent: "Callable[[str], None] | None" = None,
    ) -> str:
        """Send an already-assembled protobuf and report the id it was given.

        One chokepoint for the three media sends, so the echo-tracker handover
        cannot be present on one of them and missing on another: an id the
        tracker never saw comes back on the event stream as the operator typing,
        and the channel answers its own upload.
        """
        response = await self._client.send_message(self._parse_jid(jid_str), message)
        message_id = str(getattr(response, "ID", "") or "")
        if message_id and on_sent is not None:
            on_sent(message_id)
        return message_id

    async def edit_text(self, jid_str: str, message_id: str, text: str) -> bool:
        """Replace the body of a message this channel already sent.

        WhatsApp allows an edit for :data:`EDIT_WINDOW_S` after the original send
        and the server refuses a later one, so a long turn eventually has to stop
        editing and open a new bubble instead. That decision belongs to the
        caller, which knows when the bubble was sent; this only reports failure.

        Returns False instead of raising: a refused edit is a cosmetic loss (the
        reader keeps the previous text and the final send still lands), and it
        must never fail the turn that was merely trying to show progress.
        """
        if self._client is None:
            return False
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        try:
            await self._client.edit_message(
                self._parse_jid(jid_str), message_id, Message(conversation=text)
            )
            return True
        except Exception:  # noqa: BLE001: progress rendering is best-effort
            logger.debug("whatsapp: edit of %s failed", message_id, exc_info=True)
            return False

    async def react(self, jid_str: str, sender_jid: str, message_id: str, emoji: str) -> str:
        """React to a message; returns the reaction's own message id, or ``""``.

        ``emoji=""`` removes this account's reaction. The id is RETURNED rather
        than discarded because a reaction is itself a message: it echoes back with
        ``from_me`` set, and the caller has to hand it to the echo tracker or the
        channel answers its own receipt.

        Best-effort like :meth:`edit_text`: a receipt that cannot be drawn must
        not take the turn down with it.
        """
        if self._client is None:
            return ""
        try:
            chat = self._parse_jid(jid_str)
            built = await self._client.build_reaction(
                chat, self._parse_jid(sender_jid), message_id, emoji
            )
            response = await self._client.send_message(chat, built)
            return str(getattr(response, "ID", "") or "")
        except Exception:  # noqa: BLE001: receipts are cosmetic
            logger.debug("whatsapp: reaction on %s failed", message_id, exc_info=True)
            return ""

    async def mark_read(self, jid_str: str, sender_jid: str, message_id: str) -> bool:
        """Mark one inbound message read (the blue ticks the sender sees).

        Deliberately opt-in at the CALLER: this writes to the operator's own
        account and is visible to the other party, so it overrides whatever
        read-receipt privacy setting their phone carries. Best-effort.
        """
        if self._client is None or not message_id:
            return False
        try:
            from neonize.utils.enum import ReceiptType

            await self._client.mark_read(
                message_id,
                chat=self._parse_jid(jid_str),
                sender=self._parse_jid(sender_jid),
                receipt=ReceiptType.READ,
            )
            return True
        except Exception:  # noqa: BLE001: receipts are cosmetic
            logger.debug("whatsapp: mark_read failed", exc_info=True)
            return False

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
        # The JID proto marks RawAgent/Device/Integrator as required, so a
        # two-field JID raises EncodeError at neonize's FFI boundary and every
        # outbound call fails. Mirror neonize.utils.jid.build_jid: zero them.
        return JID(
            User=user,
            RawAgent=0,
            Device=0,
            Integrator=0,
            Server=server or USER_SERVER,
        )
