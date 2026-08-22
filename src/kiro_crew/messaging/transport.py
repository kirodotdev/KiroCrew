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
    * ``supports_session_resume`` — gates whether connecting a channel from the
      dashboard marks the binding as an INBOUND resume target
      (``accepts_inbound``), and therefore whether the slot row reports
      ``direction: both``. Only a transport whose inbound path actually resolves
      the mirror binding may declare it: Discord's dispatcher looks the
      conversation up (``DiscordSessionResume.resumed_session``), while Telegram
      and the rest derive a session key from the route alone and never consult
      the binding. Declaring it where it is not honoured makes the dashboard
      promise a two-way link whose replies silently start a separate session —
      which is exactly what this flag exists to prevent. Slack is out of scope
      here: it routes inbound through its own ``_thread_to_session`` index and
      never sets the marker.

    * ``max_buttons`` — TOTAL interactive choices a renderer may present for
      one ``[OPTIONS:]`` trailer (not a per-row layout number — row packing
      stays channel-internal). Widget-capable renderers route the parsed
      list through ``messaging.renderer.apply_options_cap``, which keeps the
      first N for the widget and degrades the remainder to a numbered text
      list in the body. Channels declaring 0 render no widget and route the
      WHOLE list through ``messaging.renderer.render_options_as_text``, which is
      the same helper with zero widget slots, so every choice arrives as a
      numbered line rather than being deleted with the trailer.

    * ``files_outbound`` — gates whether a renderer pulls local image
      references out of a sealed segment and uploads them. Discord's renderer
      reads it before running ``messaging.outbound_files`` extraction, so a
      channel declaring ``False`` keeps printing the markdown path (the honest
      degradation) instead of silently dropping the picture. Split from
      ``files_inbound`` because one boolean was undecidable: the two directions
      land per channel and in different changes — Weixin ingests CDN media
      today with no upload half written.

    * ``table_mode`` — the per-target table presentation policy: ``off`` /
      ``cards`` / ``grid`` / ``native`` / ``auto``. Renderers read this field
      at the outbound boundary, so the same canonical turn may stay native on
      one target and become cards on another. ``off`` is the conservative
      default: a transport opts in only when its renderer applies the shared
      table transform or already owns an equivalent conversion.

    * ``native_tables`` — whether the target renders a GFM pipe table AS a
      table. ``messaging.tables.resolve_table_policy`` checks it, so
      ``table_mode=native`` passes through only where the capability is true
      and safely becomes cards otherwise. Claiming native support wrongly ships
      literal pipes, which is the output the conversion exists to prevent.
      Slack is deliberately false — it renders no table, and its own flattening
      in ``slack/format.py`` predates this.

    ASPIRATIONAL (declared, honest, but nothing reads them yet — the
    capability-gated interface work will consume them; do NOT write code that
    assumes they are enforced):

    * ``streaming``, ``edit``, ``reactions``, ``rich_blocks``, ``threads``
    * ``files_inbound`` — the transport ingests user attachments into the turn.

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
    # Per-target table presentation. Renderers that opt into the shared table
    # transform read this value; ``off`` keeps every pre-existing channel byte
    # unchanged until its transport declares an intentional mode.
    table_mode: str = "off"
    # Native rich-table support. Default False because over-claiming it leaks
    # literal pipes on platforms that cannot render a GFM table.
    native_tables: bool = False
    # parameters (channels differ widely -- NOT booleans)
    max_message_chars: int = 4096  # CHARS. Slack path caps 3900, Telegram 4000, Discord 1900
    max_buttons: int = 3  # TOTAL interactive choices per prompt (WhatsApp reply buttons = 3)
    # send-policy
    supports_proactive_send: bool = True  # WhatsApp: False outside the 24h window
    # inbound-routing policy. Default False: a transport that forgets to declare
    # it gets an outbound-only binding, which is merely less capable — declaring
    # it wrongly would promise a two-way link that silently drops replies.
    supports_session_resume: bool = False
    # Whether ``send_message`` returns a real message id. Most platforms answer a
    # send with one, so a caller can read an EMPTY id as "refused or exhausted" and
    # not mistake a dropped message for a delivered one. Two platforms return no id
    # at all (WeCom's proactive command, Feishu's reply), so for them an empty
    # string is the SUCCESS value and failure raises — and a caller applying the
    # empty-id test there reports a delivered message as lost, which for a cron
    # result means the dedup hash never advances and the same result repeats.
    # Default True because that is the majority contract and the conservative
    # direction: a transport that forgets to declare it is read strictly, which
    # over-reports failure rather than inventing success.
    returns_message_id: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "edit": self.edit,
            "reactions": self.reactions,
            "files_inbound": self.files_inbound,
            "files_outbound": self.files_outbound,
            "rich_blocks": self.rich_blocks,
            "threads": self.threads,
            "table_mode": self.table_mode,
            "native_tables": self.native_tables,
            "max_message_chars": self.max_message_chars,
            "max_buttons": self.max_buttons,
            "supports_proactive_send": self.supports_proactive_send,
            "supports_session_resume": self.supports_session_resume,
            "returns_message_id": self.returns_message_id,
        }


