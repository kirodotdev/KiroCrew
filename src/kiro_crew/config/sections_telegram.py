"""The ``telegram`` section, its legacy per-account records, and its activation modes.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, _safe_bool
from kiro_crew.config.sections_channels import _clamp_pct, _coerce_int_ids, _threshold_pct
from kiro_crew.config.sections_defaults import SectionReader
from kiro_crew.config.sections_slack import ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF

logger = logging.getLogger("kiro_crew.config.loader")


#: The activation modes a Telegram forum Topic can express. A subset of
#: ``_VALID_ACTIVATIONS`` on purpose, and the subset is the point rather than an
#: omission: ``observe`` needs a channel-history buffer only Slack populates, and
#: feeding it would put non-owner prose into the prompt unfenced; ``review`` is a
#: whole second rendering mode built on Slack Block Kit ephemerals, which Telegram
#: has no equivalent for. Declaring either here would advertise a mode that
#: silently behaves like a different one.
TELEGRAM_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_telegram_activation(value: str) -> str:
    """*value* if Telegram can express it, else ``mention``.

    Degrades to the NARROWER mode, matching ``WeixinTransport.authorize``'s
    treatment of an unrecognized ``dm_policy``: a malformed value must not resolve
    to the most permissive reading of itself. ``always`` starts a turn for every
    message in an allow-listed Topic, and a Topic is a SHARED space, so agent
    output lands in front of everyone in it. Widening that because a value failed
    to parse would make a typo grant participation the operator never asked for,
    and it fails silently in the direction nobody audits.

    ``mention`` rather than ``off`` because it is fail-safe without being
    fail-dead: an explicit ``@handle`` is an unambiguous request, so the operator
    can still reach the bot while it is refusing to answer unaddressed messages.

    Reached ONLY for a value that was present and unparseable. An ABSENT key is
    resolved to ``always`` by the caller before this runs, and that stays: taking
    the documented default is not the same act as asking for something specific
    and being misunderstood.
    """
    if value in TELEGRAM_ACTIVATIONS:
        return value
    logger.warning(
        "telegram.forum_activation=%r is not one of %s; using %r (the narrower mode, "
        "so an unreadable value cannot widen who the bot answers).",
        value,
        ", ".join(repr(a) for a in sorted(TELEGRAM_ACTIVATIONS)),
        ACTIVATION_MENTION,
    )
    return ACTIVATION_MENTION


def _parse_telegram_accounts(raw: object) -> dict[str, "TelegramAccountConfig"]:
    """Parse the deprecated ``telegram.accounts`` map from raw config JSON.

    Parsing is retained so a config written by an earlier release round-trips
    through :meth:`KiroCrewConfig.save` with its tokens and allow-lists intact;
    no bot is started from the result. Each value is a dict with optional keys
    matching :class:`TelegramAccountConfig`. Invalid entries (non-dict values,
    missing bot_token) are skipped so a hand-edited config never crashes
    gateway startup.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, TelegramAccountConfig] = {}
    for account_id, acct_data in raw.items():
        if not isinstance(account_id, str) or not isinstance(acct_data, dict):
            continue
        # Account IDs are held to the same shape they were accepted under, so a
        # config that round-trips here is byte-comparable to what an earlier
        # release wrote: alphanumeric plus dash and underscore, never empty.
        if not account_id or not account_id.replace("-", "").replace("_", "").isalnum():
            continue
        token = str(acct_data.get("bot_token", "")).strip()
        if not token:
            continue
        account = SectionReader(TelegramAccountConfig, acct_data)
        out[account_id] = TelegramAccountConfig(
            bot_token=token,
            allowed_user_ids=_coerce_int_ids(account.get("allowed_user_ids")),
            allow_forum=account.read("allow_forum", _safe_bool),
            allowed_forum_chat_ids=_coerce_int_ids(account.get("allowed_forum_chat_ids")),
            soft_threshold_pct=account.read("soft_threshold_pct", _threshold_pct),
        )
    return out


@dataclass
class TelegramAccountConfig:
    """A single named Telegram bot account, retained only to preserve config.

    Deprecated and inert: nothing starts a bot from this entry. It stays
    parseable and serializable so that loading and saving a config written by an
    earlier release round-trips the operator's tokens and allow-lists instead of
    erasing them. To serve one of these bots, move its token to
    ``telegram.bot_token``.
    """

    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token for this account.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM this bot account.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve forum Topics for this account.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Supergroup chat_ids permitted for this account.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt threshold for this account.",
            tags=["telegram"],
        ),
    )


@dataclass
class TelegramConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Telegram Bot API channel (long-polling). Requires "
            "TELEGRAM_BOT_TOKEN (env/.env) or telegram.bot_token.",
            tags=["telegram"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token from @BotFather. Prefer the TELEGRAM_BOT_TOKEN "
            "credential (env/.env) over storing it here.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM the bot. Empty = deny all "
            "(fail closed): a Telegram bot is globally reachable by @username.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["telegram"],
        ),
    )
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's reasoning after each answer as a collapsed, "
            "expandable quote. Off by default: Telegram's rate limit is per chat "
            "and shared with the streaming edits the answer already spends, so "
            "reasoning costs an extra message per turn.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve Telegram supergroup forum Topics as per-topic sessions "
            "(Slack-thread style). Fail-closed: also requires the supergroup's "
            "chat_id in allowed_forum_chat_ids.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Numeric supergroup chat_ids permitted to run forum-topic sessions. "
            "Empty = deny all groups (fail closed).",
            tags=["telegram"],
        ),
    )
    voice_replies: bool = field(
        default=False,
        metadata=_meta(
            "Voice Replies",
            "Speak each answer as a voice/audio message in addition to the text, "
            "using the global voice_reply provider settings. Off by default: it "
            "costs a second message per turn against Telegram's per-chat rate "
            "budget, and TTS may not be configured. Toggle per conversation with "
            "/voice on|off; this is the default for a new conversation.",
            tags=["telegram"],
        ),
    )
    forum_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta(
            "Forum Activation",
            "When the bot answers inside an allow-listed forum Topic: 'always' "
            "(every message), 'mention' (only when its @handle is used or one of "
            "its own messages is replied to), or 'off' (never). Slack's channel "
            "equivalent defaults to 'mention'; this defaults to 'always' so an "
            "existing forum keeps working after an upgrade instead of going quiet. "
            "Does not apply to a 1:1 DM, which is always served.",
            tags=["telegram"],
        ),
    )
    accounts: dict[str, TelegramAccountConfig] = field(
        default_factory=dict,
        metadata=_meta(
            "Accounts",
            "Deprecated and inert: named Telegram bot accounts no longer start a "
            "bot. Multi-bot operation is withdrawn until a bot is a governable "
            "unit (its own enable switch, its own posture ceiling, and honest "
            "audit attribution) rather than a second inbound door that only the "
            "global telegram.enabled can close. The map is still parsed and "
            "written back so an existing config keeps its tokens and allow-lists, "
            "but while the map is non-empty the whole Telegram channel stays OFF: "
            "move the token you want served to telegram.bot_token AND remove the "
            "accounts block.",
            tags=["telegram"],
            deprecated=True,
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
            tags=["telegram"],
        ),
    )

    def __post_init__(self) -> None:
        # Telegram carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)
