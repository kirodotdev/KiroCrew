"""Typed client for Slack's ``agents.conversations.*`` family.

One method per Slack method, each taking a validated request from
:mod:`.models` and answering a parsed value or raising
:class:`~.models.CodeChannelApiError`. The client wraps a single
``api_call(method, json=...)`` coroutine -- ``slack_sdk``'s
``AsyncWebClient.api_call`` in production, a recorded fake in tests -- so it
holds no token itself and can be built per call.

Rules every method follows:

* **Validate before sending.** ``request.validate()`` runs first; a refused
  request never reaches Slack.
* **Check ``ok``.** A response without ``ok: true`` raises, even when the SDK
  did not.
* **Never retry a write.** A write that failed in transport may have landed,
  so it raises with ``maybe_applied=True`` and the caller reconciles (for
  ``create``, by repeating it with the same ``session_id``, which Slack
  treats as idempotent).
* **Redact.** Anything logged goes through :func:`redact_credentials`, so a
  token echoed in an SDK error never reaches the log.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.security import redact_credentials
from kiro_crew.slack.code_channels.models import (
    ChannelInfo,
    CodeChannelApiError,
    CodeChannelRequestError,
    CreateRequest,
    _check_id,
)

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Any]]

#: Read-only methods: a transport failure on these means nothing was written.
_READS = frozenset({"conversations.info"})

#: ``agents.conversations.setView`` refuses ``content`` over this many bytes
#: (``content_too_large``); checked here so the request never leaves.
MAX_VIEW_CONTENT_BYTES = 1_000_000


def _response_data(resp: Any) -> dict[str, Any]:
    """The JSON body of an SDK response (``SlackResponse`` or a plain dict)."""
    data = getattr(resp, "data", resp)
    return data if isinstance(data, dict) else {}


def _error_code(exc: BaseException) -> str:
    """Slack's ``error`` string from a ``SlackApiError``, else ``""``."""
    data = _response_data(getattr(exc, "response", None))
    code = data.get("error")
    return str(code) if code else ""


def _report(code: str, method: str) -> None:
    """Hand every refusal to the shared capability latch.

    ``features.latch_unavailable`` keeps only the install-wide codes
    (``feature_disabled`` / ``missing_scope`` / ``not_allowed_token_type``) and
    switches code channels off for the process on the first one.
    """
    from kiro_crew.slack import features

    if features.latch_unavailable(features.CODE_CHANNELS, code):
        logger.info("%s latched code channels off (%s)", method, code)


def _safe(text: str) -> str:
    return redact_credentials(text)[0]


