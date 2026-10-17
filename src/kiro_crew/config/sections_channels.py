"""The messaging-channel sections other than Slack and Telegram, and the coercers they share.

WeCom, Feishu, Weixin, WhatsApp, Discord, Webex, iMessage and Teams, the shared
context-threshold range, and the id-list, group and session-folder coercers.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, _safe_int, field_default

#: Bounds of a context-threshold percentage, and the single statement of the range.
#: The floor is 1, not 0, because a 0% threshold means "always over" and would fire the
#: notice/compaction on every turn. Public because the dashboard's channel-config
#: handlers validate an inbound percentage against exactly this range, and a validator
#: that restated the numbers would drift from what the loader will actually accept.
THRESHOLD_PCT_MIN = 1
THRESHOLD_PCT_MAX = 100


def _clamp_pct(value: int) -> int:
    """Clamp an integer context-threshold percentage to the shared range."""
    return max(THRESHOLD_PCT_MIN, min(THRESHOLD_PCT_MAX, value))


def _threshold_pct(raw: object, default: int) -> int:
    """Coerce a transport context-threshold percentage and clamp it to 1..100.

    The single coercion for every ``soft_threshold_pct`` / ``hard_threshold_pct``
    read, so a hand-edited config can never load an out-of-range threshold on
    any channel.
    """
    return _clamp_pct(_safe_int(raw, default))


def _normalize_threshold_pair(soft: int, hard: int) -> tuple[int, int]:
    """Normalize a soft/hard context-threshold pair to a valid ordering.

    Clamp both to the shared range and pull the soft threshold down to the
    hard one when it exceeds it, so a misconfig (e.g. hard=50, soft=95) can't
    make the soft nudge unreachable — the transports check ``pct >= hard``
    first.
    """
    soft = _clamp_pct(soft)
    hard = _clamp_pct(hard)
    if soft > hard:
        soft = hard
    return soft, hard


#: Outbound services the iMessage bridge accepts. Anything else is a typo that
#: would be rejected per send rather than at load time. Shared with the settings
#: API so the form's choices and the loader's clamp cannot drift apart.
IMESSAGE_SERVICES = frozenset(("imessage", "sms", "auto"))


@dataclass
class WeComConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WeCom channel via WeCom AI-bot. Requires the WECOM_BOT_ID "
            "and WECOM_SECRET credentials to be set.",
            tags=["wecom"],
        ),
    )
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "WeCom users allowed to DM the bot. Each entry: {userid, name}. "
            "The owner is always allowed.",
            tags=["wecom"],
        ),
    )
    allow_all_users: bool = field(
        default=False,
        metadata=_meta(
            "Allow All Users",
            "Let every member of the WeCom organization DM the bot, bypassing "
            "the allow-list. Safe-ish because a WeCom AI bot is reachable only "
            "inside your own org tenant (unlike globally addressable bots), "
            "but it grants agent access to the whole company. Default off.",
            tags=["wecom"],
        ),
    )
    ws_url: str = field(
        default="wss://openws.work.weixin.qq.com",
        metadata=_meta(
            "WebSocket URL",
            "WeCom AI-bot long-connection endpoint.",
            tags=["wecom"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["wecom"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["wecom"],
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
            tags=["wecom"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig (e.g. hard=50, soft=95, or an out-of-range value) can't
        # make the soft nudge unreachable -- _maybe_notice checks ``pct >= hard``
        # first.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class FeishuConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Feishu (Lark/飞书) channel. Requires FEISHU_APP_ID and "
            "FEISHU_APP_SECRET environment variables to be set.",
            tags=["feishu"],
        ),
    )
    allowed_open_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Open IDs",
            "Feishu open_ids allowed to DM the bot (deny-by-default: empty list "
            "authorises nobody). Find your open_id via the Feishu developer console.",
            tags=["feishu"],
        ),
    )
    allow_group: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Chat",
            "Serve messages from group chats whose chat_id is in allowed_group_ids. "
            "The bot must be @-mentioned in a group to receive the message.",
            tags=["feishu"],
        ),
    )
    allowed_group_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Group IDs",
            "Feishu group chat_ids allowed to drive a turn (requires allow_group=true).",
            tags=["feishu"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to /compact "
            "or /new instead of auto-compacting.",
            tags=["feishu"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this so the window never overflows.",
            tags=["feishu"],
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
            tags=["feishu"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig. The helper's floor
        # is 1, not 0, because a 0% threshold reads as "always over" and would
        # compact every turn -- a hand-rolled max(0, ...) admits exactly that.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


def _coerce_int_ids(raw: object) -> list[int]:
    """Coerce a config value to a clean ``list[int]``, dropping anything invalid.

    Fail closed against a hand-edited config: a non-list (e.g. the string
    ``"12345"``) yields ``[]`` instead of iterating char-by-char, and any entry
    that isn't a clean base-10 integer (``"--100"``, ``"1.5"``, unicode digits,
    booleans) is skipped rather than raising in ``int()`` and crashing config
    load / gateway startup.
    """
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for u in raw:
        try:
            ids.append(int(str(u)))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_opaque_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of OPAQUE IDs.

    For channels whose user IDs are not numeric — WeChat/iLink uses forms like
    ``wxid_abc123`` and ``<hex>@im.bot`` — so the digit-only filter in
    :func:`_coerce_str_ids` would silently drop every entry. With a
    deny-by-default ``dm_policy`` that would lock out every intended sender.

    Still fails closed on shape: a non-list yields ``[]``, and blank entries are
    dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s and s not in out:
            out.append(s)
    return out


_WHATSAPP_GROUP_MODES = ("mention", "rules", "off")
_WHATSAPP_GROUP_COOLDOWN_DEFAULT = 120


def _coerce_whatsapp_groups(raw: object) -> list[dict]:
    """Coerce the whatsapp ``groups`` config value to sanitized rule entries.

    Each entry needs at least a non-empty ``jid``; everything else gets a safe
    default. Unknown ``mode`` values fall back to ``mention`` (never to an
    unprompted-speech mode), and cooldown is clamped to >= 0. Fails closed on
    shape: a non-list yields ``[]``, malformed entries are dropped, duplicate
    JIDs keep the first entry.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("jid", "")).strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        mode = str(entry.get("mode", "mention")).strip().lower()
        if mode not in _WHATSAPP_GROUP_MODES:
            mode = "mention"
        try:
            cooldown = int(entry.get("cooldown_s", _WHATSAPP_GROUP_COOLDOWN_DEFAULT))
        except (TypeError, ValueError):
            cooldown = _WHATSAPP_GROUP_COOLDOWN_DEFAULT
        out.append(
            {
                "jid": jid,
                "name": str(entry.get("name", "")).strip(),
                "mode": mode,
                "rules": str(entry.get("rules", "")).strip(),
                "cooldown_s": max(0, cooldown),
            }
        )
    return out


