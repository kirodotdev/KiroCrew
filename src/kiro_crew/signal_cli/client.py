"""The signal-cli client -- Signal semantics over the JSON-RPC peer.

Owns everything that is true of Signal rather than of JSON-RPC: spawning
``signal-cli -a <account> jsonRpc``, the readiness probe, classifying inbound
``receive`` envelopes, outbound ``send`` (including Note to Self and file
attachments), fetching a received attachment's bytes (``getAttachment``), the
typing indicator, read receipts and status reactions, and recovering when the
child exits.

signal-cli's ``jsonRpc`` mode receives on start (``--receive-mode=on-start``,
its default), so every inbound message arrives as an unsolicited notification::

    {"method": "receive", "params": {"envelope": {...}, "account": "+44..."}}

and, under ``--receive-mode=manual`` + ``subscribeReceive``, wrapped one level
deeper as ``params.result.envelope``. Both shapes are read. Signal itself
queues messages server-side while the gateway is down and signal-cli fetches
them on the next start, so no cursor is kept here; delivery past that point is
at-most-once, matching every other channel.

Identities are numbers OR ACIs. An account may be numberless, in which case
``sourceNumber`` is null and only ``sourceUuid`` names the sender, so the
allow-list matches either, and a reply goes to the number when there is one and
to the ACI otherwise.

**Note to Self.** When signal-cli is a linked device of the owner's own account,
a message the owner writes to themselves arrives as a ``syncMessage.sentMessage``
whose destination is the account itself. That is the owner's input; a
``sentMessage`` to anyone ELSE is the owner's own outgoing message to a third
party and is never input to the agent. Replies go back with ``noteToSelf``.

**Own-message suppression** follows the iMessage client's ledger design: a
record of what this client sent, keyed by both a digest of the body and the
timestamp signal-cli reports for it, consumed whole on a match, with a TTL that
starts only once the send resolves. In this channel the only route by which the
agent's own reply can come back as input is the self-addressed one (signal-cli
does not deliver its own sends back to itself, but another device on the same
account can re-sync them), so the ledger is consulted for self-addressed
traffic only and a DM from another person can never be swallowed by it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from kiro_crew.signal_cli.bridge_path import (
    BRIDGE_BINARY,
    describe_lookup,
    resolve_bridge_path,
)

# Re-exported: the channel's modules, doctor and the tests read these from here.
from kiro_crew.signal_cli.identifiers import (  # noqa: F401
    _E164_RE,
    _UUID_RE,
    account_identifier,
    coerce_identifier,
    identifier_problem,
    normalize_identifier,
    redact_identifier,
)
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError, SignalRpcPeer

logger = logging.getLogger(__name__)

#: How long a self-addressed message this client sent stays recognisable as its
#: own echo. Same balance as iMessage's ECHO_TTL_S: long enough to outlive a
#: delivery round trip, short enough that a genuine repeat is not swallowed.
ECHO_TTL_S = 30.0

#: Bound on remembered outbound messages (entries, not turns).
ECHO_WINDOW = 256

#: Remembered ``(sender, timestamp)`` pairs, so a re-delivered envelope is not
#: answered twice. Signal timestamps are the sender's message id.
DEDUPE_WINDOW = 1024

#: Backoff bounds for respawning signal-cli after it exits on its own. The loop
#: WAITS before every attempt, including the first: a child that died on start
#: (an unregistered account, a locked data dir) dies again at once, and an
#: immediate respawn is a tight loop.
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0

#: How long signal-cli must stay up before an exit counts as a fresh failure.
#: The backoff delay is carried ACROSS restarts and only reset after this much
#: uptime, so a child that passes its probe and then dies seconds later (a held
#: account lock, a crash on the first message) still backs off to the cap rather
#: than restarting every two seconds forever.
STABLE_UPTIME_S = 300.0

#: Error codes that mean "this method is not available here" -- the only ones
#: that switch the optional typing indicator / read receipts off for good. A
#: timeout or a transport blip says nothing about the method and is retried on
#: the next turn.
_UNSUPPORTED_CODES = frozenset({-32601, -32602})

#: signal-cli's error code for a send refused because a recipient's safety
#: number changed (untrusted identity).
ERR_UNTRUSTED_IDENTITY = -4

#: The readiness probe's bound. Generous because the JVM build of signal-cli can
#: take many seconds to start; the native build answers in well under one.
PROBE_TIMEOUT_S = 60.0

#: JSON-RPC / signal-cli error codes that PROVE a send never left the device.
#: -32602 invalid params and -32601 unknown method are rejected before dispatch;
#: signal-cli maps a send that failed for EVERY recipient to -1 (user error),
#: untrusted identities to -4, rate limiting to -5 and a rejected captcha to -6.
_DEFINITIVE_REJECTIONS = frozenset({-32602, -32601, -1, -4, -5, -6})

#: Bound on one ``getAttachment`` round trip. The answer is the whole file,
#: base64-encoded on one line, so it is allowed longer than a send.
ATTACHMENT_FETCH_TIMEOUT_S = 60.0

#: The ledger's clock, bound once at module level so a test can freeze it here
#: without touching the process-wide ``time.monotonic`` the event loop reads.
_monotonic = time.monotonic

#: The reconnect backoff's sleep, bound here for the same reason: a test can
#: observe the delays without patching ``asyncio.sleep`` process-wide.
_sleep = asyncio.sleep

KIND_DATA = "data"
KIND_NOTE_TO_SELF = "note_to_self"
KIND_SYNC_OTHER = "sync_other"
KIND_OTHER = "other"


def _echo_key(text: str) -> str:
    # NUL is dropped so a body key can never spell an attachment key, which
    # starts with one (``_ATTACHMENT_KEY_PREFIX``).
    return unicodedata.normalize("NFC", text or "").replace("\x00", "").strip()


def _echo_digest(key: str) -> bytes:
    """The fixed-size form of an echo key the ledger holds, ``b""`` for no key.

    The ledger keeps a digest, never the body: a record is the same 32 bytes for
    a one-word reply and a long answer, so the ledger's memory is bounded by its
    entry cap alone, and no message text outlives its send in it. Equal keys
    have equal digests, so matching is unchanged; an empty key stays empty and
    still never matches. ``surrogatepass`` because a JSON body can carry a lone
    surrogate, which strict UTF-8 refuses to encode.
    """
    if not key:
        return b""
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()


#: The content type Signal gives a message body too long to send inline. The
#: recipient's signal-cli reads that attachment back into ``message`` itself, so
#: the entry is the body a second time, never a file the person sent.
LONG_TEXT_CONTENT_TYPE = "text/x-signal-plain"

#: Marks an attachment-only echo key. It starts with NUL, which ``_echo_key``
#: removes from every body, so no typed message can equal one.
_ATTACHMENT_KEY_PREFIX = "\x00attachments:"


def _base_content_type(value: object) -> str:
    return _str(value).split(";", 1)[0].strip().lower()


def _attachment_echo_key(parts: Iterable[tuple[str, int]]) -> str:
    """The echo key of an attachment-only message: each file's type and size, in order.

    Not a hash of the bytes: the re-synced copy of a send carries only an id,
    the content type and the plaintext size of each attachment, never its data,
    so the key is built from what BOTH sides can see. ``""`` when there are no
    parts (an empty key never matches).
    """
    listed = [f"{ctype}={size}" for ctype, size in parts]
    if not listed:
        return ""
    return f"{_ATTACHMENT_KEY_PREFIX}{len(listed)}:" + "|".join(listed)


def _data_uri_part(uri: str) -> tuple[str, int]:
    """``(content type, decoded byte count)`` of one ``data:`` URI we send.

    The size is computed from the base64 length, without decoding the payload.
    A string that is not a base64 data URI yields ``("", 0)``.
    """
    head, sep, payload = uri.partition(",")
    if not sep or not head.startswith("data:") or not head.endswith(";base64"):
        return "", 0
    ctype = _base_content_type(head[len("data:") :])
    payload = payload.strip()
    size = len(payload) * 3 // 4 - (len(payload) - len(payload.rstrip("=")))
    return ctype, max(size, 0)


def outbound_echo_key(text: str, attachments: Sequence[str] = ()) -> str:
    """The ledger key of an outbound send: its body, else its attachments' shape."""
    return _echo_key(text) or _attachment_echo_key(_data_uri_part(a) for a in attachments)


@dataclass(frozen=True)
class SignalAttachmentRef:
    """One entry of a message's ``attachments[]``, as signal-cli reports it.

    signal-cli downloads the attachment itself while it receives and names the
    stored copy by :attr:`id` (``<remote id><.ext>`` under its data dir); the
    bytes are fetched with ``getAttachment`` by that id, so the channel never
    has to know where signal-cli keeps its data. Every other field is the
    SENDER's claim: ``size`` and ``content_type`` are advisory, ``filename`` is
    untrusted input and is only ever used as a display name.
    """

    id: str
    content_type: str = ""
    filename: str = ""
    size: int = 0
    voice_note: bool = False


@dataclass
class SignalInbound:
    """One classified ``receive`` envelope, as the transport and dispatcher need it."""

    kind: str
    text: str = ""
    source_number: str = ""
    source_uuid: str = ""
    source_name: str = ""
    #: The message's own timestamp (ms) -- Signal's message id.
    timestamp: int = 0
    is_group: bool = False
    attachments: int = 0
    #: The fetchable attachments (entries with an id), in the order sent.
    attachment_refs: list[SignalAttachmentRef] = field(default_factory=list)
    #: Every attachment's ``(content type, size)``, in the order sent, for the
    #: echo ledger's attachment-only key (:func:`outbound_echo_key`).
    attachment_shape: tuple[tuple[str, int], ...] = ()
    #: A reaction to some message (``dataMessage.reaction``). Never input: a
    #: reaction carries no body, and this channel's own status reactions come
    #: back this way in Note to Self.
    is_reaction: bool = False
    #: The body as the person sent it, kept by the transport before it rewrites
    #: :attr:`text` with attachment context; ``None`` while it was not rewritten.
    original_text: str | None = None
    #: The decrypted attachment copies staged for this message, which it owns.
    #: The transport deletes whatever is still listed once the dispatch returns;
    #: a dispatcher that hands them to another owner (the running turn a
    #: message is steered into) empties this list.
    temp_paths: list[str] = field(default_factory=list)
    #: The account this envelope was received on (as configured).
    account: str = ""
    #: Set by the transport on a message a person sent (FOREGROUND start).
    person_origin: bool = False
    #: Set by the transport once authorized: the identity that matched the
    #: allow-list. The reply goes there, so the conversation is always one the
    #: roster names (a sender listed by ACI is answered by ACI).
    principal: str = ""
    #: Set by the transport with :attr:`principal`: whether that identity is the
    #: operator (``SignalTransport.is_operator``), the narrower set that may
    #: approve tools, run session-acting commands and steer a running turn.
    operator: bool = False

    @property
    def spool_text(self) -> str:
        """What the person wrote, without the attachment context or temp paths.

        The durable inbound spool quotes this back in a restart notice, and the
        ingested form names on-disk temp files that are gone after a restart.
        """
        return self.text if self.original_text is None else self.original_text

    @property
    def sender(self) -> str:
        """The sender's number when it has one, else its ACI."""
        return self.source_number or self.source_uuid

    @property
    def sender_ids(self) -> frozenset[str]:
        return frozenset(
            n for v in (self.source_number, self.source_uuid) if (n := normalize_identifier(v))
        )

    @property
    def reply_to(self) -> str:
        """Where the answer goes: back to the sender, or to Note to Self."""
        if self.kind == KIND_NOTE_TO_SELF:
            return self.account or self.sender
        return self.sender

    @property
    def conversation_id(self) -> str:
        """The normalized identity the session and the outbound link key off."""
        return normalize_identifier(self.principal or self.reply_to)


