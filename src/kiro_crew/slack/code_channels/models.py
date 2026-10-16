"""Typed requests, responses and errors for Slack's code-channel API.

Every request a caller can build here is validated BEFORE anything is sent:
Slack accepts several malformed shapes and silently "fixes" them (it keeps only
the first five context-bar items, it turns an action item that carries a url
into an info item), so the only way to find out a request was wrong is to refuse
it locally. :meth:`validate` raises :class:`CodeChannelRequestError`, which is
a programming error in the caller, never a Slack outcome.

Slack outcomes are :class:`CodeChannelApiError`. Its ``unavailable`` flag marks
the codes that mean "this workspace or install cannot use code channels at all"
(``feature_disabled``, ``missing_scope``, ...): callers degrade on it -- answer
in place, as if the feature were off -- instead of reporting a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Slack keeps at most this many context-bar items.
MAX_CONTEXT_BAR_ITEMS = 5
#: Slack caps a channel at this many runtime commands, across every agent.
MAX_COMMANDS = 10
#: ``setView`` / ``setCanvasContent`` content limit, in UTF-8 bytes.
MAX_VIEW_CONTENT_BYTES = 1_000_000
#: Slack caps a channel at this many view tabs.
MAX_VIEWS = 5
#: Channel / session title limit (``agents.sessions.rename``).
MAX_TITLE_CHARS = 200

#: Answers that mean this INSTALL cannot use code channels: the first one latches
#: the feature off for the process (``settings.report_slack_refusal``).
LATCH_ERRORS = frozenset({"feature_disabled", "missing_scope", "not_allowed_token_type"})

#: Answers a caller degrades on -- answer in place, as without code channels --
#: instead of reporting a failure: the latching ones plus those scoped to one
#: user, workspace or channel, which switch nothing off for anyone else.
UNAVAILABLE_ERRORS = LATCH_ERRORS | frozenset(
    {
        "user_not_enabled",
        "enterprise_is_restricted",
        "app_not_installed_on_team",
        "no_actions_url",
    }
)

#: ``conversations.canvases.create`` answers this when the channel already has
#: one. Retrying cannot succeed and a second create must never be attempted.
CANVAS_ALREADY_EXISTS = "channel_canvas_already_exists"

ContextBarIcon = Literal[
    "branch",
    "folder",
    "hierarchy",
    "life-ring",
    "link",
    "globe",
    "terminal",
    "code",
    "search",
    "lock",
]
_ICONS = frozenset(
    {
        "branch",
        "folder",
        "hierarchy",
        "life-ring",
        "link",
        "globe",
        "terminal",
        "code",
        "search",
        "lock",
    }
)
ViewType = Literal["html", "diff", "block_kit", "canvas", "pull_request"]
_VIEW_TYPES = frozenset({"html", "diff", "block_kit", "canvas", "pull_request"})
SessionStatus = Literal["active", "processing", "suspended", "closed"]
SESSION_STATUSES = frozenset({"active", "processing", "suspended", "closed"})


class CodeChannelError(Exception):
    """Base class for everything this package raises about the Slack API."""


class CodeChannelRequestError(CodeChannelError, ValueError):
    """A request was refused locally before it was sent (caller bug)."""


class CodeChannelApiError(CodeChannelError):
    """Slack answered ``ok: false`` (or the call never completed).

    ``code`` is Slack's ``error`` string (``"transport_error"`` when the call
    did not complete). ``unavailable`` is True when the code means the feature
    cannot be used on this install -- see :data:`UNAVAILABLE_ERRORS`.
    ``maybe_applied`` is True when a write may have landed even though no
    success was observed (a transport failure after the request left), so a
    caller must reconcile instead of retrying blindly.
    """

    def __init__(self, method: str, code: str, *, maybe_applied: bool = False) -> None:
        self.method = method
        self.code = code or "unknown_error"
        self.maybe_applied = maybe_applied
        super().__init__(f"{method} failed: {self.code}")

    @property
    def unavailable(self) -> bool:
        return self.code in UNAVAILABLE_ERRORS


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise CodeChannelRequestError(message)


def _check_id(value: str, what: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), f"{what} is required")


def _check_content(content: str | None, what: str = "content") -> None:
    if content is None:
        return
    _require(isinstance(content, str), f"{what} must be a string")
    _require(
        len(content.encode("utf-8")) <= MAX_VIEW_CONTENT_BYTES,
        f"{what} exceeds {MAX_VIEW_CONTENT_BYTES} bytes",
    )


@dataclass(frozen=True)
class CreateRequest:
    """``agents.conversations.create``.

    ``is_private`` None means "inherit the origin's privacy" (Slack's default
    when an origin link is given). Sending an explicit ``False`` would make a
    public channel out of a private thread, so callers leave it None.
    """

    name: str
    session_id: str
    origin_channel_id: str = ""
    origin_message_ts: str = ""
    team_id: str = ""
    is_private: bool | None = None

    def validate(self) -> None:
        _require(
            bool(self.name.strip()) or bool(self.origin_channel_id), "name or origin is required"
        )
        _require(len(self.name) <= MAX_TITLE_CHARS, "name is too long")
        _require(bool(self.session_id), "session_id is required (idempotency key)")
        _require(
            bool(self.origin_channel_id) == bool(self.origin_message_ts),
            "origin_channel_id and origin_message_ts go together",
        )

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"name": self.name, "session_id": self.session_id}
        if self.origin_channel_id:
            body["origin_channel_id"] = self.origin_channel_id
            body["origin_message_ts"] = self.origin_message_ts
        if self.team_id:
            body["team_id"] = self.team_id
        if self.is_private is not None:
            body["is_private"] = self.is_private
        return body


@dataclass(frozen=True)
class ContextBarItem:
    """One context-bar entry: a link (``url``) or an action, never both."""

    key: str
    label: str
    icon: str = ""
    url: str = ""
    item_type: Literal["", "info", "action"] = ""

    def validate(self) -> None:
        _check_id(self.key, "context bar key")
        _check_id(self.label, "context bar label")
        if self.icon:
            _require(self.icon in _ICONS, f"unknown context bar icon {self.icon!r}")
        if self.item_type == "action":
            _require(not self.url, "an action item carries no url")
        if self.url:
            _require(self.url.startswith("https://"), "context bar url must be https")

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label}
        if self.icon:
            out["icon"] = self.icon
        if self.url:
            out["url"] = self.url
        if self.item_type:
            out["item_type"] = self.item_type
        return out


@dataclass(frozen=True)
class SetPropertiesRequest:
    """``agents.conversations.setProperties``.

    ``context_bar_items`` is a FULL replacement: None leaves the bar alone, an
    empty tuple clears it.
    """

    channel_id: str
    context_bar_items: tuple[ContextBarItem, ...] | None = None
    agent_resource: dict[str, Any] | None = None
    summary_message: dict[str, Any] | None = None

    def validate(self) -> None:
        _check_id(self.channel_id, "channel_id")
        _require(
            self.context_bar_items is not None
            or self.agent_resource is not None
            or self.summary_message is not None,
            "no properties provided",
        )
        if self.context_bar_items is not None:
            _require(
                len(self.context_bar_items) <= MAX_CONTEXT_BAR_ITEMS,
                f"at most {MAX_CONTEXT_BAR_ITEMS} context bar items",
            )
            keys = [item.key for item in self.context_bar_items]
            _require(len(set(keys)) == len(keys), "context bar keys must be unique")
            for item in self.context_bar_items:
                item.validate()

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": self.channel_id}
        code_channel: dict[str, Any] = {}
        if self.context_bar_items is not None:
            code_channel["context_bar_items"] = [i.body() for i in self.context_bar_items]
        if self.summary_message is not None:
            code_channel["summary_message"] = dict(self.summary_message)
        if code_channel:
            body["code_channel"] = code_channel
        if self.agent_resource is not None:
            body["agent_resource"] = dict(self.agent_resource)
        return body


@dataclass(frozen=True)
class AgentCommand:
    """One runtime slash command registered on a channel."""

    name: str
    description: str
    argument_hint: str = ""

    def validate(self) -> None:
        _check_id(self.name, "command name")
        _require(
            not self.name.startswith("/") and not any(c.isspace() for c in self.name),
            "command name takes no slash or whitespace",
        )
        _check_id(self.description, "command description")

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.argument_hint:
            out["argument_hint"] = self.argument_hint
        return out


@dataclass(frozen=True)
class SetCommandsRequest:
    """``agents.conversations.setCommands`` (full replacement of OUR set)."""

    channel_id: str
    commands: tuple[AgentCommand, ...] = ()

    def validate(self) -> None:
        _check_id(self.channel_id, "channel_id")
        _require(len(self.commands) <= MAX_COMMANDS, f"at most {MAX_COMMANDS} commands")
        names = [c.name for c in self.commands]
        _require(len(set(names)) == len(names), "command names must be unique")
        for command in self.commands:
            command.validate()

    def body(self) -> dict[str, Any]:
        return {"channel_id": self.channel_id, "commands": [c.body() for c in self.commands]}


@dataclass(frozen=True)
class SetViewRequest:
    """``agents.conversations.setView`` (upsert keyed by ``view_key``)."""

    channel_id: str
    type: ViewType = "html"
    view_key: str = ""
    content: str | None = None
    blocks: tuple[dict[str, Any], ...] | None = None
    canvas_id: str = ""
    access_level: Literal["", "read", "write", "comment"] = ""
    pr_url: str = ""
    name: str = ""
    base_branch: str = ""
    head_branch: str = ""
    csp: dict[str, Any] | None = field(default=None)

    def validate(self) -> None:
        _check_id(self.channel_id, "channel_id")
        _require(self.type in _VIEW_TYPES, f"unknown view type {self.type!r}")
        _check_content(self.content)
        if self.type in ("html", "block_kit", "canvas"):
            _check_id(self.view_key, "view_key")
        if self.type in ("html", "diff"):
            _require(self.content is not None, f"{self.type} view needs content")
        if self.type == "block_kit":
            _require(bool(self.blocks), "block_kit view needs blocks")
        if self.type == "canvas":
            _check_id(self.canvas_id, "canvas_id")
        if self.type == "pull_request":
            _require(self.pr_url.startswith("https://"), "pull_request view needs an https pr_url")

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": self.channel_id, "type": self.type}
        # diff is a per-channel singleton: Slack ignores view_key for it.
        if self.view_key and self.type != "diff":
            body["view_key"] = self.view_key
        if self.content is not None:
            body["content"] = self.content
        if self.blocks is not None:
            body["blocks"] = list(self.blocks)
        for key in ("canvas_id", "access_level", "pr_url", "name", "base_branch", "head_branch"):
            value = getattr(self, key)
            if value:
                body[key] = value
        if self.csp is not None:
            body["csp"] = dict(self.csp)
        return body


@dataclass(frozen=True)
class ViewRecord:
    """One view as ``setView`` / ``listViews`` report it."""

    view_id: str
    view_key: str = ""
    file_id: str = ""
    canvas_id: str = ""
    label: str = ""
    type: str = ""
    content_version: int = 0

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> ViewRecord:
        version = raw.get("content_version")
        return cls(
            view_id=str(raw.get("view_id") or ""),
            view_key=str(raw.get("view_key") or ""),
            file_id=str(raw.get("file_id") or ""),
            canvas_id=str(raw.get("canvas_id") or ""),
            label=str(raw.get("label") or raw.get("name") or ""),
            type=str(raw.get("type") or ""),
            content_version=version if isinstance(version, int) else 0,
        )


@dataclass(frozen=True)
class ChannelInfo:
    """What ``conversations.info`` says about a channel's code-channel record.

    ``origin_channel_id`` / ``origin_ts`` come from
    ``properties.agent_session.origin_link``, present when the channel was
    opened from a message.
    """

    channel_id: str
    is_code_channel: bool
    agent_bot_user_ids: frozenset[str] = frozenset()
    is_private: bool = False
    name: str = ""
    origin_channel_id: str = ""
    origin_ts: str = ""
