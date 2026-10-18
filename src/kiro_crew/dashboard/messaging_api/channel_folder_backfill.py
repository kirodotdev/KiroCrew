"""``POST /api/channel-folders/backfill``: file a channel's existing conversations
into its folder, for every channel namespace through one route.
"""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        CHANNEL_CONFIG_SECTIONS,
        _sel,
        backfill_channel_folder,
        is_direct_local_request,
    )


async def api_channel_folder_backfill(request: web.Request) -> web.Response:
    """POST /api/channel-folders/backfill - file a channel's EXISTING conversations.

    One endpoint for all channels rather than one per channel: the namespace
    arrives in the body and the work is byte-identical for every one of them, so
    ten copies would be ten places for the eligibility guard to drift apart.

    Loopback-only, matching the config saves it sits beside. It writes no
    credential and no config, so that is not inherited reasoning: it bulk-moves
    conversations with no collective undo, and a remote caller can neither see
    the sidebar it rearranges nor put anything back.

    Answers 200 with the report even when nothing moved, because "nothing to do"
    is a normal outcome the panel has to render (and ``reason`` says which kind
    it was). A 4xx is reserved for a request that was never actionable.
    """
    caller = request.get("user", "dashboard")

    def _deny(msg: str, code: str, status: int = 400) -> web.Response:
        # The ``code`` rides in the dict LITERAL beside the message, which is what
        # makes the body machine-readable at any status: the panel renders `error`,
        # while a caller that needs to branch reads `code` rather than matching on
        # prose that translation or rewording can change under it.
        _sel().log_api_access(
            caller=caller,
            operation="channel.folder.backfill",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg, "code": code}, status=status)

    if not is_direct_local_request(request):
        # Deliberately NOT the neighbouring panels' wording ("read-only from
        # remote sessions"). This endpoint's own button says "File existing
        # sessions", meaning chat conversations, so a refusal that says
        # "sessions" meaning LOGIN sessions puts one word for two different
        # things on one card -- a blind reader could not tell which was meant.
        #
        # It is also a whole sentence naming the remedy, not a fragment: a
        # reader who does not already know what "the local machine" is has
        # nothing to act on, which is a dead end rather than a refusal.
        #
        # And it NAMES that machine. "The computer that hosts this dashboard"
        # tells a remote reader what kind of computer to look for, not which
        # one: a blind read of that sentence recorded "I have no idea how I'd
        # find out which computer that is". The host's first DNS label is the
        # same stand-in `session_transfer.local_instance_label` uses for the
        # same reader, and the same fallback shape: a host with no name keeps
        # the description alone, never a blank or an exception on a refusal path.
        # The `code` is unchanged, so nothing machine-readable moves with this.
        try:
            host = platform.node().split(".")[0]
        except Exception:
            host = ""
        # It also states the reader's own situation first. A remote viewer IS
        # looking at a dashboard, so "open the dashboard there" read as circular
        # to a blind reader who then stopped; "this same page on <host>" tells
        # them what to do with the name.
        where = (
            f"{host}, the computer that hosts this dashboard"
            if host
            else "the computer that hosts this dashboard"
        )
        there = f"on {host}" if host else "there"
        return _deny(
            "You are viewing this page from another computer. "
            f"Filing runs only on {where}. "
            f"Open this same page {there} and click again.",
            "read_only_remote",
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _deny("body must be an object", "invalid_body")
    raw_namespace = body.get("namespace")
    if not isinstance(raw_namespace, str):
        return _deny("namespace must be text", "namespace_invalid")
    namespace = raw_namespace.strip().lower()
    # Closed set, checked here rather than left to the config read: the namespace
    # selects a config section and stamps a folder, and an unrecognised one must
    # be a refusal the caller can see, not a silently empty pass.
    if namespace not in CHANNEL_CONFIG_SECTIONS:
        return _deny("unknown channel", "unknown_channel")
    state = request.app.get("state")
    if state is None:
        return _deny("dashboard state unavailable", "state_unavailable", status=503)

    report = await backfill_channel_folder(state, namespace)
    _sel().log_api_access(
        caller=caller,
        operation="channel.folder.backfill",
        outcome="ok",
        source="dashboard",
        # The count, not the keys: a session key names a channel conversation and
        # the audit log is not the place to enumerate which ones a user filed.
        resources=f"{namespace}:{len(report['moved'])}",
    )
    return web.json_response(report)