InboundHandler = Callable[[SignalInbound], Awaitable[None]]


def receive_envelope(params: object) -> dict[str, Any] | None:
    """Extract the envelope from a ``receive`` notification's params.

    ``params.envelope`` under the default on-start receive mode;
    ``params.result.envelope`` under ``subscribeReceive``.
    """
    if not isinstance(params, dict):
        return None
    envelope = params.get("envelope")
    if isinstance(envelope, dict):
        return envelope
    result = params.get("result")
    if isinstance(result, dict) and isinstance(result.get("envelope"), dict):
        return result["envelope"]
    return None


def parse_receive(
    params: object, account: str = "", self_ids: Iterable[str] = ()
) -> SignalInbound | None:
    """Classify one ``receive`` notification. ``None`` when it carries no envelope.

    *self_ids* are further identities of the account (its number or ACI where
    the configured *account* spells only the other), so a Note to Self whose
    destination names the account by the form the config does not use is still
    recognised.

    Every read is typed with a fallback: signal-cli writes null for absent
    numbers (a numberless account), and a field of the wrong type is treated as
    absent rather than crashing the reader.
    """
    envelope = receive_envelope(params)
    if envelope is None:
        return None
    acct = account
    if not acct and isinstance(params, dict):
        acct = _str(params.get("account"))
        if not acct and isinstance(params.get("result"), dict):
            acct = _str(params["result"].get("account"))
    number = _str(envelope.get("sourceNumber"))
    uuid = _str(envelope.get("sourceUuid"))
    legacy = _str(envelope.get("source"))
    if legacy and not (number or uuid):
        # ``source`` is the deprecated legacy identifier: a number when there is
        # one, else the ACI.
        if _UUID_RE.match(legacy):
            uuid = legacy
        else:
            number = legacy
    inbound = SignalInbound(
        kind=KIND_OTHER,
        source_number=number,
        source_uuid=uuid,
        source_name=_str(envelope.get("sourceName")),
        timestamp=_int(envelope.get("timestamp")),
        account=acct,
    )
    data = envelope.get("dataMessage")
    sync = envelope.get("syncMessage")
    if isinstance(data, dict):
        inbound.kind = KIND_DATA
        _fill_body(inbound, data)
        return inbound
    sent = sync.get("sentMessage") if isinstance(sync, dict) else None
    if isinstance(sent, dict):
        _fill_body(inbound, sent)
        # Note to Self needs BOTH ends to be the account, judged against the
        # identities the client TRUSTS (the configured account plus what it
        # learned from envelopes whose source already matched one of them) --
        # never against what this envelope claims about itself. signal-cli only
        # surfaces a sync from the account's own devices, but the decision that
        # makes a message the owner's input is not left to that alone.
        own = {normalize_identifier(acct), *(normalize_identifier(i) for i in self_ids)} - {""}
        dest_ids = {
            normalize_identifier(sent.get(k))
            for k in ("destinationNumber", "destinationUuid", "destination")
        } - {""}
        from_self = bool(inbound.sender_ids & own)
        if not inbound.is_group and from_self and dest_ids and dest_ids & own:
            inbound.kind = KIND_NOTE_TO_SELF
        else:
            inbound.kind = KIND_SYNC_OTHER
        return inbound
    # Receipts, typing indicators, calls, stories, decryption failures.
    return inbound


