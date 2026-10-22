"""The ``agents.*`` writes the agent surface makes, on the core's typed client.

:class:`SurfaceClient` extends :class:`~.client.CodeChannelsClient` (the core
keeps only what the core itself calls), so every method here inherits its rules:
nothing is sent that fails local validation, ``ok`` is checked, a write is never
retried, the install-wide refusals reach the shared latch, and anything logged is
redacted. The channel is always the first argument and always the caller's
pinned channel; nothing here chooses one.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.models import (
    MAX_TITLE_CHARS,
    CodeChannelApiError,
    CodeChannelRequestError,
    _check_id,
)

#: ``setView`` / ``setCanvasContent`` content limit, in UTF-8 bytes.
MAX_CONTENT_BYTES = 1_000_000
#: Slack caps a channel at this many runtime commands, across every agent.
MAX_COMMANDS = 10
#: ``conversations.canvases.create`` answers this when the channel has one; a
#: second create can never succeed and is never attempted.
CANVAS_ALREADY_EXISTS = "channel_canvas_already_exists"
#: The session statuses an agent may set (``closed`` is Slack's, via archive).
AGENT_STATUSES = frozenset({"processing", "suspended", "active"})
VIEW_TYPES = frozenset({"html", "block_kit", "canvas"})


def _check_size(text: str, what: str) -> None:
    if len(text.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise CodeChannelRequestError(f"{what} exceeds {MAX_CONTENT_BYTES} bytes")


class SurfaceClient(CodeChannelsClient):
    """Views, canvas, commands, summary, rename, status and archive."""

    async def set_view(
        self,
        channel_id: str,
        *,
        type: str,
        view_key: str,
        name: str = "",
        content: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        canvas_id: str = "",
        access_level: str = "",
    ) -> str:
        """``agents.conversations.setView`` (upsert by ``view_key``); returns the view id."""
        _check_id(channel_id, "channel_id")
        _check_id(view_key, "view_key")
        if type not in VIEW_TYPES:
            raise CodeChannelRequestError(f"unknown view type {type!r}")
        body: dict[str, Any] = {"channel_id": channel_id, "type": type, "view_key": view_key}
        if name:
            body["name"] = name
        if type == "html":
            if not content:
                raise CodeChannelRequestError("an html view needs content")
            _check_size(content, "content")
            body["content"] = content
        elif type == "block_kit":
            if not blocks:
                raise CodeChannelRequestError("a block_kit view needs blocks")
            body["blocks"] = list(blocks)
        else:
            _check_id(canvas_id, "canvas_id")
            body["canvas_id"] = canvas_id
            if access_level:
                if access_level != "comment":
                    # The plan loop: people comment, only the agent edits.
                    raise CodeChannelRequestError("a canvas view is comment-only")
                body["access_level"] = access_level
        data = await self._call("agents.conversations.setView", body)
        return str(data.get("view_id") or "")

    async def create_canvas(self, channel_id: str, markdown: str, *, title: str = "") -> str:
        """``conversations.canvases.create``; returns the canvas id (never retried)."""
        _check_id(channel_id, "channel_id")
        _check_size(markdown, "markdown")
        body: dict[str, Any] = {
            "channel_id": channel_id,
            "document_content": {"type": "markdown", "markdown": markdown},
        }
        if title:
            body["title"] = title[:MAX_TITLE_CHARS]
        data = await self._call("conversations.canvases.create", body)
        canvas_id = str(data.get("canvas_id") or "")
        if not canvas_id:
            raise CodeChannelApiError("conversations.canvases.create", "missing_canvas_id")
        return canvas_id

    async def set_canvas_content(self, channel_id: str, canvas_id: str, markdown: str) -> None:
        """``agents.conversations.setCanvasContent`` (replaces the canvas markdown)."""
        _check_id(channel_id, "channel_id")
        _check_id(canvas_id, "canvas_id")
        _check_size(markdown, "markdown")
        body = {"channel": channel_id, "canvas_id": canvas_id, "content": markdown}
        await self._call("agents.conversations.setCanvasContent", body)

    async def set_summary_message(self, channel_id: str, message_ts: str) -> None:
        """Record *message_ts* as the channel's ``code_channel.summary_message``."""
        _check_id(channel_id, "channel_id")
        _check_id(message_ts, "message_ts")
        body = {
            "channel_id": channel_id,
            "code_channel": {"summary_message": {"message_ts": message_ts}},
        }
        await self._call("agents.conversations.setProperties", body)

    async def set_commands(self, channel_id: str, commands: list[dict[str, str]]) -> int:
        """``agents.conversations.setCommands`` (a full replacement of THIS app's set)."""
        _check_id(channel_id, "channel_id")
        if len(commands) > MAX_COMMANDS:
            raise CodeChannelRequestError(f"at most {MAX_COMMANDS} commands")
        names = [c.get("name", "") for c in commands]
        if len(set(names)) != len(names) or not all(names):
            raise CodeChannelRequestError("command names must be present and unique")
        body = {"channel_id": channel_id, "commands": [dict(c) for c in commands]}
        data = await self._call("agents.conversations.setCommands", body)
        count = data.get("command_count")
        return count if isinstance(count, int) else len(commands)

    async def archive(self, channel_id: str, *, summary_message_ts: str) -> None:
        """``agents.conversations.archive`` with the summary Slack keeps afterwards."""
        _check_id(channel_id, "channel_id")
        _check_id(summary_message_ts, "summary_message_ts")
        body = {"channel_id": channel_id, "summary_message_ts": summary_message_ts}
        await self._call("agents.conversations.archive", body)

    async def rename(self, channel_id: str, title: str) -> str:
        """``agents.sessions.rename`` for a code channel (never a ``thread_ts``)."""
        _check_id(channel_id, "channel_id")
        title = title.strip() if isinstance(title, str) else ""
        if not title or len(title) > MAX_TITLE_CHARS:
            raise CodeChannelRequestError(f"title must be 1-{MAX_TITLE_CHARS} characters")
        data = await self._call(
            "agents.sessions.rename", {"channel_id": channel_id, "title": title}
        )
        return str(data.get("title") or title)

    async def set_status(self, channel_id: str, status: str) -> None:
        """``agents.sessions.setStatus`` for a code channel (never a ``thread_ts``)."""
        _check_id(channel_id, "channel_id")
        if status not in AGENT_STATUSES:
            raise CodeChannelRequestError(f"unknown session status {status!r}")
        await self._call("agents.sessions.setStatus", {"channel_id": channel_id, "status": status})
