"""Slack API client abstraction for KiroCrew.

Provides an async interface for Slack Web API operations.
Uses an ABC so tests can swap in a mock without touching Slack.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any

import aiohttp
from slack_sdk.errors import SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# conversations.list pagination limits
_CONVERSATIONS_LIST_MAX_PAGES = 20  # 20 pages × 1000 = 20k channels max
_CONVERSATIONS_LIST_PAGE_SIZE = 1000

# A Slack file fetch is the one outbound request that carries the bot token in
# an ``Authorization`` header, and its URL is not ours: it is whatever
# ``url_private_download`` / ``url_private`` the inbound event envelope carried.
# So the host is constrained to Slack's own domain, which is the boundary that
# makes attaching the credential safe at all — anything else is a request to
# hand bot-level workspace access to a third party. Suffix-matched rather than
# an exact host set because Slack serves files from several hosts under this
# domain and varies them by install shape (Enterprise Grid included); the
# credential stays inside Slack either way.
_SLACK_FILE_DOMAIN = "slack.com"

# A download that stalls holds an ingest slot and a temp file. 60s is generous
# for the 20 MiB document ceiling in ``messaging/attachments.py`` while still
# bounded, where aiohttp's 5-minute default is not.
_FILE_DOWNLOAD_TIMEOUT_SECS = 60


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

    async def probe_channel_history(self, channel: str) -> str | None:
        """Probe whether the bot token can read *channel*'s history.

        Returns the Slack API error code when the read fails with a definite
        API error (e.g. ``missing_scope`` on an install that predates the
        ``groups:history`` scope, or ``channel_not_found`` when the token
        cannot see the channel), or None when the channel is readable or the
        outcome is undeterminable (transient network failure). Never raises.

        Default returns None (readable) — subclasses override to hit Slack.
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
        # Channel→workspace team_id cache. Populated ONLY from
        # conversations_info()'s home-workspace answer by
        # ensure_channel_team(), and used to auto-inject team_id into
        # outbound chat.* / reactions.* calls so org-wide (Enterprise Grid)
        # installs route to the correct workspace. The team on an inbound
        # event is the AUTHOR's workspace, which on a Slack Connect shared
        # channel is not the workspace the channel lives in, so it must
        # never feed this cache. Empty for non-org-wide installs —
        # _inject_team becomes a no-op in that case.
        self._channel_team: dict[str, str] = {}
        # Channels whose home team could not be resolved this process. One
        # failed lookup must not become one extra API call per inbound
        # message for the rest of the run; a gateway restart clears the set
        # and lets resolution try again.
        self._channel_team_unresolvable: set[str] = set()

    def record_channel_team(self, channel: str, team_id: str) -> None:
        """Cache the workspace team_id for a channel.  Idempotent.

        Only a CANONICAL home-team answer may enter here: outbound calls
        route by the workspace the channel lives in, so this method must be
        fed from :meth:`ensure_channel_team` (conversations_info), never
        from an inbound event's author-side ``team`` field.
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

    async def ensure_channel_team(self, channel: str) -> None:
        """Resolve a channel's HOME workspace into the routing cache.

        Inbound handlers call this once per message. conversations_info
        answers with the caller's own ``context_team_id`` for the channel,
        which is the workspace the channel lives in — the value every
        outbound ``team_id`` has to carry on an org-wide install. Recording
        the author's team instead is what flipped shared-channel caches to
        a participant's workspace and demoted replies off streaming with
        ``team_access_not_granted``.

        Best-effort: a channel that cannot be resolved simply stays out of
        the cache, which leaves outbound calls without team_id — the same
        routing Slack does for a cache miss — and lands in
        ``_channel_team_unresolvable`` so the failure costs one lookup per
        process rather than one per message.
        """
        if not channel:
            return
        cache = getattr(self, "_channel_team", None) or {}
        if channel in cache:
            return
        unresolvable = getattr(self, "_channel_team_unresolvable", None)
        if unresolvable is None:
            unresolvable = set()
            self._channel_team_unresolvable = unresolvable
        if channel in unresolvable:
            return
        try:
            resp = await self._web.conversations_info(channel=channel)
            ch = (
                resp.data.get("channel", {})  # type: ignore[union-attr]
                if hasattr(resp, "data")
                else {}
            )
            home = str(ch.get("context_team_id") or "")
        except Exception:
            logger.info(
                "could not resolve the home workspace of channel %s", channel,
                exc_info=True,
            )
            home = ""
        if home:
            self.record_channel_team(channel, home)
        else:
            unresolvable.add(channel)

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
            # home team for this channel — that's the workspace the channel
            # lives in, which is what ``body["team_id"]`` must route to.
            # Fall back to the caller-supplied recipient's team_id only when
            # the home team hasn't been resolved yet.
            cache = getattr(self, "_channel_team", None)
            cached_team = cache.get(channel) if cache else None
            workspace_team = cached_team or team_id
            if workspace_team:
                body["team_id"] = workspace_team
            # An org-wide install rejects startStream with
            # ``missing_recipient_team_id`` when the recipient's workspace is
            # unknown, so fall back to the channel's cached team: for a channel
            # message the recipient's workspace IS the channel's workspace.
            # Without the fallback every caller that does not thread an explicit
            # team_id (the renderer transport path) is rejected on each turn and
            # silently demoted to the non-streaming chat.update surface.
            recipient_team = team_id or cached_team
            if recipient_team:
                body["recipient_team_id"] = recipient_team
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

    async def probe_channel_history(self, channel: str) -> str | None:
        """Capability probe: one ``conversations.history`` call with limit=1.

        The lightest way to prove the installed token's grant can actually
        read the channel — ``conversations.info`` is not a substitute because
        reading a private channel's info needs ``groups:read``, which the same
        stale install may also lack.
        """
        kwargs: dict[str, Any] = {"channel": channel, "limit": 1}
        self._inject_team(channel, kwargs)
        try:
            await self._web.conversations_history(**kwargs)
        except (SlackClientError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            resp = getattr(exc, "response", None)
            error = ""
            if resp is not None:
                try:
                    error = str(resp.get("error", "") or "")
                except Exception:
                    error = ""
            if error:
                return error
            logger.debug("history probe failed for %s", channel, exc_info=True)
        return None

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
        """Download a Slack-hosted file using the bot token for auth.

        The URL comes from the inbound event envelope, not from us, so it is
        validated before the bot token is attached to a request for it: HTTPS,
        a host inside :data:`_SLACK_FILE_DOMAIN`, and the default port. Redirects
        are refused rather than followed, because aiohttp replays an explicitly
        set ``Authorization`` header across a redirect — so following one would
        let an allowed URL bounce a bot-level workspace credential to an
        arbitrary host, and the host check would have been true only of the hop
        that did not carry the bytes. Mirrors
        ``discord/client.py::download_attachment``, which guards its (credential
        -free) CDN fetch the same way.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid Slack file URL") from exc
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not (host == _SLACK_FILE_DOMAIN or host.endswith(f".{_SLACK_FILE_DOMAIN}"))
            or port not in (None, 443)
        ):
            raise ValueError("refusing non-Slack file URL")

        headers = {"Authorization": f"Bearer {self._web.token}"}
        timeout = aiohttp.ClientTimeout(total=_FILE_DOWNLOAD_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    raise ValueError("refusing redirected Slack file URL")
                resp.raise_for_status()
                # Off-loop: a 20 MiB document is ~2,500 write() calls, and this
                # runs on the gateway's single event loop.
                fh = await asyncio.to_thread(open, dest, "wb")
                try:
                    async for chunk in resp.content.iter_chunked(8192):
                        await asyncio.to_thread(fh.write, chunk)
                finally:
                    await asyncio.to_thread(fh.close)