def _fill_body(inbound: SignalInbound, message: dict[str, Any]) -> None:
    inbound.text = _str(message.get("message"))
    inbound.timestamp = _int(message.get("timestamp")) or inbound.timestamp
    inbound.is_group = isinstance(message.get("groupInfo"), dict) or isinstance(
        message.get("groupV2"), dict
    )
    inbound.is_reaction = isinstance(message.get("reaction"), dict)
    raw = message.get("attachments")
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    if inbound.text:
        # A body over Signal's inline limit travels as a long-text attachment
        # that signal-cli ALSO reads back into ``message``; keeping the entry
        # would hand the person's text to the turn twice. Only when the body is
        # present: without it, the attachment is the only copy there is.
        entries = [
            e for e in entries if _base_content_type(e.get("contentType")) != LONG_TEXT_CONTENT_TYPE
        ]
    inbound.attachments = len(entries)
    inbound.attachment_refs = [
        ref for entry in entries if (ref := _attachment_ref(entry)) is not None
    ]
    inbound.attachment_shape = tuple(
        (_base_content_type(e.get("contentType")), max(_int(e.get("size")), 0)) for e in entries
    )


def _attachment_ref(entry: object) -> SignalAttachmentRef | None:
    """One ``attachments[]`` entry, or ``None`` when it names nothing fetchable.

    An entry without an id is an attachment signal-cli did not store (a
    stream attachment, or one it was told to ignore), so there is nothing to
    fetch and nothing to say about it.
    """
    if not isinstance(entry, dict):
        return None
    attachment_id = _str(entry.get("id"))
    if not attachment_id:
        return None
    return SignalAttachmentRef(
        id=attachment_id,
        content_type=_str(entry.get("contentType")),
        filename=_str(entry.get("filename")),
        size=max(_int(entry.get("size")), 0),
        voice_note=entry.get("isVoiceNote") is True,
    )


def _proves_nothing_was_sent(exc: BaseException) -> bool:
    """Whether ``exc`` PROVES the send never left. Fails closed (see iMessage)."""
    return isinstance(exc, RpcError) and exc.code in _DEFINITIVE_REJECTIONS


def _method_unsupported(exc: BaseException) -> bool:
    return isinstance(exc, RpcError) and exc.code in _UNSUPPORTED_CODES


def _exit_reason(stderr_tail: str, exc: BaseException) -> str:
    if stderr_tail:
        return f"signal-cli exited: {stderr_tail}"
    return f"signal-cli did not answer: {exc}"


@dataclass
class _OwnSend:
    """One self-addressed message this client sent (see iMessage's ``_OwnSend``).

    ``body_digest`` is :func:`_echo_digest` of the send's echo key, never the
    text itself. ``expires_at`` is ``None`` while the send is in flight: the TTL
    clock starts when the outcome is known, and an unresolved record never
    expires.
    """

    body_digest: bytes
    expires_at: float | None = None
    timestamp: int = 0


