"""Layer 1 -- the ``MessagingTransport`` contract and value objects.

Depends only on the standard library so the package stays channel-neutral
and cycle-free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConfiguredChannelTarget:
    """A user-configured destination exposed to the dashboard."""

    target_id: str
    label: str
    available: bool = True
    unavailable_reason: str = ""

    def to_dict(self, channel_type: str) -> dict[str, Any]:
        return {
            "channel_type": channel_type,
            "target_id": self.target_id,
            "label": self.label,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class TransportCapabilities:
    """What a messaging channel can do.

    HONESTY CONTRACT. A declaration here is a claim other code is entitled to
    trust, so every field carries its real enforcement status below and
    ``test/test_capability_ledger.py`` forces any new field to be classified.
    History: several flags drifted into being false (a channel declaring
    ``threads=False`` while threading end to end; a ``max_buttons`` cap no
    renderer applied; docstrings describing gates that did not exist). Declare
    what the CODE does today — not the platform ceiling, and not intent.

    ENFORCED (something behaves differently when the value changes):

    * ``max_message_chars`` — the cross-surface mirror leg chunks on it
      (``dashboard/chat_runner.py``) and five renderers size their own chunks
      from it. This is a CHARACTER count. A byte-capped platform (Webex) must
      declare a character value that is safe under its byte limit, because the
      chunker counts chars and cannot see bytes.
    * ``supports_proactive_send`` — gates mirror-link creation (HTTP 400) and
      the outbound mirror leg (skipped).

    ASPIRATIONAL (declared, honest, but nothing reads them yet — the
    capability-gated interface work will consume them; do NOT write code that
    assumes they are enforced):

    * ``streaming``, ``edit``, ``reactions``, ``rich_blocks``, ``threads``
    * ``max_buttons`` — per-renderer caps are currently hardcoded
      (slack ``[:10]``, discord ``[:25]``); no renderer reads this field.
    * ``files_inbound`` / ``files_outbound`` — split because one boolean was
      undecidable: discord ingests attachments but cannot upload, slack does
      both. Inbound = the transport ingests user attachments into the turn;
      outbound = the transport can deliver a file to the user.

    Defaults are deliberately conservative (the WhatsApp-like floor) so a
    transport that forgets to declare a capability degrades safely rather
    than over-promising.
    """

    # feature flags
    streaming: bool = False
    edit: bool = False
    reactions: bool = False
    files_inbound: bool = False
    files_outbound: bool = False
    rich_blocks: bool = False
    threads: bool = False
    # parameters (channels differ widely -- NOT booleans)
    max_message_chars: int = 4096  # CHARS. Slack path caps 3900, Telegram 4000, Discord 1900
    max_buttons: int = 3  # interactive choices per prompt (WhatsApp reply buttons = 3)
    # send-policy
    supports_proactive_send: bool = True  # WhatsApp: False outside the 24h window

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "edit": self.edit,
            "reactions": self.reactions,
            "files_inbound": self.files_inbound,
            "files_outbound": self.files_outbound,
            "rich_blocks": self.rich_blocks,
            "threads": self.threads,
            "max_message_chars": self.max_message_chars,
            "max_buttons": self.max_buttons,
            "supports_proactive_send": self.supports_proactive_send,
        }


@dataclass
class InboundMessage:
    """A normalized inbound message, channel-agnostic."""

    channel_type: str  # "slack" | "telegram" | "discord" | "whatsapp" | ...
    user_id: str
    conversation_id: str
    text: str
    thread_id: str | None = None
    attachments: list[Any] = field(default_factory=list)
    is_mention: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "text": self.text,
            "thread_id": self.thread_id,
            "attachments": list(self.attachments),
            "is_mention": self.is_mention,
        }


class MessagingTransport(ABC):
    """Channel-neutral inbound/outbound contract for a messaging channel.

    A new channel = implement this interface + an inbound adapter, with
    *zero* change to the shared turn-handling core.
    """

    channel_type: str = ""
    capabilities: TransportCapabilities

    @property
    def dispatcher(self) -> Any:
        """Return the object owning the bound inbound dispatch callback."""
        return getattr(getattr(self, "_dispatch", None), "__self__", None)

    # -- Tier-1 core (every transport) --------------------------------------
    @abstractmethod
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        """Send ``content`` to a conversation; return a platform message id."""

    @abstractmethod
    async def resolve_conversation(self, user_id: str) -> str:
        """Resolve a direct-conversation id for ``user_id`` (``open_dm`` equiv)."""

    @abstractmethod
    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        """Return prior messages for a conversation/thread."""

    # -- Lifecycle (default no-ops; override as needed) ---------------------
    async def connect(self) -> None:
        """Establish the channel connection. Lazy-import client libs HERE."""
        return None

    async def maintain(self) -> None:
        """Keep the connection alive (poll / heartbeat). Optional."""
        return None

    async def disconnect(self) -> None:
        """Tear down the channel connection. Optional."""
        return None

    # -- Dashboard-configured outbound targets -----------------------------
    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        """Return configured destinations suitable for dashboard linking."""
        return []

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        """Resolve an advertised target to ``(conversation_id, thread_id)``."""
        return None

    # -- Inbound adapter ----------------------------------------------------
    @abstractmethod
    async def receive(self, raw_envelope: Any) -> None:
        """Handle a raw platform event: ack -> filter -> authorize ->
        normalize -> dispatch to the turn handler."""

    @abstractmethod
    def authorize(self, msg: InboundMessage) -> bool:
        """Return True iff ``msg`` is allowed to drive a turn.

        Implementations MUST be deny-by-default: an unconfigured transport
        authorizes nobody.
        """
