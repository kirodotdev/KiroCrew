"""The ``slack`` section, its per-channel records, and the channel activation modes.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")


@dataclass
class SlackConfig:
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "Ignored for access: Slack is owner-only, so only the owner can interact. "
            "A name here is only a display-name fallback. Each entry: {slack_id, name}.",
        ),
    )
    tracking_channels: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Tracking Channels",
            "Slack channels to monitor. Each entry: {channel_id, name}.",
        ),
    )
    open_channels: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Open Channels",
            "Ignored for access: open channels are disabled (Slack is owner-only).",
        ),
    )
    command: str = field(
        default="kirocrew",
        metadata=_meta(
            "Command",
            "Slack slash command trigger word.",
            # Boot-read: the trigger is registered in the Slack app MANIFEST, so
            # a local reload cannot make Slack route a new word. Applying it
            # in-process would only change the help text and report success for a
            # command that still does not exist on Slack's side.
            restart=True,
        ),
    )
    forward_to_agent_callback: str = field(
        default="",
        metadata=_meta(
            "Forward to Agent Callback",
            "Callback ID for the 'Forward to Agent' message shortcut. "
            "Must match the callback_id configured in your Slack app manifest. "
            "Leave empty to disable the feature.",
            tags=["slack"],
        ),
    )
    trusted_bot_ids: set[str] = field(
        default_factory=set,
        metadata=_meta(
            "Trusted Bot IDs",
            "Bot IDs allowed to bypass the bot filter for multi-node mesh communication. "
            "The gateway's own bot ID is never trusted, even if listed "
            "(it would reply to itself in a loop).",
            tags=["slack"],
        ),
    )
    trusted_bot_turn_limit: int = field(
        default=5,
        metadata=_meta(
            "Trusted Bot Turn Limit",
            "Maximum consecutive turns a thread may run on trusted-bot messages "
            "before a human message is required (loop guard for mutually trusted "
            "gateways). A message from an allowed human resets the count. "
            "Minimum 1; values below 1 are treated as 1.",
            tags=["slack"],
        ),
    )
    allowed_enterprise_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Enterprise IDs",
            "Slack Enterprise Grid org IDs to allow. Empty list allows all orgs (default-open).",
            tags=["slack"],
        ),
    )
    reactions: dict[str, str | None] = field(
        default_factory=dict,
        metadata=_meta(
            "Reactions",
            "Override the status reaction emojis. Valid keys: queued (the ack shown while a turn "
            "runs where Slack shows no native thread status; removed when the reply lands) and "
            "error (a failed turn). Set a value to null to suppress it. Keys of the retired "
            "per-phase trail (thinking, coding, browsing, tool, done) are accepted and ignored.",
            tags=["slack"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Ack a Slack message with a reaction while it is being answered (where Slack shows "
            "no native thread status) and mark a failed turn with the error reaction.",
            tags=["slack"],
        ),
    )
    show_thinking: bool = field(
        default=True,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a thread reply in Slack. "
            "Disable to keep responses concise.",
            tags=["slack"],
        ),
    )
    dm_single_session: bool = field(
        default=False,
        metadata=_meta(
            "DM Single Session",
            "Treat each 1:1 DM as one continuous conversation instead of starting "
            "a new session per top-level message: every message in the DM, "
            "threaded replies included, joins one slack:<channel_id> session. A "
            "top-level message is answered at channel root; a threaded reply is "
            "still answered in its thread. Applies only when "
            "messaging.use_transport is true and the channel is not in review "
            "mode. Group channels and group DMs are unaffected. Off by default: "
            "turning it on routes the next DM to a different session than the "
            "previous one.",
            tags=["slack"],
        ),
    )
    home_tab_sessions_per_kind: int = field(
        default=5,
        metadata=_meta(
            "Home Tab Sessions Per Kind",
            "Max sessions shown per category (main chat / task runner) in the Slack Home Tab.",
            tags=["slack"],
        ),
    )
    sessions_limit: int = field(
        default=10,
        metadata=_meta(
            "Sessions List Length",
            "Max sessions listed by the Slack 'sessions' DM keyword and the "
            "'sessions' slash command. Raise it on an install with many "
            "background sessions, where the ones worth resuming are pushed past "
            "the end of the list.",
            tags=["slack"],
        ),
    )
    use_tunnel_url: bool = field(
        default=False,
        metadata=_meta(
            "Use Tunnel URL in Slack",
            "When true, dashboard links posted to Slack (e.g. via /kirocrew dashboard) "
            "use the tunnel URL if one is active. When false (default), "
            "Slack links always use the configured dashboard origin or host:port. "
            "Disabled by default until the tunnel mechanism is scaled for general use.",
            tags=["slack"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["slack"],
        ),
    )
    auto_link_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Connect New Sessions To Slack Automatically",
            "Open a Slack thread for every new dashboard session on its first "
            "message, exactly as the Connect to Slack button does, so you can "
            "follow and reply from Slack without clicking the button each time. "
            "The thread is created when the first message is sent, not when the "
            "tab opens, so an abandoned tab leaves nothing behind. Only sessions "
            "a person starts in the dashboard qualify: cron, app, sub-agent, "
            "incognito, temporary and channel-born sessions are never connected "
            "this way. The thread always opens in the owner's DM with the bot, "
            "which only the owner can read. Off by default. Takes effect on the "
            "next new session; no restart.",
            tags=["slack"],
        ),
    )
    agent_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Agent Sessions",
            "Run Slack threads as Slack agent sessions: threads show a live status "
            "and title, and Slack's native Stop button cancels the turn. Adds the "
            "agent view (features.agent_view), the assistant:write scope and the "
            "agent-session events to the generated app manifest; an installed app "
            "needs that manifest update and a reinstall before it takes effect, and "
            "until then Slack behaves as before. Slack cannot switch an app back "
            "from agent_view to assistant_view. Off by default; no restart.",
            tags=["slack"],
        ),
    )


ACTIVATION_ALWAYS = "always"  # Process every message
ACTIVATION_MENTION = "mention"  # Only respond when @mentioned
ACTIVATION_OBSERVE = "observe"  # Record messages, respond only when @mentioned (deep context)
ACTIVATION_REVIEW = "review"  # Generate response, show ephemeral draft for owner approval
ACTIVATION_OFF = "off"  # Ignore all messages completely — no history recorded
_VALID_ACTIVATIONS = frozenset(
    {ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OBSERVE, ACTIVATION_REVIEW, ACTIVATION_OFF}
)


@dataclass
class ChannelConfig:
    """Per-channel Slack configuration."""

    activation: str = field(
        default=ACTIVATION_MENTION,
        metadata=_meta(
            "Activation",
            "Channel activation mode.",
            enum=["always", "mention", "observe", "review", "off"],
        ),
    )
    agent: str = field(
        default="",
        metadata=_meta("Agent", "Agent override for this channel (empty = default)."),
    )
    thread_follow: bool = field(
        default=True,
        metadata=_meta(
            "Thread Follow",
            "Respond to all messages in threads where bot was previously @mentioned.",
        ),
    )

    @classmethod
    def from_dict(cls, data: dict) -> ChannelConfig:
        section = SectionReader(cls, data)
        activation = section.get("activation")
        if activation not in _VALID_ACTIVATIONS:
            # Narrows to mention whatever the default is: a typo must not widen who
            # the channel answers.
            activation = ACTIVATION_MENTION
        return cls(
            activation=activation,
            agent=section.get("agent"),
            thread_follow=section.get("thread_follow"),
        )


_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


def _validate_tracking_channels(raw: list) -> list[dict]:
    """Validate and coerce tracking_channels entries.

    Accepted formats:
    - ``{"channel_id": "C...", "name": "..."}`` — passed through
    - ``"C..."`` (bare string) — auto-coerced to ``{"channel_id": "C..."}`` with a warning

    Rejects entries that are neither strings starting with C/D/G nor dicts with channel_id.
    """
    if not raw:
        return []
    result: list[dict] = []
    coerced = 0
    rejected = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("channel_id"):
            result.append(entry)
        elif isinstance(entry, str) and len(entry) > 1 and entry[0] in _VALID_CHANNEL_PREFIXES:
            result.append({"channel_id": entry})
            coerced += 1
        else:
            rejected += 1
    if coerced:
        logger.warning(
            "Config: slack.tracking_channels has %d bare string(s) — auto-coerced to "
            '{"channel_id": "..."} format. Prefer: [{"channel_id": "C...", "name": "..."}]',
            coerced,
        )
    if rejected:
        logger.warning(
            "Config: slack.tracking_channels has %d invalid entries (expected objects with "
            '"channel_id" field or bare channel ID strings starting with C/D/G). '
            "These entries were ignored.",
            rejected,
        )
    return result
