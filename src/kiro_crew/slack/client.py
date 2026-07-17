"""Slack API client abstraction for KiroCrew.

Provides an async interface for Slack Web API operations.
Uses an ABC so tests can swap in a mock without touching Slack.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import aiohttp
from slack_sdk.errors import SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# conversations.list pagination limits
_CONVERSATIONS_LIST_MAX_PAGES = 20  # 20 pages × 1000 = 20k channels max
_CONVERSATIONS_LIST_PAGE_SIZE = 1000


class SlackClientOps(ABC):
    """Core Slack operations needed by KiroCrew."""

    @abstractmethod
    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
    ) -> str:
        """Post a message, return its ts."""

    @abstractmethod
    async def post_blocks(
        self,
        channel: str,
        blocks: list[dict],
        text: str,
        thread_ts: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
    ) -> str:
        """Post a Block Kit message, return its ts."""

    @abstractmethod
    async def update_message(
        self, channel: str, ts: str, text: str = "", blocks: list[dict] | None = None
    ) -> None:
        """Edit an existing message."""

    @abstractmethod
    async def delete_message(self, channel: str, ts: str) -> None:
        """Delete a message."""

    @abstractmethod
    async def add_reaction(
        self, channel: str, ts: str, emoji: str, raise_on_error: bool = False
    ) -> None:
        """Add a reaction to a message.

        Best-effort by default (swallows errors). Pass raise_on_error=True
        when the caller needs to surface failures (e.g. an API route).
        """

    @abstractmethod
    async def remove_reaction(
        self, channel: str, ts: str, emoji: str, raise_on_error: bool = False
    ) -> None:
        """Remove a reaction from a message. See add_reaction for raise_on_error."""

    async def add_pin(self, channel: str, ts: str) -> None:
        """Pin a message to a channel."""

    async def remove_pin(self, channel: str, ts: str) -> None:
        """Unpin a message from a channel."""

    async def list_pins(self, channel: str) -> list[dict]:
        """List pinned messages in a channel as [{ts, text}, ...]."""
        return []

    @abstractmethod
    async def upload_file(
        self,
        channel: str,
        thread_ts: str,
        file: str,
        filename: str,
        title: str,
    ) -> None:
        """Upload a file to a Slack thread."""

    @abstractmethod
    async def open_dm(self, user_id: str) -> str:
        """Open a DM channel with a user, return channel ID."""

    @abstractmethod
    async def post_ephemeral(
        self, channel: str, user_id: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None
    ) -> None:
        """Post an ephemeral message visible only to the specified user."""

    @abstractmethod
    async def views_publish(self, user_id: str, view: dict) -> None:
        """Publish a Home Tab view for a user."""

    async def is_dm(self, channel: str) -> bool:
        """Check if a channel is a 1:1 DM via conversations.info.

        Default implementation uses channel ID prefix heuristic.
        Subclasses should override with the real API call.
        """
        return channel.startswith("D")

    async def views_open(self, trigger_id: str, view: dict) -> None:
        """Open a modal view."""

    async def views_update(self, view_id: str, view: dict) -> None:
        """Update an existing modal view."""

    # ── Streaming API (chat.startStream / appendStream / stopStream) ──

    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        initial_text: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """Start a streaming message. Returns ts or None if unsupported."""
        return None

    async def append_stream(self, channel: str, ts: str, text: str) -> bool:
        """Append text to a streaming message. Returns True on success."""
        return False

    async def stop_stream(self, channel: str, ts: str, final_text: str | None = None) -> bool:
        """Stop a streaming message. Returns True on success."""
        return False

    async def append_task(
        self,
        channel: str,
        ts: str,
        task_id: str,
        title: str,
        status: str,
        details: str = "",
        output: str = "",
    ) -> bool:
        """Append a task_update chunk to a streaming message. Returns True on success."""
        return False

    async def set_thread_status(self, channel: str, thread_ts: str, status: str) -> None:
        """Set assistant thread status via assistant.threads.setStatus.

        Pass an empty string to clear the status indicator.
        """

    async def set_thread_title(self, channel: str, thread_ts: str, title: str) -> None:
        """Set assistant thread title via assistant.threads.setTitle."""

    async def set_suggested_prompts(
        self, channel: str, thread_ts: str, prompts: list[dict[str, str]]
    ) -> None:
        """Set suggested prompts via assistant.threads.setSuggestedPrompts.

        Each prompt is a dict with 'title' (button label) and 'message'
        (text sent when clicked).
        """

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        """Fetch a single message's text by channel and timestamp.

        Returns the message text, or None on failure.
        """
        return None

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        """Fetch thread replies. Returns list of message dicts with 'user'/'bot_id' and 'text'."""
        return []

    async def conversations_list(self) -> list[dict]:
        """List channels the bot can see. Each dict has at least ``id`` and ``name``.

        Default returns empty list — subclasses must override to hit Slack API.
        Used by ``ChannelNameResolver`` to populate the channel-name cache.
        """
        return []

    async def download_file(self, url: str, dest: str) -> None:
        """Download a Slack-hosted file to a local path."""
        raise NotImplementedError


class RealSlackClient(SlackClientOps):
    """Slack Web API client backed by slack_sdk."""

    def __init__(self, bot_token: str):
        self._web = AsyncWebClient(token=bot_token)
        # Channel→workspace team_id cache.  Populated from inbound events
        # by record_channel_team() and used to auto-inject team_id into
        # outbound chat.* / reactions.* calls so org-wide (Enterprise Grid)
        # installs route to the correct workspace.  Empty for non-org-wide
        # installs — _inject_team becomes a no-op in that case.
        self._channel_team: dict[str, str] = {}

    def record_channel_team(self, channel: str, team_id: str) -> None:
        """Cache the workspace team_id for a channel.  Idempotent.

        Called from inbound event handlers so subsequent outbound API
        calls on the same channel can include team_id, which org-wide
        installs require to avoid ``team_access_not_granted``.
        """
        if not (channel and team_id):
            return
        # Lazily create the cache so callers that bypass __init__ (e.g.
        # tests using RealSlackClient.__new__()) don't AttributeError.
        cache = getattr(self, "_channel_team", None)
        if cache is None:
            cache = {}
            self._channel_team = cache
        cache[channel] = team_id

    def _inject_team(self, channel: str, kwargs: dict[str, Any]) -> None:
        """Add team_id to kwargs from cache, if known and not already set.

        No-op for non-Enterprise installs (cache will be empty).  Also a
        no-op when callers bypass ``__init__`` (the cache attr is absent).
        """
        if "team_id" in kwargs:
            return
        cache = getattr(self, "_channel_team", None)
        if cache:
            tid = cache.get(channel)
            if tid:
                kwargs["team_id"] = tid

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        if unfurl_links is not None:
            kwargs["unfurl_links"] = unfurl_links
        if unfurl_media is not None:
            kwargs["unfurl_media"] = unfurl_media
        if reply_broadcast and thread_ts is not None:
            kwargs["reply_broadcast"] = True
        self._inject_team(channel, kwargs)
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def post_blocks(
        self,
        channel: str,
        blocks: list[dict],
        text: str,
        thread_ts: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"channel": channel, "blocks": blocks, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        if unfurl_links is not None:
            kwargs["unfurl_links"] = unfurl_links
        if unfurl_media is not None:
            kwargs["unfurl_media"] = unfurl_media
        if reply_broadcast and thread_ts is not None:
            kwargs["reply_broadcast"] = True
        self._inject_team(channel, kwargs)
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def update_message(
        self, channel: str, ts: str, text: str = "", blocks: list[dict] | None = None
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        self._inject_team(channel, kwargs)
        await self._web.chat_update(**kwargs)

    async def delete_message(self, channel: str, ts: str) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts}
        self._inject_team(channel, kwargs)
        await self._web.chat_delete(**kwargs)

    async def add_reaction(
        self, channel: str, ts: str, emoji: str, raise_on_error: bool = False
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "name": emoji, "timestamp": ts}
        self._inject_team(channel, kwargs)
        try:
            await self._web.reactions_add(**kwargs)
        except Exception:
            if raise_on_error:
                raise
            # else best-effort: swallow

    async def remove_reaction(
        self, channel: str, ts: str, emoji: str, raise_on_error: bool = False
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "name": emoji, "timestamp": ts}
        self._inject_team(channel, kwargs)
        try:
            await self._web.reactions_remove(**kwargs)
        except Exception:
            if raise_on_error:
                raise
            # else best-effort: swallow

    async def add_pin(self, channel: str, ts: str) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "timestamp": ts}
        self._inject_team(channel, kwargs)
        await self._web.pins_add(**kwargs)

    async def remove_pin(self, channel: str, ts: str) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "timestamp": ts}
        self._inject_team(channel, kwargs)
        await self._web.pins_remove(**kwargs)

    async def list_pins(self, channel: str) -> list[dict]:
        kwargs: dict[str, Any] = {"channel": channel}
        self._inject_team(channel, kwargs)
        resp = await self._web.pins_list(**kwargs)
        items = resp.get("items") or []
        pins: list[dict] = []
        for item in items:
            msg = item.get("message") or {}
            ts = msg.get("ts")
            if ts:
                pins.append({"ts": ts, "text": msg.get("text", "")})
        return pins

    async def views_publish(self, user_id: str, view: dict) -> None:
        await self._web.views_publish(user_id=user_id, view=view)

    async def upload_file(
        self,
        channel: str,
        thread_ts: str,
        file: str,
        filename: str,
        title: str,
    ) -> None:
        await self._web.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=file,
            filename=filename,
            title=title,
        )

    async def open_dm(self, user_id: str) -> str:
        resp = await self._web.conversations_open(users=[user_id])
        return resp["channel"]["id"]

    async def post_ephemeral(
        self, channel: str, user_id: str, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None
    ) -> None:
        kwargs: dict = {"channel": channel, "user": user_id, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        self._inject_team(channel, kwargs)
        await self._web.chat_postEphemeral(**kwargs)

    async def is_dm(self, channel: str) -> bool:
        """Check if channel is a 1:1 DM via conversations.info API."""
        try:
            kwargs: dict[str, Any] = {"channel": channel}
            self._inject_team(channel, kwargs)
            resp = await self._web.conversations_info(**kwargs)
            ch = resp.data.get("channel", {}) if hasattr(resp, "data") else {}  # type: ignore[union-attr]
            return bool(ch.get("is_im", False))
        except Exception:
            # Fallback to heuristic if API call fails
            return channel.startswith("D")

    async def views_open(self, trigger_id: str, view: dict) -> None:
        await self._web.views_open(trigger_id=trigger_id, view=view)

    async def views_update(self, view_id: str, view: dict) -> None:
        await self._web.views_update(view_id=view_id, view=view)

    async def get_user_info(self, user_id: str) -> dict[str, str]:
        """Look up a user's profile via users.info API."""
        try:
            resp = await self._web.users_info(user=user_id)
            user: dict = resp.get("user", {})
            profile: dict = user.get("profile", {})
            return {
                "id": user_id,
                "name": user.get("name", user_id),
                "real_name": profile.get("real_name") or user.get("name") or user_id,
            }
        except Exception:
            return {"id": user_id, "name": user_id, "real_name": user_id}

    async def get_user_profile(self, user_id: str) -> dict:
        """Look up a user's full Slack profile via users.info API."""
        resp = await self._web.users_info(user=user_id)
        user: dict = resp.get("user", {})
        profile: dict = user.get("profile", {})
        info: dict = {
            "id": user_id,
            "name": user.get("name", user_id),
            "real_name": profile.get("real_name", ""),
            "display_name": profile.get("display_name", ""),
            "title": profile.get("title", ""),
            "status_text": profile.get("status_text", ""),
            "status_emoji": profile.get("status_emoji", ""),
            "timezone": user.get("tz", ""),
            "is_bot": bool(user.get("is_bot")),
            "is_admin": bool(user.get("is_admin")),
            "image_url": profile.get("image_192", ""),
        }
        return {k: v for k, v in info.items() if v is not None and v != ""}

    # ── Streaming API ──

    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        initial_text: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """Start a streaming message via chat.startStream with task plan mode."""
        try:
            body: dict[str, Any] = {
                "channel": channel,
                "thread_ts": thread_ts,
                "task_display_mode": "plan",
            }
            # Workspace routing for org-wide installs.  Prefer the cached
            # team for this channel — that's the workspace the channel lives
            # in, which is what ``body["team_id"]`` must route to.  Fall
            # back to the recipient's team_id only when the cache hasn't
            # been populated yet (first message into a never-seen channel).
            cache = getattr(self, "_channel_team", None)
            cached_team = cache.get(channel) if cache else None
            workspace_team = cached_team or team_id
            if workspace_team:
                body["team_id"] = workspace_team
            if team_id:
                body["recipient_team_id"] = team_id
            if user_id:
                body["recipient_user_id"] = user_id
            chunks: list[dict[str, Any]] = []
            if initial_text:
                chunks.append({"type": "markdown_text", "text": initial_text})
            if chunks:
                body["chunks"] = chunks
            resp = await self._web.api_call("chat.startStream", json=body)
            return resp.get("ts")
        except Exception:
            logger.warning("chat.startStream failed", exc_info=True)
            return None

    async def append_stream(self, channel: str, ts: str, text: str) -> bool:
        """Append markdown text to a streaming message via chat.appendStream."""
        try:
            body: dict[str, Any] = {
                "channel": channel,
                "ts": ts,
                "chunks": [{"type": "markdown_text", "text": text}],
            }
            self._inject_team(channel, body)
            await self._web.api_call("chat.appendStream", json=body)
            return True
        except Exception:
            logger.debug("chat.appendStream failed", exc_info=True)
            return False

    async def append_task(
        self,
        channel: str,
        ts: str,
        task_id: str,
        title: str,
        status: str,
        details: str = "",
        output: str = "",
    ) -> bool:
        """Append a task_update chunk to a streaming message."""
        try:
            task: dict[str, Any] = {
                "type": "task_update",
                "id": task_id,
                "title": title,
                "status": status,
            }
            if details:
                task["details"] = details
            if output:
                task["output"] = output
            body: dict[str, Any] = {"channel": channel, "ts": ts, "chunks": [task]}
            self._inject_team(channel, body)
            await self._web.api_call("chat.appendStream", json=body)
            return True
        except Exception:
            logger.debug("chat.appendStream task_update failed", exc_info=True)
            return False

    async def stop_stream(self, channel: str, ts: str, final_text: str | None = None) -> bool:
        """Stop a streaming message via chat.stopStream.

        We intentionally do NOT call chat.update after stopping — the streamed
        content already has rich formatting (syntax-highlighted code blocks, etc.)
        that chat.update would downgrade to plain mrkdwn.
        """
        try:
            body: dict[str, Any] = {"channel": channel, "ts": ts}
            self._inject_team(channel, body)
            await self._web.api_call("chat.stopStream", json=body)
            return True
        except Exception:
            logger.debug("chat.stopStream failed", exc_info=True)
            return False

    async def set_thread_status(self, channel: str, thread_ts: str, status: str) -> None:
        """Set assistant thread loading status via assistant.threads.setStatus."""
        try:
            await self._web.api_call(
                "assistant.threads.setStatus",
                params={"channel_id": channel, "thread_ts": thread_ts, "status": status},
            )
        except Exception:
            logger.debug("assistant.threads.setStatus failed", exc_info=True)

    async def set_thread_title(self, channel: str, thread_ts: str, title: str) -> None:
        """Set assistant thread title via assistant.threads.setTitle."""
        try:
            await self._web.api_call(
                "assistant.threads.setTitle",
                params={"channel_id": channel, "thread_ts": thread_ts, "title": title},
            )
        except Exception:
            logger.debug("assistant.threads.setTitle failed", exc_info=True)

    async def set_suggested_prompts(
        self, channel: str, thread_ts: str, prompts: list[dict[str, str]]
    ) -> None:
        """Set suggested prompts via assistant.threads.setSuggestedPrompts."""
        try:
            import json

            await self._web.api_call(
                "assistant.threads.setSuggestedPrompts",
                params={
                    "channel_id": channel,
                    "thread_ts": thread_ts,
                    "prompts": json.dumps(prompts),
                },
            )
        except Exception:
            logger.debug("assistant.threads.setSuggestedPrompts failed", exc_info=True)

    @staticmethod
    def _extract_inline_texts(elements: list[dict[str, Any]]) -> list[str]:
        """Extract text from inline rich_text elements (text, link, user, emoji, channel, usergroup)."""
        texts: list[str] = []
        for element in elements:
            inline_type = element.get("type")
            if inline_type == "text":
                texts.append(element.get("text", ""))
            elif inline_type == "link":
                texts.append(element.get("url", ""))
            elif inline_type == "user":
                texts.append(f'<@{element.get("user_id", "")}>')
            elif inline_type == "emoji":
                texts.append(f':{element.get("name", "")}:')
            elif inline_type == "channel":
                texts.append(f'<#{element.get("channel_id", "")}>')
            elif inline_type == "usergroup":
                texts.append(f'<!subteam^{element.get("usergroup_id", "")}>')
        return [t for t in texts if t]

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        """Fetch a single message's text by channel and timestamp.

        Prefers content extracted from Block Kit ``blocks`` and falls back
        to the top-level ``text`` field when blocks yield nothing.
        """
        try:
            resp = await self._web.conversations_history(
                channel=channel, oldest=ts, latest=ts, inclusive=True, limit=1
            )
            messages: list[dict[str, Any]] = resp.get("messages", [])
            if messages:
                message = messages[0]
                text = message.get("text", "")
                parts: list[str] = []
                for block in message.get("blocks", []):
                    block_type = block.get("type")
                    if block_type == "section":
                        text_obj = block.get("text")
                        if text_obj:
                            section_text = text_obj.get("text", "")
                            if section_text:
                                parts.append(section_text)
                    elif block_type == "rich_text":
                        for rich_text_element in block.get("elements", []):
                            # rich_text_list has children that are each rich_text_section;
                            # rich_text_preformatted and rich_text_quote have inline
                            # elements directly, so the else branch handles them correctly.
                            leaves = (
                                rich_text_element.get("elements", [])
                                if rich_text_element.get("type") == "rich_text_list"
                                else [rich_text_element]
                            )
                            for leaf in leaves:
                                inline_texts = self._extract_inline_texts(
                                    leaf.get("elements", [])
                                )
                                if inline_texts:
                                    parts.append("".join(inline_texts))
                return "\n".join(parts) or text or None
        except (SlackClientError, aiohttp.ClientError, asyncio.TimeoutError):
            logger.debug("fetch_message failed for %s/%s", channel, ts, exc_info=True)
        return None

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        """Fetch parent message + replies via conversations.replies API."""
        try:
            resp = await self._web.conversations_replies(
                channel=channel, ts=thread_ts, limit=limit,
            )
            data: dict = resp.data if hasattr(resp, "data") else dict(resp)  # type: ignore[assignment,call-overload]
            messages: list[dict] = data.get("messages", [])
            meta: dict = data.get("response_metadata", {})
            if warn_on_pagination and meta.get("next_cursor"):
                logger.warning(
                    "Thread %s/%s has more messages than limit=%d; import is incomplete",
                    channel, thread_ts, limit,
                )
            return messages
        except (SlackClientError, aiohttp.ClientError, asyncio.TimeoutError):
            logger.debug("fetch_thread_replies failed for %s/%s", channel, thread_ts, exc_info=True)
        return []

    async def conversations_list(self) -> list[dict]:
        """Fetch all public + private channels the bot is a member of.

        Paginates via cursor and returns a flat list of channel dicts. Each
        dict contains ``id``, ``name``, and Slack's standard channel fields.
        """
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(_CONVERSATIONS_LIST_MAX_PAGES):
            try:
                kwargs: dict[str, Any] = {
                    "types": "public_channel,private_channel",
                    "exclude_archived": True,
                    "limit": _CONVERSATIONS_LIST_PAGE_SIZE,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                resp = await self._web.conversations_list(**kwargs)
                data: dict = resp.data if hasattr(resp, "data") else dict(resp)  # type: ignore[assignment,call-overload]
                channels: list[dict] = data.get("channels", [])
                out.extend(channels)
                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    break
            except (SlackClientError, aiohttp.ClientError, asyncio.TimeoutError):
                logger.debug("conversations_list page failed", exc_info=True)
                break
        return out

    async def download_file(self, url: str, dest: str) -> None:
        """Download a Slack-hosted file using the bot token for auth."""
        headers = {"Authorization": f"Bearer {self._web.token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