class SignalClient:
    """Long-lived ``signal-cli jsonRpc`` child for one account."""

    def __init__(
        self,
        account: str,
        *,
        on_message: InboundHandler | None = None,
        data_dir: str = "",
    ) -> None:
        self._account = account_identifier(account)
        self._on_message = on_message
        self._data_dir = data_dir
        self._peer: Optional[SignalRpcPeer] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None
        self._closing = False
        #: The delay before the NEXT restart attempt, carried across restarts
        #: and reset only after STABLE_UPTIME_S of uptime (see there).
        self._next_delay = RECONNECT_MIN_S
        #: When the current peer was adopted (``_monotonic`` time), or None.
        self._up_since: float | None = None
        #: Every identity the account is known by: the configured one, plus the
        #: number and ACI learned from its own sync envelopes.
        self._self_ids: set[str] = {n for n in (normalize_identifier(self._account),) if n}
        self._own_sends: list[_OwnSend] = []
        self._seen: dict[tuple[str, int, str], None] = {}
        self._echo_reported = False

        self.ready = asyncio.Event()
        self.last_error = ""
        self.on_state_change: Callable[[bool, str], None] | None = None
        #: Both switch off for good only when signal-cli says the method is not
        #: available (``_UNSUPPORTED_CODES``); a timeout is retried next turn.
        self.typing_supported = True
        self.read_supported = True
        self.reactions_supported = True

    # -- identity -------------------------------------------------------------

    @property
    def account(self) -> str:
        return self._account

    def is_self(self, identifier: str) -> bool:
        """Whether *identifier* names this client's own account."""
        normalized = normalize_identifier(identifier)
        return bool(normalized) and normalized in self._self_ids

    def self_ids(self) -> frozenset[str]:
        return frozenset(self._self_ids)

    # -- lifecycle ------------------------------------------------------------

    def set_message_handler(self, on_message: InboundHandler) -> None:
        """Wire the inbound handler after construction (breaks the cycle)."""
        self._on_message = on_message

    async def start(self) -> None:
        """Spawn signal-cli and probe it. Raises with the child's own reason."""
        self._closing = False
        self._adopt(await self._open_peer())
        logger.info("signal: signal-cli is up for %s", redact_identifier(self._account))

    def start_in_background(self, first_error: str) -> None:
        """After a failed :meth:`start`, keep trying on the restart backoff.

        A boot-time failure is often transient -- the network is not up yet, a
        previous instance still holds the account lock, a slow JVM missed the
        probe bound -- so it is handed to the same loop that recovers a child
        that exits later. Each failed round updates the state (and so the
        dashboard's ``signal_connect_error``); the channel comes up by itself
        once signal-cli answers.
        """
        if self._closing or self._peer is not None or self._reconnect_task is not None:
            return
        self._set_state(False, first_error[:120])
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _adopt(self, peer: SignalRpcPeer) -> None:
        self._peer = peer
        self._up_since = _monotonic()
        self._set_state(True, "")

    def _argv(self) -> list[str]:
        cli_path = resolve_bridge_path()
        if not cli_path:
            raise RpcTransportError(f"{BRIDGE_BINARY} is not installed ({describe_lookup()})")
        if not self._account:
            raise RpcTransportError("signal.account is not set")
        argv = [cli_path]
        if self._data_dir:
            argv += ["--data-dir", self._data_dir]
        # No ``-o json``: jsonRpc always speaks JSON, and the default receive
        # mode (on-start) delivers inbound as ``receive`` notifications.
        argv += ["-a", self._account, "jsonRpc"]
        return argv

    async def _open_peer(self) -> SignalRpcPeer:
        """Spawn a child and prove it answers. Only a probed peer is ever adopted.

        The disconnect callback is bound to THIS peer, so a child that dies
        during its own probe (the "not registered" case) is reported by the
        probe's failure and never also schedules a reconnect.
        """
        holder: list[SignalRpcPeer] = []
        peer = SignalRpcPeer(
            self._argv(),
            on_notification=self._on_notification,
            on_disconnect=lambda reason: self._on_peer_lost(holder[0], reason),
        )
        holder.append(peer)
        try:
            await peer.start()
            await peer.call("version", timeout=PROBE_TIMEOUT_S)
        except (RpcError, RpcTransportError) as exc:
            try:
                tail = await peer.settle_stderr()
            finally:
                await peer.close()
            raise RpcTransportError(_exit_reason(tail, exc)) from exc
        except BaseException:
            # A cancellation (a shutdown during start, ``close()`` cancelling
            # the reconnect loop mid-probe) or any other failure: this peer is
            # never adopted, so nothing else would ever close it, and its
            # signal-cli child would keep running holding the account lock.
            await peer.close()
            raise
        return peer

    def _on_peer_lost(self, peer: SignalRpcPeer, reason: str) -> None:
        """signal-cli exited on its own after it was up: report it, then recover."""
        if self._closing or peer is not self._peer:
            return
        self._peer = None
        if self._up_since is not None and _monotonic() - self._up_since >= STABLE_UPTIME_S:
            # It ran long enough that this exit is a new failure, not the same
            # one again: start the backoff over.
            self._next_delay = RECONNECT_MIN_S
        self._up_since = None
        self.ready.clear()
        self._set_state(False, reason[:120])
        if self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._recover(peer))

    async def _recover(self, lost: SignalRpcPeer) -> None:
        # The reader saw EOF before stderr was necessarily drained; the last
        # stderr line is the reason an operator can act on.
        tail = await lost.settle_stderr()
        try:
            await lost.close()
        except Exception:
            logger.debug("signal: closing the lost peer failed", exc_info=True)
        message = f"signal-cli exited: {tail}" if tail else "signal-cli exited"
        logger.warning("signal: %s; will try to restart it", message)
        self._set_state(False, message[:120])
        await self._reconnect_loop()

    async def _reconnect_loop(self) -> None:
        """Respawn on the carried backoff until a probe succeeds or we close.

        Waits BEFORE every attempt: a child that just died dies again at once,
        and an immediate respawn is a tight loop.
        """
        try:
            while not self._closing and self._peer is None:
                delay = self._next_delay
                self._next_delay = min(delay * 2, RECONNECT_MAX_S)
                await _sleep(delay)
                if self._closing:
                    return
                try:
                    self._adopt(await self._open_peer())
                    logger.info("signal: signal-cli is up again")
                    return
                except (RpcError, RpcTransportError) as exc:
                    self._set_state(False, str(exc)[:120])
                    logger.warning(
                        "signal: start failed (%s); retrying in %.0fs", exc, self._next_delay
                    )
        finally:
            if asyncio.current_task() is self._reconnect_task:
                self._reconnect_task = None

    async def close(self) -> None:
        self._closing = True
        # Through the state callback, not just the event: a channel disabled while
        # connected must stop showing as connected on Settings and Services.
        self._set_state(False, "")
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        peer = self._peer
        self._peer = None
        if peer is not None:
            await peer.close()

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- outbound -------------------------------------------------------------

    async def send(self, recipient: str, text: str, *, attachments: Sequence[str] = ()) -> str:
        """Send *text*; return signal-cli's message timestamp as the message id.

        *attachments* are passed to signal-cli's ``send`` as they are: this
        channel only ever hands it ``data:`` URIs (see
        :func:`kiro_crew.signal_cli.files.data_uri`), so the bytes that travel
        are the ones the outbound gates validated and no path is re-opened. With
        attachments the text is the caption and may be empty.

        A recipient that is the account itself goes to Note to Self. Any
        failure RAISES -- never collapsed into an empty id -- so a turn is not
        recorded as answered when nothing was delivered.
        """
        if not text and not attachments:
            return ""
        note_to_self = self.is_self(recipient)
        params: dict[str, Any] = {"message": text}
        if attachments:
            params["attachments"] = list(attachments)
        if note_to_self:
            params["noteToSelf"] = True
        else:
            params["recipient"] = [recipient]
        # An attachment-only send has no body to recognise its echo by, so its
        # key is the attachments' types and sizes, which the re-synced copy
        # carries too: a send whose timestamp never came back is still matched.
        record = (
            self._remember_own_send(outbound_echo_key(text, attachments)) if note_to_self else None
        )
        try:
            result = await self._call("send", params)
        except BaseException as exc:
            if isinstance(exc, RpcError) and exc.code == ERR_UNTRUSTED_IDENTITY:
                self._report_untrusted(recipient)
            if record is not None:
                if _proves_nothing_was_sent(exc):
                    self._forget_own_send(record)
                else:
                    record.expires_at = _monotonic() + ECHO_TTL_S
            raise
        timestamp = _int(result.get("timestamp"))
        if record is not None:
            record.timestamp = timestamp
            record.expires_at = _monotonic() + ECHO_TTL_S
        return str(timestamp) if timestamp else ""

    async def get_attachment(self, attachment_id: str, sender: str) -> bytes:
        """The bytes of a received attachment signal-cli already stored.

        ``getAttachment`` answers ``{"data": "<base64>"}`` for the id the
        envelope carried, so the channel never reads signal-cli's data dir
        itself (which it would have to guess: ``--data-dir`` is optional and
        the default follows ``XDG_DATA_HOME``). *sender* fills the command's
        required recipient-or-group argument; signal-cli looks the file up by
        id alone.

        Raises ``ValueError`` for a refusal the user should be told about (the
        file is gone, the answer is not base64) and the RPC errors otherwise.
        """
        try:
            result = await self._call(
                "getAttachment",
                {"id": attachment_id, "recipient": sender},
                timeout=ATTACHMENT_FETCH_TIMEOUT_S,
            )
        except RpcError as exc:
            if exc.code == -1:
                # signal-cli's user error: "Could not find attachment with ID".
                # It logs a failed download and delivers the envelope anyway.
                raise ValueError("signal-cli has no stored copy of it") from exc
            raise
        data = result.get("data")
        if not isinstance(data, str):
            raise ValueError("signal-cli returned no attachment data")
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("signal-cli returned unreadable attachment data") from exc

    async def send_reaction(
        self, recipient: str, emoji: str, target_author: str, target_timestamp: int
    ) -> None:
        """React with *emoji* to the message *target_author* sent at *target_timestamp*.

        Best-effort status decoration: a failure is logged and swallowed, and
        signal-cli saying the method is unavailable switches reactions off for
        good (the typing indicator's degrade policy). A newer reaction by the
        same author REPLACES the previous one on Signal, so a phase change is
        one call. In Note to Self the reaction targets the owner's own message;
        it comes back from the owner's other devices as a reaction envelope,
        which the transport never treats as input.
        """
        if not self.reactions_supported or not emoji or not target_author:
            return
        if not recipient or not target_timestamp:
            return
        params: dict[str, Any] = {
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        }
        if self.is_self(recipient):
            params["noteToSelf"] = True
        else:
            params["recipient"] = [recipient]
        try:
            await self._call("sendReaction", params, timeout=10.0)
        except (RpcError, RpcTransportError) as exc:
            if _method_unsupported(exc):
                logger.debug("signal: reactions unavailable, disabling (%s)", exc)
                self.reactions_supported = False
            else:
                logger.debug("signal: status reaction not sent (%s)", exc)

    def _report_untrusted(self, recipient: str) -> None:
        """A send was refused because the recipient's safety number changed.

        signal-cli will not send to a changed identity until it is trusted
        again, so every later message to that person fails the same way. Say
        how to fix it, in the log and on the dashboard's status line; the
        channel itself stays connected. The suggested command names the account
        by a placeholder: the operator's own number is personal data too, and
        neither the log nor the status line may carry it whole.
        """
        message = (
            f"untrusted identity for {redact_identifier(recipient)}: verify the safety "
            "number, then run `signal-cli -a <your number> trust -a <number>`"
        )
        logger.warning("signal: send refused, %s", message)
        self._set_state(self.ready.is_set(), message[:120])

    def _remember_own_send(self, body_key: str) -> _OwnSend:
        record = _OwnSend(body_digest=_echo_digest(body_key))
        self._own_sends.append(record)
        overflow = len(self._own_sends) - ECHO_WINDOW
        if overflow > 0:
            kept: list[_OwnSend] = []
            for held in self._own_sends:
                # Oldest RESOLVED records go first; an in-flight one never does.
                if overflow > 0 and held.expires_at is not None:
                    overflow -= 1
                    continue
                kept.append(held)
            self._own_sends = kept
        return record

    def _forget_own_send(self, record: _OwnSend) -> None:
        for index, held in enumerate(self._own_sends):
            if held is record:
                del self._own_sends[index]
                return

    def is_own_echo(self, inbound: SignalInbound) -> bool:
        """Whether a self-addressed *inbound* is this client's own send coming back.

        Consumes the matching record. Only self-addressed traffic is checked --
        a message from another person cannot be the agent's own reply.
        """
        if inbound.kind == KIND_DATA and not (inbound.sender_ids & self._self_ids):
            return False
        now = _monotonic()
        self._own_sends = [r for r in self._own_sends if r.expires_at is None or r.expires_at > now]
        body_digest = _echo_digest(
            _echo_key(inbound.text) or _attachment_echo_key(inbound.attachment_shape)
        )
        for index, record in enumerate(self._own_sends):
            # Once signal-cli has reported the send's timestamp, the timestamp
            # alone decides: an echo carries it, and the owner's own short reply
            # ("1", "yes") that happens to equal a recent send must not be
            # swallowed. The body is the fallback only while the send is in
            # flight or when it never resolved -- an attachment-only send is
            # keyed by its attachments' shape (``outbound_echo_key``), and an
            # empty key never matches.
            if record.timestamp:
                if record.timestamp != inbound.timestamp:
                    continue
            elif not record.body_digest or record.body_digest != body_digest:
                continue
            del self._own_sends[index]
            if not self._echo_reported:
                self._echo_reported = True
                logger.warning(
                    "signal: dropping this channel's own Note to Self reply echoed back; "
                    "without this the channel would answer itself in a loop."
                )
            else:
                logger.debug("signal: suppressed an echo of this channel's own message")
            return True
        return False

    async def send_typing(self, recipient: str) -> None:
        """Show a typing indicator. No-op for Note to Self; degrades on rejection."""
        if not self.typing_supported or not recipient or self.is_self(recipient):
            return
        try:
            await self._call("sendTyping", {"recipient": [recipient]}, timeout=10.0)
        except (RpcError, RpcTransportError) as exc:
            if _method_unsupported(exc):
                logger.debug("signal: typing unavailable, disabling (%s)", exc)
                self.typing_supported = False
            else:
                logger.debug("signal: typing indicator not sent (%s)", exc)

    async def mark_read(self, recipient: str, timestamp: int) -> None:
        """Send a read receipt for *timestamp*. Same degrade policy."""
        if not self.read_supported or not recipient or not timestamp or self.is_self(recipient):
            return
        try:
            await self._call(
                "sendReceipt",
                {"recipient": recipient, "targetTimestamp": [timestamp], "type": "read"},
                timeout=10.0,
            )
        except (RpcError, RpcTransportError) as exc:
            if _method_unsupported(exc):
                logger.debug("signal: read receipts unavailable, disabling (%s)", exc)
                self.read_supported = False
            else:
                logger.debug("signal: read receipt not sent (%s)", exc)

    # -- inbound --------------------------------------------------------------

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "receive":
            return
        if not self.ready.is_set():
            # signal-cli starts receiving the moment it starts, so a queued
            # message can arrive while the probe is still in flight, before the
            # peer is adopted -- and answering it then would fail with "not
            # running". Each notification runs in its own task (see the peer),
            # so it waits here for adoption; if the probe fails instead, closing
            # the peer cancels this task with it.
            await self.ready.wait()
        envelope = receive_envelope(params)
        if envelope is not None and isinstance(envelope.get("syncMessage"), dict):
            self._learn_from_own_sync(envelope)
        inbound = parse_receive(params, self._account, self._self_ids)
        if inbound is None:
            return
        if inbound.kind in (KIND_DATA, KIND_NOTE_TO_SELF) and inbound.timestamp:
            if self._already_seen(inbound):
                return
        if self._on_message is None:
            return
        await self._on_message(inbound)

    def _learn_from_own_sync(self, envelope: dict[str, Any]) -> None:
        """Learn the account's other identity from one of its OWN sync envelopes.

        Only when the envelope's source already matches a trusted identity --
        ``sourceNumber`` equal to the configured account, or ``sourceUuid`` an
        ACI that is configured or was learned that way -- is the other half
        adopted. A sync claiming any other source teaches nothing, so no
        envelope can add itself to the identities that pass as the owner.
        """
        number = normalize_identifier(_str(envelope.get("sourceNumber")))
        uuid = normalize_identifier(_str(envelope.get("sourceUuid")))
        if number and number in self._self_ids:
            self._learn_self_ids([uuid] if _UUID_RE.match(uuid) else [])
        elif uuid and uuid in self._self_ids:
            self._learn_self_ids([number] if _E164_RE.match(number) else [])

    def _learn_self_ids(self, ids: Iterable[str]) -> None:
        for identifier in ids:
            if identifier and identifier not in self._self_ids:
                self._self_ids.add(identifier)
                logger.info(
                    "signal: learned the account's other identity %s",
                    redact_identifier(identifier),
                )

    def _already_seen(self, inbound: SignalInbound) -> bool:
        key = (normalize_identifier(inbound.sender), inbound.timestamp, inbound.kind)
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > DEDUPE_WINDOW:
            self._seen.pop(next(iter(self._seen)))
        return False

    # -- helpers --------------------------------------------------------------

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        peer = self._peer
        if peer is None:
            raise RpcTransportError("signal-cli is not running")
        return await peer.call(method, params, timeout=timeout)

    def _set_state(self, connected: bool, error: str) -> None:
        self.last_error = error
        if connected:
            self.ready.set()
        else:
            self.ready.clear()
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("signal: state callback failed", exc_info=True)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0