def _coerce_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of digit IDs.

    Used for Discord snowflakes, which exceed 2^53 and therefore stay strings
    (JSON round-trip safe). Fails closed like :func:`_coerce_int_ids`: a
    non-list yields ``[]`` and non-digit entries are dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s.isdigit() and s not in out:
            out.append(s)
    return out


#: Longest accepted channel session-folder name — matches the 100-char cap the
#: folder CRUD endpoint applies, so a name that round-trips through config can
#: never be longer than one created in the sidebar.
SESSION_FOLDER_NAME_MAX = 100


def _coerce_session_folder(raw: object) -> str:
    """Coerce a channel's ``session_folder`` value to a usable folder name.

    Empty string means the feature is off (the default) — sessions from the
    channel stay unfiled. Anything else is the name of the sidebar folder they
    are filed into. Non-strings, control characters, path separators, and
    over-long values all fail closed to off rather than producing a folder the
    user did not ask for: truncating an over-long hand-edited value would file
    conversations into a real folder whose name nobody chose, which is worse
    than leaving them where they already were.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if len(name) > SESSION_FOLDER_NAME_MAX:
        return ""
    if any(ch in name for ch in ("/", "\\")) or any(ord(ch) < 0x20 for ch in name):
        return ""
    return name


@dataclass
class WeixinConfig:
    """Weixin (personal WeChat) channel via Tencent's iLink Bot API.

    Distinct from :class:`WeComConfig` (enterprise WeCom over WebSocket). The
    bot ``token`` + ``account_id`` are obtained through the Settings > Messaging Channels
    QR-login flow; prefer the WEIXIN_TOKEN credential over storing the token
    here.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Weixin (iLink personal WeChat) channel (long-polling). "
            "Requires a bot token + account id from the Settings QR flow.",
            tags=["weixin"],
        ),
    )
    token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "iLink bot token (from QR login). Prefer the WEIXIN_TOKEN credential "
            "(env/.env / cred store) over storing it here.",
            tags=["weixin"],
            sensitive=True,
        ),
    )
    account_id: str = field(
        default="",
        metadata=_meta(
            "Account ID",
            "iLink bot account id captured during QR login.",
            tags=["weixin"],
        ),
    )
    base_url: str = field(
        default="https://ilinkai.weixin.qq.com",
        metadata=_meta(
            "iLink Base URL",
            "iLink API base URL (per-account, returned by QR login).",
            tags=["weixin"],
        ),
    )
    dm_policy: str = field(
        default="allowlist",
        metadata=_meta(
            "DM Policy",
            "Who may DM the bot: 'allowlist' (only allowed_user_ids, the default), "
            "'open' (any sender), or 'disabled'. Defaults to allowlist with an empty "
            "list, so a freshly connected bot authorizes NOBODY until you add an id.",
            tags=["weixin"],
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Weixin user ids permitted to DM the bot when dm_policy='allowlist'. "
            "Empty = deny all (fail closed).",
            tags=["weixin"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["weixin"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["weixin"],
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
            tags=["weixin"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class WhatsAppConfig:
    """WhatsApp channel via a QR-linked personal account (WhatsApp Web protocol).

    Pairs as a linked device on the operator's own WhatsApp account — there is
    no bot token. Pairing state lives in a local session database under the
    data home (``whatsapp/session.db``), created by the Settings > Messaging Channels QR
    flow. Requires the optional ``whatsapp`` dependency
    (``pip install 'neonize==0.4.3.post0'``; see :mod:`kiro_crew.extras`).

    Uses the unofficial WhatsApp Web protocol; automation on a personal
    account is against WhatsApp's Terms of Service and carries a small risk
    of the linked number being banned. Keep volumes personal-scale.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WhatsApp channel (QR-linked personal account over the "
            "WhatsApp Web protocol). Pair a device from Settings > Messaging Channels; "
            "needs the 'whatsapp' dependency extra installed.",
            tags=["whatsapp"],
        ),
    )
    dm_policy: str = field(
        default="self",
        metadata=_meta(
            "DM Policy",
            "Who may command the agent in direct chats: 'self' (only the linked "
            "account itself — your own messages, the default), 'allowlist' "
            "(yourself plus allowed_wa_ids), 'open' (any sender), or 'disabled'. "
            "Unknown values deny everyone (fail closed).",
            tags=["whatsapp"],
        ),
    )
    allowed_wa_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed WhatsApp IDs",
            "Phone numbers (digits only, country code, no '+') permitted to "
            "address the agent besides the linked account: in direct chats when "
            "dm_policy='allowlist', and in every configured group regardless of "
            "dm_policy (a group member not listed here is dropped silently, even "
            "when they @-mention the agent or the group is in 'rules' mode). Empty "
            "adds nobody beyond the linked account.",
            tags=["whatsapp"],
        ),
    )
    groups: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Group Rules",
            "Per-group participation rules. Each entry: {'jid': group JID "
            "(…@g.us), 'name': display label, 'mode': 'mention' (reply only "
            "when @-mentioned or quoted, the default) | 'rules' (also speak "
            "unprompted when the entry's rules say the agent can genuinely "
            "help) | 'off', 'rules': free-text guidance for when to speak, "
            "'cooldown_s': minimum seconds between unprompted replies "
            "(default 120)}. Groups not listed are ignored entirely. Listing a "
            "group lets the agent speak there; it does not admit its members: "
            "only you and the numbers in allowed_wa_ids can make the agent reply.",
            tags=["whatsapp"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Session DB Path",
            "Read-only. The pairing session database always lives at "
            "<data home>/whatsapp/session.db, because that path is what the "
            "sensitive-path protection matches: it holds the linked-device keys, "
            "and moving it elsewhere would take the credential out from behind "
            "the one control that stops an agent reading it.",
            tags=["whatsapp"],
            restart=True,
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["whatsapp"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["whatsapp"],
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
            tags=["whatsapp"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeixinConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class DiscordConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Discord channel (Gateway WebSocket, DMs plus optional "
            "allow-listed server threads). Requires DISCORD_BOT_TOKEN (env/.env) "
            "or discord.bot_token.",
            tags=["discord"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Discord bot token from the Developer Portal (Bot page). Prefer the "
            "DISCORD_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["discord"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Discord user IDs (snowflakes) permitted to message the bot. Empty = "
            "deny all (fail closed).",
            tags=["discord"],
        ),
    )
    allowed_thread_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Thread IDs",
            "Discord server thread IDs where approved users may run the agent. "
            "Empty = DMs only. A server channel is denied unless it is listed in "
            "allowed_channel_ids, and a turn there still runs in a thread.",
            tags=["discord"],
        ),
    )
    allowed_channel_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Channel IDs",
            "Discord server channels where approved users may start a new agent "
            "thread. Requires auto_thread=true: with auto_thread off, messages in "
            "these channels are ignored.",
            tags=["discord"],
        ),
    )
    auto_thread: bool = field(
        default=True,
        metadata=_meta(
            "Auto-create Threads",
            "Create one Discord thread per approved message in an allowed channel. "
            "When off, messages in allowed channels get no reply.",
            tags=["discord"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to !compact or !new when context passes this percentage.",
            tags=["discord"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Discord messages during processing.",
            tags=["discord"],
        ),
    )
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a subtext note in Discord. "
            "Off by default to keep responses concise.",
            tags=["discord"],
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
            tags=["discord"],
        ),
    )

    def __post_init__(self) -> None:
        # Discord carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)


@dataclass
class WebexConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Webex Messaging channel (device WebSocket, no public "
            "URL needed). Requires WEBEX_BOT_TOKEN (env/.env) or webex.bot_token.",
            tags=["webex"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Webex bot access token from developer.webex.com (My Webex Apps). "
            "Prefer the WEBEX_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["webex"],
            sensitive=True,
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Webex account emails permitted to DM the bot. Empty = deny all "
            "(fail closed): anyone in the org can message a Webex bot.",
            tags=["webex"],
        ),
    )
    allow_group_rooms: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Spaces",
            "Answer in group spaces as well as direct messages. Off by default: a "
            "reply in a space is visible to every member, including people who are "
            "not on the allow-list, so tool output would leave the DM. A Webex bot "
            "only ever sees messages that @mention it in a space.",
            tags=["webex"],
        ),
    )
    allowed_room_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Room IDs",
            "Webex space IDs the bot may answer in when group spaces are enabled. "
            "Empty = deny all (fail closed), so turning the switch on alone grants "
            "nothing; the sender must ALSO be on the email allow-list.",
            tags=["webex"],
        ),
    )
    reply_in_thread: bool = field(
        default=True,
        metadata=_meta(
            "Reply in Thread",
            "Reply under the message's own thread when it has one, keeping a space "
            "readable. Webex threads are flat, so a reply always attaches to the "
            "thread root.",
            tags=["webex"],
        ),
    )
    wdm_base: str = field(
        default="",
        metadata=_meta(
            "Device Manager Base URL",
            "Override the Webex Device Manager host used for the inbound "
            "WebSocket. Empty (the default) discovers the org's own regional host "
            "per token, which is what a non-US-resident org needs; set this only "
            "to pin a REGIONAL WEBEX host for a network that reaches it but not "
            "the service catalog. Must be an https Webex host (*.wbx2.com, "
            "*.webex.com, *.ciscospark.com) — the bot token rides device "
            "registration, so anything else is refused and discovery is used "
            "instead. An outbound proxy belongs in HTTPS_PROXY, not here.",
            tags=["webex"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["webex"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["webex"],
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
            tags=["webex"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class IMessageConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the iMessage channel. macOS only, and the gateway must run "
            "on the Mac that is signed in to Messages. Needs no bot and no "
            "token — it drives Messages.app through the local imsg bridge, so "
            "the transport involves no third party. The turn itself still goes "
            "to the configured model provider, as on any channel.",
            tags=["imessage"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Messages Database Path",
            "Override the Messages database location. Empty (the default) lets "
            "the bridge use ~/Library/Messages/chat.db. Reading it needs Full "
            "Disk Access for the process the gateway runs as.",
            tags=["imessage"],
        ),
    )
    allowed_handles: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Handles",
            "Phone numbers or Apple ID emails permitted to message the agent. "
            "Empty = deny all (fail closed): anyone who knows this Mac's handle "
            "can send to it. Formatting is ignored, so '+61 400 000 000' and "
            "'+61400000000' are the same handle.",
            tags=["imessage"],
        ),
    )
    service: str = field(
        default="imessage",
        metadata=_meta(
            "Send Service",
            "Which service outbound replies use: 'imessage' (default), 'sms', "
            "or 'auto' to let the bridge fall back to SMS when iMessage is "
            "unavailable. Inbound is unaffected — the channel answers on "
            "whichever service the message arrived over.",
            tags=["imessage"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to "
            "/compact or /new instead of auto-compacting.",
            tags=["imessage"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["imessage"],
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
            tags=["imessage"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )
        # An unrecognized service would be forwarded to the bridge and rejected
        # per send, turning a typo into a channel that accepts messages and
        # never answers. Fall back to the safe default instead.
        service = (self.service or "").strip().lower()
        self.service = (
            service if service in IMESSAGE_SERVICES else field_default(IMessageConfig, "service")
        )


@dataclass
class TeamsConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Microsoft Teams channel (self-hosted inbound HTTPS "
            "webhook via the Bot Framework). Requires a public HTTPS endpoint "
            "pointing at /api/messaging/teams plus MICROSOFT_APP_ID and "
            "MICROSOFT_APP_PASSWORD (env/.env) or teams.app_id/app_password.",
            tags=["teams"],
        ),
    )
    app_id: str = field(
        default="",
        metadata=_meta(
            "App ID",
            "Microsoft App (Client) ID of the Azure Bot registration. Prefer "
            "the MICROSOFT_APP_ID credential (env/.env) over storing it here.",
            tags=["teams"],
        ),
    )
    app_password: str = field(
        default="",
        metadata=_meta(
            "App Password",
            "Azure Bot client secret. Set ONLY via the MICROSOFT_APP_PASSWORD "
            "credential (env/.env); it is deliberately NOT read from config.json "
            "so the agent-readable config never holds the secret.",
            tags=["teams"],
            sensitive=True,
        ),
    )
    tenant_id: str = field(
        default="",
        metadata=_meta(
            "Tenant ID",
            "Azure AD tenant id for a single-tenant bot. Leave empty for a "
            "multi-tenant bot (uses the botframework.com token authority).",
            tags=["teams"],
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Azure AD UPNs/emails OR AAD object ids permitted to DM the bot. "
            "Teams activities reliably carry the sender's object id (email is "
            "often absent), so listing object ids works out of the box; emails "
            "are matched when Teams supplies them. Empty = deny all (fail "
            "closed): a Teams bot is reachable by anyone in the org.",
            tags=["teams"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["teams"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["teams"],
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
            tags=["teams"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable. Mirrors
        # WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )
