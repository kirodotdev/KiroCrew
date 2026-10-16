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
  treats as idempotent). ``channel_canvas_already_exists`` is final.
* **Redact.** Anything logged goes through :func:`redact_credentials`, so a
  token echoed in an SDK error never reaches the log.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.security import redact_credentials
from kiro_crew.slack.code_channels.models import (
    MAX_TITLE_CHARS,
    SESSION_STATUSES,
    ChannelInfo,
    CodeChannelApiError,
    CodeChannelRequestError,
    CreateRequest,
    SetCommandsRequest,
    SetPropertiesRequest,
    SetViewRequest,
    ViewRecord,
    _check_content,
    _check_id,
)

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Any]]

#: Read-only methods: a transport failure on these means nothing was written.
_READS = frozenset(
    {"agents.conversations.listViews", "agents.conversations.getCanvas", "conversations.info"}
)


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
    """Every refusal passes the latch, which acts only on the install-wide ones."""
    from kiro_crew.slack.code_channels.settings import report_slack_refusal

    report_slack_refusal(code, method=method)


def _safe(text: str) -> str:
    return redact_credentials(text)[0]


class CodeChannelsClient:
    """``agents.conversations.*`` (+ channel-level ``agents.sessions.setStatus``)."""

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

    async def archive(self, channel_id: str, *, summary_message_ts: str = "") -> None:
        _check_id(channel_id, "channel_id")
        body: dict[str, Any] = {"channel_id": channel_id}
        if summary_message_ts:
            body["summary_message_ts"] = summary_message_ts
        await self._call("agents.conversations.archive", body)

    async def set_properties(self, request: SetPropertiesRequest) -> None:
        request.validate()
        await self._call("agents.conversations.setProperties", request.body())

    async def set_commands(self, request: SetCommandsRequest) -> int:
        """Replace this agent's commands; returns Slack's ``command_count``."""
        request.validate()
        data = await self._call("agents.conversations.setCommands", request.body())
        count = data.get("command_count")
        return count if isinstance(count, int) else len(request.commands)

    # ── Views ────────────────────────────────────────────────────────────

    async def set_view(self, request: SetViewRequest) -> ViewRecord:
        request.validate()
        data = await self._call("agents.conversations.setView", request.body())
        return ViewRecord.parse(data)

    async def list_views(self, channel_id: str) -> list[ViewRecord]:
        _check_id(channel_id, "channel_id")
        data = await self._call("agents.conversations.listViews", {"channel_id": channel_id})
        views = data.get("views")
        return (
            [ViewRecord.parse(v) for v in views if isinstance(v, dict)]
            if isinstance(views, list)
            else []
        )

    async def remove_view(self, channel_id: str, *, view_key: str = "", view_id: str = "") -> None:
        _check_id(channel_id, "channel_id")
        if bool(view_key) == bool(view_id):
            raise CodeChannelRequestError("exactly one of view_key or view_id")
        body: dict[str, Any] = {"channel_id": channel_id}
        body["view_key" if view_key else "view_id"] = view_key or view_id
        await self._call("agents.conversations.removeView", body)

    # ── Canvases ─────────────────────────────────────────────────────────

    async def create_canvas(self, channel_id: str, markdown: str, *, title: str = "") -> str:
        """``conversations.canvases.create``; returns the canvas id.

        ``channel_canvas_already_exists`` is raised like any other refusal and
        must not be retried: the channel can hold only one.
        """
        _check_id(channel_id, "channel_id")
        _check_content(markdown, "markdown")
        body: dict[str, Any] = {
            "channel_id": channel_id,
            "document_content": {"type": "markdown", "markdown": markdown},
        }
        if title:
            body["title"] = title[:MAX_TITLE_CHARS]
        data = await self._call("conversations.canvases.create", body)
        return str(data.get("canvas_id") or "")

    async def get_canvas(
        self, channel_id: str, canvas_id: str, *, include_resolved: bool = False
    ) -> dict[str, Any]:
        _check_id(channel_id, "channel_id")
        _check_id(canvas_id, "canvas_id")
        body = {
            "channel": channel_id,
            "canvas_id": canvas_id,
            "content_format": "markdown",
            "include_resolved": include_resolved,
        }
        return await self._call("agents.conversations.getCanvas", body)

    async def set_canvas_content(self, channel_id: str, canvas_id: str, content: str) -> int:
        """Replace a canvas's markdown; returns ``sections_changed_count``."""
        _check_id(channel_id, "channel_id")
        _check_id(canvas_id, "canvas_id")
        _check_content(content)
        data = await self._call(
            "agents.conversations.setCanvasContent",
            {"channel": channel_id, "canvas_id": canvas_id, "content": content},
        )
        changed = data.get("sections_changed_count")
        return changed if isinstance(changed, int) else 0

    # ── Session status (whole channel = one session) ─────────────────────

    async def set_channel_status(self, channel_id: str, status: str) -> None:
        """``agents.sessions.setStatus`` for a code channel: never a thread_ts.

        Slack refuses a ``thread_ts`` in a code channel
        (``thread_ts_not_allowed``), so this method has no parameter for one.
        """
        _check_id(channel_id, "channel_id")
        if status not in SESSION_STATUSES:
            raise CodeChannelRequestError(f"unknown session status {status!r}")
        await self._call("agents.sessions.setStatus", {"channel_id": channel_id, "status": status})

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
