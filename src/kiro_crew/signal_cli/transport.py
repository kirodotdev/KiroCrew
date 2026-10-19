"""Layer 1 -- Signal as a concrete ``MessagingTransport``.

Wraps :class:`SignalClient` (a local ``signal-cli jsonRpc`` child) in the
channel-neutral transport contract, so Signal rides the shared ``TurnDriver``
(credential/exfil redaction + tool-approval ladder + SEL audit) and the shared
proactive-send ladder (``send_message`` MCP tool, cron/notification delivery,
the dashboard mirror) like every other channel.

Dependency direction is ``signal_cli -> messaging`` (allowed).

Access control mirrors iMessage, because the identity on the other end is a
phone number rather than a workspace member:

* **Allow-list, deny-by-default.** ``signal.allowed_numbers`` holds E.164
  numbers and/or ACIs; an empty list authorizes nobody. A sender matches on
  EITHER its number or its ACI, because a numberless account has only the
  latter.
* **DM only, fail closed.** A group message is refused with a
  ``denied_group_chat`` audit: a reply there would reach members who are not on
  the allow-list.
* **Note to Self is the owner talking**, admitted only when the account itself
  is on the allow-list. Any other message the owner's own devices sync (the
  owner writing to a third party) is never input to the agent.
* **Own messages are ignored**, and the echo ledger runs LAST, after the group
  and allow-list gates, because consuming a record is a side effect a dropped
  message must not spend (the iMessage ordering rule).
* **Unauthorized inbound is dropped with no reply.**
* **The operator is narrower than the allow-list** (:meth:`SignalTransport.is_operator`).
  Every allowed contact may chat; only the operator may answer a tool approval,
  run a session-acting command, or steer a running turn. The operator is the
  account itself when one of its identities is on the list (a linked device /
  Note to Self setup), else the list's only entry, else nobody -- with several
  allowed numbers and no own identity among them, tool approvals and operator
  commands are off and a tool that needs approval is refused.
* **Reactions are never input.** A reaction envelope carries no body, and this
  channel's own status reactions come back that way in Note to Self; dropping
  them by kind (not by "has no text") is what keeps them from becoming turns
  once attachment-only messages are admitted.
* **Attachments are fetched last**, after every gate above AND the channels
  governance gate, and only for a sender who passed them: fetching earlier
  would let anyone who can message the number make the host pull bytes
  (:mod:`kiro_crew.signal_cli.attachments`). The fetched temp files live for
  the dispatch and are deleted after it, unless the dispatcher handed them to
  the running turn the message was steered into, which deletes them when it
  ends (:attr:`SignalInbound.temp_paths`).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from kiro_crew.messaging.attachments import append_attachment_context, cleanup_offloaded
from kiro_crew.messaging.dispatch import inbound_permitted
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.signal_cli.attachments import ingest_inbound_attachments
from kiro_crew.signal_cli.client import (
    KIND_DATA,
    KIND_NOTE_TO_SELF,
    KIND_SYNC_OTHER,
    SignalClient,
    SignalInbound,
    coerce_identifier,
    identifier_problem,
    normalize_identifier,
    redact_identifier,
)
from kiro_crew.signal_cli.files import data_uri, document_filename
from kiro_crew.signal_cli.renderer import display_safe_text

logger = logging.getLogger(__name__)

DispatchFn = Callable[[SignalInbound], Awaitable[None]]

#: Signal keeps a message body inline up to 2000 bytes; signal-cli turns a
#: longer one into a "long text" attachment the recipient has to open. Splitting
#: at 2000 characters keeps ordinary answers inline (multi-byte text can still
#: cross the byte line, which signal-cli then delivers as that attachment).
SIGNAL_SAFE_MESSAGE_CHARS = 2000

#: One SEL denial audit per (sender, outcome) per this many seconds. A phone
#: number that keeps writing is one fact, not one audit row per message.
DENIAL_AUDIT_INTERVAL_S = 600.0

#: Bound on remembered (sender, outcome) audit times.
_DENIAL_AUDIT_WINDOW = 1024

#: The audit rate limit's clock, bound here so a test can drive it.
_monotonic = time.monotonic

# Signal can edit, but this channel does not: no streaming, no edit, no buttons,
# so the answer is delivered once at the end and an ``[OPTIONS:]`` trailer
# becomes a numbered list. ``send`` always answers with the message timestamp,
# which is Signal's message id, and failure raises, so the default
# ``returns_message_id=True`` is the honest declaration.
SIGNAL_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    # Phase reactions on the person's own message (renderer, via sendReaction).
    reactions=True,
    # Pictures, voice notes (transcribed) and documents ride the shared ingest
    # path, fetched with getAttachment after the authorization gauntlet.
    files_inbound=True,
    # The renderer runs the shared outbound extractor and sends each picture as
    # a send attachment; ``send_document`` serves the file_send tool.
    files_outbound=True,
    rich_blocks=False,
    threads=False,
    max_message_chars=SIGNAL_SAFE_MESSAGE_CHARS,
    max_buttons=0,
    supports_proactive_send=True,
    supports_session_resume=False,
)


def allowed_numbers_from_config(entries: object) -> list[str] | None:
    """Normalize ``signal.allowed_numbers``; ``None`` when it is not a list.

    The ONE reading of the field, shared by the boot factory and the live
    reconfigure path so the two cannot disagree about the roster. An entry that
    is neither a +E.164 number nor an ACI is dropped: signal-cli never reports a
    sender that could match it. ``warn_unusable_entries`` names it in the log.
    """
    if not isinstance(entries, list):
        return None
    return [
        n
        for e in entries
        if not identifier_problem(e) and (n := normalize_identifier(coerce_identifier(e)))
    ]


def warn_unusable_entries(entries: object, *, field: str = "signal.allowed_numbers") -> int:
    """Log one WARNING per entry that is neither a +E.164 number nor an ACI.

    Such an entry can never match a sender, so the person the operator meant
    to admit is silently refused; saying so at boot and on reload is the only
    way they find out. The entry is redacted. Returns how many were reported.
    """
    values = entries if isinstance(entries, list) else [entries]
    reported = 0
    for entry in values:
        problem = identifier_problem(entry)
        if problem and problem != "empty":
            reported += 1
            logger.warning(
                "signal: %s entry %s is %s; it will never match",
                field,
                redact_identifier(str(entry)),
                problem,
            )
        elif isinstance(entry, int) and not isinstance(entry, bool):
            logger.warning(
                "signal: %s entry %s is a JSON number; read as %s -- quote it in config.json",
                field,
                redact_identifier(str(entry)),
                redact_identifier(coerce_identifier(entry)),
            )
    return reported


#: :func:`operator_mode`'s answers, also reported verbatim by the settings GET
#: (``operator_mode``) and mirrored by the dashboard panel.
OPERATOR_ACCOUNT = "account"
OPERATOR_SOLE_ENTRY = "sole_entry"
OPERATOR_NONE = "none"


def operator_mode(allowed: Iterable[str], self_ids: Iterable[str]) -> str:
    """Who the operator is for this allow-list and these account identities.

    The ONE rule, shared by :meth:`SignalTransport.is_operator` and the
    settings endpoint, so the panel's warning and the gateway's behaviour cannot
    disagree. Both inputs are compared in their canonical form
    (:func:`normalize_identifier`):

    * :data:`OPERATOR_ACCOUNT` -- one of the account's own identities is on the
      list (signal-cli is a linked device of the owner's phone): the account
      itself is the operator;
    * :data:`OPERATOR_SOLE_ENTRY` -- otherwise, the list has exactly one entry,
      and that entry is the operator;
    * :data:`OPERATOR_NONE` -- otherwise nobody is: an empty list, or a bot
      number with two or more allowed senders.
    """
    roster = {n for e in allowed if (n := normalize_identifier(e))}
    if not roster:
        return OPERATOR_NONE
    if roster & {n for e in self_ids if (n := normalize_identifier(e))}:
        return OPERATOR_ACCOUNT
    return OPERATOR_SOLE_ENTRY if len(roster) == 1 else OPERATOR_NONE


class SignalTransport(MessagingTransport):
    """Concrete Signal transport over a local ``signal-cli`` child."""

    channel_type = "signal"

    def __init__(
        self,
        client: SignalClient,
        *,
        allowed_numbers: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        self._allowed: frozenset[str] = frozenset(
            n for e in allowed_numbers if (n := normalize_identifier(e))
        )
        self._dispatch = dispatch
        self.capabilities = SIGNAL_CAPABILITIES
        self._last_denial_audit: dict[tuple[str, str], float] = {}
        self._warn_if_no_operator()

    @property
    def client(self) -> SignalClient:
        return self._client

    # -- Live config ---------------------------------------------------------
    def reconfigure(self, section: Any) -> None:
        """Adopt a reloaded ``signal`` section's allow-list (replaced wholesale).

        An EMPTY list is adopted (deny-all is the fail-closed direction); a value
        that is not a list keeps the previous roster. ``account`` is boot-only:
        the registry restarts the channel for it.
        """
        raw = getattr(section, "allowed_numbers", None)
        numbers = allowed_numbers_from_config(raw)
        if numbers is not None:
            warn_unusable_entries(raw)
        if numbers is None:
            logger.warning(
                "signal: allowed_numbers is not a list in the reloaded config; keeping the "
                "previous allow-list (%d entr(ies))",
                len(self._allowed),
            )
            return
        new_allowed = frozenset(numbers)
        if new_allowed == self._allowed:
            return
        added = len(new_allowed - self._allowed)
        removed = len(self._allowed - new_allowed)
        self._allowed = new_allowed
        logger.info("signal: allow-list reloaded (+%d/-%d)", added, removed)
        if not new_allowed:
            logger.warning(
                "signal: allowed_numbers is now empty — every inbound message is refused"
            )
        else:
            self._warn_if_no_operator()
        sel().log_api_access(
            caller="config",
            operation="signal_transport.reconfigure",
            outcome="allow_list_changed",
            source="signal",
            resources=f"added={added} removed={removed} size={len(new_allowed)}",
        )

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        return await self._client.send(conversation_id, content)

    async def send_document(
        self,
        conversation_id: str,
        file: OutboundFile,
        *,
        caption: str = "",
        thread_id: str | None = None,
    ) -> str:
        """Send one validated file (the ``file_send`` tool's native leg).

        *file* carries bytes the ``file_send`` gates already validated (the
        ``OutboundFile`` contract: the path is provenance, never re-opened), so
        they travel as a data URI. The roster is re-checked here as well as by
        the caller: a document is a disclosure that cannot be taken back.

        The caption and the file name are screened here, at the sink. The
        caption goes through the renderer's own display screen: the upload
        endpoint already screens the description it passes, but this verb is
        the transport's, and a caption is agent-authored text reaching the
        phone like any other. The name goes through the credential and
        exfiltration screen the shared ``upload_filename`` applies to picture
        names (:func:`~kiro_crew.signal_cli.files.document_filename`): the gates
        admitted the file, not what it is called.
        """
        if not self.may_send_to(conversation_id):
            raise PermissionError("signal: conversation not authorized")
        name = document_filename(Path(file.path or "").name)
        return await self._client.send(
            normalize_identifier(conversation_id),
            display_safe_text(caption),
            attachments=[data_uri(file.data, file.mime, name)],
        )

    async def resolve_conversation(self, user_id: str) -> str:
        # The number/ACI IS the conversation: ``send`` opens or reuses the 1:1
        # chat, and the account's own identity is Note to Self.
        return normalize_identifier(user_id)

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # signal-cli keeps no readable history; sessions persist via the
        # conversation log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets = []
        for ident in sorted(self._allowed):
            label = "Signal · Note to Self" if self._client.is_self(ident) else f"Signal · {ident}"
            targets.append(ConfiguredChannelTarget(f"user:{ident}", label))
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        if kind != "user" or not separator or normalize_identifier(value) not in self._allowed:
            return None
        return await self.resolve_conversation(value), None

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Re-check the roster before a proactive send. Fails closed.

        The number/ACI IS the conversation, so the conversation id is checked
        directly. Note to Self is permitted when ANY identity of the account
        (number or ACI) is on the roster.
        """
        ident = normalize_identifier(conversation_id)
        if not ident:
            return False
        if ident in self._allowed:
            return True
        return self._client.is_self(ident) and self._self_allowed()

    def _self_allowed(self) -> bool:
        return bool(self._client.self_ids() & self._allowed)

    # -- Operator -----------------------------------------------------------
    def is_operator(self, identity: str) -> bool:
        """Whether *identity* (an admitted principal) is the operator.

        The gate for everything that ACTS AS the agent rather than merely talks
        to it: answering a tool approval (including 3, which trusts the whole
        session), a session-acting command, steering a running turn. Narrower
        than :meth:`authorize` on purpose, as WhatsApp's is: under
        ``messaging.dm_scope = "unified"`` every allowed contact shares the
        operator's session, so "may talk to the agent" cannot also mean "may
        approve tools on the operator's machine".

        Positive identity, recomputed per call so a reloaded allow-list and an
        account identity learned later both take effect at once:

        1. the account itself, when one of its identities is on the allow-list
           (signal-cli is a linked device of the owner's phone);
        2. else the allow-list's only entry, when it has exactly one;
        3. else nobody.

        Which case applies is :func:`operator_mode`, the rule the settings
        endpoint reports to the dashboard.
        """
        ident = normalize_identifier(identity)
        if not ident:
            return False
        mode = self.operator_mode()
        if mode == OPERATOR_ACCOUNT:
            return self._client.is_self(ident)
        if mode == OPERATOR_SOLE_ENTRY:
            return ident in self._allowed
        return False

    def operator_mode(self) -> str:
        """:func:`operator_mode` for the live roster and the identities learned so far."""
        return operator_mode(self._allowed, self._client.self_ids())

    def _warn_if_no_operator(self) -> None:
        """Say once, at boot and on a reload that changes the roster, that nobody is the operator."""
        if len(self._allowed) > 1 and self.operator_mode() == OPERATOR_NONE:
            logger.warning(
                "signal: allowed_numbers has %d entries and none is this account, so no "
                "sender is the operator: tool approvals and operator commands (/new, "
                "/compact, /status, /stop) are disabled and a tool that needs approval is "
                "refused. Add the account's own number to allowed_numbers to make its "
                "owner the operator, or list a single number.",
                len(self._allowed),
            )

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.start()

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Allow-list, deny-by-default. Empty allow-list authorizes nobody."""
        ident = normalize_identifier(msg.user_id)
        allowed = bool(ident) and (
            ident in self._allowed or (self._client.is_self(ident) and self._self_allowed())
        )
        if not allowed:
            self._audit_denial(msg.user_id, "signal_transport.authorize", "denied")
        return allowed

    def _audit_denial(self, sender: str, operation: str, outcome: str) -> None:
        """SEL-audit a refused inbound -- at most once per sender and outcome per interval.

        Every setup is audited, at the same rate: at most one row per
        ``(sender, outcome)`` per :data:`DENIAL_AUDIT_INTERVAL_S`, with the
        remembered pairs capped at :data:`_DENIAL_AUDIT_WINDOW`. With a
        dedicated bot number a stranger's message is an access attempt. When
        the account itself is on the allow-list (signal-cli is a linked device
        of the owner's phone) every message the owner receives from anyone,
        and every group they are in, arrives here and is refused -- the owner's
        ordinary inbox -- which is why the rate limit, not silence, is what
        keeps a busy inbox from flooding the audit log: a refusal there can
        still be someone trying the agent, and it leaves a record.
        """
        caller = redact_identifier(sender)
        key = (normalize_identifier(sender), outcome)
        now = _monotonic()
        last = self._last_denial_audit.get(key)
        if last is not None and now - last < DENIAL_AUDIT_INTERVAL_S:
            logger.debug("signal: %s %s (audited recently)", outcome, caller)
            return
        self._last_denial_audit.pop(key, None)
        self._last_denial_audit[key] = now
        while len(self._last_denial_audit) > _DENIAL_AUDIT_WINDOW:
            self._last_denial_audit.pop(next(iter(self._last_denial_audit)))
        sel().log_api_access(caller=caller, operation=operation, outcome=outcome, source="signal")

    async def receive(self, raw_envelope: Any) -> None:
        """Classify -> authorize -> echo check -> dispatch."""
        if not isinstance(raw_envelope, SignalInbound):
            return
        inbound = raw_envelope
        if inbound.kind == KIND_SYNC_OTHER:
            # The owner's own message to somebody else, synced from their phone.
            # Not input to the agent and not a denial: no audit.
            return
        if inbound.kind not in (KIND_DATA, KIND_NOTE_TO_SELF):
            return  # receipts, typing, calls, stories
        if inbound.is_reaction:
            # Someone's reaction, or this channel's own status reaction synced
            # back in Note to Self: never input, never a denial.
            return
        if not inbound.text and not inbound.attachment_refs:
            return
        if inbound.kind == KIND_DATA and inbound.sender_ids & self._client.self_ids():
            # A data message FROM the account itself (a send with notify-self):
            # this channel's own traffic, dropped without an audit event.
            return
        if inbound.is_group:
            self._audit_denial(inbound.sender, "signal_transport.receive", "denied_group_chat")
            return
        if inbound.kind == KIND_NOTE_TO_SELF:
            user_id = inbound.reply_to
        else:
            # Match on the number OR the ACI; carry the identity that matched.
            user_id = next(
                (i for i in sorted(inbound.sender_ids) if i in self._allowed), inbound.sender
            )
        msg = InboundMessage(
            channel_type="signal",
            user_id=user_id,
            conversation_id=normalize_identifier(user_id),
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        # LAST gate (side effect: consumes a ledger record), see module docstring.
        if self._client.is_own_echo(inbound):
            return
        inbound.principal = normalize_identifier(user_id)
        inbound.operator = self.is_operator(inbound.principal)
        if self._dispatch is None:
            return
        if inbound.attachment_refs:
            # The governance gate BEFORE the fetch: the dispatcher's own check
            # runs after this method has already pulled the bytes, and a
            # `channels` deny must stop the download, not merely the reply.
            if not await inbound_permitted("signal"):
                return
            result = await ingest_inbound_attachments(
                self._client, inbound.attachment_refs, inbound.sender
            )
            inbound.temp_paths = list(result.temp_paths)
            inbound.original_text = inbound.text
            inbound.text = append_attachment_context(inbound.text, result)
        try:
            inbound.person_origin = True
            await self._dispatch(inbound)
        finally:
            # The shared ingest hands path ownership to the caller; the files
            # hold the sender's decrypted bytes. Whatever the dispatcher did not
            # hand on to a running turn goes now.
            owned, inbound.temp_paths = inbound.temp_paths, []
            await cleanup_offloaded(owned)