class CodeChannelsClient:
    """``agents.conversations.create`` and ``conversations.info`` for code channels."""

    def __init__(self, api_call: ApiCall, *, team_for: Callable[[str], str] | None = None) -> None:
        self._api_call = api_call
        # Org-wide installs need the channel's home team on reads; the live
        # client caches it per channel (``RealSlackClient._channel_team``).
        self._team_for = team_for

    @classmethod
    def from_slack(cls, slack: Any) -> CodeChannelsClient | None:
        """Build one over the live Slack client's web client, or None without one."""
        web = getattr(slack, "_web", None)
        api_call = getattr(web, "api_call", None)
        if not callable(api_call):
            return None
        cache = getattr(slack, "_channel_team", None)
        team_for = (lambda ch: str(cache.get(ch) or "")) if isinstance(cache, dict) else None
        return cls(api_call, team_for=team_for)

    async def _call(
        self, method: str, body: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        try:
            if query:
                # Read methods such as conversations.info take form/query
                # parameters, not a JSON body.
                resp = await self._api_call(method, http_verb="GET", params=body)
            else:
                resp = await self._api_call(method, json=body)
        except Exception as exc:
            code = _error_code(exc)
            if code:
                logger.info("%s refused: %s", method, code)
                _report(code, method)
                raise CodeChannelApiError(method, code) from None
            logger.warning("%s did not complete: %s", method, _safe(repr(exc)))
            raise CodeChannelApiError(
                method, "transport_error", maybe_applied=method not in _READS
            ) from None
        data = _response_data(resp)
        if data.get("ok") is not True:
            code = str(data.get("error") or "not_ok")
            logger.info("%s refused: %s", method, code)
            _report(code, method)
            raise CodeChannelApiError(method, code)
        for warning in data.get("response_metadata", {}).get("warnings", []) or ():
            logger.info("%s warning: %s", method, _safe(str(warning)))
        return data

    # ── Structure ────────────────────────────────────────────────────────

    async def create(self, request: CreateRequest) -> str:
        """Open a code channel; returns its id. Idempotent per ``session_id``."""
        request.validate()
        data = await self._call("agents.conversations.create", request.body())
        channel_id = str(data.get("channel_id") or "")
        if not channel_id:
            raise CodeChannelApiError("agents.conversations.create", "missing_channel_id")
        return channel_id

    # ── Context bar and views (the workspace feature's writes) ───────────

    async def set_context_bar(self, channel_id: str, items: list[dict[str, str]]) -> None:
        """``agents.conversations.setProperties`` with ``code_channel.context_bar_items``.

        A full replacement: the list sent is the whole bar. The caller builds
        items within Slack's limits (at most five, unique keys, known icons).
        """
        _check_id(channel_id, "channel_id")
        body = {"channel_id": channel_id, "code_channel": {"context_bar_items": list(items)}}
        await self._call("agents.conversations.setProperties", body)

    async def set_diff_view(
        self, channel_id: str, content: str, *, base_branch: str = "", head_branch: str = ""
    ) -> str:
        """``agents.conversations.setView`` ``type=diff`` (a per-channel singleton).

        Returns the ``view_id`` Slack answers with ("" when it sent none).
        """
        _check_id(channel_id, "channel_id")
        if len(content.encode("utf-8")) > MAX_VIEW_CONTENT_BYTES:
            raise CodeChannelRequestError(f"diff exceeds {MAX_VIEW_CONTENT_BYTES} bytes")
        body: dict[str, Any] = {"channel_id": channel_id, "type": "diff", "content": content}
        if base_branch:
            body["base_branch"] = base_branch
        if head_branch:
            body["head_branch"] = head_branch
        data = await self._call("agents.conversations.setView", body)
        return str(data.get("view_id") or "")

    async def remove_view(self, channel_id: str, view_id: str) -> None:
        """``agents.conversations.removeView`` by ``view_id``."""
        _check_id(channel_id, "channel_id")
        _check_id(view_id, "view_id")
        body = {"channel_id": channel_id, "view_id": view_id}
        await self._call("agents.conversations.removeView", body)

    # ── Recognition ──────────────────────────────────────────────────────

    async def channel_info(self, channel_id: str) -> ChannelInfo:
        """Read whether *channel_id* is a code channel and which agents it has."""
        _check_id(channel_id, "channel_id")
        params = {"channel": channel_id}
        team = self._team_for(channel_id) if self._team_for else ""
        if team:
            params["team_id"] = team
        data = await self._call("conversations.info", params, query=True)
        return parse_channel_info(channel_id, data.get("channel"))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_channel_info(channel_id: str, raw: Any) -> ChannelInfo:
    """Parse a ``conversations.info`` ``channel`` object (tolerates any shape)."""
    channel = _dict(raw)
    props = _dict(channel.get("properties"))
    record = _dict(props.get("record_channel"))
    session = _dict(props.get("agent_session"))
    bots = session.get("encoded_agent_bot_user_ids")
    origin = _dict(session.get("origin_link"))
    return ChannelInfo(
        channel_id=channel_id,
        is_code_channel=record.get("record_type") == "agent_channel",
        agent_bot_user_ids=(
            frozenset(str(b) for b in bots if b) if isinstance(bots, list) else frozenset()
        ),
        is_private=channel.get("is_private") is True,
        name=str(channel.get("name") or ""),
        origin_channel_id=str(origin.get("channel_id") or ""),
        origin_ts=str(origin.get("ts") or ""),
    )
