"""Layer 1 -- the ``MessagingTransport`` contract and value objects.

Depends only on the standard library so the package stays channel-neutral
and cycle-free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TransportCapabilities:
    """What a messaging channel can do.

    Boolean flags gate features. The integer *parameters* capture where
    channels differ quantitatively (so the Renderer can chunk / degrade
    rather than assume a single shape), and the send-policy flag gates
    proactive / delayed sends.

    Defaults are deliberately conservative (the WhatsApp-like floor) so a
    transport that forgets to declare a capability degrades safely rather
    than over-promising.
    """

    # feature flags
    streaming: bool = False
    edit: bool = False
    reactions: bool = False
    files: bool = False
    rich_blocks: bool = False
    threads: bool = False
    # parameters (channels differ widely -- NOT booleans)
    max_message_chars: int = 4096  # Slack ~40000, Telegram 4096, Discord 2000, WhatsApp 4096
    max_buttons: int = 3  # interactive choices per prompt (WhatsApp reply buttons = 3)
    # send-policy
    supports_proactive_send: bool = True  # WhatsApp: False outside the 24h window

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "edit": self.edit,
            "reactions": self.reactions,
            "files": self.files,
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
