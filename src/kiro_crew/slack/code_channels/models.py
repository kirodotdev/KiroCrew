"""Typed requests, responses and errors for Slack's code-channel API.

Every request a caller can build here is validated BEFORE anything is sent:
Slack accepts several malformed shapes and silently "fixes" them, so the only
way to find out a request was wrong is to refuse it locally. :meth:`validate`
raises :class:`CodeChannelRequestError`, which is a programming error in the
caller, never a Slack outcome. A method added on top of the client builds its
request from :func:`_require` / :func:`_check_id` and sends it through
``CodeChannelsClient._call``, which owns the ``ok`` check and the latch.

Slack outcomes are :class:`CodeChannelApiError`. Its ``unavailable`` flag marks
the codes that mean "this workspace or install cannot use code channels at all"
(``feature_disabled``, ``missing_scope``, ...): callers degrade on it -- answer
in place, as if the feature were off -- instead of reporting a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.slack import features

#: Channel / session title limit (``agents.sessions.rename``).
MAX_TITLE_CHARS = 200

#: Answers that mean this INSTALL cannot use code channels; the first one
#: latches the feature off for the process (``slack.features.latch_unavailable``).
LATCH_ERRORS = features.LATCH_ERRORS

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