def delivery_confirmed(capabilities: TransportCapabilities, message_id: str) -> bool:
    """Whether a ``send_message`` that did not raise actually delivered.

    Two conventions exist and a caller cannot assume either. Most transports report
    a refused or exhausted send by returning an EMPTY message id rather than raising
    (``send_message`` ends in ``str(mid or "")``), so there "no exception" is not
    delivery, and treating it as delivery is a silent loss with consequences
    downstream: cron stands its Slack fallback down and advances its dedup hash on a
    confirmed delivery, so one false success loses the result on every surface at
    once. WeCom's proactive command and Feishu's reply carry no id at all, so for
    them the empty string is the SUCCESS value and failure raises -- and applying the
    empty-id test there turns every delivered cron result into a reported loss whose
    dedup hash never advances, repeating the same result on every tick.

    ``returns_message_id`` is each transport's own answer about which one it follows.
    This function exists so the three proactive-send call sites ask it the same way:
    the rationale above was written out at each of them, and the version that
    forgot the second convention was the version that shipped.
    """
    return bool(message_id) or not capabilities.returns_message_id


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

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Whether a PROACTIVE send to this conversation is still authorized.

        :meth:`authorize` gates a turn the user drove; this gates a message
        nobody asked for -- a cron result, a compaction notice, a subagent
        completion. Those resolve their destination from a **persisted**
        ``ChannelLink``, which records a conversation but not the principal that
        authorized it, so removing a recipient from the roster and restarting
        leaves the link intact and the sends still flowing. Revocation has to be
        re-decided at egress, and only the transport can decide it: the roster
        holds principals while the link holds a conversation id, and whether
        those are the same string is a per-platform fact (a Telegram private
        ``chat_id`` IS the ``user_id``; a Discord DM channel id is not).

        *principal* is the peer's platform id when the session key positively
        names one (a 1:1 DM under the default scope), else ``""``. It is what
        makes the answer reachable for a transport whose conversation id is
        opaque: check it against the same roster :meth:`authorize` uses. Empty
        means "the key does not name one principal" -- a room-audience route or a
        unified bucket -- NOT that nobody is authorized, so a transport that can
        only answer via the principal should permit rather than deny when it is
        absent, or it would refuse every group and unified-scope send.

        A transport whose conversation id already IS the roster identity (Telegram,
        iMessage, WeCom, Weixin) can ignore *principal* and answer from
        *conversation_id* alone, which also covers its room-audience routes.

        Called on the shared send ladder (``chat_runner._resolve_channel_target``),
        so it MUST stay synchronous and in-memory -- no network, no awaits. A
        revocation check that needs a round trip would put an unbounded call on
        every proactive send, and a check that can time out is a check that
        fails open under load.

        Defaults to True: a transport with no roster to consult would otherwise
        have no way to deliver. That default is deliberately NOT relied on for
        the shipped channels -- ``test_channel_transport_outbound_authz.py``
        requires every transport under ``src/kiro_crew/<channel>/`` to override
        this and make its own decision explicit, so a channel cannot inherit
        permission silently. Override it and return False for a conversation
        whose principal is no longer on the roster.
        """
        return True

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
